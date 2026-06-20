import sqlite3

import pytest

from case_calendar.store import Store

from .conftest import must


def _hearing(case_id="us-v-x", key="sentencing", **over):
    base = {
        "case_id": case_id,
        "hearing_key": key,
        "title": "Sentencing",
        "starts_at_utc": "2026-04-14T15:00:00+00:00",
        "duration_minutes": 90,
        "timezone": "America/New_York",
        "location": "Courtroom 4",
        "judge": "Judge X",
        "notes": None,
        "dial_in": None,
        "status": "scheduled",
        "gcal_event_id": None,
        "docket_id": 12345,
        "source_entry_ids": [1],
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
        store.mark_entry(1, 100, "2026-01-01T00:00:00Z", "fp1", date_filed="2026-01-01")
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

    def test_get_entries_with_body_returns_only_full_rows(self, store: Store):
        # Body-bearing entries (hearing-relevant + op/disp) are the rows the
        # summary pipeline reads instead of re-fetching from CourtListener. Stubs
        # (description IS NULL) must NOT be returned — otherwise summary's
        # local-cache check would think it's warm when it's actually cold.
        store.mark_entry(
            1,
            10,
            "2024-01-01T00:00:00Z",
            "fp1",
            date_filed="2024-01-01",
            entry_number=1,
            description="INDICTMENT",
            recap_documents=[{"id": 500, "plain_text": "body"}],
        )
        store.mark_entry(
            1,
            11,
            "2024-01-02T00:00:00Z",
            "fp2",
            date_filed="2024-01-02",
            entry_number=2,
            description=None,
        )  # filter-failed stub
        rows = store.get_entries_with_body(1)
        assert len(rows) == 1
        # ``id`` is renamed from entry_id so the shape matches what CourtListener's
        # docket-entries response returns (callers can treat both paths the
        # same way without branching).
        assert rows[0]["id"] == 10
        assert rows[0]["description"] == "INDICTMENT"
        # recap_documents is deserialized from JSON, including plain_text.
        assert rows[0]["recap_documents"][0]["plain_text"] == "body"

    def test_get_entries_with_body_orders_by_date_filed(self, store: Store):
        # Summary's CourtListener fallback returns primary documents oldest-first and
        # then sorts them; the local-cache path must produce the same order
        # so swap-ability between the two paths is invariant.
        store.mark_entry(
            1,
            20,
            "2024-06-01T00:00:00Z",
            "fp2",
            date_filed="2024-06-01",
            entry_number=2,
            description="SUPERSEDING INDICTMENT",
            recap_documents=[],
        )
        store.mark_entry(
            1,
            10,
            "2024-01-01T00:00:00Z",
            "fp1",
            date_filed="2024-01-01",
            entry_number=1,
            description="INDICTMENT",
            recap_documents=[],
        )
        rows = store.get_entries_with_body(1)
        assert [r["id"] for r in rows] == [10, 20]

    def test_get_entries_with_body_handles_legacy_null_recap_documents(
        self,
        store: Store,
    ):
        # Pre-fix rows have recap_documents=NULL. Reading must not crash —
        # those rows simply have an empty list, so pdf.extract_text would
        # find nothing to short-circuit on and fall through to network.
        store.mark_entry(
            1,
            10,
            "2024-01-01T00:00:00Z",
            "fp1",
            date_filed="2024-01-01",
            entry_number=1,
            description="INDICTMENT",
            recap_documents=None,
        )
        rows = store.get_entries_with_body(1)
        assert rows[0]["recap_documents"] == []

    def test_get_recent_relevant_entries_skips_filter_failed(self, store: Store):
        # Filter-failed entries are stored without description; they shouldn't
        # appear as context for downstream LLM calls.
        store.mark_entry(
            1,
            100,
            "2026-01-01T00:00:00Z",
            "fp",
            description="MOTION for Hearing TO SET CIPA",
            entry_number=65,
        )
        store.mark_entry(
            1, 101, "2026-01-02T00:00:00Z", "fp", description=None
        )  # filter-failed stub
        store.mark_entry(
            1,
            102,
            "2026-01-03T00:00:00Z",
            "fp",
            description="PAPERLESS Order Setting Pretrial",
            entry_number=66,
        )
        recent = store.get_recent_relevant_entries(
            1, before_date_modified="2026-02-01T00:00:00Z", limit=5
        )
        assert [r["entry_id"] for r in recent] == [102, 100]  # newest-first
        assert all(r["description"] for r in recent)

    def test_get_recent_relevant_entries_respects_before_cutoff(self, store: Store):
        # Should only return entries strictly older than the cutoff.
        store.mark_entry(
            1, 100, "2026-01-01T00:00:00Z", "fp", description="earlier", entry_number=1
        )
        store.mark_entry(
            1, 200, "2026-03-01T00:00:00Z", "fp", description="later", entry_number=2
        )
        recent = store.get_recent_relevant_entries(
            1, before_date_modified="2026-02-01T00:00:00Z", limit=5
        )
        assert [r["entry_id"] for r in recent] == [100]

    def test_get_relevant_entries_in_date_range(self, store: Store):
        # Filters on date_filed (the stable "hit the docket" anchor), NOT
        # date_modified — note entry 100/101 have date_modified bumped well
        # after filing (an OCR / metadata re-sync), yet still match by their
        # 2023 date_filed. Newest-first by date_filed; filter-failed stubs
        # (NULL description) and out-of-range rows excluded.
        store.mark_entry(
            1,
            100,
            "2026-05-01T00:00:00Z",
            "fp",
            date_filed="2023-12-14",
            description="Minute Entry: Sentencing held 12/14/2023",
            entry_number=90,
        )
        store.mark_entry(
            1,
            101,
            "2026-05-02T00:00:00Z",
            "fp",
            date_filed="2023-12-18",
            description="JUDGMENT IN A CRIMINAL CASE",
            entry_number=91,
        )
        store.mark_entry(
            1,
            102,
            "2024-06-01T00:00:00Z",
            "fp",
            date_filed="2024-06-01",
            description="later, out of range",
            entry_number=95,
        )
        store.mark_entry(
            1,
            103,
            "2026-01-01T00:00:00Z",
            "fp",
            date_filed="2023-12-16",
            description=None,
            entry_number=92,
        )  # filter-failed stub, in range but no description
        got = store.get_relevant_entries_in_date_range(1, "2023-12-12", "2024-01-31")
        assert [r["entry_id"] for r in got] == [
            101,
            100,
        ]  # in-range, newest date_filed first
        assert all(r["description"] for r in got)

    def test_get_relevant_entries_in_date_range_limit_and_null_filed(
        self, store: Store
    ):
        store.mark_entry(
            1,
            200,
            "2026-01-01T00:00:00Z",
            "fp",
            date_filed=None,
            description="paperless, no date_filed",
            entry_number=1,
        )
        for i in range(5):
            store.mark_entry(
                1,
                300 + i,
                "2026-01-01T00:00:00Z",
                "fp",
                date_filed=f"2024-03-{i + 1:02d}",
                description=f"e{i}",
                entry_number=10 + i,
            )
        got = store.get_relevant_entries_in_date_range(
            1, "2024-03-01", "2024-03-31", limit=3
        )
        assert len(got) == 3  # limit respected
        assert all(r["date_filed"] for r in got)  # NULL date_filed row excluded
        assert 200 not in [r["entry_id"] for r in got]

    def test_get_entry_numbers(self, store: Store):
        store.mark_entry(1, 100, "2026-01-01T00:00:00Z", "fp", entry_number=65)
        store.mark_entry(1, 101, "2026-01-02T00:00:00Z", "fp", entry_number=66)
        # Entry without an entry_number — paperless minute order.
        store.mark_entry(1, 102, "2026-01-03T00:00:00Z", "fp")
        got = store.get_entry_numbers([100, 101, 102, 999])
        # 102 omitted (no number), 999 omitted (unknown).
        assert got == {100: 65, 101: 66}

    def test_get_entry_numbers_empty_input(self, store: Store):
        assert store.get_entry_numbers([]) == {}

    def test_get_entries_by_ids_returns_full_rows(self, store: Store):
        # Same column shape as get_recent_relevant_entries /
        # get_relevant_entries_in_date_range so the verify-pass merge can
        # treat all three sources interchangeably. Used to surface a
        # hearing's source entries (the entries that allocated the row)
        # in the verify-pass context — the McGonigal scheduling-order-
        # too-old-for-recent-window class of regression.
        store.mark_entry(
            1,
            100,
            "2026-01-01T00:00:00Z",
            "fp",
            date_filed="2023-08-01",
            description="ORDER: TRIAL SET FOR JUNE 12, 2024",
            entry_number=42,
        )
        store.mark_entry(
            1,
            101,
            "2026-01-02T00:00:00Z",
            "fp",
            date_filed="2023-08-15",
            description="Minute Entry: status conference held",
            entry_number=43,
        )
        got = store.get_entries_by_ids(1, [100, 101])
        ids = {r["entry_id"] for r in got}
        assert ids == {100, 101}
        assert {r["entry_number"] for r in got} == {42, 43}
        # Same shape — description / short_description / date_filed are
        # always returned, even when None.
        assert all(
            {
                "entry_id",
                "entry_number",
                "date_filed",
                "description",
                "short_description",
            }
            <= set(r)
            for r in got
        )

    def test_get_entries_by_ids_empty_input(self, store: Store):
        assert store.get_entries_by_ids(1, []) == []

    def test_get_entries_by_ids_silently_drops_unknown(self, store: Store):
        store.mark_entry(
            1, 100, "2026-01-01T00:00:00Z", "fp", description="known", entry_number=1
        )
        got = store.get_entries_by_ids(1, [100, 999, 1000])
        assert {r["entry_id"] for r in got} == {100}

    def test_get_entries_by_ids_includes_filter_failed_stubs(self, store: Store):
        # Filter-failed stubs (NULL description) ARE returned — the
        # source entry might be a stub if its body wasn't worth keeping
        # at sync time, but the verify pass still needs the model to see
        # SOME evidence the row was scheduled, even just the short
        # description. Differs from get_recent_relevant_entries /
        # get_relevant_entries_in_date_range which both gate on
        # ``description IS NOT NULL``.
        store.mark_entry(
            1,
            100,
            "2026-01-01T00:00:00Z",
            "fp",
            description=None,  # filter-failed stub
            short_description="Notice of Hearing",
            entry_number=42,
        )
        got = store.get_entries_by_ids(1, [100])
        assert len(got) == 1
        assert got[0]["entry_id"] == 100
        assert got[0]["description"] is None
        assert got[0]["short_description"] == "Notice of Hearing"

    def test_get_entries_by_ids_scoped_to_docket(self, store: Store):
        # Entry_id alone is unique, but the verify pass is per-docket and
        # sibling-docket entries shouldn't bleed into a different
        # docket's verify context — so the query is scoped on both.
        store.mark_entry(
            1, 100, "2026-01-01T00:00:00Z", "fp", description="docket 1", entry_number=1
        )
        store.mark_entry(
            2, 200, "2026-01-01T00:00:00Z", "fp", description="docket 2", entry_number=1
        )
        assert {r["entry_id"] for r in store.get_entries_by_ids(1, [100, 200])} == {100}
        assert {r["entry_id"] for r in store.get_entries_by_ids(2, [100, 200])} == {200}

    def test_get_entry_documents_roundtrip(self, store: Store):
        docs = [
            {
                "id": 5,
                "document_number": 65,
                "attachment_number": None,
                "is_available": True,
                "is_sealed": False,
                "filepath_ia": "https://archive.org/65.pdf",
                "filepath_local": "recap/x/65.pdf",
                "description": None,
            },
            {
                "id": 6,
                "document_number": 65,
                "attachment_number": 1,
                "is_available": True,
                "is_sealed": False,
                "filepath_ia": "https://archive.org/65a.pdf",
                "filepath_local": "recap/x/65a.pdf",
                "description": "Exhibit A",
            },
        ]
        store.mark_entry(
            1, 100, "2026-01-01T00:00:00Z", "fp", entry_number=65, recap_documents=docs
        )
        # Unknown ids are silently dropped. Filter-failed stubs have no docs.
        store.mark_entry(1, 101, "2026-01-02T00:00:00Z", "fp", entry_number=66)
        got = store.get_entry_documents([100, 101, 999])
        assert set(got) == {100}
        assert got[100] == docs

    def test_get_entry_documents_overwrite_on_reprocess(self, store: Store):
        # Adding a doc to an existing entry is the "watch for new
        # documents" case: re-marking with a longer list replaces the
        # cached JSON so emit-time descriptions show the new doc.
        first = [
            {
                "id": 5,
                "document_number": 65,
                "attachment_number": None,
                "is_available": True,
                "is_sealed": False,
                "filepath_ia": "https://archive.org/65.pdf",
                "filepath_local": None,
                "description": None,
            },
        ]
        store.mark_entry(
            1, 100, "2026-01-01T00:00:00Z", "fp", entry_number=65, recap_documents=first
        )
        second = first + [
            {
                "id": 6,
                "document_number": 65,
                "attachment_number": 1,
                "is_available": True,
                "is_sealed": False,
                "filepath_ia": "https://archive.org/65a.pdf",
                "filepath_local": None,
                "description": None,
            },
        ]
        store.mark_entry(
            1,
            100,
            "2026-01-02T00:00:00Z",
            "fp2",
            entry_number=65,
            recap_documents=second,
        )
        got = store.get_entry_documents([100])
        assert got[100] == second

    def test_get_entry_documents_empty_input(self, store: Store):
        assert store.get_entry_documents([]) == {}

    def test_get_recent_relevant_entries_limit(self, store: Store):
        for i in range(10):
            ts = f"2026-01-{i + 1:02d}T00:00:00Z"
            store.mark_entry(
                1, 100 + i, ts, "fp", description=f"entry {i}", entry_number=i
            )
        recent = store.get_recent_relevant_entries(
            1, before_date_modified="2026-02-01T00:00:00Z", limit=3
        )
        assert len(recent) == 3
        # Newest-first: entries 9, 8, 7.
        assert [r["entry_id"] for r in recent] == [109, 108, 107]


class TestDockets:
    def test_meta_roundtrip(self, store: Store):
        store.upsert_docket_meta(
            7,
            {
                "court_id": "mad",
                "docket_number": "1:25",
                "case_name": "US v. X",
                "absolute_url": "/docket/7/",
            },
        )
        got = must(store.get_docket_meta(7))
        assert got["court_id"] == "mad"
        assert got["docket_number"] == "1:25"

    def test_set_docket_last_modified_preserves_meta(self, store: Store):
        store.upsert_docket_meta(
            7,
            {
                "court_id": "mad",
                "docket_number": "1:25",
                "case_name": "X",
                "absolute_url": "/d/7/",
            },
        )
        store.set_docket_last_modified(7, "2026-05-08T11:00:00-07:00")
        meta = must(store.get_docket_meta(7))
        assert meta["docket_number"] == "1:25"  # not nuked
        assert store.docket_last_modified(7) == "2026-05-08T11:00:00-07:00"

    def test_meta_upsert_overwrites(self, store: Store):
        store.upsert_docket_meta(
            7,
            {
                "court_id": "mad",
                "docket_number": "old",
                "case_name": "X",
                "absolute_url": "/x/",
            },
        )
        store.upsert_docket_meta(
            7,
            {
                "court_id": "mad",
                "docket_number": "new",
                "case_name": "Y",
                "absolute_url": "/y/",
            },
        )
        assert must(store.get_docket_meta(7))["docket_number"] == "new"

    def test_bump_advances_forward(self, store: Store):
        # Forward-only advance: a newer entry's date_modified bumps the
        # docket's cutoff, which is what the index's "updated at"
        # display reads from.
        store.set_docket_last_modified(7, "2026-05-01T00:00:00Z")
        store.bump_docket_last_modified(7, "2026-05-08T00:00:00Z")
        assert store.docket_last_modified(7) == "2026-05-08T00:00:00Z"

    def test_bump_ignores_older(self, store: Store):
        # Out-of-order webhook delivery (older entry arrives after a
        # newer one) must not move the cutoff backwards.
        store.set_docket_last_modified(7, "2026-05-08T00:00:00Z")
        store.bump_docket_last_modified(7, "2026-05-01T00:00:00Z")
        assert store.docket_last_modified(7) == "2026-05-08T00:00:00Z"

    def test_bump_inserts_when_missing(self, store: Store):
        # First-time webhook delivery for a docket we haven't poll-synced —
        # the row may not exist yet, or may exist with NULL date_modified.
        # Either way, bump should land the value.
        store.bump_docket_last_modified(7, "2026-05-08T00:00:00Z")
        assert store.docket_last_modified(7) == "2026-05-08T00:00:00Z"

    def test_known_docket_ids(self, store: Store):
        # Backs the `sync --only-new` filter: a docket is "known" once it
        # has a row in the dockets table — set_docket_last_modified is the
        # usual path; upsert_docket_meta gets there too via a different
        # column write.
        assert store.known_docket_ids() == set()
        store.set_docket_last_modified(100, "2026-05-08T00:00:00Z")
        store.upsert_docket_meta(
            200,
            {
                "court_id": "mad",
                "docket_number": "1:25",
                "case_name": "X",
                "absolute_url": "/d/200/",
            },
        )
        assert store.known_docket_ids() == {100, 200}

    def test_date_last_filing_persists_via_upsert(self, store: Store):
        # date_last_filing is captured from CourtListener on the polling path; ensure
        # it round-trips through upsert_docket_meta + get_docket_meta.
        store.upsert_docket_meta(
            7,
            {
                "court_id": "mad",
                "docket_number": "1:25",
                "case_name": "X",
                "absolute_url": "/d/7/",
                "date_last_filing": "2026-05-08",
            },
        )
        meta = must(store.get_docket_meta(7))
        assert meta["date_last_filing"] == "2026-05-08"

    def test_date_last_filing_none_does_not_clobber(self, store: Store):
        # A subsequent upsert that doesn't pass date_last_filing (e.g. a
        # webhook-driven path that touches metadata but never re-fetches
        # the docket) must NOT wipe the previously-cached value.
        store.upsert_docket_meta(
            7,
            {
                "court_id": "mad",
                "docket_number": "1:25",
                "case_name": "X",
                "absolute_url": "/d/7/",
                "date_last_filing": "2026-05-08",
            },
        )
        store.upsert_docket_meta(
            7,
            {
                "court_id": "mad",
                "docket_number": "1:25",
                "case_name": "X",
                "absolute_url": "/d/7/",
            },
        )
        assert must(store.get_docket_meta(7))["date_last_filing"] == "2026-05-08"

    def test_docket_group_ids_returns_all_records_with_stable_canonical(
        self, store: Store
    ):
        # CourtListener can split one logical PACER docket across several
        # docket_id rows (bug #7345). The group is every docket_id sharing
        # (docket_number, court_id); min(group) is the stable canonical the
        # extractor normalizes to (independent of date_modified ordering).
        for did, mod in (
            (73510620, "2026-06-20T09:00:00Z"),
            (71820111, "2026-06-20T09:01:00Z"),
        ):
            store.upsert_docket_meta(
                did,
                {
                    "court_id": "tnmd",
                    "docket_number": "3:23-cr-00088",
                    "case_name": "X",
                },
            )
            store.set_docket_last_modified(did, mod)
        # A genuinely-distinct docket (different number) is NOT in the group.
        store.upsert_docket_meta(
            999,
            {"court_id": "tnmd", "docket_number": "3:23-cr-99999", "case_name": "Y"},
        )
        group = store.get_docket_group_ids("3:23-cr-00088", "tnmd")
        assert set(group) == {71820111, 73510620}
        assert min(group) == 71820111
        assert store.get_docket_group_ids("3:23-cr-99999", "tnmd") == [999]

    def test_bump_last_filing_advances_forward(self, store: Store):
        # process_entry calls this with entry.date_filed so webhook-only
        # deployments can keep the index date current without refetching
        # the parent docket per delivery.
        store.upsert_docket_meta(
            7,
            {
                "court_id": "mad",
                "docket_number": "1:25",
                "case_name": "X",
                "absolute_url": "/d/7/",
                "date_last_filing": "2026-05-01",
            },
        )
        store.bump_docket_last_filing(7, "2026-05-08")
        assert must(store.get_docket_meta(7))["date_last_filing"] == "2026-05-08"

    def test_bump_last_filing_ignores_older(self, store: Store):
        # An entry whose date_filed is older than what CourtListener already gave us
        # (e.g. a late-arriving webhook for an old entry) must not move
        # the cutoff backwards.
        store.upsert_docket_meta(
            7,
            {
                "court_id": "mad",
                "docket_number": "1:25",
                "case_name": "X",
                "absolute_url": "/d/7/",
                "date_last_filing": "2026-05-08",
            },
        )
        store.bump_docket_last_filing(7, "2026-05-01")
        assert must(store.get_docket_meta(7))["date_last_filing"] == "2026-05-08"

    def test_bump_last_filing_inserts_when_missing(self, store: Store):
        # First-time webhook delivery for a docket we haven't poll-synced;
        # bump should land the value even though no row exists yet.
        store.bump_docket_last_filing(7, "2026-05-08")
        assert must(store.get_docket_meta(7))["date_last_filing"] == "2026-05-08"

    def test_bump_last_filing_empty_string_noop(self, store: Store):
        # The opportunistic bump in process_entry passes whatever the
        # entry's date_filed was; CourtListener sometimes omits the field, and an
        # empty-string bump must not insert a row or clobber an existing
        # value.
        store.upsert_docket_meta(
            7,
            {
                "court_id": "mad",
                "docket_number": "1:25",
                "case_name": "X",
                "absolute_url": "/d/7/",
                "date_last_filing": "2026-05-08",
            },
        )
        store.bump_docket_last_filing(7, "")
        assert must(store.get_docket_meta(7))["date_last_filing"] == "2026-05-08"


class TestCaseAggregates:
    def test_min_filed_max_last_filing_across_dockets(self, store: Store):
        # Earliest date_filed across the case's dockets wins as the case's
        # "filed" date; latest docket-level date_last_filing wins as the
        # "last filing" date the index page surfaces.
        store.upsert_docket_meta(
            10,
            {
                "court_id": "nysd",
                "docket_number": "1:25",
                "case_name": "X",
                "absolute_url": "/d/10/",
                "date_last_filing": "2026-05-10",
            },
        )
        store.upsert_docket_meta(
            11,
            {
                "court_id": "nysd",
                "docket_number": "1:24",
                "case_name": "X",
                "absolute_url": "/d/11/",
                "date_last_filing": "2026-04-01",
            },
        )
        store.mark_entry(10, 1, "2025-01-15T08:00:00Z", "fp", date_filed="2025-01-15")
        store.mark_entry(11, 2, "2024-09-01T08:00:00Z", "fp", date_filed="2024-09-01")
        agg = store.get_case_aggregates([10, 11])
        assert agg["date_filed"] == "2024-09-01"
        assert agg["last_filing_date"] == "2026-05-10"

    def test_ignores_date_modified_for_last_filing(self, store: Store):
        # Regression: the aggregate previously read from dockets.date_modified,
        # which bumps on OCR / metadata churn. After the switch to
        # date_last_filing, a docket whose date_modified is newer than its
        # date_last_filing must NOT show date_modified as "last filing".
        store.set_docket_last_modified(10, "2026-05-10T12:00:00Z")
        store.upsert_docket_meta(
            10,
            {
                "court_id": "nysd",
                "docket_number": "1:25",
                "case_name": "X",
                "absolute_url": "/d/10/",
                "date_last_filing": "2026-04-01",
            },
        )
        agg = store.get_case_aggregates([10])
        assert agg["last_filing_date"] == "2026-04-01"

    def test_returns_none_when_no_rows(self, store: Store):
        agg = store.get_case_aggregates([42, 43])
        assert agg == {"date_filed": None, "last_filing_date": None}

    def test_empty_docket_list(self, store: Store):
        # A case with no dockets configured shouldn't blow up; just return None.
        agg = store.get_case_aggregates([])
        assert agg == {"date_filed": None, "last_filing_date": None}


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

    def test_in_court_filters_cross_court_siblings(self, store: Store):
        # Parallel proceedings in different courts must not show up in each
        # other's known-events context — that's the contamination this guards.
        store.upsert_docket_meta(
            1001,
            {
                "court_id": "cadc",
                "docket_number": "26-1049",
                "case_name": "X",
                "absolute_url": "/x/",
            },
        )
        store.upsert_docket_meta(
            1002,
            {
                "court_id": "ca9",
                "docket_number": "26-2011",
                "case_name": "X",
                "absolute_url": "/y/",
            },
        )
        store.upsert_hearing(_hearing(key="oral-arg-dc", docket_id=1001))
        store.upsert_hearing(_hearing(key="oral-arg-9", docket_id=1002))
        cadc = store.get_hearings_in_court("us-v-x", "cadc")
        assert {h["hearing_key"] for h in cadc} == {"oral-arg-dc"}
        ca9 = store.get_hearings_in_court("us-v-x", "ca9")
        assert {h["hearing_key"] for h in ca9} == {"oral-arg-9"}

    def test_in_court_keeps_same_court_siblings(self, store: Store):
        # Multi-defendant criminal: same court, separate dockets per defendant —
        # legitimately aggregated, must still appear together.
        store.upsert_docket_meta(
            2001,
            {
                "court_id": "dcd",
                "docket_number": "1:24-cr-261",
                "case_name": "X",
                "absolute_url": "/a/",
            },
        )
        store.upsert_docket_meta(
            2002,
            {
                "court_id": "dcd",
                "docket_number": "1:24-cr-261",
                "case_name": "X",
                "absolute_url": "/b/",
            },
        )
        store.upsert_hearing(_hearing(key="arraignment-a", docket_id=2001))
        store.upsert_hearing(_hearing(key="arraignment-b", docket_id=2002))
        out = store.get_hearings_in_court("us-v-x", "dcd")
        assert {h["hearing_key"] for h in out} == {"arraignment-a", "arraignment-b"}

    def test_in_court_includes_dangling_rows(self, store: Store):
        # docket_id NULL (legacy data) or court_id NULL (docket metadata not yet
        # cached) — keep them so we don't silently drop context.
        store.upsert_hearing(_hearing(key="legacy", docket_id=None))
        store.upsert_docket_meta(
            3001,
            {
                "court_id": None,
                "docket_number": "x",
                "case_name": "X",
                "absolute_url": "/c/",
            },
        )
        store.upsert_hearing(_hearing(key="uncached", docket_id=3001))
        out = store.get_hearings_in_court("us-v-x", "cadc")
        assert {h["hearing_key"] for h in out} == {"legacy", "uncached"}

    def test_find_concurrent_hearing_clusters_groups_by_docket_and_time(
        self,
        store: Store,
    ):
        # Two future hearings sharing the same (docket_id, starts_at_utc)
        # form a cluster. A third hearing at a different time on the same
        # docket does NOT. A fourth hearing at the same time but on a
        # different docket does NOT (the rule is same-court same-slot,
        # not same-time-anywhere).
        future = "2099-04-14T15:00:00+00:00"
        store.upsert_hearing(
            _hearing(
                key="msj-hearing",
                starts_at_utc=future,
                docket_id=1,
            )
        )
        store.upsert_hearing(
            _hearing(
                key="motion-hearing-2",
                starts_at_utc=future,
                docket_id=1,
            )
        )
        store.upsert_hearing(
            _hearing(
                key="status",
                starts_at_utc="2099-04-15T15:00:00+00:00",
                docket_id=1,
            )
        )
        store.upsert_hearing(
            _hearing(
                key="other-docket",
                starts_at_utc=future,
                docket_id=2,
            )
        )
        clusters = store.find_concurrent_hearing_clusters("us-v-x")
        assert len(clusters) == 1
        keys = {h["hearing_key"] for h in clusters[0]}
        assert keys == {"msj-hearing", "motion-hearing-2"}
        # source_entry_ids is JSON-decoded for the caller.
        assert all(isinstance(h["source_entry_ids"], list) for h in clusters[0])

    def test_find_concurrent_hearing_clusters_excludes_past_and_non_scheduled(
        self,
        store: Store,
    ):
        # Past slots are handled by the auto-held sweep — don't bother
        # the LLM about them.
        past = "2020-01-01T00:00:00+00:00"
        store.upsert_hearing(
            _hearing(
                key="past-a",
                starts_at_utc=past,
                docket_id=1,
            )
        )
        store.upsert_hearing(
            _hearing(
                key="past-b",
                starts_at_utc=past,
                docket_id=1,
            )
        )
        # Cancelled / held rows must not poison a future cluster either —
        # only count 'scheduled' rows.
        future = "2099-04-14T15:00:00+00:00"
        store.upsert_hearing(
            _hearing(
                key="future-cancelled",
                starts_at_utc=future,
                docket_id=1,
                status="cancelled",
            )
        )
        store.upsert_hearing(
            _hearing(
                key="future-scheduled",
                starts_at_utc=future,
                docket_id=1,
                status="scheduled",
            )
        )
        assert store.find_concurrent_hearing_clusters("us-v-x") == []

    def test_find_concurrent_hearing_clusters_groups_cross_sibling_drift(
        self,
        store: Store,
    ):
        # The cross-CourtListener-sibling case: two CourtListener docket_ids in the same
        # (docket_number, court_id) group hold same-slot future hearings
        # under different keys. The cluster key uses (docket_number,
        # court_id, starts_at_utc) so they group together.
        for did in (4001, 4002):
            store.upsert_docket_meta(
                did,
                {
                    "court_id": "dcd",
                    "docket_number": "1:24-cr-00261",
                    "case_name": "United States v. Didenko",
                    "absolute_url": f"/docket/{did}/x/",
                },
            )
        future = "2099-04-14T15:00:00+00:00"
        store.upsert_hearing(
            _hearing(key="sentencing-didenko", starts_at_utc=future, docket_id=4001)
        )
        store.upsert_hearing(
            _hearing(key="sentencing-didenko-2", starts_at_utc=future, docket_id=4002)
        )
        clusters = store.find_concurrent_hearing_clusters("us-v-x")
        assert len(clusters) == 1
        keys = {h["hearing_key"] for h in clusters[0]}
        assert keys == {"sentencing-didenko", "sentencing-didenko-2"}

    def test_find_concurrent_held_hearing_clusters_groups_same_slot_held_rows(
        self,
        store: Store,
    ):
        # Cross-CourtListener-sibling held-row drift — the verbatim didenko shape.
        # Two CourtListener docket_ids in the same (docket_number, court_id) group;
        # both have a HELD hearing at the same UTC slot under different
        # keys. The held-cluster helper picks them up.
        for did in (4001, 4002):
            store.upsert_docket_meta(
                did,
                {
                    "court_id": "dcd",
                    "docket_number": "1:24-cr-00261",
                    "case_name": "X",
                    "absolute_url": f"/docket/{did}/x/",
                },
            )
        slot = "2026-02-19T16:00:00+00:00"  # the didenko sentencing UTC slot
        store.upsert_hearing(
            _hearing(
                key="sentencing-didenko",
                starts_at_utc=slot,
                docket_id=4001,
                status="held",
            )
        )
        store.upsert_hearing(
            _hearing(
                key="sentencing-didenko-2",
                starts_at_utc=slot,
                docket_id=4002,
                status="held",
            )
        )
        clusters = store.find_concurrent_held_hearing_clusters("us-v-x")
        assert len(clusters) == 1
        keys = {h["hearing_key"] for h in clusters[0]}
        assert keys == {"sentencing-didenko", "sentencing-didenko-2"}

    def test_find_concurrent_held_hearing_clusters_excludes_non_held(
        self,
        store: Store,
    ):
        # Scheduled / cancelled rows must not appear in the held cluster
        # (their dedup paths are separate).
        for did in (5001, 5002):
            store.upsert_docket_meta(
                did,
                {
                    "court_id": "dcd",
                    "docket_number": "1:24-cr-00500",
                    "case_name": "X",
                    "absolute_url": "/x/",
                },
            )
        slot = "2026-02-19T16:00:00+00:00"
        store.upsert_hearing(
            _hearing(key="held-1", starts_at_utc=slot, docket_id=5001, status="held")
        )
        store.upsert_hearing(
            _hearing(
                key="scheduled-clone",
                starts_at_utc=slot,
                docket_id=5002,
                status="scheduled",
            )
        )
        store.upsert_hearing(
            _hearing(
                key="cancelled-clone",
                starts_at_utc=slot,
                docket_id=5002,
                status="cancelled",
            )
        )
        assert store.find_concurrent_held_hearing_clusters("us-v-x") == []


class TestDeleteHearing:
    """``Store.delete_hearing`` — single-row delete used by the dedupe
    sweeps to remove same-slot key-drift siblings once their
    ``source_entry_ids`` are merged onto the canonical row.
    """

    def test_delete_removes_matching_row(self, store: Store):
        store.upsert_hearing(_hearing(key="keep-me"))
        store.upsert_hearing(_hearing(key="delete-me"))
        deleted = store.delete_hearing("us-v-x", "delete-me")
        assert deleted == 1
        remaining = {h["hearing_key"] for h in store.get_hearings("us-v-x")}
        assert remaining == {"keep-me"}

    def test_delete_returns_zero_on_unknown_key(self, store: Store):
        store.upsert_hearing(_hearing(key="exists"))
        deleted = store.delete_hearing("us-v-x", "never-existed")
        assert deleted == 0
        # Existing row untouched.
        assert len(store.get_hearings("us-v-x")) == 1

    def test_delete_scopes_to_case_id(self, store: Store):
        # Same hearing_key, two different cases — delete touches only the
        # matching case_id.
        store.upsert_hearing(_hearing(case_id="us-v-akhter", key="shared-key"))
        store.upsert_hearing(_hearing(case_id="us-v-x", key="shared-key"))
        deleted = store.delete_hearing("us-v-x", "shared-key")
        assert deleted == 1
        # Akhter row survives.
        assert {h["hearing_key"] for h in store.get_hearings("us-v-akhter")} == {
            "shared-key"
        }
        assert store.get_hearings("us-v-x") == []


def _deadline(case_id="anthropic-v-dow", key="govt-response-mtd", **over):
    base = {
        "case_id": case_id,
        "deadline_key": key,
        "title": "Govt response to MTD",
        "due_at_utc": "2026-05-24T21:00:00+00:00",  # 5pm ET → 21:00 UTC
        "timezone": "America/New_York",
        "notes": None,
        "status": "pending",
        "significance": "major",
        "deadline_type": "response",
        "gcal_event_id": None,
        "docket_id": 72380208,
        "source_entry_ids": [1],
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

    def test_in_court_filters_cross_court_siblings(self, store: Store):
        store.upsert_docket_meta(
            1001,
            {
                "court_id": "cadc",
                "docket_number": "26-1049",
                "case_name": "X",
                "absolute_url": "/x/",
            },
        )
        store.upsert_docket_meta(
            1002,
            {
                "court_id": "ca9",
                "docket_number": "26-2011",
                "case_name": "X",
                "absolute_url": "/y/",
            },
        )
        store.upsert_deadline(_deadline(key="reply-dc", docket_id=1001))
        store.upsert_deadline(_deadline(key="reply-9", docket_id=1002))
        cadc = store.get_deadlines_in_court("anthropic-v-dow", "cadc")
        assert {d["deadline_key"] for d in cadc} == {"reply-dc"}
        ca9 = store.get_deadlines_in_court("anthropic-v-dow", "ca9")
        assert {d["deadline_key"] for d in ca9} == {"reply-9"}

    def test_in_court_keeps_same_court_siblings(self, store: Store):
        store.upsert_docket_meta(
            2001,
            {
                "court_id": "dcd",
                "docket_number": "1:24-cr-261",
                "case_name": "X",
                "absolute_url": "/a/",
            },
        )
        store.upsert_docket_meta(
            2002,
            {
                "court_id": "dcd",
                "docket_number": "1:24-cr-261",
                "case_name": "X",
                "absolute_url": "/b/",
            },
        )
        store.upsert_deadline(_deadline(key="brief-a", docket_id=2001))
        store.upsert_deadline(_deadline(key="brief-b", docket_id=2002))
        out = store.get_deadlines_in_court("anthropic-v-dow", "dcd")
        assert {d["deadline_key"] for d in out} == {"brief-a", "brief-b"}

    def test_in_court_includes_dangling_rows(self, store: Store):
        store.upsert_deadline(_deadline(key="legacy", docket_id=None))
        store.upsert_docket_meta(
            3001,
            {
                "court_id": None,
                "docket_number": "x",
                "case_name": "X",
                "absolute_url": "/c/",
            },
        )
        store.upsert_deadline(_deadline(key="uncached", docket_id=3001))
        out = store.get_deadlines_in_court("anthropic-v-dow", "cadc")
        assert {d["deadline_key"] for d in out} == {"legacy", "uncached"}


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


class TestTxRollback:
    def test_exception_inside_tx_triggers_rollback(self, store: Store):
        # Write something that would commit on success, then raise inside
        # the with-block — the row must NOT be visible afterward.
        with pytest.raises(RuntimeError):
            with store.tx():
                store.conn.execute(
                    "INSERT INTO webhook_events (idempotency_key, event_type, received_at) "
                    "VALUES (?, ?, datetime('now'))",
                    ("rollback-key", 1),
                )
                raise RuntimeError("boom")
        # Rollback restored the table.
        assert not store.webhook_seen("rollback-key")


class TestConcurrencyPragmas:
    """The polling sync and the webhook-serving process share the same
    SQLite file. Without WAL + a busy_timeout, the second writer raises
    SQLITE_BUSY immediately on any commit overlap — a webhook landing
    mid-sync would bubble up as HTTP 500 (CourtListener retries with the same
    Idempotency-Key, but transient errors show up in the log), and a
    sync that loses a race aborts the whole invocation.
    """

    def test_wal_mode_enabled(self, store: Store):
        mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_busy_timeout_set(self, store: Store):
        timeout = store.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == 5000

    def test_contending_writer_blocks_instead_of_immediately_raising(
        self,
        tmp_path,
    ):
        # Two Store instances against the same DB file (simulates the
        # sync-process / serve-process split). Hold a write lock on one,
        # then attempt a write on the other in a background thread —
        # without busy_timeout this would raise OperationalError immediately;
        # with it, the loser blocks until the holder commits, then succeeds.
        import threading
        import time

        db = tmp_path / "concurrency.sqlite"
        a = Store(db)
        b = Store(db)

        a.conn.execute("BEGIN IMMEDIATE")
        a.conn.execute(
            "INSERT INTO webhook_events (idempotency_key, event_type, received_at) "
            "VALUES ('a', 1, datetime('now'))"
        )

        result: dict = {}

        def contend():
            try:
                b.conn.execute(
                    "INSERT INTO webhook_events "
                    "(idempotency_key, event_type, received_at) "
                    "VALUES ('b', 1, datetime('now'))"
                )
                b.conn.commit()
                result["status"] = "ok"
            except sqlite3.OperationalError as e:
                result["status"] = "busy"
                result["error"] = str(e)

        t = threading.Thread(target=contend)
        t.start()
        # Give the contender enough time to attempt the write and start
        # blocking on the busy lock.
        time.sleep(0.2)
        # Holder releases — contender should unblock and commit, NOT
        # have already raised SQLITE_BUSY.
        a.conn.commit()
        t.join(timeout=2.0)

        assert result.get("status") == "ok", (
            f"expected loser to block-and-succeed, got {result!r}"
        )
        # Both rows landed.
        assert a.webhook_seen("a")
        assert b.webhook_seen("b")
        a.close()
        b.close()


class TestEntryByNumber:
    def test_returns_row(self, store: Store):
        store.mark_entry(
            1, 100, "2026-01-01T00:00:00Z", "fp", entry_number=65, description="Order"
        )
        row = store.get_entry_by_number(1, 65)
        assert row and row["entry_id"] == 100

    def test_missing_returns_none(self, store: Store):
        assert store.get_entry_by_number(1, 999) is None


class TestEntryDocumentsMalformedJson:
    def test_skips_rows_with_invalid_json(self, store: Store):
        # Insert an entry with malformed recap_documents JSON via raw SQL.
        # get_entry_documents must catch the JSONDecodeError and skip the row
        # rather than crashing the whole emit.
        store.mark_entry(
            1, 100, "2026-01-01T00:00:00Z", "fp", entry_number=65, description="Order"
        )
        store.conn.execute(
            "UPDATE entries SET recap_documents=? WHERE entry_id=?",
            ("not json", 100),
        )
        out = store.get_entry_documents([100])
        assert out == {}  # bad row skipped


class TestRefreshEntryRecapDocuments:
    """The self-heal write path. `summary.find_primary_documents` calls
    this when it detects a stale cached recap_documents row (the
    us-v-moucka shape — locally-stored plain_text empty even though
    CourtListener has 39 KB of text). The method overwrites only the
    `recap_documents` column, leaving fingerprint and the rest alone so
    a real future content change still trips the normal sync path.
    """

    def test_returns_false_when_entry_has_no_id(self, store: Store):
        # The CourtListener-shaped entry dict normally carries `id`;
        # defensive callers may hand in a partial dict. The method must
        # no-op rather than corrupt or raise.
        assert store.refresh_entry_recap_documents({}, docket_id=1) is False

    def test_returns_false_when_row_missing(self, store: Store):
        # No row in the local store yet — nothing to refresh. Doesn't
        # write a new row (refresh is for repairing existing data, not
        # inserting; sync owns inserts).
        result = store.refresh_entry_recap_documents(
            {"id": 999, "recap_documents": []},
            docket_id=1,
        )
        assert result is False
        assert store.get_entry_by_number(1, 65) is None  # nothing inserted

    def test_overwrites_recap_documents_without_touching_fingerprint(
        self, store: Store
    ):
        store.mark_entry(
            1,
            100,
            "2026-01-01T00:00:00Z",
            "original-fingerprint",
            entry_number=65,
            description="INDICTMENT",
            recap_documents=[
                {
                    "id": 500,
                    "attachment_number": None,
                    "is_available": True,
                    "is_sealed": False,
                    "plain_text": None,  # stale — the bug shape
                }
            ],
        )
        # Hand the method a fresh CourtListener-shaped entry whose main
        # recap_document carries full plain_text.
        fresh_entry = {
            "id": 100,
            "recap_documents": [
                {
                    "id": 500,
                    "document_number": "1",
                    "attachment_number": None,
                    "is_available": True,
                    "is_sealed": False,
                    "plain_text": "Body of the indictment with charges.",
                }
            ],
        }
        assert store.refresh_entry_recap_documents(fresh_entry, docket_id=1) is True
        # plain_text now populated.
        refreshed = store.get_entries_with_body(1)
        rd = refreshed[0]["recap_documents"][0]
        assert rd["plain_text"] == "Body of the indictment with charges."
        # Fingerprint and description left alone — sync still owns those.
        fp_row = store.conn.execute(
            "SELECT fingerprint, description FROM entries "
            "WHERE docket_id=1 AND entry_id=100"
        ).fetchone()
        assert fp_row["fingerprint"] == "original-fingerprint"
        assert fp_row["description"] == "INDICTMENT"


class TestGcalAndM365Setters:
    def test_set_gcal_id(self, store: Store):
        store.upsert_hearing(_hearing())
        store.set_gcal_id("us-v-x", "sentencing", "evt-123")
        row = must(store.get_hearing("us-v-x", "sentencing"))
        assert row["gcal_event_id"] == "evt-123"

    def test_set_m365_id_for_hearing_writes_and_clears(self, store: Store):
        store.upsert_hearing(_hearing())
        store.set_m365_id_for_hearing("us-v-x", "sentencing", "AAMk-EVT")
        # Read raw because get_hearing doesn't surface m365_event_id by default.
        row = store.conn.execute(
            "SELECT m365_event_id FROM hearings WHERE hearing_key=?",
            ("sentencing",),
        ).fetchone()
        assert row["m365_event_id"] == "AAMk-EVT"
        store.set_m365_id_for_hearing("us-v-x", "sentencing", None)
        row = store.conn.execute(
            "SELECT m365_event_id FROM hearings WHERE hearing_key=?",
            ("sentencing",),
        ).fetchone()
        assert row["m365_event_id"] is None

    def test_set_m365_id_for_deadline_writes_and_clears(self, store: Store):
        store.upsert_deadline(_deadline())
        store.set_m365_id_for_deadline(
            "anthropic-v-dow",
            "govt-response-mtd",
            "AAMk-DL",
        )
        row = store.conn.execute(
            "SELECT m365_event_id FROM deadlines WHERE deadline_key=?",
            ("govt-response-mtd",),
        ).fetchone()
        assert row["m365_event_id"] == "AAMk-DL"
        store.set_m365_id_for_deadline(
            "anthropic-v-dow",
            "govt-response-mtd",
            None,
        )
        row = store.conn.execute(
            "SELECT m365_event_id FROM deadlines WHERE deadline_key=?",
            ("govt-response-mtd",),
        ).fetchone()
        assert row["m365_event_id"] is None


class TestCaseSummaries:
    # case_summaries is keyed by the LOGICAL PACER docket
    # (case_id, docket_number, court_id), not the CourtListener docket_id —
    # see the AGENTS.md design decision on docket grouping.
    DKT = "1:25-cr-00307"
    DKT2 = "1:25-cr-00308"
    COURT = "vaed"

    def test_upsert_and_retrieve(self, store: Store):
        store.upsert_case_summary(
            "us-v-x",
            self.DKT,
            self.COURT,
            summary="The defendants are charged with...",
            model="anthropic/claude-sonnet-4-6",
            source_entry_ids=[10, 20],
        )
        row = must(store.get_docket_summary("us-v-x", self.DKT, self.COURT))
        assert row["summary"].startswith("The defendants")
        assert row["model"] == "anthropic/claude-sonnet-4-6"
        assert row["source_entry_ids"] == [10, 20]
        assert row["docket_number"] == self.DKT
        assert row["court_id"] == self.COURT

    def test_upsert_overwrites_existing(self, store: Store):
        store.upsert_case_summary(
            "us-v-x", self.DKT, self.COURT, summary="v1", model="m1"
        )
        store.upsert_case_summary(
            "us-v-x", self.DKT, self.COURT, summary="v2", model="m2"
        )
        row = must(store.get_docket_summary("us-v-x", self.DKT, self.COURT))
        assert row["summary"] == "v2"

    def test_get_docket_summary_missing_returns_none(self, store: Store):
        assert store.get_docket_summary("nope", self.DKT, self.COURT) is None

    def test_get_case_summaries_returns_all_groups(self, store: Store):
        store.upsert_case_summary(
            "us-v-x", self.DKT, self.COURT, summary="a", model="m"
        )
        store.upsert_case_summary(
            "us-v-x", self.DKT2, self.COURT, summary="b", model="m"
        )
        rows = store.get_case_summaries("us-v-x")
        assert {(r["docket_number"], r["court_id"]) for r in rows} == {
            (self.DKT, self.COURT),
            (self.DKT2, self.COURT),
        }

    def test_stale_lifecycle(self, store: Store):
        # New row is not stale; mark_summary_stale flips it; upsert resets.
        store.upsert_case_summary(
            "us-v-x", self.DKT, self.COURT, summary="v1", model="m"
        )
        assert store.is_summary_stale("us-v-x", self.DKT, self.COURT) is False
        store.mark_summary_stale("us-v-x", self.DKT, self.COURT)
        assert store.is_summary_stale("us-v-x", self.DKT, self.COURT) is True
        assert store.get_summary_stale_since("us-v-x", self.DKT, self.COURT) is not None
        # Upserting after a refresh resets stale flag + clears stale_since.
        store.upsert_case_summary(
            "us-v-x", self.DKT, self.COURT, summary="v2", model="m"
        )
        assert store.is_summary_stale("us-v-x", self.DKT, self.COURT) is False
        assert store.get_summary_stale_since("us-v-x", self.DKT, self.COURT) is None

    def test_missing_row_is_stale_by_definition(self, store: Store):
        # New cases never written get treated as stale so refresh_stale
        # creates a row on the next sync.
        assert store.is_summary_stale("never-summarized", self.DKT, self.COURT) is True

    def test_mark_summary_stale_on_missing_row_is_noop(self, store: Store):
        # No row exists -> UPDATE matches nothing; subsequent get returns None.
        store.mark_summary_stale("nope", self.DKT, self.COURT)
        assert store.get_summary_stale_since("nope", self.DKT, self.COURT) is None

    def test_get_case_summaries_handles_malformed_source_entry_ids(self, store: Store):
        # source_entry_ids stored as malformed JSON falls back to [].
        store.upsert_case_summary(
            "us-v-x", self.DKT, self.COURT, summary="v1", model="m"
        )
        store.conn.execute(
            "UPDATE case_summaries SET source_entry_ids=? WHERE case_id=?",
            ("not-json", "us-v-x"),
        )
        rows = store.get_case_summaries("us-v-x")
        assert rows[0]["source_entry_ids"] == []
        # Same fallback in get_docket_summary.
        row = must(store.get_docket_summary("us-v-x", self.DKT, self.COURT))
        assert row["source_entry_ids"] == []

    def test_pool_groups_one_summary_across_cl_splits(self, store: Store):
        # The canonical Akhter case: three CourtListener docket_ids share one
        # (docket_number, court_id). The summary lives on the GROUP, so
        # all three CourtListener ids round-trip to the same row.
        for did in (71989485, 73333500, 73320754):
            store.upsert_docket_meta(
                did,
                {
                    "court_id": self.COURT,
                    "docket_number": self.DKT,
                    "case_name": "US v. Akhter",
                    "absolute_url": "/x/",
                },
            )
        store.upsert_case_summary(
            "us-v-akhter",
            self.DKT,
            self.COURT,
            summary="pooled",
            model="m",
        )
        # Every CourtListener docket_id in the group resolves to the same summary.
        for did in (71989485, 73333500, 73320754):
            meta = must(store.get_docket_meta(did))
            row = must(
                store.get_docket_summary(
                    "us-v-akhter", meta["docket_number"], meta["court_id"]
                )
            )
            assert row["summary"] == "pooled"
        # And only ONE summary row exists for the case.
        assert len(store.get_case_summaries("us-v-akhter")) == 1

    def test_get_docket_group_ids(self, store: Store):
        for did in (71989485, 73333500, 73320754):
            store.upsert_docket_meta(
                did,
                {
                    "court_id": self.COURT,
                    "docket_number": self.DKT,
                    "case_name": "US v. Akhter",
                    "absolute_url": "/x/",
                },
            )
        # A different (docket_number, court_id) is correctly excluded.
        store.upsert_docket_meta(
            99,
            {
                "court_id": "cacd",
                "docket_number": "2:26-cr-99",
                "case_name": "other",
                "absolute_url": "/y/",
            },
        )
        ids = store.get_docket_group_ids(self.DKT, self.COURT)
        assert set(ids) == {71989485, 73333500, 73320754}
        assert 99 not in ids


class TestSummaryStalePostureChange:
    """upsert_hearing / upsert_deadline flag the docket's summary stale on a
    posture change — a new event, a status change (scheduled→held / cancelled
    / reinstated), or a reschedule — but NOT on a metadata-only re-save.

    This is the complement to the primary-document / disposition trigger in
    sync.process_entry (TestSummaryStaleMarkOnPrimaryOrDisposition). The
    end-of-sync verify and dedupe sweeps change a hearing's posture WITHOUT
    any new document entry, so before this hook a verify-pass MARK_HELD (an
    oral argument flipped to 'held' the day after it happened) left the
    summary frozen at its pre-event prose. The canonical regression is
    anthropic-v-dow 26-1049 (D.C. Cir.).
    """

    DKT = "26-1049"
    COURT = "cadc"
    HDID = 12345  # _hearing() default docket_id
    DLID = 72380208  # _deadline() default docket_id

    def _cache_docket(self, store: Store, docket_id: int):
        store.upsert_docket_meta(
            docket_id,
            {
                "court_id": self.COURT,
                "docket_number": self.DKT,
                "case_name": "Anthropic PBC v. United States Department of War",
                "absolute_url": "/docket/x/",
            },
        )

    def _seed_summary(self, store: Store, case_id: str):
        # Reset stale=0; call AFTER any seeding upsert so the precondition
        # holds regardless of what the seed itself flipped.
        store.upsert_case_summary(
            case_id, self.DKT, self.COURT, summary="pre-event prose", model="m"
        )
        assert store.is_summary_stale(case_id, self.DKT, self.COURT) is False

    # --- hearings ---

    def test_status_change_flags_stale(self, store: Store):
        # The anthropic-v-dow regression: scheduled oral argument → 'held'.
        self._cache_docket(store, self.HDID)
        store.upsert_hearing(_hearing(status="scheduled"))
        self._seed_summary(store, "us-v-x")
        store.upsert_hearing(_hearing(status="held"))
        assert store.is_summary_stale("us-v-x", self.DKT, self.COURT) is True

    def test_reschedule_flags_stale(self, store: Store):
        self._cache_docket(store, self.HDID)
        store.upsert_hearing(_hearing())
        self._seed_summary(store, "us-v-x")
        store.upsert_hearing(_hearing(starts_at_utc="2026-09-01T15:00:00+00:00"))
        assert store.is_summary_stale("us-v-x", self.DKT, self.COURT) is True

    def test_new_event_flags_existing_summary_stale(self, store: Store):
        self._cache_docket(store, self.HDID)
        self._seed_summary(store, "us-v-x")
        # A brand-new hearing key on a docket that already has a summary.
        store.upsert_hearing(_hearing(key="oral-arg", status="scheduled"))
        assert store.is_summary_stale("us-v-x", self.DKT, self.COURT) is True

    def test_metadata_only_change_does_not_flag_stale(self, store: Store):
        # Same status, same starts_at_utc — only notes / source_entry_ids
        # differ (the dedupe-target source-id merge shape). No churn.
        self._cache_docket(store, self.HDID)
        store.upsert_hearing(_hearing())
        self._seed_summary(store, "us-v-x")
        store.upsert_hearing(_hearing(notes="dial-in changed", source_entry_ids=[1, 2]))
        assert store.is_summary_stale("us-v-x", self.DKT, self.COURT) is False

    def test_incoming_null_docket_resolves_via_prior_row(self, store: Store):
        # A sibling-docket CANCEL_HEARING passes docket_id=None (sticky-docket logic);
        # the flip must still resolve the docket via the prior row.
        self._cache_docket(store, self.HDID)
        store.upsert_hearing(_hearing(docket_id=self.HDID, status="scheduled"))
        self._seed_summary(store, "us-v-x")
        store.upsert_hearing(_hearing(docket_id=None, status="cancelled"))
        assert store.is_summary_stale("us-v-x", self.DKT, self.COURT) is True

    def test_no_docket_meta_is_noop(self, store: Store):
        # Hearing's docket was never cached → get_docket_meta is None → the
        # flip is a silent no-op (the document-trigger path still covers it).
        store.upsert_hearing(_hearing(status="scheduled"))
        self._seed_summary(store, "us-v-x")
        store.upsert_hearing(_hearing(status="held"))
        assert store.is_summary_stale("us-v-x", self.DKT, self.COURT) is False

    def test_meta_without_court_id_is_noop(self, store: Store):
        # Warm-up shape: docket row exists but court_id not yet known.
        store.upsert_docket_meta(
            self.HDID,
            {
                "court_id": None,
                "docket_number": self.DKT,
                "case_name": "x",
                "absolute_url": "/x/",
            },
        )
        store.upsert_hearing(_hearing(status="scheduled"))
        self._seed_summary(store, "us-v-x")
        store.upsert_hearing(_hearing(status="held"))
        assert store.is_summary_stale("us-v-x", self.DKT, self.COURT) is False

    def test_new_event_with_null_docket_is_noop(self, store: Store):
        # docket_id None on a brand-new row → nothing to resolve, no crash.
        self._cache_docket(store, self.HDID)
        self._seed_summary(store, "us-v-x")
        store.upsert_hearing(_hearing(key="ghost", docket_id=None))
        assert store.is_summary_stale("us-v-x", self.DKT, self.COURT) is False

    # --- deadlines (parallel structure) ---

    def test_deadline_status_change_flags_stale(self, store: Store):
        self._cache_docket(store, self.DLID)
        store.upsert_deadline(_deadline(status="pending"))
        self._seed_summary(store, "anthropic-v-dow")
        store.upsert_deadline(_deadline(status="met"))
        assert store.is_summary_stale("anthropic-v-dow", self.DKT, self.COURT) is True

    def test_deadline_reschedule_flags_stale(self, store: Store):
        self._cache_docket(store, self.DLID)
        store.upsert_deadline(_deadline())
        self._seed_summary(store, "anthropic-v-dow")
        store.upsert_deadline(_deadline(due_at_utc="2026-06-07T21:00:00+00:00"))
        assert store.is_summary_stale("anthropic-v-dow", self.DKT, self.COURT) is True

    def test_deadline_metadata_only_change_does_not_flag_stale(self, store: Store):
        self._cache_docket(store, self.DLID)
        store.upsert_deadline(_deadline())
        self._seed_summary(store, "anthropic-v-dow")
        store.upsert_deadline(_deadline(notes="clarified", source_entry_ids=[1, 9]))
        assert store.is_summary_stale("anthropic-v-dow", self.DKT, self.COURT) is False

    def test_deadline_incoming_null_docket_resolves_via_prior_row(self, store: Store):
        # Mirror of the hearing case: a sibling-docket CANCEL_HEARING passes
        # docket_id=None; the flip resolves the docket via the prior row.
        self._cache_docket(store, self.DLID)
        store.upsert_deadline(_deadline(docket_id=self.DLID, status="pending"))
        self._seed_summary(store, "anthropic-v-dow")
        store.upsert_deadline(_deadline(docket_id=None, status="cancelled"))
        assert store.is_summary_stale("anthropic-v-dow", self.DKT, self.COURT) is True


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
        s.upsert_docket_meta(
            1,
            {
                "court_id": "mad",
                "docket_number": "1:25",
                "case_name": "X",
                "absolute_url": "/x/",
            },
        )
        s.mark_entry(1, 100, "2026-01-01", "fp", date_filed="2026-01-01")
        assert must(s.get_docket_meta(1))["court_id"] == "mad"
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


class TestCaseSummariesGroupMigration:
    """Migration that re-keys case_summaries from (case_id, docket_id) to
    (case_id, docket_number, court_id). Non-destructive — the old table is
    renamed to case_summaries_pre_group_migration as a rollback escape hatch.
    """

    def _seed_pre_migration_db(self, path):
        """Build a DB carrying the pre-group case_summaries shape."""
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        c.executescript("""
            CREATE TABLE dockets (
                docket_id INTEGER PRIMARY KEY,
                date_modified TEXT,
                last_synced_at TEXT NOT NULL,
                court_id TEXT,
                docket_number TEXT,
                case_name TEXT,
                absolute_url TEXT,
                date_last_filing TEXT
            );
            CREATE TABLE case_summaries (
                case_id TEXT NOT NULL,
                docket_id INTEGER NOT NULL,
                summary TEXT NOT NULL,
                model TEXT,
                source_entry_ids TEXT,
                stale INTEGER NOT NULL DEFAULT 0,
                stale_since TEXT,
                generated_at TEXT NOT NULL,
                PRIMARY KEY (case_id, docket_id)
            );
        """)
        return c

    def test_backfills_docket_number_and_court_id(self, tmp_path):
        path = tmp_path / "old.sqlite"
        c = self._seed_pre_migration_db(path)
        c.execute(
            "INSERT INTO dockets VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, "2026-01-01", "2026-01-01", "vaed", "1:25-cr-1", "X", "/x/", None),
        )
        c.execute(
            "INSERT INTO case_summaries "
            "(case_id, docket_id, summary, model, source_entry_ids, stale, "
            "stale_since, generated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("us-v-x", 1, "v1", "m", "[]", 0, None, "2026-01-01T00:00:00+00:00"),
        )
        c.commit()
        c.close()

        # Migration runs automatically on Store init.
        s = Store(path)
        try:
            row = must(s.get_docket_summary("us-v-x", "1:25-cr-1", "vaed"))
            assert row["summary"] == "v1"
            # The pre-migration table is preserved as the rollback escape hatch.
            tables = {
                r["name"]
                for r in s.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "case_summaries_pre_group_migration" in tables
        finally:
            s.close()

    def test_collision_keeps_newest_generated_at(self, tmp_path):
        # Three CourtListener docket_ids sharing one (docket_number, court_id) —
        # the migration must collapse to ONE row and keep the newest.
        path = tmp_path / "akhter.sqlite"
        c = self._seed_pre_migration_db(path)
        for did in (71989485, 73333500, 73320754):
            c.execute(
                "INSERT INTO dockets VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    did,
                    "2026-05-15",
                    "2026-05-15",
                    "vaed",
                    "1:25-cr-00307",
                    "United States v. Akhter",
                    "/x/",
                    None,
                ),
            )
        c.executemany(
            "INSERT INTO case_summaries "
            "(case_id, docket_id, summary, model, source_entry_ids, stale, "
            "stale_since, generated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "us-v-akhter",
                    71989485,
                    "older",
                    "m",
                    "[]",
                    0,
                    None,
                    "2026-05-14T00:00:00+00:00",
                ),
                (
                    "us-v-akhter",
                    73320754,
                    "middle",
                    "m",
                    "[]",
                    0,
                    None,
                    "2026-05-15T00:00:00+00:00",
                ),
                (
                    "us-v-akhter",
                    73333500,
                    "newest",
                    "m",
                    "[]",
                    0,
                    None,
                    "2026-05-16T00:00:00+00:00",
                ),
            ],
        )
        c.commit()
        c.close()

        s = Store(path)
        try:
            rows = s.get_case_summaries("us-v-akhter")
            # Three CourtListener docket_ids → ONE summary row post-migration.
            assert len(rows) == 1
            assert rows[0]["summary"] == "newest"
        finally:
            s.close()

    def test_orphan_summary_is_skipped_with_warning(self, tmp_path, caplog):
        # A case_summaries row whose docket_id has no matching dockets row
        # (sync interrupted mid-write) gets skipped — there's no way to
        # resolve (docket_number, court_id). The orphan stays in the
        # pre-migration aside table so an operator can investigate.
        path = tmp_path / "orphan.sqlite"
        c = self._seed_pre_migration_db(path)
        c.execute(
            "INSERT INTO case_summaries "
            "(case_id, docket_id, summary, model, source_entry_ids, stale, "
            "stale_since, generated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "us-v-orphan",
                999,
                "stranded",
                "m",
                "[]",
                0,
                None,
                "2026-05-01T00:00:00+00:00",
            ),
        )
        c.commit()
        c.close()

        with caplog.at_level("WARNING", logger="case_calendar.store"):
            s = Store(path)
        try:
            # Orphan dropped from the new table.
            assert s.get_case_summaries("us-v-orphan") == []
            # Preserved in the aside table.
            row = s.conn.execute(
                "SELECT summary FROM case_summaries_pre_group_migration "
                "WHERE case_id=? AND docket_id=?",
                ("us-v-orphan", 999),
            ).fetchone()
            assert row is not None and row["summary"] == "stranded"
            assert "orphan summary" in caplog.text
        finally:
            s.close()

    def test_idempotent_re_open(self, tmp_path):
        # Open once (runs migration), close, open again — the second
        # Store() construction must not error on the already-migrated
        # shape (detection short-circuits at the top).
        path = tmp_path / "x.sqlite"
        c = self._seed_pre_migration_db(path)
        c.execute(
            "INSERT INTO dockets VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, "2026-01-01", "2026-01-01", "vaed", "1:25-cr-1", "X", "/x/", None),
        )
        c.execute(
            "INSERT INTO case_summaries "
            "(case_id, docket_id, summary, model, source_entry_ids, stale, "
            "stale_since, generated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("us-v-x", 1, "v1", "m", "[]", 0, None, "2026-01-01T00:00:00+00:00"),
        )
        c.commit()
        c.close()
        Store(path).close()
        # Second open must succeed.
        s = Store(path)
        try:
            assert (
                must(s.get_docket_summary("us-v-x", "1:25-cr-1", "vaed"))["summary"]
                == "v1"
            )
        finally:
            s.close()


