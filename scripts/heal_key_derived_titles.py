"""Recompute key-derived hearing titles so single-defendant dockets drop the
redundant defendant name ("Sentencing Lytvynenko" -> "Sentencing").

A hearing the LLM never gave an explicit title to falls back to the humanized
key, which carries the defendant slug the key holds for disambiguation. On a
single-defendant docket that name is redundant noise the prompt itself says to
omit; on a genuine co-defendant docket it is needed. The write-time fallback
(``sync._fallback_hearing_title``) now applies that single-vs-multi-defendant
rule, but rows stored before it landed keep the old title — this sweep repairs
them deterministically (no LLM, no CourtListener).

Touches ONLY rows whose CURRENT title is the plain key humanization, so an
explicit LLM title is never rewritten, and a case is judged single- vs
multi-defendant from its own hearing keys, so co-defendant dockets keep their
names (rendered "<Type> - <Name>").

Usage:
    uv run python scripts/heal_key_derived_titles.py            # dry run
    uv run python scripts/heal_key_derived_titles.py --apply    # write + commit
    uv run python scripts/heal_key_derived_titles.py --db <path>

Read-only by default. Rewrites only the ``title`` column of the ``hearings``
table (no key change, no schema change); back up the store before ``--apply`` if
you care about the DB (see AGENTS.md).
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
            f"  {c['case_id']} / {c['hearing_key']}: "
            f"{c['old_title']!r} -> {c['new_title']!r}"
        )
    if not args.apply:
        print("\n(dry run — re-run with --apply to write these changes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
