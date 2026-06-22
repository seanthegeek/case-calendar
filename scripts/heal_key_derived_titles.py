"""Recompute key-derived titles: hearings drop a redundant single-defendant
name ("Sentencing Lytvynenko" -> "Sentencing"), and hearings AND deadlines
expand proceeding-type abbreviations ("Status Conf" -> "Status Conference",
"Govt Status Report" -> "Government Status Report").

A row the LLM never gave an explicit title to falls back to the humanized key:
a hearing carries the defendant slug the key holds for disambiguation (redundant
on a single-defendant docket, needed on a co-defendant one), and either kind can
carry a key abbreviation (`conf`, `govt`). The write-time fallbacks
(``sync._fallback_hearing_title`` / ``_fallback_deadline_title``) now handle
both, but rows stored before that landed keep the old title — this sweep repairs
them deterministically (no LLM, no CourtListener).

Touches ONLY rows whose CURRENT title is a fallback-derived form, so an explicit
LLM title is never rewritten. Hearings are judged single- vs multi-defendant
from each case's own hearing keys (co-defendant dockets keep their names,
rendered "<Type> - <Name>"); deadlines get abbreviation expansion only (freeform
keys, no name stripping).

Usage:
    uv run python scripts/heal_key_derived_titles.py            # dry run
    uv run python scripts/heal_key_derived_titles.py --apply    # write + commit
    uv run python scripts/heal_key_derived_titles.py --db <path>

Read-only by default. Rewrites only the ``title`` column of the ``hearings`` and
``deadlines`` tables (no key change, no schema change); back up the store before
``--apply`` if you care about the DB (see AGENTS.md).
"""

from __future__ import annotations

import argparse
import sys

from case_calendar.store import Store
from case_calendar.sync import heal_key_derived_titles


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/case-calendar.sqlite")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the recomputed titles back to the DB (default: dry run)",
    )
    args = parser.parse_args(argv)

    store = Store(args.db)
    changes = heal_key_derived_titles(store, apply=args.apply)

    if not changes:
        print("no key-derived titles need cleaning — nothing to heal")
        return 0

    verb = "healed" if args.apply else "would heal"
    print(f"{verb} {len(changes)} key-derived title(s):\n")
    for c in changes:
        print(
            f"  [{c['table']}] {c['case_id']} / {c['key']}: "
            f"{c['old_title']!r} -> {c['new_title']!r}"
        )
    if not args.apply:
        print("\n(dry run — re-run with --apply to write these changes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
