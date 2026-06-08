"""Provider-agnostic LLM call layer.

One dispatch over Anthropic / OpenAI / Gemini, each wrapped with the SDK's
retry headroom and max-token-truncation detection, plus auto-detection of
which provider is configured. Every successful call records its token usage
via :mod:`case_calendar.llmkit.usage`.

This module knows nothing about case_calendar's domain (no court prompts, no
hearing/deadline shapes) — it's the part of the LLM stack that could be lifted
out as a standalone project. Domain prompts and the high-level
extract/verify/summarize functions live in ``case_calendar.llm`` and call
:func:`_dispatch_llm_call` here.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from . import usage

logger = logging.getLogger(__name__)


class OutputTruncatedError(RuntimeError):
    """The provider stopped generation because it hit ``max_tokens``.

    The partial text is preserved on ``.partial`` so callers can log a
    useful prefix; the parsed JSON is almost always unrecoverable
    (truncation mid-string), so callers should treat this as a hard
    failure and skip the entry rather than try to parse around it.
    """

    def __init__(self, provider: str, partial: str, max_tokens: int) -> None:
        super().__init__(
            f"{provider} stopped at max_tokens={max_tokens}; "
            f"got {len(partial)} chars of partial output"
        )
        self.provider = provider
        self.partial = partial
        self.max_tokens = max_tokens


class ContextWindowExceededError(RuntimeError):
    """The prompt was too large for the model's / configured context window.

    Distinct from :class:`OutputTruncatedError` (the *output* hit
    ``max_tokens``): here the *input* didn't fit. The danger is silent — Ollama
    TRUNCATES an over-long prompt and returns a normal-looking answer built from
    a partial prompt, and the hosted providers reject the request with a
    context-length API error. Either way the result would be built from
    incomplete input, so callers convert this into a refusal (an ``IGNORE`` for
    extraction, a UNCLEAR no-op for the verify/dedupe passes, a polite "too
    large" message for summaries) rather than emit half-baked output.

    ``sent`` / ``processed`` / ``limit`` are best-effort token figures for the
    log line; any may be ``None`` when the figure isn't known (a hosted
    provider's error gives us only a message, not counts). ``detail`` carries the
    provider's own error text when there is one.
    """

    def __init__(
        self,
        provider: str,
        *,
        sent: Optional[int] = None,
        processed: Optional[int] = None,
        limit: Optional[int] = None,
        detail: str = "",
    ) -> None:
        bits = []
        if sent is not None:
            bits.append(f"sent~{sent} tok")
        if processed is not None:
            bits.append(f"processed={processed} tok")
        if limit is not None:
            bits.append(f"limit={limit} tok")
        if detail:
            bits.append(detail)
        suffix = f" ({'; '.join(bits)})" if bits else ""
        super().__init__(f"{provider} prompt exceeds the context window{suffix}")
        self.provider = provider
        self.sent = sent
        self.processed = processed
        self.limit = limit
        self.detail = detail


# Default model per provider when the caller passes none and ``LLM_MODEL`` is
# unset. The small/fast tier — this layer is used for structured extraction by
# default; callers that want a heavier model (e.g. case_calendar's summary
# track) pass ``model=`` explicitly.
_DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-5.4-nano",
    "gemini": "gemini-3.1-flash-lite",
    # Local inference via Ollama's OpenAI-compatible endpoint. The local
    # default is gemma4:e4b (a 9.6 GB download, 4.5B effective params,
    # multimodal) for BOTH tracks, so a zero-config local install pulls and
    # runs ONE model that fits a mainstream 16 GB card (12 GB at a reduced
    # window); an 8 GB card is too small for it (use the 7.2 GB gemma4:e2b
    # there, or hosted summaries). The larger gemma4:31b needs 20 GB just for
    # weights, which leaves no room for a summary-sized KV cache on a 24 GB card
    # (it OOMs / spills to RAM and crawls) — it wants a 32 GB GPU (an RTX 5090),
    # so it is the opt-in QUALITY upgrade, not the default. Operators with a
    # 32 GB+ card trade UP with
    # LLM_MODEL=gemma4:31b (or LLM_SUMMARY_MODEL=gemma4:31b to upgrade only
    # summaries); on 24 GB or less the quality path for summaries is hosted
    # (the hybrid setup), not local 31b. Local inference has no per-token cost,
    # so the one-model-for-both-tracks design holds either way. gemma4 is
    # Western-built (Google), permissively licensed, and text-capable — see
    # docs/local-llms.md. Ollama is opt-in only (no API key to auto-detect
    # from): select it with LLM_PROVIDER=ollama or a per-track override.
    "ollama": "gemma4:e4b",
}

# Every provider this layer can dispatch to. Used to validate the
# LLM_PROVIDER / LLM_*_PROVIDER env overrides before trusting them.
_KNOWN_PROVIDERS = ("anthropic", "openai", "gemini", "ollama")


# Default API-key auto-detection priority. The two tracks differ on purpose
# (see the SCORECARD): extraction prefers Gemini — it wins the provider
# comparison on accuracy AND is ~4x cheaper / ~2x faster, and the structured
# DEADLINE_SIGNIFICANCE_RULES closed the substantive-deadline-bucketing gap
# that previously held it back — while the case-summary track prefers Anthropic
# for case-distinguishing prose (statute cites, count numbers, sentence
# breakdowns). Either is overridable per-track via LLM_EXTRACTION_PROVIDER /
# LLM_SUMMARY_PROVIDER, or globally via LLM_PROVIDER.
_EXTRACTION_KEY_PRIORITY = ("gemini", "anthropic", "openai")
_SUMMARY_KEY_PRIORITY = ("anthropic", "gemini", "openai")


def _detect_provider(
    key_priority: tuple[str, ...] = _SUMMARY_KEY_PRIORITY,
) -> Optional[str]:
    """The base/global provider selection.

    Reads ``LLM_PROVIDER`` (the global default applying to both tracks) and
    otherwise falls back to API-key auto-detection in ``key_priority`` order.
    The default order is the SUMMARY-track order (``anthropic > gemini >
    openai``) — Anthropic for case-distinguishing summary prose — because the
    summary track and most direct callers want it; the extraction track passes
    the Gemini-first order via :func:`_detect_extraction_provider`.

    The per-track override env vars layer on top:
      * ``LLM_EXTRACTION_PROVIDER`` — extraction + verify-pass + dedupe, via
        :func:`_detect_extraction_provider`.
      * ``LLM_SUMMARY_PROVIDER`` — case summaries, via
        ``llm.summary_provider_info`` / ``llm.generate_docket_summary``.
    """
    provider = os.environ.get("LLM_PROVIDER", "").lower().strip()
    if provider in _KNOWN_PROVIDERS:
        return provider
    have = {
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "gemini": bool(
            os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        ),
        "openai": bool(os.environ.get("OPENAI_API_KEY")),
    }
    for p in key_priority:
        if have[p]:
            return p
    return None


def _detect_extraction_provider() -> Optional[str]:
    """The provider used for extraction + verify-pass + dedupe calls.

    Precedence:
      1. ``LLM_EXTRACTION_PROVIDER`` env var (track-specific override).
      2. ``LLM_PROVIDER`` (the global default), then API-key auto-detection in
         the EXTRACTION priority order ``gemini > anthropic > openai`` — so a
         zero-config install with multiple keys present extracts with Gemini
         (best accuracy + cheapest + fastest on the comparison), while the
         summary track still defaults to Anthropic.
    """
    extract = os.environ.get("LLM_EXTRACTION_PROVIDER", "").lower().strip()
    if extract in _KNOWN_PROVIDERS:
        return extract
    return _detect_provider(key_priority=_EXTRACTION_KEY_PRIORITY)


def _call_anthropic(
    system: str,
    user: str,
    max_tokens: int,
    *,
    model: Optional[str] = None,
    purpose: str = "llm",
    docket: Any = None,
    temperature: Optional[float] = None,
) -> str:
    import anthropic

    chosen = model or os.environ.get("LLM_MODEL", _DEFAULT_MODELS["anthropic"])
    # Bump from the SDK default of 2 retries to 8. The default gives up
    # after ~1.5s of cumulative backoff (0.5s + 1s), which is not enough
    # for the 529 Overloaded condition the API returns when capacity is
    # tight — overload events routinely last tens of seconds, so the
    # SDK gives up before the API clears and the per-entry call falls
    # through to the IGNORE-on-failure path in `extract_actions`. With
    # max_retries=8 the cumulative backoff ceiling is ~127s (0.5 + 1 +
    # 2 + 4 + 8 + 16 + 32 + 64) before honoring any Retry-After header
    # the server sends, which covers nearly every transient overload.
    # The SDK uses exponential backoff with jitter and honors
    # Retry-After, so steady-state cost is minimal — this just buys
    # headroom for the worst case.
    client = anthropic.Anthropic(timeout=120.0, max_retries=8)
    create_kwargs: dict[str, Any] = {
        "model": chosen,
        "max_tokens": max_tokens,
        "system": [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": user}],
    }
    if temperature is not None:
        create_kwargs["temperature"] = temperature
    resp = client.messages.create(**create_kwargs)
    usage.record(
        purpose=purpose,
        provider="anthropic",
        model=chosen,
        tokens=usage.from_anthropic(resp),
        docket=docket,
    )
    text: str | None = None
    for block in resp.content:
        if block.type == "text":
            text = block.text
            break
    if text is None:
        raise ValueError("No text block in Anthropic response")
    if getattr(resp, "stop_reason", None) == "max_tokens":
        raise OutputTruncatedError("anthropic", text, max_tokens)
    return text


def _call_openai(
    system: str,
    user: str,
    max_tokens: int,
    *,
    model: Optional[str] = None,
    json_mode: bool = True,
    purpose: str = "llm",
    docket: Any = None,
    temperature: Optional[float] = None,
) -> str:
    import openai

    chosen = model or os.environ.get("LLM_MODEL", _DEFAULT_MODELS["openai"])
    # See the matching comment on `_call_anthropic`: bump max_retries
    # from the SDK default of 2 to give the cumulative backoff enough
    # headroom to ride out a multi-second provider overload (~127s
    # ceiling before any Retry-After header is honored).
    client = openai.OpenAI(timeout=120.0, max_retries=8)
    kwargs: dict[str, Any] = {
        "model": chosen,
        # The gpt-5 family (our default openai tier — gpt-5.4-nano / gpt-5.4)
        # rejects the older `max_tokens` parameter with a 400
        # ``unsupported_parameter`` error and requires `max_completion_tokens`
        # instead. The newer name is the one current chat-completions models
        # accept, so we always send it.
        "max_completion_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if temperature is not None:
        kwargs["temperature"] = temperature
    resp = client.chat.completions.create(**kwargs)
    usage.record(
        purpose=purpose,
        provider="openai",
        model=chosen,
        tokens=usage.from_openai(resp),
        docket=docket,
    )
    choice = resp.choices[0]
    text = choice.message.content
    if not text:
        raise ValueError("No content in OpenAI response")
    if getattr(choice, "finish_reason", None) == "length":
        raise OutputTruncatedError("openai", text, max_tokens)
    return text


def _call_gemini(
    system: str,
    user: str,
    max_tokens: int,
    *,
    model: Optional[str] = None,
    json_mode: bool = True,
    purpose: str = "llm",
    docket: Any = None,
    temperature: Optional[float] = None,
) -> str:
    from google import genai
    from google.genai import types as gtypes

    chosen = model or os.environ.get("LLM_MODEL", _DEFAULT_MODELS["gemini"])
    client = genai.Client()
    config_kwargs: dict[str, Any] = {
        "system_instruction": system,
        "max_output_tokens": max_tokens,
    }
    if json_mode:
        config_kwargs["response_mime_type"] = "application/json"
    if temperature is not None:
        config_kwargs["temperature"] = temperature
    resp = client.models.generate_content(
        model=chosen,
        contents=user,
        config=gtypes.GenerateContentConfig(**config_kwargs),
    )
    usage.record(
        purpose=purpose,
        provider="gemini",
        model=chosen,
        tokens=usage.from_gemini(resp),
        docket=docket,
    )
    if not resp.text:
        raise ValueError("No content in Gemini response")
    # Gemini's finish_reason is an enum on the candidate; the truncation
    # value is named MAX_TOKENS. Compare by `.name` so we don't need to
    # import the enum class.
    candidates = getattr(resp, "candidates", None) or []
    if candidates:
        finish = getattr(candidates[0], "finish_reason", None)
        if getattr(finish, "name", None) == "MAX_TOKENS":
            raise OutputTruncatedError("gemini", resp.text, max_tokens)
    return resp.text


# Cache each local model's full /api/show response so the lookup runs at most
# once per (base_url, model) per process. Both `ollama_capabilities` and
# `ollama_context_window` read from this one cache, so a model is shown only
# once even though two different fields are wanted. A model name's /api/show
# data doesn't change at runtime, so the cache never needs busting; a failed
# lookup caches None so we don't retry a downed server on every call.
_OLLAMA_SHOW_CACHE: dict[tuple[str, str], Optional[dict[str, Any]]] = {}


def _ollama_show(model: str) -> Optional[dict[str, Any]]:
    """Fetch (and cache) the model's ``/api/show`` payload, or ``None`` on any
    failure (server down, old Ollama, unknown model). Shared by
    :func:`ollama_capabilities` and :func:`ollama_context_window`."""
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    cache_key = (base_url, model)
    if cache_key in _OLLAMA_SHOW_CACHE:
        return _OLLAMA_SHOW_CACHE[cache_key]

    import json
    import urllib.request

    # /api/show is Ollama's native endpoint; the OpenAI-compat base_url carries a
    # trailing /v1 that has to come off first.
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3].rstrip("/")
    data: Optional[dict[str, Any]] = None
    try:
        req = urllib.request.Request(
            root + "/api/show",
            data=json.dumps({"model": model}).encode(),
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
    except Exception:
        logger.debug(
            "ollama /api/show lookup failed for model=%s (base_url=%s); "
            "treating as unknown",
            model,
            base_url,
            exc_info=True,
        )
        data = None
    _OLLAMA_SHOW_CACHE[cache_key] = data
    return data


def ollama_capabilities(model: str) -> frozenset[str]:
    """The capabilities the local Ollama model reports via ``/api/show`` — e.g.
    ``frozenset({"completion", "tools", "thinking"})``.

    The summary track uses this to tell a *thinking* model (whose reasoning is
    drawn from the same output budget as the answer, so it needs a larger
    ``max_tokens``) from a plain instruction model (which stops at its end-of-turn
    token and would only be encouraged to over-generate by a bigger ceiling).
    Cached per ``(OLLAMA_BASE_URL, model)``.

    Returns an EMPTY set when the capability can't be determined — an Ollama too
    old to report ``capabilities``, an unreachable server, or an unknown model.
    Callers treat "unknown" conservatively (see ``llm.generate_docket_summary``):
    guessing wrong in the not-a-thinking-model direction would re-introduce the
    empty-summary failure, so unknown is handled like thinking.
    """
    data = _ollama_show(model)
    if not data:
        return frozenset()
    return frozenset(data.get("capabilities") or ())


def ollama_context_window(model: str) -> Optional[int]:
    """The model's MAXIMUM trained context length, read from ``/api/show``'s
    ``model_info`` (the ``<architecture>.context_length`` field, e.g.
    ``gemma4.context_length``). Cached per ``(OLLAMA_BASE_URL, model)`` via the
    shared :func:`_ollama_show`.

    Returns ``None`` when it can't be determined (old Ollama, unreachable
    server, unknown model, unexpected payload shape) — the caller degrades to
    the post-flight truncation backstop in :func:`_call_ollama`.

    NOTE: this is the model's architecture CEILING, which is not necessarily the
    server's runtime window. The Ollama desktop app's ``num_ctx`` can be set
    LOWER and isn't exposed through the API, so a prompt under this ceiling can
    still be truncated by a smaller runtime window. The post-flight check is the
    backstop for that case; an explicit ``OLLAMA_NUM_CTX`` (which case-calendar
    both forwards and reads) is the exact-knowledge case.
    """
    data = _ollama_show(model)
    if not data:
        return None
    info = data.get("model_info") or {}
    if not isinstance(info, dict):
        return None
    for key, val in info.items():
        if (
            key.endswith("context_length")
            and isinstance(val, int)
            and not isinstance(val, bool)
        ):
            return val
    return None


def _ollama_context_limit(model: str) -> Optional[int]:
    """The effective context window to check a prompt against, or ``None`` when
    we have no figure to check (the post-flight backstop then handles it).

    Resolution order:
      1. ``OLLAMA_NUM_CTX`` — the operator's explicit per-request window, which
         :func:`_call_ollama` also forwards as ``options.num_ctx``, so when it's
         set we know the limit EXACTLY.
      2. the model's architecture max via :func:`ollama_context_window`.
    A malformed ``OLLAMA_NUM_CTX`` falls through to the model max rather than
    crashing the call.
    """
    env = os.environ.get("OLLAMA_NUM_CTX", "").strip()
    if env:
        try:
            return int(env)
        except ValueError:
            logger.warning("OLLAMA_NUM_CTX=%r is not an integer; ignoring", env)
    return ollama_context_window(model)


# Calibration: legal prose tokenizes at roughly 3.83 chars/token on Anthropic's
# tokenizer (measured against SYSTEM_PROMPT — see AGENTS.md cache notes), and
# other tokenizers land within ~15% of that. The pre-flight guard divides by a
# slightly SMALLER number so it OVER-counts tokens and errs toward refusing a
# borderline prompt rather than letting Ollama silently truncate it; the
# post-flight comparison uses the best-estimate number.
_PREFLIGHT_CHARS_PER_TOKEN = 3.5
_ESTIMATE_CHARS_PER_TOKEN = 3.83
# Post-flight backstop when the limit is UNKNOWN (no OLLAMA_NUM_CTX and
# /api/show unavailable): only flag truncation on a GROSS shortfall between what
# we estimate we sent and what the server reports it processed, so tokenizer
# variance between our char estimate and the server's real count can't
# false-positive on a prompt that actually fit.
_UNKNOWN_LIMIT_TRUNCATION_RATIO = 1.6
_UNKNOWN_LIMIT_MIN_GAP_TOKENS = 1000


def _detect_ollama_input_truncation(
    *, processed: int, prompt_chars: int, limit: Optional[int], max_tokens: int
) -> None:
    """Raise :class:`ContextWindowExceededError` if the Ollama server appears to
    have silently truncated the prompt. Ground-truth: ``processed`` is the
    prompt-token count the server actually evaluated (post-truncation), read
    from the OpenAI-shaped ``usage``.

    - **Limit known:** truncation shows up as the server SATURATING the prompt
      budget — ``processed`` reaching ``limit - max_tokens``. A prompt that
      genuinely fit leaves headroom, so it stays below that line. This signal is
      tokenizer-independent (it doesn't depend on our char estimate at all).
    - **Limit unknown:** fall back to comparing our char-based estimate of what
      we sent against ``processed``; only fire on a gross shortfall (both the
      ratio AND an absolute-gap floor) to stay clear of tokenizer variance.

    A non-positive ``processed`` (e.g. a test double whose usage coerces to 0)
    carries no signal, so it's a no-op.
    """
    if processed <= 0:
        return
    est_sent = int(prompt_chars / _ESTIMATE_CHARS_PER_TOKEN)
    if limit is not None:
        if processed >= limit - max_tokens:
            raise ContextWindowExceededError(
                "ollama", sent=est_sent, processed=processed, limit=limit
            )
        return
    if (
        est_sent > processed * _UNKNOWN_LIMIT_TRUNCATION_RATIO
        and est_sent - processed > _UNKNOWN_LIMIT_MIN_GAP_TOKENS
    ):
        raise ContextWindowExceededError("ollama", sent=est_sent, processed=processed)


def ensure_thinking_budget(
    provider: str,
    model: Optional[str],
    requested: int,
    *,
    floor: int = 8192,
) -> int:
    """Raise a too-small output budget to ``floor`` for a "thinking" model.

    Thinking models draw their reasoning tokens from the SAME output budget as
    the answer, so a small ``requested`` ceiling can be consumed entirely by
    reasoning and leave zero answer text (an empty / ``No content`` response).
    For a non-thinking model ``requested`` is just a ceiling the answer stops
    well under, so it is returned unchanged — raising it would only give a
    rambling model room to over-generate.

    Which providers/models count as "thinking":

    - **Gemini 2.5** always — its reasoning is counted against
      ``max_output_tokens`` (and billed as output).
    - **Ollama** iff the model reports the ``thinking`` capability via
      :func:`ollama_capabilities`. An unconfirmable lookup (old Ollama, offline,
      unknown model) is treated AS thinking — the safe default, since an
      under-budgeted thinking model fails hard (empty answer) while an
      over-budgeted plain model is at worst a soft quality issue.
    - **Anthropic / OpenAI** never — they keep reasoning off the answer budget,
      stopping at the natural end of the response regardless of the ceiling.

    This lives in llmkit, not the domain layer, because it is purely about how a
    provider/model spends its output budget — independent of what the call is for.
    """
    if provider == "gemini":
        thinking = True
    elif provider == "ollama":
        caps = ollama_capabilities(model) if model else frozenset()
        thinking = (not caps) or ("thinking" in caps)
    else:
        thinking = False
    return max(requested, floor) if thinking else requested


def _call_ollama(
    system: str,
    user: str,
    max_tokens: int,
    *,
    model: Optional[str] = None,
    json_mode: bool = True,
    purpose: str = "llm",
    docket: Any = None,
    temperature: Optional[float] = None,
) -> str:
    """Local inference through Ollama's OpenAI-compatible endpoint.

    Ollama serves an OpenAI-shaped ``/v1/chat/completions`` API, so we reuse
    the ``openai`` SDK pointed at the local server (``OLLAMA_BASE_URL``,
    default ``http://localhost:11434/v1``) with a throwaway key — Ollama
    ignores the key but the SDK requires a non-empty one. The response carries
    OpenAI-shaped ``usage`` and ``finish_reason``, so token telemetry and
    truncation detection go through the same ``from_openai`` / ``length`` paths
    as the hosted OpenAI provider.

    Three deliberate differences from :func:`_call_openai`:

    - **``max_tokens``, not ``max_completion_tokens``.** The latter is a
      gpt-5-family requirement; Ollama's endpoint expects the classic name.
    - **A local ``base_url`` + dummy key**, so nothing leaves the machine.
    - **Optional ``num_ctx``.** Local models default to a small context window
      (often 4K–8K tokens) and SILENTLY TRUNCATE longer prompts — which the
      summary track (tens of thousands of tokens of legal prose) will hit.
      When ``OLLAMA_NUM_CTX`` is set we pass it through ``extra_body`` so the
      server allocates a larger window. It is opt-in because the OpenAI-compat
      passthrough of ``options`` is Ollama-version-dependent; the guaranteed
      way to raise the window is a Modelfile ``PARAMETER num_ctx`` (see
      docs/local-llms.md). Left unset, we send a vanilla request that any
      Ollama version accepts.
    """
    import openai

    chosen = model or os.environ.get("LLM_MODEL", _DEFAULT_MODELS["ollama"])

    # Pre-flight: refuse a prompt that won't fit BEFORE spending a (possibly
    # multi-minute) local generation that Ollama would build from a silently
    # truncated prompt. When we can't learn the window, we fall through and let
    # the post-flight check below catch any truncation from the real token count.
    limit = _ollama_context_limit(chosen)
    prompt_chars = len(system) + len(user)
    if limit is not None:
        est_prompt = int(prompt_chars / _PREFLIGHT_CHARS_PER_TOKEN) + 1
        if est_prompt + max_tokens > limit:
            raise ContextWindowExceededError(
                "ollama",
                sent=est_prompt,
                limit=limit,
                detail=f"reserving max_tokens={max_tokens} for output",
            )

    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    # Ollama ignores the key, but the SDK refuses to construct without one.
    api_key = os.environ.get("OLLAMA_API_KEY", "ollama")
    client = openai.OpenAI(base_url=base_url, api_key=api_key, timeout=600.0)
    kwargs: dict[str, Any] = {
        "model": chosen,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if temperature is not None:
        kwargs["temperature"] = temperature
    num_ctx = os.environ.get("OLLAMA_NUM_CTX", "").strip()
    if num_ctx:
        # Ollama reads model runtime options from a top-level `options` object;
        # the OpenAI SDK forwards unknown body fields via `extra_body`.
        kwargs["extra_body"] = {"options": {"num_ctx": int(num_ctx)}}
    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as exc:
        # A configured context window the hardware can't hold (e.g. a 256K
        # num_ctx set in the Ollama desktop app on a GPU that can't fit the KV
        # cache) surfaces here as a memory-allocation failure, NOT a
        # context-length error. Log a clear, operator-actionable hint (the
        # remedy is to LOWER the window, the opposite of the too-big-prompt
        # case) and re-raise unchanged so the caller's existing fail-safe runs
        # (extraction -> IGNORE, verify/dedupe -> UNCLEAR, summary -> left
        # stale to retry). We do NOT turn this into ContextWindowExceededError:
        # that would tell the operator to RAISE the window, making it worse.
        if _is_memory_error(exc):
            logger.warning(
                "Ollama could not allocate memory for model=%s at context "
                "window=%s tok — the hardware likely can't hold the KV cache "
                "for the configured num_ctx (OLLAMA_NUM_CTX / the Ollama "
                "desktop context setting). LOWER the context window or free "
                "GPU/system RAM. Error: %s",
                chosen,
                limit,
                str(exc)[:300],
            )
        raise
    tok = usage.from_openai(resp)
    usage.record(
        purpose=purpose,
        provider="ollama",
        model=chosen,
        tokens=tok,
        docket=docket,
    )
    # Post-flight backstop: the server reports how many prompt tokens it
    # actually evaluated. If it silently truncated an over-long prompt (the
    # desktop num_ctx is lower than the model max we checked pre-flight, or we
    # had no limit to check), this raises rather than return an answer built
    # from a partial prompt.
    _detect_ollama_input_truncation(
        processed=tok.input,
        prompt_chars=prompt_chars,
        limit=limit,
        max_tokens=max_tokens,
    )
    choice = resp.choices[0]
    text = choice.message.content
    if not text:
        raise ValueError("No content in Ollama response")
    if getattr(choice, "finish_reason", None) == "length":
        raise OutputTruncatedError("ollama", text, max_tokens)
    return text


def _dispatch_llm_call(
    provider: str,
    system: str,
    user: str,
    max_tokens: int,
    *,
    model: Optional[str] = None,
    json_mode: bool = True,
    purpose: str = "llm",
    docket: Any = None,
    temperature: Optional[float] = None,
) -> str:
    """Route to the per-provider call function by ``provider`` name.

    Single home for the ``anthropic | openai | gemini | ollama``
    dispatch. The per-provider functions still own their SDK quirks
    (truncation signal detection, json-mode kwargs, model-default
    selection); this helper just picks which one to call so callers don't
    have to rewrite the if/elif/else when another provider is added or a
    kwarg shape shifts. ``OutputTruncatedError`` and any other exceptions
    propagate unchanged so callers can convert them into their own
    caller-specific fallback shape (IGNORE list vs UNCLEAR dict vs raise).

    ``temperature`` is a single optional knob in [0, 2]: when ``None``
    (the default) each provider's SDK default is used unchanged
    (currently 1.0 across all three); when set, the value is forwarded
    to whatever per-provider parameter that SDK names for it (Anthropic
    ``temperature``, OpenAI ``temperature``, Gemini
    ``GenerateContentConfig.temperature``). The intent is one common
    knob for "how stochastic should this call be" — pinning to ``0.0``
    is what ``case_calendar.llm`` uses for every domain call so
    extraction / verify / dedupe / summary decisions don't depend on
    sampling variance across syncs.

    A hosted provider's "context length exceeded" error is normalized to
    :class:`ContextWindowExceededError` here (matched on the SDK error
    message — see :func:`_is_context_length_error`), so a too-large prompt
    reads the same to callers regardless of provider: Ollama raises it from
    its own pre/post-flight checks, and the hosted SDKs' 400s are converted
    to it here. Callers then turn it into a refusal instead of crashing.
    """
    try:
        if provider == "anthropic":
            # Anthropic has no `json_mode` knob (no JSON mode flag in the
            # SDK; we just rely on the prompt and validate the response).
            return _call_anthropic(
                system,
                user,
                max_tokens,
                model=model,
                purpose=purpose,
                docket=docket,
                temperature=temperature,
            )
        if provider == "openai":
            return _call_openai(
                system,
                user,
                max_tokens,
                model=model,
                json_mode=json_mode,
                purpose=purpose,
                docket=docket,
                temperature=temperature,
            )
        if provider == "ollama":
            return _call_ollama(
                system,
                user,
                max_tokens,
                model=model,
                json_mode=json_mode,
                purpose=purpose,
                docket=docket,
                temperature=temperature,
            )
        return _call_gemini(
            system,
            user,
            max_tokens,
            model=model,
            json_mode=json_mode,
            purpose=purpose,
            docket=docket,
            temperature=temperature,
        )
    except (ContextWindowExceededError, OutputTruncatedError):
        # Already in the shape callers expect — pass through unchanged.
        raise
    except Exception as exc:
        # A hosted provider rejects an over-long prompt with a context-length
        # 400 rather than truncating silently; convert it so callers handle
        # over-context uniformly. Anything else propagates unchanged.
        if _is_context_length_error(exc):
            raise ContextWindowExceededError(provider, detail=str(exc)[:300]) from exc
        raise


# Substrings (lowercased) that mark a provider's "the prompt is too big for the
# model's context window" error. Curated from each SDK's actual message rather
# than matching on error class, so the check stays provider-agnostic and needs
# no SDK imports: OpenAI says "maximum context length" + code
# `context_length_exceeded`; Anthropic says "prompt is too long"; Gemini says
# "input token count ... exceeds the maximum number of tokens".
_CONTEXT_ERROR_MARKERS = (
    "context length",
    "context_length_exceeded",
    "maximum context",
    "context window",
    "prompt is too long",
    "input token count",
    "exceeds the maximum number of tokens",
    "too many input tokens",
    "reduce the length of the messages",
)


def _is_context_length_error(exc: Exception) -> bool:
    """True when an SDK exception is a context-length / prompt-too-long error,
    matched on its message text (see :data:`_CONTEXT_ERROR_MARKERS`)."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _CONTEXT_ERROR_MARKERS)


# Substrings (lowercased) marking an Ollama "couldn't allocate memory for this
# context window" failure — the hardware can't hold the KV cache for the
# configured num_ctx (e.g. an operator set a 256K window on a GPU that can't
# fit it). This is the OPPOSITE problem from ContextWindowExceededError (the
# prompt may be tiny) with the OPPOSITE remedy (LOWER num_ctx / free RAM, not
# raise it), so it is deliberately NOT converted to ContextWindowExceededError
# — see `_call_ollama`, which only logs a clearer operator hint and re-raises
# so the call still fails safe.
_MEMORY_ERROR_MARKERS = (
    "out of memory",
    "cudamalloc",
    "failed to allocate",
    "requires more system memory",
    "not enough memory",
    "insufficient memory",
)


def _is_memory_error(exc: Exception) -> bool:
    """True when an exception looks like an Ollama out-of-memory / can't-allocate
    failure (see :data:`_MEMORY_ERROR_MARKERS`)."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _MEMORY_ERROR_MARKERS)


def provider_info() -> str:
    """One-line ``provider=… model=…`` for the extraction track, or a
    ``no provider configured`` notice. Reflects ``LLM_EXTRACTION_PROVIDER``
    > ``LLM_PROVIDER`` for the provider, plus ``LLM_MODEL`` and the
    per-provider defaults for the model."""
    p = _detect_extraction_provider()
    if p is None:
        return "no provider configured"
    model = os.environ.get("LLM_MODEL", _DEFAULT_MODELS[p])
    return f"provider={p} model={model}"
