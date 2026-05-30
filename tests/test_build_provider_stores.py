"""Tests for the per-provider log + DECISION-trace capture in the
``model-comparison/build_provider_stores.py`` comparison tool.

The script lives outside the ``case_calendar`` package, so it's loaded by path.
We exercise the new, independently-testable units: the thread-local-routed log
handler, the decision-line formatters, and the LLM-wrapping factory."""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "model-comparison"
    / "build_provider_stores.py"
)
_spec = importlib.util.spec_from_file_location("build_provider_stores", _SCRIPT)
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
# Register before exec so the module-level @dataclass can resolve its own
# __module__ in sys.modules during class processing.
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)


@pytest.fixture(autouse=True)
def _reset_threadlocal():
    """Each test sets the ``_TL`` slots it needs; clear them afterwards so a
    leaked value can't route a later test's records into a stray file/column."""
    yield
    mod._TL.provider = None
    mod._TL.label = None
    mod._TL.extract_model = None
    mod.TIMING.wall.clear()
    mod.TIMING.call_secs.clear()


# --------------------------------------------------------------------------- #
# _action_brief
# --------------------------------------------------------------------------- #


def test_action_brief_type_only():
    assert mod._action_brief({"type": "ignore"}) == "IGNORE"


def test_action_brief_full():
    b = mod._action_brief(
        {
            "type": "add",
            "hearing_key": "sentencing-wang",
            "significance": "major",
            "local_date": "2026-06-03",
        }
    )
    assert b.startswith("ADD(")
    assert "sentencing-wang" in b and "major" in b and "2026-06-03" in b


def test_action_brief_deadline_and_target_keys():
    assert "resp-due" in mod._action_brief(
        {"type": "add_deadline", "deadline_key": "resp-due"}
    )
    assert "keep-me" in mod._action_brief(
        {"type": "merge_into", "target_key": "keep-me"}
    )


def test_action_brief_non_dict():
    assert mod._action_brief("nope") == "'nope'"


# --------------------------------------------------------------------------- #
# _format_decision
# --------------------------------------------------------------------------- #


def test_format_decision_extract():
    out = mod._format_decision(
        "extract",
        {
            "docket_id": 99,
            "entry": {"id": 7, "short_description": "Order setting Jury Trial"},
        },
        [{"type": "add", "hearing_key": "jury-trial-wang", "significance": "major"}],
    )
    assert "extract docket=99 entry=7" in out
    assert "Order setting Jury Trial" in out
    assert "jury-trial-wang" in out and "ADD" in out


def test_format_decision_extract_no_actions():
    out = mod._format_decision("extract", {"entry": {"id": 1}}, [])
    assert "(none)" in out


def test_format_decision_extract_truncates_long_description():
    out = mod._format_decision(
        "extract", {"entry": {"id": 1, "description": "x" * 200}}, []
    )
    assert "…" in out  # _short truncated the body


def test_format_decision_verify_hearing():
    out = mod._format_decision(
        "verify_hearing",
        {
            "hearing": {
                "hearing_key": "sentencing-wang",
                "starts_at_utc": "2026-01-02T00:00:00",
                "status": "scheduled",
            }
        },
        {"type": "mark_held"},
    )
    assert "verify_hearing" in out and "sentencing-wang" in out and "MARK_HELD" in out


def test_format_decision_verify_deadline():
    out = mod._format_decision(
        "verify_deadline",
        {
            "deadline": {
                "deadline_key": "resp-due",
                "due_at_utc": "2026-05-24T00:00:00",
                "status": "pending",
            }
        },
        {"type": "confirm"},
    )
    assert "verify_deadline" in out and "resp-due" in out and "CONFIRM" in out


def test_format_decision_dedupe():
    out = mod._format_decision(
        "dedupe",
        {"cluster": [{"hearing_key": "a", "starts_at_utc": "T"}, {"hearing_key": "b"}]},
        {"type": "merge_into", "target_key": "a"},
    )
    assert "dedupe cluster=[a, b]" in out and "MERGE_INTO" in out


def test_format_decision_unknown_kind():
    assert "weird ->" in mod._format_decision("weird", {}, {"x": 1})


# --------------------------------------------------------------------------- #
# _PerProviderLogHandler
# --------------------------------------------------------------------------- #


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("x", logging.INFO, __file__, 1, msg, None, None)


