"""Tests for the model-comparison tooling.

``model-comparison/score.py`` (the deterministic scorer) and
``export_model_events.py`` (stores -> committed events CSV) live outside the
``case_calendar`` package, so they're loaded by path. Both are pure stdlib."""

from __future__ import annotations

import csv
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

_MC = Path(__file__).resolve().parent.parent / "model-comparison"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _MC / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


score = _load("mc_score", "score.py")
exporter = _load("mc_export", "export_model_events.py")

_TRUTH_COLS = [
    "case_id",
    "case_name",
    "docket_number",
    "court",
    "courtlistener_records",
    "courtlistener_urls",
    "hearings_scheduled",
    "hearings_held",
    "hearings_cancelled",
    "deadlines_pending",
    "deadlines_met_or_passed",
    "deadlines_cancelled",
    "notes",
]


def _write_csv(path: Path, cols: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def _truth_row(docket, court, **counts):
    row = {c: "" for c in _TRUTH_COLS}
    row.update(docket_number=docket, court=court, case_name=f"case {docket}", **counts)
    return row


# --------------------------------------------------------------------------- #
# score.load_truth
# --------------------------------------------------------------------------- #


def test_load_truth_filled_and_unfilled(tmp_path):
    p = tmp_path / "t.csv"
    _write_csv(
        p,
        _TRUTH_COLS,
        [
            _truth_row(
                "D1",
                "c",
                hearings_scheduled=1,
                hearings_held=2,
                hearings_cancelled=0,
                deadlines_pending=3,
                deadlines_met_or_passed=0,
                deadlines_cancelled=0,
            ),
            _truth_row("D2", "c"),  # all counts blank -> unfilled
        ],
    )
    truth, names, unfilled = score.load_truth(p)
    assert set(truth) == {("D1", "c")}
    assert truth[("D1", "c")]["hearings_held"] == 2
    assert names[("D1", "c")] == "case D1"
    assert len(unfilled) == 1 and "D2" in unfilled[0]


def test_load_truth_rejects_non_integer(tmp_path):
    p = tmp_path / "t.csv"
    _write_csv(
        p,
        _TRUTH_COLS,
        [
            _truth_row(
                "D1",
                "c",
                hearings_scheduled="two",
                hearings_held=0,
                hearings_cancelled=0,
                deadlines_pending=0,
                deadlines_met_or_passed=0,
                deadlines_cancelled=0,
            ),
        ],
    )
    with pytest.raises(SystemExit):
        score.load_truth(p)


# --------------------------------------------------------------------------- #
# score.load_model_events
# --------------------------------------------------------------------------- #

_EVENT_COLS = exporter.COLUMNS


def _event(provider, typ, docket, court, status, docket_id=1):
    return {
        "provider": provider,
        "type": typ,
        "case_id": "x",
        "docket_number": docket,
        "court": court,
        "docket_id": docket_id,
        "title": "t",
        "status": status,
        "significance": "major",
        "date": "2026-01-01T00:00:00+00:00",
        "source_entry_ids": "[]",
    }


def test_load_model_events_groups_records_and_buckets_statuses(tmp_path):
    p = tmp_path / "events.csv"
    _write_csv(
        p,
        _EVENT_COLS,
        [
            # Same logical docket (D1, c) across two CourtListener records -> summed.
            _event("anthropic", "hearing", "D1", "c", "held", docket_id=1),
            _event("anthropic", "hearing", "D1", "c", "held", docket_id=2),
            _event("anthropic", "hearing", "D1", "c", "scheduled", docket_id=1),
            # met + passed both fold into met_or_passed.
            _event("anthropic", "deadline", "D1", "c", "met", docket_id=1),
            _event("anthropic", "deadline", "D1", "c", "passed", docket_id=2),
            _event("anthropic", "deadline", "D1", "c", "cancelled", docket_id=1),
            _event("gemini", "hearing", "D1", "c", "held", docket_id=1),
        ],
    )
    ev = score.load_model_events(p)
    a = ev["anthropic"][("D1", "c")]
    assert a["hearings_held"] == 2  # collapsed across records
    assert a["hearings_scheduled"] == 1
    assert a["deadlines_met_or_passed"] == 2  # met + passed
    assert a["deadlines_cancelled"] == 1
    assert ev["gemini"][("D1", "c")]["hearings_held"] == 1  # separate provider


# --------------------------------------------------------------------------- #
# score.deviation + build_report
# --------------------------------------------------------------------------- #


def test_deviation_is_absolute_per_category():
    truth = {c: 0 for c in score.CATEGORIES}
    truth["hearings_held"] = 1
    model = {c: 0 for c in score.CATEGORIES}
    model["hearings_held"] = 3
    model["hearings_scheduled"] = 2
    dev = score.deviation(model, truth)
    assert dev["hearings_held"] == 2 and dev["hearings_scheduled"] == 2
    assert sum(dev.values()) == 4


def test_build_report_totals_and_ordering():
    key = ("D1", "c")
    z = {c: 0 for c in score.CATEGORIES}
    truth = {key: {**z, "hearings_held": 1}}
    names = {key: "Case One"}
    events = {
        "anthropic": {key: {**z, "hearings_held": 1}},  # exact -> 0
        "gemini": {key: {**z, "hearings_held": 4}},  # off by 3
    }
    report = score.build_report(truth, names, events, unfilled=[])
    assert "Scored **1** of 1 logical dockets" in report
    assert "| anthropic | **0** |" in report
    assert "| gemini | **3** |" in report
    assert "Case One — D1 (c)" in report
    assert "deviation 0" in report and "deviation 3" in report


def test_build_report_lists_unfilled():
    report = score.build_report({}, {}, {"anthropic": {}}, unfilled=["Case Z — D9 (c)"])
    assert "Not yet scored" in report and "Case Z — D9 (c)" in report


# --------------------------------------------------------------------------- #
# export_model_events
# --------------------------------------------------------------------------- #


def _make_store(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE dockets (docket_id INTEGER, docket_number TEXT, court_id TEXT);
        CREATE TABLE hearings (case_id TEXT, docket_id INTEGER, hearing_key TEXT,
            title TEXT, starts_at_utc TEXT, status TEXT, significance TEXT,
            source_entry_ids TEXT);
        CREATE TABLE deadlines (case_id TEXT, docket_id INTEGER, deadline_key TEXT,
            title TEXT, due_at_utc TEXT, status TEXT, significance TEXT,
            source_entry_ids TEXT);
        """
    )
    db.execute("INSERT INTO dockets VALUES (1, '1:25-cr-1', 'nysd')")
    db.execute(
        "INSERT INTO hearings VALUES ('x', 1, 'h1', 'Trial', "
        "'2026-06-01T00:00:00+00:00', 'scheduled', 'major', '[1]')"
    )
    db.execute(
        "INSERT INTO deadlines VALUES ('x', 1, 'd1', 'Brief', "
        "'2026-05-01T00:00:00+00:00', 'pending', 'minor', '[2]')"
    )
    db.commit()
    db.close()


def test_export_events_joins_docket_metadata(tmp_path):
    store = tmp_path / "s.sqlite"
    _make_store(store)
    rows = exporter._events(store, "anthropic")
    assert len(rows) == 2
    by_type = {r["type"]: r for r in rows}
    assert by_type["hearing"]["docket_number"] == "1:25-cr-1"
    assert by_type["hearing"]["court"] == "nysd"
    assert by_type["hearing"]["provider"] == "anthropic"
    assert by_type["hearing"]["status"] == "scheduled"
    assert by_type["deadline"]["significance"] == "minor"  # everything stored


def test_export_main_writes_combined_csv(tmp_path):
    stores = tmp_path / "provider-stores"
    for prov in ("anthropic", "openai", "gemini"):
        (stores / prov).mkdir(parents=True)
        _make_store(stores / prov / "case-calendar.sqlite")
    prod = tmp_path / "prod.sqlite"
    _make_store(prod)
    out = tmp_path / "model_events.csv"
    rc = exporter.main(
        ["--stores", str(stores), "--prod", str(prod), "--out", str(out)]
    )
    assert rc == 0
    providers = {r["provider"] for r in csv.DictReader(out.open())}
    assert providers == {"prod", "anthropic", "openai", "gemini"}


def test_export_main_errors_without_provider_stores(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SystemExit):
        exporter.main(
            [
                "--stores",
                str(empty),
                "--prod",
                str(tmp_path / "nope.sqlite"),
                "--out",
                str(tmp_path / "o.csv"),
            ]
        )


def test_export_discovers_nested_variant_columns_by_relative_path(tmp_path):
    # The real layout nests each column at <provider>/<extract-model>/; the
    # exporter labels it by its path relative to --stores, so sibling models on
    # one provider become distinct columns without the exporter knowing names in
    # advance.
    stores = tmp_path / "provider-stores"
    for col in (
        "gemini/gemini-3.1-flash-lite",
        "gemini/gemini-3.5-flash",
        "openai/gpt-5.4-mini",
    ):
        (stores / col).mkdir(parents=True)
        _make_store(stores / col / "case-calendar.sqlite")
    out = tmp_path / "model_events.csv"
    rc = exporter.main(
        [
            "--stores",
            str(stores),
            "--prod",
            str(tmp_path / "nope.sqlite"),
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    labels = {r["provider"] for r in csv.DictReader(out.open())}
    assert labels == {
        "gemini/gemini-3.1-flash-lite",
        "gemini/gemini-3.5-flash",
        "openai/gpt-5.4-mini",
    }
