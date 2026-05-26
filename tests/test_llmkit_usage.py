"""Tests for the LLM token-telemetry module (`case_calendar.llmkit.usage`)."""

from __future__ import annotations

import logging
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

from case_calendar.llmkit import providers, usage
from case_calendar.llmkit.usage import (
    TokenLedger,
    TokenUsage,
    _as_int,
    from_anthropic,
    from_gemini,
    from_openai,
)


class TestAsInt:
    def test_plain_int(self):
        assert _as_int(7) == 7

    def test_float_truncates(self):
        assert _as_int(5.9) == 5

    def test_bool_is_rejected(self):
        # bool is an int subclass, but a usage field as True/False is a bug.
        assert _as_int(True) == 0
        assert _as_int(False) == 0

    def test_none_and_str_and_mock_become_zero(self):
        assert _as_int(None) == 0
        assert _as_int("12") == 0
        assert _as_int(MagicMock()) == 0


class TestTokenUsage:
    def test_add(self):
        a = TokenUsage(input=10, output=2, cached=5, cache_write=1)
        b = TokenUsage(input=20, output=4, cached=0, cache_write=3)
        assert a + b == TokenUsage(input=30, output=6, cached=5, cache_write=4)

    def test_str(self):
        assert (
            str(TokenUsage(input=10, output=2, cached=5, cache_write=1))
            == "in=10 out=2 cached=5 cache_write=1"
        )

    def test_default_is_zero(self):
        assert TokenUsage() == TokenUsage(0, 0, 0, 0)


class TestExtractors:
    def test_from_anthropic_folds_cache_into_input(self):
        resp = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=20,
                cache_read_input_tokens=80,
                cache_creation_input_tokens=10,
            )
        )
        # input = uncached(100) + read(80) + write(10)
        assert from_anthropic(resp) == TokenUsage(
            input=190, output=20, cached=80, cache_write=10
        )

    def test_from_anthropic_missing_usage(self):
        assert from_anthropic(SimpleNamespace()) == TokenUsage()

    def test_from_openai_prompt_includes_cached(self):
        resp = SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=150,
                completion_tokens=30,
                prompt_tokens_details=SimpleNamespace(cached_tokens=120),
            )
        )
        assert from_openai(resp) == TokenUsage(
            input=150, output=30, cached=120, cache_write=0
        )

    def test_from_openai_no_details(self):
        resp = SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=10, completion_tokens=2, prompt_tokens_details=None
            )
        )
        assert from_openai(resp) == TokenUsage(input=10, output=2, cached=0)

    def test_from_openai_missing_usage(self):
        assert from_openai(SimpleNamespace()) == TokenUsage()

    def test_from_gemini(self):
        resp = SimpleNamespace(
            usage_metadata=SimpleNamespace(
                prompt_token_count=200,
                candidates_token_count=40,
                cached_content_token_count=160,
            )
        )
        assert from_gemini(resp) == TokenUsage(
            input=200, output=40, cached=160, cache_write=0
        )

    def test_from_gemini_missing_metadata(self):
        assert from_gemini(SimpleNamespace()) == TokenUsage()