class TestSplitAuditSegments:
    """``_split_audit_segments`` is the migration helper that separates
    pipeline-synthesized audit paragraphs (``[verify-pass]`` / ``[dedupe]``)
    from docket-derived ``notes``. Both writers AND the migration rely on
    its exact semantics; an over-eager split would drop real court text,
    an under-eager one would leave self-confirming audit text in ``notes``
    where the verify-pass LLM reads it.
    """

    def test_none_returns_empty_pair(self):
        from case_calendar.store import _split_audit_segments

        assert _split_audit_segments(None) == ("", "")
        assert _split_audit_segments("") == ("", "")

    def test_no_audit_prefix_passes_notes_through_unchanged(self):
        from case_calendar.store import _split_audit_segments

        notes = "Trial commences June 12, 2024. Pretrial deadlines: ..."
        clean, audit = _split_audit_segments(notes)
        assert clean == notes
        assert audit == ""

    def test_verify_pass_paragraph_moves(self):
        from case_calendar.store import _split_audit_segments

        notes = (
            "Trial commences June 12, 2024.\n\n"
            "[verify-pass] Cancellation not supported by docket; reverted."
        )
        clean, audit = _split_audit_segments(notes)
        assert clean == "Trial commences June 12, 2024."
        assert audit == (
            "[verify-pass] Cancellation not supported by docket; reverted."
        )

    def test_dedupe_paragraph_moves(self):
        from case_calendar.store import _split_audit_segments

        notes = "Motion hearing on MSJ.\n\n[dedupe] Merged into msj-hearing: same slot"
        clean, audit = _split_audit_segments(notes)
        assert clean == "Motion hearing on MSJ."
        assert audit == "[dedupe] Merged into msj-hearing: same slot"

    def test_multiple_audit_paragraphs_concatenated(self):
        from case_calendar.store import _split_audit_segments

        notes = (
            "Original scheduling order text.\n\n"
            "[verify-pass] First reschedule per entry 65.\n\n"
            "[verify-pass] Second reschedule per entry 88.\n\n"
            "[dedupe] Merged into other-key: same slot"
        )
        clean, audit = _split_audit_segments(notes)
        assert clean == "Original scheduling order text."
        # Order preserved chronologically.
        assert audit == (
            "[verify-pass] First reschedule per entry 65.\n\n"
            "[verify-pass] Second reschedule per entry 88.\n\n"
            "[dedupe] Merged into other-key: same slot"
        )

    def test_un_prefixed_brackets_stay_in_notes(self):
        # The McGonigal-shape legacy hallucination: bracketed paragraph
        # without a [verify-pass] / [dedupe] tag. We must NOT auto-move
        # these — they might be real court text that happens to contain
        # brackets. Manual cleanup only.
        from case_calendar.store import _split_audit_segments

        notes = (
            "Trial commences June 12, 2024.\n\n"
            "[Trial vacated by guilty plea entered 8/15/2023.]"
        )
        clean, audit = _split_audit_segments(notes)
        assert clean == notes  # whole string preserved
        assert audit == ""

    def test_inline_brackets_inside_paragraph_stay_inline(self):
        # A docket entry's notes may contain inline bracket references
        # like "[1]" or "[Doc. 65]". Only paragraphs that LEAD with the
        # audit prefix are moved.
        from case_calendar.store import _split_audit_segments

        notes = "Order [Doc. 65] resets trial to 6/12/2024."
        clean, audit = _split_audit_segments(notes)
        assert clean == notes
        assert audit == ""


