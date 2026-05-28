"""Targeted coverage tests for sync.py.

These cover branches the broader integration suite doesn't reach: log lines
inside ``_maybe_fetch_pdfs`` for the various recap_document failure shapes,
the docket-refs branch of ``_resolve_docket_refs``, the
``_is_cross_court_mutation`` "either court_id unknown" fallthrough, the
``_verify_scheduled_hearings`` skip when a hearing has no docket_id, and the
remaining no-op branches inside ``process_entry`` (entries without
date_modified / date_filed, summary-relevant entry on a docket with missing
metadata, the docket-level short-circuit when the docket has no
``date_last_filing``).
"""

from __future__ import annotations

import pytest

from case_calendar import llm as llm_mod
from case_calendar.store import Store
from case_calendar.sync import CaseConfig, CaseSyncer

from .conftest import FakeCourtListener


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


def _entry(eid, desc, *, date_filed="2026-01-01", recap_documents=None):
    return {
        "id": eid,
        "docket": 100,
        "entry_number": eid,
        "date_filed": date_filed,
        "date_modified": f"{date_filed}T00:00:00-07:00",
        "description": desc,
        "short_description": "",
        "recap_documents": list(recap_documents or []),
    }


@pytest.fixture(autouse=True)
def _stub_verify(monkeypatch):
    """The verify pass is unrelated to these tests; stub it to CONFIRM."""

    def fake(*, hearing, **_):
        return {"type": "CONFIRM", "reason": "stub"}

    monkeypatch.setattr(llm_mod, "verify_hearing", fake)


def _stub_extract(monkeypatch, actions=None):
    actions = actions if actions is not None else []

    def fake(*, entry, **_):
        return actions

    monkeypatch.setattr(llm_mod, "extract_actions", fake)


