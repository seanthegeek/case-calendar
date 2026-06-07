#!/usr/bin/env python3
"""Build the model-comparison benchmark snapshot — every docket entry's full text.

The harness (``build_provider_stores.py``) replays the LLM pipeline over a store's
cached ``entries`` — NOT live CourtListener. Pinning it to a frozen snapshot
(``--source <snapshot> --frozen``) keeps a model/prompt benchmark reproducible
while the live cases keep moving, and keeps the human ``ground_truth.csv`` (filled
at one point in time) matched to the data the models extracted from.

The snapshot carries EVERY entry's COMPLETE text. The operational store keeps full
``description`` / ``recap_documents`` only for entries that passed the extractor's
regex pre-filter (``extractor.is_extractable``) OR matched a primary / disposition
document; every other entry is a fingerprint-only stub. A stub would hide a real
date from BOTH the models AND a human reading the snapshot, making the regex
stage's own recall unmeasurable. So this script copies the operational store
(docket metadata, courts, every entry row), clears the model-output tables, then
re-paginates each benchmark docket's full ``docket-entries`` feed from the v4 API
(no ``modified_after`` cutoff) and overwrites EVERY entry's ``description`` /
``short_description`` / ``recap_documents`` with the complete text — no
stub-dropping. The result is what ``build_scoring_page.py`` reads (so a
regex-dropped entry that actually schedules a hearing is visible and gets a human
count no model could) and a drop-in ``--source`` for the harness. (Background:
CourtListener's web UI is itself incomplete relative to the v4 API —
freelawproject/courtlistener#7429 — which is why ground truth must come from the
API text, not the page.)

The model-output tables (hearings / deadlines / case_summaries — what the harness
rebuilds per column) are cleared so the shared file is input-only: smaller, and it
can't be opened to peek at what a model produced, which keeps the blind
ground-truth scoring honest. The ``.sqlite`` is committed via Git LFS (see
``.gitattributes``; fetch with ``git lfs pull``); the sibling
``benchmark-store.manifest.json`` records the snapshot date, source, caseload, row
counts, and the file's sha256.

Cost: one ``docket-entries`` pagination per docket (~60–110 requests for the
current caseload); docket metadata is already cached. One-time — result is frozen
read-only.

Usage:
    # full (re)build:
    uv run python model-comparison/snapshot_benchmark.py \
        [--config config.yaml] [--source data/case-calendar.sqlite] \
        [--out model-comparison/snapshots/benchmark-store.sqlite] \
        [--page-size 100] [--max-pages 50] [--force]
    # surgical: refresh ONLY one case in the existing snapshot:
    uv run python model-comparison/snapshot_benchmark.py --case us-v-ding

Refuses to overwrite an existing snapshot unless --force.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

load_dotenv()

from case_calendar.cli import _cases_from_config, _load_config  # noqa: E402
from case_calendar.courtlistener import CourtListener  # noqa: E402
from case_calendar.store import compact_recap_documents  # noqa: E402
from case_calendar.sync import fingerprint_entry  # noqa: E402

_DEFAULT_OUT = "model-comparison/snapshots/benchmark-store.sqlite"
_DEFAULT_STORE = "data/case-calendar.sqlite"

# Tables the benchmark REBUILDS per column from the inputs. Cleared from the
# shared snapshot for size AND so the file can't be opened to peek at what a
# model produced (which would defeat the blind ground-truth scoring). Mirrors
# build_provider_stores.DERIVED_TABLES; the snapshot carries only the
# CourtListener-fetched INPUTS (entries / dockets / courts) the benchmark
# replays from.
_MODEL_OUTPUT_TABLES = ("hearings", "deadlines", "case_summaries")


def _store_path_from_config(config_path: str) -> str:
    """The ``store_path`` the harness would read, so the default source matches
    what an un-pinned build uses."""
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return _DEFAULT_STORE
    return cfg.get("store_path", _DEFAULT_STORE)


def _backup(src: Path, dst: Path) -> None:
    """Consistent single-file copy via SQLite's online-backup API. Reads ``src``
    READ-ONLY (never write-locks the source) and produces a clean, self-contained
    ``dst`` with no ``-wal`` / ``-shm`` sidecars."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(str(dst))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def _clear_model_output_tables(path: Path) -> list[str]:
    """Empty the model-output tables in the snapshot (the benchmark rebuilds
    them) and VACUUM, so the shared file is input-only. Returns tables cleared."""
    conn = sqlite3.connect(str(path))
    cleared: list[str] = []
    try:
        existing = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for t in _MODEL_OUTPUT_TABLES:
            if t in existing:
                conn.execute(f'DELETE FROM "{t}"')
                cleared.append(t)
        conn.commit()
        conn.execute("VACUUM")  # reclaim the space the deleted rows held
    finally:
        conn.close()
    return cleared