class TestAuditNotesMigration:
    """The store opens with a migration that moves existing
    ``[verify-pass]`` / ``[dedupe]`` paragraphs out of ``notes`` into
    the new ``audit_notes`` column. Tests cover the migration AND the
    runtime contract that subsequent writes preserve the separation.
    """

    def test_legacy_notes_get_split_on_open(self, tmp_path):
        # Pre-migration DB: hearings table without audit_notes column,
        # notes containing a [verify-pass] paragraph.
        path = tmp_path / "legacy.sqlite"
        c = sqlite3.connect(path)
        c.executescript("""
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
            CREATE TABLE deadlines (
                case_id TEXT NOT NULL,
                deadline_key TEXT NOT NULL,
                title TEXT NOT NULL,
                due_at_utc TEXT,
                timezone TEXT NOT NULL,
                notes TEXT,
                status TEXT NOT NULL,
                gcal_event_id TEXT,
                source_entry_ids TEXT NOT NULL,
                last_updated TEXT NOT NULL,
                PRIMARY KEY (case_id, deadline_key)
            );
        """)
        c.execute(
            "INSERT INTO hearings VALUES "
            "('us-v-x', 'trial', 'Trial', '2024-06-12T14:00:00+00:00', "
            "240, 'America/New_York', NULL, NULL, "
            "'Trial commences June 12, 2024.\n\n"
            "[verify-pass] Cancellation not supported per recent entries.', "
            "NULL, 'scheduled', NULL, '[1]', '2026-01-01T00:00:00+00:00')"
        )
        c.execute(
            "INSERT INTO deadlines VALUES "
            "('us-v-x', 'reply-mtd', 'Reply', '2026-02-01T22:00:00+00:00', "
            "'America/New_York', "
            "'Reply due 2/1/2026.\n\n[verify-pass] Extended per docket.', "
            "'pending', NULL, '[1]', '2026-01-01T00:00:00+00:00')"
        )
        c.commit()
        c.close()

        s = Store(path)
        h = must(s.get_hearing("us-v-x", "trial"))
        assert h["notes"] == "Trial commences June 12, 2024."
        assert h["audit_notes"] == (
            "[verify-pass] Cancellation not supported per recent entries."
        )
        d = s.get_deadlines("us-v-x")[0]
        assert d["notes"] == "Reply due 2/1/2026."
        assert d["audit_notes"] == "[verify-pass] Extended per docket."
        s.close()

    def test_inline_audit_marker_in_paragraph_body_is_left_alone(self, tmp_path):
        # The SQL pre-filter (`notes LIKE '%[verify-pass]%'`) catches
        # any row whose notes contain the literal marker, but the
        # _split_audit_segments helper only moves paragraphs that
        # *lead* with the marker. A docket note that mentions
        # `[verify-pass]` mid-sentence — say, a clerk quoting it back
        # in their own prose — should be left untouched. The migration
        # `continue`s when nothing is extracted, leaving the row as-is.
        path = tmp_path / "inline.sqlite"
        c = sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE hearings (
                case_id TEXT NOT NULL, hearing_key TEXT NOT NULL,
                title TEXT NOT NULL, starts_at_utc TEXT,
                duration_minutes INTEGER, timezone TEXT NOT NULL,
                location TEXT, judge TEXT, notes TEXT, dial_in TEXT,
                status TEXT NOT NULL, gcal_event_id TEXT,
                source_entry_ids TEXT NOT NULL, last_updated TEXT NOT NULL,
                PRIMARY KEY (case_id, hearing_key)
            );
            CREATE TABLE deadlines (
                case_id TEXT NOT NULL, deadline_key TEXT NOT NULL,
                title TEXT NOT NULL, due_at_utc TEXT,
                timezone TEXT NOT NULL, notes TEXT,
                status TEXT NOT NULL, gcal_event_id TEXT,
                source_entry_ids TEXT NOT NULL, last_updated TEXT NOT NULL,
                PRIMARY KEY (case_id, deadline_key)
            );
        """)
        c.execute(
            "INSERT INTO hearings VALUES "
            "('us-v-x', 'trial', 'Trial', '2024-06-12T14:00:00+00:00', "
            "240, 'America/New_York', NULL, NULL, "
            "'Trial commences after [verify-pass]-style audit completes.', "
            "NULL, 'scheduled', NULL, '[1]', '2026-01-01T00:00:00+00:00')"
        )
        c.commit()
        c.close()

        s = Store(path)
        h = must(s.get_hearing("us-v-x", "trial"))
        # The marker stays in the notes — it's not at the start of a
        # paragraph so it's not a real audit segment.
        assert h["notes"] == (
            "Trial commences after [verify-pass]-style audit completes."
        )
        # And audit_notes is empty (nothing got extracted).
        assert (h.get("audit_notes") or "") == ""
        s.close()

    def test_migration_is_idempotent(self, tmp_path):
        # Running open twice mustn't double-split: by the second open the
        # audit text already lives in audit_notes, so notes contains no
        # [verify-pass] paragraph and the migration finds nothing to move.
        path = tmp_path / "idem.sqlite"
        c = sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE hearings (
                case_id TEXT NOT NULL, hearing_key TEXT NOT NULL,
                title TEXT NOT NULL, starts_at_utc TEXT,
                duration_minutes INTEGER, timezone TEXT NOT NULL,
                location TEXT, judge TEXT, notes TEXT, dial_in TEXT,
                status TEXT NOT NULL, gcal_event_id TEXT,
                source_entry_ids TEXT NOT NULL, last_updated TEXT NOT NULL,
                PRIMARY KEY (case_id, hearing_key)
            );
            CREATE TABLE deadlines (
                case_id TEXT NOT NULL, deadline_key TEXT NOT NULL,
                title TEXT NOT NULL, due_at_utc TEXT,
                timezone TEXT NOT NULL, notes TEXT, status TEXT NOT NULL,
                gcal_event_id TEXT, source_entry_ids TEXT NOT NULL,
                last_updated TEXT NOT NULL,
                PRIMARY KEY (case_id, deadline_key)
            );
        """)
        c.execute(
            "INSERT INTO hearings VALUES "
            "('us-v-x', 'trial', 'Trial', '2024-06-12T14:00:00+00:00', "
            "240, 'America/New_York', NULL, NULL, "
            "'Court text.\n\n[verify-pass] Reason A.', "
            "NULL, 'scheduled', NULL, '[1]', '2026-01-01T00:00:00+00:00')"
        )
        c.commit()
        c.close()

        s = Store(path)
        s.close()
        # Second open: nothing to migrate, results unchanged.
        s2 = Store(path)
        h = must(s2.get_hearing("us-v-x", "trial"))
        assert h["notes"] == "Court text."
        assert h["audit_notes"] == "[verify-pass] Reason A."
        s2.close()

    def test_pre_existing_audit_notes_preserved_during_migration(self, tmp_path):
        # If a row already has audit_notes set (from a prior write) AND
        # notes still contains a legacy [verify-pass] paragraph, the
        # migration appends rather than overwrites.
        path = tmp_path / "mixed.sqlite"
        s = Store(path)  # fresh DB, all columns present
        s.conn.execute(
            "INSERT INTO hearings (case_id, hearing_key, title, starts_at_utc, "
            "duration_minutes, timezone, notes, audit_notes, status, "
            "source_entry_ids, last_updated) VALUES "
            "('us-v-x', 'trial', 'Trial', '2024-06-12T14:00:00+00:00', "
            "240, 'America/New_York', "
            "'Court text.\n\n[verify-pass] Stale reason.', "
            "'[verify-pass] Already-extracted.', "
            "'scheduled', '[1]', '2026-01-01T00:00:00+00:00')"
        )
        s.conn.commit()
        # Re-run the migration in place — production opens hit it
        # automatically on every Store() construction.
        s._migrate_audit_segments()
        h = must(s.get_hearing("us-v-x", "trial"))
        assert h["notes"] == "Court text."
        assert "Already-extracted" in h["audit_notes"]
        assert "Stale reason" in h["audit_notes"]
        s.close()


