"""Canonicalize hearing AND deadline keys left as ``base-<digits>`` key drift.

When CourtListener splits one logical PACER docket across several docket_id
rows (the pacer_case_id reconciler, bug #7345), the extractor used to mint a
fresh ``-N`` key for an event the sibling record already had. For hearings the
end-of-sync dedupe sweeps collapse the duplicate but historically kept the
``-N`` row as the survivor, so the key-derived title fallback renders it as
"Sentencing Lytvynenko 2"; for deadlines there is no dedupe sweep at all, so the
``transcript-release-...-12-17`` / ``...-12-17-2`` pair simply both survive. The
code fix (``sync.py`` / ``llm.py``) stops new drift and makes future hearing
merges keep the canonical key, but rows already in the store stay — re-running
sync can't re-cluster them.

This sweep repairs both tables deterministically (no LLM, no CourtListener) by
PROVABLE-drift signals only, so a MEANINGFUL trailing number (sequential status
conferences, trial days, distinct same-day deadlines) is never touched:

- rename: the row's audit_notes record it absorbed its own suffix-free base, and
  that base is no longer a row — rename ``base-N`` → ``base``.
- delete: the suffix-free ``base`` still exists at the same slot in the same
  logical PACER group — fold the ``-N`` row's sources onto ``base`` and delete it.

For deadlines this is NOT the forbidden deterministic same-slot deadline merge
(see the "Deadlines deliberately have NO same-slot dedupe sweep" design note in
AGENTS.md): it only collapses a literal ``base`` / ``base-N`` pair coexisting at
the same ``due_at_utc`` in one PACER group. Two genuinely distinct deadlines on
the same date have descriptive, different keys (not a base/base-N pairing) and
are never touched.

Usage:
    uv run python scripts/heal_drifted_keys.py            # dry run
    uv run python scripts/heal_drifted_keys.py --apply    # write + commit
    uv run python scripts/heal_drifted_keys.py --db <path>

Read-only by default. Renaming a key changes its ICS UID and gcal/M365 event id,
so subscribers' clients re-create those events once and the old-key gcal/Graph
events orphan — a one-time, accepted cost. Back up the store before ``--apply``
if you care about the DB (see AGENTS.md); this rewrites only the ``hearings`` and
``deadlines`` table rows (no schema change).
"""

from __future__ import annotations

import argparse
import sys

from case_calendar.store import Store
from case_calendar.sync import heal_drifted_deadline_keys, heal_drifted_keys


def _report(kind: str, key_field: str, changes: list[dict], apply: bool) -> None:
    if not changes:
        print(f"no drifted {kind} keys found — nothing to heal")
        return
    verb = "healed" if apply else "would heal"
    print(f"{verb} {len(changes)} drifted {kind} key(s):\n")
    for c in changes:
        suffix = "rename" if c["action"] == "rename" else "delete dup, fold sources"
        print(f"  {c['case_id']} / {c[key_field]} -> {c['new_key']}  ({suffix})")
        if c.get("old_title") != c.get("new_title"):
            print(f"      title: {c.get('old_title')!r} -> {c.get('new_title')!r}")
    print()


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
    hearing_changes = heal_drifted_keys(store, apply=args.apply)
    deadline_changes = heal_drifted_deadline_keys(store, apply=args.apply)

    _report("hearing", "hearing_key", hearing_changes, args.apply)
    _report("deadline", "deadline_key", deadline_changes, args.apply)

    if not (hearing_changes or deadline_changes):
        return 0
    if not args.apply:
        print("(dry run — re-run with --apply to write these changes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