def test_handler_routes_by_threadlocal(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "OUT_DIR", tmp_path)
    h = mod._PerProviderLogHandler()
    h.setFormatter(logging.Formatter("%(message)s"))
    mod._TL.provider = "gemini"
    h.emit(_record("hi gemini"))
    h.close()
    assert (tmp_path / "gemini" / "build.log").read_text().strip() == "hi gemini"


def test_handler_separates_providers(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "OUT_DIR", tmp_path)
    h = mod._PerProviderLogHandler()
    h.setFormatter(logging.Formatter("%(message)s"))
    mod._TL.provider = "openai"
    h.emit(_record("for openai"))
    mod._TL.provider = "anthropic"
    h.emit(_record("for anthropic"))
    h.close()
    assert (tmp_path / "openai" / "build.log").read_text().strip() == "for openai"
    assert (tmp_path / "anthropic" / "build.log").read_text().strip() == "for anthropic"


def test_handler_drops_records_without_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "OUT_DIR", tmp_path)
    h = mod._PerProviderLogHandler()
    mod._TL.provider = None
    h.emit(_record("orphan"))
    h.close()
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------- #
# _DropDecisions filter
# --------------------------------------------------------------------------- #


def test_drop_decisions_filter():
    f = mod._DropDecisions()
    assert f.filter(_record("keep")) is True  # name "x" != decisions logger
    dec = logging.LogRecord(
        mod._DLOG.name, logging.INFO, __file__, 1, "drop", None, None
    )
    assert f.filter(dec) is False


# --------------------------------------------------------------------------- #
# _wrap_llm
# --------------------------------------------------------------------------- #


def test_wrap_llm_logs_decision_and_returns(monkeypatch, caplog):
    sentinel = [{"type": "add", "hearing_key": "k1", "significance": "major"}]
    monkeypatch.setattr(mod.llm, "extract_actions", lambda **k: sentinel)
    wrapped = mod._wrap_llm("extract_actions", "extract")
    mod._TL.provider = "openai"
    with caplog.at_level(logging.INFO, logger=mod._DLOG.name):
        result = wrapped(entry={"id": 5, "short_description": "Order"}, docket_id=9)
    assert result is sentinel  # real result passed through unchanged
    assert any("k1" in r.message and "ADD" in r.message for r in caplog.records)


def test_wrap_llm_silent_without_provider(monkeypatch, caplog):
    monkeypatch.setattr(mod.llm, "verify_hearing", lambda **k: {"type": "CONFIRM"})
    wrapped = mod._wrap_llm("verify_hearing", "verify_hearing")
    mod._TL.provider = None
    with caplog.at_level(logging.INFO, logger=mod._DLOG.name):
        result = wrapped(hearing={"hearing_key": "h"})
    assert result == {"type": "CONFIRM"}
    assert not caplog.records  # no provider context -> no decision line


def test_wrap_llm_logging_failure_never_propagates(monkeypatch):
    monkeypatch.setattr(mod.llm, "verify_deadline", lambda **k: {"type": "CONFIRM"})
    wrapped = mod._wrap_llm("verify_deadline", "verify_deadline")
    # Force the formatter to blow up; the wrapper must swallow it and still
    # return the real result.
    monkeypatch.setattr(mod, "_format_decision", lambda *a, **k: 1 / 0)
    mod._TL.provider = "gemini"
    assert wrapped(deadline={"deadline_key": "d"}) == {"type": "CONFIRM"}


# --------------------------------------------------------------------------- #
# Variant set + parsing
# --------------------------------------------------------------------------- #


def test_default_variants_cover_providers_plus_eval_candidates():
    by_label = {v.label: v for v in mod.VARIANTS}
    # One column per provider at its out-of-the-box models; label is
    # provider/extraction-model.
    assert by_label["anthropic/claude-haiku-4-5"].provider == "anthropic"
    assert by_label["openai/gpt-5.4-nano"].extract_model == "gpt-5.4-nano"
    assert "gemini/gemini-3.1-flash-lite" in by_label  # the gemini default
    # Evaluation candidate: same provider, different EXTRACTION model only.
    o = by_label["openai/gpt-5.4-mini"]
    assert o.provider == "openai" and o.extract_model == "gpt-5.4-mini"
    assert o.summary_model == by_label["openai/gpt-5.4-nano"].summary_model
    # gemini-3.5-flash was dropped from the default set due to long
    # processing times; if you need it, pass --extra-variant explicitly.
    assert "gemini/gemini-3.5-flash" not in by_label