class TestPruneHelpers:
    def _seed_docket(
        self,
        store: Store,
        docket_id: int,
        *,
        case_id: str = "us-v-x",
    ) -> None:
        # Full row set: dockets meta + one entry + one hearing + one deadline
        # + one case_summary, all keyed on docket_id. Mirrors what a normal
        # sync leaves on disk for one docket.
        store.upsert_docket_meta(
            docket_id,
            {
                "court_id": "dcd",
                "docket_number": f"1:24-cr-{docket_id:05d}",
                "case_name": f"US v. Docket {docket_id}",
                "absolute_url": None,
                "date_last_filing": None,
            },
        )
        entry_id = docket_id * 100 + 1
        store.mark_entry(
            docket_id=docket_id,
            entry_id=entry_id,
            date_modified="2026-01-01T00:00:00+00:00",
            fingerprint="fp",
            entry_number=1,
            date_filed="2026-01-01",
            description="Indictment",
            short_description="Indictment",
            recap_documents=[],
        )
        store.upsert_hearing(
            {
                "case_id": case_id,
                "hearing_key": f"sentencing-{docket_id}",
                "title": "Sentencing",
                "starts_at_utc": "2099-01-01T00:00:00+00:00",
                "duration_minutes": 60,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": docket_id,
                "source_entry_ids": [entry_id],
            }
        )
        store.upsert_deadline(
            {
                "case_id": case_id,
                "deadline_key": f"reply-{docket_id}",
                "title": "Reply ISO MTD",
                "due_at_utc": "2099-01-15T22:00:00+00:00",
                "timezone": "America/New_York",
                "status": "pending",
                "significance": "major",
                "deadline_type": "reply",
                "docket_id": docket_id,
                "source_entry_ids": [entry_id],
            }
        )
        store.upsert_case_summary(
            case_id=case_id,
            docket_number=f"1:24-cr-{docket_id:05d}",
            court_id="dcd",
            summary="text",
            model="anthropic/test",
            source_entry_ids=[entry_id],
        )
        store.conn.commit()

    def test_list_all_docket_ids_includes_dockets_and_child_orphans(
        self,
        store: Store,
    ):
        # Two dockets with full metadata + a child-only orphan whose dockets
        # row never landed (sync interrupted between mark_entry and
        # upsert_docket_meta). The child row alone should still surface so
        # prune can sweep it.
        self._seed_docket(store, 100)
        self._seed_docket(store, 200)
        store.mark_entry(
            docket_id=300,
            entry_id=30001,
            date_modified="2026-01-01T00:00:00+00:00",
            fingerprint="fp",
            entry_number=1,
            date_filed="2026-01-01",
            description="x",
            short_description="x",
            recap_documents=[],
        )
        store.conn.commit()
        assert store.list_all_docket_ids() == [100, 200, 300]

    def test_list_all_docket_ids_empty_store(self, store: Store):
        assert store.list_all_docket_ids() == []

    def test_count_docket_rows_per_table(self, store: Store):
        self._seed_docket(store, 100)
        counts = store.count_docket_rows(100)
        assert counts == {
            "entries": 1,
            "hearings": 1,
            "deadlines": 1,
            "case_summaries": 1,
            "dockets": 1,
        }

    def test_count_docket_rows_unknown_id_is_all_zero(self, store: Store):
        assert store.count_docket_rows(999) == {
            "entries": 0,
            "hearings": 0,
            "deadlines": 0,
            "case_summaries": 0,
            "dockets": 0,
        }

    def test_delete_docket_removes_every_referenced_row(self, store: Store):
        self._seed_docket(store, 100)
        self._seed_docket(store, 200)
        deleted = store.delete_docket(100)
        assert deleted == {
            "entries": 1,
            "hearings": 1,
            "deadlines": 1,
            "case_summaries": 1,
            "dockets": 1,
        }
        # Sibling docket 200 untouched.
        assert store.list_all_docket_ids() == [200]
        assert store.count_docket_rows(100) == {
            "entries": 0,
            "hearings": 0,
            "deadlines": 0,
            "case_summaries": 0,
            "dockets": 0,
        }
        # Tx commits inside delete_docket — close+reopen still shows the
        # deletion (catches a "forgot to commit" regression).
        path = store.path
        store.close()
        s2 = Store(path)
        try:
            assert s2.list_all_docket_ids() == [200]
        finally:
            s2.close()

    def _seed_docket_in_group(
        self,
        store: Store,
        docket_id: int,
        *,
        docket_number: str,
        case_id: str = "us-v-akhter",
    ) -> None:
        # Sibling docket variant of _seed_docket — sibling CourtListener docket_ids
        # share `(docket_number, court_id)` so they belong to one logical
        # PACER docket group. Used for the case-summaries-stay-with-
        # surviving-siblings tests.
        store.upsert_docket_meta(
            docket_id,
            {
                "court_id": "vaed",
                "docket_number": docket_number,
                "case_name": "United States v. Akhter",
                "absolute_url": None,
                "date_last_filing": None,
            },
        )
        entry_id = docket_id * 100 + 1
        store.mark_entry(
            docket_id=docket_id,
            entry_id=entry_id,
            date_modified="2026-01-01T00:00:00+00:00",
            fingerprint="fp",
            entry_number=1,
            date_filed="2026-01-01",
            description="Indictment",
            short_description="Indictment",
            recap_documents=[],
        )
        store.conn.commit()

    def test_count_docket_rows_skips_case_summaries_when_group_has_siblings(
        self, store: Store
    ):
        # Two CourtListener docket_ids share one logical PACER docket — case_summaries
        # is keyed by (docket_number, court_id), so deleting docket_id=100
        # alone would NOT orphan the summary (docket_id=101 is still in the
        # group). count_docket_rows therefore reports case_summaries=0 for
        # the non-last sibling — the prune preview correctly shows nothing
        # would be lost. Without this skip the count would double-count.
        docket_number = "1:25-cr-00307"
        self._seed_docket_in_group(store, 100, docket_number=docket_number)
        self._seed_docket_in_group(store, 101, docket_number=docket_number)
        store.upsert_case_summary(
            case_id="us-v-akhter",
            docket_number=docket_number,
            court_id="vaed",
            summary="indictment text",
            model="anthropic/test",
            source_entry_ids=[10001],
        )
        store.conn.commit()
        # 100 has a sibling (101) — case_summaries=0 because deleting 100
        # alone wouldn't orphan the summary.
        counts = store.count_docket_rows(100)
        assert counts["case_summaries"] == 0
        # 101 also still has a sibling (100) — same answer.
        assert store.count_docket_rows(101)["case_summaries"] == 0

    def test_count_docket_rows_for_docket_id_without_metadata(self, store: Store):
        # A child-only orphan row (mark_entry was called but
        # upsert_docket_meta wasn't, e.g. sync interrupted between the
        # two) has no `dockets` metadata. The case_summaries count
        # branch must short-circuit cleanly to 0 instead of running the
        # group-size query with NULL docket_number / court_id.
        store.mark_entry(
            docket_id=500,
            entry_id=50001,
            date_modified="2026-01-01T00:00:00+00:00",
            fingerprint="fp",
            entry_number=1,
            date_filed="2026-01-01",
            description="orphan",
            short_description="orphan",
            recap_documents=[],
        )
        store.conn.commit()
        counts = store.count_docket_rows(500)
        assert counts["entries"] == 1
        assert counts["dockets"] == 0
        assert counts["case_summaries"] == 0

    def test_delete_docket_preserves_case_summary_when_group_has_siblings(
        self, store: Store
    ):
        # Same shape as the count test: deleting one CourtListener docket_id out of a
        # multi-sibling group must NOT delete the case_summaries row,
        # because the summary belongs to the LOGICAL PACER docket and a
        # surviving sibling still references it. Deleting the LAST
        # sibling then DOES delete the summary (group is empty).
        docket_number = "1:25-cr-00307"
        self._seed_docket_in_group(store, 100, docket_number=docket_number)
        self._seed_docket_in_group(store, 101, docket_number=docket_number)
        store.upsert_case_summary(
            case_id="us-v-akhter",
            docket_number=docket_number,
            court_id="vaed",
            summary="indictment text",
            model="anthropic/test",
            source_entry_ids=[10001],
        )
        store.conn.commit()

        # Delete the first sibling — the case_summary survives because
        # docket_id=101 is still in the group.
        deleted_first = store.delete_docket(100)
        assert deleted_first["dockets"] == 1
        assert deleted_first["case_summaries"] == 0
        assert (
            store.get_docket_summary("us-v-akhter", docket_number, "vaed") is not None
        )

        # Delete the last sibling — now the case_summary IS removed
        # because the group has no surviving members.
        deleted_last = store.delete_docket(101)
        assert deleted_last["case_summaries"] == 1
        assert store.get_docket_summary("us-v-akhter", docket_number, "vaed") is None

    def test_delete_docket_with_no_metadata_skips_case_summary_logic(
        self, store: Store
    ):
        # The other partial branch on delete_docket: an orphan docket_id
        # with no `dockets` metadata. The case_summaries cleanup is
        # gated on `meta and meta.get("docket_number") and
        # meta.get("court_id")`, so this path skips the cleanup
        # entirely without crashing on missing fields.
        store.mark_entry(
            docket_id=600,
            entry_id=60001,
            date_modified="2026-01-01T00:00:00+00:00",
            fingerprint="fp",
            entry_number=1,
            date_filed="2026-01-01",
            description="orphan",
            short_description="orphan",
            recap_documents=[],
        )
        store.conn.commit()
        deleted = store.delete_docket(600)
        assert deleted["entries"] == 1
        assert deleted["case_summaries"] == 0

    def test_delete_docket_handles_child_only_orphan(self, store: Store):
        # No dockets row, only a child entry — delete still cleans it up
        # and reports dockets=0 for the row that wasn't there.
        store.mark_entry(
            docket_id=300,
            entry_id=30001,
            date_modified="2026-01-01T00:00:00+00:00",
            fingerprint="fp",
            entry_number=1,
            date_filed="2026-01-01",
            description="x",
            short_description="x",
            recap_documents=[],
        )
        store.conn.commit()
        deleted = store.delete_docket(300)
        assert deleted["entries"] == 1
        assert deleted["dockets"] == 0
        assert store.list_all_docket_ids() == []


