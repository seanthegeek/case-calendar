"""Integration tests for the sync pipeline.

These exercise CaseSyncer end-to-end against the FakeCourtListener fixture and a
controllable LLM stub. The goal is to cover the pieces unit tests can't
easily reach: how actions translate into hearing rows, the docket-level
short-circuit, the entry-fingerprint dedup, and reschedule/cancel flows.
"""

from __future__ import annotations

import pytest

from case_calendar import llm as llm_mod
from case_calendar.store import Store
from case_calendar.sync import (
    CaseConfig,
    CaseSyncer,
    _absorbed_sibling_keys,
    _best_dedupe_title,
    _best_proceeding_notes,
    _canonical_drift_key,
    _describes_proceeding,
    _drift_base,
    _entry_records_proceeding,
    _hearing_date_tokens,
    _is_admin_notice,
    _key_to_title,
    _proceeding_notes_from_entry,
    _proceeding_record_rank,
    _proceeding_types,
    _record_proceeding_name,
    _same_logical_slot,
    _title_is_key_derived,
    fingerprint_entry,
    heal_drifted_keys,
    heal_proceeding_notes,
    is_pending_enrichment,
)

from .conftest import FakeCourtListener, must


@pytest.fixture
def case():
    return CaseConfig(
        case_id="us-v-x",
        name="United States v. X",
        dockets=[100],
        calendar="cyber",
    )


def _docket(date_modified="2026-05-01T00:00:00-07:00", date_last_filing="2026-05-01"):
    return {
        "id": 100,
        "court_id": "mad",
        "docket_number": "1:25-cr-00001-X",
        "case_name": "United States v. X",
        "absolute_url": "/docket/100/x/",
        "date_modified": date_modified,
        "date_last_filing": date_last_filing,
    }


def _entry(eid, desc, date_filed="2026-01-01"):
    return {
        "id": eid,
        "docket": 100,
        "entry_number": eid,
        "date_filed": date_filed,
        "date_modified": f"{date_filed}T00:00:00-07:00",
        "description": desc,
        "short_description": "",
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


@pytest.fixture(autouse=True)
def _default_stub_verify(monkeypatch):
    """Autouse safety net: stub verify_hearing to CONFIRM by default.

    Previously the verify pass ran only over future-dated 'scheduled'
    rows, so tests that seeded a past-dated row could get away without
    stubbing verify (the row went straight to the now-removed auto-held
    sweep). Now verify covers past rows too, and every test that runs
    ``sync_case`` would need to remember to stub it. This autouse fixture
    is the global safety net — tests that want non-default verify
    behavior call ``stub_verify(by_key=...)`` to override.
    """

    def fake(*, hearing, **_):
        return {"type": "CONFIRM", "reason": "autouse stub"}

    monkeypatch.setattr(llm_mod, "verify_hearing", fake)


# --- success path: schedule, then reschedule, then mark held ---


class TestDateLessAddIsDropped:
    def test_add_without_local_date_is_skipped(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # Defensive guard: if the LLM returns ADD_HEARING with no date (e.g. on a
        # motion-for-hearing or plea agreement), drop it. Otherwise we'd
        # store a date-less ghost row that never reaches the calendar.
        cl = FakeCourtListener(
            dockets={100: _docket()},
            entries={100: [_entry(1, "MOTION for Hearing by USA")]},
        )
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_HEARING",
                        "hearing_key": "status-conf-x",
                        "hearing_type": "status_conference",
                        "title": "Status Conference",
                        "local_date": None,
                        "local_time": None,
                        "reason": "motion requesting hearing",
                    }
                ],
            },
        )
        syncer = CaseSyncer(cl, store)
        syncer.sync_case(case)
        assert store.get_hearings("us-v-x") == []


class TestScheduleRescheduleFlow:
    def test_schedule_creates_hearing(self, store: Store, case, monkeypatch):
        cl = FakeCourtListener(
            dockets={100: _docket()},
            entries={100: [_entry(1, "Sentencing set for 4/14/2026 03:00 PM")]},
        )
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_HEARING",
                        "hearing_key": "sentencing-x",
                        "hearing_type": "sentencing",
                        "title": "Sentencing",
                        "local_date": "2026-04-14",
                        "local_time": "15:00",
                        "duration_minutes": 90,
                        "location": "Courtroom 4",
                        "judge": "Judge Y",
                        "reason": "first set",
                    }
                ],
            },
        )
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
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_HEARING",
                        "hearing_key": "sentencing-x",
                        "hearing_type": "sentencing",
                        "title": "Sentencing",
                        "local_date": "2026-04-14",
                        "local_time": "15:00",
                        "duration_minutes": 90,
                        "location": "Courtroom 4",
                    }
                ],
                2: [
                    {
                        "type": "RESCHEDULE_HEARING",
                        "hearing_key": "sentencing-x",
                        "title": "Sentencing",
                        "local_date": "2026-04-14",
                        "local_time": "11:00",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        # Original scheduling first ...
        syncer.process_entry(
            case, 100, _entry(1, "Sentencing set for 4/14/2026 03:00 PM")
        )
        # ... then the reschedule.
        syncer.process_entry(
            case,
            100,
            _entry(
                2, "Sentencing reset for 4/14/2026 11:00 AM", date_filed="2026-04-08"
            ),
        )
        rows = store.get_hearings("us-v-x")
        assert len(rows) == 1, (
            "RESCHEDULE_HEARING should update in place, not duplicate"
        )
        # 11:00 EDT → 15:00 UTC.
        assert rows[0]["starts_at_utc"] == "2026-04-14T15:00:00+00:00"
        assert set(rows[0]["source_entry_ids"]) == {1, 2}

    def test_mark_held(self, store: Store, case, monkeypatch):
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_HEARING",
                        "hearing_key": "sentencing-x",
                        "hearing_type": "sentencing",
                        "title": "Sentencing",
                        "local_date": "2026-04-14",
                        "local_time": "15:00",
                    }
                ],
                2: [{"type": "MARK_HELD", "hearing_key": "sentencing-x"}],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case, 100, _entry(1, "Sentencing set for 4/14/2026 03:00 PM")
        )
        syncer.process_entry(
            case, 100, _entry(2, "Minute Entry: Sentencing held on 4/14/2026")
        )
        rows = store.get_hearings("us-v-x")
        assert len(rows) == 1
        assert rows[0]["status"] == "held"

    def test_cancel(self, store: Store, case, monkeypatch):
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_HEARING",
                        "hearing_key": "sentencing-x",
                        "hearing_type": "sentencing",
                        "title": "Sentencing",
                        "local_date": "2026-04-14",
                        "local_time": "15:00",
                    }
                ],
                2: [
                    {
                        "type": "CANCEL_HEARING",
                        "hearing_key": "sentencing-x",
                        "notes": "vacated",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case, 100, _entry(1, "Sentencing set for 4/14/2026 03:00 PM")
        )
        syncer.process_entry(case, 100, _entry(2, "Sentencing vacated"))
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "cancelled"
        assert h["notes"] == "vacated"

    def test_update_details_adds_dial_in_without_changing_time(
        self, store: Store, case, monkeypatch
    ):
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_HEARING",
                        "hearing_key": "status-conf-x",
                        "hearing_type": "status_conference",
                        "title": "Status Conference",
                        "local_date": "2026-03-02",
                        "local_time": "10:30",
                        "duration_minutes": 30,
                    }
                ],
                2: [
                    {
                        "type": "UPDATE_DETAILS",
                        "hearing_key": "status-conf-x",
                        "title": "Status Conference",
                        "dial_in": "Zoom: meet.example/abc",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case, 100, _entry(1, "Status Conference set for 3/2/2026 at 10:30 AM")
        )
        syncer.process_entry(
            case, 100, _entry(2, "Hearing will be conducted via Zoom: meet.example/abc")
        )
        h = store.get_hearings("us-v-x")[0]
        assert h["dial_in"] == "Zoom: meet.example/abc"
        # Time unchanged.
        assert h["starts_at_utc"] == "2026-03-02T15:30:00+00:00"


# --- short-circuits ---


class TestShortCircuits:
    def test_irrelevant_entry_skips_llm_entirely(self, store: Store, case, monkeypatch):
        called = []

        def fake(**_):
            called.append("nope")
            return [{"type": "IGNORE"}]

        monkeypatch.setattr(llm_mod, "extract_actions", fake)

        cl = FakeCourtListener(
            dockets={100: _docket()},
            entries={
                100: [
                    _entry(1, "CERTIFICATE OF SERVICE by Peter B. Hegseth"),
                    _entry(2, "NOTICE OF ATTORNEY APPEARANCE for USA"),
                ]
            },
        )
        syncer = CaseSyncer(cl, store)
        stats = syncer.sync_case(case)
        assert stats["entries_processed"] == 0
        assert called == []

    def test_unchanged_docket_short_circuits_on_resync(
        self, store: Store, case, monkeypatch
    ):
        cl = FakeCourtListener(
            dockets={100: _docket()},
            entries={100: [_entry(1, "Sentencing set for 4/14/2026 03:00 PM")]},
        )
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_HEARING",
                        "hearing_key": "sentencing-x",
                        "hearing_type": "sentencing",
                        "title": "Sentencing",
                        "local_date": "2026-04-14",
                        "local_time": "15:00",
                    }
                ],
            },
        )
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
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: [e]})
        syncer = CaseSyncer(cl, store)
        syncer.sync_case(case)
        first = called[0]

        # Force the second sync to re-iterate the same entry by bumping the
        # docket date_modified (defeats the docket-level skip).
        cl._dockets[100] = _docket(date_modified="2026-06-01T00:00:00-07:00")
        syncer.sync_case(case)
        # Entry fingerprint hasn't changed, so the LLM stays at the same count.
        assert called[0] == first


class TestQuietSyncGatesSweeps:
    """When no docket in a case advances past the date-modified
    short-circuit, the LLM-backed verify / dedupe sweeps are skipped — their
    inputs come entirely from the store, which is unchanged, so at
    temperature=0 the verdicts can't differ from the prior sync. ``reverify``
    forces them; the time-driven ``_auto_mark_passed_stale`` always runs.
    """

    def _seed_quiet_docket(self, store, docket_id=100):
        """Make ``_docket()``'s date_modified short-circuit on the next sync.

        Caches court metadata too so the verify pass's ``ensure_docket_cached``
        resolves a timezone without a CourtListener fetch.
        """
        store.upsert_docket_meta(
            docket_id,
            {
                "court_id": "mad",
                "docket_number": "1:25-cr-00001-X",
                "case_name": "United States v. X",
                "absolute_url": f"/docket/{docket_id}/x/",
                "date_last_filing": "2026-05-01",
            },
        )
        store.set_docket_last_modified(docket_id, "2026-05-01T00:00:00-07:00")

    def _future_hearing(self, key="sentencing-x"):
        return {
            "case_id": "us-v-x",
            "hearing_key": key,
            "title": "Sentencing",
            "starts_at_utc": "2027-01-01T20:00:00+00:00",
            "timezone": "America/New_York",
            "status": "scheduled",
            "significance": "major",
            "docket_id": 100,
            "source_entry_ids": [1],
        }

    def _future_deadline(self, key="reply"):
        return {
            "case_id": "us-v-x",
            "deadline_key": key,
            "title": "Reply",
            "due_at_utc": "2027-02-01T21:00:00+00:00",
            "timezone": "America/New_York",
            "status": "pending",
            "significance": "major",
            "docket_id": 100,
            "source_entry_ids": [1],
        }

    def test_quiet_case_skips_verify_and_dedupe_sweeps(
        self, store: Store, case, monkeypatch
    ):
        self._seed_quiet_docket(store)
        # Rows that WOULD be verified / deduped if the gate didn't fire.
        store.upsert_hearing(self._future_hearing())
        store.upsert_deadline(self._future_deadline())

        def boom(*a, **k):
            raise AssertionError("sweep LLM called on a fully-quiet sync")

        monkeypatch.setattr(llm_mod, "verify_hearing", boom)
        monkeypatch.setattr(llm_mod, "verify_deadline", boom)
        monkeypatch.setattr(llm_mod, "resolve_duplicate_hearings", boom)

        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)

        assert stats["dockets_skipped"] == 1
        # No boom raised => the sweep LLMs were never called. Stats stay 0.
        assert stats["verified"] == 0
        assert stats["deadlines_verified"] == 0
        assert stats["deduped"] == 0
        assert stats["deduped_held"] == 0
        assert stats["deduped_nearslot"] == 0

    def test_reverify_forces_sweeps_on_quiet_case(
        self, store: Store, case, monkeypatch
    ):
        self._seed_quiet_docket(store)
        store.upsert_hearing(self._future_hearing())

        seen = []

        def rec_verify(*, hearing, **_):
            seen.append(hearing.get("hearing_key"))
            return {"type": "CONFIRM", "reason": "stub"}

        monkeypatch.setattr(llm_mod, "verify_hearing", rec_verify)

        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case, reverify=True)

        # Docket still short-circuited, but --reverify forced the sweep.
        assert stats["dockets_skipped"] == 1
        assert seen == ["sentencing-x"]

    def test_auto_passed_runs_on_quiet_case(self, store: Store, case, monkeypatch):
        self._seed_quiet_docket(store)

        def boom(*a, **k):
            raise AssertionError("verify sweep called on a quiet sync")

        monkeypatch.setattr(llm_mod, "verify_hearing", boom)
        monkeypatch.setattr(llm_mod, "verify_deadline", boom)

        # A future pending deadline (verify_deadline boom would fire on it if
        # the gate failed) plus a past-due one the time-driven sweep must flip.
        store.upsert_deadline(self._future_deadline(key="future-reply"))
        store.upsert_deadline(
            {
                "case_id": "us-v-x",
                "deadline_key": "stale-reply",
                "title": "Stale reply",
                "due_at_utc": "2024-01-01T22:00:00+00:00",
                "timezone": "America/New_York",
                "status": "pending",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [99],
            }
        )

        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)

        assert stats["dockets_skipped"] == 1
        # _auto_mark_passed_stale runs unconditionally despite the gate.
        assert stats["auto_passed"] == 1
        rows = {d["deadline_key"]: d["status"] for d in store.get_deadlines("us-v-x")}
        assert rows["stale-reply"] == "passed"
        assert rows["future-reply"] == "pending"

    def test_sibling_docket_advancing_runs_sweeps_for_quiet_docket(
        self, store: Store, case, monkeypatch
    ):
        # Multi-docket case: docket 100 is quiet, docket 200 lands a new
        # entry. The whole case's sweeps must run, so 100's row IS verified.
        self._seed_quiet_docket(store, docket_id=100)
        store.upsert_hearing(self._future_hearing())  # row lives on docket 100

        two_docket_case = CaseConfig(
            case_id="us-v-x",
            name="United States v. X",
            dockets=[100, 200],
            calendar="cyber",
        )
        d200 = {
            "id": 200,
            "court_id": "mad",
            "docket_number": "1:25-cr-00002-Y",
            "case_name": "United States v. X",
            "absolute_url": "/docket/200/y/",
            "date_modified": "2026-05-20T00:00:00-07:00",
            "date_last_filing": "2026-05-20",
        }

        seen = []

        def rec_verify(*, hearing, **_):
            seen.append(hearing.get("hearing_key"))
            return {"type": "CONFIRM", "reason": "stub"}

        monkeypatch.setattr(llm_mod, "verify_hearing", rec_verify)
        make_llm_stub(monkeypatch, by_entry={})  # docket 200's entry -> IGNORE

        cl = FakeCourtListener(
            dockets={100: _docket(), 200: d200},
            entries={
                100: [],
                200: [{**_entry(7, "NOTICE OF ATTORNEY APPEARANCE"), "docket": 200}],
            },
        )
        stats = CaseSyncer(cl, store).sync_case(two_docket_case)

        assert stats["dockets_skipped"] == 1  # only docket 100 short-circuited
        assert seen == ["sentencing-x"]  # 100's row verified because 200 moved


class TestInterruptDoesNotAdvanceCutoff:
    """Mid-sync interrupts must not advance the docket's date_last_modified.

    The docket-level short-circuit at the top of `sync_case` skips a
    docket entirely on the next run when its stored `date_last_modified`
    matches what CourtListener returns. AGENTS.md documents the
    invariant ``the docket last-modified cutoff is only advanced on a
    clean run, so a mid-sync error retries the whole docket on the next
    run`` — without it, an interrupt mid-iteration would mark the docket
    as caught-up and the unprocessed entries past the interrupt point
    would be permanently invisible until CourtListener bumped the
    docket again.

    The original implementation used ``except Exception: iterated_ok =
    False; raise; finally: if iterated_ok: bump_cutoff()``, which
    correctly handled Exception subclasses but silently let
    ``KeyboardInterrupt`` (Ctrl+C) and ``SystemExit`` — both
    ``BaseException`` subclasses, not caught by ``except Exception`` —
    fall through with ``iterated_ok=True``, advancing the cutoff
    despite the interrupted iteration. The fix removes the
    try/except/finally; the cutoff bump now sits after the loop in
    linear control flow, so any escaping exception (Exception or
    BaseException) leaves the cutoff at the prior value.
    """

    # The CourtListener-side date_modified for the docket — well past every
    # individual entry's date_modified seeded in the tests. This is the
    # value the OLD buggy `finally` block would have set on the docket
    # row, causing the next sync's docket-level short-circuit to fire
    # and skip the unprocessed entries entirely.
    _CL_DOCKET_MODIFIED = "2026-06-01T12:00:00-07:00"

    def _make_cl_that_blows_up_on_second_entry(self, exc_type):
        """FakeCourtListener subclass whose iter_entries yields one entry then raises."""

        class _Boom(FakeCourtListener):
            def iter_entries(self, docket_id, *, modified_after=None, **_):
                self.calls.append(("entries", docket_id))
                entries = self._entries.get(docket_id, [])
                for e in entries[:1]:
                    yield e
                raise exc_type("interrupted mid-iteration")

        return _Boom

    def _seed_prior_clean_sync(self, store, prior_cutoff: str) -> None:
        """Simulate a previous clean sync that left the cutoff at prior_cutoff."""
        store.upsert_docket_meta(
            100,
            {
                "court_id": "mad",
                "docket_number": "1:25-cr-00001-X",
                "case_name": "United States v. X",
                "absolute_url": "/d/100/",
            },
        )
        store.set_docket_last_modified(100, prior_cutoff)

    def _two_entries_well_before_cl_docket_modified(self):
        # Both entries' date_modified are strictly LESS than _CL_DOCKET_MODIFIED.
        # That gap is the assertion target: after an interrupt mid-iteration,
        # the docket cutoff must stay below the docket's CourtListener-side
        # date_modified, so the next sync's short-circuit doesn't fire.
        return [
            _entry(1, "first entry", date_filed="2026-01-15"),
            _entry(2, "should never reach", date_filed="2026-01-20"),
        ]

    def test_keyboard_interrupt_mid_iteration_keeps_cutoff_below_docket_modified(
        self, store, case, monkeypatch
    ):
        prior_cutoff = "2026-01-01T00:00:00-07:00"
        self._seed_prior_clean_sync(store, prior_cutoff)
        make_llm_stub(monkeypatch, by_entry={})

        cls = self._make_cl_that_blows_up_on_second_entry(KeyboardInterrupt)
        cl = cls(
            dockets={100: _docket(date_modified=self._CL_DOCKET_MODIFIED)},
            entries={100: self._two_entries_well_before_cl_docket_modified()},
        )
        syncer = CaseSyncer(cl, store)

        with pytest.raises(KeyboardInterrupt):
            syncer.sync_case(case)

        # Entry 1's per-entry commit ran before the interrupt — durable.
        assert (
            store.entry_seen(
                100,
                1,
                fingerprint_entry(_entry(1, "first entry", date_filed="2026-01-15")),
            )
            is True
        )
        # Critical invariant: the docket cutoff is strictly less than
        # the CourtListener-side date_modified. The next sync's docket-level
        # short-circuit will NOT fire (because they're unequal), so it
        # will iterate the docket again and pick up the entries the
        # interrupt left behind. The old buggy `finally` would have
        # equated them.
        assert store.docket_last_modified(100) is not None
        assert store.docket_last_modified(100) < self._CL_DOCKET_MODIFIED

    def test_system_exit_mid_iteration_keeps_cutoff_below_docket_modified(
        self, store, case, monkeypatch
    ):
        prior_cutoff = "2026-01-01T00:00:00-07:00"
        self._seed_prior_clean_sync(store, prior_cutoff)
        make_llm_stub(monkeypatch, by_entry={})

        cls = self._make_cl_that_blows_up_on_second_entry(SystemExit)
        cl = cls(
            dockets={100: _docket(date_modified=self._CL_DOCKET_MODIFIED)},
            entries={100: self._two_entries_well_before_cl_docket_modified()},
        )
        syncer = CaseSyncer(cl, store)

        with pytest.raises(SystemExit):
            syncer.sync_case(case)

        assert store.docket_last_modified(100) is not None
        assert store.docket_last_modified(100) < self._CL_DOCKET_MODIFIED

    def test_regular_exception_mid_iteration_keeps_cutoff_below_docket_modified(
        self, store, case, monkeypatch
    ):
        # Exception subclasses were already handled by the old
        # ``except Exception`` path. Pin the behavior so the refactor
        # doesn't quietly regress it.
        prior_cutoff = "2026-01-01T00:00:00-07:00"
        self._seed_prior_clean_sync(store, prior_cutoff)
        make_llm_stub(monkeypatch, by_entry={})

        cls = self._make_cl_that_blows_up_on_second_entry(RuntimeError)
        cl = cls(
            dockets={100: _docket(date_modified=self._CL_DOCKET_MODIFIED)},
            entries={100: self._two_entries_well_before_cl_docket_modified()},
        )
        syncer = CaseSyncer(cl, store)

        with pytest.raises(RuntimeError, match="interrupted"):
            syncer.sync_case(case)

        assert store.docket_last_modified(100) is not None
        assert store.docket_last_modified(100) < self._CL_DOCKET_MODIFIED

    def test_clean_iteration_does_advance_cutoff_to_docket_modified(
        self, store, case, monkeypatch
    ):
        # The fix must not break the happy path — a clean iteration
        # still bumps the cutoff all the way to the docket's CourtListener-side
        # date_modified at end-of-loop.
        prior_cutoff = "2026-01-01T00:00:00-07:00"
        self._seed_prior_clean_sync(store, prior_cutoff)
        make_llm_stub(monkeypatch, by_entry={})

        cl = FakeCourtListener(
            dockets={100: _docket(date_modified=self._CL_DOCKET_MODIFIED)},
            entries={100: self._two_entries_well_before_cl_docket_modified()},
        )
        syncer = CaseSyncer(cl, store)
        syncer.sync_case(case)

        assert store.docket_last_modified(100) == self._CL_DOCKET_MODIFIED

    def test_empty_docket_modified_skips_cutoff_write(self, store, case, monkeypatch):
        # Defensive case: CourtListener should always populate
        # `date_modified` on a docket record, but the code guards
        # against an empty / missing value anyway. Writing "" as the
        # cutoff would let the next docket-level short-circuit
        # misbehave on a string-vs-empty compare, so the explicit
        # skip is the documented contract. This pins the falsy path
        # of `if docket_mod:` after the iteration loop — without it
        # codecov sees the post-fix region as having a partial branch.
        prior_cutoff = "2026-01-01T00:00:00-07:00"
        self._seed_prior_clean_sync(store, prior_cutoff)
        make_llm_stub(monkeypatch, by_entry={})

        # CourtListener returns a docket without `date_modified` — coerced to "".
        cl = FakeCourtListener(
            dockets={100: _docket(date_modified="")},
            entries={100: []},
        )
        syncer = CaseSyncer(cl, store)
        syncer.sync_case(case)

        # The end-of-loop cutoff bump did NOT fire (its guard saw an
        # empty `docket_mod`), so the stored value is still the prior
        # clean cutoff — not blank.
        assert store.docket_last_modified(100) == prior_cutoff


