"""SQLite-backed state store.

Keeps:

* ``dockets`` — per-docket high-water mark for the docket-level short-circuit
  plus cached metadata (court_id, case_name, docket_number, absolute_url) for
  description rendering at emit time.
* ``courts`` — citation_string + name lookup per court_id, fetched once.
* ``entries`` — every docket entry we've already processed, with a content
  fingerprint. Used for dedup and the docket-level high-water mark.
* ``hearings`` — the canonical "logical" hearings per case. Each hearing has a
  stable ``hearing_key`` (chosen by the LLM) so reschedules and dial-in info
  updates land on the same row.
* ``webhook_events`` — Idempotency-Key dedup for the webhook receiver.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS dockets (
    docket_id INTEGER PRIMARY KEY,
    date_modified TEXT,
    last_synced_at TEXT NOT NULL,
    court_id TEXT,
    docket_number TEXT,
    case_name TEXT,
    absolute_url TEXT
);

CREATE TABLE IF NOT EXISTS courts (
    court_id TEXT PRIMARY KEY,
    citation_string TEXT,
    short_name TEXT,
    full_name TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    docket_id INTEGER NOT NULL,
    entry_id INTEGER NOT NULL,
    entry_number INTEGER,        -- docket-position number (PACER's "[65]")
    date_filed TEXT,
    date_modified TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    description TEXT,            -- raw entry text; used to resolve cross-refs
    short_description TEXT,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (docket_id, entry_id)
);

CREATE TABLE IF NOT EXISTS hearings (
    case_id TEXT NOT NULL,
    hearing_key TEXT NOT NULL,
    title TEXT NOT NULL,
    starts_at_utc TEXT,
    duration_minutes INTEGER,
    timezone TEXT NOT NULL,
    location TEXT,
    judge TEXT,
    notes TEXT,
    dial_in TEXT,
    status TEXT NOT NULL,        -- scheduled | held | cancelled | unknown
    significance TEXT,           -- "major" (default) | "minor" — calendar filter
    gcal_event_id TEXT,
    docket_id INTEGER,           -- docket whose entry most recently updated this hearing
    source_entry_ids TEXT NOT NULL, -- JSON list of entry IDs
    last_updated TEXT NOT NULL,
    PRIMARY KEY (case_id, hearing_key)
);

CREATE TABLE IF NOT EXISTS webhook_events (
    -- CourtListener sends an Idempotency-Key (UUID) header on every webhook
    -- delivery. We store seen keys so retries are no-ops.
    idempotency_key TEXT PRIMARY KEY,
    event_type INTEGER,
    received_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False so the webhook server (ThreadingHTTPServer)
        # can use this connection from worker threads. We serialize callers
        # explicitly — see WebhookServer.process_locked.
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Idempotent ALTER TABLE for columns added after first release."""
        for table, col, type_ in [
            ("dockets", "court_id", "TEXT"),
            ("dockets", "docket_number", "TEXT"),
            ("dockets", "case_name", "TEXT"),
            ("dockets", "absolute_url", "TEXT"),
            ("hearings", "docket_id", "INTEGER"),
            ("hearings", "significance", "TEXT"),
            ("entries", "date_filed", "TEXT"),
            ("entries", "entry_number", "INTEGER"),
            ("entries", "description", "TEXT"),
            ("entries", "short_description", "TEXT"),
        ]:
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {type_}")
            except sqlite3.OperationalError:
                pass  # column already exists
        # Drop columns that older releases populated for description rendering.
        # Renderer no longer reads them; storing them was wasted IO and disk.
        for col in ("description_text", "pdf_text_excerpt"):
            try:
                self.conn.execute(f"ALTER TABLE entries DROP COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # column doesn't exist (fresh DB)
        # Index lives in _migrate (not SCHEMA) because it depends on a column
        # added by ALTER TABLE above; running it from SCHEMA on an old DB
        # would reference entry_number before the migration creates it.
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entries_docket_entry_number "
            "ON entries (docket_id, entry_number)"
        )

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # --- dockets ---

    def docket_last_modified(self, docket_id: int) -> Optional[str]:
        row = self.conn.execute(
            "SELECT date_modified FROM dockets WHERE docket_id=?",
            (docket_id,),
        ).fetchone()
        return row["date_modified"] if row else None

    def set_docket_last_modified(self, docket_id: int, date_modified: str) -> None:
        # Preserves any metadata columns set by upsert_docket_meta.
        self.conn.execute(
            """
            INSERT INTO dockets (docket_id, date_modified, last_synced_at)
            VALUES (?, ?, ?)
            ON CONFLICT(docket_id) DO UPDATE SET
              date_modified=excluded.date_modified,
              last_synced_at=excluded.last_synced_at
            """,
            (docket_id, date_modified, _now()),
        )

    def upsert_docket_meta(self, docket_id: int, meta: dict[str, Any]) -> None:
        """Cache the human-readable docket metadata we display in event bodies."""
        self.conn.execute(
            """
            INSERT INTO dockets
              (docket_id, last_synced_at, court_id, docket_number, case_name, absolute_url)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(docket_id) DO UPDATE SET
              court_id=excluded.court_id,
              docket_number=excluded.docket_number,
              case_name=excluded.case_name,
              absolute_url=excluded.absolute_url
            """,
            (
                docket_id,
                _now(),
                meta.get("court_id"),
                meta.get("docket_number"),
                meta.get("case_name"),
                meta.get("absolute_url"),
            ),
        )

    def get_docket_meta(self, docket_id: int) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT court_id, docket_number, case_name, absolute_url "
            "FROM dockets WHERE docket_id=?",
            (docket_id,),
        ).fetchone()
        return dict(row) if row else None

    # --- courts ---

    def get_court_citation(self, court_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT citation_string FROM courts WHERE court_id=?",
            (court_id,),
        ).fetchone()
        return row["citation_string"] if row else None

    def upsert_court(
        self,
        court_id: str,
        citation_string: Optional[str],
        short_name: Optional[str],
        full_name: Optional[str],
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO courts "
            "(court_id, citation_string, short_name, full_name, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (court_id, citation_string, short_name, full_name, _now()),
        )

    # --- entries ---

    def entry_seen(self, docket_id: int, entry_id: int, fingerprint: str) -> bool:
        row = self.conn.execute(
            "SELECT fingerprint FROM entries WHERE docket_id=? AND entry_id=?",
            (docket_id, entry_id),
        ).fetchone()
        return row is not None and row["fingerprint"] == fingerprint

    def mark_entry(
        self,
        docket_id: int,
        entry_id: int,
        date_modified: str,
        fingerprint: str,
        *,
        date_filed: Optional[str] = None,
        entry_number: Optional[int] = None,
        description: Optional[str] = None,
        short_description: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO entries
              (docket_id, entry_id, entry_number, date_filed, date_modified,
               fingerprint, description, short_description, processed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(docket_id, entry_id) DO UPDATE SET
              entry_number=COALESCE(excluded.entry_number, entries.entry_number),
              date_filed=COALESCE(excluded.date_filed, entries.date_filed),
              date_modified=excluded.date_modified,
              fingerprint=excluded.fingerprint,
              description=COALESCE(excluded.description, entries.description),
              short_description=COALESCE(
                  excluded.short_description, entries.short_description
              ),
              processed_at=excluded.processed_at
            """,
            (
                docket_id,
                entry_id,
                entry_number,
                date_filed,
                date_modified,
                fingerprint,
                description,
                short_description,
                _now(),
            ),
        )

    def get_entry_by_number(
        self, docket_id: int, entry_number: int
    ) -> Optional[dict[str, Any]]:
        """Look up a docket entry by its position number (PACER's "[65]").

        Returns the stored description / short_description so callers can
        resolve cross-references like "ORDER granting 65 Motion ..." without
        a CL round-trip. Older entries (pre-migration) won't have description
        stored — we return whatever is there.
        """
        row = self.conn.execute(
            "SELECT entry_id, entry_number, date_filed, description, short_description "
            "FROM entries WHERE docket_id=? AND entry_number=?",
            (docket_id, entry_number),
        ).fetchone()
        return dict(row) if row else None

    def get_recent_relevant_entries(
        self, docket_id: int, before_date_modified: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Return the last `limit` hearing-relevant entries that came before
        the given timestamp on the docket, newest first.

        "Hearing-relevant" is detected by ``description IS NOT NULL`` — we
        only persist description for entries that passed the regex pre-filter
        in the sync pipeline (filter-failed entries are stored as fingerprint
        stubs with NULL description). This gives the LLM context on what
        recently happened on the docket so it can name a hearing correctly
        even when the entry that sets it (e.g. "PAPERLESS Order Setting
        Telephonic Pretrial Conference") doesn't itself name the underlying
        motion's subject — the answer often lives in a recent motion that
        the order doesn't cite by docket position.
        """
        rows = self.conn.execute(
            """
            SELECT entry_id, entry_number, date_filed, description, short_description
            FROM entries
            WHERE docket_id=? AND date_modified < ? AND description IS NOT NULL
            ORDER BY date_modified DESC LIMIT ?
            """,
            (docket_id, before_date_modified, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_entry_modified(self, docket_id: int) -> Optional[str]:
        row = self.conn.execute(
            "SELECT MAX(date_modified) AS m FROM entries WHERE docket_id=?",
            (docket_id,),
        ).fetchone()
        return row["m"] if row and row["m"] else None

    # --- hearings ---

    def get_hearings(self, case_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM hearings WHERE case_id=?", (case_id,)
        ).fetchall()
        return [self._row_to_hearing(r) for r in rows]

    def get_hearing(self, case_id: str, hearing_key: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM hearings WHERE case_id=? AND hearing_key=?",
            (case_id, hearing_key),
        ).fetchone()
        return self._row_to_hearing(row) if row else None

    def upsert_hearing(self, h: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO hearings
            (case_id, hearing_key, title, starts_at_utc, duration_minutes, timezone,
             location, judge, notes, dial_in, status, significance, gcal_event_id,
             docket_id, source_entry_ids, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(case_id, hearing_key) DO UPDATE SET
              title=excluded.title,
              starts_at_utc=excluded.starts_at_utc,
              duration_minutes=excluded.duration_minutes,
              timezone=excluded.timezone,
              location=excluded.location,
              judge=excluded.judge,
              notes=excluded.notes,
              dial_in=excluded.dial_in,
              status=excluded.status,
              significance=COALESCE(excluded.significance, hearings.significance),
              gcal_event_id=COALESCE(excluded.gcal_event_id, hearings.gcal_event_id),
              docket_id=COALESCE(excluded.docket_id, hearings.docket_id),
              source_entry_ids=excluded.source_entry_ids,
              last_updated=excluded.last_updated
            """,
            (
                h["case_id"],
                h["hearing_key"],
                h["title"],
                h.get("starts_at_utc"),
                h.get("duration_minutes"),
                h["timezone"],
                h.get("location"),
                h.get("judge"),
                h.get("notes"),
                h.get("dial_in"),
                h["status"],
                h.get("significance"),
                h.get("gcal_event_id"),
                h.get("docket_id"),
                json.dumps(h.get("source_entry_ids", [])),
                _now(),
            ),
        )

    def set_gcal_id(self, case_id: str, hearing_key: str, gcal_event_id: str) -> None:
        self.conn.execute(
            "UPDATE hearings SET gcal_event_id=? WHERE case_id=? AND hearing_key=?",
            (gcal_event_id, case_id, hearing_key),
        )

    def all_active_hearings(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM hearings WHERE status != 'cancelled'"
        ).fetchall()
        return [self._row_to_hearing(r) for r in rows]

    @staticmethod
    def _row_to_hearing(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["source_entry_ids"] = json.loads(d.get("source_entry_ids") or "[]")
        return d

    # --- webhook idempotency ---

    def webhook_seen(self, idempotency_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM webhook_events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        return row is not None

    def mark_webhook_seen(self, idempotency_key: str, event_type: Optional[int]) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO webhook_events (idempotency_key, event_type, received_at) "
            "VALUES (?, ?, ?)",
            (idempotency_key, event_type, _now()),
        )