class TestSingularDefendantBase:
    """``_singular_defendant_base`` collapses a once-only-proceeding key to a
    ``type|defendant`` cluster id (or None for repeatable / non-singular
    types), so the near-slot sweep groups ``sentencing-mcgonigal`` with
    ``sentencing-mcgonigal-2`` but never a status conference."""

    def test_singular_with_and_without_sequence_suffix(self):
        from case_calendar.store import _singular_defendant_base as b

        assert b("sentencing-mcgonigal") == "sentencing|mcgonigal"
        assert b("sentencing-mcgonigal-2") == "sentencing|mcgonigal"
        assert b("change-of-plea-mcgonigal-2") == "change-of-plea|mcgonigal"
        assert b("arraignment-akhter-3") == "arraignment|akhter"
        # Bare type with no defendant tail collapses to a stable "(unnamed)".
        assert b("sentencing") == "sentencing|(unnamed)"

    def test_repeatable_and_unknown_types_return_none(self):
        from case_calendar.store import _singular_defendant_base as b

        assert b("status-conf-mcgonigal-2") is None
        assert b("motion-hearing-ding-3") is None
        assert b("trial-wei") is None
        assert b("oral-arg") is None

    def test_distinct_defendants_get_distinct_bases(self):
        from case_calendar.store import _singular_defendant_base as b

        assert b("sentencing-wang") != b("sentencing-prince")


