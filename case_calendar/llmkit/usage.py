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
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# A price estimator maps (model, usage) -> estimated USD for one call, or None
# when the model isn't in the caller's price table. llmkit ships no prices: the
# consumer supplies this so the library stays domain- and pricing-free. When
# set, the ledger logs a `cost_est=` field alongside the token counts.
PriceFn = Callable[[str, "TokenUsage"], Optional[float]]


def _fmt_cost(cost: Optional[float]) -> str:
    """`$0.0123` for a known estimate, `?` when the model had no price entry."""
    return f"${cost:.4f}" if cost is not None else "?"


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
        # Per-model subtotals. Model is the natural axis for "where did the
        # tokens / dollars go": the extractor and the summarizer run on
        # different models (a cheap small/fast tier vs a higher synthesis
        # tier), so a per-model split separates the two tracks in the run
        # summary. The ledger treats the model string opaquely — it doesn't
        # know which model is "the extractor" — so this stays domain-free.
        self._by_model: dict[str, TokenUsage] = {}
        self._calls_by_model: dict[str, int] = {}
        self._total = TokenUsage()
        self._calls = 0
        # Cost estimation (opt-in). `_price_fn` is None until a consumer sets
        # one; when set, per-call costs are accumulated here and `_unpriced`
        # counts calls whose model had no price entry (so the estimate is
        # flagged partial rather than silently undercounting). A model is
        # priced-or-not deterministically, so `_cost_by_model` containing a
        # model key (even at $0.0000) means that model was priced; its
        # absence while a price fn is set means it was unpriced.
        self._price_fn: Optional[PriceFn] = None
        self._cost_by_docket: dict[str, float] = {}
        self._cost_by_model: dict[str, float] = {}
        self._cost_total = 0.0
        self._unpriced = 0

    def set_price_estimator(self, fn: Optional[PriceFn]) -> None:
        """Attach (or clear, with ``None``) the cost estimator. Once set, the
        ledger logs a `cost_est=` field per call and in the summary."""
        with self._lock:
            self._price_fn = fn

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
        subtotals. ``docket`` is stringified; ``None`` buckets under ``"?"``.
        When a price estimator is set, the per-call line carries a `cost_est=`
        field and the cost is accumulated for the summary."""
        key = "?" if docket is None else str(docket)
        price_fn = self._price_fn
        cost = price_fn(model, tokens) if price_fn is not None else None
        cost_field = f" cost_est={_fmt_cost(cost)}" if price_fn is not None else ""
        logger.info(
            "llm-tokens call purpose=%s provider=%s model=%s docket=%s %s%s",
            purpose,
            provider,
            model,
            key,
            tokens,
            cost_field,
        )
        with self._lock:
            self._by_docket[key] = self._by_docket.get(key, TokenUsage()) + tokens
            self._calls_by_docket[key] = self._calls_by_docket.get(key, 0) + 1
            self._by_model[model] = self._by_model.get(model, TokenUsage()) + tokens
            self._calls_by_model[model] = self._calls_by_model.get(model, 0) + 1
            self._total = self._total + tokens
            self._calls += 1
            if price_fn is not None:
                if cost is not None:
                    self._cost_by_docket[key] = (
                        self._cost_by_docket.get(key, 0.0) + cost
                    )
                    self._cost_by_model[model] = (
                        self._cost_by_model.get(model, 0.0) + cost
                    )
                    self._cost_total += cost
                else:
                    self._unpriced += 1

    def log_summary(self, *, scope: str = "run") -> None:
        """Log a per-docket subtotal line for every docket seen this run, then
        a per-model subtotal line for every model, then a grand-total line.
        No-op when no calls were recorded. When a price estimator is set, each
        line also carries a `cost_est=` field; a model with no price entry
        shows `cost_est=?`, and the TOTAL notes how many calls were unpriced so
        a partial estimate is obvious.

        The per-model lines are what separate the cost of the extractor track
        from the summary track: the two run on different models, so reading the
        run total by model tells you which one spent what. The startup log
        names which model is the extractor vs the summarizer."""
        with self._lock:
            by_docket = dict(self._by_docket)
            calls_by_docket = dict(self._calls_by_docket)
            cost_by_docket = dict(self._cost_by_docket)
            by_model = dict(self._by_model)
            calls_by_model = dict(self._calls_by_model)
            cost_by_model = dict(self._cost_by_model)
            total = self._total
            calls = self._calls
            priced = self._price_fn is not None
            cost_total = self._cost_total
            unpriced = self._unpriced
        if calls == 0:
            return
        for key in sorted(by_docket):
            cost_field = (
                f" cost_est=${cost_by_docket.get(key, 0.0):.4f}" if priced else ""
            )
            logger.info(
                "llm-tokens docket=%s calls=%d %s%s",
                key,
                calls_by_docket[key],
                by_docket[key],
                cost_field,
            )
        for model in sorted(by_model):
            if priced:
                # A priced model is in cost_by_model (even at $0.0000); a model
                # absent there while a price fn is set had no price entry.
                if model in cost_by_model:
                    cost_field = f" cost_est=${cost_by_model[model]:.4f}"
                else:
                    cost_field = " cost_est=?"
            else:
                cost_field = ""
            logger.info(
                "llm-tokens model=%s calls=%d %s%s",
                model,
                calls_by_model[model],
                by_model[model],
                cost_field,
            )
        if priced:
            note = f" ({unpriced} call(s) had no price entry)" if unpriced else ""
            cost_field = f" cost_est=${cost_total:.4f}{note}"
        else:
            cost_field = ""
        logger.info(
            "llm-tokens %s TOTAL calls=%d dockets=%d models=%d %s%s",
            scope,
            calls,
            len(by_docket),
            len(by_model),
            total,
            cost_field,
        )

    def reset(self) -> None:
        """Clear all accumulated data AND the price estimator — a full reset to
        fresh state. Callers that want cost estimation must (re)attach the
        estimator after a reset."""
        with self._lock:
            self._by_docket.clear()
            self._calls_by_docket.clear()
            self._by_model.clear()
            self._calls_by_model.clear()
            self._total = TokenUsage()
            self._calls = 0
            self._price_fn = None
            self._cost_by_docket.clear()
            self._cost_by_model.clear()
            self._cost_total = 0.0
            self._unpriced = 0


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


def set_price_estimator(fn: Optional[PriceFn]) -> None:
    """Attach (or clear) the cost estimator on the process-wide ledger. Cleared
    by :func:`reset`, so set it once per run before any calls are recorded."""
    _LEDGER.set_price_estimator(fn)


def log_summary(*, scope: str = "run") -> None:
    """Log the process-wide ledger's per-docket subtotals and grand total."""
    _LEDGER.log_summary(scope=scope)


def reset() -> None:
    """Clear the process-wide ledger (used at run boundaries and in tests)."""
    _LEDGER.reset()
