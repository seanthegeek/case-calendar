"""Tests for the per-docket case-summary pipeline (case_calendar/summary.py).

These tests don't hit CL, don't load PDFs, and don't call any real LLM —
``pdf.extract_text`` and ``llm.generate_docket_summary`` are monkeypatched,
and the CL client is replaced with ``_FakeCL`` whose ``_get`` returns
pre-canned ``/docket-entries/`` pages.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from case_calendar import summary
from case_calendar.summary import (
    find_operative_documents,
    is_disposition,
    is_operative_pleading,
    refresh_stale,
    summarize_case,
    summarize_docket,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _Case:
    """Minimal CaseConfig-shaped stand-in (avoids importing sync into tests
    that don't otherwise need it). The summary module duck-types these."""
    case_id: str
    name: str
    dockets: list[int]
    calendar: str = "test"
    extract_deadlines: bool = False


class _FakeResp:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeCL:
    """Records GETs and replays canned ``/docket-entries/`` pages.

    Pages are keyed by ``(docket_id, order_by)`` — `find_operative_documents`
    makes two such requests per docket (date_filed and -date_filed). Each
    page payload is the raw CL response shape: ``{"results": [...], "next": ...}``.
    """

    def __init__(self, pages: dict[tuple[int, str], list[dict[str, Any]]]):
        # Each value is the FULL list of entries for that (docket, order_by)
        # combination; we just shove them all on one page since the page_size
        # default is 50 and tests use a handful of entries.
        self._pages = pages
        self.calls: list[dict[str, Any]] = []

    def _get(self, url: str, params: dict[str, Any] | None = None) -> _FakeResp:
        self.calls.append({"url": url, "params": params})
        if params is None:
            # The summary code only passes params on the first page; the
            # `next` URL is None in our canned data so we never get here.
            return _FakeResp({"results": [], "next": None})
        docket_id = params["docket"]
        order_by = params["order_by"]
        entries = self._pages.get((docket_id, order_by), [])
        return _FakeResp({"results": entries, "next": None})


# ---------------------------------------------------------------------------
# Operative / disposition classifiers
# ---------------------------------------------------------------------------


class TestOperativeDetection:
    @pytest.mark.parametrize("description", [
        "INDICTMENT as to John Doe",
        "SUPERSEDING INDICTMENT (Count Three)",
        "SECOND AMENDED COMPLAINT for Damages",
        "INFORMATION",
        "Petition for Writ of Habeas Corpus",
        "COMPLAINT and Demand for Jury Trial",
    ])
    def test_matches_operative_pleadings(self, description):
        assert is_operative_pleading({"description": description})

    @pytest.mark.parametrize("description", [
        "Response to Motion to Dismiss the Indictment",
        "Notice of Appearance",
        "Order on Motion for Discovery",
        "",
    ])
    def test_rejects_non_operative(self, description):
        assert not is_operative_pleading({"description": description})

    def test_falls_back_to_short_description(self):
        entry = {"description": "", "short_description": "INDICTMENT"}
        assert is_operative_pleading(entry)

    def test_falls_back_to_recap_document_description(self):
        entry = {
            "description": "",
            "short_description": "",
            "recap_documents": [{"description": "INDICTMENT"}],
        }
        assert is_operative_pleading(entry)

    def test_empty_entry_returns_false(self):
        assert not is_operative_pleading({})


class TestDispositionDetection:
    @pytest.mark.parametrize("description", [
        "JUDGMENT in a Criminal Case",
        "FINAL JUDGMENT",
        "VERDICT FORM",
        "ORDER OF DISMISSAL",
        "STIPULATION OF DISMISSAL",
        "NOTICE OF VOLUNTARY DISMISSAL",
        "PLEA AGREEMENT",
        "MEMORANDUM OPINION and Order",
        "OPINION AND ORDER on Motion to Suppress",
    ])
    def test_matches_dispositions(self, description):
        assert is_disposition({"description": description})

    @pytest.mark.parametrize("description", [
        # Minute entries for sentencing hearings held — these don't anchor
        # at the start with JUDGMENT/SENTENCE, so the keyword regex is
        # what makes them count.
        "Minute Entry for proceedings held before Judge X: "
        "Sentencing held on 2/19/2026 as to OLEKSANDR DIDENKO (1). "
        "Imprisonment for a total term of 36 months...",
        "PAPERLESS Minute Entry for proceedings held before Judge Y: "
        "Sentencing held on 5/6/2026 as to ERICK PRINCE...",
        # Sentencing memoranda and continuances are sentencing-phase
        # signals worth refreshing on — the scheduled date moves and the
        # arguments about the term are exactly what changes "where does
        # the case stand".
        "PAPERLESS ORDER SETTING SENTENCING HEARING as to John Doe...",
        "PAPERLESS ORDER granting Unopposed Motion to Continue "
        "Sentencing Hearing as to John Doe...",
        "Government's Sentencing Memorandum",
        "Defendant's Sentencing Memorandum",
    ])
    def test_matches_sentencing_keyword(self, description):
        assert is_disposition({"description": description})

    @pytest.mark.parametrize("description", [
        # Any mention of judgment is treated as notable — Rule 50 motions,
        # judgments on the pleadings, amended judgments, judgment orders.
        "Motion for Judgment as a Matter of Law",
        "Motion for Judgment on the Pleadings",
        "ORDER denying Motion for Judgment as a Matter of Law",
        "Amended Judgment in a Criminal Case",
        "Notice of Filing of Judgments rendered against codefendants",
        # British spelling, in case it ever shows up.
        "Memorandum supporting Judgement on the Pleadings",
    ])
    def test_matches_judgment_keyword(self, description):
        assert is_disposition({"description": description})

    @pytest.mark.parametrize("description", [
        # TRO — acronym or spelled out, granted, denied, or sought.
        "Motion for TRO",
        "Motion for Temporary Restraining Order",
        "ORDER granting Plaintiff's Motion for TRO",
        "ORDER denying Motion for Temporary Restraining Order",
        "Memorandum in opposition to Motion for TRO",
        # Injunctions — preliminary, permanent, or unqualified.
        "Motion for Preliminary Injunction",
        "ORDER granting Motion for Preliminary Injunction",
        "ORDER denying Motion for Permanent Injunction",
        "Stipulated Injunction and Agreed Order",
        "Plaintiff's response to Motion for Injunction Pending Appeal",
    ])
    def test_matches_tro_and_injunction_keywords(self, description):
        assert is_disposition({"description": description})

    @pytest.mark.parametrize("description", [
        # Criminal — verdict-phase and post-trial events.
        "ORDER declaring mistrial sua sponte",
        "Verdict of Acquittal returned by jury",
        "ORDER granting Rule 29 Motion; defendant acquitted on Count 2",
        "Preliminary Order of Forfeiture as to defendant",
        "Final Order of Forfeiture",
        "Nolle Prosequi as to Count 3",
        "Notice of Nolle Prossed counts",
        # Civil — class certification, removal, default.
        "ORDER granting Motion for Class Certification",
        "ORDER denying class certification",
        "ORDER of REMAND to State Court",
        "Case remanded to Superior Court of California",
        "Entry of Default as to John Doe",
        # Cross-domain — dismissal and appellate dispositions.
        "ORDER granting Motion to Dismiss; case dismissed with prejudice",
        "Notice of voluntary dismissal under Rule 41",
        "MANDATE of the Court of Appeals issued",
        "Mandates received from the D.C. Circuit",
        "Judgment of the Court of Appeals: AFFIRMED",
        "Per curiam opinion: affirmance of district court judgment",
        "Order of Reversed and Remanded",
        "ORDER vacated and remanded for further proceedings",
    ])
    def test_matches_extended_disposition_keywords(self, description):
        assert is_disposition({"description": description})

    @pytest.mark.parametrize("description", [
        # Conference is the negative keyword — scheduling entries that
        # mention disposition vocabulary must NOT trip the keyword match.
        "Notice of Settlement Conference",
        "ORDER setting Telephonic Status Conference re: Sentencing",
        "Final Pretrial Conference held; further conference set",
        "ORDER scheduling Status Conference on Motion for Preliminary "
        "Injunction",
    ])
    def test_conference_overrides_disposition_match(self, description):
        assert not is_disposition({"description": description})

    @pytest.mark.parametrize("description", [
        "Notice of Filing of Plea Agreement Reply",
        "Reply in support of Motion to Dismiss",
        # No keyword anywhere — stay un-flagged.
        "Joint Status Report regarding discovery",
        "ORDER granting Motion to Compel Production",
    ])
    def test_rejects_non_dispositions(self, description):
        assert not is_disposition({"description": description})


# ---------------------------------------------------------------------------
# find_operative_documents
# ---------------------------------------------------------------------------


class TestFindOperativeDocuments:
    def test_returns_operative_and_disposition_lists(self):
        cl = _FakeCL({
            (1, "date_filed"): [
                {"id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01"},
                {"id": 11, "description": "Motion to Dismiss", "date_filed": "2024-02-01"},
            ],
            (1, "-date_filed"): [
                {"id": 99, "description": "JUDGMENT in a Criminal Case", "date_filed": "2025-06-15"},
            ],
        })
        operative, dispositions = find_operative_documents(cl, 1)
        assert [e["id"] for e in operative] == [10]
        assert [e["id"] for e in dispositions] == [99]

    def test_dedups_overlap_between_oldest_and_newest_pages(self):
        # Same entry appearing in both order_bys is folded to one row.
        same = {"id": 10, "description": "COMPLAINT", "date_filed": "2024-01-01"}
        cl = _FakeCL({
            (1, "date_filed"): [same],
            (1, "-date_filed"): [same],
        })
        operative, _ = find_operative_documents(cl, 1)
        assert [e["id"] for e in operative] == [10]

    def test_sorts_oldest_first_within_each_group(self):
        cl = _FakeCL({
            (1, "date_filed"): [
                {"id": 20, "description": "SUPERSEDING INDICTMENT", "date_filed": "2024-06-01"},
                {"id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01"},
            ],
            (1, "-date_filed"): [],
        })
        operative, _ = find_operative_documents(cl, 1)
        assert [e["id"] for e in operative] == [10, 20]


# ---------------------------------------------------------------------------
# _attach_text / _entry_doc_text behavior, indirectly via summarize_docket
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_llm(monkeypatch):
    """Replace ``llm.generate_docket_summary`` with a recording stub."""
    calls: list[dict[str, Any]] = []

    def _fake(**kwargs):
        calls.append(kwargs)
        return ("A two-sentence summary of the matter.", "fake/model-v1")

    monkeypatch.setattr(summary.llm, "generate_docket_summary", _fake)
    return calls


@pytest.fixture
def patch_pdf(monkeypatch):
    """Replace ``pdf.extract_text`` with a deterministic stub.

    Routes by ``recap_doc['id']``: any id present in the mapping returns its
    text; anything else returns ''. Tests pass `texts={rd_id: "..."}`.
    """
    state = {"texts": {}}

    def _fake(rd, *, allow_ocr=True):
        return state["texts"].get(rd.get("id"), "")

    monkeypatch.setattr(summary.pdf, "extract_text", _fake)
    return state


def _seed_docket_meta(store, docket_id, *, court_id="dcd", docket_number="1:24-cr-100"):
    """Populate the docket/court rows the summary code reads."""
    store.upsert_docket_meta(docket_id, {
        "docket_number": docket_number,
        "case_name": "United States v. Doe",
        "court_id": court_id,
        "absolute_url": f"/docket/{docket_id}/foo/",
    })
    store.upsert_court(court_id, "D.D.C.", "DDC", "U.S. District Court for the District of Columbia")


class TestSummarizeDocket:
    def test_writes_summary_when_operative_text_available(self, store, patch_llm, patch_pdf):
        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {500: "INDICTMENT body text..."}
        cl = _FakeCL({
            (1, "date_filed"): [{
                "id": 10,
                "description": "INDICTMENT",
                "date_filed": "2024-01-01",
                "entry_number": 1,
                "recap_documents": [{"id": 500}],
            }],
            (1, "-date_filed"): [],
        })
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")

        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)

        assert row is not None
        assert row["summary"] == "A two-sentence summary of the matter."
        assert row["model"] == "fake/model-v1"
        # Persisted to the store.
        persisted = store.get_docket_summary("us-v-doe", 1)
        assert persisted["summary"] == row["summary"]
        # LLM received the expected scaffold.
        assert len(patch_llm) == 1
        call = patch_llm[0]
        assert call["case_name"] == "US v. Doe"
        assert call["docket"]["court_citation"] == "D.D.C."
        assert call["docket"]["court_tz"] is not None
        assert [d["entry_id"] for d in call["operative_docs"]] == [10]

    def test_returns_none_when_no_operative_text_extractable(self, store, patch_llm, patch_pdf):
        _seed_docket_meta(store, 1)
        # Operative pleading present but PDF text is empty for every doc.
        patch_pdf["texts"] = {}
        cl = _FakeCL({
            (1, "date_filed"): [{
                "id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01",
                "recap_documents": [{"id": 500}],
            }],
            (1, "-date_filed"): [],
        })
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")

        assert summarize_docket(cl=cl, store=store, case=case, docket_id=1) is None
        assert patch_llm == []
        assert store.get_docket_summary("us-v-doe", 1) is None

    def test_borrows_from_sibling_when_primary_has_no_operative(
        self, store, patch_llm, patch_pdf,
    ):
        # Primary docket 1 has no operative pleading (appellate-style).
        # Sibling docket 2 has the indictment.
        _seed_docket_meta(store, 1, docket_number="24-1234", court_id="ca9")
        _seed_docket_meta(store, 2, docket_number="1:24-cr-100", court_id="dcd")
        patch_pdf["texts"] = {500: "INDICTMENT body text..."}
        cl = _FakeCL({
            (1, "date_filed"): [],
            (1, "-date_filed"): [],
            (2, "date_filed"): [{
                "id": 20,
                "description": "INDICTMENT",
                "date_filed": "2024-01-01",
                "recap_documents": [{"id": 500}],
            }],
            (2, "-date_filed"): [],
        })
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1, 2], calendar="cyber")

        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)

        assert row is not None
        # The borrowed-from label was appended to the description.
        operative_docs = patch_llm[0]["operative_docs"]
        assert operative_docs[0]["description"].endswith("[from sibling 1:24-cr-100 D.D.C.]")

    def test_borrowing_swallows_sibling_failure_and_continues(
        self, store, patch_llm, patch_pdf, caplog,
    ):
        _seed_docket_meta(store, 1, docket_number="24-1234", court_id="ca9")
        _seed_docket_meta(store, 2, docket_number="1:24-cr-100", court_id="dcd")
        _seed_docket_meta(store, 3, docket_number="1:24-cr-200", court_id="dcd")
        patch_pdf["texts"] = {600: "INDICTMENT body..."}

        # Sibling 2 raises on its docket-entries call; sibling 3 succeeds.
        original_get = _FakeCL._get

        def _flaky_get(self, url, params=None):
            if params and params.get("docket") == 2:
                raise RuntimeError("transient CL outage")
            return original_get(self, url, params)

        cl = _FakeCL({
            (1, "date_filed"): [],
            (1, "-date_filed"): [],
            (3, "date_filed"): [{
                "id": 30, "description": "INDICTMENT",
                "date_filed": "2024-01-01",
                "recap_documents": [{"id": 600}],
            }],
            (3, "-date_filed"): [],
        })
        cl._get = _flaky_get.__get__(cl, _FakeCL)
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1, 2, 3], calendar="cyber")

        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None

    def test_returns_none_when_no_sibling_has_operative(
        self, store, patch_llm, patch_pdf,
    ):
        _seed_docket_meta(store, 1, docket_number="24-1", court_id="ca9")
        _seed_docket_meta(store, 2, docket_number="24-2", court_id="ca9")
        cl = _FakeCL({
            (1, "date_filed"): [],
            (1, "-date_filed"): [],
            (2, "date_filed"): [],
            (2, "-date_filed"): [],
        })
        case = _Case(case_id="case-x", name="X", dockets=[1, 2], calendar="cyber")

        assert summarize_docket(cl=cl, store=store, case=case, docket_id=1) is None
        assert patch_llm == []

    def test_attaches_dispositions_when_present(self, store, patch_llm, patch_pdf):
        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {500: "INDICTMENT body", 600: "JUDGMENT body"}
        cl = _FakeCL({
            (1, "date_filed"): [{
                "id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01",
                "recap_documents": [{"id": 500}],
            }],
            (1, "-date_filed"): [{
                "id": 99, "description": "JUDGMENT in a Criminal Case",
                "date_filed": "2025-06-15",
                "recap_documents": [{"id": 600}],
            }],
        })
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")
        summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert [d["entry_id"] for d in patch_llm[0]["disposition_docs"]] == [99]

    def test_paperless_disposition_falls_back_to_description(
        self, store, patch_llm, patch_pdf,
    ):
        # "Electronic Clerk's Notes" for a sentencing held in court carries
        # the full imposed sentence in the docket text — no PDF is ever
        # attached. The summary pipeline must still feed this text to the
        # LLM so the resulting prose can name the actual sentence imposed.
        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {500: "INDICTMENT body"}
        clerk_notes = (
            "Electronic Clerk's Notes for proceedings held before Judge X: "
            "Sentencing held. Court imposes sentence: 92 months imprisonment, "
            "3 years Supervised Release; $200 Special Assessment."
        )
        cl = _FakeCL({
            (1, "date_filed"): [{
                "id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01",
                "recap_documents": [{"id": 500}],
            }],
            (1, "-date_filed"): [{
                "id": 99, "description": clerk_notes,
                "date_filed": "2026-04-15",
                "entry_number": 37,
                "recap_documents": [],  # paperless — no attachments
            }],
        })
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")
        summarize_docket(cl=cl, store=store, case=case, docket_id=1)

        dispositions = patch_llm[0]["disposition_docs"]
        assert [d["entry_id"] for d in dispositions] == [99]
        # The sentence figures must reach the LLM — that's the whole point.
        assert "92 months imprisonment" in dispositions[0]["text"]

    def test_operative_pleading_without_pdf_is_still_dropped(
        self, store, patch_llm, patch_pdf,
    ):
        # By design the description fallback is scoped to dispositions only.
        # Operative pleadings are indictments / complaints — a clerk's
        # minute-entry stub isn't an acceptable substitute, and feeding one
        # in would produce a vacuous summary. Confirm the asymmetry.
        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {}  # no PDF text extracts
        cl = _FakeCL({
            (1, "date_filed"): [{
                "id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01",
                "recap_documents": [{"id": 500}],
            }],
            (1, "-date_filed"): [],
        })
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")
        assert summarize_docket(cl=cl, store=store, case=case, docket_id=1) is None
        assert patch_llm == []

    def test_falls_back_to_attachments_when_main_doc_has_no_text(
        self, store, patch_llm, patch_pdf,
    ):
        _seed_docket_meta(store, 1)
        # Main doc (id=500, no attachment_number) extracts to empty;
        # attachment (id=501) has text. The helper should fall through.
        patch_pdf["texts"] = {501: "INDICTMENT body via attachment"}
        cl = _FakeCL({
            (1, "date_filed"): [{
                "id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01",
                "recap_documents": [
                    {"id": 500},
                    {"id": 501, "attachment_number": 1},
                ],
            }],
            (1, "-date_filed"): [],
        })
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")
        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None


