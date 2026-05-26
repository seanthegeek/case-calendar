"""Rough USD cost ESTIMATES for LLM calls, layered on the token telemetry.

This is the case_calendar-side price source that plugs into the price-free
``llmkit`` ledger (via ``llmkit.usage.set_price_estimator``). It turns the exact
token counts we already record into a dollar figure so the `llm-tokens` log
lines can carry a `cost_est=` field.

It is deliberately an ESTIMATE, not a bill:

- Prices are a hand-kept static table sourced directly from each provider's
  published pricing page, verified on the date in ``PRICES_VERIFIED``. Provider
  prices change; when the table is stale the estimate drifts, and a model the
  operator switches to via ``LLM_MODEL`` that isn't in the table produces no
  estimate at all (the ledger logs `cost_est=?` and flags the run total
  partial) rather than a wrong number.
- It does NOT model batch discounts, long-context (>200k) tiers, data-residency
  multipliers, or per-hour cache storage — only the standard per-token rates.

The estimate uses our normalized :class:`~case_calendar.llmkit.usage.TokenUsage`
breakdown, which matters: we cache the system prompt on nearly every call, so
the cache-read and cache-write tokens are priced at their own (much cheaper /
slightly dearer) rates instead of being lumped into plain input.
"""

from __future__ import annotations

from typing import Optional

from .llmkit.usage import TokenUsage

# Date the rates below were last checked against the providers' pricing pages.
PRICES_VERIFIED = "2026-05-26"

# Standard per-million-token USD rates: (input, cache_read, cache_write, output).
#
# Anthropic (https://platform.claude.com/docs/en/about-claude/pricing): cache
#   figures are the documented multipliers off base input — 5-minute cache
#   write = 1.25x, cache read (hit) = 0.1x — which is the ephemeral cache this
#   project uses.
# Gemini (https://ai.google.dev/gemini-api/docs/pricing): the <=200k-prompt
#   standard tier; cache_read is the context-cache token rate, and there is no
#   per-token cache write (Gemini bills cache by storage-time, not written
#   tokens), so cache_write is 0.
#
# OpenAI (https://developers.openai.com/api/docs/pricing): standard-tier rates.
#   OpenAI bills cached prompt tokens at the "cached input" rate and has no
#   separate per-token cache-write charge, so cache_write is 0 (and our usage
#   reports 0 cache_write tokens for OpenAI). The `-pro` models publish no
#   cached rate, so cached input is priced at the full input rate. The whole
#   current 5.4 + 5.5 family is listed (every -mini/-nano/-pro variant) so the
#   prefix fallback in `_rates` can't mis-price one variant as another; older
#   / legacy OpenAI models aren't listed and log `cost_est=?` if used.
_RATES_USD_PER_MTOK: dict[str, tuple[float, float, float, float]] = {
    # Anthropic
    "claude-haiku-4-5": (1.00, 0.10, 1.25, 5.00),
    "claude-sonnet-4-6": (3.00, 0.30, 3.75, 15.00),
    # Gemini (<=200k standard tier)
    "gemini-2.5-flash-lite": (0.10, 0.01, 0.0, 0.40),
    "gemini-2.5-pro": (1.25, 0.125, 0.0, 10.00),
    # OpenAI (standard tier)
    "gpt-5.5": (5.00, 0.50, 0.0, 30.00),
    "gpt-5.5-pro": (30.00, 30.00, 0.0, 180.00),
    "gpt-5.4": (2.50, 0.25, 0.0, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 0.0, 4.50),
    "gpt-5.4-nano": (0.20, 0.02, 0.0, 1.25),
    "gpt-5.4-pro": (30.00, 30.00, 0.0, 180.00),
}


def _rates(model: str) -> Optional[tuple[float, float, float, float]]:
    """Look up a model's rates by exact id, then by longest-matching prefix so
    a dated/suffixed id (``claude-haiku-4-5-20251001``) still resolves."""
    if model in _RATES_USD_PER_MTOK:
        return _RATES_USD_PER_MTOK[model]
    for key in sorted(_RATES_USD_PER_MTOK, key=len, reverse=True):
        if model.startswith(key):
            return _RATES_USD_PER_MTOK[key]
    return None


def estimate_cost(model: str, usage: TokenUsage) -> Optional[float]:
    """Estimated USD for one call, or ``None`` when ``model`` isn't in the
    table (the caller flags that as unpriced rather than guessing).

    ``usage.input`` is the TOTAL prompt tokens with the cached + cache-write
    portions included, so the freshly-processed (uncached) input is
    ``input - cached - cache_write``; each slice is billed at its own rate.
    """
    rates = _rates(model)
    if rates is None:
        return None
    input_rate, cache_read_rate, cache_write_rate, output_rate = rates
    uncached_input = max(0, usage.input - usage.cached - usage.cache_write)
    total_micro = (
        uncached_input * input_rate
        + usage.cached * cache_read_rate
        + usage.cache_write * cache_write_rate
        + usage.output * output_rate
    )
    return total_micro / 1_000_000
