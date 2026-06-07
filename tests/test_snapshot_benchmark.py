"""Tests for ``model-comparison/snapshot_benchmark.py`` — the benchmark snapshot
builder (full-text). Loaded by path; the inlined helpers (online-backup copy,
model-output-table clear, sha256, manifest) and ``backfill_complete_text`` (which
takes an injectable ``cl``) are tested hermetically with a fake CourtListener."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "model-comparison"
    / "snapshot_benchmark.py"
)
_spec = importlib.util.spec_from_file_location("snapshot_benchmark", _SCRIPT)
assert _spec and _spec.loader
snap = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = snap
_spec.loader.exec_module(snap)


def _make_store(path: Path, *, with_stub_entry: bool = True) -> None:
    """A minimal store-shaped SQLite: the entry columns backfill touches plus the
    model-output tables the snapshot must clear."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE entries (docket_id INTEGER, entry_id INTEGER, "
        "entry_number INTEGER, date_filed TEXT, date_modified TEXT, "
        "fingerprint TEXT, description TEXT, short_description TEXT, "
        "recap_documents TEXT, processed_at TEXT)"
    )
    conn.execute("CREATE TABLE dockets (docket_id INTEGER)")
    for t in ("hearings", "deadlines", "case_summaries"):
        conn.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY)")
        conn.executemany(f"INSERT INTO {t} (id) VALUES (?)", [(i,) for i in range(3)])
    if with_stub_entry:
        # a fingerprint-only stub (no body) that backfill should OVERWRITE.
        conn.execute(
            "INSERT INTO entries (docket_id, entry_id, fingerprint) VALUES (1, 100, 'old')"
        )
    conn.commit()
    conn.close()


class _FakeCase:
    def __init__(self, case_id: str, name: str, dockets: list[int]) -> None:
        self.case_id = case_id
        self.name = name
        self.dockets = dockets


class _FakeCL:
    def __init__(self, entries_by_docket: dict[int, list[dict]]) -> None:
        self._e = entries_by_docket
        self._request_total = 7

    def iter_entries(self, did, *, modified_after=None, page_size=100, max_pages=50):
        yield from self._e.get(did, [])


def _entry(eid, desc, num, *, df="2026-01-01", dm="2026-01-02"):
    return {
        "id": eid,
        "description": desc,
        "short_description": "",
        "entry_number": num,
        "date_filed": df,
        "date_modified": dm,
        "recap_documents": [],
    }


# --------------------------------------------------------------------------- #
# backfill_complete_text
# --------------------------------------------------------------------------- #


def test_backfill_overwrites_stub_and_inserts_new(tmp_path):
    store = tmp_path / "s.sqlite"
    _make_store(store)
    cl = _FakeCL(
        {
            1: [
                _entry(100, "FULL TEXT of entry 100", 5),  # overwrites the stub
                _entry(200, "new entry 200", 6),  # not in store -> insert
            ]
        }
    )
    res = snap.backfill_complete_text(
        store, [_FakeCase("c", "Case C", [1])], cl=cl, page_size=100, max_pages=50
    )
    assert res["updated"] == 1
    assert res["inserted"] == 1
    assert res["courtlistener_requests"] == 7

    conn = sqlite3.connect(store)
    rows = {
        r[0]: r[1] for r in conn.execute("SELECT entry_id, description FROM entries")
    }
    assert rows[100] == "FULL TEXT of entry 100"  # stub body filled in
    assert rows[200] == "new entry 200"  # new entry inserted complete


def test_backfill_emits_per_case_progress_line(tmp_path, capsys):
    store = tmp_path / "s.sqlite"
    _make_store(store, with_stub_entry=False)
    cl = _FakeCL({1: [_entry(1, "x", 1)]})
    snap.backfill_complete_text(
        store,
        [_FakeCase("us-v-ding", "United States v. Ding", [1])],
        cl=cl,
        page_size=100,
        max_pages=50,
    )
    out = capsys.readouterr().out
    assert "[1/1] us-v-ding (United States v. Ding)" in out
    assert "ETA pending" in out  # the first case has no ETA yet


def test_backfill_warns_when_fetched_fewer_than_stored(tmp_path, capsys):
    store = tmp_path / "s.sqlite"
    _make_store(store)  # 1 stored entry on docket 1
    cl = _FakeCL({1: []})  # upstream returns nothing
    snap.backfill_complete_text(
        store, [_FakeCase("c", "C", [1])], cl=cl, page_size=100, max_pages=50
    )
    assert "WARNING docket 1: fetched 0 < 1 stored" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# inlined helpers
# --------------------------------------------------------------------------- #


def test_clear_model_output_tables_empties_outputs_keeps_inputs(tmp_path):
    p = tmp_path / "s.sqlite"
    _make_store(p)
    cleared = snap._clear_model_output_tables(p)
    assert set(cleared) == {"hearings", "deadlines", "case_summaries"}
    conn = sqlite3.connect(p)
    for t in ("hearings", "deadlines", "case_summaries"):
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1  # input kept


def test_table_counts_and_sha256(tmp_path):
    p = tmp_path / "s.sqlite"
    _make_store(p)
    counts = snap._table_counts(p)
    assert counts["entries"] == 1
    assert counts["hearings"] == 3
    assert len(snap._sha256(p)) == 64


def test_manifest_path_sibling_json():
    assert (
        snap._manifest_path(Path("x/benchmark-store.sqlite")).name
        == "benchmark-store.manifest.json"
    )


def test_store_path_from_config(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("store_path: /tmp/foo.sqlite\n")
    assert snap._store_path_from_config(str(cfg)) == "/tmp/foo.sqlite"
    # missing config falls back to the default store path
    assert (
        snap._store_path_from_config(str(tmp_path / "missing.yaml"))
        == "data/case-calendar.sqlite"
    )


def test_remove_existing_drops_readonly_snapshot_and_manifest(tmp_path):
    out = tmp_path / "benchmark-store.sqlite"
    man = snap._manifest_path(out)
    out.write_text("db")
    man.write_text("{}")
    out.chmod(0o444)  # snapshots are committed read-only
    snap._remove_existing(out, man)
    assert not out.exists() and not man.exists()
