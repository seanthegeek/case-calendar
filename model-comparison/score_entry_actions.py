#!/usr/bin/env python3
"""Score each provider's per-entry extractor actions against human ground truth.

The human fills ``ground_truth.csv`` by reading every entry's COMPLETE text in
``ground_truth_scoring.html`` (built by ``build_scoring_page.py``) and tallying,
per entry, the eight action counts the extractor itself emits. The model side
(``model_entry_actions.csv``, captured by ``build_provider_stores.py
--entry-actions-csv``) carries the same eight counts per entry per provider. This
script joins them on ``entry_id`` and reports, per provider, how far the model's
per-entry actions deviate from the human's.

Why per-entry (not per-docket counts off the web UI like the old ``score.py``):
the web UI is incomplete relative to the v4 API (freelawproject/courtlistener
#7429), so a web-UI count under-reports real actions and penalizes a correct
extractor. Here the human reads the SAME complete API text the model saw, so a
deviation is a real extraction error — and a real action the regex pre-filter
dropped (no provider ever saw it) shows up as a provider-independent miss.

Metrics (lower deviation is better):
  * per-entry total deviation = sum over scored entries, over the 8 categories,
    of |model − human|; split into OVER (model > human, e.g. duplicate keys /
    hallucination) and UNDER (model < human, missed).
  * per-docket-aggregate deviation = same, but counts summed per logical docket
    first — robust to the model and human pinning an action to a slightly
    different entry (the docket total is identical either way).
  * regex-stage misses = scored entries where the human counted > 0 but EVERY
    provider emitted 0 (the regex pre-filter is provider-independent, so an
    all-providers-zero with a human count means the regex dropped it before any
    LLM saw it). This is the number that quantifies the "intentionally
    over-inclusive regex" claim.

Entries flagged ``bad_ocr`` in the worksheet are EXCLUDED (unreadable source —
not the model's fault). Only ``reviewed`` entries are scored unless
``--include-unreviewed`` (an unreviewed row's 0s aren't a real human judgment).

Usage:
    python3 model-comparison/score_entry_actions.py \
        [--truth model-comparison/ground_truth.csv] \
        [--model model-comparison/model_entry_actions.csv] \
        [--out model-comparison/SCORECARD_entry.md] [--include-unreviewed]
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path
from typing import Optional

CATS = [
    "h_scheduled",
    "h_rescheduled",
    "h_held",
    "h_cancelled",
    "d_set",
    "d_rescheduled",
    "d_met_filed",
    "d_cancelled",
]
_SHORT = {
    "h_scheduled": "Hs",
    "h_rescheduled": "Hr",
    "h_held": "Hh",
    "h_cancelled": "Hc",
    "d_set": "Ds",
    "d_rescheduled": "Dr",
    "d_met_filed": "Df",
    "d_cancelled": "Dc",
}


def _int(v: Optional[str]) -> int:
    v = (v or "").strip()
    try:
        return int(v)
    except ValueError:
        return 0


def load_truth(path: Path, include_unreviewed: bool) -> dict[str, dict]:
    """entry_id -> {counts, docket key, flags}. Skips bad_ocr; skips unreviewed
    unless asked."""
    truth: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            eid = (row.get("entry_id") or "").strip()
            if not eid:
                continue
            if _int(row.get("bad_ocr")):
                continue
            if not include_unreviewed and not _int(row.get("reviewed")):
                continue
            truth[eid] = {
                "case_id": (row.get("case_id") or "").strip(),
                "docket": (row.get("docket_number") or "").strip(),
                "court": (row.get("court") or "").strip(),
                "entry_number": (row.get("entry_number") or "").strip(),
                "counts": {c: _int(row.get(c)) for c in CATS},
            }
    return truth


def load_model(path: Path) -> dict[str, dict[str, dict[str, int]]]:
    """provider -> entry_id -> counts."""
    out: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            prov = (row.get("provider") or "").strip()
            eid = (row.get("entry_id") or "").strip()
            if not prov or not eid:
                continue
            out[prov][eid] = {c: _int(row.get(c)) for c in CATS}
    return out


def build_report(
    truth: dict[str, dict], model: dict[str, dict[str, dict[str, int]]]
) -> str:
    providers = sorted(model)
    zero = {c: 0 for c in CATS}

    # per-entry over/under per provider per category
    over = {p: {c: 0 for c in CATS} for p in providers}
    under = {p: {c: 0 for c in CATS} for p in providers}
    # per-docket aggregate deviation per provider
    agg_h: dict[tuple, dict[str, int]] = defaultdict(lambda: {c: 0 for c in CATS})
    agg_m: dict[tuple, dict[str, dict[str, int]]] = defaultdict(
        lambda: {p: {c: 0 for c in CATS} for p in providers}
    )
    # regex-miss accounting
    regex_miss_actions = {c: 0 for c in CATS}
    regex_miss_entries: list[dict] = []

    for eid, t in truth.items():
        dk = (t["case_id"], t["docket"], t["court"])
        hc = t["counts"]
        for c in CATS:
            agg_h[dk][c] += hc[c]
        human_total = sum(hc.values())
        all_zero = True
        for p in providers:
            mc = model.get(p, {}).get(eid, zero)
            for c in CATS:
                d = mc[c] - hc[c]
                if d > 0:
                    over[p][c] += d
                elif d < 0:
                    under[p][c] += -d
                agg_m[dk][p][c] += mc[c]
            if sum(mc.values()) > 0:
                all_zero = False
        if human_total > 0 and all_zero:
            for c in CATS:
                regex_miss_actions[c] += hc[c]
            regex_miss_entries.append({**t, "entry_id": eid})

    def dev(p: str) -> int:
        return sum(over[p][c] + under[p][c] for c in CATS)

    agg_dev = {p: 0 for p in providers}
    for dk in agg_h:
        for p in providers:
            for c in CATS:
                agg_dev[p] += abs(agg_m[dk][p][c] - agg_h[dk][c])

    total_human_actions = sum(sum(t["counts"].values()) for t in truth.values())

    L: list[str] = ["# Per-entry extraction accuracy vs human ground truth", ""]
    L.append(
        f"Scored **{len(truth)}** entries (reviewed, not bad-OCR) carrying "
        f"**{total_human_actions}** human-counted actions, across "
        f"**{len({(t['case_id'], t['docket'], t['court']) for t in truth.values()})}** "
        "logical dockets. Lower deviation = closer to the human truth."
    )
    L.append("")

    L.append("## Per-entry deviation (lower is better)")
    L.append("")
    L.append(
        "| provider | total | over | under | "
        + " | ".join(_SHORT[c] for c in CATS)
        + " |"
    )
    L.append("| --- | ---: | ---: | ---: |" + " ---: |" * len(CATS))
    for p in sorted(providers, key=dev):
        o = sum(over[p].values())
        u = sum(under[p].values())
        cells = " | ".join(str(over[p][c] + under[p][c]) for c in CATS)
        L.append(f"| {p} | **{dev(p)}** | {o} | {u} | {cells} |")
    L.append("")
    L.append(
        "`over` = model counted MORE than the human (duplicate keys / "
        "hallucination); `under` = model counted FEWER (missed). "
        + " · ".join(f"{_SHORT[c]}={c}" for c in CATS)
    )
    L.append("")

    L.append("## Per-docket-aggregate deviation (attribution-drift-robust)")
    L.append("")
    L.append("| provider | aggregate deviation |")
    L.append("| --- | ---: |")
    for p in sorted(providers, key=lambda p: agg_dev[p]):
        L.append(f"| {p} | **{agg_dev[p]}** |")
    L.append("")

    L.append("## Regex-stage misses (provider-independent)")
    L.append("")
    miss_total = sum(regex_miss_actions.values())
    pct = (100 * miss_total / total_human_actions) if total_human_actions else 0
    L.append(
        f"**{len(regex_miss_entries)}** scored entries carried **{miss_total}** "
        f"human-counted actions that EVERY provider missed with a 0 — i.e. the "
        f"`is_extractable` regex dropped them before any LLM saw them "
        f"(**{pct:.1f}%** of all human actions). By category: "
        + ", ".join(
            f"{_SHORT[c]} {regex_miss_actions[c]}"
            for c in CATS
            if regex_miss_actions[c]
        )
        + "."
    )
    if regex_miss_entries:
        L.append("")
        for e in regex_miss_entries[:40]:
            nz = ", ".join(
                f"{_SHORT[c]} {e['counts'][c]}" for c in CATS if e["counts"][c]
            )
            L.append(
                f"- {e['case_id']} {e['docket']} ({e['court']}) "
                f"entry #{e['entry_number'] or '?'} (id {e['entry_id']}): {nz}"
            )
        if len(regex_miss_entries) > 40:
            L.append(f"- … and {len(regex_miss_entries) - 40} more")
    L.append("")
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--truth", default="model-comparison/ground_truth.csv")
    ap.add_argument("--model", default="model-comparison/model_entry_actions.csv")
    ap.add_argument("--out", help="also write the markdown report here")
    ap.add_argument(
        "--include-unreviewed",
        action="store_true",
        help="score rows the human didn't tick 'reviewed' (their 0s are scored as truth)",
    )
    args = ap.parse_args(argv)

    truth_path, model_path = Path(args.truth), Path(args.model)
    if not truth_path.exists():
        raise SystemExit(
            f"{truth_path} not found — fill ground_truth_scoring.html and Download CSV first."
        )
    if not model_path.exists():
        raise SystemExit(
            f"{model_path} not found — run build_provider_stores.py --entry-actions-csv first."
        )

    truth = load_truth(truth_path, args.include_unreviewed)
    if not truth:
        raise SystemExit(
            f"{truth_path} has no scored entries yet (none reviewed, or all bad-OCR)."
        )
    model = load_model(model_path)
    if not model:
        raise SystemExit(f"{model_path} has no model rows.")

    report = build_report(truth, model)
    print(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report + "\n", encoding="utf-8")
        print(f"\nwrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
