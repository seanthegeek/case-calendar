#!/usr/bin/env python3
"""Freeze the model-comparison INPUT store into an immutable, dated snapshot.

The harness (``build_provider_stores.py``) replays the LLM pipeline over the
local store's cached ``entries`` — NOT live CourtListener. But by default it
reads the LIVE prod store (``store_path``), which ``case-calendar sync`` mutates
as the active cases move. That means a model/prompt benchmark run today isn't
comparable to one last week, and the human ground-truth worksheet (filled at one
point in time) drifts away from the data the models actually extracted from.

This script copies the current store into a READ-ONLY, dated SNAPSHOT that the
harness pins to with ``build_provider_stores.py --source <snapshot> --frozen``.
Paired with the already-committed ``ground_truth.csv``, the snapshot freezes BOTH
halves of the benchmark — the data the models extract from AND the truth they're
scored against — so you can test any number of new models or prompts against an
UNCHANGING baseline, even while the real dockets keep moving. ``--frozen`` on the
build makes any attempt to reach live CourtListener / download a PDF a hard
error, so a frozen run provably uses only the snapshot's data.

The snapshot is INPUT-ONLY: the model-output tables (hearings / deadlines /
case_summaries — what the harness rebuilds per column) are cleared, so the file
is smaller AND can't be opened to peek at what a model produced, which keeps the
blind ground-truth scoring honest. That makes it safe to SHARE — others can
reproduce the comparison, or test their own models against the IDENTICAL inputs.
The ``.sqlite`` is committed via Git LFS (see ``.gitattributes``; fetch with
``git lfs pull``), so it ships with the repo; the sibling ``<name>.manifest.json``
is a normal committed file recording the snapshot date, source, caseload, row
counts, the file's sha256, and the paired ground-truth file's sha256, so anyone
can verify they hold the snapshot a SCORECARD was produced against.

Usage:
    uv run python model-comparison/snapshot_benchmark_store.py \
        [--config config.yaml] [--source data/case-calendar.sqlite] \
        [--out model-comparison/snapshots/benchmark-store.sqlite] \
        [--ground-truth model-comparison/ground_truth.csv] [--force]

Refuses to overwrite an existing snapshot (so you can't clobber a frozen
baseline); pass --force to deliberately re-snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

load_dotenv()

_DEFAULT_OUT = "model-comparison/snapshots/benchmark-store.sqlite"
_DEFAULT_GROUND_TRUTH = "model-comparison/ground_truth.csv"
_DEFAULT_STORE = "data/case-calendar.sqlite"

# Tables the benchmark REBUILDS per column from the inputs. They're cleared from
# the shared snapshot for two reasons: size, and — more importantly — leaving
# the current model's output in a shared file would let anyone open it and SEE
# what a model produced, defeating the BLIND ground-truth scoring. Mirrors
# ``build_provider_stores.DERIVED_TABLES``; the snapshot carries only the
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


def _case_ids(config_path: str) -> list[str]:
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return []
    return [c["id"] for c in cfg.get("cases", []) if isinstance(c, dict) and "id" in c]


def _backup(src: Path, dst: Path) -> None:
    """Consistent single-file copy via SQLite's online-backup API.

    Reads ``src`` READ-ONLY (never write-locks prod) and produces a clean,
    self-contained ``dst`` with no ``-wal`` / ``-shm`` sidecars — so the snapshot
    is one file, safe to checksum and mark read-only.
    """
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
    them) and VACUUM, so the shared file is input-only — smaller, and it can't
    be opened to peek at what a model produced. Returns the tables cleared."""
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
    # the gitignore for the SQLite file doesn't also hide the committed
    # manifest).
    return out.with_suffix(".manifest.json")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument(
        "--source",
        default=None,
        help="store to freeze; default: store_path from --config "
        f"(else {_DEFAULT_STORE})",
    )
    ap.add_argument("--out", default=_DEFAULT_OUT)
    ap.add_argument("--ground-truth", default=_DEFAULT_GROUND_TRUTH)
    ap.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing snapshot (otherwise refuses, to protect a "
        "frozen baseline)",
    )
    args = ap.parse_args(argv)

    source = Path(args.source or _store_path_from_config(args.config))
    if not source.exists():
        raise SystemExit(f"source store not found: {source}")

    out = Path(args.out)
    manifest_path = _manifest_path(out)
    if out.exists() and not args.force:
        raise SystemExit(
            f"snapshot already exists: {out} — pass --force to re-snapshot "
            "(this protects a frozen benchmark baseline from being clobbered)"
        )
    _remove_existing(out, manifest_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    _backup(source, out)
    cleared = _clear_model_output_tables(out)

    counts = _table_counts(out)
    gt = Path(args.ground_truth)
    gt_sha = _sha256(gt) if gt.exists() else None

    manifest: dict[str, Any] = {
        "snapshot_utc": datetime.now(timezone.utc).isoformat(),
        "source_store": str(source),
        "config": args.config,
        "case_ids": _case_ids(args.config),
        "snapshot_file": out.name,
        "snapshot_bytes": out.stat().st_size,
        "snapshot_sha256": _sha256(out),
        "model_output_tables_cleared": cleared,
        "row_counts": counts,
        "ground_truth": str(gt) if gt_sha else None,
        "ground_truth_sha256": gt_sha,
        "code_git_sha": _git_sha(),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    # Read-only LAST, so the manifest write above and any --force cleanup didn't
    # have to fight the permission bit.
    out.chmod(0o444)

    mb = out.stat().st_size / 1_000_000
    print(f"froze {source} -> {out} ({mb:.1f} MB, read-only, input-only)")
    print(
        f"  dockets={counts.get('dockets', '?')} entries={counts.get('entries', '?')} "
        f"courts={counts.get('courts', '?')}  (cleared: {', '.join(cleared) or 'none'})"
    )
    print(f"  manifest: {manifest_path}")
    if gt_sha is None:
        print(
            f"  WARNING: ground truth not found at {gt} — manifest records no pairing"
        )
    print(
        "\nBenchmark against it (no live data, reproducible):\n"
        f"  build_provider_stores.py --source {out} --frozen ..."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
