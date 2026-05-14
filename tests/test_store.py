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

    def test_get_entries_with_body_returns_only_full_rows(self, store: Store):
        # Body-bearing entries (hearing-relevant + op/disp) are the rows the
        # summary pipeline reads instead of re-fetching from CL. Stubs
        # (description IS NULL) must NOT be returned — otherwise summary's
        # local-cache check would think it's warm when it's actually cold.
        store.mark_entry(1, 10, "2024-01-01T00:00:00Z", "fp1",
                         date_filed="2024-01-01", entry_number=1,
                         description="INDICTMENT",
                         recap_documents=[{"id": 500, "plain_text": "body"}])
        store.mark_entry(1, 11, "2024-01-02T00:00:00Z", "fp2",
                         date_filed="2024-01-02", entry_number=2,
                         description=None)  # filter-failed stub
        rows = store.get_entries_with_body(1)
        assert len(rows) == 1
        # ``id`` is renamed from entry_id so the shape matches what CL's
        # docket-entries response returns (callers can treat both paths the
        # same way without branching).
        assert rows[0]["id"] == 10
        assert rows[0]["description"] == "INDICTMENT"
        # recap_documents is deserialized from JSON, including plain_text.
        assert rows[0]["recap_documents"][0]["plain_text"] == "body"

    def test_get_entries_with_body_orders_by_date_filed(self, store: Store):
        # Summary's CL fallback returns operative pleadings oldest-first and
        # then sorts them; the local-cache path must produce the same order
        # so swap-ability between the two paths is invariant.
        store.mark_entry(1, 20, "2024-06-01T00:00:00Z", "fp2",
                         date_filed="2024-06-01", entry_number=2,
                         description="SUPERSEDING INDICTMENT",
                         recap_documents=[])
        store.mark_entry(1, 10, "2024-01-01T00:00:00Z", "fp1",
                         date_filed="2024-01-01", entry_number=1,
                         description="INDICTMENT",
                         recap_documents=[])
        rows = store.get_entries_with_body(1)
        assert [r["id"] for r in rows] == [10, 20]

    def test_get_entries_with_body_handles_legacy_null_recap_documents(
        self, store: Store,
    ):
        # Pre-fix rows have recap_documents=NULL. Reading must not crash —
        # those rows simply have an empty list, so pdf.extract_text would
        # find nothing to short-circuit on and fall through to network.
        store.mark_entry(1, 10, "2024-01-01T00:00:00Z", "fp1",
                         date_filed="2024-01-01", entry_number=1,
                         description="INDICTMENT", recap_documents=None)
        rows = store.get_entries_with_body(1)
        assert rows[0]["recap_documents"] == []

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

    def test_get_entry_documents_roundtrip(self, store: Store):
        docs = [
            {"id": 5, "document_number": 65, "attachment_number": None,
             "is_available": True, "is_sealed": False,
             "filepath_ia": "https://archive.org/65.pdf",
             "filepath_local": "recap/x/65.pdf", "description": None},
            {"id": 6, "document_number": 65, "attachment_number": 1,
             "is_available": True, "is_sealed": False,
             "filepath_ia": "https://archive.org/65a.pdf",
             "filepath_local": "recap/x/65a.pdf", "description": "Exhibit A"},
        ]
        store.mark_entry(1, 100, "2026-01-01T00:00:00Z", "fp",
                         entry_number=65, recap_documents=docs)
        # Unknown ids are silently dropped. Filter-failed stubs have no docs.
        store.mark_entry(1, 101, "2026-01-02T00:00:00Z", "fp",
                         entry_number=66)
        got = store.get_entry_documents([100, 101, 999])
        assert set(got) == {100}
        assert got[100] == docs

    def test_get_entry_documents_overwrite_on_reprocess(self, store: Store):
        # Adding a doc to an existing entry is the "watch for new
        # documents" case: re-marking with a longer list replaces the
        # cached JSON so emit-time descriptions show the new doc.
        first = [
            {"id": 5, "document_number": 65, "attachment_number": None,
             "is_available": True, "is_sealed": False,
             "filepath_ia": "https://archive.org/65.pdf",
             "filepath_local": None, "description": None},
        ]
        store.mark_entry(1, 100, "2026-01-01T00:00:00Z", "fp",
                         entry_number=65, recap_documents=first)
        second = first + [
            {"id": 6, "document_number": 65, "attachment_number": 1,
             "is_available": True, "is_sealed": False,
             "filepath_ia": "https://archive.org/65a.pdf",
             "filepath_local": None, "description": None},
        ]
        store.mark_entry(1, 100, "2026-01-02T00:00:00Z", "fp2",
                         entry_number=65, recap_documents=second)
        got = store.get_entry_documents([100])
        assert got[100] == second

    def test_get_entry_documents_empty_input(self, store: Store):
        assert store.get_entry_documents([]) == {}

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

    def test_bump_advances_forward(self, store: Store):
        # Forward-only advance: a newer entry's date_modified bumps the
        # docket's watermark, which is what the index's "updated at"
        # display reads from.
        store.set_docket_last_modified(7, "2026-05-01T00:00:00Z")
        store.bump_docket_last_modified(7, "2026-05-08T00:00:00Z")
        assert store.docket_last_modified(7) == "2026-05-08T00:00:00Z"

    def test_bump_ignores_older(self, store: Store):
        # Out-of-order webhook delivery (older entry arrives after a
        # newer one) must not move the watermark backwards.
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
        store.upsert_docket_meta(200, {
            "court_id": "mad", "docket_number": "1:25",
            "case_name": "X", "absolute_url": "/d/200/",
        })
        assert store.known_docket_ids() == {100, 200}

    def test_date_last_filing_persists_via_upsert(self, store: Store):
        # date_last_filing is captured from CL on the polling path; ensure
        # it round-trips through upsert_docket_meta + get_docket_meta.
        store.upsert_docket_meta(7, {
            "court_id": "mad", "docket_number": "1:25",
            "case_name": "X", "absolute_url": "/d/7/",
            "date_last_filing": "2026-05-08",
        })
        meta = store.get_docket_meta(7)
        assert meta["date_last_filing"] == "2026-05-08"

    def test_date_last_filing_none_does_not_clobber(self, store: Store):
        # A subsequent upsert that doesn't pass date_last_filing (e.g. a
        # webhook-driven path that touches metadata but never re-fetches
        # the docket) must NOT wipe the previously-cached value.
        store.upsert_docket_meta(7, {
            "court_id": "mad", "docket_number": "1:25",
            "case_name": "X", "absolute_url": "/d/7/",
            "date_last_filing": "2026-05-08",
        })
        store.upsert_docket_meta(7, {
            "court_id": "mad", "docket_number": "1:25",
            "case_name": "X", "absolute_url": "/d/7/",
        })
        assert store.get_docket_meta(7)["date_last_filing"] == "2026-05-08"

    def test_bump_last_filing_advances_forward(self, store: Store):
        # process_entry calls this with entry.date_filed so webhook-only
        # deployments can keep the index date current without refetching
        # the parent docket per delivery.
        store.upsert_docket_meta(7, {
            "court_id": "mad", "docket_number": "1:25",
            "case_name": "X", "absolute_url": "/d/7/",
            "date_last_filing": "2026-05-01",
        })
        store.bump_docket_last_filing(7, "2026-05-08")
        assert store.get_docket_meta(7)["date_last_filing"] == "2026-05-08"

    def test_bump_last_filing_ignores_older(self, store: Store):
        # An entry whose date_filed is older than what CL already gave us
        # (e.g. a late-arriving webhook for an old entry) must not move
        # the watermark backwards.
        store.upsert_docket_meta(7, {
            "court_id": "mad", "docket_number": "1:25",
            "case_name": "X", "absolute_url": "/d/7/",
            "date_last_filing": "2026-05-08",
        })
        store.bump_docket_last_filing(7, "2026-05-01")
        assert store.get_docket_meta(7)["date_last_filing"] == "2026-05-08"

    def test_bump_last_filing_inserts_when_missing(self, store: Store):
        # First-time webhook delivery for a docket we haven't poll-synced;
        # bump should land the value even though no row exists yet.
        store.bump_docket_last_filing(7, "2026-05-08")
        assert store.get_docket_meta(7)["date_last_filing"] == "2026-05-08"

    def test_bump_last_filing_empty_string_noop(self, store: Store):
        # The opportunistic bump in process_entry passes whatever the
        # entry's date_filed was; CL sometimes omits the field, and an
        # empty-string bump must not insert a row or clobber an existing
        # value.
        store.upsert_docket_meta(7, {
            "court_id": "mad", "docket_number": "1:25",
            "case_name": "X", "absolute_url": "/d/7/",
            "date_last_filing": "2026-05-08",
        })
        store.bump_docket_last_filing(7, "")
        assert store.get_docket_meta(7)["date_last_filing"] == "2026-05-08"


