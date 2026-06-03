"""Tests for model-comparison/snapshot_benchmark_store.py — the tool that
freezes the benchmark INPUT store into an immutable, dated snapshot so a
model/prompt comparison stays reproducible while the live cases keep moving.

The script lives outside the ``case_calendar`` package, so it's loaded by path.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "model-comparison"
    / "snapshot_benchmark_store.py"
)
_spec = importlib.util.spec_from_file_location("snapshot_benchmark_store", _SCRIPT)
assert _spec and _spec.loader
snap = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = snap
_spec.loader.exec_module(snap)


def _make_store(path: Path, *, dockets: int = 2, entries: int = 5) -> None:
    """A minimal store-shaped SQLite with a couple of countable tables. Starts
    fresh each call so a test can re-make the source with different counts."""
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE dockets (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE entries (entry_id INTEGER PRIMARY KEY)")
    # A model-output table that the snapshot must CLEAR (so a shared snapshot
    # can't be opened to peek at model output, and the blind scoring holds).
    conn.execute("CREATE TABLE hearings (id INTEGER PRIMARY KEY)")
    conn.executemany(
        "INSERT INTO dockets (id) VALUES (?)", [(i,) for i in range(dockets)]
    )
    conn.executemany(
        "INSERT INTO entries (entry_id) VALUES (?)", [(i,) for i in range(entries)]
    )
    conn.executemany("INSERT INTO hearings (id) VALUES (?)", [(i,) for i in range(4)])
    conn.commit()
    conn.close()


def _args(src: Path, out: Path, gt: Path, *, force: bool = False) -> list[str]:
    a = ["--source", str(src), "--out", str(out), "--ground-truth", str(gt)]
    if force:
        a.append("--force")
    return a


def test_creates_readonly_snapshot_and_manifest(tmp_path):
    src = tmp_path / "prod.sqlite"
    _make_store(src, dockets=3, entries=7)
    gt = tmp_path / "ground_truth.csv"
    gt.write_text("docket_id,h_sched\n1,2\n")
    out = tmp_path / "snapshots" / "benchmark-store.sqlite"

    assert snap.main(_args(src, out, gt)) == 0

    # The snapshot exists, holds the same rows, and is read-only.
    assert out.exists()
    assert not os.access(out, os.W_OK), "snapshot must be read-only"
    conn = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 7
    conn.close()

    # The manifest is a SIBLING .manifest.json (committed; not gitignored as a
    # *.sqlite*), and records counts + both checksums.
    manifest = out.with_suffix(".manifest.json")
    assert manifest.exists()
    m = json.loads(manifest.read_text())
    assert m["row_counts"]["entries"] == 7
    assert m["row_counts"]["dockets"] == 3
    # Inputs preserved, model output CLEARED so the shared snapshot isn't
    # peekable and the blind ground-truth scoring stays honest.
    assert m["row_counts"]["hearings"] == 0
    assert m["model_output_tables_cleared"] == ["hearings"]
    assert m["snapshot_sha256"] and m["ground_truth_sha256"]
    assert m["ground_truth"] == str(gt)
    assert "snapshot_utc" in m

    # And the actual snapshot DB has no model-output rows to peek at.
    conn2 = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    assert conn2.execute("SELECT COUNT(*) FROM hearings").fetchone()[0] == 0
    assert conn2.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 7
    conn2.close()


def test_refuses_overwrite_without_force(tmp_path):
    src = tmp_path / "prod.sqlite"
    _make_store(src)
    gt = tmp_path / "gt.csv"
    gt.write_text("x\n")
    out = tmp_path / "snap.sqlite"
    assert snap.main(_args(src, out, gt)) == 0

    # A second run must NOT clobber the frozen baseline.
    with pytest.raises(SystemExit, match="already exists"):
        snap.main(_args(src, out, gt))

    # ...unless --force is given (and it overwrites the read-only file).
    _make_store(src, entries=9)  # change the source
    assert snap.main(_args(src, out, gt, force=True)) == 0
    conn = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 9
    conn.close()


def test_missing_source_raises(tmp_path):
    out = tmp_path / "snap.sqlite"
    gt = tmp_path / "gt.csv"
    with pytest.raises(SystemExit, match="source store not found"):
        snap.main(_args(tmp_path / "nope.sqlite", out, gt))


def test_missing_ground_truth_records_no_pairing(tmp_path, capsys):
    src = tmp_path / "prod.sqlite"
    _make_store(src)
    out = tmp_path / "snap.sqlite"
    gt = tmp_path / "absent.csv"  # does not exist
    assert snap.main(_args(src, out, gt)) == 0
    m = json.loads(out.with_suffix(".manifest.json").read_text())
    assert m["ground_truth_sha256"] is None
    assert m["ground_truth"] is None
    assert "ground truth not found" in capsys.readouterr().out


def test_source_defaults_to_config_store_path(tmp_path, monkeypatch):
    # With no --source, the tool reads store_path from the config so the frozen
    # snapshot matches what an un-pinned build would have read.
    src = tmp_path / "configured.sqlite"
    _make_store(src, entries=4)
    config = tmp_path / "config.yaml"
    config.write_text(f"store_path: {src}\ncases: []\n")
    out = tmp_path / "snap.sqlite"
    gt = tmp_path / "gt.csv"
    gt.write_text("x\n")
    assert (
        snap.main(
            ["--config", str(config), "--out", str(out), "--ground-truth", str(gt)]
        )
        == 0
    )
    m = json.loads(out.with_suffix(".manifest.json").read_text())
    assert m["row_counts"]["entries"] == 4
    assert m["source_store"] == str(src)
