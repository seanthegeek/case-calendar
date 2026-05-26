"""Token-usage telemetry for LLM calls.

Captures input / output / cached / cache-write token counts from each
provider's response, logs one line per call, and accumulates per-docket and
per-run subtotals so an operator can read REAL numbers instead of the rough
cost estimates in the docs. Log-only — nothing is persisted to the store.

Provider semantics differ and are normalized here so the numbers are
comparable across providers:

- **Anthropic** reports cache reads / writes as counters SEPARATE from
  ``input_tokens`` (``input_tokens`` is the uncached prompt only). We fold all
  three into ``input`` so it means "total prompt tokens processed".
- **OpenAI** and **Gemini** fold cached prompt tokens INTO the prompt count
  and report the cached portion as a subset, so ``input`` already includes
  them.

After normalization: ``input`` is ALWAYS the total prompt tokens (cached
included), ``cached`` is the portion served from cache, and ``cache_write`` is
tokens written to the cache (Anthropic-only; 0 elsewhere).

The recorder is deliberately forgiving: any usage field that isn't a plain int
(a missing attribute, ``None``, or a test double) coerces to 0, so telemetry
never turns a working LLM call into a crash.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


def _as_int(value: Any) -> int:
    """Coerce a usage field to int; non-numeric values (missing field, None,
    a MagicMock in tests) become 0 so telemetry can't crash a call.

    ``bool`` is rejected explicitly — it's an ``int`` subclass, but a usage
    field coming through as ``True``/``False`` is a bug, not a count.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


@dataclass(frozen=True)
class TokenUsage:
    """Normalized per-call token counts. See the module docstring for the
    cross-provider normalization rules."""

    input: int = 0  # total prompt tokens, cached portion included
    output: int = 0  # completion / candidate tokens
    cached: int = 0  # prompt tokens served from cache (subset of `input`)
    cache_write: int = 0  # prompt tokens written to the cache (Anthropic only)

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            self.input + other.input,
            self.output + other.output,
            self.cached + other.cached,
            self.cache_write + other.cache_write,
        )

    def __str__(self) -> str:
        return (
            f"in={self.input} out={self.output} "
            f"cached={self.cached} cache_write={self.cache_write}"
        )


def from_anthropic(resp: Any) -> TokenUsage:
    """Extract usage from an Anthropic ``messages.create`` response.

    Anthropic's ``input_tokens`` excludes cache reads / writes, which it
    reports as separate counters — fold all three into ``input`` so it means
    "total prompt tokens processed".
    """
    u = getattr(resp, "usage", None)
    if u is None:
        return TokenUsage()
    uncached = _as_int(getattr(u, "input_tokens", 0))
    cache_read = _as_int(getattr(u, "cache_read_input_tokens", 0))
    cache_write = _as_int(getattr(u, "cache_creation_input_tokens", 0))
    return TokenUsage(
        input=uncached + cache_read + cache_write,
        output=_as_int(getattr(u, "output_tokens", 0)),
        cached=cache_read,
        cache_write=cache_write,
    )


def from_openai(resp: Any) -> TokenUsage:
    """Extract usage from an OpenAI ``chat.completions.create`` response.

    ``prompt_tokens`` already includes the cached portion; the cached count is
    a subset reported under ``prompt_tokens_details.cached_tokens``.
    """
    u = getattr(resp, "usage", None)
    if u is None:
        return TokenUsage()
    details = getattr(u, "prompt_tokens_details", None)
    cached = _as_int(getattr(details, "cached_tokens", 0)) if details is not None else 0
    return TokenUsage(
        input=_as_int(getattr(u, "prompt_tokens", 0)),
        output=_as_int(getattr(u, "completion_tokens", 0)),
        cached=cached,
        cache_write=0,
    )


def from_gemini(resp: Any) -> TokenUsage:
    """Extract usage from a Gemini ``generate_content`` response.

    ``prompt_token_count`` already includes the cached portion, which is
    reported separately as ``cached_content_token_count``.
    """
    u = getattr(resp, "usage_metadata", None)
    if u is None:
        return TokenUsage()
    return TokenUsage(
        input=_as_int(getattr(u, "prompt_token_count", 0)),
        output=_as_int(getattr(u, "candidates_token_count", 0)),
        cached=_as_int(getattr(u, "cached_content_token_count", 0)),
        cache_write=0,
    )


class TokenLedger:
    """Accumulates token usage per docket and in total for one process run.

    Thread-safe: ``record`` may be called from the webhook server's worker
    threads. ``log_summary`` dumps the per-docket subtotals plus a grand total
    at a run boundary (end of ``sync`` / ``summarize``); ``reset`` clears it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_docket: dict[str, TokenUsage] = {}
        self._calls_by_docket: dict[str, int] = {}
        self._total = TokenUsage()
        self._calls = 0

    def record(
        self,
        *,
        purpose: str,
        provider: str,
        model: str,
        tokens: TokenUsage,
        docket: Any = None,
    ) -> None:
        """Log one per-call line and fold the usage into the docket + total
        subtotals. ``docket`` is stringified; ``None`` buckets under ``"?"``."""
        key = "?" if docket is None else str(docket)
        logger.info(
            "llm-tokens call purpose=%s provider=%s model=%s docket=%s %s",
            purpose,
            provider,
            model,
            key,
            tokens,
        )
        with self._lock:
            self._by_docket[key] = self._by_docket.get(key, TokenUsage()) + tokens
            self._calls_by_docket[key] = self._calls_by_docket.get(key, 0) + 1
            self._total = self._total + tokens
            self._calls += 1

    def log_summary(self, *, scope: str = "run") -> None:
        """Log a per-docket subtotal line for every docket seen this run, then
        a grand-total line. No-op when no calls were recorded."""
        with self._lock:
            by_docket = dict(self._by_docket)
            calls_by_docket = dict(self._calls_by_docket)
            total = self._total
            calls = self._calls
        if calls == 0:
            return
        for key in sorted(by_docket):
            logger.info(
                "llm-tokens docket=%s calls=%d %s",
                key,
                calls_by_docket[key],
                by_docket[key],
            )
        logger.info(
            "llm-tokens %s TOTAL calls=%d dockets=%d %s",
            scope,
            calls,
            len(by_docket),
            total,
        )

    def reset(self) -> None:
        with self._lock:
            self._by_docket.clear()
            self._calls_by_docket.clear()
            self._total = TokenUsage()
            self._calls = 0


# Process-wide default ledger. `case-calendar sync` / `summarize` each run as a
# fresh process, so the ledger naturally scopes to one run; the long-running
# `serve` process leaves it to grow (bounded by the number of configured
# dockets) and relies on the per-call lines rather than a run total.
_LEDGER = TokenLedger()


def record(
    *,
    purpose: str,
    provider: str,
    model: str,
    tokens: TokenUsage,
    docket: Any = None,
) -> None:
    """Record one call against the process-wide ledger (see TokenLedger)."""
    _LEDGER.record(
        purpose=purpose, provider=provider, model=model, tokens=tokens, docket=docket
    )


def log_summary(*, scope: str = "run") -> None:
    """Log the process-wide ledger's per-docket subtotals and grand total."""
    _LEDGER.log_summary(scope=scope)


def reset() -> None:
    """Clear the process-wide ledger (used at run boundaries and in tests)."""
    _LEDGER.reset()
