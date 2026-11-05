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
- Where a provider charges more for a long prompt (Claude Haiku 5.5 over 100K
  tokens, the Gemini Pro models over 200K), the whole call is priced at the
  higher rate, the way the provider bills it. OpenAI's over-272K rates aren't
  modeled: its pricing page loads them by script, so they couldn't be checked.
- It does NOT model batch discounts, data-residency multipliers, or per-hour
  cache storage — only the standard per-token rates.

The estimate uses our normalized :class:`~case_calendar.llmkit.usage.TokenUsage`
breakdown, which matters: we cache the system prompt on nearly every call, so
the cache-read and cache-write tokens are priced at their own (much cheaper /
slightly dearer) rates instead of being lumped into plain input.
"""

from __future__ import annotations

import re
from typing import Optional, TypedDict

from .llmkit.usage import TokenUsage

# Date the rates below were last checked against the providers' pricing pages.
PRICES_VERIFIED = "2026-11-05"


class _Rates(TypedDict):
    """Standard per-million-token USD rates for one model.

    One key per token slice so the table reads without having to remember a
    positional order, and so a missing slice is a type error rather than a
    silently shifted tuple. ``cache_write`` is 0 for providers that don't
    bill a separate per-token cache-write charge (Gemini, the older OpenAI
    models).
    """

    input: float
    cache_read: float
    cache_write: float
    output: float


class _LongPromptRates(TypedDict):
    """The higher rates a model bills once its prompt passes a size limit.

    ``over`` is the prompt size in tokens — every input token, cached and
    cache-write tokens included — above which the WHOLE call is billed at
    ``rates``, not just the tokens past the limit.
    """

    over: int
    rates: _Rates


# Standard per-million-token USD rates, keyed by model id.
#
# Anthropic (https://platform.claude.com/docs/en/about-claude/pricing):
#   cache_write is the 5-minute cache-write rate (1.25x base input), which is
#   the cache this project uses; cache_read is the "cache hits" column, which
#   is 0.1x base input except where the page says otherwise (0.025x on Fable
#   5.1 / Mythos 5.1, 0.05x on Opus 5.5 / Sonnet 5.5). Models the
#   deprecations page (https://platform.claude.com/docs/en/about-claude/model-deprecations)
#   marks deprecated or retired are omitted: Sonnet 4.5 (retires 2026-11-30),
#   Opus 4.1, Opus 4, Sonnet 4, and the Claude 3 line.
# Gemini (https://aistudio.google.com/docs/pricing): the standard paid tier,
#   text input; cache_read is the context-cache token rate, and there is no
#   per-token cache write (Gemini bills cache by storage time, not written
#   tokens), so cache_write is 0. Deprecated models are omitted (2.5 Flash;
#   3.5 Flash is no longer on the page) EXCEPT gemini-2.5-pro, which is still
#   the Gemini-only summary default — after its 2026-12-01 shutdown Google
#   routes those calls to gemini-3.8-flash and bills them at its rates, so
#   estimates logged under the 2.5-pro name will run high.
# OpenAI (https://developers.openai.com/api/docs/pricing): the standard tier,
#   at the short-context (<=272K) rates. cache_read is the "cached input"
#   rate; the `-pro` models publish no cached rate, so cached input is priced
#   at the full input rate. The GPT-5.6 and GPT-6 models also charge for cache
#   writes (cache_write is their 30-minute write rate), but our OpenAI token
#   counts don't report written tokens yet, so in practice those tokens are
#   priced as plain input. The whole current GPT-5 and GPT-6 text lineup is
#   listed; the older GPT-4 / GPT-4o / o-series and legacy models aren't and
#   log `cost_est=?` if used (the `_rates` snapshot fallback only resolves
#   date/pin suffixes, never a different model, so they stay unpriced rather
#   than mis-priced).
_RATES_USD_PER_MTOK: dict[str, _Rates] = {
    # Anthropic (cache_write = 5-minute cache-write rate)
    "claude-fable-5-1": {
        "input": 10.00,
        "cache_read": 0.25,
        "cache_write": 12.50,
        "output": 50.00,
    },
    "claude-mythos-5-1": {
        "input": 10.00,
        "cache_read": 0.25,
        "cache_write": 12.50,
        "output": 50.00,
    },
    "claude-fable-5": {
        "input": 10.00,
        "cache_read": 1.00,
        "cache_write": 12.50,
        "output": 50.00,
    },
    "claude-mythos-5": {
        "input": 10.00,
        "cache_read": 1.00,
        "cache_write": 12.50,
        "output": 50.00,
    },
    "claude-opus-5-5": {
        "input": 4.00,
        "cache_read": 0.20,
        "cache_write": 5.00,
        "output": 20.00,
    },
    "claude-opus-5": {
        "input": 5.00,
        "cache_read": 0.50,
        "cache_write": 6.25,
        "output": 25.00,
    },
    "claude-opus-4-8": {
        "input": 5.00,
        "cache_read": 0.50,
        "cache_write": 6.25,
        "output": 25.00,
    },
    "claude-opus-4-7": {
        "input": 5.00,
        "cache_read": 0.50,
        "cache_write": 6.25,
        "output": 25.00,
    },
    "claude-opus-4-6": {
        "input": 5.00,
        "cache_read": 0.50,
        "cache_write": 6.25,
        "output": 25.00,
    },
    "claude-opus-4-5": {
        "input": 5.00,
        "cache_read": 0.50,
        "cache_write": 6.25,
        "output": 25.00,
    },
    "claude-sonnet-5-5": {
        "input": 2.00,
        "cache_read": 0.10,
        "cache_write": 2.50,
        "output": 10.00,
    },
    "claude-sonnet-5": {
        "input": 2.00,
        "cache_read": 0.20,
        "cache_write": 2.50,
        "output": 10.00,
    },
    "claude-sonnet-4-6": {
        "input": 3.00,
        "cache_read": 0.30,
        "cache_write": 3.75,
        "output": 15.00,
    },
    # Prompts up to 100K tokens; the higher rate is in _LONG_PROMPT_RATES.
    "claude-haiku-5-5": {
        "input": 0.10,
        "cache_read": 0.01,
        "cache_write": 0.125,
        "output": 0.50,
    },
    "claude-haiku-4-5": {
        "input": 1.00,
        "cache_read": 0.10,
        "cache_write": 1.25,
        "output": 5.00,
    },
    # Gemini (standard tier, text input)
    # 3.8 / 3.6 Flash: rates through 2026-12-31; Google's page lists them
    # doubling on 2027-01-01 (input $1.50, cache $0.15, output $7.50).
    "gemini-3.8-flash": {
        "input": 0.75,
        "cache_read": 0.075,
        "cache_write": 0.0,
        "output": 3.75,
    },
    "gemini-3.6-flash": {
        "input": 0.75,
        "cache_read": 0.075,
        "cache_write": 0.0,
        "output": 3.75,
    },
    "gemini-3.5-flash-lite": {
        "input": 0.30,
        "cache_read": 0.03,
        "cache_write": 0.0,
        "output": 2.50,
    },
    # Prompts up to 200K tokens; the higher rate is in _LONG_PROMPT_RATES.
    "gemini-3.1-pro-preview": {
        "input": 2.00,
        "cache_read": 0.20,
        "cache_write": 0.0,
        "output": 12.00,
    },
    "gemini-3.1-flash-lite": {
        "input": 0.25,
        "cache_read": 0.025,
        "cache_write": 0.0,
        "output": 1.50,
    },
    "gemini-3-flash-preview": {
        "input": 0.50,
        "cache_read": 0.05,
        "cache_write": 0.0,
        "output": 3.00,
    },
    # Prompts up to 200K tokens; the higher rate is in _LONG_PROMPT_RATES.
    "gemini-2.5-pro": {
        "input": 1.25,
        "cache_read": 0.125,
        "cache_write": 0.0,
        "output": 10.00,
    },
    "gemini-2.5-flash-lite": {
        "input": 0.10,
        "cache_read": 0.01,
        "cache_write": 0.0,
        "output": 0.40,
    },
    # OpenAI (standard tier; -pro models have no cached rate -> cached = input;
    # cache_write = 30-minute write rate where the page lists one)
    "gpt-6-astra": {
        "input": 10.00,
        "cache_read": 1.00,
        "cache_write": 12.50,
        "output": 50.00,
    },
    "gpt-6.1-sol": {
        "input": 2.00,
        "cache_read": 0.10,
        "cache_write": 2.50,
        "output": 10.00,
    },
    "gpt-6.1-terra": {
        "input": 1.00,
        "cache_read": 0.05,
        "cache_write": 1.25,
        "output": 5.00,
    },
    "gpt-6-sol": {
        "input": 2.00,
        "cache_read": 0.20,
        "cache_write": 2.50,
        "output": 10.00,
    },
    "gpt-6-luna": {
        "input": 0.10,
        "cache_read": 0.01,
        "cache_write": 0.125,
        "output": 0.50,
    },
    # Promotional pricing through 2026-11-30, per OpenAI's page.
    "gpt-5.6-sol": {
        "input": 4.00,
        "cache_read": 0.40,
        "cache_write": 5.00,
        "output": 20.00,
    },
    "gpt-5.6-terra": {
        "input": 2.00,
        "cache_read": 0.20,
        "cache_write": 2.50,
        "output": 12.00,
    },
    "gpt-5.6-luna": {
        "input": 0.20,
        "cache_read": 0.02,
        "cache_write": 0.25,
        "output": 1.20,
    },
    "gpt-5.5": {
        "input": 5.00,
        "cache_read": 0.50,
        "cache_write": 0.0,
        "output": 30.00,
    },
    "gpt-5.5-pro": {
        "input": 30.00,
        "cache_read": 30.00,
        "cache_write": 0.0,
        "output": 180.00,
    },
    "gpt-5.4": {
        "input": 2.50,
        "cache_read": 0.25,
        "cache_write": 0.0,
        "output": 15.00,
    },
    "gpt-5.4-mini": {
        "input": 0.75,
        "cache_read": 0.075,
        "cache_write": 0.0,
        "output": 4.50,
    },
    "gpt-5.4-nano": {
        "input": 0.20,
        "cache_read": 0.02,
        "cache_write": 0.0,
        "output": 1.25,
    },
    "gpt-5.4-pro": {
        "input": 30.00,
        "cache_read": 30.00,
        "cache_write": 0.0,
        "output": 180.00,
    },
    "gpt-5.2": {
        "input": 1.75,
        "cache_read": 0.175,
        "cache_write": 0.0,
        "output": 14.00,
    },
    "gpt-5.2-pro": {
        "input": 21.00,
        "cache_read": 21.00,
        "cache_write": 0.0,
        "output": 168.00,
    },
    "gpt-5.1": {
        "input": 1.25,
        "cache_read": 0.125,
        "cache_write": 0.0,
        "output": 10.00,
    },
    "gpt-5": {
        "input": 1.25,
        "cache_read": 0.125,
        "cache_write": 0.0,
        "output": 10.00,
    },
    "gpt-5-mini": {
        "input": 0.25,
        "cache_read": 0.025,
        "cache_write": 0.0,
        "output": 2.00,
    },
    "gpt-5-nano": {
        "input": 0.05,
        "cache_read": 0.005,
        "cache_write": 0.0,
        "output": 0.40,
    },
    "gpt-5-pro": {
        "input": 15.00,
        "cache_read": 15.00,
        "cache_write": 0.0,
        "output": 120.00,
    },
}

# Higher rates for long prompts, keyed by model id — only for models whose
# provider charges more past a prompt-size limit. Same sources as above.
_LONG_PROMPT_RATES: dict[str, _LongPromptRates] = {
    "claude-haiku-5-5": {
        "over": 100_000,
        "rates": {
            "input": 0.50,
            "cache_read": 0.05,
            "cache_write": 0.625,
            "output": 2.50,
        },
    },
    "gemini-3.1-pro-preview": {
        "over": 200_000,
        "rates": {
            "input": 4.00,
            "cache_read": 0.40,
            "cache_write": 0.0,
            "output": 18.00,
        },
    },
    "gemini-2.5-pro": {
        "over": 200_000,
        "rates": {
            "input": 2.50,
            "cache_read": 0.25,
            "cache_write": 0.0,
            "output": 15.00,
        },
    },
}


# A trailing model snapshot / pin suffix: Anthropic ``-YYYYMMDD``, OpenAI
# ``-YYYY-MM-DD``, Gemini ``-NNN``. Stripping it lets a pinned snapshot fall
# back to its base model's rate. A TIER suffix (``-mini`` / ``-nano`` /
# ``-pro``) is a word, not digits, so it is NOT stripped — an unlisted tier (or
# an unlisted sibling version like a hypothetical ``gpt-5.3``) therefore stays
# unpriced (``cost_est=?``) rather than being silently mis-priced as a
# different model. Exact-match-first means a base id that itself ends in digits
# is never wrongly stripped.
_SNAPSHOT_SUFFIX_RE = re.compile(r"(?:-\d{4}-\d{2}-\d{2}|-\d{3,8})$")


def _lookup[T](table: dict[str, T], model: str) -> Optional[T]:
    """Look ``model`` up in ``table`` by exact id, then — only for a
    dated/pinned snapshot id (``claude-haiku-4-5-20251001``,
    ``gpt-5.4-2026-01-15``, ``gemini-2.5-flash-lite-002``) — by the base model
    with that suffix stripped."""
    if model in table:
        return table[model]
    base = _SNAPSHOT_SUFFIX_RE.sub("", model)
    if base != model and base in table:
        return table[base]
    return None


def _rates(model: str, prompt_tokens: int = 0) -> Optional[_Rates]:
    """A model's rates for a call whose prompt is ``prompt_tokens`` long: the
    long-prompt rates when the model has them and the prompt is over their
    limit, otherwise the standard rates (``None`` when the model is unlisted)."""
    rates = _lookup(_RATES_USD_PER_MTOK, model)
    if rates is None:
        return None
    long_prompt = _lookup(_LONG_PROMPT_RATES, model)
    if long_prompt is not None and prompt_tokens > long_prompt["over"]:
        return long_prompt["rates"]
    return rates


def estimate_cost(
    model: str, usage: TokenUsage, provider: Optional[str] = None
) -> Optional[float]:
    """Estimated USD for one call, or ``None`` when ``model`` isn't in the
    table (the caller flags that as unpriced rather than guessing).

    ``usage.input`` is the TOTAL prompt tokens with the cached + cache-write
    portions included, so the freshly-processed (uncached) input is
    ``input - cached - cache_write``; each slice is billed at its own rate.
    That total is also the prompt size that picks a model's long-prompt rates
    (see ``_LONG_PROMPT_RATES``).

    ``provider`` lets us price by provider as well as model. A local provider
    (Ollama) has no per-token API charge, so it bills ``0.0`` regardless of
    the model name — returning ``None`` there would mislabel every local call
    as "unpriced" (``cost_est=?``) when the API cost is genuinely zero. (Local
    inference still has hardware / electricity cost; this estimator only ever
    modeled per-token API billing, so ``0.0`` is the accurate figure for the
    thing it measures.) ``provider`` defaults to ``None`` so direct 2-arg
    callers keep the original table-lookup behavior.
    """
    if provider == "ollama":
        return 0.0
    rates = _rates(model, usage.input)
    if rates is None:
        return None
    uncached_input = max(0, usage.input - usage.cached - usage.cache_write)
    total_micro = (
        uncached_input * rates["input"]
        + usage.cached * rates["cache_read"]
        + usage.cache_write * rates["cache_write"]
        + usage.output * rates["output"]
    )
    return total_micro / 1_000_000
