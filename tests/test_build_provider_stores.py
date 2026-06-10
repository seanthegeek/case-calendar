"""Tests for the per-provider log + DECISION-trace capture in the
``model-comparison/build_provider_stores.py`` comparison tool.

The script lives outside the ``case_calendar`` package, so it's loaded by path.
We exercise the new, independently-testable units: the thread-local-routed log
handler, the decision-line formatters, and the LLM-wrapping factory."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sqlite3
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
            "type": "add_hearing",
            "hearing_key": "sentencing-wang",
            "significance": "major",
            "local_date": "2026-06-03",
        }
    )
    assert b.startswith("ADD_HEARING(")
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
        [
            {
                "type": "add_hearing",
                "hearing_key": "jury-trial-wang",
                "significance": "major",
            }
        ],
    )
    assert "extract docket=99 entry=7" in out
    assert "Order setting Jury Trial" in out
    assert "jury-trial-wang" in out and "ADD_HEARING" in out


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
    sentinel = [{"type": "add_hearing", "hearing_key": "k1", "significance": "major"}]
    monkeypatch.setattr(mod.llm, "extract_actions", lambda **k: sentinel)
    wrapped = mod._wrap_llm("extract_actions", "extract")
    mod._TL.provider = "openai"
    with caplog.at_level(logging.INFO, logger=mod._DLOG.name):
        result = wrapped(entry={"id": 5, "short_description": "Order"}, docket_id=9)
    assert result is sentinel  # real result passed through unchanged
    assert any("k1" in r.message and "ADD_HEARING" in r.message for r in caplog.records)


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


def test_parse_extra_variant_explicit_summary_via_comma():
    v = mod._parse_extra_variant("openai:gpt-5.4-mini,gpt-5.4")
    assert v.summary_model == "gpt-5.4" and v.extract_model == "gpt-5.4-mini"


def test_parse_extra_variant_rejects_bad_shape_and_provider():
    with pytest.raises(SystemExit):
        mod._parse_extra_variant("onlyonefield")
    with pytest.raises(SystemExit):
        mod._parse_extra_variant("notaprovider:m")
    with pytest.raises(SystemExit):
        mod._parse_extra_variant("gemini:")  # empty extract field


def test_parse_extra_variant_accepts_ollama_opt_in():
    # Ollama is NOT in the default-built set (it needs a local server), but is
    # selectable as an explicit extra column for local-vs-hosted benchmarking.
    v = mod._parse_extra_variant("ollama:llama3.1")
    assert v.provider == "ollama" and v.extract_model == "llama3.1"
    assert v.summary_model == mod.llm._DEFAULT_SUMMARY_MODELS["ollama"]
    assert v.label == "ollama/llama3.1"
    # Distinct summary model via the comma separator.
    v2 = mod._parse_extra_variant("ollama:llama3.1,qwen2.5")
    assert v2.summary_model == "qwen2.5"


def test_parse_extra_variant_colon_bearing_model_tag():
    # Regression: an Ollama model TAG contains a colon (``gemma4:e4b``). The
    # provider is split on the FIRST colon, so the whole tag becomes the
    # extraction model and the summary defaults — it must NOT mis-parse as
    # extract=``gemma4`` / summary=``e4b``.
    v = mod._parse_extra_variant("ollama:gemma4:e4b")
    assert v.provider == "ollama"
    assert v.extract_model == "gemma4:e4b"
    assert v.summary_model == mod.llm._DEFAULT_SUMMARY_MODELS["ollama"]
    assert v.label == "ollama/gemma4:e4b"
    # A distinct, also-colon-bearing summary tag, separated by a comma.
    v2 = mod._parse_extra_variant("ollama:gemma4:e4b,gemma4:31b")
    assert v2.extract_model == "gemma4:e4b"
    assert v2.summary_model == "gemma4:31b"


def test_has_key_ollama_needs_no_api_key(monkeypatch):
    # Ollama runs locally, so a column on it must be buildable with NO provider
    # keys set — otherwise the build refuses an ollama column unless an unrelated
    # Gemini/Google key happens to be present.
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    assert mod._has_key("ollama") is True
    assert mod._has_key("anthropic") is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert mod._has_key("anthropic") is True


def test_copy_store_makes_readonly_source_writable(tmp_path):
    # A frozen snapshot is read-only (0444); the working copy must come out
    # writable so _clear_derived can DELETE from it. Regression: shutil.copy2
    # preserved the read-only mode -> "attempt to write a readonly database".
    src = tmp_path / "snap.sqlite"
    sqlite3.connect(src).close()
    os.chmod(src, 0o444)
    dst = tmp_path / "work" / "case-calendar.sqlite"
    mod._copy_store(str(src), str(dst))
    assert dst.exists()
    assert os.access(dst, os.W_OK)
    # And it's writable at the SQLite level (the actual failure mode).
    con = sqlite3.connect(dst)
    con.execute("CREATE TABLE t(x)")
    con.commit()
    con.close()


def test_ollama_not_in_default_built_set():
    # The committed default comparison set stays the three hosted providers —
    # a clean `--validate` / `--fake` run can't assume a local server.
    assert "ollama" not in mod.ALL_PROVIDERS
    assert "ollama" in mod.SELECTABLE_PROVIDERS
    assert {v.provider for v in mod._default_variants()} == set(mod.ALL_PROVIDERS)


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


def test_variant_dispatch_forwards_schema():
    # Regression: extract_actions passes schema= (structured output); the variant
    # wrapper must thread it through, else every extract TypeErrors -> IGNORE.
    seen = {}

    def base(provider, system, user, max_tokens, *, model=None, schema=None, **kw):
        seen["schema"] = schema
        return "ok"

    dispatch = mod._make_variant_dispatch(base)
    schema = {"type": "object"}
    dispatch("gemini", "s", "u", 100, purpose="extract", schema=schema)
    assert seen["schema"] == schema
    dispatch("gemini", "s", "u", 100, purpose="extract")  # default None still works
    assert seen["schema"] is None


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
        schema=None,
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
        schema=None,
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


# ---------------------------------------------------------------------------
# Persistent LLM-response cache
# ---------------------------------------------------------------------------


def _make_counting_base():
    """Return (base, calls) where base records each (provider, model, system,
    user) it's called with and returns a deterministic response string."""
    calls: list[tuple] = []

    def base(
        provider,
        system,
        user,
        max_tokens,
        *,
        model=None,
        json_mode=True,
        schema=None,
        purpose="llm",
        docket=None,
        temperature=None,
    ):
        calls.append(
            (provider, model, system, user, max_tokens, json_mode, temperature, schema)
        )
        return f"resp::{provider}::{model}::{system}::{user}"

    return base, calls


