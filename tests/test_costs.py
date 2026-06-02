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

    def test_anthropic_other_non_deprecated_models(self):
        # Opus + Sonnet versions an operator might switch to (1M input + 1M out).
        usage = TokenUsage(input=1_000_000, output=1_000_000, cached=0, cache_write=0)
        assert costs.estimate_cost("claude-opus-4-7", usage) == pytest.approx(30.0)
        assert costs.estimate_cost("claude-opus-4-6", usage) == pytest.approx(30.0)
        assert costs.estimate_cost("claude-opus-4-5", usage) == pytest.approx(30.0)
        assert costs.estimate_cost("claude-opus-4-1", usage) == pytest.approx(90.0)
        assert costs.estimate_cost("claude-sonnet-4-5", usage) == pytest.approx(18.0)

    def test_gemini_flash_lite(self):
        usage = TokenUsage(input=1_000_000, output=1_000_000, cached=0, cache_write=0)
        # $0.10 input + $0.40 output.
        assert costs.estimate_cost("gemini-2.5-flash-lite", usage) == pytest.approx(
            0.50
        )

    def test_gemini_pro(self):
        usage = TokenUsage(input=1_000_000, output=1_000_000, cached=0, cache_write=0)
        assert costs.estimate_cost("gemini-2.5-pro", usage) == pytest.approx(11.25)

    def test_gemini_other_non_deprecated_models_priced(self):
        # The fuller Gemini lineup an operator might switch to (input + output
        # @ 1M each). $/1M: flash 0.30+2.50, 3.5-flash 1.50+9, 3.1-pro 2+12,
        # 3.1-flash-lite 0.25+1.50.
        usage = TokenUsage(input=1_000_000, output=1_000_000, cached=0, cache_write=0)
        assert costs.estimate_cost("gemini-2.5-flash", usage) == pytest.approx(2.80)
        assert costs.estimate_cost("gemini-3.5-flash", usage) == pytest.approx(10.50)
        assert costs.estimate_cost("gemini-3.1-pro-preview", usage) == pytest.approx(
            14.00
        )
        assert costs.estimate_cost("gemini-3.1-flash-lite", usage) == pytest.approx(
            1.75
        )

    def test_gemini_flash_not_mispriced_as_flash_lite(self):
        # `gemini-2.5-flash-lite` is a prefix-superstring of `gemini-2.5-flash`;
        # each must keep its own rate, and a dated flash id must resolve to
        # flash (0.30), not flash-lite (0.10).
        usage = TokenUsage(input=1_000_000, output=0, cached=0, cache_write=0)
        assert costs.estimate_cost("gemini-2.5-flash", usage) == pytest.approx(0.30)
        assert costs.estimate_cost("gemini-2.5-flash-lite", usage) == pytest.approx(
            0.10
        )
        assert costs.estimate_cost("gemini-2.5-flash-002", usage) == pytest.approx(0.30)

    def test_openai_nano_extractor_default(self):
        # gpt-5.4-nano (the OpenAI extractor default): $0.20 input + $1.25 out.
        usage = TokenUsage(input=1_000_000, output=1_000_000, cached=0, cache_write=0)
        assert costs.estimate_cost("gpt-5.4-nano", usage) == pytest.approx(1.45)

    def test_openai_summary_default(self):
        # gpt-5.4 (the OpenAI summary default): $2.50 input + $15.00 output.
        usage = TokenUsage(input=1_000_000, output=1_000_000, cached=0, cache_write=0)
        assert costs.estimate_cost("gpt-5.4", usage) == pytest.approx(17.50)

    def test_openai_cached_input_rate(self):
        # OpenAI cached prompt tokens billed at the cheaper cached-input rate
        # ($0.02 for nano), not the $0.20 input rate.
        usage = TokenUsage(input=1_000_000, output=0, cached=1_000_000, cache_write=0)
        assert costs.estimate_cost("gpt-5.4-nano", usage) == pytest.approx(0.02)

    def test_openai_variant_not_mispriced_as_base(self):
        # gpt-5.4-mini must use its own rate, not fall back to gpt-5.4.
        usage = TokenUsage(input=1_000_000, output=0, cached=0, cache_write=0)
        assert costs.estimate_cost("gpt-5.4-mini", usage) == pytest.approx(0.75)
        assert costs.estimate_cost("gpt-5.4", usage) == pytest.approx(2.50)

    def test_openai_gpt5_family(self):
        usage = TokenUsage(input=1_000_000, output=1_000_000, cached=0, cache_write=0)
        assert costs.estimate_cost("gpt-5", usage) == pytest.approx(11.25)
        assert costs.estimate_cost("gpt-5-mini", usage) == pytest.approx(2.25)
        assert costs.estimate_cost("gpt-5-nano", usage) == pytest.approx(0.45)
        assert costs.estimate_cost("gpt-5-pro", usage) == pytest.approx(135.0)
        assert costs.estimate_cost("gpt-5.1", usage) == pytest.approx(11.25)
        assert costs.estimate_cost("gpt-5.2", usage) == pytest.approx(15.75)
        assert costs.estimate_cost("gpt-5.2-pro", usage) == pytest.approx(189.0)

    def test_unknown_model_returns_none(self):
        # A legacy/unlisted model (or any LLM_MODEL override we didn't price).
        usage = TokenUsage(input=1000, output=100)
        assert costs.estimate_cost("gpt-4o-mini", usage) is None
        assert costs.estimate_cost("some-future-model", usage) is None
        # Snapshot suffix strips to a base that still isn't in the table -> None.
        assert costs.estimate_cost("frob-9-001", usage) is None

    def test_snapshot_suffix_resolves_to_base(self):
        # Date/pin-suffixed ids resolve to the base model's rate...
        usage = TokenUsage(input=1_000_000, output=0, cached=0, cache_write=0)
        assert costs.estimate_cost("claude-haiku-4-5-20251001", usage) == pytest.approx(
            1.0
        )
        assert costs.estimate_cost("gpt-5.4-2026-01-15", usage) == pytest.approx(2.50)

    def test_unlisted_sibling_version_not_mispriced(self):
        # ...but a sibling VERSION we don't list (a hypothetical gpt-5.3) is a
        # word-suffix, not a snapshot, so it stays unpriced rather than being
        # mis-priced as gpt-5.
        usage = TokenUsage(input=1_000_000, output=0, cached=0, cache_write=0)
        assert costs.estimate_cost("gpt-5.3", usage) is None

    def test_zero_usage_is_zero(self):
        assert costs.estimate_cost("claude-haiku-4-5", TokenUsage()) == 0.0

    def test_ollama_provider_is_free_regardless_of_model(self):
        # Local inference has no per-token API charge, so the ollama provider
        # bills $0.00 for ANY model name — including names the table doesn't
        # list, which would otherwise return None (unpriced). The provider, not
        # the model string, is what makes it free.
        usage = TokenUsage(input=1_000_000, output=1_000_000)
        assert costs.estimate_cost("llama3.1", usage, provider="ollama") == 0.0
        assert (
            costs.estimate_cost("some-local-model-we-never-heard-of", usage, "ollama")
            == 0.0
        )
        # An empty-usage local call is also $0, not None.
        assert costs.estimate_cost("llama3.1", TokenUsage(), "ollama") == 0.0

    def test_provider_arg_defaults_to_table_lookup(self):
        # When provider is omitted (or a hosted provider is passed), pricing
        # still comes from the per-model table — the ollama short-circuit must
        # not change hosted behavior.
        usage = TokenUsage(input=1_000_000, output=1_000_000, cached=0, cache_write=0)
        assert costs.estimate_cost("claude-haiku-4-5", usage) == pytest.approx(6.0)
        assert costs.estimate_cost(
            "claude-haiku-4-5", usage, provider="anthropic"
        ) == pytest.approx(6.0)
        # A hosted provider with an unlisted model is still unpriced (None),
        # NOT zeroed — only ollama is free.
        assert costs.estimate_cost("gpt-4o-mini", usage, provider="openai") is None

    def test_prices_verified_date_present(self):
        # The table is dated so an operator can judge staleness.
        assert isinstance(costs.PRICES_VERIFIED, str) and costs.PRICES_VERIFIED