class TestTokenLedger:
    def _seed(self) -> TokenLedger:
        led = TokenLedger()
        led.record(
            purpose="extract",
            provider="anthropic",
            model="m",
            tokens=TokenUsage(10, 2, 5, 1),
            docket=1,
        )
        led.record(
            purpose="verify_hearing",
            provider="anthropic",
            model="m",
            tokens=TokenUsage(20, 4, 0, 0),
            docket=1,
        )
        led.record(
            purpose="summary",
            provider="anthropic",
            model="m2",
            tokens=TokenUsage(100, 50, 80, 10),
            docket=2,
        )
        return led

    def test_record_logs_per_call_line(self, caplog):
        led = TokenLedger()
        with caplog.at_level(logging.INFO, logger="case_calendar.llmkit.usage"):
            led.record(
                purpose="extract",
                provider="anthropic",
                model="claude-x",
                tokens=TokenUsage(10, 2, 5, 1),
                docket=42,
            )
        msgs = [r.getMessage() for r in caplog.records]
        assert any(
            "llm-tokens call purpose=extract provider=anthropic model=claude-x "
            "docket=42 in=10 out=2 cached=5 cache_write=1" == m
            for m in msgs
        )

    def test_log_summary_emits_per_docket_and_total(self, caplog):
        led = self._seed()
        with caplog.at_level(logging.INFO, logger="case_calendar.llmkit.usage"):
            led.log_summary(scope="sync")
        msgs = [r.getMessage() for r in caplog.records]
        assert "llm-tokens docket=1 calls=2 in=30 out=6 cached=5 cache_write=1" in msgs
        assert (
            "llm-tokens docket=2 calls=1 in=100 out=50 cached=80 cache_write=10" in msgs
        )
        assert (
            "llm-tokens sync TOTAL calls=3 dockets=2 models=2 in=130 out=56 "
            "cached=85 cache_write=11" in msgs
        )

    def test_log_summary_emits_per_model_subtotals(self, caplog):
        # The per-model lines are what separate the extractor track (model "m"
        # here: the two extract / verify calls) from the summary track (model
        # "m2": the one summary call) in the run total.
        led = self._seed()
        with caplog.at_level(logging.INFO, logger="case_calendar.llmkit.usage"):
            led.log_summary(scope="sync")
        msgs = [r.getMessage() for r in caplog.records]
        assert "llm-tokens model=m calls=2 in=30 out=6 cached=5 cache_write=1" in msgs
        assert (
            "llm-tokens model=m2 calls=1 in=100 out=50 cached=80 cache_write=10" in msgs
        )

    def test_empty_ledger_log_summary_is_noop(self, caplog):
        with caplog.at_level(logging.INFO, logger="case_calendar.llmkit.usage"):
            TokenLedger().log_summary()
        assert not any("TOTAL" in r.getMessage() for r in caplog.records)

    def test_reset_clears(self, caplog):
        led = self._seed()
        led.reset()
        with caplog.at_level(logging.INFO, logger="case_calendar.llmkit.usage"):
            led.log_summary()
        assert not any("TOTAL" in r.getMessage() for r in caplog.records)

    def test_none_docket_buckets_under_question_mark(self, caplog):
        led = TokenLedger()
        led.record(
            purpose="extract",
            provider="openai",
            model="m",
            tokens=TokenUsage(5, 1, 0, 0),
            docket=None,
        )
        with caplog.at_level(logging.INFO, logger="case_calendar.llmkit.usage"):
            led.log_summary()
        assert any("docket=? calls=1" in r.getMessage() for r in caplog.records)


class TestModuleFacade:
    def test_record_log_summary_reset(self, caplog):
        usage.record(
            purpose="summary",
            provider="gemini",
            model="g",
            tokens=TokenUsage(7, 3, 0, 0),
            docket=9,
        )
        with caplog.at_level(logging.INFO, logger="case_calendar.llmkit.usage"):
            usage.log_summary(scope="run")
        assert any(
            "run TOTAL calls=1 dockets=1 models=1 in=7 out=3" in r.getMessage()
            for r in caplog.records
        )
        usage.reset()
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="case_calendar.llmkit.usage"):
            usage.log_summary()
        assert not any("TOTAL" in r.getMessage() for r in caplog.records)


class TestCallRecordsUsage:
    """Wiring check: a provider call function records into the process-wide
    ledger with the purpose / docket it was handed."""

    def test_call_anthropic_records(self, monkeypatch, caplog):
        resp = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="{}")],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=20,
                cache_read_input_tokens=80,
                cache_creation_input_tokens=0,
            ),
        )
        fake = types.ModuleType("anthropic")

        class _Anthropic:
            def __init__(self, **kwargs):
                self.messages = SimpleNamespace(create=lambda **kw: resp)

        fake.Anthropic = _Anthropic  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "anthropic", fake)

        text = providers._call_anthropic(
            "sys", "user", 100, model="claude-x", purpose="extract", docket=42
        )
        assert text == "{}"
        with caplog.at_level(logging.INFO, logger="case_calendar.llmkit.usage"):
            usage.log_summary()
        # input folds the 80 cache-read tokens in: 100 + 80 + 0 = 180.
        assert any(
            "docket=42 calls=1 in=180 out=20 cached=80 cache_write=0" in r.getMessage()
            for r in caplog.records
        )