class TestCaseAggregates:
    def test_min_filed_max_last_filing_across_dockets(self, store: Store):
        # Earliest date_filed across the case's dockets wins as the case's
        # "filed" date; latest docket-level date_last_filing wins as the
        # "last filing" date the index page surfaces.
        store.upsert_docket_meta(10, {
            "court_id": "nysd", "docket_number": "1:25",
            "case_name": "X", "absolute_url": "/d/10/",
            "date_last_filing": "2026-05-10",
        })
        store.upsert_docket_meta(11, {
            "court_id": "nysd", "docket_number": "1:24",
            "case_name": "X", "absolute_url": "/d/11/",
            "date_last_filing": "2026-04-01",
        })
        store.mark_entry(10, 1, "2025-01-15T08:00:00Z", "fp",
                         date_filed="2025-01-15")
        store.mark_entry(11, 2, "2024-09-01T08:00:00Z", "fp",
                         date_filed="2024-09-01")
        agg = store.get_case_aggregates([10, 11])
        assert agg["date_filed"] == "2024-09-01"
        assert agg["last_filing_date"] == "2026-05-10"

    def test_ignores_date_modified_for_last_filing(self, store: Store):
        # Regression: the aggregate previously read from dockets.date_modified,
        # which bumps on OCR / metadata churn. After the switch to
        # date_last_filing, a docket whose date_modified is newer than its
        # date_last_filing must NOT show date_modified as "last filing".
        store.set_docket_last_modified(10, "2026-05-10T12:00:00Z")
        store.upsert_docket_meta(10, {
            "court_id": "nysd", "docket_number": "1:25",
            "case_name": "X", "absolute_url": "/d/10/",
            "date_last_filing": "2026-04-01",
        })
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
        store.upsert_docket_meta(1001, {"court_id": "cadc", "docket_number": "26-1049",
                                         "case_name": "X", "absolute_url": "/x/"})
        store.upsert_docket_meta(1002, {"court_id": "ca9", "docket_number": "26-2011",
                                         "case_name": "X", "absolute_url": "/y/"})
        store.upsert_hearing(_hearing(key="oral-arg-dc", docket_id=1001))
        store.upsert_hearing(_hearing(key="oral-arg-9", docket_id=1002))
        cadc = store.get_hearings_in_court("us-v-x", "cadc")
        assert {h["hearing_key"] for h in cadc} == {"oral-arg-dc"}
        ca9 = store.get_hearings_in_court("us-v-x", "ca9")
        assert {h["hearing_key"] for h in ca9} == {"oral-arg-9"}

    def test_in_court_keeps_same_court_siblings(self, store: Store):
        # Multi-defendant criminal: same court, separate dockets per defendant —
        # legitimately aggregated, must still appear together.
        store.upsert_docket_meta(2001, {"court_id": "dcd", "docket_number": "1:24-cr-261",
                                         "case_name": "X", "absolute_url": "/a/"})
        store.upsert_docket_meta(2002, {"court_id": "dcd", "docket_number": "1:24-cr-261",
                                         "case_name": "X", "absolute_url": "/b/"})
        store.upsert_hearing(_hearing(key="arraignment-a", docket_id=2001))
        store.upsert_hearing(_hearing(key="arraignment-b", docket_id=2002))
        out = store.get_hearings_in_court("us-v-x", "dcd")
        assert {h["hearing_key"] for h in out} == {"arraignment-a", "arraignment-b"}

    def test_in_court_includes_dangling_rows(self, store: Store):
        # docket_id NULL (legacy data) or court_id NULL (docket metadata not yet
        # cached) — keep them so we don't silently drop context.
        store.upsert_hearing(_hearing(key="legacy", docket_id=None))
        store.upsert_docket_meta(3001, {"court_id": None, "docket_number": "x",
                                         "case_name": "X", "absolute_url": "/c/"})
        store.upsert_hearing(_hearing(key="uncached", docket_id=3001))
        out = store.get_hearings_in_court("us-v-x", "cadc")
        assert {h["hearing_key"] for h in out} == {"legacy", "uncached"}

    def test_find_concurrent_hearing_clusters_groups_by_docket_and_time(
        self, store: Store,
    ):
        # Two future hearings sharing the same (docket_id, starts_at_utc)
        # form a cluster. A third hearing at a different time on the same
        # docket does NOT. A fourth hearing at the same time but on a
        # different docket does NOT (the rule is same-court same-slot,
        # not same-time-anywhere).
        future = "2099-04-14T15:00:00+00:00"
        store.upsert_hearing(_hearing(
            key="msj-hearing", starts_at_utc=future, docket_id=1,
        ))
        store.upsert_hearing(_hearing(
            key="motion-hearing-2", starts_at_utc=future, docket_id=1,
        ))
        store.upsert_hearing(_hearing(
            key="status", starts_at_utc="2099-04-15T15:00:00+00:00",
            docket_id=1,
        ))
        store.upsert_hearing(_hearing(
            key="other-docket", starts_at_utc=future, docket_id=2,
        ))
        clusters = store.find_concurrent_hearing_clusters("us-v-x")
        assert len(clusters) == 1
        keys = {h["hearing_key"] for h in clusters[0]}
        assert keys == {"msj-hearing", "motion-hearing-2"}
        # source_entry_ids is JSON-decoded for the caller.
        assert all(isinstance(h["source_entry_ids"], list) for h in clusters[0])

    def test_find_concurrent_hearing_clusters_excludes_past_and_non_scheduled(
        self, store: Store,
    ):
        # Past slots are handled by the auto-held sweep — don't bother
        # the LLM about them.
        past = "2020-01-01T00:00:00+00:00"
        store.upsert_hearing(_hearing(
            key="past-a", starts_at_utc=past, docket_id=1,
        ))
        store.upsert_hearing(_hearing(
            key="past-b", starts_at_utc=past, docket_id=1,
        ))
        # Cancelled / held rows must not poison a future cluster either —
        # only count 'scheduled' rows.
        future = "2099-04-14T15:00:00+00:00"
        store.upsert_hearing(_hearing(
            key="future-cancelled", starts_at_utc=future, docket_id=1,
            status="cancelled",
        ))
        store.upsert_hearing(_hearing(
            key="future-scheduled", starts_at_utc=future, docket_id=1,
            status="scheduled",
        ))
        assert store.find_concurrent_hearing_clusters("us-v-x") == []


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

    def test_in_court_filters_cross_court_siblings(self, store: Store):
        store.upsert_docket_meta(1001, {"court_id": "cadc", "docket_number": "26-1049",
                                         "case_name": "X", "absolute_url": "/x/"})
        store.upsert_docket_meta(1002, {"court_id": "ca9", "docket_number": "26-2011",
                                         "case_name": "X", "absolute_url": "/y/"})
        store.upsert_deadline(_deadline(key="reply-dc", docket_id=1001))
        store.upsert_deadline(_deadline(key="reply-9", docket_id=1002))
        cadc = store.get_deadlines_in_court("anthropic-v-dow", "cadc")
        assert {d["deadline_key"] for d in cadc} == {"reply-dc"}
        ca9 = store.get_deadlines_in_court("anthropic-v-dow", "ca9")
        assert {d["deadline_key"] for d in ca9} == {"reply-9"}

    def test_in_court_keeps_same_court_siblings(self, store: Store):
        store.upsert_docket_meta(2001, {"court_id": "dcd", "docket_number": "1:24-cr-261",
                                         "case_name": "X", "absolute_url": "/a/"})
        store.upsert_docket_meta(2002, {"court_id": "dcd", "docket_number": "1:24-cr-261",
                                         "case_name": "X", "absolute_url": "/b/"})
        store.upsert_deadline(_deadline(key="brief-a", docket_id=2001))
        store.upsert_deadline(_deadline(key="brief-b", docket_id=2002))
        out = store.get_deadlines_in_court("anthropic-v-dow", "dcd")
        assert {d["deadline_key"] for d in out} == {"brief-a", "brief-b"}

    def test_in_court_includes_dangling_rows(self, store: Store):
        store.upsert_deadline(_deadline(key="legacy", docket_id=None))
        store.upsert_docket_meta(3001, {"court_id": None, "docket_number": "x",
                                         "case_name": "X", "absolute_url": "/c/"})
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
    mid-sync would bubble up as HTTP 500 (CL retries with the same
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
        self, tmp_path,
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
        store.mark_entry(1, 100, "2026-01-01T00:00:00Z", "fp",
                         entry_number=65, description="Order")
        row = store.get_entry_by_number(1, 65)
        assert row and row["entry_id"] == 100

    def test_missing_returns_none(self, store: Store):
        assert store.get_entry_by_number(1, 999) is None


class TestEntryDocumentsMalformedJson:
    def test_skips_rows_with_invalid_json(self, store: Store):
        # Insert an entry with malformed recap_documents JSON via raw SQL.
        # get_entry_documents must catch the JSONDecodeError and skip the row
        # rather than crashing the whole emit.
        store.mark_entry(1, 100, "2026-01-01T00:00:00Z", "fp",
                         entry_number=65, description="Order")
        store.conn.execute(
            "UPDATE entries SET recap_documents=? WHERE entry_id=?",
            ("not json", 100),
        )
        out = store.get_entry_documents([100])
        assert out == {}  # bad row skipped


class TestGcalAndM365Setters:
    def test_set_gcal_id(self, store: Store):
        store.upsert_hearing(_hearing())
        store.set_gcal_id("us-v-x", "sentencing", "evt-123")
        row = store.get_hearing("us-v-x", "sentencing")
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
            "anthropic-v-dow", "govt-response-mtd", "AAMk-DL",
        )
        row = store.conn.execute(
            "SELECT m365_event_id FROM deadlines WHERE deadline_key=?",
            ("govt-response-mtd",),
        ).fetchone()
        assert row["m365_event_id"] == "AAMk-DL"
        store.set_m365_id_for_deadline(
            "anthropic-v-dow", "govt-response-mtd", None,
        )
        row = store.conn.execute(
            "SELECT m365_event_id FROM deadlines WHERE deadline_key=?",
            ("govt-response-mtd",),
        ).fetchone()
        assert row["m365_event_id"] is None


