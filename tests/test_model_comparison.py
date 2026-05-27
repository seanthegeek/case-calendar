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
worksheet = _load("mc_worksheet", "ground_truth_worksheet.py")

_TRUTH_COLS = [
    "case_id",
    "case_name",
    "docket_number",
    "court",
    "courtlistener_id",
    "courtlistener_url",
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


def _truth_row(cl_id, docket, court, **counts):
    """One worksheet row, keyed (by the scorer) on its courtlistener_id."""
    row = {c: "" for c in _TRUTH_COLS}
    row.update(
        courtlistener_id=cl_id,
        docket_number=docket,
        court=court,
        case_name=f"case {docket}",
        **counts,
    )
    return row


# --------------------------------------------------------------------------- #
# score.load_truth
# --------------------------------------------------------------------------- #


def test_load_truth_keys_by_courtlistener_id(tmp_path):
    p = tmp_path / "t.csv"
    _write_csv(
        p,
        _TRUTH_COLS,
        [
            _truth_row(
                "11",
                "D1",
                "c",
                hearings_scheduled=1,
                hearings_held=2,
                hearings_cancelled=0,
                deadlines_pending=3,
                deadlines_met_or_passed=0,
                deadlines_cancelled=0,
            ),
            _truth_row("22", "D2", "c"),  # all counts blank -> unfilled
        ],
    )
    truth, labels, unfilled = score.load_truth(p)
    assert set(truth) == {"11"}  # keyed by docket_id, not (docket_number, court)
    assert truth["11"]["hearings_held"] == 2
    assert labels["11"] == "case D1 — D1 (c) #11"
    assert len(unfilled) == 1 and "#22" in unfilled[0]


def test_load_truth_skips_rows_without_a_courtlistener_id(tmp_path):
    p = tmp_path / "t.csv"
    _write_csv(
        p,
        _TRUTH_COLS,
        [
            _truth_row(
                "",  # no id -> cannot be attributed to a record, skipped
                "D1",
                "c",
                hearings_scheduled=1,
                hearings_held=0,
                hearings_cancelled=0,
                deadlines_pending=0,
                deadlines_met_or_passed=0,
                deadlines_cancelled=0,
            ),
        ],
    )
    truth, labels, unfilled = score.load_truth(p)
    assert truth == {} and labels == {} and unfilled == []


def test_load_truth_rejects_non_integer(tmp_path):
    p = tmp_path / "t.csv"
    _write_csv(
        p,
        _TRUTH_COLS,
        [
            _truth_row(
                "11",
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


def test_load_model_events_buckets_per_record_and_status(tmp_path):
    p = tmp_path / "events.csv"
    _write_csv(
        p,
        _EVENT_COLS,
        [
            # docket_id 1 and 2 are SEPARATE CourtListener records of the same
            # PACER docket -> separate keys, NOT summed together.
            _event("anthropic", "hearing", "D1", "c", "held", docket_id=1),
            _event("anthropic", "hearing", "D1", "c", "scheduled", docket_id=1),
            _event("anthropic", "hearing", "D1", "c", "held", docket_id=2),
            # met + passed both fold into met_or_passed within one record.
            _event("anthropic", "deadline", "D1", "c", "met", docket_id=1),
            _event("anthropic", "deadline", "D1", "c", "passed", docket_id=1),
            _event("anthropic", "deadline", "D1", "c", "cancelled", docket_id=2),
            _event("gemini", "hearing", "D1", "c", "held", docket_id=1),
        ],
    )
    ev = score.load_model_events(p)
    a1 = ev["anthropic"]["1"]  # docket_id read back from CSV as a string
    a2 = ev["anthropic"]["2"]
    assert a1["hearings_held"] == 1  # record 1 only
    assert a1["hearings_scheduled"] == 1
    assert a1["deadlines_met_or_passed"] == 2  # met + passed in record 1
    assert a2["hearings_held"] == 1  # record 2 scored separately
    assert a2["deadlines_cancelled"] == 1
    assert ev["gemini"]["1"]["hearings_held"] == 1  # separate provider


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
    key = "1"  # a CourtListener docket_id
    z = {c: 0 for c in score.CATEGORIES}
    truth = {key: {**z, "hearings_held": 1}}
    labels = {key: "Case One — D1 (c) #1"}
    events = {
        "anthropic": {key: {**z, "hearings_held": 1}},  # exact -> 0
        "gemini": {key: {**z, "hearings_held": 4}},  # off by 3
    }
    report = score.build_report(truth, labels, events, unfilled=[])
    assert "Scored **1** of 1 CourtListener records" in report
    assert "| anthropic | **0** |" in report
    assert "| gemini | **3** |" in report
    assert "Case One — D1 (c) #1" in report
    assert "deviation 0" in report and "deviation 3" in report


def test_build_report_lists_unfilled():
    report = score.build_report(
        {}, {}, {"anthropic": {}}, unfilled=["Case Z — D9 (c) #9"]
    )
    assert "Not yet scored" in report and "Case Z — D9 (c) #9" in report


# --------------------------------------------------------------------------- #
# ground_truth_worksheet.build_rows
# --------------------------------------------------------------------------- #


class _FakeCase:
    def __init__(self, case_id, name, dockets):
        self.case_id = case_id
        self.name = name
        self.dockets = dockets


def _patch_worksheet(monkeypatch, cases, meta):
    """Stub the worksheet's config + store so build_rows runs hermetically."""

    class _FakeStore:
        def __init__(self, *a, **k):
            pass

        def get_docket_meta(self, did):
            return meta.get(did)

        def close(self):
            pass

    monkeypatch.setattr(worksheet, "_load_config", lambda _p: {})
    monkeypatch.setattr(worksheet, "_cases_from_config", lambda _cfg: cases)
    monkeypatch.setattr(worksheet, "Store", _FakeStore)


def test_worksheet_one_row_per_courtlistener_record(monkeypatch):
    # A PACER docket CourtListener split across two records (101, 102) must
    # produce TWO adjacent rows; a third, unrelated docket (200) its own row.
    cases = [_FakeCase("us-v-x", "United States v. X", [101, 102, 200])]
    meta = {
        101: {
            "docket_number": "1:25-cr-1",
            "court_id": "nysd",
            "absolute_url": "/docket/101/x/",
        },
        102: {
            "docket_number": "1:25-cr-1",
            "court_id": "nysd",
            "absolute_url": "/docket/102/x/",
        },
        200: {
            "docket_number": "2:25-cr-9",
            "court_id": "cand",
            "absolute_url": "/docket/200/y/",
        },
    }
    _patch_worksheet(monkeypatch, cases, meta)

    rows = worksheet.build_rows("ignored.yaml")

    assert len(rows) == 3  # one row per CourtListener record, not one per docket
    # Records of the split docket sit on adjacent rows, sorted by (number, court).
    assert [r["courtlistener_id"] for r in rows] == [101, 102, 200]
    split = [r for r in rows if r["docket_number"] == "1:25-cr-1"]
    assert len(split) == 2 and all(r["court"] == "nysd" for r in split)
    # Each row carries exactly one id + its own full URL (no " | " combining).
    assert split[0]["courtlistener_url"] == (
        "https://www.courtlistener.com/docket/101/x/"
    )
    assert "|" not in split[0]["courtlistener_url"]
    # Count columns are left blank for the human to fill.
    assert all(rows[0][c] == "" for c in worksheet._COUNT_COLUMNS)


def test_worksheet_unsynced_docket_falls_back_to_placeholder(monkeypatch):
    # A docket_id with no stored metadata still gets its own row.
    cases = [_FakeCase("us-v-y", "United States v. Y", [999])]
    _patch_worksheet(monkeypatch, cases, {})  # get_docket_meta -> None

    rows = worksheet.build_rows("ignored.yaml")

    assert len(rows) == 1
    assert rows[0]["courtlistener_id"] == 999
    assert rows[0]["docket_number"] == "(unsynced docket_id 999)"
    assert rows[0]["court"] == "?"
    assert rows[0]["courtlistener_url"] == ""


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