def test_llm_cache_miss_then_hit(tmp_path):
    cache = mod._LLMCache(str(tmp_path / "c.sqlite"))
    base, calls = _make_counting_base()
    wrapped = cache.wrap(base)

    r1 = wrapped("anthropic", "sys", "usr", 512, model="haiku", temperature=0.0)
    r2 = wrapped("anthropic", "sys", "usr", 512, model="haiku", temperature=0.0)

    assert r1 == r2
    assert len(calls) == 1  # second call served from cache, base hit once
    assert sum(cache.hits.values()) == 1
    assert sum(cache.misses.values()) == 1
    cache.close()


def test_llm_cache_persists_across_instances(tmp_path):
    path = str(tmp_path / "c.sqlite")
    base, calls = _make_counting_base()

    c1 = mod._LLMCache(path)
    first = c1.wrap(base)("gemini", "sys", "usr", 256, model="flash", temperature=0.0)
    c1.close()

    # A SECOND run (new process → new _LLMCache on the same file) replays the
    # identical request for free: this is the dev-iteration win.
    c2 = mod._LLMCache(path)
    second = c2.wrap(base)("gemini", "sys", "usr", 256, model="flash", temperature=0.0)
    c2.close()

    assert first == second
    assert len(calls) == 1  # base never called on the second instance
    assert sum(c2.hits.values()) == 1
    assert sum(c2.misses.values()) == 0


