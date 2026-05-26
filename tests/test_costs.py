"""Tests for case_calendar.costs — the static-table cost ESTIMATOR."""

from __future__ import annotations

import pytest

from case_calendar import costs
from case_calendar.llmkit.usage import TokenUsage


class TestEstimateCost:
    def test_haiku_uncached(self):
        # 1M uncached input @ $1 + 1M output @ $5 = $6.00.
        usage = TokenUsage(input=1_000_000, output=1_000_000, cached=0, cache_write=0)
        assert costs.estimate_cost("claude-haiku-4-5", usage) == pytest.approx(6.0)

    def test_haiku_cache_read_is_cheaper(self):
        # All input served from cache: 1M cache-read @ $0.10 = $0.10 (not $1).
        usage = TokenUsage(input=1_000_000, output=0, cached=1_000_000, cache_write=0)
        assert costs.estimate_cost("claude-haiku-4-5", usage) == pytest.approx(0.10)

    def test_haiku_cache_write_priced_separately(self):
        # input=1M split as 600k uncached + 400k cache-write; output 0.
        # 600k@$1 + 400k@$1.25 = $0.60 + $0.50 = $1.10 (per 1M scaling).
        usage = TokenUsage(input=1_000_000, output=0, cached=0, cache_write=400_000)
        assert costs.estimate_cost("claude-haiku-4-5", usage) == pytest.approx(1.10)

    def test_sonnet_rates(self):
        usage = TokenUsage(input=1_000_000, output=1_000_000, cached=0, cache_write=0)
        # $3 input + $15 output.
        assert costs.estimate_cost("claude-sonnet-4-6", usage) == pytest.approx(18.0)

    def test_gemini_flash_lite(self):
        usage = TokenUsage(input=1_000_000, output=1_000_000, cached=0, cache_write=0)
        # $0.10 input + $0.40 output.
        assert costs.estimate_cost("gemini-2.5-flash-lite", usage) == pytest.approx(
            0.50
        )

    def test_gemini_pro(self):
        usage = TokenUsage(input=1_000_000, output=1_000_000, cached=0, cache_write=0)
        assert costs.estimate_cost("gemini-2.5-pro", usage) == pytest.approx(11.25)

    def test_unknown_model_returns_none(self):
        # OpenAI defaults aren't in the table (unverified) -> no estimate.
        usage = TokenUsage(input=1000, output=100)
        assert costs.estimate_cost("gpt-5.4-nano", usage) is None
        assert costs.estimate_cost("some-future-model", usage) is None

    def test_prefix_match_handles_dated_id(self):
        # A dated/suffixed id resolves to its base model's rates.
        usage = TokenUsage(input=1_000_000, output=0, cached=0, cache_write=0)
        assert costs.estimate_cost("claude-haiku-4-5-20251001", usage) == pytest.approx(
            1.0
        )

    def test_zero_usage_is_zero(self):
        assert costs.estimate_cost("claude-haiku-4-5", TokenUsage()) == 0.0

    def test_prices_verified_date_present(self):
        # The table is dated so an operator can judge staleness.
        assert isinstance(costs.PRICES_VERIFIED, str) and costs.PRICES_VERIFIED
