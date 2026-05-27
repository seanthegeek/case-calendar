"""Tests for the per-provider log + DECISION-trace capture added to the
``scripts/build_provider_stores.py`` comparison tool.

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
    Path(__file__).resolve().parent.parent / "scripts" / "build_provider_stores.py"
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
    """Each test sets ``_TL.provider`` itself; clear it afterwards so a leaked
    value can't route a later test's records into a stray file."""
    yield
    mod._TL.provider = None


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