class TestNearslotClusters:
    """``find_nearslot_hearing_clusters`` — the candidates the exact-slot
    sweeps miss: same court-day at different times, and the same once-only
    proceeding at drifted dates. Genuinely-distinct rows must NOT cluster."""

    def _keys(self, clusters):
        return sorted(sorted(h["hearing_key"] for h in c) for c in clusters)

    def test_same_day_different_time_clusters(self, store: Store):
        # Two held CIPA rows on the same court day, different times — the
        # date-only-vs-timed duplicate shape.
        store.upsert_hearing(
            _hearing(
                key="cipa-mcgonigal",
                status="held",
                starts_at_utc="2023-03-08T05:00:00+00:00",
            )
        )
        store.upsert_hearing(
            _hearing(
                key="cipa-mcgonigal-3-6",
                status="held",
                starts_at_utc="2023-03-08T18:00:00+00:00",
            )
        )
        clusters = store.find_nearslot_hearing_clusters("us-v-x")
        assert self._keys(clusters) == [["cipa-mcgonigal", "cipa-mcgonigal-3-6"]]

    def test_exact_same_slot_is_excluded(self, store: Store):
        # Identical starts_at_utc is the EXACT-slot sweeps' job, not this one:
        # only one distinct slot in the bucket, so no near-slot cluster.
        store.upsert_hearing(
            _hearing(
                key="cipa-a", status="held", starts_at_utc="2023-03-08T18:00:00+00:00"
            )
        )
        store.upsert_hearing(
            _hearing(
                key="cipa-b", status="held", starts_at_utc="2023-03-08T18:00:00+00:00"
            )
        )
        assert store.find_nearslot_hearing_clusters("us-v-x") == []

    def test_singular_type_clusters_across_dates(self, store: Store):
        # Sentencing recorded at its scheduled date AND the held date — a
        # once-only proceeding, so cluster despite the 4-day gap.
        store.upsert_hearing(
            _hearing(
                key="sentencing-mcgonigal",
                status="held",
                starts_at_utc="2023-12-18T05:00:00+00:00",
            )
        )
        store.upsert_hearing(
            _hearing(
                key="sentencing-mcgonigal-2",
                status="held",
                starts_at_utc="2023-12-14T18:30:00+00:00",
            )
        )
        clusters = store.find_nearslot_hearing_clusters("us-v-x")
        assert self._keys(clusters) == [
            ["sentencing-mcgonigal", "sentencing-mcgonigal-2"]
        ]

    def test_singular_different_defendants_not_clustered(self, store: Store):
        store.upsert_hearing(
            _hearing(
                key="sentencing-wang",
                status="held",
                starts_at_utc="2026-01-05T16:00:00+00:00",
            )
        )
        store.upsert_hearing(
            _hearing(
                key="sentencing-prince",
                status="held",
                starts_at_utc="2026-02-09T16:00:00+00:00",
            )
        )
        assert store.find_nearslot_hearing_clusters("us-v-x") == []

    def test_repeatable_type_across_dates_not_clustered(self, store: Store):
        # Two motion hearings on different days are NOT a near-slot dup
        # (repeatable type, different dates) — left alone.
        store.upsert_hearing(
            _hearing(
                key="motion-hearing-x",
                status="held",
                starts_at_utc="2026-01-05T16:00:00+00:00",
            )
        )
        store.upsert_hearing(
            _hearing(
                key="motion-hearing-x-2",
                status="held",
                starts_at_utc="2026-02-09T16:00:00+00:00",
            )
        )
        assert store.find_nearslot_hearing_clusters("us-v-x") == []

    def test_bad_timezone_falls_back_to_utc_date_prefix(self, store: Store):
        # A corrupt timezone makes the court-local-date conversion raise;
        # _local_date falls back to the UTC date prefix so clustering still
        # works (two held rows, same UTC day, distinct times -> cluster).
        store.upsert_hearing(
            _hearing(
                key="cipa-a",
                status="held",
                timezone="Bogus/Zone",
                starts_at_utc="2023-03-08T05:00:00+00:00",
            )
        )
        store.upsert_hearing(
            _hearing(
                key="cipa-b",
                status="held",
                timezone="Bogus/Zone",
                starts_at_utc="2023-03-08T18:00:00+00:00",
            )
        )
        clusters = store.find_nearslot_hearing_clusters("us-v-x")
        assert sorted(h["hearing_key"] for c in clusters for h in c) == [
            "cipa-a",
            "cipa-b",
        ]


