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


# Default model per provider when the caller passes none and ``LLM_MODEL`` is
# unset. The small/fast tier — this layer is used for structured extraction by
# default; callers that want a heavier model (e.g. case_calendar's summary
# track) pass ``model=`` explicitly.
_DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-5.4-nano",
    "gemini": "gemini-2.5-flash-lite",
}


def _detect_provider() -> Optional[str]:
    provider = os.environ.get("LLM_PROVIDER", "").lower().strip()
    if provider in ("anthropic", "openai", "gemini"):
        return provider
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    return None


def _call_anthropic(
    system: str,
    user: str,
    max_tokens: int,
    *,
    model: Optional[str] = None,
    purpose: str = "llm",
    docket: Any = None,
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
    resp = client.messages.create(
        model=chosen,
        max_tokens=max_tokens,
        system=[
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user}],
    )
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
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
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
) -> str:
    """Route to the per-provider call function by ``provider`` name.

    Single home for the three-way ``anthropic | openai | gemini``
    dispatch. The per-provider functions still own their SDK quirks
    (truncation signal detection, json-mode kwargs, model-default
    selection); this helper just picks which one to call so callers don't
    have to rewrite the if/elif/else when a fourth provider is added or a
    kwarg shape shifts. ``OutputTruncatedError`` and any other exceptions
    propagate unchanged so callers can convert them into their own
    caller-specific fallback shape (IGNORE list vs UNCLEAR dict vs raise).
    """
    if provider == "anthropic":
        # Anthropic has no `json_mode` knob (no JSON mode flag in the
        # SDK; we just rely on the prompt and validate the response).
        return _call_anthropic(
            system, user, max_tokens, model=model, purpose=purpose, docket=docket
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
        )
    return _call_gemini(
        system,
        user,
        max_tokens,
        model=model,
        json_mode=json_mode,
        purpose=purpose,
        docket=docket,
    )


def provider_info() -> str:
    """One-line ``provider=… model=…`` for the auto-detected provider, or a
    ``no provider configured`` notice. Reflects ``LLM_PROVIDER`` / ``LLM_MODEL``
    overrides and the per-provider defaults."""
    p = _detect_provider()
    if p is None:
        return "no provider configured"
    model = os.environ.get("LLM_MODEL", _DEFAULT_MODELS[p])
    return f"provider={p} model={model}"