def test_llm_cache_busts_on_model_change(tmp_path):
    cache = mod._LLMCache(str(tmp_path / "c.sqlite"))
    base, calls = _make_counting_base()
    wrapped = cache.wrap(base)

    wrapped("anthropic", "sys", "usr", 512, model="haiku", temperature=0.0)
    wrapped("anthropic", "sys", "usr", 512, model="sonnet", temperature=0.0)

    assert len(calls) == 2  # different model → different key → live call
    cache.close()


def test_llm_cache_busts_on_prompt_change(tmp_path):
    cache = mod._LLMCache(str(tmp_path / "c.sqlite"))
    base, calls = _make_counting_base()
    wrapped = cache.wrap(base)

    wrapped("anthropic", "sys-v1", "usr", 512, model="haiku", temperature=0.0)
    wrapped(
        "anthropic", "sys-v2", "usr", 512, model="haiku", temperature=0.0
    )  # system edit
    wrapped(
        "anthropic", "sys-v2", "usr-b", 512, model="haiku", temperature=0.0
    )  # user edit

    assert len(calls) == 3  # each distinct prompt re-runs live
    cache.close()


def test_llm_cache_does_not_cache_errors(tmp_path):
    cache = mod._LLMCache(str(tmp_path / "c.sqlite"))
    boom_count = {"n": 0}

    def base(
        provider,
        system,
        user,
        max_tokens,
        *,
        model=None,
        json_mode=True,
        schema=None,
        purpose="llm",
        docket=None,
        temperature=None,
    ):
        boom_count["n"] += 1
        if boom_count["n"] == 1:
            raise RuntimeError("transient")
        return "recovered"

    wrapped = cache.wrap(base)
    with pytest.raises(RuntimeError):
        wrapped("anthropic", "sys", "usr", 512, model="haiku", temperature=0.0)
    # The failed request was NOT stored, so the retry hits base again (and
    # succeeds), rather than replaying a cached error.
    assert (
        wrapped("anthropic", "sys", "usr", 512, model="haiku", temperature=0.0)
        == "recovered"
    )
    assert boom_count["n"] == 2
    assert sum(cache.misses.values()) == 1  # only the successful store counts
    cache.close()


def test_llm_cache_buckets_counts_by_label(tmp_path):
    cache = mod._LLMCache(str(tmp_path / "c.sqlite"))
    base, _ = _make_counting_base()
    wrapped = cache.wrap(base)

    mod._TL.label = "anthropic/claude-haiku-4-5"
    wrapped("anthropic", "sys", "usr", 512, model="haiku", temperature=0.0)  # miss
    wrapped("anthropic", "sys", "usr", 512, model="haiku", temperature=0.0)  # hit
    mod._TL.label = "gemini/flash"
    wrapped("gemini", "sys", "usr", 512, model="flash", temperature=0.0)  # miss

    assert cache.misses["anthropic/claude-haiku-4-5"] == 1
    assert cache.hits["anthropic/claude-haiku-4-5"] == 1
    assert cache.misses["gemini/flash"] == 1
    cache.close()


def test_llm_cache_forwards_all_kwargs(tmp_path):
    cache = mod._LLMCache(str(tmp_path / "c.sqlite"))
    seen: dict = {}

    def base(
        provider,
        system,
        user,
        max_tokens,
        *,
        model=None,
        json_mode=True,
        schema=None,
        purpose="llm",
        docket=None,
        temperature=None,
    ):
        seen.update(
            model=model,
            json_mode=json_mode,
            schema=schema,
            purpose=purpose,
            docket=docket,
            temperature=temperature,
        )
        return "ok"

    cache.wrap(base)(
        "openai",
        "sys",
        "usr",
        99,
        model="gpt",
        json_mode=False,
        schema={"type": "object"},
        purpose="summary",
        docket=123,
        temperature=0.0,
    )
    assert seen == {
        "model": "gpt",
        "json_mode": False,
        "schema": {"type": "object"},
        "purpose": "summary",
        "docket": 123,
        "temperature": 0.0,
    }
    cache.close()


