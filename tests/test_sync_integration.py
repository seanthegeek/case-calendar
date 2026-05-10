"""Integration tests for the sync pipeline.

These exercise CaseSyncer end-to-end against the FakeCL fixture and a
controllable LLM stub. The goal is to cover the pieces unit tests can't
easily reach: how actions translate into hearing rows, the docket-level
short-circuit, the entry-fingerprint dedup, and reschedule/cancel flows.
"""

from __future__ import annotations

import pytest

from case_calendar import llm as llm_mod
from case_calendar.store import Store
from case_calendar.sync import CaseConfig, CaseSyncer

from .conftest import FakeCL


@pytest.fixture
def case():
    return CaseConfig(
        case_id="us-v-x", name="United States v. X",
        dockets=[100], calendar="cyber",
    )


def _docket(date_modified="2026-05-01T00:00:00-07:00"):
    return {
        "id": 100, "court_id": "mad",
        "docket_number": "1:25-cr-00001-X",
        "case_name": "United States v. X",
        "absolute_url": "/docket/100/x/",
        "date_modified": date_modified,
    }


def _entry(eid, desc, date_filed="2026-01-01"):
    return {
        "id": eid, "docket": 100, "entry_number": eid,
        "date_filed": date_filed,
        "date_modified": f"{date_filed}T00:00:00-07:00",
        "description": desc, "short_description": "",
        "recap_documents": [],
    }


def make_llm_stub(monkeypatch, *, by_entry: dict[int, list[dict]]):
    """Stub llm.extract_actions to return canned actions per entry."""
    def fake(*, entry, **_):
        return by_entry.get(entry["id"], [{"type": "IGNORE", "reason": "stub"}])
    monkeypatch.setattr(llm_mod, "extract_actions", fake)


def stub_verify(monkeypatch, *, by_key: dict[str, dict] | None = None):
    """Stub llm.verify_hearing to return canned per-key actions.

    Defaults to CONFIRM (no-op) for any hearing not explicitly listed,
    so tests that don't care about verification just bypass it.
    """
    by_key = by_key or {}
    def fake(*, hearing, **_):
        return by_key.get(
            hearing.get("hearing_key"),
            {"type": "CONFIRM", "reason": "stub"},
        )
    monkeypatch.setattr(llm_mod, "verify_hearing", fake)


# --- happy path: schedule, then reschedule, then mark held ---


class TestDateLessAddIsDropped:
    def test_add_without_local_date_is_skipped(
        self, store: Store, case, monkeypatch,
    ):
        # Defensive guard: if the LLM returns ADD with no date (e.g. on a
        # motion-for-hearing or plea agreement), drop it. Otherwise we'd
        # store a date-less ghost row that never reaches the calendar.
        cl = FakeCL(
            dockets={100: _docket()},
            entries={100: [_entry(1, "MOTION for Hearing by USA")]},
        )
        make_llm_stub(monkeypatch, by_entry={
            1: [{"type": "ADD", "hearing_key": "status-conf-x",
                 "hearing_type": "status_conference", "title": "Status Conference",
                 "local_date": None, "local_time": None,
                 "reason": "motion requesting hearing"}],
        })
        syncer = CaseSyncer(cl, store)
        syncer.sync_case(case)
        assert store.get_hearings("us-v-x") == []


