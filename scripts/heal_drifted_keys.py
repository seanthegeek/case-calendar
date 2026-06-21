"""Canonicalize hearing keys left as ``base-<digits>`` key-drift artifacts.

When CourtListener splits one logical PACER docket across several docket_id
rows (the pacer_case_id reconciler, bug #7345), the extractor used to mint a
fresh ``-N`` key for an event the sibling record already had — and the
end-of-sync dedupe sweeps, which collapse the duplicate, historically kept the
``-N`` row as the survivor. The key-derived title fallback then renders that to
subscribers as "Sentencing Lytvynenko 2". The code fix (``sync.py`` /
``llm.py``) stops new drift and makes future merges keep the canonical key, but
rows already collapsed in the store stay collapsed — the suffix-free sibling was
deleted at merge time, so re-running sync can't re-cluster them.

This sweep repairs them deterministically (no LLM, no CourtListener) by two
PROVABLE-drift signals only, so a MEANINGFUL trailing number (sequential status
conferences, trial days) is never touched:

- rename: the row's audit_notes record it absorbed its own suffix-free base, and
  that base is no longer a row — rename ``base-N`` → ``base``.
- delete: the suffix-free ``base`` still exists at the same slot in the same
  logical PACER group — fold the ``-N`` row's sources onto ``base`` and delete it.

Usage:
    uv run python scripts/heal_drifted_keys.py            # dry run
    uv run python scripts/heal_drifted_keys.py --apply    # write + commit
    uv run python scripts/heal_drifted_keys.py --db <path>

Read-only by default. Renaming a key changes its ICS UID and gcal/M365 event id,
so subscribers' clients re-create those events once and the old-key gcal/Graph
events orphan — a one-time, accepted cost. Back up the store before ``--apply``
if you care about the DB (see AGENTS.md); this rewrites only the ``hearings``
table rows (no schema change).
"""

from __future__ import annotations

import argparse
import sys

from case_calendar.store import Store
from case_calendar.sync import heal_drifted_keys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/case-calendar.sqlite")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the canonicalized keys back to the DB (default: dry run)",
    )
    args = parser.parse_args(argv)

    store = Store(args.db)
    changes = heal_drifted_keys(store, apply=args.apply)

    if not changes:
        print("no drifted hearing keys found — nothing to heal")
        return 0

    verb = "healed" if args.apply else "would heal"
    print(f"{verb} {len(changes)} drifted hearing key(s):\n")
    for c in changes:
        if c["action"] == "rename":
            print(f"  {c['case_id']} / {c['hearing_key']} -> {c['new_key']}  (rename)")
        else:
            print(
                f"  {c['case_id']} / {c['hearing_key']} -> {c['new_key']}  "
                f"(delete dup, fold sources)"
            )
        if c.get("old_title") != c.get("new_title"):
            print(f"      title: {c.get('old_title')!r} -> {c.get('new_title')!r}")
    if not args.apply:
        print("\n(dry run — re-run with --apply to write these changes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