def test_parse_extra_variant_two_fields_defaults_summary():
    v = mod._parse_extra_variant("gemini:gemini-3.1-pro-preview")
    assert v.provider == "gemini" and v.extract_model == "gemini-3.1-pro-preview"
    assert v.summary_model == mod.llm._DEFAULT_SUMMARY_MODELS["gemini"]
    assert v.label == "gemini/gemini-3.1-pro-preview"


def test_parse_extra_variant_three_fields_explicit_summary():
    v = mod._parse_extra_variant("openai:gpt-5.4-mini:gpt-5.4")
    assert v.summary_model == "gpt-5.4" and v.extract_model == "gpt-5.4-mini"


def test_parse_extra_variant_rejects_bad_shape_and_provider():
    with pytest.raises(SystemExit):
        mod._parse_extra_variant("onlyonefield")
    with pytest.raises(SystemExit):
        mod._parse_extra_variant("notaprovider:m")
    with pytest.raises(SystemExit):
        mod._parse_extra_variant("gemini:")  # empty extract field


# --------------------------------------------------------------------------- #
# _variant_dispatch — per-column extraction-model injection
# --------------------------------------------------------------------------- #


def _recording_base():
    """A base dispatch that records the resolved ``model`` it was called with."""
    seen = {}

    def base(provider, system, user, max_tokens, *, model=None, **kw):
        seen["model"] = model
        seen["purpose"] = kw.get("purpose")
        return "ok"

    return base, seen


def test_variant_dispatch_injects_extract_model_for_extraction():
    base, seen = _recording_base()
    dispatch = mod._make_variant_dispatch(base)
    mod._TL.extract_model = "gemini-3.5-flash"
    dispatch("gemini", "s", "u", 100, purpose="extract")
    assert seen["model"] == "gemini-3.5-flash"
    # verify/dedupe are extraction-tier too — they get the injected model.
    dispatch("gemini", "s", "u", 100, purpose="verify_hearing")
    assert seen["model"] == "gemini-3.5-flash"


def test_variant_dispatch_leaves_summary_track_alone():
    base, seen = _recording_base()
    dispatch = mod._make_variant_dispatch(base)
    mod._TL.extract_model = "gemini-3.5-flash"
    # Summary calls pass their own model; the extraction override must not apply.
    dispatch("gemini", "s", "u", 100, model="gemini-2.5-pro", purpose="summary")
    assert seen["model"] == "gemini-2.5-pro"
    # Even with no explicit model, a summary call is not given the extract model.
    dispatch("gemini", "s", "u", 100, purpose="summary")
    assert seen["model"] is None


def test_variant_dispatch_respects_explicit_model():
    base, seen = _recording_base()
    dispatch = mod._make_variant_dispatch(base)
    mod._TL.extract_model = "gemini-3.5-flash"
    dispatch("gemini", "s", "u", 100, model="pinned", purpose="extract")
    assert seen["model"] == "pinned"


# --------------------------------------------------------------------------- #
# _capturing_record + build_report bucket by column label, not provider
# --------------------------------------------------------------------------- #


def test_capturing_record_buckets_by_label(monkeypatch):
    monkeypatch.setattr(mod.CAP, "calls", [])
    tok = mod.usage.TokenUsage(input=10, output=5)
    mod._TL.label = "gemini/gemini-3.5-flash"
    mod._capturing_record(
        purpose="extract", provider="gemini", model="gemini-3.5-flash", tokens=tok
    )
    assert mod.CAP.calls[-1].label == "gemini/gemini-3.5-flash"
    assert mod.CAP.calls[-1].provider == "gemini"