def test_llm_cache_log_summary_emits_totals(tmp_path, caplog):
    cache = mod._LLMCache(str(tmp_path / "c.sqlite"))
    base, _ = _make_counting_base()
    wrapped = cache.wrap(base)
    mod._TL.label = "anthropic/claude-haiku-4-5"
    wrapped("anthropic", "sys", "usr", 512, model="haiku", temperature=0.0)  # miss
    wrapped("anthropic", "sys", "usr", 512, model="haiku", temperature=0.0)  # hit

    with caplog.at_level(logging.INFO, logger="provider_stores"):
        cache.log_summary()
    text = caplog.text
    assert "llm-cache [anthropic/claude-haiku-4-5] hits=1 misses=1" in text
    assert "llm-cache TOTAL hits=1 misses=1" in text
    cache.close()


def test_llm_cache_keys_on_resolved_model_not_none_arg(tmp_path, monkeypatch):
    """model=None resolves via LLM_MODEL when the request is built, so two
    None-arg calls under DIFFERENT LLM_MODEL values are DIFFERENT requests and
    must not collide on one cache entry (the soundness bug: keying on the
    unresolved dispatch arg would have let them share a response)."""
    cache = mod._LLMCache(str(tmp_path / "c.sqlite"))
    base, calls = _make_counting_base()
    wrapped = cache.wrap(base)

    monkeypatch.setenv("LLM_MODEL", "model-A")
    wrapped("anthropic", "sys", "usr", 512, model=None, temperature=0.0)
    monkeypatch.setenv("LLM_MODEL", "model-B")
    wrapped("anthropic", "sys", "usr", 512, model=None, temperature=0.0)

    assert len(calls) == 2  # different resolved model → no collision
    cache.close()


def test_llm_cache_none_arg_equals_explicit_resolved_model(tmp_path, monkeypatch):
    """A model=None call and an explicit call naming the SAME resolved model
    build the identical request, so the second is a HIT — the key reflects what
    is actually sent, not how the caller spelled it."""
    monkeypatch.delenv("LLM_MODEL", raising=False)
    default = mod.providers._DEFAULT_MODELS["anthropic"]
    cache = mod._LLMCache(str(tmp_path / "c.sqlite"))
    base, calls = _make_counting_base()
    wrapped = cache.wrap(base)

    wrapped("anthropic", "sys", "usr", 512, model=None, temperature=0.0)  # → default
    wrapped(
        "anthropic", "sys", "usr", 512, model=default, temperature=0.0
    )  # same request

    assert len(calls) == 1  # second served from cache
    assert sum(cache.hits.values()) == 1
    cache.close()


def test_llm_cache_keys_on_schema(tmp_path, monkeypatch):
    """A structured-output call (schema set) and the same call without a schema
    build DIFFERENT requests and must not collide — otherwise a
    LLM_STRUCTURED_OUTPUT OFF-vs-ON comparison would replay the wrong response.
    The schema is also threaded through to the base dispatch on a miss."""
    monkeypatch.delenv("LLM_MODEL", raising=False)
    cache = mod._LLMCache(str(tmp_path / "c.sqlite"))
    base, calls = _make_counting_base()
    wrapped = cache.wrap(base)

    schema = {"type": "object", "required": ["actions"]}
    wrapped("anthropic", "sys", "usr", 512, model="m", temperature=0.0)  # no schema
    wrapped("anthropic", "sys", "usr", 512, model="m", temperature=0.0, schema=schema)
    wrapped("anthropic", "sys", "usr", 512, model="m", temperature=0.0)  # repeat: HIT

    assert len(calls) == 2  # the third (no-schema) repeat is served from cache
    assert calls[0][-1] is None  # base received schema=None on the first call
    assert calls[1][-1] == schema  # and the real schema on the second
    # backward compat: a schema=None call keys identically to the historical
    # (schema-less) form, so adding the param does not orphan the warm cache
    assert mod._LLMCache._key("p", "m", "s", "u", 1, True, 0.0) == mod._LLMCache._key(
        "p", "m", "s", "u", 1, True, 0.0, None
    )
    assert mod._LLMCache._key("p", "m", "s", "u", 1, True, 0.0) != mod._LLMCache._key(
        "p", "m", "s", "u", 1, True, 0.0, {"a": 1}
    )
    cache.close()