class TestScheduleRescheduleFlow:
    def test_schedule_creates_hearing(self, store: Store, case, monkeypatch):
        cl = FakeCL(
            dockets={100: _docket()},
            entries={100: [_entry(1, "Sentencing set for 4/14/2026 03:00 PM")]},
        )
        make_llm_stub(monkeypatch, by_entry={
            1: [{"type": "ADD", "hearing_key": "sentencing-x",
                 "hearing_type": "sentencing", "title": "Sentencing",
                 "local_date": "2026-04-14", "local_time": "15:00",
                 "duration_minutes": 90, "location": "Courtroom 4",
                 "judge": "Judge Y", "reason": "first set"}],
        })
        syncer = CaseSyncer(cl, store)
        stats = syncer.sync_case(case)
        assert stats["actions"] == 1
        rows = store.get_hearings("us-v-x")
        assert len(rows) == 1
        h = rows[0]
        assert h["hearing_key"] == "sentencing-x"
        assert h["starts_at_utc"] == "2026-04-14T19:00:00+00:00"  # 3pm EDT
        assert h["docket_id"] == 100

    def test_reschedule_updates_in_place(self, store: Store, case, monkeypatch):
        # Drive entries via process_entry so we can replay them in the order
        # we want without depending on iter_entries' newest-first semantics.
        make_llm_stub(monkeypatch, by_entry={
            1: [{"type": "ADD", "hearing_key": "sentencing-x",
                 "hearing_type": "sentencing", "title": "Sentencing",
                 "local_date": "2026-04-14", "local_time": "15:00",
                 "duration_minutes": 90, "location": "Courtroom 4"}],
            2: [{"type": "RESCHEDULE", "hearing_key": "sentencing-x",
                 "title": "Sentencing",
                 "local_date": "2026-04-14", "local_time": "11:00"}],
        })
        cl = FakeCL(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        # Original scheduling first ...
        syncer.process_entry(case, 100,
                             _entry(1, "Sentencing set for 4/14/2026 03:00 PM"))
        # ... then the reschedule.
        syncer.process_entry(case, 100,
                             _entry(2, "Sentencing reset for 4/14/2026 11:00 AM",
                                     date_filed="2026-04-08"))
        rows = store.get_hearings("us-v-x")
        assert len(rows) == 1, "RESCHEDULE should update in place, not duplicate"
        # 11:00 EDT → 15:00 UTC.
        assert rows[0]["starts_at_utc"] == "2026-04-14T15:00:00+00:00"
        assert set(rows[0]["source_entry_ids"]) == {1, 2}

    def test_mark_held(self, store: Store, case, monkeypatch):
        make_llm_stub(monkeypatch, by_entry={
            1: [{"type": "ADD", "hearing_key": "sentencing-x",
                 "hearing_type": "sentencing", "title": "Sentencing",
                 "local_date": "2026-04-14", "local_time": "15:00"}],
            2: [{"type": "MARK_HELD", "hearing_key": "sentencing-x"}],
        })
        cl = FakeCL(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100,
                             _entry(1, "Sentencing set for 4/14/2026 03:00 PM"))
        syncer.process_entry(case, 100,
                             _entry(2, "Minute Entry: Sentencing held on 4/14/2026"))
        rows = store.get_hearings("us-v-x")
        assert len(rows) == 1
        assert rows[0]["status"] == "held"

    def test_cancel(self, store: Store, case, monkeypatch):
        make_llm_stub(monkeypatch, by_entry={
            1: [{"type": "ADD", "hearing_key": "sentencing-x",
                 "hearing_type": "sentencing", "title": "Sentencing",
                 "local_date": "2026-04-14", "local_time": "15:00"}],
            2: [{"type": "CANCEL", "hearing_key": "sentencing-x",
                 "notes": "vacated"}],
        })
        cl = FakeCL(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100,
                             _entry(1, "Sentencing set for 4/14/2026 03:00 PM"))
        syncer.process_entry(case, 100,
                             _entry(2, "Sentencing vacated"))
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "cancelled"
        assert h["notes"] == "vacated"

    def test_update_details_adds_dial_in_without_changing_time(
        self, store: Store, case, monkeypatch
    ):
        make_llm_stub(monkeypatch, by_entry={
            1: [{"type": "ADD", "hearing_key": "status-conf-x",
                 "hearing_type": "status_conference", "title": "Status Conference",
                 "local_date": "2026-03-02", "local_time": "10:30",
                 "duration_minutes": 30}],
            2: [{"type": "UPDATE_DETAILS", "hearing_key": "status-conf-x",
                 "title": "Status Conference",
                 "dial_in": "Zoom: meet.example/abc"}],
        })
        cl = FakeCL(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100,
                             _entry(1, "Status Conference set for 3/2/2026 at 10:30 AM"))
        syncer.process_entry(case, 100,
                             _entry(2, "Hearing will be conducted via Zoom: meet.example/abc"))
        h = store.get_hearings("us-v-x")[0]
        assert h["dial_in"] == "Zoom: meet.example/abc"
        # Time unchanged.
        assert h["starts_at_utc"] == "2026-03-02T15:30:00+00:00"


# --- short-circuits ---


class TestShortCircuits:
    def test_irrelevant_entry_skips_llm_entirely(
        self, store: Store, case, monkeypatch
    ):
        called = []
        def fake(**_):
            called.append("nope")
            return [{"type": "IGNORE"}]
        monkeypatch.setattr(llm_mod, "extract_actions", fake)

        cl = FakeCL(
            dockets={100: _docket()},
            entries={100: [
                _entry(1, "RESPONDENT BRIEF filed by Peter B. Hegseth"),
                _entry(2, "NOTICE OF ATTORNEY APPEARANCE for USA"),
            ]},
        )
        syncer = CaseSyncer(cl, store)
        stats = syncer.sync_case(case)
        assert stats["entries_processed"] == 0
        assert called == []

    def test_unchanged_docket_short_circuits_on_resync(
        self, store: Store, case, monkeypatch
    ):
        cl = FakeCL(
            dockets={100: _docket()},
            entries={100: [_entry(1, "Sentencing set for 4/14/2026 03:00 PM")]},
        )
        make_llm_stub(monkeypatch, by_entry={
            1: [{"type": "ADD", "hearing_key": "sentencing-x",
                 "hearing_type": "sentencing", "title": "Sentencing",
                 "local_date": "2026-04-14", "local_time": "15:00"}],
        })
        syncer = CaseSyncer(cl, store)
        syncer.sync_case(case)
        cl.calls.clear()

        stats2 = syncer.sync_case(case)
        assert stats2["dockets_skipped"] == 1
        assert stats2["entries_seen"] == 0
        # Second pass touches /dockets/ once, no /docket-entries/.
        kinds = [c[0] for c in cl.calls]
        assert kinds == ["docket"]

    def test_repeat_entry_with_same_fingerprint_does_not_call_llm(
        self, store: Store, case, monkeypatch
    ):
        called = [0]
        def fake(**_):
            called[0] += 1
            return [{"type": "IGNORE"}]
        monkeypatch.setattr(llm_mod, "extract_actions", fake)

        e = _entry(1, "Notice of Hearing")
        cl = FakeCL(dockets={100: _docket()}, entries={100: [e]})
        syncer = CaseSyncer(cl, store)
        syncer.sync_case(case)
        first = called[0]

        # Force the second sync to re-iterate the same entry by bumping the
        # docket date_modified (defeats the docket-level skip).
        cl._dockets[100] = _docket(date_modified="2026-06-01T00:00:00-07:00")
        syncer.sync_case(case)
        # Entry fingerprint hasn't changed, so the LLM stays at the same count.
        assert called[0] == first


class TestDocketMetaCaching:
    def test_court_fetched_once(self, store: Store, case, monkeypatch):
        make_llm_stub(monkeypatch, by_entry={})
        cl = FakeCL(
            dockets={100: _docket()},
            entries={100: [_entry(1, "x")]},
            courts={"mad": {"citation_string": "D. Mass.",
                            "short_name": "Massachusetts",
                            "full_name": "District of Massachusetts"}},
        )
        syncer = CaseSyncer(cl, store)
        syncer.sync_case(case)
        court_calls = [c for c in cl.calls if c[0] == "court"]
        # First sync hits /courts/mad/ exactly once.
        assert court_calls == [("court", "mad")]

        # Force re-iteration by bumping date_modified.
        cl._dockets[100] = _docket(date_modified="2026-06-01T00:00:00-07:00")
        cl.calls.clear()
        syncer.sync_case(case)
        # Should NOT re-fetch the court — already cached.
        assert not any(c[0] == "court" for c in cl.calls)

    def test_docket_meta_persisted(self, store: Store, case, monkeypatch):
        make_llm_stub(monkeypatch, by_entry={})
        cl = FakeCL(dockets={100: _docket()}, entries={100: [_entry(1, "x")]})
        syncer = CaseSyncer(cl, store)
        syncer.sync_case(case)
        meta = store.get_docket_meta(100)
        assert meta["court_id"] == "mad"
        assert meta["docket_number"] == "1:25-cr-00001-X"


class TestStickyTimezone:
    """Regression: a hearing's tz must stick to the docket that scheduled it.

    Multi-docket cases (e.g. Anthropic v. DOW spans cadc/cand/ca9) can have a
    cand entry that references a cadc oral argument. The cand entry must NOT
    overwrite the tz from PT to ET (or vice versa), since the UTC value
    stored was computed from the original docket's tz.
    """

    def test_update_from_different_court_does_not_change_tz(
        self, store: Store, case, monkeypatch
    ):
        # First sight: cadc (ET) docket schedules an oral argument.
        cadc_docket = {
            "id": 200, "court_id": "cadc",
            "docket_number": "26-1049", "case_name": "X",
            "absolute_url": "/d/200/", "date_modified": "2026-05-01T00:00:00-07:00",
        }
        # Second sight: cand (PT) sibling docket references the same hearing.
        cand_docket = {
            "id": 300, "court_id": "cand",
            "docket_number": "3:26-cv-1996", "case_name": "X",
            "absolute_url": "/d/300/", "date_modified": "2026-05-02T00:00:00-07:00",
        }
        cl = FakeCL(dockets={200: cadc_docket, 300: cand_docket})

        case_multi = CaseConfig(case_id="x", name="X", dockets=[200, 300], calendar="t")

        make_llm_stub(monkeypatch, by_entry={
            1: [{"type": "ADD", "hearing_key": "oral-arg",
                 "hearing_type": "oral_argument", "title": "Oral Argument",
                 "local_date": "2026-05-19", "local_time": None}],
            2: [{"type": "UPDATE_DETAILS", "hearing_key": "oral-arg",
                 "title": "Oral Argument",
                 "notes": "cand reference: see appellate calendar"}],
        })

        syncer = CaseSyncer(cl, store)
        # First entry from cadc (ET).
        syncer.process_entry(case_multi, 200, _entry(1, "Oral argument scheduled"))
        h_before = store.get_hearings("x")[0]
        assert h_before["timezone"] == "America/New_York"
        # 2026-05-19 midnight ET = 04:00 UTC.
        assert h_before["starts_at_utc"] == "2026-05-19T04:00:00+00:00"

        # Second entry from cand (PT) referencing the same hearing.
        syncer.process_entry(case_multi, 300, _entry(2, "Oral argument referenced"))
        h_after = store.get_hearings("x")[0]
        # Timezone must remain ET (not flip to PT).
        assert h_after["timezone"] == "America/New_York"
        # And the starts_at_utc must NOT have shifted by 3 hours.
        assert h_after["starts_at_utc"] == "2026-05-19T04:00:00+00:00"


class TestProcessEntryDirect:
    """``process_entry`` is the entry point the webhook server uses."""

    def test_processes_a_single_entry(self, store: Store, case, monkeypatch):
        make_llm_stub(monkeypatch, by_entry={
            1: [{"type": "ADD", "hearing_key": "sentencing-x",
                 "hearing_type": "sentencing", "title": "Sentencing",
                 "local_date": "2026-04-14", "local_time": "15:00",
                 "duration_minutes": 90}],
        })
        cl = FakeCL(dockets={100: _docket()})  # no entries pre-loaded
        syncer = CaseSyncer(cl, store)
        e = _entry(1, "Sentencing set for 4/14/2026 03:00 PM")
        was_processed = syncer.process_entry(case, 100, e)
        assert was_processed is True
        assert len(store.get_hearings("us-v-x")) == 1

    def test_dedup_returns_false(self, store: Store, case, monkeypatch):
        make_llm_stub(monkeypatch, by_entry={
            1: [{"type": "ADD", "hearing_key": "x", "title": "T",
                 "local_date": "2026-04-14", "local_time": "15:00",
                 "hearing_type": "sentencing"}],
        })
        cl = FakeCL(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        e = _entry(1, "Sentencing set for 4/14/2026 03:00 PM")
        assert syncer.process_entry(case, 100, e) is True
        # Second call with identical entry should be a no-op.
        assert syncer.process_entry(case, 100, e) is False


class TestCancelOnUnknownKey:
    """Adjournment memo for a hearing whose original scheduling entry was
    filtered out before reaching the LLM should still leave a cancelled
    audit-trail row, not silently drop."""

    def test_cancel_with_local_date_inserts_cancelled_row(
        self, store: Store, case, monkeypatch,
    ):
        make_llm_stub(monkeypatch, by_entry={
            1: [{"type": "CANCEL", "hearing_key": "status-conf-x-7",
                 "title": "Status Conference",
                 "local_date": "2023-07-18",
                 "notes": "adjourned by court"}],
        })
        cl = FakeCL(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100,
                             _entry(1, "ENDORSEMENT: status conference "
                                       "previously scheduled for July 18, "
                                       "2023 is hereby adjourned"))
        rows = store.get_hearings("us-v-x")
        assert len(rows) == 1
        assert rows[0]["status"] == "cancelled"
        assert rows[0]["hearing_key"] == "status-conf-x-7"
        assert rows[0]["starts_at_utc"].startswith("2023-07-18")

    def test_cancel_without_local_date_drops(
        self, store: Store, case, monkeypatch, caplog,
    ):
        make_llm_stub(monkeypatch, by_entry={
            1: [{"type": "CANCEL", "hearing_key": "status-conf-x-7"}],
        })
        cl = FakeCL(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100,
                             _entry(1, "ENDORSEMENT: hearing adjourned"))
        assert store.get_hearings("us-v-x") == []
        assert any("CANCEL on unknown key with no local_date" in r.message
                   for r in caplog.records)


class TestMarkHeldOnUnknownKey:
    """Held minute entry for a hearing whose scheduling never reached the
    store should ADD a new row in 'held' status."""

    def test_mark_held_with_local_date_inserts_held_row(
        self, store: Store, case, monkeypatch,
    ):
        make_llm_stub(monkeypatch, by_entry={
            1: [{"type": "MARK_HELD", "hearing_key": "cipa-hearing-x",
                 "title": "CIPA Hearing",
                 "local_date": "2023-03-06",
                 "significance": "major"}],
        })
        cl = FakeCL(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100,
                             _entry(1, "Minute Entry: CIPA Hearing "
                                       "held on 3/6/2023"))
        rows = store.get_hearings("us-v-x")
        assert len(rows) == 1
        assert rows[0]["status"] == "held"
        assert rows[0]["hearing_key"] == "cipa-hearing-x"
        assert rows[0]["starts_at_utc"].startswith("2023-03-06")


class TestMarkHeldDateValidation:
    """When the LLM picks the wrong existing key for a MARK_HELD action,
    the date-proximity check rejects it instead of poisoning the matched
    row's source list."""

    def test_mark_held_with_far_off_date_is_rejected(
        self, store: Store, case, monkeypatch, caplog,
    ):
        # Set up: existing scheduled hearing on 2023-03-08.
        make_llm_stub(monkeypatch, by_entry={
            1: [{"type": "ADD", "hearing_key": "status-conf-x",
                 "hearing_type": "status_conference", "title": "Status Conf",
                 "local_date": "2023-03-08", "local_time": "12:30"}],
            # LLM tries to MARK_HELD this key using a 3/6 minute entry —
            # 2 days off is borderline-fine, but 3+ days off is rejected.
            2: [{"type": "MARK_HELD", "hearing_key": "status-conf-x",
                 "local_date": "2023-03-04"}],
        })
        cl = FakeCL(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100,
                             _entry(1, "Status Conference set for 3/8/2023 12:30 PM"))
        syncer.process_entry(case, 100,
                             _entry(2, "Minute Entry: Hearing held on 3/4/2023"))
        rows = store.get_hearings("us-v-x")
        assert len(rows) == 1
        # Status should NOT have flipped to held — date mismatch rejected.
        assert rows[0]["status"] == "scheduled"
        assert any("MARK_HELD date mismatch" in r.message
                   for r in caplog.records)

    def test_mark_held_within_tolerance_still_applies(
        self, store: Store, case, monkeypatch,
    ):
        # 1-day diff (e.g. minute entry filed day after hearing) is fine.
        make_llm_stub(monkeypatch, by_entry={
            1: [{"type": "ADD", "hearing_key": "sentencing-x",
                 "hearing_type": "sentencing", "title": "Sentencing",
                 "local_date": "2026-04-14", "local_time": "15:00"}],
            2: [{"type": "MARK_HELD", "hearing_key": "sentencing-x",
                 "local_date": "2026-04-15"}],
        })
        cl = FakeCL(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100,
                             _entry(1, "Sentencing set for 4/14/2026 3 PM"))
        syncer.process_entry(case, 100,
                             _entry(2, "Sentencing held on 4/14/2026"))
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "held"


class TestAutoMarkHeldStaleSweep:
    """End-of-sync sweep that flips any past 'scheduled' row to 'held'.
    No buffer — starts_at_utc is already UTC and we have a UTC clock."""

    def test_stale_scheduled_flipped_to_held(
        self, store: Store, case, monkeypatch,
    ):
        stub_verify(monkeypatch)
        store.upsert_hearing({
            "case_id": "us-v-x",
            "hearing_key": "stale-conf-x",
            "title": "Status Conference",
            "starts_at_utc": "2024-01-01T12:00:00+00:00",
            "duration_minutes": 30,
            "timezone": "America/New_York",
            "status": "scheduled",
            "significance": "major",
            "docket_id": 100,
            "source_entry_ids": [],
        })
        cl = FakeCL(dockets={100: _docket()}, entries={100: []})
        syncer = CaseSyncer(cl, store)
        stats = syncer.sync_case(case)
        assert stats["auto_held"] == 1
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "held"

    def test_future_scheduled_not_flipped(
        self, store: Store, case, monkeypatch,
    ):
        stub_verify(monkeypatch)
        from datetime import datetime, timedelta, timezone
        future_iso = (
            datetime.now(timezone.utc) + timedelta(days=30)
        ).isoformat()
        store.upsert_hearing({
            "case_id": "us-v-x",
            "hearing_key": "future-trial",
            "title": "Trial",
            "starts_at_utc": future_iso,
            "duration_minutes": 240,
            "timezone": "America/New_York",
            "status": "scheduled",
            "significance": "major",
            "docket_id": 100,
            "source_entry_ids": [],
        })
        cl = FakeCL(dockets={100: _docket()}, entries={100: []})
        syncer = CaseSyncer(cl, store)
        stats = syncer.sync_case(case)
        assert stats["auto_held"] == 0
        assert store.get_hearings("us-v-x")[0]["status"] == "scheduled"

    def test_cancelled_not_flipped(self, store: Store, case, monkeypatch):
        stub_verify(monkeypatch)
        store.upsert_hearing({
            "case_id": "us-v-x",
            "hearing_key": "cancelled-conf",
            "title": "Status Conference",
            "starts_at_utc": "2024-01-01T12:00:00+00:00",
            "duration_minutes": 30,
            "timezone": "America/New_York",
            "status": "cancelled",
            "significance": "major",
            "docket_id": 100,
            "source_entry_ids": [],
        })
        cl = FakeCL(dockets={100: _docket()}, entries={100: []})
        syncer = CaseSyncer(cl, store)
        stats = syncer.sync_case(case)
        assert stats["auto_held"] == 0
        assert store.get_hearings("us-v-x")[0]["status"] == "cancelled"

    def test_just_past_scheduled_flipped(self, store: Store, case, monkeypatch):
        # Hearing whose start was even 1 hour ago is flipped — no buffer.
        stub_verify(monkeypatch)
        from datetime import datetime, timedelta, timezone
        past_iso = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        store.upsert_hearing({
            "case_id": "us-v-x",
            "hearing_key": "just-past",
            "title": "Status Conference",
            "starts_at_utc": past_iso,
            "duration_minutes": 30,
            "timezone": "America/New_York",
            "status": "scheduled",
            "significance": "major",
            "docket_id": 100,
            "source_entry_ids": [],
        })
        cl = FakeCL(dockets={100: _docket()}, entries={100: []})
        syncer = CaseSyncer(cl, store)
        stats = syncer.sync_case(case)
        assert stats["auto_held"] == 1
        assert store.get_hearings("us-v-x")[0]["status"] == "held"


class TestVerifyScheduledHearings:
    """Per-hearing confidence pass: for every future scheduled hearing,
    ask the LLM whether recent docket entries support it."""

    def _seed_future_hearing(self, store, key="future-trial"):
        from datetime import datetime, timedelta, timezone
        future_iso = (
            datetime.now(timezone.utc) + timedelta(days=14)
        ).isoformat()
        store.upsert_hearing({
            "case_id": "us-v-x",
            "hearing_key": key,
            "title": "Trial",
            "starts_at_utc": future_iso,
            "duration_minutes": 240,
            "timezone": "America/New_York",
            "status": "scheduled",
            "significance": "major",
            "docket_id": 100,
            "source_entry_ids": [42],
        })
        return future_iso

    def test_confirm_is_no_op(self, store, case, monkeypatch):
        before = self._seed_future_hearing(store)
        stub_verify(monkeypatch)  # default CONFIRM
        cl = FakeCL(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["verified"] == 0
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "scheduled"
        assert h["starts_at_utc"] == before

    def test_unclear_is_no_op(self, store, case, monkeypatch):
        self._seed_future_hearing(store)
        stub_verify(monkeypatch, by_key={
            "future-trial": {"type": "UNCLEAR", "reason": "ambiguous"},
        })
        cl = FakeCL(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["verified"] == 0
        assert store.get_hearings("us-v-x")[0]["status"] == "scheduled"

    def test_cancel_flips_to_cancelled(self, store, case, monkeypatch):
        self._seed_future_hearing(store)
        stub_verify(monkeypatch, by_key={
            "future-trial": {"type": "CANCEL", "reason": "trial vacated by plea"},
        })
        cl = FakeCL(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["verified"] == 1
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "cancelled"
        assert "vacated" in (h["notes"] or "")

    def test_delete_hallucination_flips_to_cancelled(
        self, store, case, monkeypatch,
    ):
        # Hallucinated row — LLM says no docket entry supports it. Marked
        # cancelled (preserves audit trail; renderers skip cancelled rows).
        self._seed_future_hearing(store, key="hallucinated-conf")
        stub_verify(monkeypatch, by_key={
            "hallucinated-conf": {
                "type": "DELETE_HALLUCINATION",
                "reason": "no docket entry mentions this date",
            },
        })
        cl = FakeCL(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["verified"] == 1
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "cancelled"
        assert "no docket entry" in (h["notes"] or "")

    def test_reschedule_moves_starts_at_utc(self, store, case, monkeypatch):
        self._seed_future_hearing(store)
        stub_verify(monkeypatch, by_key={
            "future-trial": {
                "type": "RESCHEDULE",
                "local_date": "2099-01-15",
                "local_time": "09:00",
                "reason": "rescheduled per latest order",
            },
        })
        cl = FakeCL(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["verified"] == 1
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "scheduled"
        # ET 09:00 on 2099-01-15 → 14:00 UTC.
        assert h["starts_at_utc"] == "2099-01-15T14:00:00+00:00"

    def test_mark_held_via_verify(self, store, case, monkeypatch):
        # Edge case: the LLM's verify pass might catch a held event the
        # extractor missed. The pass runs only on FUTURE hearings, but
        # the LLM might still emit MARK_HELD if the recent entries show
        # the hearing happened earlier than its scheduled date.
        self._seed_future_hearing(store)
        stub_verify(monkeypatch, by_key={
            "future-trial": {"type": "MARK_HELD",
                             "reason": "minute entry shows held"},
        })
        cl = FakeCL(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["verified"] == 1
        assert store.get_hearings("us-v-x")[0]["status"] == "held"

    def test_only_runs_on_future_scheduled(self, store, case, monkeypatch):
        # Past + cancelled + held rows must NOT call verify.
        from datetime import datetime, timedelta, timezone
        store.upsert_hearing({
            "case_id": "us-v-x", "hearing_key": "past-held",
            "title": "Sentencing", "status": "held",
            "starts_at_utc": "2024-01-01T00:00:00+00:00",
            "duration_minutes": 90, "timezone": "America/New_York",
            "significance": "major", "docket_id": 100,
            "source_entry_ids": [1],
        })
        store.upsert_hearing({
            "case_id": "us-v-x", "hearing_key": "future-cancelled",
            "title": "Conf", "status": "cancelled",
            "starts_at_utc": (datetime.now(timezone.utc)
                              + timedelta(days=30)).isoformat(),
            "duration_minutes": 30, "timezone": "America/New_York",
            "significance": "major", "docket_id": 100,
            "source_entry_ids": [2],
        })
        called = []
        def fake(*, hearing, **_):
            called.append(hearing.get("hearing_key"))
            return {"type": "CONFIRM"}
        monkeypatch.setattr(llm_mod, "verify_hearing", fake)
        cl = FakeCL(dockets={100: _docket()}, entries={100: []})
        CaseSyncer(cl, store).sync_case(case)
        assert called == []  # no future-scheduled rows present
