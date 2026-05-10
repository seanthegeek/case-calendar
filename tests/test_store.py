import sqlite3

import pytest

from case_calendar.store import Store


def _hearing(case_id="us-v-x", key="sentencing", **over):
    base = {
        "case_id": case_id, "hearing_key": key,
        "title": "Sentencing", "starts_at_utc": "2026-04-14T15:00:00+00:00",
        "duration_minutes": 90, "timezone": "America/New_York",
        "location": "Courtroom 4", "judge": "Judge X", "notes": None,
        "dial_in": None, "status": "scheduled", "gcal_event_id": None,
        "docket_id": 12345, "source_entry_ids": [1],
    }
    base.update(over)
    return base


class TestEntries:
    def test_seen_returns_false_initially(self, store: Store):
        assert not store.entry_seen(1, 1, "fp")

    def test_mark_then_seen(self, store: Store):
        store.mark_entry(1, 100, "2026-01-01T00:00:00Z", "fp1")
        assert store.entry_seen(1, 100, "fp1")

    def test_seen_false_when_fingerprint_changes(self, store: Store):
        store.mark_entry(1, 100, "2026-01-01T00:00:00Z", "fp1")
        assert not store.entry_seen(1, 100, "fp2")

    def test_date_filed_preserved_when_re_marked_without_it(self, store: Store):
        # A re-marked entry (fingerprint flip) shouldn't blank the date_filed
        # we cached on first sight.
        store.mark_entry(1, 100, "2026-01-01T00:00:00Z", "fp1",
                         date_filed="2026-01-01")
        store.mark_entry(1, 100, "2026-01-02T00:00:00Z", "fp2")
        row = store.conn.execute(
            "SELECT date_filed FROM entries WHERE entry_id=100"
        ).fetchone()
        assert row["date_filed"] == "2026-01-01"

    def test_latest_entry_modified(self, store: Store):
        assert store.latest_entry_modified(1) is None
        store.mark_entry(1, 1, "2026-01-01T00:00:00Z", "fp")
        store.mark_entry(1, 2, "2026-02-01T00:00:00Z", "fp")
        assert store.latest_entry_modified(1) == "2026-02-01T00:00:00Z"

    def test_get_recent_relevant_entries_skips_filter_failed(self, store: Store):
        # Filter-failed entries are stored without description; they shouldn't
        # appear as context for downstream LLM calls.
        store.mark_entry(1, 100, "2026-01-01T00:00:00Z", "fp",
                         description="MOTION for Hearing TO SET CIPA",
                         entry_number=65)
        store.mark_entry(1, 101, "2026-01-02T00:00:00Z", "fp",
                         description=None)  # filter-failed stub
        store.mark_entry(1, 102, "2026-01-03T00:00:00Z", "fp",
                         description="PAPERLESS Order Setting Pretrial",
                         entry_number=66)
        recent = store.get_recent_relevant_entries(
            1, before_date_modified="2026-02-01T00:00:00Z", limit=5
        )
        assert [r["entry_id"] for r in recent] == [102, 100]  # newest-first
        assert all(r["description"] for r in recent)

    def test_get_recent_relevant_entries_respects_before_cutoff(self, store: Store):
        # Should only return entries strictly older than the cutoff.
        store.mark_entry(1, 100, "2026-01-01T00:00:00Z", "fp",
                         description="earlier", entry_number=1)
        store.mark_entry(1, 200, "2026-03-01T00:00:00Z", "fp",
                         description="later", entry_number=2)
        recent = store.get_recent_relevant_entries(
            1, before_date_modified="2026-02-01T00:00:00Z", limit=5
        )
        assert [r["entry_id"] for r in recent] == [100]

    def test_get_entry_numbers(self, store: Store):
        store.mark_entry(1, 100, "2026-01-01T00:00:00Z", "fp",
                         entry_number=65)
        store.mark_entry(1, 101, "2026-01-02T00:00:00Z", "fp",
                         entry_number=66)
        # Entry without an entry_number — paperless minute order.
        store.mark_entry(1, 102, "2026-01-03T00:00:00Z", "fp")
        got = store.get_entry_numbers([100, 101, 102, 999])
        # 102 omitted (no number), 999 omitted (unknown).
        assert got == {100: 65, 101: 66}

    def test_get_entry_numbers_empty_input(self, store: Store):
        assert store.get_entry_numbers([]) == {}

    def test_get_recent_relevant_entries_limit(self, store: Store):
        for i in range(10):
            ts = f"2026-01-{i+1:02d}T00:00:00Z"
            store.mark_entry(1, 100 + i, ts, "fp",
                             description=f"entry {i}", entry_number=i)
        recent = store.get_recent_relevant_entries(
            1, before_date_modified="2026-02-01T00:00:00Z", limit=3
        )
        assert len(recent) == 3
        # Newest-first: entries 9, 8, 7.
        assert [r["entry_id"] for r in recent] == [109, 108, 107]