class TestDocketMetaCaching:
    def test_court_fetched_once(self, store: Store, case, monkeypatch):
        make_llm_stub(monkeypatch, by_entry={})
        cl = FakeCourtListener(
            dockets={100: _docket()},
            entries={100: [_entry(1, "x")]},
            courts={
                "mad": {
                    "citation_string": "D. Mass.",
                    "short_name": "Massachusetts",
                    "full_name": "District of Massachusetts",
                }
            },
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
        cl = FakeCourtListener(
            dockets={100: _docket()}, entries={100: [_entry(1, "x")]}
        )
        syncer = CaseSyncer(cl, store)
        syncer.sync_case(case)
        meta = must(store.get_docket_meta(100))
        assert meta["court_id"] == "mad"
        assert meta["docket_number"] == "1:25-cr-00001-X"


class TestLastFilingDateCapture:
    """The index page's "Last filing" date is sourced from CourtListener's
    ``date_last_filing`` (not ``date_modified``, which bumps on OCR /
    metadata churn). Verify both capture paths: full polling sync, and
    the webhook ``process_entry`` opportunistic bump.
    """

    def test_polling_captures_date_last_filing(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        make_llm_stub(monkeypatch, by_entry={})
        cl = FakeCourtListener(
            dockets={100: _docket(date_last_filing="2026-05-08")},
            entries={100: [_entry(1, "x")]},
        )
        syncer = CaseSyncer(cl, store)
        syncer.sync_case(case)
        meta = must(store.get_docket_meta(100))
        assert meta["date_last_filing"] == "2026-05-08"

    def test_webhook_bumps_last_filing_from_entry(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # Pre-seed the docket meta with an older date_last_filing — this
        # simulates the polling pass having captured CourtListener's value, and now
        # a webhook delivers an entry filed AFTER that capture.
        store.upsert_docket_meta(
            100,
            {
                "court_id": "mad",
                "docket_number": "1:25-cr-00001-X",
                "case_name": "X",
                "absolute_url": "/d/100/",
                "date_last_filing": "2026-05-01",
            },
        )
        store.upsert_court("mad", "D. Mass.", "mad", "District of Massachusetts")
        make_llm_stub(monkeypatch, by_entry={})
        cl = FakeCourtListener(dockets={100: _docket(date_last_filing="2026-05-01")})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100, _entry(1, "x", date_filed="2026-05-10"))
        assert must(store.get_docket_meta(100))["date_last_filing"] == "2026-05-10"

    def test_polling_captures_last_filing_on_short_circuit(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # Quiet dockets (unchanged since last sync) hit the short-circuit
        # in sync_case before upsert_docket_meta would normally run. We
        # still need to populate date_last_filing on those — otherwise
        # the column stays NULL for every docket that hasn't moved since
        # the migration landed, and the index shows empty dates.
        # Pre-seed the cutoff so the short-circuit fires.
        store.set_docket_last_modified(100, "2026-05-01T00:00:00-07:00")
        make_llm_stub(monkeypatch, by_entry={})
        cl = FakeCourtListener(
            dockets={
                100: _docket(
                    date_modified="2026-05-01T00:00:00-07:00",
                    date_last_filing="2026-04-28",
                )
            },
            entries={100: []},
        )
        syncer = CaseSyncer(cl, store)
        stats = syncer.sync_case(case)
        assert stats["dockets_skipped"] == 1
        assert must(store.get_docket_meta(100))["date_last_filing"] == "2026-04-28"

    def test_webhook_does_not_move_last_filing_backwards(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # Out-of-order delivery: an older entry arriving after CourtListener's
        # date_last_filing has already advanced must not regress the
        # cutoff.
        store.upsert_docket_meta(
            100,
            {
                "court_id": "mad",
                "docket_number": "1:25-cr-00001-X",
                "case_name": "X",
                "absolute_url": "/d/100/",
                "date_last_filing": "2026-05-08",
            },
        )
        store.upsert_court("mad", "D. Mass.", "mad", "District of Massachusetts")
        make_llm_stub(monkeypatch, by_entry={})
        cl = FakeCourtListener(dockets={100: _docket(date_last_filing="2026-05-08")})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100, _entry(1, "x", date_filed="2026-04-01"))
        assert must(store.get_docket_meta(100))["date_last_filing"] == "2026-05-08"


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
            "id": 200,
            "court_id": "cadc",
            "docket_number": "26-1049",
            "case_name": "X",
            "absolute_url": "/d/200/",
            "date_modified": "2026-05-01T00:00:00-07:00",
        }
        # Second sight: cand (PT) sibling docket references the same hearing.
        cand_docket = {
            "id": 300,
            "court_id": "cand",
            "docket_number": "3:26-cv-1996",
            "case_name": "X",
            "absolute_url": "/d/300/",
            "date_modified": "2026-05-02T00:00:00-07:00",
        }
        cl = FakeCourtListener(dockets={200: cadc_docket, 300: cand_docket})

        case_multi = CaseConfig(case_id="x", name="X", dockets=[200, 300], calendar="t")

        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_HEARING",
                        "hearing_key": "oral-arg",
                        "hearing_type": "oral_argument",
                        "title": "Oral Argument",
                        "local_date": "2026-05-19",
                        "local_time": None,
                    }
                ],
                2: [
                    {
                        "type": "UPDATE_DETAILS",
                        "hearing_key": "oral-arg",
                        "title": "Oral Argument",
                        "notes": "cand reference: see appellate calendar",
                    }
                ],
            },
        )

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


class TestCrossCourtContextFilter:
    """The per-entry extractor receives known_hearings/known_deadlines context
    scoped to the current docket's COURT, not the whole case. Without this
    filter, a "stay appellate proceedings" order in one venue would propagate
    CANCEL_HEARING actions onto a parallel proceeding's events in another venue.
    """

    def test_cross_court_siblings_are_filtered_from_llm_context(
        self,
        store: Store,
        monkeypatch,
    ):
        cadc_docket = {
            "id": 200,
            "court_id": "cadc",
            "docket_number": "26-1049",
            "case_name": "X",
            "absolute_url": "/d/200/",
            "date_modified": "2026-05-01T00:00:00-07:00",
        }
        ca9_docket = {
            "id": 300,
            "court_id": "ca9",
            "docket_number": "26-2011",
            "case_name": "X",
            "absolute_url": "/d/300/",
            "date_modified": "2026-05-02T00:00:00-07:00",
        }
        cl = FakeCourtListener(dockets={200: cadc_docket, 300: ca9_docket})
        case_multi = CaseConfig(
            case_id="x",
            name="X",
            dockets=[200, 300],
            calendar="t",
        )

        # Seed a hearing + deadline on the D.C. Cir. docket.
        store.upsert_docket_meta(200, cadc_docket)
        store.upsert_docket_meta(300, ca9_docket)
        store.upsert_hearing(
            {
                "case_id": "x",
                "hearing_key": "oral-arg-dc",
                "title": "Oral Argument",
                "starts_at_utc": "2026-05-19T13:30:00+00:00",
                "duration_minutes": 30,
                "timezone": "America/New_York",
                "location": None,
                "judge": None,
                "notes": None,
                "dial_in": None,
                "status": "scheduled",
                "significance": "major",
                "gcal_event_id": None,
                "docket_id": 200,
                "source_entry_ids": [10],
            }
        )
        store.upsert_deadline(
            {
                "case_id": "x",
                "deadline_key": "reply-brief-dc",
                "title": "Petitioner Reply Brief",
                "due_at_utc": "2026-05-13T21:00:00+00:00",
                "timezone": "America/New_York",
                "notes": None,
                "status": "pending",
                "significance": "major",
                "deadline_type": "brief",
                "gcal_event_id": None,
                "docket_id": 200,
                "source_entry_ids": [10],
            }
        )

        # Capture kwargs the LLM stub receives when we process a 9th Cir. entry.
        captured: dict = {}

        def fake(*, known_hearings, known_deadlines, **_):
            captured["hearings"] = known_hearings
            captured["deadlines"] = known_deadlines
            return [{"type": "IGNORE", "reason": "stub"}]

        monkeypatch.setattr(llm_mod, "extract_actions", fake)

        syncer = CaseSyncer(cl, store)
        # 9th Cir. entry that mentions a stay — the bug being guarded against
        # is the LLM seeing the D.C. Cir. events and emitting CANCEL_HEARING actions
        # against them. The fix is upstream of the LLM: don't feed them in.
        syncer.process_entry(
            case_multi,
            300,
            _entry(42, "ORDER granting unopposed motion to stay appellate proceedings"),
        )

        keys = {h["hearing_key"] for h in captured["hearings"]}
        d_keys = {d["deadline_key"] for d in captured["deadlines"]}
        assert "oral-arg-dc" not in keys
        assert "reply-brief-dc" not in d_keys

    def test_same_court_siblings_still_aggregate(
        self,
        store: Store,
        monkeypatch,
    ):
        # Multi-defendant criminal: two dockets in the same court should still
        # see each other's events (legitimate co-defendant aggregation).
        a = {
            "id": 400,
            "court_id": "dcd",
            "docket_number": "1:24-cr-261-A",
            "case_name": "X",
            "absolute_url": "/d/400/",
            "date_modified": "2026-01-01T00:00:00-05:00",
        }
        b = {
            "id": 401,
            "court_id": "dcd",
            "docket_number": "1:24-cr-261-B",
            "case_name": "X",
            "absolute_url": "/d/401/",
            "date_modified": "2026-01-02T00:00:00-05:00",
        }
        cl = FakeCourtListener(dockets={400: a, 401: b})
        case_multi = CaseConfig(case_id="x", name="X", dockets=[400, 401], calendar="t")

        store.upsert_docket_meta(400, a)
        store.upsert_docket_meta(401, b)
        store.upsert_hearing(
            {
                "case_id": "x",
                "hearing_key": "arraignment-a",
                "title": "Arraignment",
                "starts_at_utc": "2026-01-15T14:00:00+00:00",
                "duration_minutes": 30,
                "timezone": "America/New_York",
                "location": None,
                "judge": None,
                "notes": None,
                "dial_in": None,
                "status": "held",
                "significance": "major",
                "gcal_event_id": None,
                "docket_id": 400,
                "source_entry_ids": [1],
            }
        )

        captured: dict = {}

        def fake(*, known_hearings, **_):
            captured["hearings"] = known_hearings
            return [{"type": "IGNORE", "reason": "stub"}]

        monkeypatch.setattr(llm_mod, "extract_actions", fake)

        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case_multi, 401, _entry(2, "ARRAIGNMENT held"))

        keys = {h["hearing_key"] for h in captured["hearings"]}
        assert "arraignment-a" in keys


