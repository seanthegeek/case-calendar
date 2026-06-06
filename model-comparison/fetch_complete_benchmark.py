#!/usr/bin/env python3
"""Fetch a COMPLETE benchmark snapshot — every docket entry's full text.

The ordinary benchmark snapshot (``snapshot_benchmark_store.py``) inherits the
operational store's space-saving policy: full ``description`` + ``recap_documents``
are kept only for entries that passed the extractor's regex pre-filter
(``extractor.is_extractable``) OR matched a primary / disposition document; every
other entry is a fingerprint-only stub with no body. That makes the snapshot the
pipeline's OWN post-regex view — fine for COMPARING providers (the regex is
identical across them, so it can't change the relative ranking), but it cannot
serve as ground truth for end-to-end date-extraction accuracy: a real date hidden
in a stubbed (filter-failed) entry is invisible to both the models AND a human
reading the snapshot, so the regex stage's own recall is unmeasurable from it.

This script builds a COMPLETE store. It copies the operational store (keeping
docket metadata, courts, and every entry row), clears the model-output tables,
then re-paginates each benchmark docket's full ``docket-entries`` feed from the
v4 API (no ``modified_after`` cutoff) and overwrites EVERY entry's
``description`` / ``short_description`` / ``recap_documents`` with the complete
text — no stub-dropping. The result is a drop-in ``--source`` for
``build_provider_stores.py`` AND the text the ground-truth date-sweep reads, so
the regex stage's recall finally becomes measurable. (Background: CourtListener's
web UI is itself incomplete relative to the v4 API — see
freelawproject/courtlistener#7429 — which is exactly why ground truth must come
from the API text, not the page.)

Cost: one ``docket-entries`` pagination per docket (~60–110 requests total for
the current caseload); docket metadata is already cached, so no per-docket meta
call. One-time — the result is frozen read-only.

Usage:
    uv run python model-comparison/fetch_complete_benchmark.py \
        [--config config.yaml] [--source data/case-calendar.sqlite] \
        [--out model-comparison/snapshots/complete-benchmark-store.sqlite] \
        [--page-size 100] [--max-pages 50] [--force]

Refuses to overwrite an existing complete snapshot unless --force.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

from case_calendar.cli import _cases_from_config, _load_config  # noqa: E402
from case_calendar.courtlistener import CourtListener  # noqa: E402
from case_calendar.store import compact_recap_documents  # noqa: E402
from case_calendar.sync import fingerprint_entry  # noqa: E402

# Reuse the snapshot tooling's copy / clear / manifest helpers so the two
# snapshot artifacts are produced the same way (one source of truth for the
# online-backup copy, the model-output-table clear, and the manifest sidecar).
from snapshot_benchmark_store import (  # noqa: E402
    _backup,
    _clear_model_output_tables,
    _git_sha,
    _manifest_path,
    _remove_existing,
    _sha256,
    _store_path_from_config,
    _table_counts,
)

_DEFAULT_OUT = "model-comparison/snapshots/complete-benchmark-store.sqlite"


def _hhmmss(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def backfill_complete_text(
    out: Path,
    cases: list[Any],
    *,
    cl: Any,
    page_size: int,
    max_pages: int,
) -> dict[str, Any]:
    """Re-fetch every benchmark docket's full entries feed and overwrite each
    entry's body in ``out``. ``cl`` is an open CourtListener-shaped client (a
    real one in production, a fake in tests).

    Emits one per-case progress line with a rolling ETA (the AGENTS.md
    long-running-operation rule): ``[X/N] slug (title) — ETA HH:MM:SS ...``.
    """
    conn = sqlite3.connect(str(out))
    conn.row_factory = sqlite3.Row
    now_iso = datetime.now(timezone.utc).isoformat()
    updated = inserted = 0
    per_docket: dict[str, dict[str, int]] = {}
    n = len(cases)
    t_first: Optional[float] = None

    for i, case in enumerate(cases, 1):
        t = time.monotonic()
        if t_first is None:
            t_first = t
            print(
                f"[{i}/{n}] {case.case_id} ({case.name}) — "
                "first case starting, ETA pending",
                flush=True,
            )
        else:
            avg = (t - t_first) / (i - 1)
            remaining = n - i + 1  # include the case just starting
            print(
                f"[{i}/{n}] {case.case_id} ({case.name}) — "
                f"ETA {_hhmmss(avg * remaining)} to finish (avg {avg:.0f}s/case)",
                flush=True,
            )
        for did in case.dockets:
            stored = conn.execute(
                "SELECT COUNT(*) FROM entries WHERE docket_id=?", (did,)
            ).fetchone()[0]
            fetched = 0
            for entry in cl.iter_entries(
                did, modified_after=None, page_size=page_size, max_pages=max_pages
            ):
                eid = entry.get("id")
                if eid is None:
                    continue
                desc = (entry.get("description") or "") or None
                short = (entry.get("short_description") or "") or None
                rds = json.dumps(compact_recap_documents(entry))
                en = entry.get("entry_number")
                df = entry.get("date_filed")
                dm = entry.get("date_modified") or ""
                cur = conn.execute(
                    "UPDATE entries SET description=?, short_description=?, "
                    "recap_documents=?, entry_number=COALESCE(?, entry_number), "
                    "date_filed=COALESCE(?, date_filed), "
                    "date_modified=COALESCE(NULLIF(?, ''), date_modified) "
                    "WHERE docket_id=? AND entry_id=?",
                    (desc, short, rds, en, df, dm, did, eid),
                )
                if cur.rowcount == 0:
                    # An entry that landed upstream after the store copy was
                    # taken — insert it complete (fingerprint kept consistent
                    # with a real synced row, though replay doesn't read it).
                    conn.execute(
                        "INSERT INTO entries (docket_id, entry_id, entry_number, "
                        "date_filed, date_modified, fingerprint, description, "
                        "short_description, recap_documents, processed_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            did,
                            eid,
                            en,
                            df,
                            dm or now_iso,
                            fingerprint_entry(entry),
                            desc,
                            short,
                            rds,
                            now_iso,
                        ),
                    )
                    inserted += 1
                else:
                    updated += 1
                fetched += 1
            per_docket[str(did)] = {"fetched": fetched, "stored_before": stored}
            if fetched < stored:
                # No silent caps: surface any docket where the single-pass fetch
                # returned fewer entries than the store already held (max_pages
                # truncation, or upstream deletions).
                print(
                    f"  WARNING docket {did}: fetched {fetched} < {stored} stored "
                    "(possible max_pages truncation or upstream deletions)",
                    flush=True,
                )
        conn.commit()

    conn.commit()
    conn.close()
    return {
        "updated": updated,
        "inserted": inserted,
        "per_docket": per_docket,
        "courtlistener_requests": getattr(cl, "_request_total", None),
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument(
        "--source",
        default=None,
        help="store to copy; default: store_path from --config",
    )
    ap.add_argument("--out", default=_DEFAULT_OUT)
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--max-pages", type=int, default=50)
    ap.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing complete snapshot",
    )
    args = ap.parse_args(argv)

    source = Path(args.source or _store_path_from_config(args.config))
    if not source.exists():
        raise SystemExit(f"source store not found: {source}")

    out = Path(args.out)
    manifest_path = _manifest_path(out)
    if out.exists() and not args.force:
        raise SystemExit(
            f"{out} already exists — pass --force to rebuild the complete snapshot"
        )
    _remove_existing(out, manifest_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    _backup(source, out)
    cleared = _clear_model_output_tables(out)

    cfg = _load_config(args.config)
    cases = _cases_from_config(cfg)

    print(
        f"fetching complete entry text for {len(cases)} cases from CourtListener...",
        flush=True,
    )
    with CourtListener() as cl:
        result = backfill_complete_text(
            out, cases, cl=cl, page_size=args.page_size, max_pages=args.max_pages
        )

    counts = _table_counts(out)
    ro = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    with_body = ro.execute(
        "SELECT COUNT(*) FROM entries WHERE description IS NOT NULL "
        "AND length(description) > 0"
    ).fetchone()[0]
    ro.close()

    manifest: dict[str, Any] = {
        "snapshot_utc": datetime.now(timezone.utc).isoformat(),
        "kind": "complete-benchmark-store",
        "source_store": str(source),
        "config": args.config,
        "case_ids": [c.case_id for c in cases],
        "snapshot_file": out.name,
        "snapshot_bytes": out.stat().st_size,
        "snapshot_sha256": _sha256(out),
        "model_output_tables_cleared": cleared,
        "row_counts": counts,
        "entries_with_body": with_body,
        "entries_updated": result["updated"],
        "entries_inserted": result["inserted"],
        "courtlistener_requests": result["courtlistener_requests"],
        "per_docket_fetched": result["per_docket"],
        "ground_truth": None,
        "ground_truth_sha256": None,
        "code_git_sha": _git_sha(),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    out.chmod(0o444)

    print(
        f"DONE complete snapshot {out} ({out.stat().st_size / 1_000_000:.1f} MB)",
        flush=True,
    )
    print(
        f"  entries={counts.get('entries', '?')} with_body={with_body} "
        f"updated={result['updated']} inserted={result['inserted']} "
        f"CL_requests={result['courtlistener_requests']}",
        flush=True,
    )
    print(f"  manifest: {manifest_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
