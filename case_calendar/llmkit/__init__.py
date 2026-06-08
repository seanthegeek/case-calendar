"""``llmkit`` — a provider-agnostic LLM call layer with token telemetry.

Self-contained and domain-free: call any of Anthropic / OpenAI / Gemini
through one :func:`dispatch`, with retry headroom, max-token-truncation
detection (:class:`OutputTruncatedError`), provider auto-detection
(:func:`detect_provider`), and per-call / per-docket / per-run token
accounting (:mod:`~case_calendar.llmkit.usage`). Carries no knowledge of
case_calendar's domain — structured to be lifted out as a standalone project.

The public names below are stable aliases over the implementation in
``providers`` / ``usage``; the underscore-prefixed originals remain the
internal API the rest of case_calendar patches in tests.
"""

from __future__ import annotations

from . import usage
from .providers import (
    ContextWindowExceededError,
    OutputTruncatedError,
)
from .providers import (
    _DEFAULT_MODELS as DEFAULT_MODELS,
)
from .providers import (
    _detect_extraction_provider as detect_extraction_provider,
)
from .providers import (
    _detect_provider as detect_provider,
)
from .providers import (
    _dispatch_llm_call as dispatch,
)
from .providers import (
    provider_info,
)
from .usage import TokenLedger, TokenUsage

__all__ = [
    "DEFAULT_MODELS",
    "ContextWindowExceededError",
    "OutputTruncatedError",
    "TokenLedger",
    "TokenUsage",
    "detect_extraction_provider",
    "detect_provider",
    "dispatch",
    "provider_info",
    "usage",
]
