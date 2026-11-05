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
        assert costs.estimate_cost("claude-opus-4-8", usage) == pytest.approx(30.0)

    def test_anthropic_claude_5_models(self):
        # $/1M input + output: Fable / Mythos 10+50, Opus 5.5 4+20, Opus 5
        # 5+25, Sonnet 5.5 / Sonnet 5 2+10, Haiku 5.5 0.10+0.50 (short prompt).
        usage = TokenUsage(input=50_000, output=50_000, cached=0, cache_write=0)
        expected = {
            "claude-fable-5-1": 60.0,
            "claude-mythos-5-1": 60.0,
            "claude-fable-5": 60.0,
            "claude-mythos-5": 60.0,
            "claude-opus-5-5": 24.0,
            "claude-opus-5": 30.0,
            "claude-sonnet-5-5": 12.0,
            "claude-sonnet-5": 12.0,
            "claude-haiku-5-5": 0.60,
        }
        for model, per_mtok in expected.items():
            assert costs.estimate_cost(model, usage) == pytest.approx(
                per_mtok * 0.05
            ), model

    def test_anthropic_cache_read_rates_that_differ_from_one_tenth(self):
        # The pricing page's footnotes: cache hits are 0.025x base input on
        # Fable 5.1 / Mythos 5.1 and 0.05x on Opus 5.5 / Sonnet 5.5, not the
        # usual 0.1x. 1M tokens all served from cache.
        usage = TokenUsage(input=1_000_000, output=0, cached=1_000_000, cache_write=0)
        assert costs.estimate_cost("claude-fable-5-1", usage) == pytest.approx(0.25)
        assert costs.estimate_cost("claude-mythos-5-1", usage) == pytest.approx(0.25)
        assert costs.estimate_cost("claude-opus-5-5", usage) == pytest.approx(0.20)
        assert costs.estimate_cost("claude-sonnet-5-5", usage) == pytest.approx(0.10)
        assert costs.estimate_cost("claude-fable-5", usage) == pytest.approx(1.00)

    def test_deprecated_and_retired_models_are_unpriced(self):
        # Omitted per the providers' deprecation notices, so they log
        # cost_est=? rather than carrying a rate nobody re-checks.
        usage = TokenUsage(input=1000, output=100)
        for model in (
            "claude-sonnet-4-5",
            "claude-opus-4-1",
            "gemini-2.5-flash",
            "gemini-3.5-flash",
        ):
            assert costs.estimate_cost(model, usage) is None, model

    def test_gemini_flash_lite(self):
        usage = TokenUsage(input=1_000_000, output=1_000_000, cached=0, cache_write=0)
        # $0.10 input + $0.40 output.
        assert costs.estimate_cost("gemini-2.5-flash-lite", usage) == pytest.approx(
            0.50
        )

    def test_gemini_pro(self):
        # A prompt under the 200K limit: $1.25 input + $10.00 output per 1M.
        usage = TokenUsage(input=100_000, output=100_000, cached=0, cache_write=0)
        assert costs.estimate_cost("gemini-2.5-pro", usage) == pytest.approx(1.125)

    def test_gemini_other_non_deprecated_models_priced(self):
        # The fuller Gemini lineup an operator might switch to (input + output
        # @ 1M each). $/1M: 3.8 / 3.6 flash 0.75+3.75, 3.5-flash-lite
        # 0.30+2.50, 3.1-pro 2+12 (short prompt), 3.1-flash-lite 0.25+1.50,
        # 3-flash-preview 0.50+3.00.
        usage = TokenUsage(input=100_000, output=100_000, cached=0, cache_write=0)
        expected = {
            "gemini-3.8-flash": 4.50,
            "gemini-3.6-flash": 4.50,
            "gemini-3.5-flash-lite": 2.80,
            "gemini-3.1-pro-preview": 14.00,
            "gemini-3.1-flash-lite": 1.75,
            "gemini-3-flash-preview": 3.50,
        }
        for model, per_mtok in expected.items():
            assert costs.estimate_cost(model, usage) == pytest.approx(per_mtok * 0.1), (
                model
            )

    def test_unlisted_tier_not_mispriced_as_listed_model(self):
        # An unlisted `-lite` tier of a listed model (no gemini-3.8-flash-lite
        # exists in the table) is a word suffix, not a snapshot, so it stays
        # unpriced rather than falling back to gemini-3.8-flash; a pinned
        # flash-lite id still resolves to its own rate.
        usage = TokenUsage(input=1_000_000, output=0, cached=0, cache_write=0)
        assert costs.estimate_cost("gemini-3.8-flash-lite", usage) is None
        assert costs.estimate_cost("gemini-3.1-flash-lite-002", usage) == pytest.approx(
            0.25
        )

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

    def test_openai_gpt6_and_gpt56_models(self):
        # $/1M input + output, standard tier, short context.
        usage = TokenUsage(input=100_000, output=100_000, cached=0, cache_write=0)
        expected = {
            "gpt-6-astra": 60.0,
            "gpt-6.1-sol": 12.0,
            "gpt-6.1-terra": 6.0,
            "gpt-6-sol": 12.0,
            "gpt-6-luna": 0.60,
            "gpt-5.6-sol": 24.0,
            "gpt-5.6-terra": 14.0,
            "gpt-5.6-luna": 1.40,
            "gpt-5.5": 35.0,
            "gpt-5.5-pro": 210.0,
            "gpt-5.4-pro": 210.0,
        }
        for model, per_mtok in expected.items():
            assert costs.estimate_cost(model, usage) == pytest.approx(per_mtok * 0.1), (
                model
            )

    def test_openai_cache_write_rate_applies_when_reported(self):
        # gpt-6.1-sol lists a 30-minute cache-write rate ($2.50); the older
        # models list none, so their written tokens cost nothing extra.
        # A 200K prompt, all written to the cache: 200K @ $2.50 = $0.50.
        usage = TokenUsage(input=200_000, output=0, cached=0, cache_write=200_000)
        assert costs.estimate_cost("gpt-6.1-sol", usage) == pytest.approx(0.50)
        assert costs.estimate_cost("gpt-5.4", usage) == pytest.approx(0.0)

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


