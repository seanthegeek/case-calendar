#!/usr/bin/env python3
"""Score each model against a human-filled ground-truth worksheet.

The human establishes the truth by reading the dockets (see
``ground_truth_worksheet.py``); this is the dumb, deterministic part. It reads
the model outputs from ``model_events.csv`` (produced by
``export_model_events.py``) and, for every logical docket the human scored,
counts the same six numbers per model and reports how far each deviates from the
human's numbers. No LLM, no judgment — just |model − truth| summed up.

Counts are per LOGICAL docket (docket number + court; CourtListener records of one
PACER docket collapse together), over EVERY event regardless of significance,
bucketed by status:

  hearings:  scheduled / held / cancelled
  deadlines: pending / met_or_passed (met + passed) / cancelled

Counting raw events per logical docket is deliberate: if a model splits one
logical event across keys or CourtListener records, its count rises above the
human's truth and the deviation captures it — duplication and over-extraction
score themselves, as do missed events (the count falls below the truth).

Anyone can run this: read the same public dockets, fill your own copy of the
worksheet, and point ``--truth`` at it. Fill the worksheet from the DOCKETS, not
from ``model_events.csv`` — keeping your scoring blind to the models' answers is
the whole point.

Usage:
    python3 model-comparison/score.py \
        [--events model-comparison/model_events.csv] \
        [--truth model-comparison/ground_truth.csv] \
        [--out model-comparison/SCORECARD.md]
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Optional

CATEGORIES = [
    "hearings_scheduled",
    "hearings_held",
    "hearings_cancelled",
    "deadlines_pending",
    "deadlines_met_or_passed",
    "deadlines_cancelled",
]
_SHORT = {
    "hearings_scheduled": "H sched",
    "hearings_held": "H held",
    "hearings_cancelled": "H canc",
    "deadlines_pending": "D pend",
    "deadlines_met_or_passed": "D met/pass",
    "deadlines_cancelled": "D canc",
}
# prod is a baseline reference; the model columns are what's being compared.
# Column labels are ``provider/extraction-model`` (e.g. ``gemini/gemini-3.5-flash``);
# prod sorts first, then the rest sort alphabetically, which groups columns by
# their provider prefix so sibling models on one provider sit together.

Counts = dict[str, int]
LogicalKey = tuple[str, str]  # (docket_number, court)


def _zero() -> Counts:
    return {c: 0 for c in CATEGORIES}


def load_truth(
    path: Path,
) -> tuple[dict[LogicalKey, Counts], dict[LogicalKey, str], list[str]]:
    """Return (scored truth by logical key, case-name by key, unfilled labels).

    A row is "scored" only when all six count columns parse as integers; a row
    with any blank count is treated as not-yet-filled and skipped."""
    truth: dict[LogicalKey, Counts] = {}
    names: dict[LogicalKey, str] = {}
    unfilled: list[str] = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["docket_number"].strip(), row["court"].strip())
            names[key] = (row.get("case_name") or "").strip()
            counts: Counts = {}
            ok = True
            for c in CATEGORIES:
                raw = (row.get(c) or "").strip()
                if raw == "":
                    ok = False
                    break
                try:
                    counts[c] = int(raw)
                except ValueError as exc:
                    raise SystemExit(
                        f"{path}: docket {key[0]} ({key[1]}) column {c!r} is "
                        f"{raw!r}, not an integer"
                    ) from exc
            if ok:
                truth[key] = counts
            else:
                unfilled.append(f"{names[key]} — {key[0]} ({key[1]})")
    return truth, names, unfilled


def load_model_events(path: Path) -> dict[str, dict[LogicalKey, Counts]]:
    """Aggregate the events CSV into per-provider, per-logical-docket counts."""
    by_provider: dict[str, dict[LogicalKey, Counts]] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            prov = row["provider"].strip()
            key = (row["docket_number"].strip(), row["court"].strip())
            counts = by_provider.setdefault(prov, {}).setdefault(key, _zero())
            status = (row["status"] or "").strip()
            if row["type"] == "hearing":
                if status in ("scheduled", "held", "cancelled"):
                    counts[f"hearings_{status}"] += 1
            elif row["type"] == "deadline":
                if status == "pending":
                    counts["deadlines_pending"] += 1
                elif status in ("met", "passed"):
                    counts["deadlines_met_or_passed"] += 1
                elif status == "cancelled":
                    counts["deadlines_cancelled"] += 1
    return by_provider


def _tuple(c: Counts) -> str:
    return (
        f"H {c['hearings_scheduled']}/{c['hearings_held']}/{c['hearings_cancelled']} "
        f"D {c['deadlines_pending']}/{c['deadlines_met_or_passed']}/{c['deadlines_cancelled']}"
    )


def deviation(model: Counts, truth: Counts) -> dict[str, int]:
    return {c: abs(model.get(c, 0) - truth[c]) for c in CATEGORIES}


def build_report(
    truth: dict[LogicalKey, Counts],
    names: dict[LogicalKey, str],
    events: dict[str, dict[LogicalKey, Counts]],
    unfilled: list[str],
) -> str:
    # prod first (a baseline), then the model columns sorted — alphabetical
    # order groups them by provider prefix.
    order = ["prod"] if "prod" in events else []
    order += sorted(p for p in events if p != "prod")

    totals = {p: 0 for p in order}
    cat_totals = {p: _zero() for p in order}
    per_docket: dict[LogicalKey, dict[str, int]] = {}
    for key, t in truth.items():
        per_docket[key] = {}
        for p in order:
            dev = deviation(events[p].get(key, _zero()), t)
            d = sum(dev.values())
            per_docket[key][p] = d
            totals[p] += d
            for c in CATEGORIES:
                cat_totals[p][c] += dev[c]

    label = {"prod": "prod (live)"}
    L: list[str] = ["# Provider accuracy vs human ground truth", ""]
    L.append(
        f"Scored **{len(truth)}** of {len(truth) + len(unfilled)} logical dockets "
        "(those with all six counts filled in). Lower deviation = closer to the "
        "human-read truth. Deviation is the sum of |model count − your count| over "
        "the six status categories."
    )
    L.append("")
    L.append("## Totals (lower is better)")
    L.append("")
    L.append(
        "| model | total deviation | "
        + " | ".join(_SHORT[c] for c in CATEGORIES)
        + " |"
    )
    L.append("| --- | ---: |" + " ---: |" * len(CATEGORIES))
    for p in sorted(order, key=lambda p: totals[p]):
        cells = " | ".join(str(cat_totals[p][c]) for c in CATEGORIES)
        L.append(f"| {label.get(p, p)} | **{totals[p]}** | {cells} |")
    L.append("")

    L.append("## Per-docket detail")
    L.append("")
    L.append(
        "Truth vs each model. Format: "
        "`H scheduled/held/cancelled  D pending/met-or-passed/cancelled`."
    )
    L.append("")
    for key in sorted(truth, key=lambda k: -max(per_docket[k].values(), default=0)):
        L.append(f"### {names.get(key, '')} — {key[0]} ({key[1]})")
        L.append("")
        L.append(f"- truth: `{_tuple(truth[key])}`")
        for p in order:
            c = events[p].get(key, _zero())
            L.append(
                f"- {label.get(p, p)}: `{_tuple(c)}` — deviation {per_docket[key][p]}"
            )
        L.append("")

    if unfilled:
        L.append("## Not yet scored")
        L.append("")
        for lab in unfilled:
            L.append(f"- {lab}")
        L.append("")
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--events", default="model-comparison/model_events.csv")
    ap.add_argument("--truth", default="model-comparison/ground_truth.csv")
    ap.add_argument("--out", help="also write the markdown report here")
    args = ap.parse_args(argv)

    events_path = Path(args.events)
    if not events_path.exists():
        raise SystemExit(
            f"{events_path} not found — produce it with export_model_events.py."
        )
    truth_path = Path(args.truth)
    if not truth_path.exists():
        raise SystemExit(
            f"{truth_path} not found — copy ground_truth.template.csv to it and "
            "fill in your counts first."
        )

    truth, names, unfilled = load_truth(truth_path)
    if not truth:
        raise SystemExit(
            f"{truth_path} has no filled-in rows yet. Fill the six count columns "
            "for at least one logical docket, then re-run."
        )
    events = load_model_events(events_path)
    if not any(p != "prod" for p in events):
        raise SystemExit(f"{events_path} has no model-column events.")

    report = build_report(truth, names, events, unfilled)
    print(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report + "\n", encoding="utf-8")
        print(f"\nwrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