class TestDockets:
    def test_meta_roundtrip(self, store: Store):
        store.upsert_docket_meta(7, {
            "court_id": "mad", "docket_number": "1:25",
            "case_name": "US v. X", "absolute_url": "/docket/7/"
        })
        got = store.get_docket_meta(7)
        assert got["court_id"] == "mad"
        assert got["docket_number"] == "1:25"

    def test_set_docket_last_modified_preserves_meta(self, store: Store):
        store.upsert_docket_meta(7, {
            "court_id": "mad", "docket_number": "1:25",
            "case_name": "X", "absolute_url": "/d/7/",
        })
        store.set_docket_last_modified(7, "2026-05-08T11:00:00-07:00")
        meta = store.get_docket_meta(7)
        assert meta["docket_number"] == "1:25"  # not nuked
        assert store.docket_last_modified(7) == "2026-05-08T11:00:00-07:00"

    def test_meta_upsert_overwrites(self, store: Store):
        store.upsert_docket_meta(7, {"court_id": "mad", "docket_number": "old",
                                     "case_name": "X", "absolute_url": "/x/"})
        store.upsert_docket_meta(7, {"court_id": "mad", "docket_number": "new",
                                     "case_name": "Y", "absolute_url": "/y/"})
        assert store.get_docket_meta(7)["docket_number"] == "new"


class TestCourts:
    def test_get_returns_none_when_missing(self, store: Store):
        assert store.get_court_citation("zzz") is None

    def test_upsert_then_get(self, store: Store):
        store.upsert_court("mad", "D. Mass.", "Massachusetts", "District of Mass")
        assert store.get_court_citation("mad") == "D. Mass."

    def test_replace_existing(self, store: Store):
        store.upsert_court("mad", "old", "x", "y")
        store.upsert_court("mad", "D. Mass.", "x", "y")
        assert store.get_court_citation("mad") == "D. Mass."


class TestHearings:
    def test_insert_then_get(self, store: Store):
        store.upsert_hearing(_hearing())
        rows = store.get_hearings("us-v-x")
        assert len(rows) == 1
        assert rows[0]["title"] == "Sentencing"
        assert rows[0]["source_entry_ids"] == [1]

    def test_upsert_overwrites_time(self, store: Store):
        store.upsert_hearing(_hearing())
        store.upsert_hearing(_hearing(starts_at_utc="2026-04-14T19:00:00+00:00"))
        rows = store.get_hearings("us-v-x")
        assert len(rows) == 1
        assert rows[0]["starts_at_utc"] == "2026-04-14T19:00:00+00:00"

    def test_upsert_preserves_gcal_event_id_when_new_is_none(self, store: Store):
        store.upsert_hearing(_hearing(gcal_event_id="abc"))
        store.upsert_hearing(_hearing(gcal_event_id=None))  # update without clobbering
        rows = store.get_hearings("us-v-x")
        assert rows[0]["gcal_event_id"] == "abc"

    def test_upsert_preserves_docket_id_when_new_is_none(self, store: Store):
        store.upsert_hearing(_hearing(docket_id=12345))
        store.upsert_hearing(_hearing(docket_id=None))
        assert store.get_hearings("us-v-x")[0]["docket_id"] == 12345

    def test_get_hearing_by_key(self, store: Store):
        store.upsert_hearing(_hearing())
        h = store.get_hearing("us-v-x", "sentencing")
        assert h and h["title"] == "Sentencing"
        assert store.get_hearing("us-v-x", "missing") is None

    def test_active_excludes_cancelled(self, store: Store):
        store.upsert_hearing(_hearing(key="a", status="scheduled"))
        store.upsert_hearing(_hearing(key="b", status="cancelled"))
        active = store.all_active_hearings()
        keys = {h["hearing_key"] for h in active}
        assert keys == {"a"}


def _deadline(case_id="anthropic-v-dow", key="govt-response-mtd", **over):
    base = {
        "case_id": case_id, "deadline_key": key,
        "title": "Govt response to MTD",
        "due_at_utc": "2026-05-24T21:00:00+00:00",  # 5pm ET → 21:00 UTC
        "timezone": "America/New_York",
        "notes": None, "status": "pending", "significance": "major",
        "deadline_type": "response", "gcal_event_id": None,
        "docket_id": 72380208, "source_entry_ids": [1],
    }
    base.update(over)
    return base