class TestEmptyBodyEntriesSince:
    """The cheap SQL pre-filter for the placeholder-reconcile sweep:
    empty-body + recent + has-recap_documents. The doc-level placeholder
    test (is_pending_enrichment) is applied by the caller on top of this.
    """

    def _doc(self):
        return [
            {"id": 9001, "is_available": False, "is_sealed": False, "plain_text": ""}
        ]

    def test_selects_recent_empty_body_with_docs(self, store):
        # A: the placeholder we want — empty body, has docs, recent.
        store.mark_entry(
            1,
            100,
            "2026-05-20T10:00:00Z",
            "fpA",
            date_filed="2026-05-20",
            description="",
            recap_documents=self._doc(),
        )
        # B: has a body → excluded.
        store.mark_entry(
            1,
            101,
            "2026-05-21T10:00:00Z",
            "fpB",
            date_filed="2026-05-21",
            description="ORDER ...",
            recap_documents=self._doc(),
        )
        # C: fingerprint-only stub (no recap_documents) → excluded.
        store.mark_entry(
            1,
            102,
            "2026-05-22T10:00:00Z",
            "fpC",
            date_filed="2026-05-22",
            description=None,
            recap_documents=None,
        )
        # D: empty body + docs but filed before the window → excluded.
        store.mark_entry(
            1,
            103,
            "2026-01-01T10:00:00Z",
            "fpD",
            date_filed="2026-01-01",
            description="",
            recap_documents=self._doc(),
        )
        # E: matches but on a docket not in the query → excluded.
        store.mark_entry(
            2,
            104,
            "2026-05-20T10:00:00Z",
            "fpE",
            date_filed="2026-05-20",
            description="",
            recap_documents=self._doc(),
        )
        rows = store.get_empty_body_entries_since([1], filed_after="2026-05-01")
        assert [r["entry_id"] for r in rows] == [100]
        assert rows[0]["recap_documents"] is not None

    def test_null_description_is_treated_as_empty_body(self, store):
        # A NULL description with docs present still qualifies (the SQL
        # checks both NULL and trimmed-empty).
        store.mark_entry(
            1,
            200,
            "2026-05-20T10:00:00Z",
            "fp",
            date_filed="2026-05-20",
            description=None,
            recap_documents=self._doc(),
        )
        rows = store.get_empty_body_entries_since([1], filed_after="2026-05-01")
        assert [r["entry_id"] for r in rows] == [200]

    def test_empty_docket_list_returns_empty(self, store):
        store.mark_entry(
            1,
            100,
            "2026-05-20T10:00:00Z",
            "fp",
            date_filed="2026-05-20",
            description="",
            recap_documents=self._doc(),
        )
        assert store.get_empty_body_entries_since([], filed_after="2026-05-01") == []