# ---------------------------------------------------------------------------
# refresh_stale + summarize_case
# ---------------------------------------------------------------------------


class TestRefreshStale:
    def test_skips_dockets_that_are_not_stale(self, store, patch_llm, patch_pdf):
        _seed_docket_meta(store, 1)
        # Pre-seed a fresh (non-stale) summary row.
        store.upsert_case_summary(
            "us-v-doe", 1, summary="existing", model="prev/model",
            source_entry_ids=[],
        )
        cl = _FakeCL({})  # never queried
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")

        written = refresh_stale(cl=cl, store=store, cases=[case])

        assert written == {}
        assert patch_llm == []

    def test_regenerates_when_missing(self, store, patch_llm, patch_pdf):
        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {500: "INDICTMENT body"}
        cl = _FakeCL({
            (1, "date_filed"): [{
                "id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01",
                "recap_documents": [{"id": 500}],
            }],
            (1, "-date_filed"): [],
        })
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")
        written = refresh_stale(cl=cl, store=store, cases=[case])
        assert written == {"us-v-doe": {1}}

    def test_regenerates_when_stale_flag_set(self, store, patch_llm, patch_pdf):
        _seed_docket_meta(store, 1)
        store.upsert_case_summary(
            "us-v-doe", 1, summary="old", model="prev/model", source_entry_ids=[],
        )
        store.mark_summary_stale("us-v-doe", 1)
        patch_pdf["texts"] = {500: "INDICTMENT body"}
        cl = _FakeCL({
            (1, "date_filed"): [{
                "id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01",
                "recap_documents": [{"id": 500}],
            }],
            (1, "-date_filed"): [],
        })
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")
        written = refresh_stale(cl=cl, store=store, cases=[case])
        assert written == {"us-v-doe": {1}}

    def test_only_case_ids_scopes_the_walk(self, store, patch_llm, patch_pdf):
        _seed_docket_meta(store, 1)
        _seed_docket_meta(store, 2)
        patch_pdf["texts"] = {500: "INDICTMENT"}
        cl = _FakeCL({
            (1, "date_filed"): [{
                "id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01",
                "recap_documents": [{"id": 500}],
            }],
            (1, "-date_filed"): [],
        })
        case_a = _Case(case_id="a", name="A", dockets=[1], calendar="cyber")
        case_b = _Case(case_id="b", name="B", dockets=[2], calendar="cyber")
        written = refresh_stale(
            cl=cl, store=store, cases=[case_a, case_b], only_case_ids={"a"},
        )
        assert written == {"a": {1}}

    def test_uses_aggregation_note_override(self, store, patch_llm, patch_pdf):
        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {500: "INDICTMENT body"}
        cl = _FakeCL({
            (1, "date_filed"): [{
                "id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01",
                "recap_documents": [{"id": 500}],
            }],
            (1, "-date_filed"): [],
        })
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")
        refresh_stale(
            cl=cl, store=store, cases=[case],
            case_overrides={"us-v-doe": {"aggregation_note": "Parallel district + appellate."}},
        )
        assert patch_llm[0]["aggregation_note"] == "Parallel district + appellate."


class TestSummarizeCase:
    def test_force_overwrites_existing_summary(self, store, patch_llm, patch_pdf):
        _seed_docket_meta(store, 1)
        store.upsert_case_summary(
            "us-v-doe", 1, summary="old", model="prev/model", source_entry_ids=[],
        )
        patch_pdf["texts"] = {500: "INDICTMENT body"}
        cl = _FakeCL({
            (1, "date_filed"): [{
                "id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01",
                "recap_documents": [{"id": 500}],
            }],
            (1, "-date_filed"): [],
        })
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")
        rows = summarize_case(cl=cl, store=store, case=case, force=True)
        assert [r["docket_id"] for r in rows] == [1]
        assert store.get_docket_summary("us-v-doe", 1)["summary"] == \
            "A two-sentence summary of the matter."

    def test_default_skips_when_summary_already_present(
        self, store, patch_llm, patch_pdf,
    ):
        _seed_docket_meta(store, 1)
        store.upsert_case_summary(
            "us-v-doe", 1, summary="existing", model="prev/model",
            source_entry_ids=[],
        )
        cl = _FakeCL({})
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")
        rows = summarize_case(cl=cl, store=store, case=case)
        assert rows == []
        assert patch_llm == []