class TestCrossCourtActionGuard:
    """The LLM context filter prevents the model from *seeing* cross-court
    rows on the same case, but ``_apply_action`` / ``_apply_deadline_action``
    look up ``existing`` by ``(case_id, key)`` only. When an LLM in court B
    independently invents a kebab-case key that happens to collide with an
    existing court-A row (generic slugs like ``petitioner-reply-brief-
    appellate`` are hit-prone), the court-B entry would otherwise pollute
    the court-A row's source_entry_ids and could clobber its fields. The
    apply-layer guard rejects the action entirely.
    """

    def _seed_aggregated_case(self, store):
        cadc = {
            "id": 200,
            "court_id": "cadc",
            "docket_number": "26-1049",
            "case_name": "X",
            "absolute_url": "/d/200/",
            "date_modified": "2026-05-01T00:00:00-07:00",
        }
        ca9 = {
            "id": 300,
            "court_id": "ca9",
            "docket_number": "26-2011",
            "case_name": "X",
            "absolute_url": "/d/300/",
            "date_modified": "2026-05-02T00:00:00-07:00",
        }
        store.upsert_docket_meta(200, cadc)
        store.upsert_docket_meta(300, ca9)
        case_multi = CaseConfig(
            case_id="x",
            name="X",
            dockets=[200, 300],
            calendar="t",
        )
        cl = FakeCourtListener(dockets={200: cadc, 300: ca9})
        return cl, case_multi

    def test_cross_court_deadline_action_rejected(
        self,
        store: Store,
        monkeypatch,
    ):
        cl, case_multi = self._seed_aggregated_case(store)
        # Seed the D.C. Cir. row.
        store.upsert_deadline(
            {
                "case_id": "x",
                "deadline_key": "petitioner-reply-brief-appellate",
                "title": "Petitioner's Reply Brief",
                "due_at_utc": "2026-05-13T21:00:00+00:00",
                "timezone": "America/New_York",
                "notes": "Original",
                "status": "pending",
                "significance": "major",
                "deadline_type": "reply",
                "gcal_event_id": None,
                "docket_id": 200,
                "source_entry_ids": [101],
            }
        )
        # 9th Cir. entry whose LLM invents the SAME deadline_key. Without
        # the guard, this entry would land on source_entry_ids and possibly
        # rewrite fields. With the guard, action is dropped.
        make_llm_stub(
            monkeypatch,
            by_entry={
                42: [
                    {
                        "type": "RESCHEDULE_DEADLINE",
                        "deadline_key": "petitioner-reply-brief-appellate",
                        "title": "Petitioner's Reply Brief",
                        "local_date": "2026-06-01",
                        "local_time": None,
                        "deadline_type": "reply",
                        "significance": "major",
                    }
                ],
            },
        )
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case_multi,
            300,
            _entry(
                42,
                "ORDER stay appellate proceedings granted; brief schedule moved",
            ),
        )

        d = must(store.get_deadline("x", "petitioner-reply-brief-appellate"))
        # Unchanged: still owned by D.C. Cir.; date and notes intact;
        # ca9 entry 42 NOT folded into source_entry_ids.
        assert d["docket_id"] == 200
        assert d["due_at_utc"] == "2026-05-13T21:00:00+00:00"
        assert d["notes"] == "Original"
        assert d["source_entry_ids"] == [101]

    def test_cross_court_hearing_action_rejected(
        self,
        store: Store,
        monkeypatch,
    ):
        cl, case_multi = self._seed_aggregated_case(store)
        store.upsert_hearing(
            {
                "case_id": "x",
                "hearing_key": "oral-arg",
                "title": "Oral Argument",
                "starts_at_utc": "2026-05-19T13:30:00+00:00",
                "duration_minutes": 30,
                "timezone": "America/New_York",
                "location": None,
                "judge": None,
                "notes": None,
                "dial_in": None,
                "status": "scheduled",
                "significance": "major",
                "gcal_event_id": None,
                "docket_id": 200,
                "source_entry_ids": [101],
            }
        )
        # 9th Cir. entry inventing a colliding hearing_key.
        make_llm_stub(
            monkeypatch,
            by_entry={
                42: [
                    {
                        "type": "UPDATE_DETAILS",
                        "hearing_key": "oral-arg",
                        "notes": "ca9 reference",
                    }
                ],
            },
        )
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case_multi,
            300,
            _entry(
                42,
                "ORDER referencing oral argument in D.C. Cir.",
            ),
        )

        h = must(store.get_hearing("x", "oral-arg"))
        assert h["docket_id"] == 200
        assert h["notes"] is None  # not overwritten with the ca9 string
        assert h["source_entry_ids"] == [101]

    def test_same_court_sibling_docket_still_allowed(
        self,
        store: Store,
        monkeypatch,
    ):
        # Co-defendant aggregation: same court, two dockets. A sibling
        # docket in the SAME court can legitimately touch the row.
        a = {
            "id": 400,
            "court_id": "dcd",
            "docket_number": "1:24-cr-261-A",
            "case_name": "X",
            "absolute_url": "/d/400/",
            "date_modified": "2026-01-01T00:00:00-05:00",
        }
        b = {
            "id": 401,
            "court_id": "dcd",
            "docket_number": "1:24-cr-261-B",
            "case_name": "X",
            "absolute_url": "/d/401/",
            "date_modified": "2026-01-02T00:00:00-05:00",
        }
        store.upsert_docket_meta(400, a)
        store.upsert_docket_meta(401, b)
        case_multi = CaseConfig(case_id="x", name="X", dockets=[400, 401], calendar="t")
        cl = FakeCourtListener(dockets={400: a, 401: b})
        store.upsert_hearing(
            {
                "case_id": "x",
                "hearing_key": "status-conf",
                "title": "Status Conference",
                "starts_at_utc": "2026-02-10T14:00:00+00:00",
                "duration_minutes": 30,
                "timezone": "America/New_York",
                "location": None,
                "judge": None,
                "notes": None,
                "dial_in": None,
                "status": "scheduled",
                "significance": "major",
                "gcal_event_id": None,
                "docket_id": 400,
                "source_entry_ids": [1],
            }
        )
        make_llm_stub(
            monkeypatch,
            by_entry={
                2: [
                    {
                        "type": "MARK_HELD",
                        "hearing_key": "status-conf",
                        "local_date": "2026-02-10",
                    }
                ],
            },
        )
        syncer = CaseSyncer(cl, store)
        # Co-defendant docket 401 (same court) MARK_HELDs the row.
        syncer.process_entry(
            case_multi,
            401,
            _entry(
                2,
                "Minute entry: status conference held",
            ),
        )

        h = must(store.get_hearing("x", "status-conf"))
        assert h["status"] == "held"
        # Source entries gained the sibling-docket entry — legit aggregation.
        assert h["source_entry_ids"] == [1, 2]

    def test_no_metadata_falls_through_for_backcompat(
        self,
        store: Store,
        monkeypatch,
    ):
        # Old data: existing row carries no docket_id. Can't determine its
        # court, so the guard falls through and behaves as before. This
        # preserves backward compatibility on rows from pre-docket_id eras.
        case_local = CaseConfig(case_id="legacy", name="L", dockets=[100], calendar="t")
        store.upsert_docket_meta(100, _docket())
        store.upsert_hearing(
            {
                "case_id": "legacy",
                "hearing_key": "h1",
                "title": "Hearing",
                "starts_at_utc": "2026-05-01T14:00:00+00:00",
                "duration_minutes": 30,
                "timezone": "America/New_York",
                "location": None,
                "judge": None,
                "notes": None,
                "dial_in": None,
                "status": "scheduled",
                "significance": "major",
                "gcal_event_id": None,
                "docket_id": None,
                "source_entry_ids": [],
            }
        )
        make_llm_stub(
            monkeypatch,
            by_entry={
                7: [
                    {
                        "type": "MARK_HELD",
                        "hearing_key": "h1",
                        "local_date": "2026-05-01",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case_local,
            100,
            _entry(
                7,
                "Minute entry: hearing held",
            ),
        )
        h = must(store.get_hearing("legacy", "h1"))
        assert h["status"] == "held"


class TestProcessEntryDirect:
    """``process_entry`` is the entry point the webhook server uses."""

    def test_processes_a_single_entry(self, store: Store, case, monkeypatch):
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_HEARING",
                        "hearing_key": "sentencing-x",
                        "hearing_type": "sentencing",
                        "title": "Sentencing",
                        "local_date": "2026-04-14",
                        "local_time": "15:00",
                        "duration_minutes": 90,
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})  # no entries pre-loaded
        syncer = CaseSyncer(cl, store)
        e = _entry(1, "Sentencing set for 4/14/2026 03:00 PM")
        was_processed = syncer.process_entry(case, 100, e)
        assert was_processed is True
        assert len(store.get_hearings("us-v-x")) == 1

    def test_dedup_returns_false(self, store: Store, case, monkeypatch):
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_HEARING",
                        "hearing_key": "x",
                        "title": "T",
                        "local_date": "2026-04-14",
                        "local_time": "15:00",
                        "hearing_type": "sentencing",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        e = _entry(1, "Sentencing set for 4/14/2026 03:00 PM")
        assert syncer.process_entry(case, 100, e) is True
        # Second call with identical entry should be a no-op.
        assert syncer.process_entry(case, 100, e) is False

    def test_action_without_hearing_key_logs_and_drops(
        self,
        store: Store,
        case,
        monkeypatch,
        caplog,
    ):
        # Defensive guard: an LLM returning a hearing-shaped action with
        # no hearing_key would crash the store layer at the PRIMARY KEY
        # boundary if we tried to insert it. The handler logs and drops
        # instead. (The LLM prompt forbids this shape but the cheap
        # extractor is occasionally creative.)
        import logging

        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_HEARING",
                        "title": "Sentencing",
                        "hearing_type": "sentencing",
                        "local_date": "2026-04-14",
                        "local_time": "15:00",
                        # missing "hearing_key"
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        with caplog.at_level(logging.WARNING, logger="case_calendar.sync"):
            syncer.process_entry(
                case,
                100,
                _entry(1, "Sentencing set for 4/14/2026 03:00 PM"),
            )
        assert store.get_hearings("us-v-x") == []
        assert any("action without hearing_key" in r.message for r in caplog.records), [
            r.message for r in caplog.records
        ]

    def test_ensure_court_noop_on_empty_court_id(self, store: Store, case):
        # The `_ensure_court` guard short-circuits on missing court_id —
        # CourtListener is never called and nothing is written.
        class _BoomCourtListener(FakeCourtListener):
            def get_court(self, court_id):  # type: ignore[override]
                raise AssertionError("get_court must not run on empty court_id")

        syncer = CaseSyncer(_BoomCourtListener(), store)
        syncer._ensure_court("")  # no-op
        syncer._ensure_court(None)  # no-op


class TestRecapDocumentsPersisted:
    """The compact recap_documents JSON we render at emit time is owned by
    process_entry. New docs landing on an existing entry must overwrite
    the cached JSON so the calendar reflects them on next emit."""

    def test_docs_persisted_for_relevant_entry(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_HEARING",
                        "hearing_key": "sentencing-x",
                        "hearing_type": "sentencing",
                        "title": "Sentencing",
                        "local_date": "2026-04-14",
                        "local_time": "15:00",
                        "duration_minutes": 90,
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        e = _entry(1, "Sentencing set for 4/14/2026 03:00 PM")
        e["recap_documents"] = [
            {
                "id": 5,
                "document_number": 65,
                "attachment_number": None,
                "is_available": True,
                "is_sealed": False,
                "filepath_ia": "https://archive.org/65.pdf",
                "filepath_local": None,
                "description": "",
            },
        ]
        assert syncer.process_entry(case, 100, e) is True
        got = store.get_entry_documents([1])
        assert got[1][0]["filepath_ia"] == "https://archive.org/65.pdf"

    def test_docs_refresh_when_attachment_added(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # First sync sees the main doc; later sync sees main + attachment.
        # Fingerprint changes (is_available + new doc row), entry
        # re-processes, persisted JSON updates so emit picks up both URLs.
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "UPDATE_DETAILS",
                        "hearing_key": "sentencing-x",
                        "reason": "no change",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)

        first = _entry(1, "ORDER Setting Sentencing for 4/14/2026 03:00 PM")
        first["recap_documents"] = [
            {
                "id": 5,
                "document_number": 65,
                "attachment_number": None,
                "is_available": True,
                "is_sealed": False,
                "filepath_ia": "https://archive.org/65.pdf",
            },
        ]
        syncer.process_entry(case, 100, first)

        second = _entry(
            1,
            "ORDER Setting Sentencing for 4/14/2026 03:00 PM",
            date_filed="2026-01-02",
        )
        second["recap_documents"] = [
            {
                "id": 5,
                "document_number": 65,
                "attachment_number": None,
                "is_available": True,
                "is_sealed": False,
                "filepath_ia": "https://archive.org/65.pdf",
            },
            {
                "id": 6,
                "document_number": 65,
                "attachment_number": 1,
                "is_available": True,
                "is_sealed": False,
                "filepath_ia": "https://archive.org/65a.pdf",
            },
        ]
        assert syncer.process_entry(case, 100, second) is True
        got = store.get_entry_documents([1])
        urls = [d["filepath_ia"] for d in got[1]]
        assert urls == [
            "https://archive.org/65.pdf",
            "https://archive.org/65a.pdf",
        ]


class TestCancelOnUnknownKey:
    """Adjournment memo for a hearing whose original scheduling entry was
    filtered out before reaching the LLM should still leave a cancelled
    audit-trail row, not silently drop."""

    def test_cancel_with_local_date_inserts_cancelled_row(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "CANCEL_HEARING",
                        "hearing_key": "status-conf-x-7",
                        "title": "Status Conference",
                        "local_date": "2023-07-18",
                        "notes": "adjourned by court",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case,
            100,
            _entry(
                1,
                "ENDORSEMENT: status conference "
                "previously scheduled for July 18, "
                "2023 is hereby adjourned",
            ),
        )
        rows = store.get_hearings("us-v-x")
        assert len(rows) == 1
        assert rows[0]["status"] == "cancelled"
        assert rows[0]["hearing_key"] == "status-conf-x-7"
        assert rows[0]["starts_at_utc"].startswith("2023-07-18")

    def test_cancel_without_local_date_drops(
        self,
        store: Store,
        case,
        monkeypatch,
        caplog,
    ):
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [{"type": "CANCEL_HEARING", "hearing_key": "status-conf-x-7"}],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100, _entry(1, "ENDORSEMENT: hearing adjourned"))
        assert store.get_hearings("us-v-x") == []
        assert any(
            "CANCEL_HEARING on unknown key with no local_date" in r.message
            for r in caplog.records
        )


class TestMarkHeldOnUnknownKey:
    """Held minute entry for a hearing whose scheduling never reached the
    store should ADD_HEARING a new row in 'held' status."""

    def test_mark_held_with_local_date_inserts_held_row(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "MARK_HELD",
                        "hearing_key": "cipa-hearing-x",
                        "title": "CIPA Hearing",
                        "local_date": "2023-03-06",
                        "significance": "major",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case, 100, _entry(1, "Minute Entry: CIPA Hearing held on 3/6/2023")
        )
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
        self,
        store: Store,
        case,
        monkeypatch,
        caplog,
    ):
        # Set up: existing scheduled hearing on 2023-03-08.
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_HEARING",
                        "hearing_key": "status-conf-x",
                        "hearing_type": "status_conference",
                        "title": "Status Conf",
                        "local_date": "2023-03-08",
                        "local_time": "12:30",
                    }
                ],
                # LLM tries to MARK_HELD this key using a 3/6 minute entry —
                # 2 days off is borderline-fine, but 3+ days off is rejected.
                2: [
                    {
                        "type": "MARK_HELD",
                        "hearing_key": "status-conf-x",
                        "local_date": "2023-03-04",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case, 100, _entry(1, "Status Conference set for 3/8/2023 12:30 PM")
        )
        syncer.process_entry(
            case, 100, _entry(2, "Minute Entry: Hearing held on 3/4/2023")
        )
        rows = store.get_hearings("us-v-x")
        assert len(rows) == 1
        # Status should NOT have flipped to held — date mismatch rejected.
        assert rows[0]["status"] == "scheduled"
        assert any("MARK_HELD date mismatch" in r.message for r in caplog.records)

    def test_mark_held_within_tolerance_still_applies(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # 1-day diff (e.g. minute entry filed day after hearing) is fine.
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_HEARING",
                        "hearing_key": "sentencing-x",
                        "hearing_type": "sentencing",
                        "title": "Sentencing",
                        "local_date": "2026-04-14",
                        "local_time": "15:00",
                    }
                ],
                2: [
                    {
                        "type": "MARK_HELD",
                        "hearing_key": "sentencing-x",
                        "local_date": "2026-04-15",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100, _entry(1, "Sentencing set for 4/14/2026 3 PM"))
        syncer.process_entry(case, 100, _entry(2, "Sentencing held on 4/14/2026"))
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "held"


class TestPastScheduledHearings:
    """Past-dated 'scheduled' rows are audited by the LLM verify pass,
    not by a dumb date-based sweep.

    The replaced ``_auto_mark_held_stale`` heuristic assumed
    "date passed → MARK_HELD", which produced false 'held' status on
    trials that were continued or vacated by guilty plea without an
    explicit cancellation entry. The us-v-moucka regression is the
    canonical case: trial set 4/13/2026, change-of-plea stricken on
    3/24, no further entries — the auto-held sweep flipped the trial
    to 'held' even though the summary LLM correctly stated no verdict
    or judgment confirmed the trial occurred. Verify-pass-only status
    transitions are the fix.
    """

    def _seed_past_scheduled(self, store, key, title="Status Conference"):
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": key,
                "title": title,
                "starts_at_utc": "2024-01-01T12:00:00+00:00",
                "duration_minutes": 240 if title == "Jury Trial" else 30,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [],
            }
        )

    def test_mark_held_flips_past_row_when_llm_cites_evidence(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # The expected success path: LLM sees a minute entry for the
        # hearing's date and returns MARK_HELD. Past-dated row updates.
        self._seed_past_scheduled(store, key="past-conf")
        stub_verify(
            monkeypatch,
            by_key={
                "past-conf": {
                    "type": "MARK_HELD",
                    "reason": "minute entry 'Status Conference held on 1/1/2024'",
                },
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["verified"] == 1
        assert store.get_hearings("us-v-x")[0]["status"] == "held"

    def test_unclear_leaves_past_row_as_scheduled(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # The Moucka regression case: trial date passed, docket silent
        # on whether it actually happened. The LLM returns UNCLEAR, the
        # row stays 'scheduled' — accurately reflecting "outcome not
        # confirmed". A later sync after more entries land will re-check.
        self._seed_past_scheduled(store, key="trial-moucka", title="Jury Trial")
        stub_verify(
            monkeypatch,
            by_key={
                "trial-moucka": {
                    "type": "UNCLEAR",
                    "reason": "no minute entry, verdict, or transcript on the docket; "
                    "trial may have been vacated by plea",
                },
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["verified"] == 0
        h = store.get_hearings("us-v-x")[0]
        # Stays scheduled — explicitly NOT flipped to 'held' on date alone.
        assert h["status"] == "scheduled"

    def test_cancel_flips_past_row_when_docket_shows_vacatur(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # LLM sees a plea agreement / order vacating trial → CANCEL_HEARING.
        self._seed_past_scheduled(store, key="trial-x", title="Jury Trial")
        stub_verify(
            monkeypatch,
            by_key={
                "trial-x": {
                    "type": "CANCEL_HEARING",
                    "reason": "trial vacated by plea agreement",
                },
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["verified"] == 1
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "cancelled"
        # Verify-pass reason lands in audit_notes, never in notes — so the
        # verify LLM can't read its own prior conclusion on the next sync.
        assert "plea" in (h["audit_notes"] or "")
        assert "[verify-pass]" in (h["audit_notes"] or "")
        # notes was empty when the row was seeded; verify pass must NOT
        # have touched it.
        assert not (h["notes"] or "").strip()

    def test_no_separate_auto_held_sweep(self, store: Store, case, monkeypatch):
        # sync_case stats no longer carry an 'auto_held' key — the
        # behavior is folded into 'verified'. Regression guard: if
        # someone re-adds an auto_held sweep, this test pins the
        # change with deliberate intent.
        self._seed_past_scheduled(store, key="x")
        stub_verify(monkeypatch)  # default CONFIRM → no-op
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert "auto_held" not in stats
        # CONFIRM is a no-op so past row stays as 'scheduled'.
        assert store.get_hearings("us-v-x")[0]["status"] == "scheduled"

    def test_future_cancelled_row_skipped_by_verify(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # Future 'cancelled' rows are NOT verified — a deliberately
        # cancelled future hearing should stay cancelled until something
        # actively un-cancels it. Only PAST 'cancelled' rows are checked
        # for inverse-Moucka false-cancellations (see
        # TestPastCancelledHearings below).
        from datetime import datetime, timedelta, timezone

        future_iso = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "future-cancelled",
                "title": "Status Conference",
                "starts_at_utc": future_iso,
                "duration_minutes": 30,
                "timezone": "America/New_York",
                "status": "cancelled",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [],
            }
        )

        def boom(**_):
            raise AssertionError("verify_hearing called for a future cancelled row")

        monkeypatch.setattr(llm_mod, "verify_hearing", boom)
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        CaseSyncer(cl, store).sync_case(case)
        assert store.get_hearings("us-v-x")[0]["status"] == "cancelled"


class TestPastCancelledHearings:
    """The inverse-Moucka path: past 'cancelled' rows ARE verified, so a
    cancellation that was inferred-but-not-supported (a prior pass
    flipped the row without an explicit vacatur entry, but the case
    has continued to be actively briefed past the cancelled hearing's
    date) can be reverted to 'scheduled'.

    The us-v-mcgonigal regression is the canonical case: trial set for
    6/12/2024 with the only source entry being the 2023-05-30 scheduling
    order, status flipped to 'cancelled' on inference, but the docket
    continued to have body-bearing activity through 2025 — the case is
    plainly still live.
    """

    def _seed_past_cancelled(self, store, key="trial-x", title="Jury Trial"):
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": key,
                "title": title,
                # Past, but not ancient — within the verify pass's working window.
                "starts_at_utc": "2024-06-12T14:00:00+00:00",
                "duration_minutes": 240,
                "timezone": "America/New_York",
                "status": "cancelled",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [46],
            }
        )

    def test_reinstate_reverts_to_scheduled(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # The LLM finds no explicit vacatur AND sees that the docket
        # continued to be active past the cancelled date — REINSTATE.
        self._seed_past_cancelled(store, key="trial-mcgonigal")
        stub_verify(
            monkeypatch,
            by_key={
                "trial-mcgonigal": {
                    "type": "REINSTATE",
                    "reason": "No vacatur, dismissal, or plea entry; case continued to be "
                    "actively briefed past 6/12/2024.",
                },
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["verified"] == 1
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "scheduled"
        assert "[verify-pass]" in (h["audit_notes"] or "")
        assert "Cancellation not supported" in (
            h["audit_notes"] or ""
        ) or "No vacatur" in (h["audit_notes"] or "")

    def test_confirm_leaves_supported_cancellation(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # The other normal path: the LLM finds an explicit plea / vacatur
        # entry and CONFIRMs. Row stays cancelled.
        self._seed_past_cancelled(store, key="trial-with-plea")
        stub_verify(
            monkeypatch,
            by_key={
                "trial-with-plea": {
                    "type": "CONFIRM",
                    "reason": "Plea agreement filed before trial date; trial vacated by plea.",
                },
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["verified"] == 0
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "cancelled"

    def test_mark_held_flips_cancelled_to_held(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # Rare but valid: the row was wrongly cancelled, and a minute
        # entry / verdict on the docket shows the event actually
        # happened. Bypass REINSTATE → 'scheduled' → MARK_HELD on next
        # sync; do it in one step.
        self._seed_past_cancelled(store, key="trial-actually-held")
        stub_verify(
            monkeypatch,
            by_key={
                "trial-actually-held": {
                    "type": "MARK_HELD",
                    "reason": "verdict form filed; trial demonstrably happened",
                },
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        CaseSyncer(cl, store).sync_case(case)
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "held"

    def test_unclear_leaves_cancelled_row_alone(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # When the LLM can't tell whether the cancellation holds, the
        # conservative move is to leave the row cancelled (vs. blindly
        # un-cancelling on weak signal).
        self._seed_past_cancelled(store, key="ambiguous")
        stub_verify(
            monkeypatch,
            by_key={
                "ambiguous": {"type": "UNCLEAR", "reason": "silent docket"},
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["verified"] == 0
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "cancelled"


class TestVerifyScheduledHearings:
    """Per-hearing confidence pass: for every future scheduled hearing,
    ask the LLM whether recent docket entries support it."""

    def _seed_future_hearing(self, store, key="future-trial"):
        from datetime import datetime, timedelta, timezone

        future_iso = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
        store.upsert_hearing(
            {
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
            }
        )
        return future_iso

    def test_confirm_is_no_op(self, store, case, monkeypatch):
        before = self._seed_future_hearing(store)
        stub_verify(monkeypatch)  # default CONFIRM
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["verified"] == 0
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "scheduled"
        assert h["starts_at_utc"] == before

    def test_malformed_source_entry_ids_json_does_not_crash_verify(
        self,
        store,
        case,
        monkeypatch,
    ):
        # source_entry_ids is stored as JSON; if a row's column is
        # corrupted (manual SQL edit, an aborted migration, etc.) the
        # verify sweep must recover with an empty list rather than
        # crash the whole sync.
        self._seed_future_hearing(store, key="resilient")
        store.conn.execute(
            "UPDATE hearings SET source_entry_ids=? WHERE hearing_key=?",
            ("not-json", "resilient"),
        )
        store.conn.commit()
        stub_verify(monkeypatch)  # default CONFIRM
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        # Should not raise.
        CaseSyncer(cl, store).sync_case(case)
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "scheduled"

    def test_unclear_is_no_op(self, store, case, monkeypatch):
        self._seed_future_hearing(store)
        stub_verify(
            monkeypatch,
            by_key={
                "future-trial": {"type": "UNCLEAR", "reason": "ambiguous"},
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["verified"] == 0
        assert store.get_hearings("us-v-x")[0]["status"] == "scheduled"

    def test_cancel_flips_to_cancelled(self, store, case, monkeypatch):
        self._seed_future_hearing(store)
        stub_verify(
            monkeypatch,
            by_key={
                "future-trial": {
                    "type": "CANCEL_HEARING",
                    "reason": "trial vacated by plea",
                },
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["verified"] == 1
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "cancelled"
        assert "vacated" in (h["audit_notes"] or "")

    def test_delete_hallucination_flips_to_cancelled(
        self,
        store,
        case,
        monkeypatch,
    ):
        # Hallucinated row — LLM says no docket entry supports it. Marked
        # cancelled (preserves audit trail; renderers skip cancelled rows).
        #
        # The source entry MUST be in the store so the verify-pass
        # deterministic guard sees it in the recent_entries context: the
        # rule is "DELETE_HALLUCINATION is only valid when the model has
        # seen the original source entry and concluded it does NOT
        # actually schedule this hearing." Without the source entry
        # present, the guard downgrades to UNCLEAR (see the dedicated
        # test below for that path).
        self._seed_future_hearing(store, key="hallucinated-conf")
        store.mark_entry(
            100,
            42,
            "2026-01-01T00:00:00Z",
            "fp",
            date_filed="2026-01-01",
            description="NOTICE — Set Hearing (text mentions a date but doesn't actually schedule)",
            entry_number=10,
        )
        stub_verify(
            monkeypatch,
            by_key={
                "hallucinated-conf": {
                    "type": "DELETE_HALLUCINATION",
                    "reason": "no docket entry mentions this date",
                },
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["verified"] == 1
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "cancelled"
        assert "no docket entry" in (h["audit_notes"] or "")

    def test_delete_hallucination_downgraded_when_source_entry_not_in_context(
        self,
        store,
        case,
        monkeypatch,
        caplog,
    ):
        # The deterministic guard: when the verify-pass LLM emits
        # DELETE_HALLUCINATION but the source entry isn't in the
        # recent_entries it was shown, the verdict is downgraded to
        # UNCLEAR (no-op). The McGonigal-trial regression — a 2024 jury
        # trial scheduled by a 2023 order outside both context windows,
        # then mooted by a plea without a vacatur entry — was emitted
        # as DELETE_HALLUCINATION at temperature=0 because the rule
        # "you've seen the original source entry" was unsatisfiable.
        # Fix #2 always adds source entries to the context now, but the
        # entries query can return fewer rows than asked for if a source
        # row was deleted from the store or the source_entry_ids list
        # is malformed — that's the case this guard catches.
        import logging

        self._seed_future_hearing(store, key="trial-mcgonigal")
        # NOTE: source_entry_ids on the hearing is [42], but entry 42 is
        # NOT seeded in the store. So _verify_context_entries can't
        # surface it and recent_entries given to the LLM is empty.
        stub_verify(
            monkeypatch,
            by_key={
                "trial-mcgonigal": {
                    "type": "DELETE_HALLUCINATION",
                    "reason": "no scheduling order found in recent entries",
                },
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        with caplog.at_level(logging.WARNING, logger="case_calendar.sync"):
            stats = CaseSyncer(cl, store).sync_case(case)
        # Guard fires -> no row change -> verified count is 0.
        assert stats["verified"] == 0
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "scheduled"  # unchanged
        # And a WARN log line names the rejected verdict + the missing
        # source entry so an operator can investigate the upstream
        # missing-row root cause.
        assert any(
            "rejecting DELETE_HALLUCINATION" in r.message and "[42]" in r.message
            for r in caplog.records
        )

    def test_reschedule_moves_starts_at_utc(self, store, case, monkeypatch):
        self._seed_future_hearing(store)
        stub_verify(
            monkeypatch,
            by_key={
                "future-trial": {
                    "type": "RESCHEDULE_HEARING",
                    "local_date": "2099-01-15",
                    "local_time": "09:00",
                    "reason": "rescheduled per latest order",
                },
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
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
        stub_verify(
            monkeypatch,
            by_key={
                "future-trial": {
                    "type": "MARK_HELD",
                    "reason": "minute entry shows held",
                },
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["verified"] == 1
        assert store.get_hearings("us-v-x")[0]["status"] == "held"

    def test_only_runs_on_future_scheduled(self, store, case, monkeypatch):
        # Past + cancelled + held rows must NOT call verify.
        from datetime import datetime, timedelta, timezone

        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "past-held",
                "title": "Sentencing",
                "status": "held",
                "starts_at_utc": "2024-01-01T00:00:00+00:00",
                "duration_minutes": 90,
                "timezone": "America/New_York",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [1],
            }
        )
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "future-cancelled",
                "title": "Conf",
                "status": "cancelled",
                "starts_at_utc": (
                    datetime.now(timezone.utc) + timedelta(days=30)
                ).isoformat(),
                "duration_minutes": 30,
                "timezone": "America/New_York",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [2],
            }
        )
        called = []

        def fake(*, hearing, **_):
            called.append(hearing.get("hearing_key"))
            return {"type": "CONFIRM"}

        monkeypatch.setattr(llm_mod, "verify_hearing", fake)
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        CaseSyncer(cl, store).sync_case(case)
        assert called == []  # no future-scheduled rows present


class TestDeadlineExtraction:
    """End-to-end deadline flow: ADD_DEADLINE → RESCHEDULE_DEADLINE →
    auto-passed sweep."""

    @pytest.fixture
    def case(self):
        # The deadline path tests assume deadlines are on. Force the override
        # so they don't depend on the docket-number auto-detect (which would
        # otherwise turn off for the "us-v-x" criminal-style fixture below).
        return CaseConfig(
            case_id="us-v-x",
            name="United States v. X",
            dockets=[100],
            calendar="cyber",
        )

    def test_add_deadline_creates_row_at_4pm_court_time(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_DEADLINE",
                        "deadline_key": "govt-response-mtd",
                        "deadline_type": "response",
                        "title": "Govt response to MTD",
                        "local_date": "2026-05-24",
                        "local_time": None,
                        "significance": "major",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case,
            100,
            _entry(1, "ORDER setting briefing schedule: response due by 5/24/2026"),
        )
        rows = store.get_deadlines("us-v-x")
        assert len(rows) == 1
        d = rows[0]
        assert d["deadline_key"] == "govt-response-mtd"
        assert d["status"] == "pending"
        # 16:00 ET (EDT in May — so 4pm EDT = 20:00 UTC) by default.
        assert d["due_at_utc"] == "2026-05-24T20:00:00+00:00"
        assert d["docket_id"] == 100

    def test_add_deadline_with_explicit_time(self, store, case, monkeypatch):
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_DEADLINE",
                        "deadline_key": "joint-status-report",
                        "title": "Joint Status Report",
                        "local_date": "2026-06-01",
                        "local_time": "12:00",
                        "significance": "minor",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case,
            100,
            _entry(1, "ORDER: status report due by noon June 1"),
        )
        d = store.get_deadlines("us-v-x")[0]
        # 12:00 EDT = 16:00 UTC.
        assert d["due_at_utc"] == "2026-06-01T16:00:00+00:00"

    def test_reschedule_deadline_updates_in_place(
        self,
        store,
        case,
        monkeypatch,
    ):
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_DEADLINE",
                        "deadline_key": "reply-mtd",
                        "title": "Reply ISO MTD",
                        "local_date": "2026-05-31",
                        "significance": "major",
                    }
                ],
                2: [
                    {
                        "type": "RESCHEDULE_DEADLINE",
                        "deadline_key": "reply-mtd",
                        "title": "Reply ISO MTD",
                        "local_date": "2026-06-14",
                    }
                ],  # extension granted
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case,
            100,
            _entry(1, "ORDER: reply due by 5/31/2026"),
        )
        syncer.process_entry(
            case,
            100,
            _entry(2, "STIPULATION AND ORDER granting extension to 6/14/2026"),
        )
        rows = store.get_deadlines("us-v-x")
        assert len(rows) == 1
        assert rows[0]["due_at_utc"] == "2026-06-14T20:00:00+00:00"
        assert set(rows[0]["source_entry_ids"]) == {1, 2}

    def test_update_details_on_deadline_coerces_to_reschedule_deadline(
        self,
        store,
        case,
        monkeypatch,
    ):
        # Production failure shape (us-v-ding 2025-07-11): LLM emitted
        # ``UPDATE_DETAILS`` with ``deadline_key`` after an order
        # reiterated an existing deadline. ``UPDATE_DETAILS`` is a
        # hearing-only action, so before the coercion the dispatch
        # routed to ``_apply_action`` which logged "action without
        # hearing_key" and dropped the action. With the coercion the
        # action lands as RESCHEDULE_DEADLINE on the existing row —
        # the time gets updated, the audit trail keeps the entry.
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_DEADLINE",
                        "deadline_key": "govt-status-report",
                        "title": "Government's Status Report",
                        "local_date": "2025-07-11",
                    }
                ],
                2: [
                    {
                        "type": "UPDATE_DETAILS",
                        "deadline_key": "govt-status-report",
                        "title": "Government's Status Report",
                        "local_date": "2025-07-11",
                        "local_time": "09:00",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case,
            100,
            _entry(1, "ORDER: status report due 7/11/2025"),
        )
        syncer.process_entry(
            case,
            100,
            _entry(2, "ORDER: status report due 7/11/2025 at 9:00 AM"),
        )
        rows = store.get_deadlines("us-v-x")
        assert len(rows) == 1
        # The time update landed — proves the coerced RESCHEDULE_DEADLINE
        # reached `_apply_deadline_action` and was processed normally.
        # (Court tz on the fake docket is ET → 9 AM = 13:00 UTC.)
        assert rows[0]["due_at_utc"] == "2025-07-11T13:00:00+00:00"
        # Both source entries on the audit trail.
        assert set(rows[0]["source_entry_ids"]) == {1, 2}

    def test_mark_filed_flips_to_met(self, store, case, monkeypatch):
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_DEADLINE",
                        "deadline_key": "reply-mtd",
                        "title": "Reply ISO MTD",
                        "local_date": "2026-05-31",
                    }
                ],
                2: [{"type": "MARK_FILED", "deadline_key": "reply-mtd"}],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case,
            100,
            _entry(1, "ORDER: reply due by 5/31/2026"),
        )
        # Entry text needs to pass the deadline regex; in practice the
        # verify_deadline end-of-sync pass is the more reliable path for
        # detecting filings since "X filed" notices don't always carry
        # deadline-vocabulary tokens.
        syncer.process_entry(
            case,
            100,
            _entry(2, "REPLY brief filed by Plaintiff (briefing schedule complete)"),
        )
        d = store.get_deadlines("us-v-x")[0]
        assert d["status"] == "met"

    def test_cancel_deadline_on_existing_row_flips_status(
        self,
        store,
        case,
        monkeypatch,
    ):
        # ADD_HEARING then CANCEL_HEARING on the same key — the existing row is merged
        # in place rather than inserted fresh. Covers the `if existing:`
        # branch of the CANCEL_DEADLINE handler.
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_DEADLINE",
                        "deadline_key": "reply-mtd",
                        "title": "Reply ISO MTD",
                        "local_date": "2026-05-31",
                    }
                ],
                2: [
                    {
                        "type": "CANCEL_DEADLINE",
                        "deadline_key": "reply-mtd",
                        "notes": "briefing schedule vacated",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100, _entry(1, "ORDER: reply due by 5/31/2026"))
        syncer.process_entry(case, 100, _entry(2, "ORDER vacating briefing schedule"))
        rows = store.get_deadlines("us-v-x")
        assert len(rows) == 1
        assert rows[0]["status"] == "cancelled"
        assert rows[0]["notes"] == "briefing schedule vacated"
        # Both source entries are preserved in the merged row.
        assert set(rows[0]["source_entry_ids"]) == {1, 2}

    def test_cancel_deadline_unknown_key_no_local_date_drops_with_warning(
        self,
        store,
        case,
        monkeypatch,
        caplog,
    ):
        # CANCEL_DEADLINE on a key we don't recognize AND no `local_date`
        # to anchor a fresh row → drop and log. Without `local_date` we
        # can't even insert an audit row.
        import logging

        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "CANCEL_DEADLINE",
                        "deadline_key": "ghost-key",
                        "notes": "something cancelled",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        with caplog.at_level(logging.WARNING, logger="case_calendar.sync"):
            syncer.process_entry(
                case,
                100,
                _entry(1, "ORDER vacating briefing schedule"),
            )
        assert store.get_deadlines("us-v-x") == []
        assert any(
            "CANCEL_DEADLINE on unknown key with no local_date" in r.message
            for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_mark_filed_unknown_key_logs_and_does_not_insert(
        self,
        store,
        case,
        monkeypatch,
        caplog,
    ):
        # MARK_FILED on a key we never saw an ADD_HEARING for is a benign log —
        # the deadline was filtered out or predates our store. Don't
        # create a fictional "met" row.
        import logging

        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [{"type": "MARK_FILED", "deadline_key": "ghost-key"}],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        with caplog.at_level(logging.INFO, logger="case_calendar.sync"):
            syncer.process_entry(
                case,
                100,
                _entry(1, "RESPONSE brief filed (briefing schedule complete)"),
            )
        assert store.get_deadlines("us-v-x") == []
        assert any("MARK_FILED on unknown key" in r.message for r in caplog.records), [
            r.message for r in caplog.records
        ]

    def test_reschedule_deadline_without_local_date_keeps_existing_date(
        self,
        store,
        case,
        monkeypatch,
    ):
        # RESCHEDULE_DEADLINE without a `local_date` is rare (the model
        # normally only emits a reschedule when it has a new date), but
        # the code path tolerates it: the existing row's due_at_utc
        # rides through unchanged while other fields like notes can
        # still be updated. Covers the fall-through branch where the
        # date-setting `if/elif` chain doesn't fire.
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_DEADLINE",
                        "deadline_key": "response-mtd",
                        "title": "Response ISO MTD",
                        "local_date": "2026-05-31",
                    }
                ],
                2: [
                    {
                        "type": "RESCHEDULE_DEADLINE",
                        "deadline_key": "response-mtd",
                        # No local_date — but updated notes.
                        "notes": "extension administratively docketed",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100, _entry(1, "ORDER: response due by 5/31/2026"))
        syncer.process_entry(case, 100, _entry(2, "STIPULATION AND ORDER on briefing"))
        rows = store.get_deadlines("us-v-x")
        assert len(rows) == 1
        # Date is unchanged from the ADD_HEARING.
        assert rows[0]["due_at_utc"] == "2026-05-31T20:00:00+00:00"
        # Notes did get updated.
        assert rows[0]["notes"] == "extension administratively docketed"

    def test_cancel_deadline_with_unknown_key_inserts_cancelled_row(
        self,
        store,
        case,
        monkeypatch,
    ):
        # The deadline's original setting entry was filtered out (or
        # predates our store), but a vacatur entry arrives — keep an audit
        # row so the timeline survives.
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "CANCEL_DEADLINE",
                        "deadline_key": "joint-report-vacated",
                        "title": "Joint Status Report",
                        "local_date": "2026-04-15",
                        "notes": "schedule replaced wholesale",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case,
            100,
            _entry(1, "ORDER vacating prior briefing schedule"),
        )
        rows = store.get_deadlines("us-v-x")
        assert len(rows) == 1
        assert rows[0]["status"] == "cancelled"

    def test_auto_mark_passed_stale_flips_to_passed(
        self,
        store,
        case,
        monkeypatch,
    ):
        # Past-dated pending deadline gets swept to 'passed' at end of sync.
        store.upsert_deadline(
            {
                "case_id": "us-v-x",
                "deadline_key": "stale-reply",
                "title": "Stale reply",
                "due_at_utc": "2024-01-01T22:00:00+00:00",
                "timezone": "America/New_York",
                "status": "pending",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [99],
            }
        )
        stub_verify(monkeypatch)  # default CONFIRM for any future hearings
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["auto_passed"] == 1
        assert store.get_deadlines("us-v-x")[0]["status"] == "passed"

    def test_deadlines_track_uniformly_on_every_docket_type(
        self,
        store,
        monkeypatch,
    ):
        # Deadline tracking is now uniform — no per-case opt-in, no
        # docket-number auto-detect. Both criminal and civil dockets pass
        # the same shape to llm.extract_actions: ``known_deadlines`` is
        # always a list (empty when nothing's stored yet), and there is
        # no ``extract_deadlines`` parameter at all. Pins the removal of
        # the docket-aware gate that used to split the two paths.
        captured = []

        def fake(*, known_deadlines=None, **kwargs):
            captured.append({"known_deadlines": known_deadlines, "kwargs": kwargs})
            # Loud failure if a caller starts passing the removed flag again.
            assert "extract_deadlines" not in kwargs, (
                "extract_deadlines parameter was removed; callers must not pass it"
            )
            return [{"type": "IGNORE", "reason": "stub"}]

        monkeypatch.setattr(llm_mod, "extract_actions", fake)

        # Criminal docket: the auto-detect used to turn deadlines OFF here.
        case_cr = CaseConfig(
            case_id="us-v-y",
            name="United States v. Y",
            dockets=[100],
            calendar="cyber",
        )
        cl = FakeCourtListener(dockets={100: _docket()})  # "1:25-cr-..."
        CaseSyncer(cl, store).process_entry(
            case_cr,
            100,
            _entry(1, "Trial set for 6/1/2026"),
        )

        # Civil docket: the auto-detect used to turn deadlines ON here.
        case_cv = CaseConfig(
            case_id="acme-v-widget",
            name="Acme v. Widget",
            dockets=[101],
            calendar="tech",
        )
        civil_docket = dict(_docket(), docket_number="1:25-cv-04567-AB")
        cl_cv = FakeCourtListener(dockets={101: civil_docket})
        CaseSyncer(cl_cv, store).process_entry(
            case_cv,
            101,
            _entry(1, "ORDER setting briefing schedule: response due by 5/24/2026"),
        )

        # Both paths reached the LLM with the same shape: known_deadlines
        # is a list (not None), and no extract_deadlines flag is in play.
        assert len(captured) == 2
        for call in captured:
            assert call["known_deadlines"] == []


class TestConditionalDeadline:
    """Deadlines relative to an unknown future event must NOT estimate a
    calendar date. The extractor LLM emits ADD_DEADLINE with
    ``local_date=null`` and ``conditional=true``, and the verbatim court
    text rides on ``notes``. The row persists with ``due_at_utc=NULL`` so
    the renderers skip it, but the summary scaffold still surfaces the
    trigger language. This is the 9th Cir. ``appellants-motion-relief-stay``
    shape from Anthropic v. DoW (docket 73136734 entry 17): "Appellants
    must file a motion for appropriate relief within 21 days after
    resolution of [the related D.C. Cir. case]."
    """

    @pytest.fixture
    def case(self):
        return CaseConfig(
            case_id="us-v-x",
            name="United States v. X",
            dockets=[100],
            calendar="cyber",
        )

    def test_conditional_add_persists_row_with_null_due_at_utc(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        verbatim = (
            "Appellants must file a motion for appropriate relief within "
            "21 days after resolution of related case No. 26-1049."
        )
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_DEADLINE",
                        "deadline_key": "appellants-motion-relief-stay",
                        "title": "Appellants' Motion for Appropriate Relief",
                        "local_date": None,
                        "conditional": True,
                        "notes": verbatim,
                        "significance": "major",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        CaseSyncer(cl, store).process_entry(
            case,
            100,
            # "shall file" + "scheduling order" + "stipulation" so the
            # pre-filter routes this to the LLM.
            _entry(
                1,
                "ORDER on stipulation staying appellate "
                "proceedings; appellants shall file a motion "
                "within 21 days.",
            ),
        )
        rows = store.get_deadlines("us-v-x")
        assert len(rows) == 1
        d = rows[0]
        # Persisted, but no calendar date — the renderers skip null-date rows.
        assert d["due_at_utc"] is None
        assert d["status"] == "pending"
        # Verbatim court language is preserved for the summary LLM.
        assert d["notes"] == verbatim
        assert d["docket_id"] == 100

    def test_non_conditional_dateless_add_is_still_dropped(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # Without conditional=true, a date-less ADD_DEADLINE is the
        # motion-anticipating-a-deadline pattern the LLM should have
        # IGNOREd. Defensive guard remains.
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_DEADLINE",
                        "deadline_key": "ghost",
                        "title": "Ghost",
                        "local_date": None,
                        "significance": "major",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        CaseSyncer(cl, store).process_entry(
            case,
            100,
            _entry(1, "MOTION requesting a briefing schedule and an extension of time"),
        )
        assert store.get_deadlines("us-v-x") == []

    def test_conditional_row_is_skipped_by_deadline_to_hearing_adapter(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # The render-time adapter turns a deadline row into a hearing-
        # shaped dict for ICS / gcal / index. Null due_at_utc → None
        # return → the row never reaches a renderer (and so never lands
        # on a calendar). This guard is what makes "no fake dates"
        # actually true at emit time.
        from case_calendar.cli import _deadline_to_hearing

        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_DEADLINE",
                        "deadline_key": "appellants-motion-relief-stay",
                        "title": "Appellants' Motion for Appropriate Relief",
                        "local_date": None,
                        "conditional": True,
                        "notes": "Within 21 days after resolution of related case.",
                        "significance": "major",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        CaseSyncer(cl, store).process_entry(
            case,
            100,
            _entry(
                1,
                "ORDER on stipulation staying appellate "
                "proceedings; appellants shall file a motion "
                "within 21 days.",
            ),
        )
        row = store.get_deadlines("us-v-x")[0]
        assert _deadline_to_hearing(row) is None

    def test_conditional_then_concrete_reschedule_fills_in_date(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # When the triggering event eventually occurs, a follow-up
        # RESCHEDULE_DEADLINE pins the date. The row remains the same
        # key, gets a real due_at_utc, and rejoins the calendar.
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_DEADLINE",
                        "deadline_key": "appellants-motion-relief-stay",
                        "title": "Appellants' Motion for Appropriate Relief",
                        "local_date": None,
                        "conditional": True,
                        "notes": "Within 21 days after resolution of related case.",
                        "significance": "major",
                    }
                ],
                2: [
                    {
                        "type": "RESCHEDULE_DEADLINE",
                        "deadline_key": "appellants-motion-relief-stay",
                        "title": "Appellants' Motion for Appropriate Relief",
                        "local_date": "2026-08-15",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case,
            100,
            _entry(
                1,
                "ORDER on stipulation staying proceedings; "
                "appellants shall file motion within 21 days.",
            ),
        )
        syncer.process_entry(
            case,
            100,
            _entry(2, "ORDER lifting stay; relief motion due by 8/15/2026."),
        )
        d = store.get_deadlines("us-v-x")[0]
        assert d["due_at_utc"] == "2026-08-15T20:00:00+00:00"


# --- end-of-sync dedupe sweep (same-docket same-slot hearings) ---


def stub_dedupe(monkeypatch, *, action: dict | None = None):
    """Stub llm.resolve_duplicate_hearings.

    Captures the cluster it sees so tests can assert on the prompt
    contents. The default action is KEEP_BOTH (no-op).
    """
    captured: dict = {"cluster": None}

    def fake(*, cluster, **_):
        captured["cluster"] = cluster
        return action or {"type": "KEEP_BOTH", "reason": "stub"}

    monkeypatch.setattr(llm_mod, "resolve_duplicate_hearings", fake)
    return captured


class TestDedupeConcurrentHearings:
    """End-of-sync sweep that resolves same-docket same-slot hearings
    (the Anthropic v. DoW failure mode: a stipulation scheduled a "MSJ
    Hearing" key, the order setting it called it "Motion Hearing", and
    the per-entry extractor allocated two ``hearing_key``s for one
    logical event)."""

    def _seed_concurrent_pair(self, store, when="2099-04-14T15:00:00+00:00"):
        # Target has [42, 43]; duplicate has [43, 99]. After merge, the
        # target's source_entry_ids should be [42, 43, 99] — 43 dedupes
        # against the target's existing copy, 99 gets appended (this
        # exercises the inner-loop add branch in _apply_dedupe_action).
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "msj-hearing",
                "title": "Hearing on Motion for Summary Judgment",
                "starts_at_utc": when,
                "duration_minutes": 60,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [42, 43],
            }
        )
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "motion-hearing-2",
                "title": "Motion Hearing",
                "starts_at_utc": when,
                "duration_minutes": 60,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [43, 99],
            }
        )

    def test_no_clusters_skips_llm_call(self, store, case, monkeypatch):
        # The 99% case: nothing shares (docket, time), so the LLM is
        # never asked. boom-stub verifies this stays free on quiet syncs.
        def boom(*a, **k):
            raise AssertionError("resolve_duplicate_hearings called when no clusters")

        monkeypatch.setattr(llm_mod, "resolve_duplicate_hearings", boom)
        stub_verify(monkeypatch)
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deduped"] == 0

    def test_merge_into_deletes_duplicates_and_combines_sources(
        self,
        store,
        case,
        monkeypatch,
    ):
        self._seed_concurrent_pair(store)
        stub_verify(monkeypatch)
        captured = stub_dedupe(
            monkeypatch,
            action={
                "type": "MERGE_INTO",
                "target_key": "msj-hearing",
                "reason": "Same slot — order called the SJ hearing a Motion Hearing.",
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        # One row got absorbed (deleted).
        assert stats["deduped"] == 1
        # Both hearings were sent to the LLM as one cluster.
        keys_seen = {h["hearing_key"] for h in captured["cluster"]}
        assert keys_seen == {"msj-hearing", "motion-hearing-2"}
        # Target preserved; duplicate DELETED outright (was previously
        # left in the store with status='cancelled' which inflated
        # H_canc deviation in the provider scorer).
        rows = {h["hearing_key"]: h for h in store.get_hearings("us-v-x")}
        assert rows["msj-hearing"]["status"] == "scheduled"
        assert "motion-hearing-2" not in rows
        # source_entry_ids from the duplicate were merged into the target,
        # deduping against the target's existing list.
        assert rows["msj-hearing"]["source_entry_ids"] == [42, 43, 99]
        # The CANONICAL row carries a [dedupe] audit line recording
        # which sibling key(s) were absorbed and the LLM's reason —
        # the audit trail moves to the survivor when the sibling is
        # deleted.
        target_notes = rows["msj-hearing"]["audit_notes"] or ""
        assert "[dedupe]" in target_notes
        assert "motion-hearing-2" in target_notes
        assert "Same slot" in target_notes

    def test_keep_both_leaves_cluster_alone(self, store, case, monkeypatch):
        # Stacked back-to-back proceedings — LLM says they're distinct.
        self._seed_concurrent_pair(store)
        stub_verify(monkeypatch)
        stub_dedupe(
            monkeypatch,
            action={
                "type": "KEEP_BOTH",
                "reason": "Order explicitly schedules both back-to-back",
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deduped"] == 0
        rows = {h["hearing_key"]: h for h in store.get_hearings("us-v-x")}
        assert rows["msj-hearing"]["status"] == "scheduled"
        assert rows["motion-hearing-2"]["status"] == "scheduled"

    def test_unclear_leaves_cluster_alone(self, store, case, monkeypatch):
        # On UNCLEAR (or a non-MERGE/non-KEEP_BOTH type), don't guess.
        self._seed_concurrent_pair(store)
        stub_verify(monkeypatch)
        stub_dedupe(monkeypatch, action={"type": "UNCLEAR", "reason": "ambiguous"})
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deduped"] == 0
        rows = {h["hearing_key"]: h for h in store.get_hearings("us-v-x")}
        assert rows["msj-hearing"]["status"] == "scheduled"
        assert rows["motion-hearing-2"]["status"] == "scheduled"

    def test_merge_into_unknown_target_is_a_noop(self, store, case, monkeypatch):
        # Defensive: the LLM returned a target_key that isn't in the cluster.
        # Don't touch any of the rows — leave the operator to investigate.
        self._seed_concurrent_pair(store)
        stub_verify(monkeypatch)
        stub_dedupe(
            monkeypatch,
            action={
                "type": "MERGE_INTO",
                "target_key": "completely-different-key",
                "reason": "...",
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deduped"] == 0
        rows = {h["hearing_key"]: h for h in store.get_hearings("us-v-x")}
        assert rows["msj-hearing"]["status"] == "scheduled"
        assert rows["motion-hearing-2"]["status"] == "scheduled"

    def test_past_concurrent_hearings_are_not_deduped(
        self,
        store,
        case,
        monkeypatch,
    ):
        # Past slots flip to held by the auto-held sweep — the dedupe
        # pass is for future scheduled rows only. Boom-stub the LLM to
        # prove it isn't consulted.
        self._seed_concurrent_pair(store, when="2020-01-01T00:00:00+00:00")
        stub_verify(monkeypatch)

        def boom(*a, **k):
            raise AssertionError("dedupe LLM called for past hearings")

        monkeypatch.setattr(llm_mod, "resolve_duplicate_hearings", boom)
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deduped"] == 0

    def test_cross_cl_sibling_scheduled_drift_is_clustered(
        self,
        store,
        case,
        monkeypatch,
    ):
        # The Akhter-shape OPEN-case scenario: two CourtListener docket_ids in the
        # same (docket_number, court_id) group each hold a future
        # scheduled hearing at the same UTC slot under different keys.
        # The cluster key is now group-aware, so the existing
        # _dedupe_concurrent_hearings sweep picks them up and the LLM
        # gets called to resolve.
        for did in (100, 101):
            store.upsert_docket_meta(
                did,
                {
                    "court_id": "mad",
                    "docket_number": "1:25-cr-00001-X",
                    "case_name": "United States v. X",
                    "absolute_url": f"/docket/{did}/x/",
                },
            )
        future = "2099-04-14T15:00:00+00:00"
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "trial-x",
                "title": "Jury Trial",
                "starts_at_utc": future,
                "duration_minutes": 480,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [10],
            }
        )
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "trial-x-2",
                "title": "Jury Trial",
                "starts_at_utc": future,
                "duration_minutes": 480,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": 101,  # different CourtListener docket_id, SAME group
                "source_entry_ids": [20],
            }
        )
        stub_verify(monkeypatch)
        captured = stub_dedupe(
            monkeypatch,
            action={
                "type": "MERGE_INTO",
                "target_key": "trial-x",
                "reason": "Cross-CourtListener-sibling drift on the same PACER docket.",
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deduped"] == 1
        # The LLM saw both keys as one cluster despite different docket_ids.
        keys_seen = {h["hearing_key"] for h in captured["cluster"]}
        assert keys_seen == {"trial-x", "trial-x-2"}
        rows = {h["hearing_key"]: h for h in store.get_hearings("us-v-x")}
        assert rows["trial-x"]["status"] == "scheduled"
        # Sibling row is deleted, not cancelled — the row is gone.
        assert "trial-x-2" not in rows
        # Survivor records which sibling key was absorbed.
        assert "trial-x-2" in (rows["trial-x"]["audit_notes"] or "")


class TestDedupeConcurrentHeldHearings:
    """End-of-sync deterministic merge for HELD rows that share the same
    logical PACER slot. A court physically can't hold two hearings
    simultaneously, so same-slot held clusters are unambiguous key-drift
    duplicates — no LLM call needed.

    Motivating case: didenko sentencing-didenko (from prior sync of one
    CourtListener docket) vs sentencing-didenko-2 (from today's sync of a sibling
    CourtListener docket with a different `pacer_case_id`) at the exact same UTC
    slot.
    """

    def _seed_cross_sibling_held_pair(self, store):
        # Two CourtListener docket_ids in the same (docket_number, court_id) group,
        # both with a HELD hearing at the same UTC slot under different
        # keys. The canonical row (selected by source_entry_ids count)
        # is `sentencing-didenko` with [10, 11, 12]; the duplicate has
        # just [99].
        for did in (100, 101):
            store.upsert_docket_meta(
                did,
                {
                    "court_id": "mad",
                    "docket_number": "1:25-cr-00001-X",
                    "case_name": "United States v. X",
                    "absolute_url": f"/docket/{did}/x/",
                },
            )
        slot = "2026-02-19T16:00:00+00:00"
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "sentencing-didenko",
                "title": "Sentencing",
                "starts_at_utc": slot,
                "duration_minutes": 60,
                "timezone": "America/New_York",
                "status": "held",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [10, 11, 12],
            }
        )
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "sentencing-didenko-2",
                "title": "Sentencing",
                "starts_at_utc": slot,
                "duration_minutes": 60,
                "timezone": "America/New_York",
                "status": "held",
                "significance": "major",
                "docket_id": 101,
                "source_entry_ids": [99],
            }
        )

    def test_merges_cross_sibling_held_duplicate_deterministically(
        self,
        store,
        case,
        monkeypatch,
    ):
        self._seed_cross_sibling_held_pair(store)
        stub_verify(monkeypatch)
        # No LLM should be consulted — the merge is deterministic.

        def boom(*a, **k):
            raise AssertionError(
                "resolve_duplicate_hearings called for held cluster — "
                "should be deterministic"
            )

        monkeypatch.setattr(llm_mod, "resolve_duplicate_hearings", boom)
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deduped_held"] == 1
        rows = {h["hearing_key"]: h for h in store.get_hearings("us-v-x")}
        # Canonical (more source_entry_ids) stays held.
        assert rows["sentencing-didenko"]["status"] == "held"
        assert rows["sentencing-didenko"]["source_entry_ids"] == [10, 11, 12, 99]
        # Duplicate is DELETED outright — earlier behavior flipped it
        # to status='cancelled' and kept the row, which inflated H_canc
        # deviation in the provider scorer. The row is now gone.
        assert "sentencing-didenko-2" not in rows
        # The audit trail of WHICH sibling key was absorbed lives on
        # the CANONICAL row's audit_notes instead.
        target_notes = rows["sentencing-didenko"]["audit_notes"] or ""
        assert "[dedupe-held]" in target_notes
        assert "sentencing-didenko-2" in target_notes

    def test_no_held_clusters_skips_dedup(self, store, case, monkeypatch):
        # Quiet case with no same-slot held duplicates — sweep is a no-op.
        stub_verify(monkeypatch)
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats.get("deduped_held", 0) == 0

    def test_merges_dedup_overlapping_source_entry_ids(
        self,
        store,
        case,
        monkeypatch,
    ):
        # When the canonical row and the duplicate share one or more
        # source_entry_ids (common when both CourtListener siblings cite the same
        # PACER minute-entry as the source for their respective held
        # rows), the merge must dedup those ids — emitting [10, 11, 99]
        # instead of [10, 11, 10, 99]. Exercises the "sid already in
        # seen, skip" path inside the per-cluster merge loop.
        # Use the same docket_number / court_id as `_docket()` so the
        # CourtListener re-fetch during sync_case doesn't overwrite our seed under
        # a different group key (which would split the cluster).
        for did in (100, 101):
            store.upsert_docket_meta(
                did,
                {
                    "court_id": "mad",
                    "docket_number": "1:25-cr-00001-X",
                    "case_name": "United States v. X",
                    "absolute_url": f"/docket/{did}/x/",
                },
            )
        slot = "2026-03-04T15:30:00+00:00"
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "sentencing-x",
                "title": "Sentencing",
                "starts_at_utc": slot,
                "duration_minutes": 60,
                "timezone": "America/New_York",
                "status": "held",
                "significance": "major",
                "docket_id": 100,
                # Canonical: more sources, picked as target.
                "source_entry_ids": [10, 11],
            }
        )
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "sentencing-x-2",
                "title": "Sentencing",
                "starts_at_utc": slot,
                "duration_minutes": 60,
                "timezone": "America/New_York",
                "status": "held",
                "significance": "major",
                "docket_id": 101,
                # Duplicate: 10 overlaps with canonical; 99 is new.
                "source_entry_ids": [10, 99],
            }
        )
        stub_verify(monkeypatch)

        def boom(*a, **k):
            raise AssertionError("LLM should not be consulted for held cluster")

        monkeypatch.setattr(llm_mod, "resolve_duplicate_hearings", boom)
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deduped_held"] == 1
        rows = {h["hearing_key"]: h for h in store.get_hearings("us-v-x")}
        # 10 appears once, not twice — the dedup branch fired.
        assert rows["sentencing-x"]["source_entry_ids"] == [10, 11, 99]

    def test_scheduled_rows_at_same_slot_are_not_picked_up(
        self,
        store,
        case,
        monkeypatch,
    ):
        # The held sweep MUST ignore non-held rows (those are handled by
        # the LLM-driven _dedupe_concurrent_hearings).
        for did in (100, 101):
            store.upsert_docket_meta(
                did,
                {
                    "court_id": "mad",
                    "docket_number": "1:25-cr-00001-X",
                    "case_name": "X",
                    "absolute_url": "/x/",
                },
            )
        slot = "2099-04-14T15:00:00+00:00"
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "trial-x",
                "title": "Trial",
                "starts_at_utc": slot,
                "duration_minutes": 480,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [10],
            }
        )
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "trial-x-2",
                "title": "Trial",
                "starts_at_utc": slot,
                "duration_minutes": 480,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": 101,
                "source_entry_ids": [20],
            }
        )
        stub_verify(monkeypatch)
        # The scheduled dedup will call the LLM — stub it to a no-op
        # KEEP_BOTH so we don't accidentally claim the held sweep did
        # the work.
        stub_dedupe(monkeypatch, action={"type": "KEEP_BOTH", "reason": "stub"})
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats.get("deduped_held", 0) == 0


class TestDedupeNearslotHearings:
    """End-of-sync near-slot sweep: the duplicate-held-events the exact-slot
    sweeps miss because the rows sit at NEAR (not identical) slots — same court
    day at different times, or a once-only proceeding at drifted dates (the
    Gemini key-proliferation that rendered sentencing/CIPA/trial-start twice)."""

    def _seed_same_day_pair(self, store):
        # Two held CIPA rows on the same court day, different times.
        for key, slot, src in [
            ("cipa-mcgonigal", "2023-03-08T05:00:00+00:00", [10]),
            ("cipa-mcgonigal-3-6", "2023-03-08T18:00:00+00:00", [11]),
        ]:
            store.upsert_hearing(
                {
                    "case_id": "us-v-x",
                    "hearing_key": key,
                    "title": "CIPA Hearing",
                    "starts_at_utc": slot,
                    "duration_minutes": 60,
                    "timezone": "America/New_York",
                    "status": "held",
                    "significance": "major",
                    "docket_id": 100,
                    "source_entry_ids": src,
                }
            )

    def _seed_singular_crossdate_pair(self, store):
        # Sentencing recorded at its scheduled date (12-18) AND the held date
        # (12-14) under a drifted key — a once-only proceeding, 4 days apart.
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "sentencing-mcgonigal",
                "title": "Sentencing",
                "starts_at_utc": "2023-12-18T05:00:00+00:00",
                "duration_minutes": 90,
                "timezone": "America/New_York",
                "status": "held",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [20],
            }
        )
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "sentencing-mcgonigal-2",
                "title": "Sentencing",
                "starts_at_utc": "2023-12-14T18:30:00+00:00",
                "duration_minutes": 90,
                "timezone": "America/New_York",
                "status": "held",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [21],
            }
        )

    def test_same_day_merge_deletes_dup_and_tags_audit(self, store, case, monkeypatch):
        self._seed_same_day_pair(store)
        stub_verify(monkeypatch)
        captured = stub_dedupe(
            monkeypatch,
            action={
                "type": "MERGE_INTO",
                "target_key": "cipa-mcgonigal",
                "reason": "Same CIPA hearing; date-only + timed copy.",
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deduped_nearslot"] == 1
        assert {h["hearing_key"] for h in captured["cluster"]} == {
            "cipa-mcgonigal",
            "cipa-mcgonigal-3-6",
        }
        rows = {h["hearing_key"]: h for h in store.get_hearings("us-v-x")}
        assert "cipa-mcgonigal-3-6" not in rows
        assert rows["cipa-mcgonigal"]["source_entry_ids"] == [10, 11]
        notes = rows["cipa-mcgonigal"]["audit_notes"] or ""
        assert "[dedupe-nearslot]" in notes and "cipa-mcgonigal-3-6" in notes

    def test_singular_crossdate_merge_keeps_held_date(self, store, case, monkeypatch):
        # The resolver picks the actually-held date (12-14) as the survivor.
        self._seed_singular_crossdate_pair(store)
        stub_verify(monkeypatch)
        stub_dedupe(
            monkeypatch,
            action={
                "type": "MERGE_INTO",
                "target_key": "sentencing-mcgonigal-2",
                "reason": "One sentencing; 12-18 was the scheduled date, held 12-14.",
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deduped_nearslot"] == 1
        rows = {h["hearing_key"]: h for h in store.get_hearings("us-v-x")}
        assert "sentencing-mcgonigal" not in rows
        assert rows["sentencing-mcgonigal-2"]["starts_at_utc"].startswith("2023-12-14")
        assert sorted(rows["sentencing-mcgonigal-2"]["source_entry_ids"]) == [20, 21]

    def test_keep_both_leaves_distinct_same_day_hearings(
        self, store, case, monkeypatch
    ):
        # A court CAN hold two different hearings the same day — KEEP_BOTH.
        self._seed_same_day_pair(store)
        stub_verify(monkeypatch)
        stub_dedupe(
            monkeypatch,
            action={"type": "KEEP_BOTH", "reason": "Morning + afternoon, distinct."},
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deduped_nearslot"] == 0
        keys = {h["hearing_key"] for h in store.get_hearings("us-v-x")}
        assert {"cipa-mcgonigal", "cipa-mcgonigal-3-6"} <= keys

    def test_unclear_leaves_cluster_alone(self, store, case, monkeypatch):
        self._seed_same_day_pair(store)
        stub_verify(monkeypatch)
        stub_dedupe(monkeypatch, action={"type": "UNCLEAR", "reason": "ambiguous"})
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deduped_nearslot"] == 0
        assert len(store.get_hearings("us-v-x")) == 2

    def test_no_nearslot_clusters_skips_llm(self, store, case, monkeypatch):
        # One held sentencing + one held motion hearing on different days:
        # no exact slot, no same-day, no shared singular base -> LLM untouched.
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "sentencing-x",
                "title": "Sentencing",
                "starts_at_utc": "2026-01-05T16:00:00+00:00",
                "duration_minutes": 90,
                "timezone": "America/New_York",
                "status": "held",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [1],
            }
        )
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "motion-hearing-x",
                "title": "Motion Hearing",
                "starts_at_utc": "2026-02-09T16:00:00+00:00",
                "duration_minutes": 60,
                "timezone": "America/New_York",
                "status": "held",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [2],
            }
        )
        stub_verify(monkeypatch)

        def boom(*a, **k):
            raise AssertionError("resolver called with no near-slot cluster")

        monkeypatch.setattr(llm_mod, "resolve_duplicate_hearings", boom)
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deduped_nearslot"] == 0


# --- verify_deadline end-of-case pass (parallel to TestVerifyScheduledHearings) ---


def stub_verify_deadline(monkeypatch, *, by_key: dict[str, dict] | None = None):
    by_key = by_key or {}

    def fake(*, deadline, **_):
        return by_key.get(
            deadline.get("deadline_key"),
            {"type": "CONFIRM", "reason": "stub"},
        )

    monkeypatch.setattr(llm_mod, "verify_deadline", fake)


class TestVerifyPendingDeadlines:
    @pytest.fixture
    def case(self):
        return CaseConfig(
            case_id="us-v-x",
            name="United States v. X",
            dockets=[100],
            calendar="cyber",
        )

    def _seed_future_deadline(self, store, key="reply-mtd"):
        from datetime import datetime, timedelta, timezone

        future_iso = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
        store.upsert_deadline(
            {
                "case_id": "us-v-x",
                "deadline_key": key,
                "title": "Reply ISO MTD",
                "due_at_utc": future_iso,
                "timezone": "America/New_York",
                "notes": None,
                "status": "pending",
                "significance": "major",
                "deadline_type": "reply",
                "docket_id": 100,
                "source_entry_ids": [99],
            }
        )
        return future_iso

    def test_confirm_is_no_op(self, store, case, monkeypatch):
        before = self._seed_future_deadline(store)
        stub_verify(monkeypatch)
        stub_verify_deadline(monkeypatch)  # default CONFIRM
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats.get("deadlines_verified", 0) == 0
        d = store.get_deadlines("us-v-x")[0]
        assert d["status"] == "pending"
        assert d["due_at_utc"] == before

    def test_malformed_source_entry_ids_json_does_not_crash_verify(
        self,
        store,
        case,
        monkeypatch,
    ):
        # Same recovery as the hearings verify sweep: a deadline row
        # with corrupted source_entry_ids JSON must fall back to an
        # empty list rather than crash the sync.
        self._seed_future_deadline(store, key="resilient")
        store.conn.execute(
            "UPDATE deadlines SET source_entry_ids=? WHERE deadline_key=?",
            ("not-json", "resilient"),
        )
        store.conn.commit()
        stub_verify(monkeypatch)
        stub_verify_deadline(monkeypatch)  # default CONFIRM
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        # Should not raise.
        CaseSyncer(cl, store).sync_case(case)
        d = store.get_deadlines("us-v-x")[0]
        assert d["status"] == "pending"

    def test_skips_deadline_with_no_docket_id(
        self,
        store,
        case,
        monkeypatch,
    ):
        # Defensive: deadlines without a docket_id can't be verified —
        # the sweep needs the docket's court to resolve timezones — so
        # the row is silently skipped instead of crashing the sweep.
        from datetime import datetime, timedelta, timezone

        future_iso = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
        store.upsert_deadline(
            {
                "case_id": "us-v-x",
                "deadline_key": "orphan",
                "title": "Orphan deadline",
                "due_at_utc": future_iso,
                "timezone": "America/New_York",
                "status": "pending",
                "significance": "major",
                "docket_id": None,  # the missing-docket case
                "source_entry_ids": [99],
            }
        )
        stub_verify(monkeypatch)
        stub_verify_deadline(monkeypatch)
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        # Sweep runs without crashing.
        CaseSyncer(cl, store).sync_case(case)
        d = next(
            d for d in store.get_deadlines("us-v-x") if d["deadline_key"] == "orphan"
        )
        assert d["status"] == "pending"

    def test_cancel_flips_to_cancelled(self, store, case, monkeypatch):
        self._seed_future_deadline(store)
        stub_verify(monkeypatch)
        stub_verify_deadline(
            monkeypatch,
            by_key={
                "reply-mtd": {"type": "CANCEL_HEARING", "reason": "case dismissed"},
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deadlines_verified"] == 1
        d = store.get_deadlines("us-v-x")[0]
        assert d["status"] == "cancelled"
        assert "dismissed" in (d["audit_notes"] or "")

    def test_delete_hallucination_flips_to_cancelled(
        self,
        store,
        case,
        monkeypatch,
    ):
        # Source entry seeded — guard sees it in the verify-pass context
        # so DELETE_HALLUCINATION is applied. See the matching hearing
        # test for the guard's rationale; the deadline equivalent has
        # the same shape.
        self._seed_future_deadline(store)
        store.mark_entry(
            100,
            99,
            "2026-01-01T00:00:00Z",
            "fp",
            date_filed="2026-01-01",
            description="ambiguous entry the extractor misread as setting a deadline",
            entry_number=15,
        )
        stub_verify(monkeypatch)
        stub_verify_deadline(
            monkeypatch,
            by_key={
                "reply-mtd": {
                    "type": "DELETE_HALLUCINATION",
                    "reason": "no scheduling order found",
                },
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deadlines_verified"] == 1
        d = store.get_deadlines("us-v-x")[0]
        assert d["status"] == "cancelled"
        assert "no scheduling order" in (d["audit_notes"] or "")

    def test_delete_hallucination_downgraded_when_source_entry_not_in_context(
        self,
        store,
        case,
        monkeypatch,
        caplog,
    ):
        # Deadline mirror of the hearing-side guard test: when the
        # source entry isn't in the context the model saw,
        # DELETE_HALLUCINATION is downgraded to UNCLEAR no-op.
        import logging

        self._seed_future_deadline(store)
        # source_entry_ids=[99] on the deadline, but entry 99 not in store.
        stub_verify(monkeypatch)
        stub_verify_deadline(
            monkeypatch,
            by_key={
                "reply-mtd": {
                    "type": "DELETE_HALLUCINATION",
                    "reason": "no scheduling order in context",
                },
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        with caplog.at_level(logging.WARNING, logger="case_calendar.sync"):
            stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deadlines_verified"] == 0
        d = store.get_deadlines("us-v-x")[0]
        assert d["status"] == "pending"  # unchanged
        assert any(
            "rejecting DELETE_HALLUCINATION" in r.message and "[99]" in r.message
            for r in caplog.records
        )

    def test_mark_filed_flips_to_met(self, store, case, monkeypatch):
        self._seed_future_deadline(store)
        stub_verify(monkeypatch)
        stub_verify_deadline(
            monkeypatch,
            by_key={
                "reply-mtd": {"type": "MARK_FILED", "reason": "reply on docket"},
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deadlines_verified"] == 1
        assert store.get_deadlines("us-v-x")[0]["status"] == "met"

    def test_reschedule_moves_due_at_utc(self, store, case, monkeypatch):
        self._seed_future_deadline(store)
        stub_verify(monkeypatch)
        stub_verify_deadline(
            monkeypatch,
            by_key={
                "reply-mtd": {
                    "type": "RESCHEDULE_HEARING",
                    "local_date": "2099-01-15",
                    "reason": "extension granted",
                },
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deadlines_verified"] == 1
        d = store.get_deadlines("us-v-x")[0]
        # 4pm ET default for the deadline = 21:00 UTC (Jan 15 is EST, not EDT).
        assert d["due_at_utc"] == "2099-01-15T21:00:00+00:00"

    def test_reschedule_without_local_date_is_dropped(
        self,
        store,
        case,
        monkeypatch,
    ):
        before = self._seed_future_deadline(store)
        stub_verify(monkeypatch)
        stub_verify_deadline(
            monkeypatch,
            by_key={
                "reply-mtd": {
                    "type": "RESCHEDULE_HEARING",
                    "reason": "no date provided",
                },
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        # No change, no count.
        assert stats["deadlines_verified"] == 0
        assert store.get_deadlines("us-v-x")[0]["due_at_utc"] == before

    def test_unknown_action_type_is_dropped(self, store, case, monkeypatch):
        before = self._seed_future_deadline(store)
        stub_verify(monkeypatch)
        stub_verify_deadline(
            monkeypatch,
            by_key={
                "reply-mtd": {"type": "BOGUS", "reason": "model made it up"},
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deadlines_verified"] == 0
        assert store.get_deadlines("us-v-x")[0]["due_at_utc"] == before


class TestVerifyEdgeCases:
    """Hearing-verify branches that aren't covered by the happy-path tests
    in TestVerifyScheduledHearings."""

    def _seed_future_hearing(self, store, key="future-trial"):
        from datetime import datetime, timedelta, timezone

        future_iso = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
        store.upsert_hearing(
            {
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
            }
        )
        return future_iso

    def test_reschedule_without_local_date_is_dropped(
        self,
        store,
        case,
        monkeypatch,
    ):
        before = self._seed_future_hearing(store)
        stub_verify(
            monkeypatch,
            by_key={
                "future-trial": {"type": "RESCHEDULE_HEARING", "reason": "no date"},
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        # The action is dropped — counter stays 0 and starts_at_utc unchanged.
        assert stats["verified"] == 0
        assert store.get_hearings("us-v-x")[0]["starts_at_utc"] == before

    def test_unknown_action_type_is_dropped(self, store, case, monkeypatch):
        before = self._seed_future_hearing(store)
        stub_verify(
            monkeypatch,
            by_key={
                "future-trial": {"type": "MYSTERY", "reason": "?"},
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["verified"] == 0
        assert store.get_hearings("us-v-x")[0]["starts_at_utc"] == before


class TestEnsureCourtErrorPath:
    def test_court_fetch_failure_logged_and_swallowed(
        self,
        store,
        case,
        monkeypatch,
    ):
        # The court fetch can fail (CourtListener outage, unknown court id). When it
        # does we log a warning but continue — the citation stays missing
        # rather than crashing the whole sync.
        class _RaisingCourtListener(FakeCourtListener):
            def get_court(self, court_id):
                raise RuntimeError("CourtListener down")

        cl = _RaisingCourtListener(dockets={100: _docket()})
        make_llm_stub(monkeypatch, by_entry={})  # no actions emitted
        stub_verify(monkeypatch)

        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100, _entry(1, "ORDER on irrelevant motion"))
        # No exception escaped; the citation is unset.
        assert store.get_court_citation("mad") is None


class TestApplyHearingActionEdgeCases:
    """Coverage for the CANCEL_HEARING / MARK_HELD with-no-local_date drop paths
    and the deadline-action error paths."""

    def test_cancel_on_unknown_key_without_local_date_drops(
        self,
        store,
        case,
        monkeypatch,
    ):
        # CANCEL_HEARING targeting a hearing_key the store doesn't have AND no
        # local_date to seed a new row → action is dropped with a warning.
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [{"type": "CANCEL_HEARING", "hearing_key": "never-seen"}],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100, _entry(1, "ORDER vacating prior"))
        assert store.get_hearings("us-v-x") == []

    def test_mark_held_on_unknown_key_without_local_date_drops(
        self,
        store,
        case,
        monkeypatch,
    ):
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [{"type": "MARK_HELD", "hearing_key": "never-seen"}],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100, _entry(1, "MINUTE ORDER held"))
        assert store.get_hearings("us-v-x") == []


class TestApplyDeadlineActionEdgeCases:
    @pytest.fixture
    def case(self):
        return CaseConfig(
            case_id="us-v-x",
            name="United States v. X",
            dockets=[100],
            calendar="cyber",
        )

    def test_action_without_deadline_key_is_dropped(
        self,
        store,
        case,
        monkeypatch,
    ):
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [{"type": "ADD_DEADLINE", "local_date": "2026-05-24"}],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100, _entry(1, "ORDER: response due"))
        assert store.get_deadlines("us-v-x") == []

    def test_add_deadline_without_local_date_is_dropped(
        self,
        store,
        case,
        monkeypatch,
    ):
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {"type": "ADD_DEADLINE", "deadline_key": "reply", "title": "Reply"}
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100, _entry(1, "ORDER: reply due TBD"))
        assert store.get_deadlines("us-v-x") == []

    def test_cancel_deadline_on_unknown_key_without_local_date_drops(
        self,
        store,
        case,
        monkeypatch,
    ):
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [{"type": "CANCEL_DEADLINE", "deadline_key": "never-seen"}],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100, _entry(1, "ORDER vacating schedule"))
        assert store.get_deadlines("us-v-x") == []

    def test_mark_filed_on_unknown_key_is_logged_and_dropped(
        self,
        store,
        case,
        monkeypatch,
    ):
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [{"type": "MARK_FILED", "deadline_key": "never-seen"}],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100, _entry(1, "REPLY brief filed"))
        assert store.get_deadlines("us-v-x") == []


class TestSummaryStaleMarkOnPrimaryOrDisposition:
    """A primary-document or disposition entry must flip the docket's
    case_summaries.stale flag — that's how the automatic summary refresh knows
    a regeneration is needed before the next emit."""

    def test_primary_document_marks_stale(self, store, case, monkeypatch):
        # Seed a non-stale summary row, then process an entry whose
        # description matches summary.is_primary_document. After
        # process_entry, the row should be flagged stale on the LOGICAL
        # PACER docket key (docket_number, court_id), not on the CourtListener
        # docket_id — see the docket grouping design decision in AGENTS.md.
        d = _docket()
        store.upsert_docket_meta(
            100,
            {
                "court_id": d["court_id"],
                "docket_number": d["docket_number"],
                "case_name": d["case_name"],
                "absolute_url": d["absolute_url"],
            },
        )
        group = (d["docket_number"], d["court_id"])
        store.upsert_case_summary(
            "us-v-x",
            *group,
            summary="old",
            model="m",
            source_entry_ids=[],
        )
        assert store.is_summary_stale("us-v-x", *group) is False

        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [{"type": "IGNORE", "reason": "stub"}],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        # "INDICTMENT" head matches summary.is_primary_document.
        syncer.process_entry(case, 100, _entry(1, "INDICTMENT as to defendant"))
        assert store.is_summary_stale("us-v-x", *group) is True

    def test_disposition_marks_stale(self, store, case, monkeypatch):
        d = _docket()
        store.upsert_docket_meta(
            100,
            {
                "court_id": d["court_id"],
                "docket_number": d["docket_number"],
                "case_name": d["case_name"],
                "absolute_url": d["absolute_url"],
            },
        )
        group = (d["docket_number"], d["court_id"])
        store.upsert_case_summary(
            "us-v-x",
            *group,
            summary="old",
            model="m",
            source_entry_ids=[],
        )
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [{"type": "IGNORE", "reason": "stub"}],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100, _entry(1, "JUDGMENT in a Criminal Case"))
        assert store.is_summary_stale("us-v-x", *group) is True

    def test_primary_document_persists_description_and_recap_docs(
        self,
        store,
        case,
        monkeypatch,
    ):
        # Primary documents don't match the hearing-relevance regex, so
        # historically their body was discarded — leaving the summary
        # pipeline to re-fetch the same data from CourtListener. Now sync persists the
        # description AND the compact recap_documents (including plain_text)
        # so summary can read locally. Without this, refresh_stale on a
        # freshly synced docket would burn a duplicate /docket-entries/
        # round-trip.
        make_llm_stub(monkeypatch, by_entry={1: []})
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        entry = _entry(1, "INDICTMENT as to defendant")
        entry["recap_documents"] = [
            {
                "id": 500,
                "is_available": True,
                "plain_text": "indictment body",
            }
        ]
        syncer.process_entry(case, 100, entry)

        cached = store.get_entries_with_body(100)
        assert [e["id"] for e in cached] == [1]
        assert cached[0]["description"] == "INDICTMENT as to defendant"
        # plain_text round-trips so pdf.extract_text can short-circuit.
        assert cached[0]["recap_documents"][0]["plain_text"] == "indictment body"

    def test_disposition_persists_description_for_summary(
        self,
        store,
        case,
        monkeypatch,
    ):
        # Paperless minute-entry disposition: no recap_documents at all.
        # The description still has to land so the summary pipeline can
        # use the new description-fallback path.
        make_llm_stub(monkeypatch, by_entry={1: []})
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case,
            100,
            _entry(
                1,
                "Electronic Clerk's Notes: Sentencing held. Court imposes "
                "sentence: 92 months imprisonment.",
            ),
        )
        cached = store.get_entries_with_body(100)
        assert len(cached) == 1
        assert "92 months imprisonment" in cached[0]["description"]

    def test_filter_failed_entry_still_a_stub(
        self,
        store,
        case,
        monkeypatch,
    ):
        # Notices, briefs, and attorney appearances that match neither the
        # hearing/deadline filter nor op/disp must continue to land as
        # fingerprint stubs — storing their body is dead weight.
        make_llm_stub(monkeypatch, by_entry={1: []})
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100, _entry(1, "NOTICE of attorney appearance"))
        # No body-bearing entries on the docket: stub still works for dedup
        # but doesn't show up in summary's local-cache lookup.
        assert store.get_entries_with_body(100) == []


class TestSummaryStaleOnPostureChange:
    """End-to-end: an end-of-sync sweep that changes a hearing's posture
    (the verify pass flipping a scheduled hearing to 'held') must flag the
    docket's summary stale so the next emit regenerates the prose — even
    though no primary-document / disposition ENTRY landed this sync.

    The canonical regression is anthropic-v-dow 26-1049 (D.C. Cir.): the
    oral argument was correctly marked 'held' by the verify pass, but the
    summary stayed frozen at "oral argument is scheduled for May 19" because
    the post-argument docket entries (an "ORAL ARGUMENT HELD" notice and
    supplemental-briefing per-curiam orders) matched neither
    is_primary_document nor is_disposition, so the document-only stale
    trigger never fired.
    """

    def _seed(self, store):
        from datetime import datetime, timedelta, timezone

        d = _docket()
        store.upsert_docket_meta(
            100,
            {
                "court_id": d["court_id"],
                "docket_number": d["docket_number"],
                "case_name": d["case_name"],
                "absolute_url": d["absolute_url"],
            },
        )
        future_iso = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "oral-arg",
                "title": "Oral argument",
                "starts_at_utc": future_iso,
                "duration_minutes": 30,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [42],
            }
        )
        group = (d["docket_number"], d["court_id"])
        # Reset stale=0 AFTER seeding the hearing so the precondition holds
        # regardless of the seed's own flip.
        store.upsert_case_summary(
            "us-v-x", *group, summary="oral argument is scheduled", model="m"
        )
        assert store.is_summary_stale("us-v-x", *group) is False
        return group

    def test_verify_mark_held_flags_summary_stale(self, store, case, monkeypatch):
        group = self._seed(store)
        stub_verify(
            monkeypatch,
            by_key={
                "oral-arg": {"type": "MARK_HELD", "reason": "minute entry shows held"},
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["verified"] == 1
        assert store.get_hearings("us-v-x")[0]["status"] == "held"
        # The fix: the held-flip flags the summary stale even though no
        # primary-document / disposition entry landed this sync.
        assert store.is_summary_stale("us-v-x", *group) is True


class TestVerifyContextEntries:
    """The verify pass must SEE a past hearing's outcome evidence — a minute
    entry / verdict / judgment filed around the hearing's own date — even on a
    docket that kept moving afterward, where that entry falls outside the 15
    most-recent. Without it the verify LLM can't cite the record it needs to
    MARK_HELD and the row wrongly stays 'scheduled' (the mcgonigal-sentencing
    miss surfaced in the provider-accuracy adjudication)."""

    def _seed_recent(self, store, docket_id=100, n=16):
        # n recent hearing-relevant entries that crowd older rows out of the
        # most-recent-15 window (date_modified in 2026).
        for i in range(n):
            store.mark_entry(
                docket_id,
                500 + i,
                f"2026-02-{i + 1:02d}T00:00:00Z",
                "fp",
                date_filed=f"2026-02-{i + 1:02d}",
                description=f"recent status report {i}",
                entry_number=500 + i,
            )

    def _seed_judgment(self, store, docket_id=100):
        # The judgment proving the hearing happened, filed near the 2024
        # hearing date but with an OLD date_modified -> not in recent-15.
        store.mark_entry(
            docket_id,
            90,
            "2024-06-18T00:00:00Z",
            "fp",
            date_filed="2024-06-18",
            description="JUDGMENT IN A CRIMINAL CASE: 50 months imprisonment",
            entry_number=90,
        )

    def test_near_date_evidence_surfaces_outside_recent_window(self, store: Store):
        self._seed_recent(store)
        self._seed_judgment(store)
        syncer = CaseSyncer(FakeCourtListener(), store)
        ctx = syncer._verify_context_entries(100, "2024-06-15T21:30:00+00:00")
        ids = [e["entry_id"] for e in ctx]
        assert 90 in ids  # outcome evidence surfaced via the near-date window
        assert any(i >= 500 for i in ids)  # recent context still present
        assert len(ids) == len(set(ids))  # de-duplicated by entry id

    def test_future_hearing_pulls_only_recent(self, store: Store):
        self._seed_recent(store, n=3)
        syncer = CaseSyncer(FakeCourtListener(), store)
        ctx = syncer._verify_context_entries(100, "2027-01-01T00:00:00+00:00")
        # Future window is empty -> recent set only, no error.
        assert sorted(e["entry_id"] for e in ctx) == [500, 501, 502]

    def test_recent_and_near_sets_overlap_deduplicated(self, store: Store):
        # Hearing date inside the recent window: each entry is in BOTH the
        # recent set and the near-date set, and must appear exactly once.
        self._seed_recent(store, n=3)  # entries 500-502, filed Feb 2026
        syncer = CaseSyncer(FakeCourtListener(), store)
        ctx = syncer._verify_context_entries(100, "2026-02-02T12:00:00+00:00")
        ids = [e["entry_id"] for e in ctx]
        assert sorted(ids) == [500, 501, 502]
        assert len(ids) == len(set(ids))  # the overlap was de-duplicated

    def test_unparseable_or_missing_timestamp_falls_back_to_recent(self, store: Store):
        self._seed_recent(store, n=2)
        syncer = CaseSyncer(FakeCourtListener(), store)
        for ts in ("not-a-timestamp", None):
            ctx = syncer._verify_context_entries(100, ts)
            assert sorted(e["entry_id"] for e in ctx) == [500, 501]

    def test_source_entries_surfaced_when_outside_recent_and_near_windows(
        self, store: Store
    ):
        # The McGonigal regression shape: a 2024 jury trial was scheduled
        # by an order filed in 2023. The verify pass runs in 2026 on a
        # docket that kept moving — so the scheduling order's
        # date_modified is far older than the most-recent-15 cutoff AND
        # its date_filed is far older than the around-hearing-date window
        # (45 days). Without source-entry surfacing the model can't see
        # the order that scheduled the trial, the DELETE_HALLUCINATION
        # rule ("you've seen the original source entry and concluded it
        # does NOT actually schedule this hearing") is unsatisfiable, and
        # at temperature=0 the model emits DELETE_HALLUCINATION anyway
        # rather than UNCLEAR. Source-entry surfacing makes the rule
        # satisfiable.
        self._seed_recent(store)  # 16 recent entries crowd out older rows
        store.mark_entry(
            100,
            42,
            "2023-08-01T00:00:00Z",  # old date_modified
            "fp",
            date_filed="2023-08-01",  # outside the 45-day around-hearing window for 2024-06-15
            description="ORDER: TRIAL SET FOR JUNE 12, 2024",
            entry_number=23,
        )
        syncer = CaseSyncer(FakeCourtListener(), store)
        ctx = syncer._verify_context_entries(
            100, "2024-06-15T21:30:00+00:00", source_entry_ids=[42]
        )
        ids = [e["entry_id"] for e in ctx]
        assert 42 in ids, "source entry must be in verify-pass context"

    def test_source_entries_deduplicated_with_recent_and_near(self, store: Store):
        # Source entry overlap with recent/near sets must produce exactly
        # one row per entry_id in the context. (Otherwise the LLM sees
        # the same docket entry twice and may give it double weight.)
        self._seed_recent(store, n=3)  # entries 500, 501, 502
        syncer = CaseSyncer(FakeCourtListener(), store)
        # Pass entry 501 as a source entry — it's already in the recent
        # set, so the merge must collapse it to one row.
        ctx = syncer._verify_context_entries(
            100, "2026-02-02T12:00:00+00:00", source_entry_ids=[501]
        )
        ids = [e["entry_id"] for e in ctx]
        assert sorted(ids) == [500, 501, 502]
        assert len(ids) == len(set(ids))

    def test_source_entries_none_or_empty_works(self, store: Store):
        # Both ``None`` and ``[]`` are valid "no source entries to
        # include" inputs — the helper must not raise on either.
        self._seed_recent(store, n=2)
        syncer = CaseSyncer(FakeCourtListener(), store)
        for sids in (None, []):
            ctx = syncer._verify_context_entries(100, None, source_entry_ids=sids)
            assert sorted(e["entry_id"] for e in ctx) == [500, 501]

    def test_source_entries_from_different_docket_not_surfaced(self, store: Store):
        # The verify pass is per-docket; if a hearing's source_entry_ids
        # accidentally include an entry id from a different docket (data
        # corruption shape), Store.get_entries_by_ids filters it out
        # before it can leak across.
        self._seed_recent(store, n=1)  # entry 500 on docket 100
        store.mark_entry(
            200,
            600,
            "2026-01-01T00:00:00Z",
            "fp",
            description="other docket",
            entry_number=1,
        )
        syncer = CaseSyncer(FakeCourtListener(), store)
        ctx = syncer._verify_context_entries(100, None, source_entry_ids=[600])
        ids = [e["entry_id"] for e in ctx]
        assert 600 not in ids
        assert sorted(ids) == [500]

    @staticmethod
    def _seed_past_sentencing(store):
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "sentencing-x",
                "title": "Sentencing",
                "starts_at_utc": "2024-06-15T21:30:00+00:00",
                "duration_minutes": 30,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [],
            }
        )

    @staticmethod
    def _verify_on_judgment_visibility(monkeypatch):
        # MARK_HELD only when a JUDGMENT entry is actually in the context the
        # verify pass hands the LLM — so the test asserts the wiring, not a
        # canned verdict.
        def fake(*, hearing, recent_entries, **_):
            if any("JUDGMENT" in (e.get("description") or "") for e in recent_entries):
                return {"type": "MARK_HELD", "reason": "judgment entered"}
            return {"type": "UNCLEAR", "reason": "no outcome evidence visible"}

        monkeypatch.setattr(llm_mod, "verify_hearing", fake)

    def test_verify_marks_held_using_near_date_evidence(
        self, store: Store, case, monkeypatch
    ):
        self._seed_recent(store)
        self._seed_judgment(store)
        self._seed_past_sentencing(store)
        self._verify_on_judgment_visibility(monkeypatch)
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        CaseSyncer(cl, store).sync_case(case)
        assert store.get_hearings("us-v-x")[0]["status"] == "held"

    def test_without_near_evidence_stays_scheduled(
        self, store: Store, case, monkeypatch
    ):
        # Same stub, no judgment anywhere: the recent set has no JUDGMENT and
        # the near-date window is empty -> UNCLEAR -> row stays scheduled.
        # Proves the near-date evidence (not the recent set) flips the prior test.
        self._seed_recent(store)
        self._seed_past_sentencing(store)
        self._verify_on_judgment_visibility(monkeypatch)
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        CaseSyncer(cl, store).sync_case(case)
        assert store.get_hearings("us-v-x")[0]["status"] == "scheduled"


class TestDeleteHallucinationGuard:
    """Unit tests for ``CaseSyncer._delete_hallucination_allowed`` —
    the deterministic guard that downgrades DELETE_HALLUCINATION to
    UNCLEAR when the model couldn't have seen the source entry. The
    integration tests above exercise the guard through the full
    verify-pass; these tests cover the helper's edge cases directly
    so the branch behavior is pinned independent of test scaffolding.
    """

    def test_empty_source_entry_ids_vacuously_allowed(self):
        # A row with no source entries trivially satisfies the rule
        # ("you've seen every source entry") — there are no source
        # entries to have missed. Whether the row should EXIST without
        # source entries is a separate concern (suspicious data, but
        # not this guard's job).
        assert CaseSyncer._delete_hallucination_allowed(
            "hearing", "us-v-x", "trial-x", [], []
        )

    def test_all_source_entries_shown_allowed(self):
        # Happy path: every source entry is in the recent_entries the
        # model saw, so the model's "you've seen the source entry"
        # rule is satisfiable and its DELETE_HALLUCINATION verdict
        # stands.
        recent = [{"entry_id": 42}, {"entry_id": 43}, {"entry_id": 99}]
        assert CaseSyncer._delete_hallucination_allowed(
            "hearing", "us-v-x", "trial-x", [42, 43], recent
        )

    def test_any_missing_source_entry_rejects(self, caplog):
        # One source entry missing -> rejected, WARN-logged. The
        # warning names BOTH the missing entry ids (so the operator
        # knows what to investigate) and the row key (so the operator
        # knows where to look).
        import logging

        recent = [{"entry_id": 42}]
        with caplog.at_level(logging.WARNING, logger="case_calendar.sync"):
            ok = CaseSyncer._delete_hallucination_allowed(
                "deadline", "us-v-x", "reply-mtd", [42, 99], recent
            )
        assert ok is False
        assert any(
            "rejecting DELETE_HALLUCINATION on deadline" in r.message
            and "[99]" in r.message
            and "reply-mtd" in r.message
            for r in caplog.records
        )


# --- proceeding-record notes selection (anthropic-v-dow pi-motion-hearing) ---


class TestProceedingRecordPredicates:
    """Unit coverage for the helpers that distinguish the RECORD of a held
    proceeding (minute entry / transcript / clerk's notes) from a pre-hearing
    administrative notice (clerk's notice of Zoom access / courtroom change)."""

    def test_describes_proceeding(self):
        assert _describes_proceeding("Minute Entry for proceedings held before Judge")
        assert _describes_proceeding("Minute Order for proceedings held before Judge")
        assert _describes_proceeding("MINUTES OF Status Conference held before Judge")
        assert _describes_proceeding("Parties stated appearances; under submission")
        assert _describes_proceeding("Transcript of Proceedings held on 03/24/2026")
        # Transcript with words between "of" and "proceedings".
        assert _describes_proceeding(
            "Transcript of Remote Zoom Video Conference Proceedings held on March 10"
        )
        assert not _describes_proceeding(
            "Clerk's notice providing Zoom access information"
        )
        # A bare transcript ORDER is a private purchase request, not a record.
        assert not _describes_proceeding("ORDER for Transcript")
        # The standard clerk's-notice Zoom boilerplate says "proceedings held
        # BY telephone or videoconference" — a scheduling notice, NOT a record.
        # Requiring "held before/on" keeps it out (the status-conference-1
        # false positive surfaced by the heal dry-run).
        assert not _describes_proceeding(
            "CLERK'S NOTICE SETTING STATUS CONFERENCE HEARING. This proceeding "
            "will be held on a date. Persons granted access to court proceedings "
            "held by telephone or videoconference are reminded that recording is "
            "prohibited."
        )
        assert not _describes_proceeding(None)
        assert not _describes_proceeding("")

    def test_proceeding_record_rank(self):
        # Substantive accounts rank above transcript filings.
        assert (
            _proceeding_record_rank("Minute Entry for proceedings held before X") == 0
        )
        assert (
            _proceeding_record_rank("Parties stated appearances; under submission") == 0
        )
        assert (
            _proceeding_record_rank("Transcript of Proceedings held on 03/24/2026") == 1
        )
        assert _proceeding_record_rank(None) == 0

    def test_hearing_date_tokens(self):
        # 20:30 UTC on 3/24 is 4:30 PM EDT — same court-local date.
        toks = _hearing_date_tokens(
            {
                "starts_at_utc": "2026-03-24T20:30:00+00:00",
                "timezone": "America/New_York",
            }
        )
        assert "3/24/2026" in toks
        assert "03/24/2026" in toks
        assert "3/24/26" in toks
        assert "March 24, 2026" in toks
        # No start -> no tokens (the date filter then doesn't apply).
        assert _hearing_date_tokens({"starts_at_utc": None}) == []
        # Unparseable start -> no tokens.
        assert _hearing_date_tokens({"starts_at_utc": "not-a-date"}) == []
        # Missing timezone falls back to the UTC date.
        assert "3/24/2026" in _hearing_date_tokens(
            {"starts_at_utc": "2026-03-24T12:00:00+00:00"}
        )
        # Unrecognized timezone falls back to the UTC date rather than raising.
        assert "3/24/2026" in _hearing_date_tokens(
            {"starts_at_utc": "2026-03-24T12:00:00+00:00", "timezone": "Not/AZone"}
        )

    def test_proceeding_types(self):
        assert _proceeding_types("Sentencing as to X") == {"sentencing"}
        assert _proceeding_types("status-conf-ding-2") == {"status conference"}
        assert _proceeding_types("Initial Appearance") == {"initial appearance"}
        assert _proceeding_types("Arraignment held") == {"arraignment"}
        assert _proceeding_types("Change of Plea Hearing") == {"plea"}
        assert _proceeding_types("Motion in Limine Hearing") == {"motion"}
        assert _proceeding_types("Order to Show Cause Hearing") == {"show cause"}
        assert _proceeding_types("Charging Conference") == {"charging conference"}
        # A bare "trial" tags trial; "pretrial" tags only pretrial (it contains
        # "trial" as a substring but is a distinct proceeding).
        assert _proceeding_types("Jury Trial day 2") == {"trial"}
        assert _proceeding_types("Pretrial Conference") == {"pretrial"}
        assert _proceeding_types("pre-trial conference") == {"pretrial"}
        # A row keyed for both (e.g. "Final Pretrial / Jury Trial") keeps both.
        assert _proceeding_types("Jury Trial and Final Pretrial Conference") == {
            "trial",
            "pretrial",
        }
        # A "motion-hearing-pretrial" key carries motion + pretrial, not trial.
        assert _proceeding_types("motion-hearing-pretrial-akhter-3") == {
            "motion",
            "pretrial",
        }
        assert _proceeding_types(None) == set()
        assert _proceeding_types("Notice of courtroom change") == set()

    def test_record_proceeding_name(self):
        # Dominant format: name sits after "Judge <name>:" and before "as to".
        assert (
            _record_proceeding_name(
                "Minute Entry for proceedings held before Judge Vince Chhabria: "
                "Status Conference as to Linwei Ding held on 5/26/2026. "
                "Sentencing set for 6/1/2026."
            )
            == "Status Conference"
        )
        # "MINUTES OF <name>" format: name sits after "minutes of".
        assert (
            _record_proceeding_name(
                "MINUTES OF Status Conference held before Judge Blumenfeld: "
                "The Court heard from the parties."
            )
            == "Status Conference"
        )
        # A body that mentions a future Sentencing does NOT change the name.
        assert "Sentencing" not in _record_proceeding_name(
            "Minute Entry before Judge X: Change of Plea Hearing held on 1/2/2026. "
            "Sentencing set for 3/3/2026."
        )
        # No recognizable preamble -> full text (fallback).
        assert _record_proceeding_name("Some freeform note") == "Some freeform note"

    def test_is_admin_notice(self):
        assert _is_admin_notice("Clerk's notice providing Zoom access information")
        assert _is_admin_notice("NOTICE OF COURTROOM CHANGE")
        # A record that mentions clerk's NOTES is not a mere setup notice —
        # the proceeding-record check wins.
        assert not _is_admin_notice("Electronic Clerk's Notes for proceedings held")
        assert not _is_admin_notice("Motion Hearing held; under submission")
        assert not _is_admin_notice(None)

    def test_entry_records_proceeding(self):
        assert _entry_records_proceeding(
            {"description": "Minute Entry for proceedings held before Judge X"}
        )
        assert _entry_records_proceeding(
            {"description": "", "short_description": "Transcript of Proceedings held"}
        )
        # recap-document description path
        assert _entry_records_proceeding(
            {"recap_documents": [{"description": "Minute Entry: hearing held before"}]}
        )
        assert not _entry_records_proceeding(
            {"description": "CLERK'S NOTICE RE: ZOOM ACCESS"}
        )
        assert not _entry_records_proceeding({"description": ""})

    def test_best_proceeding_notes(self):
        # Target is an admin notice; a sibling records the proceeding.
        target = {"hearing_key": "a", "notes": "Clerk's notice re Zoom access"}
        sibling = {"hearing_key": "b", "notes": "Motion hearing held; under submission"}
        assert _best_proceeding_notes(target, [target, sibling]) == sibling["notes"]

        # Target already records the proceeding — leave it alone.
        rec_target = {"hearing_key": "a", "notes": "Hearing held; under submission"}
        assert (
            _best_proceeding_notes(
                rec_target,
                [
                    rec_target,
                    {"hearing_key": "b", "notes": "Transcript of Proceedings"},
                ],
            )
            is None
        )

        # No sibling describes the proceeding — leave the target alone.
        admin_only = {"hearing_key": "a", "notes": "Clerk's notice"}
        assert (
            _best_proceeding_notes(
                admin_only,
                [admin_only, {"hearing_key": "b", "notes": "Another clerk's notice"}],
            )
            is None
        )

        # Among multiple record siblings, the richest description wins.
        t = {"hearing_key": "a", "notes": "Zoom access notice"}
        short = {"hearing_key": "b", "notes": "held before judge"}
        longest = {
            "hearing_key": "c",
            "notes": (
                "Minute entry for proceedings held before Judge; parties stated "
                "appearances and argued at length"
            ),
        }
        assert _best_proceeding_notes(t, [t, short, longest]) == longest["notes"]

    def test_proceeding_notes_from_entry_strips_trailing_metadata(self):
        # The stacked trailing clerk/court-staff parentheticals are stripped;
        # the substantive minute-entry text is kept verbatim.
        entry = {
            "description": (
                "Minute Entry for proceedings held before Judge Lin: Motion "
                "Hearing held on 3/24/2026. Court took the matter under "
                "submission. (This is a text-only entry generated by the court. "
                "There is no document associated with this entry.) (afm, COURT "
                "STAFF) (Date Filed: 3/24/2026) (Entered: 03/24/2026)"
            ),
            "short_description": "",
        }
        out = _proceeding_notes_from_entry(entry)
        assert out is not None
        assert out.endswith("Court took the matter under submission.")
        assert "COURT STAFF" not in out
        assert "Date Filed" not in out
        assert "Entered" not in out
        assert "text-only entry" not in out
        # short_description fallback when description is empty
        assert (
            _proceeding_notes_from_entry(
                {"description": "", "short_description": "Transcript of Proceedings"}
            )
            == "Transcript of Proceedings"
        )
        # nothing usable -> None
        assert (
            _proceeding_notes_from_entry({"description": "", "short_description": ""})
            is None
        )


class TestMarkHeldSupersedesAdminNotes:
    """MARK_HELD adopts the proceeding record's notes when the existing notes
    are a pre-hearing administrative notice — the anthropic-v-dow
    pi-motion-hearing regression, where a minute entry marked the PI hearing
    held but the description stayed frozen on a clerk's Zoom-access notice."""

    _ADMIN = "Clerk's notice providing Zoom access information for the PI hearing."
    _RECORD = "Motion Hearing on Preliminary Injunction held; parties argued; under submission."

    def test_mark_held_supersedes_admin_notice(self, store, case, monkeypatch):
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_HEARING",
                        "hearing_key": "pi-hearing",
                        "hearing_type": "motion_hearing",
                        "title": "PI Motion Hearing",
                        "local_date": "2026-03-24",
                        "local_time": "13:30",
                        "notes": self._ADMIN,
                    }
                ],
                2: [
                    {
                        "type": "MARK_HELD",
                        "hearing_key": "pi-hearing",
                        "notes": self._RECORD,
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case, 100, _entry(1, "CLERK'S NOTICE RE: ZOOM ACCESS for PI hearing 3/24")
        )
        syncer.process_entry(
            case,
            100,
            _entry(
                2,
                "Minute Entry for proceedings held before Judge: Motion Hearing re "
                "Preliminary Injunction. Parties stated appearances.",
            ),
        )
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "held"
        # The minute entry's account replaced the clerk's-notice paraphrase.
        assert h["notes"] == self._RECORD

    def test_mark_held_keeps_existing_proceeding_notes(self, store, case, monkeypatch):
        # When the existing notes already describe the proceeding, a later
        # MARK_HELD does NOT clobber them with a thinner record's notes.
        existing_record = "Motion Hearing held; matter taken under submission."
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_HEARING",
                        "hearing_key": "pi-hearing",
                        "hearing_type": "motion_hearing",
                        "title": "PI Motion Hearing",
                        "local_date": "2026-03-24",
                        "local_time": "13:30",
                        "notes": existing_record,
                    }
                ],
                2: [
                    {
                        "type": "MARK_HELD",
                        "hearing_key": "pi-hearing",
                        "notes": "transcript of proceedings held",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case, 100, _entry(1, "Minute Entry: Motion Hearing held; under submission")
        )
        syncer.process_entry(
            case, 100, _entry(2, "Transcript of Proceedings held on 03/24/2026")
        )
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "held"
        assert h["notes"] == existing_record

    def test_mark_held_does_not_supersede_from_non_record_entry(
        self, store, case, monkeypatch
    ):
        # The held-trigger entry is a judgment, not a record of the proceeding
        # itself — the admin notes are left in place (the supersede gate is on
        # the ENTRY being a proceeding record).
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_HEARING",
                        "hearing_key": "pi-hearing",
                        "hearing_type": "motion_hearing",
                        "title": "PI Motion Hearing",
                        "local_date": "2026-03-24",
                        "local_time": "13:30",
                        "notes": self._ADMIN,
                    }
                ],
                2: [
                    {
                        "type": "MARK_HELD",
                        "hearing_key": "pi-hearing",
                        "notes": "marked held from a non-record entry",
                    }
                ],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case, 100, _entry(1, "CLERK'S NOTICE RE: ZOOM ACCESS for PI hearing 3/24")
        )
        # Passes the hearing pre-filter (so the stubbed MARK_HELD applies) but
        # is NOT itself the record of a held proceeding, so it must not
        # supersede the existing notes.
        syncer.process_entry(
            case,
            100,
            _entry(2, "NOTICE OF HEARING: PI motion hearing set for 3/24/2026"),
        )
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "held"
        assert h["notes"] == self._ADMIN

    def test_mark_held_falls_back_to_entry_text_when_action_has_no_notes(
        self, store, case, monkeypatch
    ):
        # The LLM routinely omits `notes` on a MARK_HELD for an already-held
        # row — the live anthropic-v-dow case. The supersede must then fall
        # back to the record entry's own text (trailing metadata stripped).
        minute = (
            "Minute Entry for proceedings held before Judge: Motion Hearing re "
            "Preliminary Injunction held on 3/24/2026. Parties stated "
            "appearances. Court took the matter under submission. (afm, COURT "
            "STAFF) (Date Filed: 3/24/2026)"
        )
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD_HEARING",
                        "hearing_key": "pi-hearing",
                        "hearing_type": "motion_hearing",
                        "title": "PI Motion Hearing",
                        "local_date": "2026-03-24",
                        "local_time": "13:30",
                        "notes": self._ADMIN,
                    }
                ],
                # MARK_HELD carries NO notes of its own.
                2: [{"type": "MARK_HELD", "hearing_key": "pi-hearing"}],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case, 100, _entry(1, "CLERK'S NOTICE RE: ZOOM ACCESS for PI hearing 3/24")
        )
        syncer.process_entry(case, 100, _entry(2, minute))
        h = store.get_hearings("us-v-x")[0]
        assert h["status"] == "held"
        assert h["notes"].endswith("Court took the matter under submission.")
        assert "COURT STAFF" not in h["notes"]
        assert "Clerk's notice" not in h["notes"]


class TestDedupeAdoptsProceedingNotes:
    """A dedupe MERGE_INTO (and the deterministic held merge) gives the
    survivor an absorbed sibling's proceeding-record notes when the target's
    own notes are a pre-hearing administrative notice — the second half of the
    anthropic-v-dow regression, where the near-slot merge kept the canonical
    row's clerk's-notice description and discarded the sibling's minute-entry
    account."""

    _ADMIN = "Clerk's notice providing Zoom access information."
    _RECORD = "Motion Hearing held; parties stated appearances; under submission."

    def _seed_pair(self, store, *, target_notes, sibling_notes, status, when):
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "pi-hearing",
                "title": "PI Motion Hearing",
                "starts_at_utc": when,
                "duration_minutes": 60,
                "timezone": "America/New_York",
                "status": status,
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [1, 2, 3],
                "notes": target_notes,
            }
        )
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "proceedings-2",
                "title": "Proceedings",
                "starts_at_utc": when,
                "duration_minutes": 60,
                "timezone": "America/New_York",
                "status": status,
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [9],
                "notes": sibling_notes,
            }
        )

    def test_merge_into_adopts_sibling_proceeding_notes(self, store, case, monkeypatch):
        self._seed_pair(
            store,
            target_notes=self._ADMIN,
            sibling_notes=self._RECORD,
            status="scheduled",
            when="2099-04-14T15:00:00+00:00",
        )
        stub_verify(monkeypatch)
        stub_dedupe(
            monkeypatch,
            action={
                "type": "MERGE_INTO",
                "target_key": "pi-hearing",
                "reason": "Same slot — clerk-notice key vs transcript key.",
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deduped"] == 1
        rows = {h["hearing_key"]: h for h in store.get_hearings("us-v-x")}
        assert "proceedings-2" not in rows
        # The survivor took the absorbed sibling's proceeding-record notes.
        assert rows["pi-hearing"]["notes"] == self._RECORD

    def test_merge_into_keeps_target_proceeding_notes(self, store, case, monkeypatch):
        # Target already records the proceeding — don't churn its notes.
        target_record = "Motion Hearing held; under submission (canonical)."
        self._seed_pair(
            store,
            target_notes=target_record,
            sibling_notes="Transcript of Proceedings held",
            status="scheduled",
            when="2099-04-14T15:00:00+00:00",
        )
        stub_verify(monkeypatch)
        stub_dedupe(
            monkeypatch,
            action={
                "type": "MERGE_INTO",
                "target_key": "pi-hearing",
                "reason": "Same slot.",
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        CaseSyncer(cl, store).sync_case(case)
        rows = {h["hearing_key"]: h for h in store.get_hearings("us-v-x")}
        assert rows["pi-hearing"]["notes"] == target_record

    def test_held_dedupe_adopts_sibling_proceeding_notes(
        self, store, case, monkeypatch
    ):
        # The deterministic held-row merge gets the same treatment.
        for did in (100, 101):
            store.upsert_docket_meta(
                did,
                {
                    "court_id": "mad",
                    "docket_number": "1:25-cr-00001-X",
                    "case_name": "United States v. X",
                    "absolute_url": f"/docket/{did}/x/",
                },
            )
        slot = "2026-02-19T16:00:00+00:00"
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "sentencing-x",
                "title": "Sentencing",
                "starts_at_utc": slot,
                "duration_minutes": 60,
                "timezone": "America/New_York",
                "status": "held",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [10, 11, 12],
                "notes": self._ADMIN,
            }
        )
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "sentencing-x-2",
                "title": "Sentencing",
                "starts_at_utc": slot,
                "duration_minutes": 60,
                "timezone": "America/New_York",
                "status": "held",
                "significance": "major",
                "docket_id": 101,
                "source_entry_ids": [99],
                "notes": self._RECORD,
            }
        )
        stub_verify(monkeypatch)

        def boom(*a, **k):
            raise AssertionError("held dedupe should be deterministic")

        monkeypatch.setattr(llm_mod, "resolve_duplicate_hearings", boom)
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deduped_held"] == 1
        rows = {h["hearing_key"]: h for h in store.get_hearings("us-v-x")}
        assert "sentencing-x-2" not in rows
        # Canonical (more sources) survived but adopted the sibling's record.
        assert rows["sentencing-x"]["notes"] == self._RECORD