class TestLongPromptRates:
    def test_haiku_5_5_at_the_limit_uses_standard_rates(self):
        # Exactly 100,000 prompt tokens is "up to 100,000": standard rates.
        usage = TokenUsage(input=100_000, output=1_000, cached=0, cache_write=0)
        assert costs.estimate_cost("claude-haiku-5-5", usage) == pytest.approx(
            (100_000 * 0.10 + 1_000 * 0.50) / 1_000_000
        )

    def test_haiku_5_5_over_the_limit_bills_whole_call_at_higher_rates(self):
        # One token over: EVERY slice moves to the higher rates, not just the
        # tokens past the limit. 100,001 uncached @ $0.50 + 1,000 out @ $2.50.
        usage = TokenUsage(input=100_001, output=1_000, cached=0, cache_write=0)
        assert costs.estimate_cost("claude-haiku-5-5", usage) == pytest.approx(
            (100_001 * 0.50 + 1_000 * 2.50) / 1_000_000
        )

    def test_cached_tokens_count_toward_the_limit(self):
        # Anthropic counts cache reads and writes in the prompt size, so a
        # mostly-cached 150K prompt is still over the limit, and its cache
        # reads / writes bill at the higher cache rates ($0.05 / $0.625).
        usage = TokenUsage(input=150_000, output=0, cached=120_000, cache_write=20_000)
        assert costs.estimate_cost("claude-haiku-5-5", usage) == pytest.approx(
            (10_000 * 0.50 + 120_000 * 0.05 + 20_000 * 0.625) / 1_000_000
        )

    def test_snapshot_id_gets_the_long_prompt_rates_too(self):
        usage = TokenUsage(input=200_000, output=0, cached=0, cache_write=0)
        assert costs.estimate_cost("claude-haiku-5-5-20261007", usage) == pytest.approx(
            200_000 * 0.50 / 1_000_000
        )

    def test_gemini_pro_over_200k(self):
        # 3.1 Pro: $2 -> $4 input, $12 -> $18 output past 200K; 2.5 Pro:
        # $1.25 -> $2.50 input, $10 -> $15 output.
        short = TokenUsage(input=200_000, output=10_000)
        long = TokenUsage(input=200_001, output=10_000)
        assert costs.estimate_cost("gemini-3.1-pro-preview", short) == pytest.approx(
            (200_000 * 2.00 + 10_000 * 12.00) / 1_000_000
        )
        assert costs.estimate_cost("gemini-3.1-pro-preview", long) == pytest.approx(
            (200_001 * 4.00 + 10_000 * 18.00) / 1_000_000
        )
        assert costs.estimate_cost("gemini-2.5-pro", long) == pytest.approx(
            (200_001 * 2.50 + 10_000 * 15.00) / 1_000_000
        )

    def test_models_without_a_long_prompt_tier_keep_one_rate(self):
        # Sonnet 4.6 bills its full context window at one rate.
        usage = TokenUsage(input=900_000, output=0)
        assert costs.estimate_cost("claude-sonnet-4-6", usage) == pytest.approx(2.70)

    def test_every_long_prompt_model_has_standard_rates(self):
        # A long-prompt entry with no standard row would never be reached
        # (_rates returns None first), so a typo'd key would silently do
        # nothing. Pin that every key is also in the main table.
        assert set(costs._LONG_PROMPT_RATES) <= set(costs._RATES_USD_PER_MTOK)

    def test_openai_over_272k(self):
        # OpenAI's long-context view: gpt-6.1-sol $2 -> $4 input, $10 -> $15
        # output; gpt-6-luna $0.10 -> $0.20, $0.50 -> $0.75; gpt-6-astra $10 ->
        # $20, $50 -> $75; gpt-6.1-terra $1 -> $2, $5 -> $7.50; gpt-5.6-sol $4
        # -> $8, $20 -> $30. Exactly 272,000 tokens is still short context.
        short = TokenUsage(input=272_000, output=10_000)
        long = TokenUsage(input=272_001, output=10_000)
        assert costs.estimate_cost("gpt-6.1-sol", short) == pytest.approx(
            (272_000 * 2.00 + 10_000 * 10.00) / 1_000_000
        )
        expected_long = {
            "gpt-6.1-sol": (4.00, 15.00),
            "gpt-6-luna": (0.20, 0.75),
            "gpt-6-astra": (20.00, 75.00),
            "gpt-6.1-terra": (2.00, 7.50),
            "gpt-5.6-sol": (8.00, 30.00),
        }
        for model, (inp, out) in expected_long.items():
            assert costs.estimate_cost(model, long) == pytest.approx(
                (272_001 * inp + 10_000 * out) / 1_000_000
            ), model

    def test_openai_long_context_cache_rates(self):
        # Over 272K, cached input and cache writes move up too (gpt-6-luna:
        # $0.02 cached, $0.25 30-minute write).
        usage = TokenUsage(input=300_000, output=0, cached=200_000, cache_write=50_000)
        assert costs.estimate_cost("gpt-6-luna", usage) == pytest.approx(
            (50_000 * 0.20 + 200_000 * 0.02 + 50_000 * 0.25) / 1_000_000
        )