class TestResolveDocketRefs:
    """``_resolve_docket_refs`` pulls cross-referenced entries from the
    store so the LLM gets context on the motion an order is acting on. The
    block that walks docket-position refs (``[65]`` -> entry 65) has its
    own dedup; both halves need coverage."""

    def test_docket_ref_pulls_stored_entry_into_referenced_block(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # Pre-stage entry 65 with a body so it's eligible to surface.
        store.upsert_docket_meta(100, _docket())
        store.mark_entry(
            100,
            10065,
            "2026-04-01T00:00:00-07:00",
            "fp65",
            entry_number=65,
            description="MOTION for Hearing on suppression",
        )

        captured: dict = {}

        def fake_extract(*, entry, referenced_entries, **_):
            captured["referenced"] = list(referenced_entries)
            return []

        monkeypatch.setattr(llm_mod, "extract_actions", fake_extract)

        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        # Entry text says "granting [65] Motion ..." which both passes the
        # hearing-relevance filter and matches the docket-ref regex.
        syncer.process_entry(
            case,
            100,
            _entry(7, "ORDER granting [65] Motion for Hearing"),
        )
        # The referenced block carries entry 65, sourced from the docket-ref
        # branch (rather than the recent-entries fallback).
        ref_numbers = [r.get("entry_number") for r in captured["referenced"]]
        assert 65 in ref_numbers

    def test_docket_ref_pointing_at_unknown_entry_is_skipped(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # Entry text references [99] but we never stored entry 99 — the
        # row lookup returns None and the docket-ref branch silently
        # skips it (no exception, no entry in referenced list).
        store.upsert_docket_meta(100, _docket())
        captured: dict = {}

        def fake_extract(*, entry, referenced_entries, **_):
            captured["referenced"] = list(referenced_entries)
            return []

        monkeypatch.setattr(llm_mod, "extract_actions", fake_extract)

        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case,
            100,
            _entry(7, "ORDER granting [99] Motion for Hearing"),
        )
        # Nothing came back for entry 99 — referenced list excludes it.
        ref_numbers = [r.get("entry_number") for r in captured["referenced"]]
        assert 99 not in ref_numbers

    def test_docket_ref_dedups_against_recent_entries(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # When the same entry is BOTH a docket-ref target AND recent enough
        # to land in get_recent_relevant_entries, it must appear once.
        store.upsert_docket_meta(100, _docket())
        # Recent hearing-relevant entry that will also be referenced as #65.
        store.mark_entry(
            100,
            10065,
            "2026-04-01T00:00:00-07:00",
            "fp65",
            entry_number=65,
            description="MOTION for Hearing on suppression",
        )

        captured: dict = {}

        def fake_extract(*, entry, referenced_entries, **_):
            captured["referenced"] = list(referenced_entries)
            return []

        monkeypatch.setattr(llm_mod, "extract_actions", fake_extract)

        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        syncer.process_entry(
            case,
            100,
            _entry(
                7,
                "ORDER granting [65] Motion for Hearing",
                date_filed="2026-04-02",
            ),
        )
        # Entry 65 must appear once even though both channels produced it.
        ids = [r.get("entry_id") for r in captured["referenced"]]
        assert ids.count(10065) == 1


class TestMaybeFetchPdfs:
    """``_maybe_fetch_pdfs`` short-circuits and log paths."""

    @pytest.fixture
    def syncer(self, store):
        store.upsert_docket_meta(100, _docket())
        cl = FakeCourtListener(dockets={100: _docket()})
        return CaseSyncer(cl, store)

    def test_all_paperless_short_circuits_with_debug(self, syncer, caplog):
        # Entry has recap_documents but none is fetchable (no body, no
        # filepath, not sealed-but-with-text) -> skip the whole PDF stage.
        entry = _entry(
            1,
            "Set/Reset Hearing",  # passes hearing-relevance + _needs_pdf
            recap_documents=[
                {
                    "id": 5,
                    "is_available": False,
                    "is_sealed": False,
                    "plain_text": "",
                    "filepath_local": None,
                    "filepath_ia": "",
                }
            ],
        )
        with caplog.at_level("DEBUG", logger="case_calendar.sync"):
            out = syncer._maybe_fetch_pdfs(entry)
        assert out == []
        assert any("none are fetchable" in r.message for r in caplog.records)

    def test_logs_unavailable_doc_when_extract_returns_empty(
        self, syncer, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            "case_calendar.sync.pdf.extract_text", lambda *_a, **_kw: ""
        )
        entry = _entry(
            1,
            "Set/Reset Hearing",
            recap_documents=[
                {
                    "id": 9,
                    "is_available": False,
                    "is_sealed": False,
                    "plain_text": "stub body",  # makes _is_fetchable True
                    "filepath_local": None,
                    "filepath_ia": "",
                }
            ],
        )
        with caplog.at_level("INFO", logger="case_calendar.sync"):
            out = syncer._maybe_fetch_pdfs(entry)
        assert out == []
        assert any("not yet on PACER" in r.message for r in caplog.records)

    def test_logs_available_doc_with_empty_extraction(
        self, syncer, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            "case_calendar.sync.pdf.extract_text", lambda *_a, **_kw: ""
        )
        entry = _entry(
            1,
            "Set/Reset Hearing",
            recap_documents=[
                {
                    "id": 11,
                    "is_available": True,
                    "is_sealed": False,
                    "plain_text": "",
                    "filepath_local": "recap/usc/foo.pdf",
                    "filepath_ia": "",
                }
            ],
        )
        with caplog.at_level("INFO", logger="case_calendar.sync"):
            out = syncer._maybe_fetch_pdfs(entry)
        assert out == []
        assert any("text extraction yielded" in r.message for r in caplog.records)

    def test_skips_recap_doc_without_id(self, syncer, monkeypatch):
        # A recap_document row missing its `id` is skipped silently; the
        # surrounding code shouldn't crash on it.
        called = []

        def fake_extract(rd, **_):
            called.append(rd.get("id"))
            return "body text"

        monkeypatch.setattr("case_calendar.sync.pdf.extract_text", fake_extract)
        entry = _entry(
            1,
            "Set/Reset Hearing",
            recap_documents=[
                {
                    "id": None,  # silently skipped
                    "is_available": True,
                    "is_sealed": False,
                    "plain_text": "",
                    "filepath_local": "recap/foo.pdf",
                    "filepath_ia": "",
                },
                {
                    "id": 22,
                    "is_available": True,
                    "is_sealed": False,
                    "plain_text": "",
                    "filepath_local": "recap/bar.pdf",
                    "filepath_ia": "",
                },
            ],
        )
        out = syncer._maybe_fetch_pdfs(entry)
        assert out == ["body text"]
        assert called == [22]

    def test_skips_single_unfetchable_inside_otherwise_fetchable_entry(
        self, syncer, monkeypatch
    ):
        # An entry has two recap_documents: one paperless, one real PDF.
        # The all-paperless short-circuit doesn't fire (one IS fetchable),
        # so the inner loop's single-doc skip branch runs.
        monkeypatch.setattr(
            "case_calendar.sync.pdf.extract_text",
            lambda rd, **_: f"body for {rd.get('id')}",
        )
        entry = _entry(
            1,
            "Set/Reset Hearing",
            recap_documents=[
                {
                    "id": 30,
                    "is_available": False,
                    "is_sealed": False,
                    "plain_text": "",
                    "filepath_local": None,
                    "filepath_ia": "",
                },
                {
                    "id": 31,
                    "is_available": True,
                    "is_sealed": False,
                    "plain_text": "",
                    "filepath_local": "recap/real.pdf",
                    "filepath_ia": "",
                },
            ],
        )
        out = syncer._maybe_fetch_pdfs(entry)
        assert out == ["body for 31"]


class TestIsCrossCourtMutation:
    """Returns ``None`` when either side's court metadata is missing — the
    caller treats that as "fall through and behave as before"."""

    def _syncer(self, store):
        cl = FakeCourtListener()
        return CaseSyncer(cl, store)

    def test_missing_existing_court_returns_none(self, store: Store):
        # existing hearing references a docket whose meta has no court_id
        # cached. Guard returns None and the action proceeds.
        store.upsert_docket_meta(
            100,
            {
                "court_id": "mad",
                "docket_number": "1:25-cr-1",
                "case_name": "X",
                "absolute_url": "/x/",
            },
        )
        # Docket 200 exists with no court_id (e.g. metadata only partially cached).
        store.upsert_docket_meta(
            200,
            {
                "court_id": "",
                "docket_number": "1:25-cr-2",
                "case_name": "Y",
                "absolute_url": "/y/",
            },
        )
        syncer = self._syncer(store)
        existing = {"docket_id": 200}
        assert syncer._is_cross_court_mutation(existing, 100) is None

    def test_missing_current_court_returns_none(self, store: Store):
        store.upsert_docket_meta(
            200,
            {
                "court_id": "mad",
                "docket_number": "1:25-cr-2",
                "case_name": "Y",
                "absolute_url": "/y/",
            },
        )
        store.upsert_docket_meta(
            100,
            {
                "court_id": "",
                "docket_number": "1:25-cr-1",
                "case_name": "X",
                "absolute_url": "/x/",
            },
        )
        syncer = self._syncer(store)
        existing = {"docket_id": 200}
        assert syncer._is_cross_court_mutation(existing, 100) is None


class TestVerifyScheduledHearingsSkipsDocketless:
    """A pre-docket_id-era row that lacks ``docket_id`` is unverifiable —
    we can't load the docket's recent entries — so the verify pass skips
    it without touching its status."""

    def test_skips_hearing_with_null_docket_id(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # Seed a future scheduled hearing with no docket_id.
        store.upsert_hearing(
            {
                "case_id": case.case_id,
                "hearing_key": "ghost",
                "title": "Ghost Hearing",
                "starts_at_utc": "2099-01-01T10:00:00+00:00",
                "duration_minutes": 30,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": None,
                "source_entry_ids": [],
            }
        )
        called = []

        def fake_verify(**kwargs):
            called.append(kwargs["hearing"].get("hearing_key"))
            return {"type": "CONFIRM", "reason": "stub"}

        monkeypatch.setattr(llm_mod, "verify_hearing", fake_verify)

        cl = FakeCourtListener()
        syncer = CaseSyncer(cl, store)
        # Run the verify pass directly — it should walk past the docket-less
        # row without calling the LLM.
        n_changed = syncer._verify_scheduled_hearings(case)
        assert n_changed == 0
        assert called == []


class TestProcessEntryNoOpBranches:
    """``process_entry`` has a handful of conditional bumps and a
    summary-stale flag that only fire when the entry / docket carry the
    required fields. Cover the "field absent => skip" branches."""

    def test_entry_without_date_modified_skips_last_modified_bump(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        store.upsert_docket_meta(100, _docket())
        _stub_extract(monkeypatch, actions=[])
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)

        calls: list[tuple[int, str]] = []
        orig = store.bump_docket_last_modified

        def spy(docket_id, candidate):
            calls.append((docket_id, candidate))
            return orig(docket_id, candidate)

        monkeypatch.setattr(store, "bump_docket_last_modified", spy)

        entry = _entry(1, "NOTICE of attorney appearance")  # filter-failed
        entry["date_modified"] = ""
        syncer.process_entry(case, 100, entry)
        # Bump was skipped because the entry carried no date_modified.
        assert calls == []

    def test_entry_without_date_filed_skips_last_filing_bump(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        store.upsert_docket_meta(100, _docket())
        _stub_extract(monkeypatch, actions=[])
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)

        calls: list[tuple[int, str]] = []
        orig = store.bump_docket_last_filing

        def spy(docket_id, candidate):
            calls.append((docket_id, candidate))
            return orig(docket_id, candidate)

        monkeypatch.setattr(store, "bump_docket_last_filing", spy)

        entry = _entry(1, "NOTICE of attorney appearance")
        entry["date_filed"] = ""
        syncer.process_entry(case, 100, entry)
        assert calls == []

    def test_summary_relevant_entry_without_docket_number_skips_stale_flag(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # The docket has court_id but no docket_number cached (rare cold
        # state). With one half of the key missing, mark_summary_stale
        # isn't called. We confirm via a spy that the bump didn't fire.
        partial = {
            "id": 100,
            "court_id": "mad",
            "docket_number": "",  # the field under test
            "case_name": "X",
            "absolute_url": "/x/",
            "date_modified": "2026-01-01T00:00:00-07:00",
        }
        store.upsert_docket_meta(100, partial)
        _stub_extract(monkeypatch, actions=[])
        cl = FakeCourtListener(dockets={100: partial})
        syncer = CaseSyncer(cl, store)

        calls: list[tuple] = []
        orig = store.mark_summary_stale

        def spy(case_id, docket_number, court_id):
            calls.append((case_id, docket_number, court_id))
            return orig(case_id, docket_number, court_id)

        monkeypatch.setattr(store, "mark_summary_stale", spy)

        # "INDICTMENT" head matches summary.is_primary_document.
        syncer.process_entry(case, 100, _entry(1, "INDICTMENT as to defendant"))
        assert calls == []


class TestSyncCaseDocketShortCircuit:
    """The polling path's per-docket short-circuit (docket unchanged since
    last sync) bumps date_last_filing along the way, but only if the
    docket's metadata carries one. Cover the "no date_last_filing" branch."""

    def test_short_circuit_skips_last_filing_bump_when_field_absent(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # Pre-seed the last-modified cutoff so the short-circuit fires.
        d = _docket()
        d["date_last_filing"] = ""  # the branch under test
        store.upsert_docket_meta(100, d)
        store.bump_docket_last_modified(100, d["date_modified"])
        cl = FakeCourtListener(dockets={100: d})
        _stub_extract(monkeypatch, actions=[])
        syncer = CaseSyncer(cl, store)
        stats = syncer.sync_case(case)
        assert stats["dockets_skipped"] == 1
        # No bump happened, so the date_last_filing column is empty.
        meta = store.get_docket_meta(100) or {}
        assert not meta.get("date_last_filing")


class TestMarkHeldUnknownKeyNoLocalDate:
    """The MARK_HELD-on-unknown-key path drops when no ``local_date`` is
    supplied. The existing test exercised the entry as filter-failed; we
    need the entry text to PASS the hearing-relevance filter so the LLM
    stub's action reaches _apply_action and the warning fires."""

    def test_warning_is_logged(
        self,
        store: Store,
        case,
        monkeypatch,
        caplog,
    ):
        _stub_extract(
            monkeypatch,
            actions=[{"type": "MARK_HELD", "hearing_key": "never-seen"}],
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        # "hearing" matches the hearing-relevance regex so the entry isn't
        # filtered out before _apply_action runs.
        with caplog.at_level("WARNING", logger="case_calendar.sync"):
            syncer.process_entry(
                case, 100, _entry(1, "MINUTE ORDER: hearing held off-record")
            )
        assert store.get_hearings("us-v-x") == []
        assert any(
            "MARK_HELD on unknown key with no local_date" in r.message
            for r in caplog.records
        )


class TestApplyDeadlineActionEntryAlreadyInSources:
    """``_apply_deadline_action`` builds ``prev_sources`` from the existing
    row's source list and appends the current entry. When the entry is
    already in that list (re-process of the same entry against the same
    deadline), the append is skipped — branch ``1677->1679``."""

    def test_reprocessing_same_entry_does_not_duplicate_source_id(
        self,
        store: Store,
        monkeypatch,
    ):
        case_c = CaseConfig(
            case_id="us-v-x",
            name="United States v. X",
            dockets=[100],
            calendar="cyber",
        )
        store.upsert_docket_meta(100, _docket())
        # Seed a pending deadline with source_entry_ids=[7].
        store.upsert_deadline(
            {
                "case_id": case_c.case_id,
                "deadline_key": "response",
                "title": "Response",
                "due_at_utc": "2026-05-24T22:00:00+00:00",
                "timezone": "America/New_York",
                "status": "pending",
                "significance": "major",
                "deadline_type": "response",
                "docket_id": 100,
                "source_entry_ids": [7],
            }
        )
        _stub_extract(
            monkeypatch,
            actions=[
                {
                    "type": "RESCHEDULE_DEADLINE",
                    "deadline_key": "response",
                    "local_date": "2026-05-31",
                }
            ],
        )
        cl = FakeCourtListener(dockets={100: _docket()})
        syncer = CaseSyncer(cl, store)
        # Re-process entry 7 (the very entry that's already in source_entry_ids).
        # Entry text passes the deadline-relevance regex via "due by".
        entry = _entry(7, "ORDER: response due by 5/31")
        syncer.process_entry(case_c, 100, entry)

        deadlines = store.get_deadlines(case_c.case_id)
        assert len(deadlines) == 1
        assert deadlines[0]["source_entry_ids"].count(7) == 1