class TestHealProceedingNotes:
    """Deterministic backfill that fixes hearings whose notes regressed to a
    pre-hearing administrative notice, using each row's own record source
    entries — the cross-database heal for rows already collapsed in the store
    (where the rich-notes sibling was deleted by a dedupe merge and re-running
    sync can't recover them)."""

    _ADMIN = "Clerk's notice providing Zoom access information."
    _MINUTE = (
        "Minute Entry for proceedings held before Judge: Motion Hearing held "
        "on 3/24/2026. Parties stated appearances. Court took the matter under "
        "submission. (afm, COURT STAFF) (Date Filed: 3/24/2026)"
    )
    _MINUTE_CLEAN = (
        "Minute Entry for proceedings held before Judge: Motion Hearing held "
        "on 3/24/2026. Parties stated appearances. Court took the matter under "
        "submission."
    )

    def _seed_hearing(self, store, key, notes, sources, status="held"):
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": key,
                "title": key.replace("-", " ").title(),
                "starts_at_utc": "2026-03-24T20:30:00+00:00",
                "duration_minutes": 60,
                "timezone": "America/New_York",
                "status": status,
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": sources,
                "notes": notes,
            }
        )

    def _seed_entry(self, store, eid, desc):
        store.mark_entry(
            100, eid, "2026-03-24T00:00:00-07:00", f"fp-{eid}", description=desc
        )

    def _seed_world(self, store):
        # A clerk's notice (not a record) and the minute entry that records the
        # Motion Hearing proceeding on 3/24/2026.
        self._seed_entry(store, 112, "CLERK'S NOTICE RE: ZOOM ACCESS for PI hearing")
        self._seed_entry(store, 123, self._MINUTE)
        # A record-shaped source whose text is ENTIRELY trailing metadata, so
        # the cleaner returns nothing — exercises the "record entry but no
        # usable text" guard (must not feed None into the richest-record pick).
        self._seed_entry(
            store, 124, "(Minute Entry generated by the court, afm, COURT STAFF)"
        )
        # A non-record source for the no-record case.
        self._seed_entry(store, 130, "CLERK'S NOTICE OF COURTROOM CHANGE")
        # A record for a DIFFERENT proceeding on a DIFFERENT date — the
        # cross-proceeding date trap: a real minute entry whose date doesn't
        # match the row's, so it must NOT heal the row.
        self._seed_entry(
            store,
            140,
            "Minute Entry for proceedings held before Judge: Status Conference "
            "held on 1/15/2020. Parties appeared.",
        )
        # A Status Conference record on the SAME date as the row (3/24/2026) —
        # the cross-proceeding TYPE trap: date matches but the proceeding kind
        # doesn't, so a sentencing-keyed row must not adopt it. Its BODY even
        # mentions a future "Sentencing", which must NOT make it look like a
        # sentencing record (the type match runs on the head, before the date).
        self._seed_entry(
            store,
            150,
            "Minute Entry for proceedings held before Judge: Status Conference "
            "held on 3/24/2026. Sentencing set for 6/1/2026.",
        )
        # Record sources for the good / curated rows (proving they're skipped
        # for reasons other than "no record available").
        self._seed_entry(store, 200, "Minute Entry: proceedings held; argued")
        self._seed_entry(store, 201, "Minute Entry: proceedings held; argued")
        self._seed_entry(store, 203, self._MINUTE)

        # Regression, UNTYPED key: admin notes + a Motion-Hearing minute-entry
        # source (plus the empty-text record 124). The key has no recognizable
        # proceeding type, so the type guard is bypassed and the date match
        # alone heals it from record 123.
        self._seed_hearing(store, "pi-hearing", self._ADMIN, [112, 124, 123])
        # Regression, TYPED key that MATCHES the record's type (motion) and
        # date -> healed.
        self._seed_hearing(store, "motion-hearing-typed", self._ADMIN, [123])
        # Regression, TYPED key (sentencing) whose only same-date record is a
        # STATUS CONFERENCE -> untouched (type guard), even though the date
        # matches. This is the sentencing-ding-class mismatch.
        self._seed_hearing(store, "sentencing-typed", self._ADMIN, [150])
        # Admin notes but NO source entries at all -> untouched.
        self._seed_hearing(store, "no-sources-hearing", self._ADMIN, [])
        # Admin notes + a record source whose date (1/15/2020) does NOT match
        # the row's date (3/24/2026) -> untouched.
        self._seed_hearing(store, "wrong-date-hearing", self._ADMIN, [140])
        # Empty notes + a minute-entry source -> healed.
        self._seed_hearing(store, "empty-hearing", None, [203])
        # Already describes the proceeding -> untouched.
        self._seed_hearing(
            store, "good-hearing", "Motion Hearing held; under submission", [200]
        )
        # Curated non-administrative note -> untouched.
        self._seed_hearing(
            store,
            "curated-hearing",
            "Oral argument on cross-motions; 30 min/side",
            [201],
        )
        # Admin notes but no record among the sources -> untouched.
        self._seed_hearing(store, "no-record-hearing", self._ADMIN, [130])

    _HEALED = {"pi-hearing", "motion-hearing-typed", "empty-hearing"}

    def test_dry_run_reports_without_mutating(self, store):
        self._seed_world(store)
        changes = heal_proceeding_notes(store, apply=False)
        healed = {c["hearing_key"] for c in changes}
        assert healed == self._HEALED
        # Nothing written.
        rows = {h["hearing_key"]: h for h in store.get_hearings("us-v-x")}
        assert rows["pi-hearing"]["notes"] == self._ADMIN
        assert rows["empty-hearing"]["notes"] is None

    def test_apply_heals_regressed_rows_only(self, store):
        self._seed_world(store)
        changes = heal_proceeding_notes(store, apply=True)
        healed = {c["hearing_key"] for c in changes}
        assert healed == self._HEALED
        rows = {h["hearing_key"]: h for h in store.get_hearings("us-v-x")}
        # Healed rows now carry the cleaned minute-entry text.
        assert rows["pi-hearing"]["notes"] == self._MINUTE_CLEAN
        assert rows["motion-hearing-typed"]["notes"] == self._MINUTE_CLEAN
        assert rows["empty-hearing"]["notes"] == self._MINUTE_CLEAN
        # Untouched rows keep their notes — including the same-date but
        # wrong-TYPE sentencing row.
        assert rows["sentencing-typed"]["notes"] == self._ADMIN
        assert rows["wrong-date-hearing"]["notes"] == self._ADMIN
        assert rows["good-hearing"]["notes"] == "Motion Hearing held; under submission"
        assert (
            rows["curated-hearing"]["notes"]
            == "Oral argument on cross-motions; 30 min/side"
        )
        assert rows["no-record-hearing"]["notes"] == self._ADMIN
        # Idempotent: a second pass finds nothing.
        assert heal_proceeding_notes(store, apply=True) == []

    def test_no_regressions_returns_empty(self, store):
        self._seed_entry(store, 200, "Minute Entry: proceedings held")
        self._seed_hearing(
            store, "good-hearing", "Motion Hearing held; under submission", [200]
        )
        assert heal_proceeding_notes(store, apply=True) == []


