"""Backfill hearing notes that regressed to a pre-hearing setup notice.

A hearing's calendar description (the ``notes`` field) is written by
scheduling/notice-type actions; ``MARK_HELD`` preserved whatever was last
written and a dedupe ``MERGE_INTO`` kept the survivor's notes, so a
description could freeze on a pre-hearing administrative notice (a clerk's
notice of Zoom access / courtroom change / scheduling) even after the docket
recorded what actually happened. The code fix (``sync.py``) prevents new
regressions, but rows already collapsed in the store stay collapsed — the
sibling that held the good notes may have been deleted by the dedupe merge,
so re-running sync can't recover them.

This sweep fixes them deterministically (no LLM, no CourtListener): for every
hearing whose notes are empty or an administrative notice but whose source
entries include the RECORD of the proceeding (a minute entry / transcript /
clerk's notes of proceedings held), it replaces the notes with that record's
own text. Hearings that already describe the proceeding, and hearings carrying
a curated non-administrative note, are left untouched.

Usage:
    uv run python scripts/heal_proceeding_notes.py            # dry run
    uv run python scripts/heal_proceeding_notes.py --apply    # write + commit
    uv run python scripts/heal_proceeding_notes.py --db <path>

Read-only by default. Back up the store before ``--apply`` if you care about
the DB (see AGENTS.md) — though this only rewrites the ``notes`` column and
never changes schema, status, dates, or source lists.
"""

from __future__ import annotations

import argparse
import sys

from case_calendar.store import Store
from case_calendar.sync import heal_proceeding_notes


def _truncate(text: str | None, width: int = 100) -> str:
    if not text:
        return "(empty)"
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/case-calendar.sqlite")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the healed notes back to the DB (default: dry run)",
    )
    args = parser.parse_args(argv)

    store = Store(args.db)
    changes = heal_proceeding_notes(store, apply=args.apply)

    if not changes:
        print("no regressed hearing notes found — nothing to heal")
        return 0

    verb = "healed" if args.apply else "would heal"
    print(f"{verb} {len(changes)} hearing note(s):\n")
    for c in changes:
        print(f"  {c['case_id']} / {c['hearing_key']}")
        print(f"      old: {_truncate(c['old'])}")
        print(f"      new: {_truncate(c['new'])}")
    if not args.apply:
        print("\n(dry run — re-run with --apply to write these changes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