def _table_counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables
        }
    finally:
        conn.close()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_sha() -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except Exception:
        return None


def _remove_existing(out: Path, manifest: Path) -> None:
    """Drop a prior (read-only) snapshot + sidecars + manifest so --force can
    re-snapshot. Each is chmod'd writable first since the snapshot is 0o444."""
    for p in (out, Path(f"{out}-wal"), Path(f"{out}-shm"), manifest):
        if p.exists():
            p.chmod(0o644)
            p.unlink()


def _manifest_path(out: Path) -> Path:
    # benchmark-store.sqlite -> benchmark-store.manifest.json (NOT *.sqlite*, so
    # the gitignore for the SQLite file doesn't also hide the committed manifest).
    return out.with_suffix(".manifest.json")


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
        help="overwrite an existing snapshot",
    )
    ap.add_argument(
        "--case",
        action="append",
        help="IN-PLACE update mode: re-fetch ONLY this case id into the EXISTING "
        "snapshot, leaving every other case's frozen text untouched (repeatable). "
        "Requires the snapshot to already exist — use it to pick up a new filing "
        "on one docket without re-pulling the whole benchmark.",
    )
    args = ap.parse_args(argv)

    source = Path(args.source or _store_path_from_config(args.config))
    out = Path(args.out)
    manifest_path = _manifest_path(out)
    cfg = _load_config(args.config)
    all_cases = _cases_from_config(cfg)

    if args.case:
        # In-place surgical update: re-fetch ONLY the named case(s) into the
        # existing snapshot, leaving every other case's frozen text untouched —
        # to pick up a new filing on one docket without re-pulling (and risking
        # drift on) the whole benchmark.
        if not out.exists():
            raise SystemExit(
                f"{out} not found — run a full fetch (no --case) first; --case "
                "updates individual cases in an existing snapshot."
            )
        want = set(args.case)
        cases = [c for c in all_cases if c.case_id in want]
        missing = want - {c.case_id for c in cases}
        if missing:
            raise SystemExit(f"no such case(s) in {args.config}: {sorted(missing)}")
        out.chmod(0o644)  # committed snapshot is read-only; make writable to update
        cleared = []
        partial = [c.case_id for c in cases]
        print(
            f"in-place update of {len(cases)} case(s) in {out}: {', '.join(partial)}",
            flush=True,
        )
    else:
        if not source.exists():
            raise SystemExit(f"source store not found: {source}")
        if out.exists() and not args.force:
            raise SystemExit(
                f"{out} already exists — pass --force to rebuild the snapshot"
            )
        _remove_existing(out, manifest_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        _backup(source, out)
        cleared = _clear_model_output_tables(out)
        cases = all_cases
        partial = None
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
        "kind": "benchmark-store",
        "source_store": str(source),
        "config": args.config,
        "case_ids": [c.case_id for c in all_cases],
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
        "partial_update_cases": partial,
        "ground_truth": None,
        "ground_truth_sha256": None,
        "code_git_sha": _git_sha(),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    out.chmod(0o444)

    print(
        f"DONE snapshot {out} ({out.stat().st_size / 1_000_000:.1f} MB)",
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