class TestIsPendingEnrichment:
    """The placeholder predicate: an entry with no readable body yet but an
    unsealed, not-yet-available document that could still arrive — the
    us-v-kejia-wang #42 shape a docket-alert webhook delivers at creation.
    """

    def _rd(self, **over):
        rd = {
            "is_available": False,
            "is_sealed": False,
            "plain_text": "",
            "description": "Order on Motion for Extension of Time",
        }
        rd.update(over)
        return rd

    def test_empty_body_with_unavailable_doc_is_pending(self):
        # The Wang #42 shape: empty description, unsealed doc not on RECAP.
        entry = {"description": "", "recap_documents": [self._rd()]}
        assert is_pending_enrichment(entry) is True

    def test_whitespace_only_body_is_still_empty(self):
        entry = {"description": "   \n ", "recap_documents": [self._rd()]}
        assert is_pending_enrichment(entry) is True

    def test_nonempty_body_is_not_pending(self):
        # The Wang #41 shape: a paperless electronic order whose full text
        # IS the description. Its doc may be is_available=False forever, but
        # there is nothing to enrich — the body guard excludes it.
        entry = {
            "description": "ELECTRONIC ORDER finding as moot 39 Motion ...",
            "recap_documents": [self._rd()],
        }
        assert is_pending_enrichment(entry) is False

    def test_available_doc_is_not_pending(self):
        entry = {"description": "", "recap_documents": [self._rd(is_available=True)]}
        assert is_pending_enrichment(entry) is False

    def test_doc_with_text_is_not_pending(self):
        entry = {
            "description": "",
            "recap_documents": [self._rd(plain_text="full order text here")],
        }
        assert is_pending_enrichment(entry) is False

    def test_sealed_doc_is_not_pending(self):
        # Sealed docs won't enrich on a re-fetch (they're not on RECAP); the
        # unseal flips a different fingerprint field and is handled there.
        entry = {"description": "", "recap_documents": [self._rd(is_sealed=True)]}
        assert is_pending_enrichment(entry) is False

    def test_no_documents_is_not_pending(self):
        entry = {"description": "", "recap_documents": []}
        assert is_pending_enrichment(entry) is False

    def test_one_pending_doc_among_several_qualifies(self):
        entry = {
            "description": "",
            "recap_documents": [self._rd(is_available=True), self._rd()],
        }
        assert is_pending_enrichment(entry) is True