# --------------------------------------------------------------------------- #
# Per-track provider env overrides are neutralized for the run
# --------------------------------------------------------------------------- #


def test_neutralized_run_env_includes_provider_overrides():
    # The run pins each column's (provider, model) itself, so it must clear
    # the operator's per-track provider overrides too — not just the model
    # overrides. Regression guard: the recommended split
    # (LLM_EXTRACTION_PROVIDER=gemini, LLM_SUMMARY_PROVIDER=anthropic) in .env
    # forced every column onto one provider and 404'd until these were popped.
    for var in (
        "LLM_MODEL",
        "LLM_SUMMARY_MODEL",
        "LLM_PROVIDER",
        "LLM_EXTRACTION_PROVIDER",
        "LLM_SUMMARY_PROVIDER",
    ):
        assert var in mod._NEUTRALIZED_RUN_ENV


def test_extraction_provider_env_short_circuits_threadlocal_until_popped(monkeypatch):
    """The mechanism the fix depends on: the build patches
    ``providers._detect_provider`` to read the per-column thread-local, but
    ``_detect_extraction_provider`` checks ``LLM_EXTRACTION_PROVIDER`` FIRST —
    so an operator env override wins over the column's pinned provider until
    the run pops it (it's in ``_NEUTRALIZED_RUN_ENV``)."""
    from case_calendar.llmkit import providers

    monkeypatch.setattr(providers, "_detect_provider", mod._tl_detect)
    mod._TL.provider = "anthropic"  # this column is the anthropic column

    # With the split-config env var present, extraction routes to gemini —
    # the bug (gemini provider + anthropic model -> 404).
    monkeypatch.setenv("LLM_EXTRACTION_PROVIDER", "gemini")
    assert providers._detect_extraction_provider() == "gemini"

    # Once the run neutralizes it, the column's thread-local provider wins.
    monkeypatch.delenv("LLM_EXTRACTION_PROVIDER", raising=False)
    assert providers._detect_extraction_provider() == "anthropic"


# --------------------------------------------------------------------------- #
# Frozen-snapshot mode (--frozen): the benchmark can't silently reach live data
# --------------------------------------------------------------------------- #


class _FakeCL:
    """Stands in for the CourtListener client: counts any call that reaches it
    (i.e. that the frozen guard failed to block)."""

    def __init__(self):
        self.calls = 0

    def _get(self, *a, **k):
        self.calls += 1
        return {"live": True}

    def _post(self, *a, **k):
        self.calls += 1
        return {"live": True}


def test_frozen_cl_cache_miss_raises_without_network(monkeypatch):
    # In frozen mode a cache MISS must raise rather than hit the network, so a
    # benchmark run can't drift by pulling data the snapshot predates.
    monkeypatch.setattr(mod, "_GET_CACHE", {})
    monkeypatch.setattr(mod, "_FROZEN", True)
    cl = _FakeCL()
    mod._install_cl_cache(cl)
    with pytest.raises(mod.FrozenSnapshotError, match="not in the snapshot"):
        cl._get("dockets/1/")
    assert cl.calls == 0  # never reached the (fake) network


def test_frozen_cl_cache_hit_is_served(monkeypatch):
    # A request already in the shared cache is served even when frozen — only
    # genuinely-absent data trips the guard.
    monkeypatch.setattr(mod, "_GET_CACHE", {})
    monkeypatch.setattr(mod, "_FROZEN", False)
    cl = _FakeCL()
    mod._install_cl_cache(cl)
    assert cl._get("dockets/1/") == {"live": True}  # miss while warm -> caches
    assert cl.calls == 1
    monkeypatch.setattr(mod, "_FROZEN", True)
    assert cl._get("dockets/1/") == {"live": True}  # same request -> cache hit
    assert cl.calls == 1  # no second live call


