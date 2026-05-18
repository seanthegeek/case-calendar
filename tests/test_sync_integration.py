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
from case_calendar.sync import CaseConfig, CaseSyncer, fingerprint_entry

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
        # Defensive guard: if the LLM returns ADD with no date (e.g. on a
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
                        "type": "ADD",
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
                        "type": "ADD",
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
                        "type": "ADD",
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
                        "type": "RESCHEDULE",
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
        assert len(rows) == 1, "RESCHEDULE should update in place, not duplicate"
        # 11:00 EDT → 15:00 UTC.
        assert rows[0]["starts_at_utc"] == "2026-04-14T15:00:00+00:00"
        assert set(rows[0]["source_entry_ids"]) == {1, 2}

    def test_mark_held(self, store: Store, case, monkeypatch):
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [
                    {
                        "type": "ADD",
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
                        "type": "ADD",
                        "hearing_key": "sentencing-x",
                        "hearing_type": "sentencing",
                        "title": "Sentencing",
                        "local_date": "2026-04-14",
                        "local_time": "15:00",
                    }
                ],
                2: [
                    {
                        "type": "CANCEL",
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
                        "type": "ADD",
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
                    _entry(1, "RESPONDENT BRIEF filed by Peter B. Hegseth"),
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
                        "type": "ADD",
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

    # The CL-side date_modified for the docket — well past every
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
        # the docket cutoff must stay below the docket's CL-side
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
        # the CL-side date_modified. The next sync's docket-level
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
        # still bumps the cutoff all the way to the docket's CL-side
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
                        "type": "ADD",
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
    CANCEL actions onto a parallel proceeding's events in another venue.
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
            extract_deadlines=True,
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
        # is the LLM seeing the D.C. Cir. events and emitting CANCEL actions
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
            extract_deadlines=True,
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
                        "type": "ADD",
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
                        "type": "ADD",
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
                        "type": "ADD",
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
                        "type": "ADD",
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
                        "type": "CANCEL",
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
                1: [{"type": "CANCEL", "hearing_key": "status-conf-x-7"}],
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(case, 100, _entry(1, "ENDORSEMENT: hearing adjourned"))
        assert store.get_hearings("us-v-x") == []
        assert any(
            "CANCEL on unknown key with no local_date" in r.message
            for r in caplog.records
        )


class TestMarkHeldOnUnknownKey:
    """Held minute entry for a hearing whose scheduling never reached the
    store should ADD a new row in 'held' status."""

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
                        "type": "ADD",
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
                        "type": "ADD",
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
        # LLM sees a plea agreement / order vacating trial → CANCEL.
        self._seed_past_scheduled(store, key="trial-x", title="Jury Trial")
        stub_verify(
            monkeypatch,
            by_key={
                "trial-x": {
                    "type": "CANCEL",
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
                "future-trial": {"type": "CANCEL", "reason": "trial vacated by plea"},
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
        self._seed_future_hearing(store, key="hallucinated-conf")
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

    def test_reschedule_moves_starts_at_utc(self, store, case, monkeypatch):
        self._seed_future_hearing(store)
        stub_verify(
            monkeypatch,
            by_key={
                "future-trial": {
                    "type": "RESCHEDULE",
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
            extract_deadlines=True,
        )

    def test_add_deadline_creates_row_at_5pm_court_time(
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
        # 17:00 ET (no DST 5/24 — so 5pm EDT = 21:00 UTC) by default.
        assert d["due_at_utc"] == "2026-05-24T21:00:00+00:00"
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
        assert rows[0]["due_at_utc"] == "2026-06-14T21:00:00+00:00"
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
        # ADD then CANCEL on the same key — the existing row is merged
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
        # MARK_FILED on a key we never saw an ADD for is a benign log —
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
        # Date is unchanged from the ADD.
        assert rows[0]["due_at_utc"] == "2026-05-31T21:00:00+00:00"
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

    def test_criminal_docket_auto_detects_deadlines_off(
        self,
        store,
        monkeypatch,
    ):
        # Default behavior on a routine criminal docket: deadlines stay off
        # without any explicit config. The LLM call goes through (the entry
        # is hearing-relevant) but with extract_deadlines=False on the prompt
        # and known_deadlines unset.
        case_default = CaseConfig(
            case_id="us-v-y",
            name="United States v. Y",
            dockets=[100],
            calendar="cyber",
        )
        captured = {}

        def fake(*, known_deadlines=None, extract_deadlines=False, **_):
            captured["known_deadlines"] = known_deadlines
            captured["extract_deadlines"] = extract_deadlines
            return [{"type": "IGNORE", "reason": "stub"}]

        monkeypatch.setattr(llm_mod, "extract_actions", fake)

        cl = FakeCourtListener(dockets={100: _docket()})  # docket_number "1:25-cr-..."
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case_default,
            100,
            _entry(1, "Trial set for 6/1/2026"),  # hearing-relevant; reaches LLM
        )
        assert captured["extract_deadlines"] is False
        assert captured["known_deadlines"] is None

    def test_civil_docket_auto_detects_deadlines_on(self, store, monkeypatch):
        # Default config on a civil docket: deadlines auto-on, no override
        # needed. The LLM gets the deadline-aware prompt and known_deadlines
        # block (empty list, since none are stored yet).
        case_default = CaseConfig(
            case_id="acme-v-widget",
            name="Acme v. Widget",
            dockets=[100],
            calendar="tech",
        )
        captured = {}

        def fake(*, known_deadlines=None, extract_deadlines=False, **_):
            captured["known_deadlines"] = known_deadlines
            captured["extract_deadlines"] = extract_deadlines
            return [{"type": "IGNORE", "reason": "stub"}]

        monkeypatch.setattr(llm_mod, "extract_actions", fake)

        civil_docket = dict(_docket(), docket_number="1:25-cv-04567-AB")
        cl = FakeCourtListener(dockets={100: civil_docket})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case_default,
            100,
            _entry(1, "ORDER setting briefing schedule: response due by 5/24/2026"),
        )
        assert captured["extract_deadlines"] is True
        assert captured["known_deadlines"] == []

    def test_explicit_override_forces_deadlines_on_for_criminal_docket(
        self,
        store,
        monkeypatch,
    ):
        # The big-trial escape hatch: criminal docket number, but the case
        # opts in explicitly because pretrial motion practice is what's
        # being watched. The override beats the auto-detect.
        case_override = CaseConfig(
            case_id="us-v-z",
            name="United States v. Z",
            dockets=[100],
            calendar="cyber",
            extract_deadlines=True,
        )
        captured = {}

        def fake(*, known_deadlines=None, extract_deadlines=False, **_):
            captured["known_deadlines"] = known_deadlines
            captured["extract_deadlines"] = extract_deadlines
            return [{"type": "IGNORE", "reason": "stub"}]

        monkeypatch.setattr(llm_mod, "extract_actions", fake)

        cl = FakeCourtListener(dockets={100: _docket()})  # criminal docket_number
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case_override,
            100,
            _entry(1, "ORDER: response due by 5/24/2026"),
        )
        assert captured["extract_deadlines"] is True
        assert captured["known_deadlines"] == []


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
            extract_deadlines=True,
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
        assert d["due_at_utc"] == "2026-08-15T21:00:00+00:00"


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

    def test_merge_into_cancels_duplicates_and_combines_sources(
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
        # One row got cancelled.
        assert stats["deduped"] == 1
        # Both hearings were sent to the LLM as one cluster.
        keys_seen = {h["hearing_key"] for h in captured["cluster"]}
        assert keys_seen == {"msj-hearing", "motion-hearing-2"}
        # Target preserved, duplicate cancelled.
        rows = {h["hearing_key"]: h for h in store.get_hearings("us-v-x")}
        assert rows["msj-hearing"]["status"] == "scheduled"
        assert rows["motion-hearing-2"]["status"] == "cancelled"
        # source_entry_ids from the duplicate were merged into the target,
        # deduping against the target's existing list.
        assert rows["msj-hearing"]["source_entry_ids"] == [42, 43, 99]
        # The cancelled row carries a [dedupe] audit line pointing at the target.
        assert "[dedupe]" in (rows["motion-hearing-2"]["audit_notes"] or "")
        assert "msj-hearing" in (rows["motion-hearing-2"]["audit_notes"] or "")

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
        # The Akhter-shape OPEN-case scenario: two CL docket_ids in the
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
                "docket_id": 101,  # different CL docket_id, SAME group
                "source_entry_ids": [20],
            }
        )
        stub_verify(monkeypatch)
        captured = stub_dedupe(
            monkeypatch,
            action={
                "type": "MERGE_INTO",
                "target_key": "trial-x",
                "reason": "Cross-CL-sibling drift on the same PACER docket.",
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
        assert rows["trial-x-2"]["status"] == "cancelled"


class TestDedupeConcurrentHeldHearings:
    """End-of-sync deterministic merge for HELD rows that share the same
    logical PACER slot. A court physically can't hold two hearings
    simultaneously, so same-slot held clusters are unambiguous key-drift
    duplicates — no LLM call needed.

    Motivating case: didenko sentencing-didenko (from prior sync of one
    CL docket) vs sentencing-didenko-2 (from today's sync of a sibling
    CL docket with a different `pacer_case_id`) at the exact same UTC
    slot.
    """

    def _seed_cross_sibling_held_pair(self, store):
        # Two CL docket_ids in the same (docket_number, court_id) group,
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
        # Duplicate cancelled with [dedupe-held] audit note pointing
        # at the canonical key.
        assert rows["sentencing-didenko-2"]["status"] == "cancelled"
        notes = rows["sentencing-didenko-2"]["audit_notes"] or ""
        assert "[dedupe-held]" in notes
        assert "sentencing-didenko" in notes

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
        # source_entry_ids (common when both CL siblings cite the same
        # PACER minute-entry as the source for their respective held
        # rows), the merge must dedup those ids — emitting [10, 11, 99]
        # instead of [10, 11, 10, 99]. Exercises the "sid already in
        # seen, skip" path inside the per-cluster merge loop.
        # Use the same docket_number / court_id as `_docket()` so the
        # CL re-fetch during sync_case doesn't overwrite our seed under
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
            extract_deadlines=True,
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
                "reply-mtd": {"type": "CANCEL", "reason": "case dismissed"},
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
        self._seed_future_deadline(store)
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
                    "type": "RESCHEDULE",
                    "local_date": "2099-01-15",
                    "reason": "extension granted",
                },
            },
        )
        cl = FakeCourtListener(dockets={100: _docket()}, entries={100: []})
        stats = CaseSyncer(cl, store).sync_case(case)
        assert stats["deadlines_verified"] == 1
        d = store.get_deadlines("us-v-x")[0]
        # 5pm ET default for the deadline = 22:00 UTC (Jan 15 is EST, not EDT).
        assert d["due_at_utc"] == "2099-01-15T22:00:00+00:00"

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
                "reply-mtd": {"type": "RESCHEDULE", "reason": "no date provided"},
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
                "future-trial": {"type": "RESCHEDULE", "reason": "no date"},
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
    """Coverage for the CANCEL / MARK_HELD with-no-local_date drop paths
    and the deadline-action error paths."""

    def test_cancel_on_unknown_key_without_local_date_drops(
        self,
        store,
        case,
        monkeypatch,
    ):
        # CANCEL targeting a hearing_key the store doesn't have AND no
        # local_date to seed a new row → action is dropped with a warning.
        make_llm_stub(
            monkeypatch,
            by_entry={
                1: [{"type": "CANCEL", "hearing_key": "never-seen"}],
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
            extract_deadlines=True,
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
        # PACER docket key (docket_number, court_id), not on the CL
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