def _placeholder_entry(eid, *, date_filed="2026-05-20"):
    """A webhook-delivered stub: empty body, a not-yet-available doc whose
    label trips the deadline regex so it's stored with recap_documents."""
    return {
        "id": eid,
        "docket": 100,
        "entry_number": eid,
        "date_filed": date_filed,
        "date_modified": f"{date_filed}T10:53:00-07:00",
        "description": "",
        "short_description": "",
        "recap_documents": [
            {
                "id": 9000 + eid,
                "document_number": str(eid),
                "attachment_number": None,
                "description": "Order on Motion for Extension of Time",
                "is_available": False,
                "is_sealed": False,
                "filepath_ia": None,
                "filepath_local": None,
                "plain_text": "",
            }
        ],
    }


def _enriched_entry(eid, *, date_filed="2026-05-20"):
    """The same entry after CourtListener filled in the order text + PDF."""
    e = _placeholder_entry(eid, date_filed=date_filed)
    e["date_modified"] = f"{date_filed}T12:32:00-07:00"
    e["description"] = (
        "ENDORSED ORDER granting in part 42 Motion for Extension of Time. "
        "Defendant shall self report to the Bureau of Prisons on July 10, 2026."
    )
    e["recap_documents"][0]["is_available"] = True
    e["recap_documents"][0]["plain_text"] = "full endorsed order text ... July 10, 2026"
    return e