def test_frozen_pdf_guard_blocks_download(monkeypatch):
    # Register the current fetch_pdf_bytes for restore, then install the guard
    # (which reassigns it). monkeypatch restores the original at teardown.
    monkeypatch.setattr(mod.pdf, "fetch_pdf_bytes", mod.pdf.fetch_pdf_bytes)
    mod._install_frozen_pdf_guard()
    with pytest.raises(mod.FrozenSnapshotError, match="download a PDF"):
        mod.pdf.fetch_pdf_bytes({"id": 42})


def test_log_snapshot_manifest_present(tmp_path, caplog):
    store = tmp_path / "benchmark-store.sqlite"
    store.with_suffix(".manifest.json").write_text(
        json.dumps(
            {
                "snapshot_utc": "2026-06-02T00:00:00+00:00",
                "row_counts": {"entries": 123},
                "ground_truth_sha256": "abcdef1234567890",
            }
        )
    )
    with caplog.at_level(logging.INFO, logger=mod.logger.name):
        mod._log_snapshot_manifest(str(store))
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "frozen snapshot" in msgs and "2026-06-02" in msgs and "entries=123" in msgs


def test_log_snapshot_manifest_absent_warns(tmp_path, caplog):
    store = tmp_path / "no-manifest.sqlite"
    with caplog.at_level(logging.WARNING, logger=mod.logger.name):
        mod._log_snapshot_manifest(str(store))
    assert any("no manifest beside" in r.getMessage() for r in caplog.records)


class TestEntryActionsTap:
    """The --entry-actions-csv tap: maps each extract_actions result to per-entry
    counts keyed by (column label, entry_id). It wraps extract_actions ABOVE the
    LLM cache, so it records on every call regardless of cache hit."""

    def _reset(self):
        mod._ENTRY_ACTIONS.clear()
        for attr in ("provider", "label"):
            if hasattr(mod._TL, attr):
                delattr(mod._TL, attr)

    def test_action_to_col_mapping(self):
        m = mod._ACTION_TO_COL
        assert m["ADD_HEARING"] == "h_scheduled"
        assert m["RESCHEDULE_HEARING"] == "h_rescheduled"
        assert m["MARK_HELD"] == "h_held"
        assert m["CANCEL_HEARING"] == "h_cancelled"
        assert m["ADD_DEADLINE"] == "d_set"
        assert m["RESCHEDULE_DEADLINE"] == "d_rescheduled"
        assert m["MARK_FILED"] == "d_met_filed"
        assert m["CANCEL_DEADLINE"] == "d_cancelled"
        # non-mutating action types carry no count column
        assert "IGNORE" not in m and "UPDATE_DETAILS" not in m

    def test_record_accumulates_by_label_and_entry(self):
        self._reset()
        try:
            mod._TL.provider = "gemini/x"
            mod._record_entry_actions(
                {"entry": {"id": 7}, "docket_id": 5},
                [
                    {"type": "ADD_HEARING"},
                    {"type": "ADD_HEARING"},
                    {"type": "MARK_HELD"},
                    {"type": "IGNORE"},  # not counted
                    {"type": "UPDATE_DETAILS"},  # not counted
                ],
            )
            rec = mod._ENTRY_ACTIONS[("gemini/x", 7)]
            assert rec["h_scheduled"] == 2
            assert rec["h_held"] == 1
            assert rec["d_set"] == 0
            assert rec["docket_id"] == 5
        finally:
            self._reset()

    def test_record_noop_without_label_or_entry_id(self):
        self._reset()
        try:
            # no column label set -> no-op
            mod._record_entry_actions({"entry": {"id": 1}}, [{"type": "ADD_HEARING"}])
            assert mod._ENTRY_ACTIONS == {}
            # label set but the entry carries no id -> no-op
            mod._TL.provider = "p/m"
            mod._record_entry_actions({"entry": {}}, [{"type": "ADD_HEARING"}])
            assert mod._ENTRY_ACTIONS == {}
        finally:
            self._reset()
