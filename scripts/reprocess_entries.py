"""One-shot helper to reprocess specific docket entries through the LLM.

Usage:
    uv run python scripts/reprocess_entries.py 445337354 448991171

Reads the stored entry text from the local SQLite store, builds a synthetic
entry dict (no CourtListener API call required for paperless entries), clears the
fingerprint so the dedup check doesn't short-circuit, and runs the entry
through ``CaseSyncer.process_entry`` so the LLM can re-extract using the
current prompt + related-entries context.

Cases are looked up via config.yaml so the right ``CaseConfig.case_id`` is
attached. Hearings whose text the LLM revises will have their titles /
keys updated in place via the normal ``upsert_hearing`` path.
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from case_calendar.cli import _cases_from_config, _load_config
from case_calendar.courtlistener import CourtListener
from case_calendar.store import Store
from case_calendar.sync import CaseSyncer


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "entry_ids",
        nargs="+",
        type=int,
        help="CourtListener entry_id values (NOT entry_number) to reprocess",
    )
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("--db", default="data/case-calendar.sqlite")
    args = parser.parse_args(argv)

    cfg = _load_config(args.config)
    cases = _cases_from_config(cfg)

    store = Store(args.db)
    cl = CourtListener()
    syncer = CaseSyncer(cl, store)

    for entry_id in args.entry_ids:
        row = store.conn.execute(
            "SELECT docket_id, entry_id, entry_number, date_filed, "
            "date_modified, description, short_description "
            "FROM entries WHERE entry_id=?",
            (entry_id,),
        ).fetchone()
        if not row:
            print(f"entry_id={entry_id} not found in store; skipping")
            continue

        case = next(
            (c for c in cases if row["docket_id"] in c.dockets),
            None,
        )
        if not case:
            print(
                f"entry_id={entry_id} on docket {row['docket_id']} matches no "
                "case in config.yaml; skipping"
            )
            continue

        # Synthetic entry dict: paperless entries have no recap_documents,
        # so the LLM doesn't need a PDF fetch. The reprocess pathway runs
        # the LLM with the current prompt + related-entries resolver.
        synthetic = {
            "id": row["entry_id"],
            "entry_number": row["entry_number"],
            "date_filed": row["date_filed"],
            "date_modified": row["date_modified"],
            "description": row["description"] or "",
            "short_description": row["short_description"] or "",
            "recap_documents": [],
            "docket": row["docket_id"],
        }

        # Bypass the dedup check by clearing the stored fingerprint —
        # entry_seen() compares stored vs computed; an empty stored value
        # never matches a real sha1.
        store.conn.execute(
            "UPDATE entries SET fingerprint='' WHERE entry_id=?",
            (entry_id,),
        )
        store.conn.commit()

        print(
            f"\n--- reprocessing entry_id={entry_id} "
            f"(docket {row['docket_id']}, case {case.case_id}) ---"
        )
        syncer.process_entry(case, row["docket_id"], synthetic)
        store.conn.commit()

    store.close()
    cl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