class TestReconcilePlaceholders:
    """``CaseSyncer.reconcile_placeholders`` re-checks placeholder entries by
    id and reschedules from the enriched copy — the fix for the
    us-v-kejia-wang miss (webhook delivered a stub, CourtListener enriched it
    later with no second webhook).
    """

    def _seed_placeholder(self, store, case, monkeypatch, eid=42):
        """Run one sync that delivers the placeholder, so the store holds the
        stub (with recap_documents) and the docket meta is cached."""
        cl = FakeCourtListener(
            dockets={100: _docket()},
            entries={100: [_placeholder_entry(eid)]},
        )
        # The placeholder reaches the LLM (the doc label trips the regex) but
        # has no date to extract, so it IGNOREs — exactly the webhook path.
        make_llm_stub(
            monkeypatch, by_entry={eid: [{"type": "IGNORE", "reason": "stub"}]}
        )
        CaseSyncer(cl, store).sync_case(case)
        return cl

    def test_enriched_placeholder_reschedules(self, store, case, monkeypatch):
        self._seed_placeholder(store, case, monkeypatch)
        # No deadline yet — the stub carried no date.
        assert store.get_deadlines("us-v-x") == []

        # Now the entry has enriched upstream; the LLM, seeing the real text,
        # extracts the surrender deadline.
        def fake_extract(*, entry, **_):
            if "July 10" in (entry.get("description") or ""):
                return [
                    {
                        "type": "ADD_DEADLINE",
                        "deadline_key": "surrender-x",
                        "deadline_type": "other",
                        "title": "Self-surrender to Bureau of Prisons",
                        "local_date": "2026-07-10",
                        "local_time": "14:00",
                        "significance": "major",
                    }
                ]
            return [{"type": "IGNORE", "reason": "still a stub"}]

        monkeypatch.setattr(llm_mod, "extract_actions", fake_extract)

        cl = FakeCourtListener(
            dockets={100: _docket()},
            entry_by_id={42: _enriched_entry(42)},
        )
        stats = CaseSyncer(cl, store).reconcile_placeholders(
            case, filed_after="2026-04-01"
        )

        assert stats["checked"] == 1
        assert stats["entries_processed"] == 1
        # The reconcile fetched exactly one entry by id (O(placeholders)),
        # not a docket-entries page.
        assert [c for c in cl.calls if c[0] == "docket_entry"] == [("docket_entry", 42)]
        deadlines = {d["deadline_key"]: d for d in store.get_deadlines("us-v-x")}
        assert "surrender-x" in deadlines
        assert deadlines["surrender-x"]["due_at_utc"].startswith("2026-07-10")

    def test_unchanged_placeholder_is_a_noop(self, store, case, monkeypatch):
        self._seed_placeholder(store, case, monkeypatch)

        def boom(*a, **k):  # must not reach the LLM on an unchanged stub
            raise AssertionError("LLM called for an unchanged placeholder")

        monkeypatch.setattr(llm_mod, "extract_actions", boom)

        # get_docket_entry returns the SAME stub — fingerprint unchanged.
        cl = FakeCourtListener(
            dockets={100: _docket()},
            entry_by_id={42: _placeholder_entry(42)},
        )
        stats = CaseSyncer(cl, store).reconcile_placeholders(
            case, filed_after="2026-04-01"
        )
        assert stats["checked"] == 1
        # Re-fetched, but the fingerprint matched so process_entry no-oped
        # before the LLM — no actions, nothing to re-emit.
        assert stats["entries_processed"] == 0
        assert stats["actions"] == 0
        assert store.get_deadlines("us-v-x") == []

    def test_age_cutoff_excludes_old_placeholders(self, store, case, monkeypatch):
        # An old stub is outside the filed_after window, so it's never
        # re-fetched — bounds retries on stubs that never enrich.
        self._seed_placeholder(store, case, monkeypatch, eid=7)
        # Move the seeded entry's date_filed into the past is implicit:
        # _placeholder_entry defaults to 2026-05-20; ask for entries filed
        # on/after a later date.
        cl = FakeCourtListener(
            dockets={100: _docket()}, entry_by_id={7: _enriched_entry(7)}
        )
        stats = CaseSyncer(cl, store).reconcile_placeholders(
            case, filed_after="2026-06-01"
        )
        assert stats["checked"] == 0
        assert [c for c in cl.calls if c[0] == "docket_entry"] == []

    def test_empty_body_with_available_doc_is_not_fetched(
        self, store, case, monkeypatch
    ):
        # The SQL pre-filter returns empty-body rows that carry docs, but a
        # row whose document is already available is NOT a pending
        # placeholder — the doc-level predicate rejects it, so it's skipped
        # without a fetch. Seed it directly so the body stays empty.
        store.upsert_docket_meta(100, {"court_id": "mad", "docket_number": "1:25-x"})
        store.mark_entry(
            100,
            55,
            "2026-05-20T10:00:00Z",
            "fp",
            date_filed="2026-05-20",
            description="",
            recap_documents=[{"id": 1, "is_available": True, "plain_text": ""}],
        )

        def boom(*a, **k):
            raise AssertionError("no entry should be fetched")

        cl = FakeCourtListener(dockets={100: _docket()})
        monkeypatch.setattr(cl, "get_docket_entry", boom)
        stats = CaseSyncer(cl, store).reconcile_placeholders(
            case, filed_after="2026-04-01"
        )
        assert stats["checked"] == 0