class TestDeadlines:
    def test_insert_then_get(self, store: Store):
        store.upsert_deadline(_deadline())
        rows = store.get_deadlines("anthropic-v-dow")
        assert len(rows) == 1
        assert rows[0]["title"] == "Govt response to MTD"
        assert rows[0]["source_entry_ids"] == [1]
        assert rows[0]["status"] == "pending"

    def test_upsert_overwrites_due_at_utc(self, store: Store):
        store.upsert_deadline(_deadline())
        store.upsert_deadline(
            _deadline(due_at_utc="2026-06-07T21:00:00+00:00")  # extension granted
        )
        rows = store.get_deadlines("anthropic-v-dow")
        assert len(rows) == 1
        assert rows[0]["due_at_utc"] == "2026-06-07T21:00:00+00:00"

    def test_get_by_key(self, store: Store):
        store.upsert_deadline(_deadline())
        d = store.get_deadline("anthropic-v-dow", "govt-response-mtd")
        assert d and d["title"] == "Govt response to MTD"
        assert store.get_deadline("anthropic-v-dow", "missing") is None


class TestWebhookIdempotency:
    def test_unseen_returns_false(self, store: Store):
        assert not store.webhook_seen("uuid-1")

    def test_mark_then_seen(self, store: Store):
        store.mark_webhook_seen("uuid-1", 1)
        assert store.webhook_seen("uuid-1")

    def test_double_mark_is_a_noop(self, store: Store):
        store.mark_webhook_seen("uuid-1", 1)
        store.mark_webhook_seen("uuid-1", 1)  # should not raise
        assert store.webhook_seen("uuid-1")


class TestSchemaMigration:
    def test_old_db_gets_new_columns_added(self, tmp_path):
        # Simulate a pre-migration DB by creating a minimal schema by hand.
        path = tmp_path / "old.sqlite"
        c = sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE dockets (
                docket_id INTEGER PRIMARY KEY,
                date_modified TEXT,
                last_synced_at TEXT NOT NULL
            );
            CREATE TABLE entries (
                docket_id INTEGER NOT NULL,
                entry_id INTEGER NOT NULL,
                date_modified TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                PRIMARY KEY (docket_id, entry_id)
            );
            CREATE TABLE hearings (
                case_id TEXT NOT NULL,
                hearing_key TEXT NOT NULL,
                title TEXT NOT NULL,
                starts_at_utc TEXT,
                duration_minutes INTEGER,
                timezone TEXT NOT NULL,
                location TEXT, judge TEXT, notes TEXT, dial_in TEXT,
                status TEXT NOT NULL,
                gcal_event_id TEXT,
                source_entry_ids TEXT NOT NULL,
                last_updated TEXT NOT NULL,
                PRIMARY KEY (case_id, hearing_key)
            );
        """)
        c.commit()
        c.close()

        # Open via Store: migration should succeed and let us use new fields.
        s = Store(path)
        s.upsert_docket_meta(1, {"court_id": "mad", "docket_number": "1:25",
                                  "case_name": "X", "absolute_url": "/x/"})
        s.mark_entry(1, 100, "2026-01-01", "fp", date_filed="2026-01-01")
        assert s.get_docket_meta(1)["court_id"] == "mad"
        assert s.entry_seen(1, 100, "fp")
        s.close()

    def test_old_text_cache_columns_get_dropped(self, tmp_path):
        # Pre-cleanup DBs carried description_text and pdf_text_excerpt on
        # the entries table to feed the now-removed source-text rendering.
        # Opening via Store should drop them so writes don't waste IO.
        path = tmp_path / "old_with_text.sqlite"
        c = sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE entries (
                docket_id INTEGER NOT NULL,
                entry_id INTEGER NOT NULL,
                date_modified TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                description_text TEXT,
                pdf_text_excerpt TEXT,
                processed_at TEXT NOT NULL,
                PRIMARY KEY (docket_id, entry_id)
            );
        """)
        c.commit()
        c.close()

        s = Store(path)
        cols = {row["name"] for row in s.conn.execute("PRAGMA table_info(entries)")}
        assert "description_text" not in cols
        assert "pdf_text_excerpt" not in cols
        s.close()

    def test_re_open_does_not_error_on_existing_columns(self, tmp_path):
        # Two sequential opens — second one must not error on duplicate ALTER.
        path = tmp_path / "x.sqlite"
        Store(path).close()
        Store(path).close()
