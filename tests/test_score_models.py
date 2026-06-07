"""Tests for ``model-comparison/score_models.py`` — the deterministic per-entry
scorer (human ground_truth.csv × model_actions.csv). Loaded by path; pure stdlib."""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "model-comparison" / "score_models.py"
)
_spec = importlib.util.spec_from_file_location("score_models", _SCRIPT)
assert _spec and _spec.loader
score = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = score
_spec.loader.exec_module(score)

_TRUTH_COLS = [
    "case_id",
    "docket_number",
    "court",
    "entry_id",
    "entry_number",
    "reviewed",
    "bad_ocr",
    *score.CATS,
]
_MODEL_COLS = ["provider", "entry_id", *score.CATS]


def _write(path: Path, cols: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def _truth_row(eid, *, reviewed=1, bad_ocr=0, docket="d1", **counts):
    return {
        "case_id": "x",
        "docket_number": docket,
        "court": "c1",
        "entry_id": eid,
        "entry_number": eid,
        "reviewed": reviewed,
        "bad_ocr": bad_ocr,
        **counts,
    }


def _model_row(provider, eid, **counts):
    return {"provider": provider, "entry_id": eid, **counts}


def test_int_coercion():
    assert score._int("5") == 5
    assert score._int("") == 0
    assert score._int(None) == 0
    assert score._int("not-a-number") == 0


def test_load_truth_skips_bad_ocr_and_unreviewed(tmp_path):
    p = tmp_path / "truth.csv"
    _write(
        p,
        _TRUTH_COLS,
        [
            _truth_row("e1", h_scheduled=1),
            _truth_row("e2", bad_ocr=1, h_scheduled=9),  # excluded
            _truth_row("e3", reviewed=0, h_scheduled=9),  # excluded by default
        ],
    )
    truth = score.load_truth(p, include_unreviewed=False)
    assert set(truth) == {"e1"}
    assert truth["e1"]["counts"]["h_scheduled"] == 1
    # --include-unreviewed brings e3 back, but bad_ocr stays excluded
    truth2 = score.load_truth(p, include_unreviewed=True)
    assert set(truth2) == {"e1", "e3"}


def test_load_model_buckets_by_provider(tmp_path):
    p = tmp_path / "model.csv"
    _write(
        p,
        _MODEL_COLS,
        [
            _model_row("gemini/x", "e1", h_scheduled=1),
            _model_row("anthropic/y", "e1", h_scheduled=2),
        ],
    )
    m = score.load_model(p)
    assert set(m) == {"gemini/x", "anthropic/y"}
    assert m["gemini/x"]["e1"]["h_scheduled"] == 1
    assert m["anthropic/y"]["e1"]["h_scheduled"] == 2


def test_report_deviation_over_under_and_regex_miss(tmp_path):
    truth_p = tmp_path / "truth.csv"
    model_p = tmp_path / "model.csv"
    _write(
        truth_p,
        _TRUTH_COLS,
        [
            _truth_row("e1", h_scheduled=1),  # provs have this
            _truth_row("e2", d_set=1),  # NO provider has it -> regex miss
        ],
    )
    _write(
        model_p,
        _MODEL_COLS,
        [
            _model_row("A", "e1", h_scheduled=1),  # exact -> dev 0 on e1
            _model_row("B", "e1", h_scheduled=2),  # over 1 on e1
            _model_row("C", "e1", h_scheduled=0),  # under 1 on e1
        ],
    )
    truth = score.load_truth(truth_p, include_unreviewed=False)
    model = score.load_model(model_p)
    report = score.build_report(truth, model)

    assert "Scored **2** entries" in report
    assert "**2** human-counted actions" in report
    assert "**1** logical dockets" in report
    # A: e1 match + e2 missed = dev 1 (the leader); B/C = 2
    assert "| A | **1** |" in report
    assert "| B | **2** |" in report
    assert "| C | **2** |" in report
    # regex-stage miss: e2's d_set, missed by every provider (50% of 2 actions)
    assert "**1** scored entries carried **1**" in report
    assert "50.0%" in report
    assert "Ds 1" in report
    assert "entry #e2 (id e2): Ds 1" in report


def test_main_writes_report(tmp_path):
    truth_p = tmp_path / "truth.csv"
    model_p = tmp_path / "model.csv"
    out_p = tmp_path / "score.md"
    _write(truth_p, _TRUTH_COLS, [_truth_row("e1", h_scheduled=1)])
    _write(model_p, _MODEL_COLS, [_model_row("A", "e1", h_scheduled=1)])
    rc = score.main(
        ["--truth", str(truth_p), "--model", str(model_p), "--out", str(out_p)]
    )
    assert rc == 0
    assert "Per-entry extraction accuracy" in out_p.read_text()