# --- Part 1: multi-record group canonicalization for the extractor ---


class TestMultiRecordGroupCanonicalization:
    """When CourtListener splits one logical PACER docket across several
    docket_id rows (the pacer_case_id reconciler, bug #7345), the per-entry
    extractor must see them AS ONE docket so the cross-docket rule doesn't
    force a drift-suffixed key (the "Sentencing Lytvynenko 2" bug)."""

    @staticmethod
    def _capture_extract(monkeypatch):
        captured: dict = {}

        def fake(*, group_docket_ids=None, canonical_docket_id=None, **_):
            captured["group_docket_ids"] = group_docket_ids
            captured["canonical_docket_id"] = canonical_docket_id
            return [{"type": "IGNORE", "reason": "stub"}]

        monkeypatch.setattr(llm_mod, "extract_actions", fake)
        return captured

    def test_multi_record_group_passes_canonical_to_extractor(self, store, monkeypatch):
        # Two docket_ids sharing (docket_number, court_id) -> the extractor is
        # told the group + the stable canonical id (min of the group).
        for did in (73510620, 71820111):
            store.upsert_docket_meta(
                did, {"court_id": "tnmd", "docket_number": "3:23-cr-00088"}
            )
        case_multi = CaseConfig(
            case_id="x", name="X", dockets=[71820111, 73510620], calendar="t"
        )
        captured = self._capture_extract(monkeypatch)
        cl = FakeCourtListener(
            dockets={
                73510620: {
                    "id": 73510620,
                    "court_id": "tnmd",
                    "docket_number": "3:23-cr-00088",
                }
            }
        )
        CaseSyncer(cl, store).process_entry(
            case_multi, 73510620, _entry(42, "Sentencing set")
        )
        assert captured["group_docket_ids"] == {71820111, 73510620}
        assert captured["canonical_docket_id"] == 71820111

    def test_single_record_docket_passes_no_group(self, store, monkeypatch):
        # A docket that is the only record in its (number, court) group gets
        # identity behavior — no canonicalization.
        store.upsert_docket_meta(100, {"court_id": "mad", "docket_number": "1:25-cr-1"})
        case_one = CaseConfig(case_id="x", name="X", dockets=[100], calendar="t")
        captured = self._capture_extract(monkeypatch)
        cl = FakeCourtListener(dockets={100: _docket()})
        CaseSyncer(cl, store).process_entry(case_one, 100, _entry(42, "Sentencing set"))
        assert captured["group_docket_ids"] is None
        assert captured["canonical_docket_id"] is None

    def test_same_court_different_number_is_not_grouped(self, store, monkeypatch):
        # Co-defendant dockets in the same court but with DIFFERENT docket
        # numbers are genuinely distinct dockets, not a multi-record group —
        # the cross-docket rule must still fire, so no canonicalization.
        store.upsert_docket_meta(
            100, {"court_id": "mad", "docket_number": "1:25-cr-1-A"}
        )
        store.upsert_docket_meta(
            101, {"court_id": "mad", "docket_number": "1:25-cr-1-B"}
        )
        case_multi = CaseConfig(case_id="x", name="X", dockets=[100, 101], calendar="t")
        captured = self._capture_extract(monkeypatch)
        cl = FakeCourtListener(dockets={100: _docket()})
        CaseSyncer(cl, store).process_entry(
            case_multi, 100, _entry(42, "Sentencing set")
        )
        assert captured["group_docket_ids"] is None
        assert captured["canonical_docket_id"] is None

    def test_docket_without_number_skips_group_resolution(self, store, monkeypatch):
        # A docket whose cached meta carries no docket_number (court_id only)
        # can't be grouped — the guard skips group resolution entirely.
        store.upsert_docket_meta(100, {"court_id": "mad"})
        case_one = CaseConfig(case_id="x", name="X", dockets=[100], calendar="t")
        captured = self._capture_extract(monkeypatch)
        cl = FakeCourtListener(dockets={100: _docket()})
        CaseSyncer(cl, store).process_entry(case_one, 100, _entry(42, "Sentencing set"))
        assert captured["group_docket_ids"] is None
        assert captured["canonical_docket_id"] is None


# --- Part 2: dedupe merge prefers the suffix-free canonical key ---


class TestDedupePrefersCanonicalKey:
    """A base/base-N cluster must collapse onto the suffix-free `base`, even
    when the `-N` row carries more source_entry_ids — otherwise the survivor
    keeps the drift suffix (the visible "Sentencing Lytvynenko 2" artifact)."""

    def test_llm_gated_sweep_repoints_to_base_over_n_target(
        self, store, case, monkeypatch
    ):
        for did in (100, 101):
            store.upsert_docket_meta(
                did, {"court_id": "mad", "docket_number": "1:25-cr-00001-X"}
            )
        slot = "2099-09-10T15:00:00+00:00"
        # base has FEWER sources and a key-derived title; base-N has MORE
        # sources AND an explicit (LLM-written) title.
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "sentencing-x",
                "title": "Sentencing X",  # key-derived
                "starts_at_utc": slot,
                "duration_minutes": 60,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [10],
            }
        )
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "sentencing-x-2",
                "title": "Sentencing Hearing",  # explicit
                "starts_at_utc": slot,
                "duration_minutes": 60,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": 101,
                "source_entry_ids": [20, 21, 22],
            }
        )
        stub_verify(monkeypatch)
        # The LLM (as it did in the live bug) picks the -N row as the target.
        stub_dedupe(
            monkeypatch,
            action={
                "type": "MERGE_INTO",
                "target_key": "sentencing-x-2",
                "reason": "same slot",
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deduped"] == 1
        rows = {h["hearing_key"]: h for h in store.get_hearings("us-v-x")}
        # Survivor is the suffix-free base, not the -N the LLM named.
        assert "sentencing-x" in rows
        assert "sentencing-x-2" not in rows
        assert set(rows["sentencing-x"]["source_entry_ids"]) == {10, 20, 21, 22}
        # base's own title was key-derived, so the survivor adopts the
        # absorbed sibling's explicit title rather than carrying "Sentencing X".
        assert rows["sentencing-x"]["title"] == "Sentencing Hearing"

    def test_held_sweep_prefers_base_when_n_has_more_sources(
        self, store, case, monkeypatch
    ):
        for did in (100, 101):
            store.upsert_docket_meta(
                did, {"court_id": "mad", "docket_number": "1:25-cr-00001-X"}
            )
        slot = "2026-02-19T16:00:00+00:00"
        # base: fewer sources, key-derived title. base-N: MORE sources.
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "sentencing-didenko",
                "title": "Sentencing Didenko",
                "starts_at_utc": slot,
                "duration_minutes": 60,
                "timezone": "America/New_York",
                "status": "held",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [10],
            }
        )
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "sentencing-didenko-2",
                "title": "Sentencing Hearing",  # explicit
                "starts_at_utc": slot,
                "duration_minutes": 60,
                "timezone": "America/New_York",
                "status": "held",
                "significance": "major",
                "docket_id": 101,
                "source_entry_ids": [20, 21, 22],
            }
        )
        stub_verify(monkeypatch)
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deduped_held"] == 1
        rows = {h["hearing_key"]: h for h in store.get_hearings("us-v-x")}
        # Survivor is the suffix-free base despite having fewer sources, and
        # adopts the absorbed sibling's explicit title (base's was key-derived).
        assert "sentencing-didenko" in rows
        assert "sentencing-didenko-2" not in rows
        assert set(rows["sentencing-didenko"]["source_entry_ids"]) == {10, 20, 21, 22}
        assert rows["sentencing-didenko"]["title"] == "Sentencing Hearing"


# --- Part 3: heal already-drifted keys ---


class TestHealDriftedKeys:
    """Retroactive canonicalization of `base-N` survivors already collapsed in
    the store (re-sync can't re-cluster them). Two provable-drift signals only;
    meaningful trailing numbers are left untouched."""

    def _seed_group(self, store):
        for did in (100, 101):
            store.upsert_docket_meta(
                did, {"court_id": "tnmd", "docket_number": "3:23-cr-00088"}
            )

    def _h(
        self,
        store,
        key,
        *,
        status="scheduled",
        slot,
        sources,
        audit=None,
        did=101,
        title=None,
        notes=None,
    ):
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": key,
                "title": title if title is not None else key.replace("-", " ").title(),
                "starts_at_utc": slot,
                "duration_minutes": 60,
                "timezone": "America/Chicago",
                "notes": notes,
                "audit_notes": audit,
                "status": status,
                "significance": "major",
                "docket_id": did,
                "source_entry_ids": sources,
            }
        )

    def test_rename_via_absorption_audit_fixes_key_title_and_m365(self, store):
        # base absent (deleted at merge), audit records the absorption -> rename.
        self._seed_group(store)
        self._h(
            store,
            "sentencing-lytvynenko-2",
            status="scheduled",
            slot="2026-09-10T15:00:00+00:00",
            sources=[10, 11],
            audit="[dedupe] Absorbed sibling key(s) sentencing-lytvynenko: same slot",
            did=101,
        )
        store.set_m365_id_for_hearing(
            "us-v-x", "sentencing-lytvynenko-2", "OLD-GRAPH-ID"
        )
        changes = heal_drifted_keys(store, apply=True)
        assert len(changes) == 1
        assert changes[0]["action"] == "rename"
        rows = {h["hearing_key"]: h for h in store.get_hearings("us-v-x")}
        assert "sentencing-lytvynenko-2" not in rows
        assert "sentencing-lytvynenko" in rows
        survivor = rows["sentencing-lytvynenko"]
        # Key-derived title recomputed from the canonical key (no "2").
        assert survivor["title"] == "Sentencing Lytvynenko"
        assert survivor["source_entry_ids"] == [10, 11]
        # M365 id cleared so the next emit re-creates cleanly under the new key.
        assert (
            store.get_hearing("us-v-x", "sentencing-lytvynenko")["m365_event_id"]
            is None
        )

    def test_delete_via_same_slot_base_coexistence(self, store):
        # base exists at the SAME slot in the same group -> delete the -N row,
        # fold its sources into base. base has a key-derived title and empty
        # notes; the -N sibling carries an explicit title AND a proceeding
        # record, so the survivor adopts BOTH (the dedupe-merge reasoning).
        self._seed_group(store)
        slot = "2026-12-18T16:00:00+00:00"
        self._h(
            store,
            "sentencing-mcgonigal",  # key-derived title "Sentencing Mcgonigal"
            status="held",
            slot=slot,
            sources=[1, 2],
            did=100,
            notes=None,
        )
        self._h(
            store,
            "sentencing-mcgonigal-2",
            status="held",
            slot=slot,
            sources=[2, 3],
            did=101,
            title="Sentencing Hearing",
            notes="Minute Entry for proceedings held: Sentencing as to McGonigal held.",
        )
        changes = heal_drifted_keys(store, apply=True)
        assert [c["action"] for c in changes] == ["delete"]
        rows = {h["hearing_key"]: h for h in store.get_hearings("us-v-x")}
        assert "sentencing-mcgonigal-2" not in rows
        survivor = rows["sentencing-mcgonigal"]
        assert set(survivor["source_entry_ids"]) == {1, 2, 3}
        # Adopted the absorbed sibling's explicit title and proceeding notes.
        assert survivor["title"] == "Sentencing Hearing"
        assert "proceedings held" in survivor["notes"]
        assert "[heal-drift]" in (survivor["audit_notes"] or "")

    def test_delete_keeps_base_when_already_good(self, store):
        # Signal 2 where base already has an explicit title AND a proceeding
        # record — nothing to adopt from the absorbed sibling, so the survivor
        # keeps its own title/notes (exercises the no-upgrade branches).
        self._seed_group(store)
        slot = "2026-12-18T16:00:00+00:00"
        self._h(
            store,
            "sentencing-mcgonigal",
            status="held",
            slot=slot,
            sources=[1],
            did=100,
            title="Sentencing Hearing",
            notes="Minute Entry for proceedings held: Sentencing held.",
        )
        self._h(
            store,
            "sentencing-mcgonigal-2",
            status="held",
            slot=slot,
            sources=[2],
            did=101,
        )
        changes = heal_drifted_keys(store, apply=True)
        assert [c["action"] for c in changes] == ["delete"]
        rows = {h["hearing_key"]: h for h in store.get_hearings("us-v-x")}
        assert "sentencing-mcgonigal-2" not in rows
        survivor = rows["sentencing-mcgonigal"]
        assert survivor["title"] == "Sentencing Hearing"
        assert "proceedings held" in survivor["notes"]
        assert set(survivor["source_entry_ids"]) == {1, 2}

    def test_delete_dry_run_does_not_mutate(self, store):
        # Signal 2 in dry-run: the delete is reported but not applied.
        self._seed_group(store)
        slot = "2026-08-11T14:00:00+00:00"
        self._h(store, "trial-x", status="cancelled", slot=slot, sources=[1], did=100)
        self._h(store, "trial-x-2", status="cancelled", slot=slot, sources=[2], did=101)
        changes = heal_drifted_keys(store, apply=False)
        assert [c["action"] for c in changes] == ["delete"]
        keys = {h["hearing_key"] for h in store.get_hearings("us-v-x")}
        assert {"trial-x", "trial-x-2"} <= keys  # nothing deleted

    def test_meaningful_sequence_is_left_untouched(self, store):
        # base exists at a DIFFERENT slot and there is no absorption note —
        # a genuine sequential conference, NOT drift. Leave both alone.
        self._seed_group(store)
        self._h(
            store,
            "status-conf-ding",
            status="held",
            slot="2026-01-01T16:00:00+00:00",
            sources=[1],
            did=100,
        )
        self._h(
            store,
            "status-conf-ding-2",
            status="scheduled",
            slot="2026-03-01T16:00:00+00:00",
            sources=[2],
            did=100,
        )
        assert heal_drifted_keys(store, apply=True) == []
        rows = {h["hearing_key"] for h in store.get_hearings("us-v-x")}
        assert {"status-conf-ding", "status-conf-ding-2"} <= rows

    def test_lone_drift_key_without_proof_is_left_untouched(self, store):
        # base absent AND no absorption note -> can't prove drift, leave alone.
        self._seed_group(store)
        self._h(
            store,
            "change-of-plea-x-2",
            status="cancelled",
            slot="2026-08-03T16:00:00+00:00",
            sources=[1],
            audit="[verify-pass] superseded",
        )
        assert heal_drifted_keys(store, apply=False) == []

    def test_dry_run_does_not_mutate(self, store):
        self._seed_group(store)
        self._h(
            store,
            "sentencing-lytvynenko-2",
            status="scheduled",
            slot="2026-09-10T15:00:00+00:00",
            sources=[10],
            audit="[dedupe] Absorbed sibling key(s) sentencing-lytvynenko: x",
        )
        changes = heal_drifted_keys(store, apply=False)
        assert len(changes) == 1
        # Nothing written.
        rows = {h["hearing_key"] for h in store.get_hearings("us-v-x")}
        assert "sentencing-lytvynenko-2" in rows
        assert "sentencing-lytvynenko" not in rows


# --- drift-key helper units ---


class TestDriftKeyHelpers:
    def test_drift_base(self):
        assert _drift_base("sentencing-x-2") == "sentencing-x"
        assert _drift_base("trial-ding-day-2") == "trial-ding-day"
        assert _drift_base("sentencing-x") is None  # no trailing -digits
        assert _drift_base(None) is None
        assert _drift_base("") is None

    def test_canonical_drift_key(self):
        # base + base-N present -> base is canonical.
        assert (
            _canonical_drift_key(["sentencing-x", "sentencing-x-2"]) == "sentencing-x"
        )
        # base absent -> no canonical (leave caller's choice).
        assert _canonical_drift_key(["sentencing-x-2", "sentencing-x-3"]) is None
        # nested: shortest base wins.
        assert _canonical_drift_key(["a-b", "a-b-2", "a-b-2-3"]) == "a-b"
        # no suffix anywhere.
        assert _canonical_drift_key(["sentencing-x", "trial-x"]) is None

    def test_key_to_title_and_is_key_derived(self):
        assert _key_to_title("sentencing-lytvynenko-2") == "Sentencing Lytvynenko 2"
        assert _key_to_title(None) == ""
        assert _title_is_key_derived(
            {"hearing_key": "sentencing-x", "title": "Sentencing X"}
        )
        assert not _title_is_key_derived(
            {"hearing_key": "sentencing-x", "title": "Jury Trial"}
        )

    def test_best_dedupe_title(self):
        # Target key-derived; a sibling has an explicit title -> adopt it.
        target = {"hearing_key": "sentencing-x", "title": "Sentencing X"}
        cluster = [
            target,
            {"hearing_key": "sentencing-x-2", "title": "Sentencing Hearing"},
        ]
        assert _best_dedupe_title(target, cluster) == "Sentencing Hearing"
        # Target already explicit -> leave alone.
        explicit = {"hearing_key": "sentencing-x", "title": "Jury Trial"}
        assert _best_dedupe_title(explicit, cluster) is None
        # All key-derived -> None (no upgrade available).
        all_kd = [
            {"hearing_key": "sentencing-x", "title": "Sentencing X"},
            {"hearing_key": "sentencing-x-2", "title": "Sentencing X 2"},
        ]
        assert _best_dedupe_title(all_kd[0], all_kd) is None

    def test_absorbed_sibling_keys(self):
        audit = (
            "[verify-pass] something\n\n"
            "[dedupe] Absorbed sibling key(s) sentencing-x, motion-hearing-2: reason\n"
            "[dedupe-held] Absorbed sibling key(s) detention-x at same UTC slot 2026"
        )
        assert _absorbed_sibling_keys(audit) == {
            "sentencing-x",
            "motion-hearing-2",
            "detention-x",
        }
        # Trailing comma yields no empty token.
        assert _absorbed_sibling_keys("Absorbed sibling key(s) a, : x") == {"a"}
        assert _absorbed_sibling_keys(None) == set()

    def test_same_logical_slot(self, store):
        slot = "2026-09-10T15:00:00+00:00"
        a = {"starts_at_utc": slot, "docket_id": 100}
        # Same docket_id -> True without any meta lookup.
        assert _same_logical_slot(store, a, {"starts_at_utc": slot, "docket_id": 100})
        # Different slot -> False.
        assert not _same_logical_slot(
            store, a, {"starts_at_utc": "2026-01-01T00:00:00+00:00", "docket_id": 100}
        )
        # A None docket_id -> False (can't resolve a group).
        assert not _same_logical_slot(
            store, a, {"starts_at_utc": slot, "docket_id": None}
        )
        # Different docket_ids in the SAME (number, court) group -> True.
        store.upsert_docket_meta(100, {"court_id": "tnmd", "docket_number": "3:23-x"})
        store.upsert_docket_meta(101, {"court_id": "tnmd", "docket_number": "3:23-x"})
        assert _same_logical_slot(store, a, {"starts_at_utc": slot, "docket_id": 101})
        # Same slot but different docket group -> False.
        store.upsert_docket_meta(102, {"court_id": "tnmd", "docket_number": "9:99-y"})
        assert not _same_logical_slot(
            store, a, {"starts_at_utc": slot, "docket_id": 102}
        )
