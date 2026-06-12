"""Tests for ``model-comparison/funnel_analysis.py`` — the deviation-to-calendar
funnel tracer (CSV math, decision-log repeat collapsing, store/key helpers).
Loaded by path; pure stdlib."""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "model-comparison" / "funnel_analysis.py"
)
_spec = importlib.util.spec_from_file_location("funnel_analysis", _SCRIPT)
assert _spec and _spec.loader
funnel = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = funnel
_spec.loader.exec_module(funnel)

_TRUTH_COLS = [
    "case_id",
    "docket_number",
    "court",
    "entry_id",
    "entry_number",
    "reviewed",
    "bad_ocr",
    *funnel.CATS,
]
_MODEL_COLS = ["provider", "entry_id", *funnel.CATS]


def _write(path: Path, cols: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def _truth_row(eid: str, **counts) -> dict:
    return {
        "case_id": "case-a",
        "docket_number": "1:24-cr-1",
        "court": "cand",
        "entry_id": eid,
        "reviewed": "1",
        "bad_ocr": "0",
        **counts,
    }


class TestReadTruth:
    def test_skips_unreviewed_and_bad_ocr(self, tmp_path):
        p = tmp_path / "truth.csv"
        _write(
            p,
            _TRUTH_COLS,
            [
                _truth_row("1", h_scheduled=1),
                {**_truth_row("2"), "reviewed": "0"},
                {**_truth_row("3"), "bad_ocr": "1"},
            ],
        )
        human, docket_of = funnel.read_truth(p)
        assert set(human) == {"1"}
        assert human["1"]["h_scheduled"] == 1
        assert docket_of["1"] == ("case-a", "1:24-cr-1", "cand")


class TestDeviationMath:
    def test_over_under_split(self):
        human = {"1": {**{c: 0 for c in funnel.CATS}, "h_scheduled": 1, "d_set": 2}}
        model = {"1": {**{c: 0 for c in funnel.CATS}, "h_scheduled": 3, "d_set": 1}}
        over, under = funnel.deviation_split(human, model)
        assert over["h_scheduled"] == 2 and under["h_scheduled"] == 0
        assert over["d_set"] == 0 and under["d_set"] == 1

    def test_missing_model_entry_counts_as_under(self):
        human = {"1": {**{c: 0 for c in funnel.CATS}, "h_held": 2}}
        over, under = funnel.deviation_split(human, {})
        assert sum(over.values()) == 0
        assert under["h_held"] == 2

    def test_bucket_split(self):
        over = {c: 0 for c in funnel.CATS}
        over.update(h_scheduled=1, d_set=2, h_held=3, h_cancelled=4)
        buckets = funnel.bucket_split(over)
        assert buckets == {"add": 3, "lifecycle": 3, "cancel": 4}

    def test_aggregate_neutralizes_attribution_drift(self):
        # Human pins the action to entry 1, the model to entry 2 — per-entry
        # deviation charges both, the per-docket aggregate charges neither.
        zero = {c: 0 for c in funnel.CATS}
        human = {"1": {**zero, "h_scheduled": 1}, "2": dict(zero)}
        model = {"1": dict(zero), "2": {**zero, "h_scheduled": 1}}
        docket_of = {"1": ("a", "d", "c"), "2": ("a", "d", "c")}
        over, under = funnel.deviation_split(human, model)
        assert sum(over.values()) + sum(under.values()) == 2
        assert funnel.aggregate_deviation(human, model, docket_of) == 0

    def test_aggregate_split_keeps_real_misses(self):
        # Drifted h_scheduled nets out; the unmarked d_met_filed survives as
        # a docket-level under, and a docket-level surplus shows as over.
        zero = {c: 0 for c in funnel.CATS}
        human = {
            "1": {**zero, "h_scheduled": 1, "d_met_filed": 1},
            "2": dict(zero),
        }
        model = {
            "1": dict(zero),
            "2": {**zero, "h_scheduled": 1, "d_set": 1},
        }
        docket_of = {"1": ("a", "d", "c"), "2": ("a", "d", "c")}
        over, under = funnel.aggregate_split(human, model, docket_of)
        assert under["h_scheduled"] == 0
        assert under["d_met_filed"] == 1
        assert over["d_set"] == 1
        assert funnel.aggregate_deviation(human, model, docket_of) == 2

    def test_aggregate_split_does_not_net_across_dockets(self):
        # The same drift split across two different dockets must NOT net out.
        zero = {c: 0 for c in funnel.CATS}
        human = {"1": {**zero, "h_scheduled": 1}, "2": dict(zero)}
        model = {"1": dict(zero), "2": {**zero, "h_scheduled": 1}}
        docket_of = {"1": ("a", "d1", "c"), "2": ("a", "d2", "c")}
        over, under = funnel.aggregate_split(human, model, docket_of)
        assert under["h_scheduled"] == 1
        assert over["h_scheduled"] == 1


_LOG_LINE = (
    "2026-06-10 11:10:19,453 INFO provider_stores.decisions extract "
    'docket={d} entry={e} "{desc}" -> {actions}'
)


def _log(*lines) -> str:
    return "\n".join(lines)


class TestDecisionLogParsing:
    def test_parses_actions_with_key_significance_dates(self):
        text = _log(
            _LOG_LINE.format(
                d=1,
                e=10,
                desc="Order setting trial",
                actions="ADD_HEARING(trial-a, major, 2026-07-01, 10:00), "
                "MARK_FILED(brief-b, 2026-06-01)",
            )
        )
        actions = funnel.parse_decision_log(text, {"10"})
        assert actions["10"] == [
            ("ADD_HEARING", "trial-a", ("2026-07-01",)),
            ("MARK_FILED", "brief-b", ("2026-06-01",)),
        ]

    def test_skips_unscored_entries_and_unmapped_actions(self):
        text = _log(
            _LOG_LINE.format(d=1, e=10, desc="x", actions="IGNORE(why-not)"),
            _LOG_LINE.format(d=1, e=99, desc="y", actions="ADD_HEARING(k, major)"),
        )
        actions = funnel.parse_decision_log(text, {"10"})
        assert actions == {}

    def test_keyless_action_keeps_empty_key(self):
        text = _log(
            _LOG_LINE.format(d=1, e=10, desc="x", actions="MARK_HELD(2026-01-05)")
        )
        actions = funnel.parse_decision_log(text, {"10"})
        assert actions["10"] == [("MARK_HELD", "", ("2026-01-05",))]


class TestCollapseRepeats:
    def test_repeat_on_sibling_entry_is_dropped(self):
        sig = ("MARK_HELD", "conf-a", ("2025-07-30",))
        actions = {"1": [sig], "2": [sig], "3": [sig]}
        collapsed, repeats = funnel.collapse_repeats(actions)
        assert collapsed["1"]["h_held"] == 1
        assert "2" not in collapsed and "3" not in collapsed
        assert [(s, first, rep) for s, first, rep in repeats] == [
            (sig, "1", "2"),
            (sig, "1", "3"),
        ]

    def test_same_entry_duplicates_are_not_repeats(self):
        sig = ("ADD_DEADLINE", "resp-a", ("2026-01-01",))
        collapsed, repeats = funnel.collapse_repeats({"1": [sig, sig]})
        assert collapsed["1"]["d_set"] == 2
        assert repeats == []

    def test_different_dates_are_distinct_actions(self):
        actions = {
            "1": [("RESCHEDULE_HEARING", "trial-a", ("2026-01-01",))],
            "2": [("RESCHEDULE_HEARING", "trial-a", ("2026-02-01",))],
        }
        collapsed, repeats = funnel.collapse_repeats(actions)
        assert repeats == []
        assert collapsed["1"]["h_rescheduled"] == 1
        assert collapsed["2"]["h_rescheduled"] == 1


class TestStoreHelpers:
    def test_minor_key_buckets(self):
        buckets = funnel.minor_key_buckets(
            [
                "redaction-request-ding-01-06-transcript",
                "amicus-reply-deadline",
                "transcript-release-x",
                "mediation-questionnaire-deadline",
            ]
        )
        assert buckets["transcript redaction-request"] == 1
        assert buckets["amicus response / reply"] == 1
        assert buckets["other transcript"] == 1
        assert buckets["other procedural"] == 1

    def test_base_key_strips_numeric_suffix_only(self):
        assert funnel.base_key("motions-deadline-2") == "motions-deadline"
        assert funnel.base_key("transcript-3-8") == "transcript-3"
        assert funnel.base_key("plain-key") == "plain-key"

    def test_key_families_groups_only_multi_row_families(self):
        rows = [
            {"docket_number": "1:24-cr-1", "deadline_key": "motions-deadline"},
            {"docket_number": "1:24-cr-1", "deadline_key": "motions-deadline-2"},
            {"docket_number": "1:24-cr-1", "deadline_key": "lonely-key"},
            {"docket_number": "2:24-cr-2", "deadline_key": "motions-deadline"},
        ]
        fams = funnel.key_families(rows, "deadline_key")
        assert set(fams) == {("1:24-cr-1", "motions-deadline")}
        assert len(fams[("1:24-cr-1", "motions-deadline")]) == 2

    def test_count_vevents(self, tmp_path):
        (tmp_path / "a.ics").write_text(
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nEND:VEVENT\nBEGIN:VEVENT\n"
            "END:VEVENT\nEND:VCALENDAR\n"
        )
        (tmp_path / "b.ics").write_text("BEGIN:VCALENDAR\nEND:VCALENDAR\n")
        assert funnel.count_vevents(tmp_path) == {"a.ics": 2, "b.ics": 0}


class TestMain:
    def test_csv_only_report_without_store(self, tmp_path, capsys):
        truth = tmp_path / "truth.csv"
        _write(
            truth,
            _TRUTH_COLS,
            [_truth_row("1", h_scheduled=1), _truth_row("2", d_set=1)],
        )
        acts = tmp_path / "actions.csv"
        _write(
            acts,
            _MODEL_COLS,
            [
                {"provider": "fake/model", "entry_id": "1", "h_scheduled": 2},
                {"provider": "fake/model", "entry_id": "2", "d_set": 1},
            ],
        )
        funnel.main(
            [
                "fake/model",
                "--truth",
                str(truth),
                "--model-actions",
                str(acts),
                "--store-dir",
                str(tmp_path / "nonexistent"),
            ]
        )
        out = capsys.readouterr().out
        assert "Per-entry deviation: **1** (over 1 / under 0)" in out
        assert "store / log / ICS sections skipped" in out

    def test_unknown_model_label_fails_loud(self, tmp_path):
        truth = tmp_path / "truth.csv"
        _write(truth, _TRUTH_COLS, [_truth_row("1", h_scheduled=1)])
        acts = tmp_path / "actions.csv"
        _write(acts, _MODEL_COLS, [])
        with pytest.raises(SystemExit, match="no rows"):
            funnel.main(
                ["nope/model", "--truth", str(truth), "--model-actions", str(acts)]
            )
