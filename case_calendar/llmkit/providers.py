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

import json
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
    # Local inference via Ollama's OpenAI-compatible endpoint. The local default
    # is gpt-oss:20b for BOTH tracks — chosen on measured benchmark accuracy, not
    # only hardware fit: it is the best LOCAL extractor in the comparison
    # (deviation 710, beating the prior gemma4:e4b default's 1241 AND three
    # hosted models; 2nd overall behind only Gemini — see
    # model-comparison/SCORECARD.md), and the cleanest local summarizer (no
    # verdict fabrication or token leaks). It is also STABLE where most local
    # thinking models are not: its reasoning is LEVEL-based (inherently bounded),
    # so it completes cleanly, whereas the UNBOUNDED boolean-thinkers (qwen3,
    # mistral, glm, granite) ran away / timed out on the same benchmark. It is
    # OpenAI open-weights — Western-built and clean for adversary-nation cases.
    # Size trade vs the older gemma4:e4b default: 13.8 GB (MXFP4) vs 9.6 GB.
    # EXTRACTION (short context) still fits a 16 GB card; SUMMARIES at a large
    # context window want ~24 GB — on a 16 GB card, lower OLLAMA_NUM_CTX or send
    # summaries to a hosted provider (the hybrid setup). gemma4:e4b remains the
    # smaller-card fallback (LLM_MODEL=gemma4:e4b). Ollama is opt-in only (no API
    # key to auto-detect from): select it with LLM_PROVIDER=ollama or a per-track
    # override. See docs/local-llms.md.
    "ollama": "gpt-oss:20b",
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


# --- Structured output (schema-enforced JSON) ---------------------------------
# Generic, domain-free: a caller passes a ``schema`` (a JSON Schema dict) and
# each provider enforces it server-side. OpenAI, Gemini, and Ollama take a JSON
# Schema natively; for Anthropic we force a single tool call whose
# ``input_schema`` IS the schema and read the structured args back (best-effort —
# Anthropic's ``strict: true`` guarantee, GA 2026, rejects ACTIONS_SCHEMA as
# "too complex"; see _call_anthropic). ``schema`` overrides ``json_mode`` (a
# schema already implies JSON). The
# tool name is generic so this stays liftable into a standalone llmkit — the
# library never needs to know what the schema means; the consumer supplies it.
_STRUCTURED_OUTPUT_NAME = "structured_output"