class TestCaseSummaries:
    def test_upsert_and_retrieve(self, store: Store):
        store.upsert_case_summary(
            "us-v-x", 1,
            summary="The defendants are charged with...",
            model="anthropic/claude-sonnet-4-6",
            source_entry_ids=[10, 20],
        )
        row = store.get_docket_summary("us-v-x", 1)
        assert row["summary"].startswith("The defendants")
        assert row["model"] == "anthropic/claude-sonnet-4-6"
        assert row["source_entry_ids"] == [10, 20]

    def test_upsert_overwrites_existing(self, store: Store):
        store.upsert_case_summary("us-v-x", 1, summary="v1", model="m1")
        store.upsert_case_summary("us-v-x", 1, summary="v2", model="m2")
        assert store.get_docket_summary("us-v-x", 1)["summary"] == "v2"

    def test_get_docket_summary_missing_returns_none(self, store: Store):
        assert store.get_docket_summary("nope", 1) is None

    def test_get_case_summaries_returns_all_dockets(self, store: Store):
        store.upsert_case_summary("us-v-x", 1, summary="a", model="m")
        store.upsert_case_summary("us-v-x", 2, summary="b", model="m")
        rows = store.get_case_summaries("us-v-x")
        assert {r["docket_id"] for r in rows} == {1, 2}

    def test_stale_lifecycle(self, store: Store):
        # New row is not stale; mark_summary_stale flips it; upsert resets.
        store.upsert_case_summary("us-v-x", 1, summary="v1", model="m")
        assert store.is_summary_stale("us-v-x", 1) is False
        store.mark_summary_stale("us-v-x", 1)
        assert store.is_summary_stale("us-v-x", 1) is True
        assert store.get_summary_stale_since("us-v-x", 1) is not None
        # Upserting after a refresh resets stale flag + clears stale_since.
        store.upsert_case_summary("us-v-x", 1, summary="v2", model="m")
        assert store.is_summary_stale("us-v-x", 1) is False
        assert store.get_summary_stale_since("us-v-x", 1) is None

    def test_missing_row_is_stale_by_definition(self, store: Store):
        # New cases never written get treated as stale so refresh_stale
        # creates a row on the next sync.
        assert store.is_summary_stale("never-summarized", 1) is True

    def test_mark_summary_stale_on_missing_row_is_noop(self, store: Store):
        # No row exists -> UPDATE matches nothing; subsequent get returns None.
        store.mark_summary_stale("nope", 1)
        assert store.get_summary_stale_since("nope", 1) is None

    def test_get_case_summaries_handles_malformed_source_entry_ids(self, store: Store):
        # source_entry_ids stored as malformed JSON falls back to [].
        store.upsert_case_summary("us-v-x", 1, summary="v1", model="m")
        store.conn.execute(
            "UPDATE case_summaries SET source_entry_ids=? WHERE case_id=?",
            ("not-json", "us-v-x"),
        )
        rows = store.get_case_summaries("us-v-x")
        assert rows[0]["source_entry_ids"] == []
        # Same fallback in get_docket_summary.
        assert store.get_docket_summary("us-v-x", 1)["source_entry_ids"] == []


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
        h = s.get_hearing("us-v-x", "trial")
        assert h["notes"] == "Trial commences June 12, 2024."
        assert h["audit_notes"] == (
            "[verify-pass] Cancellation not supported per recent entries."
        )
        d = s.get_deadlines("us-v-x")[0]
        assert d["notes"] == "Reply due 2/1/2026."
        assert d["audit_notes"] == "[verify-pass] Extended per docket."
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
        h = s2.get_hearing("us-v-x", "trial")
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
        h = s.get_hearing("us-v-x", "trial")
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
        store.upsert_docket_meta(docket_id, {
            "court_id": "dcd",
            "docket_number": f"1:24-cr-{docket_id:05d}",
            "case_name": f"US v. Docket {docket_id}",
            "absolute_url": None,
            "date_last_filing": None,
        })
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
        store.upsert_hearing({
            "case_id": case_id, "hearing_key": f"sentencing-{docket_id}",
            "title": "Sentencing",
            "starts_at_utc": "2099-01-01T00:00:00+00:00",
            "duration_minutes": 60, "timezone": "America/New_York",
            "status": "scheduled", "significance": "major",
            "docket_id": docket_id, "source_entry_ids": [entry_id],
        })
        store.upsert_deadline({
            "case_id": case_id, "deadline_key": f"reply-{docket_id}",
            "title": "Reply ISO MTD",
            "due_at_utc": "2099-01-15T22:00:00+00:00",
            "timezone": "America/New_York", "status": "pending",
            "significance": "major", "deadline_type": "reply",
            "docket_id": docket_id, "source_entry_ids": [entry_id],
        })
        store.upsert_case_summary(
            case_id=case_id, docket_id=docket_id,
            summary="text", model="anthropic/test",
            source_entry_ids=[entry_id],
        )
        store.conn.commit()

    def test_list_all_docket_ids_includes_dockets_and_child_orphans(
        self, store: Store,
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
            "entries": 1, "hearings": 1, "deadlines": 1,
            "case_summaries": 1, "dockets": 1,
        }

    def test_count_docket_rows_unknown_id_is_all_zero(self, store: Store):
        assert store.count_docket_rows(999) == {
            "entries": 0, "hearings": 0, "deadlines": 0,
            "case_summaries": 0, "dockets": 0,
        }

    def test_delete_docket_removes_every_referenced_row(self, store: Store):
        self._seed_docket(store, 100)
        self._seed_docket(store, 200)
        deleted = store.delete_docket(100)
        assert deleted == {
            "entries": 1, "hearings": 1, "deadlines": 1,
            "case_summaries": 1, "dockets": 1,
        }
        # Sibling docket 200 untouched.
        assert store.list_all_docket_ids() == [200]
        assert store.count_docket_rows(100) == {
            "entries": 0, "hearings": 0, "deadlines": 0,
            "case_summaries": 0, "dockets": 0,
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
