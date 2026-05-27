#!/usr/bin/env python3
"""Export each provider store's events to one CSV — the committed source data.

``build_provider_stores.py`` produces a full SQLite store per provider under the
gitignored ``data/provider-stores/`` (large, and the rendered calendars inside
would make the blind ground-truth scoring peekable). This dumps just the model
OUTPUTS — every hearing and deadline each provider extracted, one raw row each,
with its logical docket, status, significance, and date — into a single
``model_events.csv`` that ``score.py`` reads.

Raw per-event rows on purpose: a human filling the ground-truth worksheet can't
eyeball "model X says N hearings on docket Y" from this without aggregating it
(i.e. running the scorer, which they do AFTER scoring) — so committing it doesn't
hand them an answer key. The logical docket (docket number + court) is on every
row, so one PACER docket split across several CourtListener records collapses
naturally when the scorer groups by it.

Every store under ``--stores`` is exported as one column, labelled by its path
relative to ``--stores``. ``build_provider_stores.py`` nests each column at
``<provider>/<extraction-model>/``, so the label is ``provider/extraction-model``
(e.g. ``gemini/gemini-3.5-flash``); a flat one-level store still works and is
labelled by its bare folder name. The labels flow straight through to the CSV's
``provider`` column and on into the scorer — no hardcoded list to keep in sync
when columns are added.

Usage:
    python3 model-comparison/export_model_events.py \
        [--stores data/provider-stores] [--prod data/case-calendar.sqlite] \
        [--out model-comparison/model_events.csv]
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path
from typing import Any, Optional

COLUMNS = [
    "provider",
    "type",
    "case_id",
    "docket_number",
    "court",
    "docket_id",
    "title",
    "status",
    "significance",
    "date",
    "source_entry_ids",
]


def _events(db_path: Path, provider: str) -> list[dict[str, Any]]:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    rows: list[dict[str, Any]] = []
    for typ, table, date_col in (
        ("hearing", "hearings", "starts_at_utc"),
        ("deadline", "deadlines", "due_at_utc"),
    ):
        for r in db.execute(
            f"""
            SELECT t.case_id, t.docket_id, d.docket_number, d.court_id,
                   t.title, t.status, t.significance,
                   t.{date_col} AS date, t.source_entry_ids
            FROM {table} t LEFT JOIN dockets d ON t.docket_id = d.docket_id
            ORDER BY d.docket_number, t.{date_col}
            """
        ):
            rows.append(
                {
                    "provider": provider,
                    "type": typ,
                    "case_id": r["case_id"],
                    "docket_number": (r["docket_number"] or "").strip(),
                    "court": (r["court_id"] or "").strip(),
                    "docket_id": r["docket_id"],
                    "title": r["title"],
                    "status": r["status"],
                    "significance": r["significance"],
                    "date": r["date"],
                    "source_entry_ids": r["source_entry_ids"],
                }
            )
    db.close()
    return rows


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--stores", default="data/provider-stores")
    ap.add_argument("--prod", default="data/case-calendar.sqlite")
    ap.add_argument("--out", default="model-comparison/model_events.csv")
    args = ap.parse_args(argv)

    base = Path(args.stores)
    all_rows: list[dict[str, Any]] = []
    sources: list[str] = []

    prod = Path(args.prod)
    if prod.exists():
        all_rows += _events(prod, "prod")
        sources.append("prod")
    # Discover every store under --stores (one per built comparison column),
    # labelled by its path relative to --stores — so a nested
    # <provider>/<model>/case-calendar.sqlite becomes "provider/model" and a
    # flat one-level store becomes its bare folder name. Sorted for a
    # deterministic CSV order.
    columns: list[str] = []
    if base.is_dir():
        for store in sorted(base.rglob("case-calendar.sqlite")):
            label = store.parent.relative_to(base).as_posix()
            all_rows += _events(store, label)
            sources.append(label)
            columns.append(label)

    if not columns:
        raise SystemExit(f"no model stores found under {base}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"wrote {out} — {len(all_rows)} events from {', '.join(sources)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