def _to_openai_strict_schema(schema: Any) -> Any:
    """Transform a schema into OpenAI strict-mode form. OpenAI's strict mode
    requires every object to set ``additionalProperties: false`` AND list EVERY
    declared property in ``required`` (a 400 otherwise — verified empirically).
    A schema that is minimal-required (only the discriminator) for the other
    providers is filled out here: every object node gets ``required`` = all of
    its ``properties`` keys and ``additionalProperties: false``. Pure / recursive
    — returns NEW nodes, never mutates the input (the canonical ACTIONS_SCHEMA is
    shared across providers and is deliberately minimal-required for them)."""
    if isinstance(schema, list):
        return [_to_openai_strict_schema(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: _to_openai_strict_schema(v) for k, v in schema.items()}
    if out.get("type") == "object" and isinstance(out.get("properties"), dict):
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    return out


def _openai_json_schema_format(schema: dict[str, Any]) -> dict[str, Any]:
    """OpenAI-style ``response_format`` that pins output to a JSON Schema. Shared
    by the OpenAI SDK and OpenAI-compatible local servers (LM Studio / vLLM /
    llama.cpp) that implement the same field.

    ``strict`` is True — hard grammar-enforced. OpenAI's strict mode requires a
    CLOSED schema with every property in ``required``; an under-specified schema
    is rejected 400 ("additionalProperties is required ... to be false") —
    verified empirically. ``_to_openai_strict_schema`` fills that in, so a
    canonical minimal-required schema (what the OTHER providers take, to avoid
    the all-required recall hit) is accepted here too."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": _STRUCTURED_OUTPUT_NAME,
            "schema": _to_openai_strict_schema(schema),
            "strict": True,
        },
    }


def _to_gemini_schema(schema: Any) -> Any:
    """Transform a canonical JSON-Schema dict into Gemini's ``response_schema``
    dialect (an OpenAPI 3.0 subset). Two rewrites: a nullable-union
    ``{"type": [<t>, "null"]}`` becomes ``{"type": <t>, "nullable": true}``
    (Gemini's ``type`` is a single value, never a union), and
    ``additionalProperties`` is dropped (Gemini's schema has no such key and
    rejects it). Pure and recursive — returns NEW nodes, never mutates the input,
    because the canonical ACTIONS_SCHEMA is shared across providers. ``enum`` /
    ``required`` / ``properties`` / ``items`` pass through (recursively), so a
    schema with no unions and no ``additionalProperties`` round-trips to an equal
    dict."""
    if isinstance(schema, list):
        return [_to_gemini_schema(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "additionalProperties":
            continue
        if key == "type" and isinstance(value, list):
            non_null = [t for t in value if t != "null"]
            out["type"] = non_null[0] if non_null else "string"
            if "null" in value:
                out["nullable"] = True
            continue
        out[key] = _to_gemini_schema(value)
    return out


def _call_anthropic(
    system: str,
    user: str,
    max_tokens: int,
    *,
    model: Optional[str] = None,
    schema: Optional[dict[str, Any]] = None,
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
    if schema is not None:
        # Structured output via a FORCED single tool call whose input_schema IS
        # the schema; the model returns the result as that tool's args, read back
        # below. We deliberately do NOT set Anthropic's ``strict: true`` (its
        # native structured-output guarantee, GA 2026): live checks on 2026-06-11
        # returned 400 for ACTIONS_SCHEMA under strict ("Schema is too complex",
        # or "Grammar compilation timed out" after ~3 minutes) even though the
        # schema sits within every documented strict limit — 16 union-typed
        # params exactly AT the 16 cap, 16 optional params under the 24 cap.
        # Anthropic support confirmed the cause: an internal compiled-grammar
        # complexity ceiling beyond the documented caps, dominated by OPTIONAL
        # parameters (each one roughly doubles part of the grammar state space).
        # Measured the same day: the all-required variant (16 unions, 0 optional
        # — exactly what _to_openai_strict_schema produces) compiles and works,
        # while minimal-required fails even at 14 unions (a hearing-only split)
        # and only compiles at 9 (deadline-only). We stay on plain forced
        # tool-use anyway: all-required is the exact shape that halved Gemini's
        # recall in the benchmark and it inflates output tokens (every action
        # emits all 17 fields, mostly null), and splitting the schema doubles
        # per-entry calls and breaks the single hearing-vs-deadline decision (a
        # forced deadline-only probe relabeled a sentencing hearing
        # ADD_DEADLINE). Plain forced tool-use handles this schema and was
        # validation-clean (0 degenerate) in the benchmark. Do NOT re-add
        # ``strict: true`` as-is; revisit only with a re-benchmark of the
        # all-required form or after Anthropic raises the compilation ceiling.
        create_kwargs["tools"] = [
            {
                "name": _STRUCTURED_OUTPUT_NAME,
                "description": "Return the result as structured data per the schema.",
                "input_schema": schema,
            }
        ]
        create_kwargs["tool_choice"] = {"type": "tool", "name": _STRUCTURED_OUTPUT_NAME}
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
    truncated = getattr(resp, "stop_reason", None) == "max_tokens"
    if schema is not None:
        # Read the forced tool call's args (a dict) and return them as a JSON
        # string, so the caller parses identically to the other providers.
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                out = json.dumps(getattr(block, "input", {}))
                if truncated:
                    raise OutputTruncatedError("anthropic", out, max_tokens)
                return out
        raise ValueError("No tool_use block in Anthropic response")
    text: str | None = None
    for block in resp.content:
        if block.type == "text":
            text = block.text
            break
    if text is None:
        raise ValueError("No text block in Anthropic response")
    if truncated:
        raise OutputTruncatedError("anthropic", text, max_tokens)
    return text


def _call_openai(
    system: str,
    user: str,
    max_tokens: int,
    *,
    model: Optional[str] = None,
    json_mode: bool = True,
    schema: Optional[dict[str, Any]] = None,
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
    # A schema pins output to that JSON Schema (server-enforced); plain json_mode
    # only guarantees *valid* JSON. schema wins when both are set.
    if schema is not None:
        kwargs["response_format"] = _openai_json_schema_format(schema)
    elif json_mode:
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
    schema: Optional[dict[str, Any]] = None,
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
    # response_schema pins the JSON shape; response_mime_type alone only forces
    # valid JSON. Gemini takes a JSON Schema dict directly.
    if schema is not None:
        config_kwargs["response_mime_type"] = "application/json"
        config_kwargs["response_schema"] = _to_gemini_schema(schema)
    elif json_mode:
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
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    cache_key = (base_url, model)
    if cache_key in _OLLAMA_SHOW_CACHE:
        return _OLLAMA_SHOW_CACHE[cache_key]

    import json
    import urllib.request

    # /api/show is Ollama's native endpoint at the host root; strip a trailing
    # /v1 if the operator left the OpenAI-compatible path on OLLAMA_BASE_URL.
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


# OpenAI reasoning models. The gpt-5 family reasons by DEFAULT, as do the
# o-series; their reasoning tokens count toward ``max_completion_tokens`` and are
# billed as output (https://developers.openai.com/api/docs/guides/reasoning), so
# a small ceiling can be consumed entirely by reasoning before any answer is
# emitted — the same starve-the-answer failure as Gemini / local thinking models.
# Detected by name prefix; a non-reasoning OpenAI model (gpt-4o, etc.) is left at
# its requested budget.
_OPENAI_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def _openai_is_reasoning_model(model: Optional[str]) -> bool:
    name = (model or _DEFAULT_MODELS.get("openai") or "").lower()
    return name.startswith(_OPENAI_REASONING_PREFIXES)


def ensure_thinking_budget(
    provider: str,
    model: Optional[str],
    requested: int,
    *,
    floor: int = 25000,
) -> int:
    """Raise a too-small output budget to ``floor`` for a "thinking" model.

    Thinking models draw their reasoning tokens from the SAME output budget as
    the answer, so a small ``requested`` ceiling can be consumed entirely by
    reasoning and leave zero answer text (an empty / ``No content`` response).
    For a non-thinking model ``requested`` is just a ceiling the answer stops
    well under, so it is returned unchanged — raising it would only give a
    rambling model room to over-generate. The floor matches OpenAI's published
    guidance to reserve at least 25,000 tokens for reasoning + output; it's only
    ever a CEILING (the model stops when done), so it costs nothing on a model
    that reasons lightly and prevents truncation on one that reasons a lot.

    Which providers/models count as "thinking":

    - **Gemini 2.5** always — its reasoning is counted against
      ``max_output_tokens`` (and billed as output).
    - **OpenAI** iff the model is a reasoning model (gpt-5 family / o-series, per
      :func:`_openai_is_reasoning_model`) — gpt-5 reasons by default and the
      reasoning counts toward ``max_completion_tokens``, so a small ceiling
      starves the answer exactly as Gemini does. A non-reasoning OpenAI model is
      left unchanged.
    - **Ollama** iff the model reports the ``thinking`` capability via
      :func:`ollama_capabilities`. An unconfirmable lookup (old Ollama, offline,
      unknown model) is treated AS thinking — the safe default, since an
      under-budgeted thinking model fails hard (empty answer) while an
      over-budgeted plain model is at worst a soft quality issue.
    - **Anthropic** never — extended thinking is opt-in and we don't enable it,
      so Claude answers directly and the ceiling bounds only the answer.

    This lives in llmkit, not the domain layer, because it is purely about how a
    provider/model spends its output budget — independent of what the call is for.
    """
    if provider == "gemini":
        thinking = True
    elif provider == "openai":
        thinking = _openai_is_reasoning_model(model)
    elif provider == "ollama":
        caps = ollama_capabilities(model) if model else frozenset()
        thinking = (not caps) or ("thinking" in caps)
    else:
        thinking = False
    return max(requested, floor) if thinking else requested


def _ollama_native_base() -> str:
    """Native API base URL derived from ``OLLAMA_BASE_URL``.

    ``OLLAMA_BASE_URL`` is the host root (e.g. ``http://localhost:11434``), but an
    operator may leave a trailing ``/v1`` (the OpenAI-compatible path) on it;
    per-request thinking control (the ``think`` field) is only available on
    Ollama's NATIVE ``/api/chat`` endpoint, which lives at the host root — so
    strip a trailing ``/v1`` segment. Default ``http://localhost:11434``.
    """
    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base.rstrip("/")


def _ollama_chat_request(
    body: dict[str, Any], *, timeout: float = 600.0
) -> dict[str, Any]:
    """POST ``body`` to Ollama's native ``/api/chat`` and return the parsed JSON.

    Isolated as its own function so the HTTP client choice (stdlib ``urllib`` —
    no extra dependency for the otherwise SDK-only llmkit) lives in one place,
    and tests have a clean seam to monkeypatch instead of faking a transport.
    """
    import urllib.request

    req = urllib.request.Request(
        f"{_ollama_native_base()}/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _http_error_detail(exc: Exception) -> str:
    """Best-effort human-readable detail for an exception from
    :func:`_ollama_chat_request`.

    For a urllib ``HTTPError`` the useful text (e.g. an out-of-memory message)
    is in the response BODY, not ``str(exc)``, so read it; otherwise fall back
    to ``str(exc)``.
    """
    read = getattr(exc, "read", None)
    if callable(read):
        try:
            raw = read()
            body = (
                raw.decode("utf-8", "ignore")
                if isinstance(raw, (bytes, bytearray))
                else str(raw)
            )
        except Exception:  # noqa: BLE001 — detail extraction must never raise
            body = ""
        if body:
            return body
    return str(exc)


# Models whose reasoning is tuned by a LEVEL ("low" / "medium" / "high") and
# CANNOT be turned off with think=false — Ollama ignores a boolean for these and
# the model always emits a reasoning trace. gpt-oss is the documented case
# (https://docs.ollama.com/capabilities/thinking: "GPT-OSS requires think to be
# set to low, medium, or high. Passing true/false is ignored for that model").
# For these we pick the SHORTEST level on the high-volume tracks (extract / verify
# / dedupe) and a deeper level for summaries, and ALWAYS budget output room for
# the trace plus the answer — sending think=false would be a no-op, the trace
# would eat the short max_tokens budget, and the call would come back empty (the
# qwen3 "No content" failure, but unavoidable here since the trace can't be
# disabled). Matched as a substring of the model name so tags like
# "gpt-oss:20b" / "gpt-oss:120b" all resolve.
_OLLAMA_LEVEL_THINKING_MODELS = ("gpt-oss",)


def _ollama_requires_thinking_level(model: str) -> bool:
    """True when the model's reasoning is level-based and can't be disabled with
    a boolean ``think`` (gpt-oss family) — see ``_OLLAMA_LEVEL_THINKING_MODELS``."""
    lowered = model.lower()
    return any(name in lowered for name in _OLLAMA_LEVEL_THINKING_MODELS)


# Reasoning headroom (in tokens) added ON TOP of max_tokens for a thinking
# model's output budget, so the budget covers the answer (max_tokens) PLUS a
# bounded reasoning trace. This replaces an earlier UNBOUNDED budget
# (``num_predict = -1``), which let runaway-prone models keep generating until
# they filled the whole context window — surfacing as request timeouts or empty
# "No content" responses, NOT the clean truncation the unbounded design assumed.
# A bounded headroom makes a model that won't stop truncate here
# (-> :class:`OutputTruncatedError` -> the caller skips the item) in seconds
# rather than hanging. The default is deliberately generous: across a
# structured-output extraction benchmark the disciplined thinkers' natural
# trace+answer length stayed well under it (gemma ≤ ~3.3K tokens, gpt-oss
# ≤ ~5.2K), so they are unaffected, while the models that ran away (qwen3,
# mistral, glm, granite) are bounded. It is tuned for structured / short-answer
# thinking; a long-form-generation consumer should raise ``OLLAMA_THINK_BUDGET``.
# Because the cap raises ``OutputTruncatedError`` (which the caller logs), a
# caseload that genuinely needs a longer trace reports itself rather than
# failing silently.
_DEFAULT_OLLAMA_THINK_BUDGET = 8192


def _ollama_think_budget() -> int:
    """Reasoning headroom in tokens added to ``max_tokens`` for a thinking model
    (see :data:`_DEFAULT_OLLAMA_THINK_BUDGET`). ``OLLAMA_THINK_BUDGET`` overrides
    the default; a missing or malformed value falls back to it. A non-negative
    integer is required — ``0`` means "no headroom" (cap the trace at
    ``max_tokens``)."""
    raw = os.environ.get("OLLAMA_THINK_BUDGET", "").strip()
    if raw:
        try:
            v = int(raw)
        except ValueError:
            logger.warning(
                "OLLAMA_THINK_BUDGET=%r is not an integer; using default %d",
                raw,
                _DEFAULT_OLLAMA_THINK_BUDGET,
            )
        else:
            if v >= 0:
                return v
            logger.warning(
                "OLLAMA_THINK_BUDGET=%r is negative; using default %d",
                raw,
                _DEFAULT_OLLAMA_THINK_BUDGET,
            )
    return _DEFAULT_OLLAMA_THINK_BUDGET


# Reasoning LEVELS for a level-thinking model (gpt-oss family). Unlike the
# boolean thinkers, gpt-oss can't have reasoning disabled — only tuned by level.
_OLLAMA_VALID_LEVELS = ("low", "medium", "high")


def _ollama_think_level(purpose: str) -> str:
    """Reasoning LEVEL ("low" / "medium" / "high") for a level-thinking model
    (gpt-oss family — see :func:`_ollama_requires_thinking_level`). The default
    picks the deepest level for the synthesis-heavy summary track and the
    shortest for the high-volume extract / verify / dedupe tracks (gpt-oss's
    "high" trace is too slow for per-entry work). ``OLLAMA_THINK_LEVEL``
    overrides BOTH with an explicit level — the operator knob for tuning
    reasoning depth, and the control the level-sweep benchmark uses to compare
    low / medium / high on one track. A missing or malformed value falls back to
    the per-track default."""
    override = os.environ.get("OLLAMA_THINK_LEVEL", "").strip().lower()
    if override:
        if override in _OLLAMA_VALID_LEVELS:
            return override
        logger.warning(
            "OLLAMA_THINK_LEVEL=%r is not one of %s; using the per-track default",
            override,
            ", ".join(_OLLAMA_VALID_LEVELS),
        )
    return "high" if purpose == "summary" else "low"


def _log_ollama_memory_hint(chosen: str, limit: Optional[int], detail: str) -> None:
    """Operator-actionable hint when Ollama can't allocate memory for the
    configured context window. The remedy is to LOWER the window (the opposite
    of the too-big-prompt case), so we must NOT surface this as a
    ContextWindowExceededError."""
    logger.warning(
        "Ollama could not allocate memory for model=%s at context window=%s "
        "tok — the hardware likely can't hold the KV cache for the configured "
        "num_ctx (OLLAMA_NUM_CTX / the Ollama desktop context setting). LOWER "
        "the context window or free GPU/system RAM. Error: %s",
        chosen,
        limit,
        detail[:300],
    )


def _call_ollama(
    system: str,
    user: str,
    max_tokens: int,
    *,
    model: Optional[str] = None,
    json_mode: bool = True,
    schema: Optional[dict[str, Any]] = None,
    purpose: str = "llm",
    docket: Any = None,
    temperature: Optional[float] = None,
) -> str:
    """Local inference for the ``ollama`` provider — routes to one of two
    backends, chosen automatically:

    - **Real Ollama → native ``/api/chat``** (:func:`_call_ollama_native`),
      the ONLY endpoint exposing per-request **thinking control** (the ``think``
      field). The OpenAI-compatible ``/v1`` endpoint ignores ``think`` /
      ``chat_template_kwargs`` (verified empirically), so a thinking model there
      spends its whole output budget reasoning on a dense prompt — an empty
      ``No content`` answer, 1-2 minutes per call. See docs/local-llms.md.
    - **A generic OpenAI-compatible server → ``/v1/chat/completions``**
      (:func:`_call_ollama_openai_compat`). LM Studio, vLLM, and llama.cpp's
      server speak the OpenAI API but have no ``/api/chat``, so they keep the
      original endpoint (no thinking control — there's no mechanism for it
      there, the same as before the thinking-control feature).

    Detection: Ollama's ``/api/show`` exists only on Ollama, so a successful
    lookup is the signal that the native path — and thinking control — is
    available. ``OLLAMA_NUM_CTX`` widens the context window on either backend.
    """
    chosen = model or os.environ.get("LLM_MODEL", _DEFAULT_MODELS["ollama"])

    # Pre-flight: refuse a prompt that won't fit BEFORE spending a (possibly
    # multi-minute) local generation the server would build from a silently
    # truncated prompt. When we can't learn the window, we fall through and let
    # the post-flight check catch any truncation from the real token count.
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

    # OLLAMA_USE_OPENAI_COMPAT forces the OpenAI-compatible `/v1` backend even on
    # real Ollama (where `/api/show` would otherwise select the native path).
    # This gives up thinking control, so it's NOT for thinking models that need
    # it — it exists for parity / A-B diagnostics against the native path and for
    # operators who prefer the `/v1` endpoint. Any non-empty value enables it.
    if os.environ.get("OLLAMA_USE_OPENAI_COMPAT", "").strip():
        backend = _call_ollama_openai_compat
    else:
        backend = (
            _call_ollama_native
            if _ollama_show(chosen) is not None
            else _call_ollama_openai_compat
        )
    return backend(
        system,
        user,
        max_tokens,
        chosen=chosen,
        json_mode=json_mode,
        schema=schema,
        purpose=purpose,
        docket=docket,
        temperature=temperature,
        limit=limit,
        prompt_chars=prompt_chars,
    )


def _call_ollama_native(
    system: str,
    user: str,
    max_tokens: int,
    *,
    chosen: str,
    json_mode: bool,
    schema: Optional[dict[str, Any]] = None,
    purpose: str,
    docket: Any,
    temperature: Optional[float],
    limit: Optional[int],
    prompt_chars: int,
) -> str:
    """Native ``/api/chat`` call with per-track thinking control.

    Thinking policy (for models reporting the ``thinking`` capability —
    :func:`ollama_capabilities`): the model THINKS on every track, with a BOUNDED
    output budget of ``max_tokens + _ollama_think_budget()`` (the answer
    allowance plus a reasoning headroom, default 8192, ``OLLAMA_THINK_BUDGET``-
    overridable). This replaced an earlier ``num_predict = -1`` (unbounded) that
    let runaway-prone models generate until the request timed out. Suppressing
    reasoning entirely is still wrong (an earlier ``think = false`` on the
    high-volume tracks made weaker models re-emit the KNOWN hearings/deadlines
    they were shown as spurious actions — that's what ``OLLAMA_FORCE_NO_THINK``
    is for), but the bound keeps a non-stopping model truncating cleanly. gpt-oss
    is the one exception: its reasoning can't be turned off, only tuned by LEVEL,
    so it gets the shortest level on the high-volume tracks (its deepest is too
    slow there) and the deepest on summaries.

    Non-thinking models are sent a plain request (no ``think`` field). Hosted
    thinking (Gemini) is handled separately by :func:`ensure_thinking_budget`.
    """
    # Thinking decision. An unconfirmable capability lookup is treated AS
    # thinking (the safe default — letting a plain model think is a harmless
    # no-op, whereas wrongly SUPPRESSING a thinker's reasoning is what made
    # weaker models re-emit the KNOWN context they were shown).
    caps = ollama_capabilities(chosen)
    is_thinking = (not caps) or ("thinking" in caps)
    # Escape hatch for a thinking model whose UNBOUNDED reasoning runs away
    # (never emits a stop token, generates until the request times out) or is
    # simply too slow to be practical. Setting OLLAMA_FORCE_NO_THINK to any
    # non-empty value sends an explicit think=false and keeps the output budget
    # bounded at max_tokens, so the model answers without a reasoning trace. It
    # is OFF by default (the shipped policy is reasoning ON — suppressing it made
    # weaker models re-emit their known context as spurious actions), and it is a
    # NO-OP for the gpt-oss family, whose reasoning is level-based and cannot be
    # disabled by a boolean (only lowered). Primarily a benchmarking control for
    # the thinking-on-vs-off comparison; also a real operator lever for a runaway.
    force_no_think = bool(os.environ.get("OLLAMA_FORCE_NO_THINK", "").strip())
    think: bool | str | None = None
    num_predict = max_tokens
    if is_thinking:
        if force_no_think and not _ollama_requires_thinking_level(chosen):
            # Explicit disable: send think=false (NOT merely omitting the field,
            # which would leave the model on its own default) and keep the output
            # budget bounded so there's no unbounded trace.
            think = False
        else:
            # A thinking model thinks on EVERY track. Its output budget is the
            # answer allowance (max_tokens) PLUS a bounded reasoning headroom
            # (_ollama_think_budget) — NOT the old unbounded num_predict=-1.
            # Suppressing reasoning entirely is still wrong (it made weaker models
            # re-emit their known context as spurious actions — that is what
            # force_no_think above is for); but letting it run UNbounded let
            # runaway-prone models keep generating until they filled the whole
            # context window, surfacing as request timeouts or empty "No content"
            # responses rather than the clean truncation that design assumed. A
            # generous bound leaves disciplined thinkers untouched while making a
            # non-stopping model truncate cleanly (-> OutputTruncatedError, item
            # skipped) in seconds. See _ollama_think_budget for the sizing.
            num_predict = max_tokens + _ollama_think_budget()
            if _ollama_requires_thinking_level(chosen):
                # gpt-oss is the exception: its reasoning can't be turned off,
                # only tuned by LEVEL. The default gets the shortest level on the
                # high-volume tracks (its "high" trace is too heavy for per-entry
                # extraction) and the deepest for summaries; OLLAMA_THINK_LEVEL
                # overrides both (see _ollama_think_level).
                think = _ollama_think_level(purpose)
            else:
                think = True

    options: dict[str, Any] = {"num_predict": num_predict}
    if temperature is not None:
        options["temperature"] = temperature
    num_ctx = os.environ.get("OLLAMA_NUM_CTX", "").strip()
    if num_ctx:
        options["num_ctx"] = int(num_ctx)

    body: dict[str, Any] = {
        "model": chosen,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": options,
    }
    # A JSON Schema in `format` constrains the output to that shape; the string
    # "json" only forces valid JSON. Ollama takes the schema dict directly.
    if schema is not None:
        body["format"] = schema
    elif json_mode:
        body["format"] = "json"
    if think is not None:
        body["think"] = think

    try:
        resp = _ollama_chat_request(body)
    except Exception as exc:
        # A configured context window the hardware can't hold surfaces as a
        # memory-allocation failure — an HTTP 500 whose BODY names the memory
        # error, NOT a context-length error. Read the body so the marker check
        # can see it; the caller's fail-safe then runs (extraction -> IGNORE,
        # verify/dedupe -> UNCLEAR, summary -> left stale to retry).
        detail = _http_error_detail(exc)
        if any(marker in detail.lower() for marker in _MEMORY_ERROR_MARKERS):
            _log_ollama_memory_hint(chosen, limit, detail)
        raise
    tok = usage.from_ollama(resp)
    usage.record(
        purpose=purpose,
        provider="ollama",
        model=chosen,
        tokens=tok,
        docket=docket,
    )
    # Post-flight backstop: the server reports how many prompt tokens it actually
    # evaluated. If it silently truncated an over-long prompt, this raises rather
    # than return an answer built from a partial prompt.
    _detect_ollama_input_truncation(
        processed=tok.input,
        prompt_chars=prompt_chars,
        limit=limit,
        max_tokens=max_tokens,
    )
    message = resp.get("message") or {}
    text = message.get("content")
    truncated = resp.get("done_reason") == "length"
    if not text:
        # A thinking model that filled its whole output budget with reasoning and
        # emitted no answer reports done_reason="length" with empty content. Treat
        # that as the clean truncation it is (the caller skips the item), the same
        # as the OpenAI/Gemini paths — not a bare "No content" traceback.
        if truncated:
            raise OutputTruncatedError("ollama", "", max_tokens)
        raise ValueError("No content in Ollama response")
    if truncated:
        raise OutputTruncatedError("ollama", text, max_tokens)
    return text


def _call_ollama_openai_compat(
    system: str,
    user: str,
    max_tokens: int,
    *,
    chosen: str,
    json_mode: bool,
    schema: Optional[dict[str, Any]] = None,
    purpose: str,
    docket: Any,
    temperature: Optional[float],
    limit: Optional[int],
    prompt_chars: int,
) -> str:
    """OpenAI-compatible (``/v1/chat/completions``) call for a non-Ollama local
    server (LM Studio / vLLM / llama.cpp). No thinking control — that needs
    Ollama's native endpoint — so a thinking model here runs at its default (the
    pre-thinking-control behavior). Telemetry + truncation go through the
    OpenAI-shaped ``from_openai`` / ``finish_reason`` paths, as for hosted
    OpenAI. ``max_tokens`` (the classic field) is used, not the gpt-5-family
    ``max_completion_tokens``; the key is a throwaway (the server ignores it but
    the SDK requires one)."""
    import openai

    # OLLAMA_BASE_URL is the host root (e.g. http://localhost:11434); the
    # OpenAI-compatible API lives under /v1, so append it when absent — the
    # mirror of the native helpers stripping a /v1, so a bare-root URL is safe on
    # every path. A base that already ends in /v1 (the historical form, or LM
    # Studio's own /v1 endpoint) is kept as-is.
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    if not base_url.endswith("/v1"):
        base_url += "/v1"
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
    # Most OpenAI-compatible servers (LM Studio / vLLM / llama.cpp) also accept
    # the json_schema response_format; fall back to plain json_object without one.
    if schema is not None:
        kwargs["response_format"] = _openai_json_schema_format(schema)
    elif json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if temperature is not None:
        kwargs["temperature"] = temperature
    num_ctx = os.environ.get("OLLAMA_NUM_CTX", "").strip()
    if num_ctx:
        # The OpenAI SDK forwards unknown body fields via `extra_body`; the
        # server reads runtime options from a top-level `options` object.
        kwargs["extra_body"] = {"options": {"num_ctx": int(num_ctx)}}
    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as exc:
        if _is_memory_error(exc):
            _log_ollama_memory_hint(chosen, limit, str(exc))
        raise
    tok = usage.from_openai(resp)
    usage.record(
        purpose=purpose,
        provider="ollama",
        model=chosen,
        tokens=tok,
        docket=docket,
    )
    _detect_ollama_input_truncation(
        processed=tok.input,
        prompt_chars=prompt_chars,
        limit=limit,
        max_tokens=max_tokens,
    )
    choice = resp.choices[0]
    text = choice.message.content
    truncated = getattr(choice, "finish_reason", None) == "length"
    if not text:
        # Same as the native path: a budget-exhausted thinking model returns empty
        # content with finish_reason="length" — a clean truncation, not "No content".
        if truncated:
            raise OutputTruncatedError("ollama", "", max_tokens)
        raise ValueError("No content in Ollama response")
    if truncated:
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
    schema: Optional[dict[str, Any]] = None,
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
            # Anthropic has no boolean `json_mode` flag; without a schema we rely
            # on the prompt for JSON. A `schema`, if given, is applied via a
            # forced tool-call inside _call_anthropic (best-effort — strict mode
            # rejects our schema as too complex).
            return _call_anthropic(
                system,
                user,
                max_tokens,
                model=model,
                schema=schema,
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
                schema=schema,
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
                schema=schema,
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
            schema=schema,
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