def test_build_report_separates_same_provider_columns(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "OUT_DIR", tmp_path)
    monkeypatch.setattr(mod.CAP, "calls", [])
    tok = mod.usage.TokenUsage(input=1000, output=100)

    def _call(label, provider, model):
        return mod._Call(
            label=label,
            provider=provider,
            model=model,
            purpose="extract",
            docket="d",
            tokens=tok,
            cost=0.01,
        )

    mod.CAP.calls = [
        _call("gemini/gemini-3.1-flash-lite", "gemini", "gemini-3.1-flash-lite"),
        _call("gemini/gemini-3.5-flash", "gemini", "gemini-3.5-flash"),
    ]
    variants = [
        mod.Variant("gemini", "gemini-3.1-flash-lite", "gemini-2.5-pro"),
        mod.Variant("gemini", "gemini-3.5-flash", "gemini-2.5-pro"),
    ]

    class _CL:
        _request_total = 0
        _request_times: list = []

    report = mod.build_report(
        variants, {}, "/nonexistent.sqlite", _CL(), validate=False
    )
    # Both same-provider columns appear, each with its own extraction model line.
    assert "extraction=gemini-3.1-flash-lite" in report
    assert "extraction=gemini-3.5-flash" in report
    # The cost table has a separate row per column label (not merged on provider).
    assert "| gemini/gemini-3.1-flash-lite | extraction |" in report
    assert "| gemini/gemini-3.5-flash | extraction |" in report


# --------------------------------------------------------------------------- #
# _PerProviderLogHandler routes by label when set
# --------------------------------------------------------------------------- #


def test_handler_routes_by_label_over_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "OUT_DIR", tmp_path)
    h = mod._PerProviderLogHandler()
    h.setFormatter(logging.Formatter("%(message)s"))
    # The composite label (provider/extract-model) routes to a nested folder —
    # logs land under <provider>/<model>/, not the bare provider folder.
    mod._TL.provider = "gemini"
    mod._TL.label = "gemini/gemini-3.5-flash"
    h.emit(_record("candidate line"))
    h.close()
    assert (
        tmp_path / "gemini" / "gemini-3.5-flash" / "build.log"
    ).read_text().strip() == "candidate line"
    # The provider dir exists only as the parent; no build.log sits directly in it.
    assert not (tmp_path / "gemini" / "build.log").exists()


# --------------------------------------------------------------------------- #
# timing: _make_variant_dispatch latency capture + _timing_rows rendering
# --------------------------------------------------------------------------- #


def test_variant_dispatch_records_latency_and_injects_model():
    seen = {}

    def base(
        provider,
        system,
        user,
        max_tokens,
        *,
        model=None,
        json_mode=True,
        purpose="llm",
        docket=None,
        temperature=None,
    ):
        seen["model"] = model
        return "RESULT"

    wrapped = mod._make_variant_dispatch(base)
    mod._TL.label = "gemini/gemini-3.5-flash"
    mod._TL.extract_model = "gemini-3.5-flash"
    out = wrapped("gemini", "sys", "usr", 100, purpose="extract")
    assert out == "RESULT"  # passes the real result through
    assert seen["model"] == "gemini-3.5-flash"  # extraction model injected from _TL
    acc = mod.TIMING.call_secs["gemini/gemini-3.5-flash"]
    assert acc[0] == 1.0 and acc[1] >= 0.0  # one timed call recorded


def test_variant_dispatch_leaves_summary_model_untouched():
    seen = {}

    def base(
        provider,
        system,
        user,
        max_tokens,
        *,
        model=None,
        json_mode=True,
        purpose="llm",
        docket=None,
        temperature=None,
    ):
        seen["model"] = model
        return "S"

    wrapped = mod._make_variant_dispatch(base)
    mod._TL.label = "openai/gpt-5.4-mini"
    mod._TL.extract_model = "gpt-5.4-mini"
    wrapped("openai", "s", "u", 10, model="gpt-5.4", purpose="summary")
    assert seen["model"] == "gpt-5.4"  # summary model passed through, not overwritten
    assert mod.TIMING.call_secs["openai/gpt-5.4-mini"][0] == 1.0  # still timed


def test_timing_rows_formats_and_marks_missing():
    rows = mod._timing_rows(
        ["a/m1", "b/m2"],
        wall={"a/m1": 120.0},  # 2.0 m
        call_secs={"a/m1": [10.0, 30.0]},  # 3.0 s/call
    )
    body = "\n".join(rows)
    assert "| a/m1 | 2.0 m | 10 | 3.0 |" in body
    assert "| b/m2 | — | — | — |" in body  # no timing recorded for this column