class TestLedgerCostEstimation:
    """The opt-in price estimator: when set, per-call + summary lines carry a
    `cost_est=` field; unpriced models are flagged, not silently dropped."""

    def test_no_estimator_means_no_cost_field(self, caplog):
        led = TokenLedger()  # default: no estimator
        with caplog.at_level(logging.INFO, logger="case_calendar.llmkit.usage"):
            led.record(
                purpose="extract",
                provider="anthropic",
                model="m",
                tokens=TokenUsage(10, 2, 0, 0),
                docket=1,
            )
        assert all("cost_est" not in r.getMessage() for r in caplog.records)

    def test_per_call_line_carries_cost(self, caplog):
        led = TokenLedger()
        led.set_price_estimator(lambda model, tokens: 0.0025)
        with caplog.at_level(logging.INFO, logger="case_calendar.llmkit.usage"):
            led.record(
                purpose="summary",
                provider="anthropic",
                model="m",
                tokens=TokenUsage(100, 20, 0, 0),
                docket=7,
            )
        assert any("cost_est=$0.0025" in r.getMessage() for r in caplog.records)

    def test_summary_totals_include_cost(self, caplog):
        led = TokenLedger()
        led.set_price_estimator(lambda model, tokens: 1.0)
        for d in (1, 1, 2):
            led.record(
                purpose="extract",
                provider="anthropic",
                model="m",
                tokens=TokenUsage(10, 2, 0, 0),
                docket=d,
            )
        with caplog.at_level(logging.INFO, logger="case_calendar.llmkit.usage"):
            led.log_summary(scope="sync")
        msgs = [r.getMessage() for r in caplog.records]
        assert any("docket=1 calls=2" in m and "cost_est=$2.0000" in m for m in msgs)
        assert any("docket=2 calls=1" in m and "cost_est=$1.0000" in m for m in msgs)
        # All three calls ran on model "m" -> one per-model line with the
        # combined cost.
        assert any("model=m calls=3" in m and "cost_est=$3.0000" in m for m in msgs)
        assert any("TOTAL calls=3" in m and "cost_est=$3.0000" in m for m in msgs)

    def test_unpriced_calls_flagged_partial(self, caplog):
        # Estimator prices "known" but returns None for "unknown".
        led = TokenLedger()
        led.set_price_estimator(lambda model, tokens: 0.5 if model == "known" else None)
        led.record(
            purpose="extract",
            provider="x",
            model="known",
            tokens=TokenUsage(10, 2, 0, 0),
            docket=1,
        )
        led.record(
            purpose="extract",
            provider="x",
            model="unknown",
            tokens=TokenUsage(10, 2, 0, 0),
            docket=1,
        )
        with caplog.at_level(logging.INFO, logger="case_calendar.llmkit.usage"):
            led.log_summary()
        msgs = [r.getMessage() for r in caplog.records]
        # Only the priced call counts toward the dollar figure...
        assert any(
            "TOTAL" in m
            and "cost_est=$0.5000" in m
            and "1 call(s) had no price entry" in m
            for m in msgs
        )
        # ...and the per-model lines show the priced model's dollar figure
        # while the unpriced model is flagged with `?` rather than $0.0000.
        assert any("model=known calls=1" in m and "cost_est=$0.5000" in m for m in msgs)
        assert any("model=unknown calls=1" in m and "cost_est=?" in m for m in msgs)

    def test_unpriced_per_call_shows_question_mark(self, caplog):
        led = TokenLedger()
        led.set_price_estimator(lambda model, tokens: None)
        with caplog.at_level(logging.INFO, logger="case_calendar.llmkit.usage"):
            led.record(
                purpose="extract",
                provider="x",
                model="m",
                tokens=TokenUsage(10, 2, 0, 0),
                docket=1,
            )
        assert any("cost_est=?" in r.getMessage() for r in caplog.records)

    def test_reset_clears_estimator(self, caplog):
        led = TokenLedger()
        led.set_price_estimator(lambda model, tokens: 1.0)
        led.reset()
        with caplog.at_level(logging.INFO, logger="case_calendar.llmkit.usage"):
            led.record(
                purpose="extract",
                provider="x",
                model="m",
                tokens=TokenUsage(10, 2, 0, 0),
                docket=1,
            )
        assert all("cost_est" not in r.getMessage() for r in caplog.records)

    def test_module_facade_set_price_estimator(self, caplog):
        usage.set_price_estimator(lambda model, tokens: 0.01)
        with caplog.at_level(logging.INFO, logger="case_calendar.llmkit.usage"):
            usage.record(
                purpose="summary",
                provider="x",
                model="m",
                tokens=TokenUsage(10, 2, 0, 0),
                docket=3,
            )
        assert any("cost_est=$0.0100" in r.getMessage() for r in caplog.records)
