#!/usr/bin/env python3
"""Generate a BLIND ground-truth worksheet for scoring LLM providers.

The credible way to rank the providers is to have a human establish the truth
by reading the dockets — not an AI, not a heuristic — and then let a dumb script
measure each provider store's deviation from that truth (``score.py``).

This script emits the worksheet you fill in. It lists every *CourtListener record*
— one row per CourtListener docket_id — carrying its PACER docket number + court
(so the records of one split docket sit on adjacent rows) and the link to read,
with empty columns for the counts. **It contains no model output** — so filling it
cannot be biased by what any provider produced.

How to fill the worksheet (which events count, how reschedules and split records
are handled, the six count columns) is documented once, canonically, in
``model-comparison/README.md`` under "How to fill each row" — read it there
rather than a copy here that could drift out of sync.

Usage:
    uv run python model-comparison/ground_truth_worksheet.py \
        [--config config.yaml] [--out model-comparison/ground_truth.template.csv]

Refuses to overwrite an existing worksheet (so a re-run can't wipe your filled-in
numbers); pass --force to regenerate a blank one.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

from case_calendar.cli import _cases_from_config, _load_config  # noqa: E402
from case_calendar.store import Store  # noqa: E402

_CL_BASE = "https://www.courtlistener.com"

# The reference columns (filled by this script) then the count columns (filled
# by the human). Order is the fill order in a spreadsheet, left to right.
_COUNT_COLUMNS = [
    "hearings_scheduled",
    "hearings_held",
    "hearings_cancelled",
    "deadlines_pending",
    "deadlines_met_or_passed",
    "deadlines_cancelled",
]
_COLUMNS = [
    "case_id",
    "case_name",
    "docket_number",
    "court",
    "courtlistener_id",
    "courtlistener_url",
    *_COUNT_COLUMNS,
    "notes",
]


def _full_url(absolute_url: Optional[str]) -> str:
    if not absolute_url:
        return ""
    if absolute_url.startswith("http"):
        return absolute_url
    return _CL_BASE + absolute_url


def build_rows(config_path: str) -> list[dict[str, Any]]:
    cfg = _load_config(config_path)
    cases = _cases_from_config(cfg)
    store = Store(cfg.get("store_path", "data/case-calendar.sqlite"))
    try:
        rows: list[dict[str, Any]] = []
        for case in cases:
            # One row per CourtListener record (docket_id) — the unit the human
            # scores. Group by (docket_number, court) only to keep the records of
            # one split PACER docket on adjacent, sorted rows.
            groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
            for did in case.dockets:
                meta = store.get_docket_meta(did) or {}
                key = (
                    meta.get("docket_number") or f"(unsynced docket_id {did})",
                    meta.get("court_id") or "?",
                )
                groups[key].append({"docket_id": did, **meta})
            for (docket_number, court), records in sorted(groups.items()):
                for r in records:
                    row = {
                        "case_id": case.case_id,
                        "case_name": case.name,
                        "docket_number": docket_number,
                        "court": court,
                        "courtlistener_id": r["docket_id"],
                        "courtlistener_url": _full_url(r.get("absolute_url")),
                        "notes": "",
                    }
                    for c in _COUNT_COLUMNS:
                        row[c] = ""
                    rows.append(row)
        return rows
    finally:
        closer = getattr(store, "close", None)
        if closer:
            closer()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--out", default="model-comparison/ground_truth.template.csv")
    ap.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing worksheet (DANGER: wipes any counts you filled in)",
    )
    args = ap.parse_args(argv)

    out = Path(args.out)
    if out.exists() and not args.force:
        raise SystemExit(
            f"{out} already exists; refusing to overwrite your filled-in worksheet. "
            "Pass --force to regenerate a blank one."
        )

    rows = build_rows(args.config)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(
        f"wrote {out} — {len(rows)} CourtListener records, blind (no model output). "
        f"Fill the {len(_COUNT_COLUMNS)} count columns by reading each record's page, "
        "then run model-comparison/score.py."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
