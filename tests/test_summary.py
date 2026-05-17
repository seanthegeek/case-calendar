"""Tests for the per-docket case-summary pipeline (case_calendar/summary.py).

These tests don't hit CourtListener, don't load PDFs, and don't call any real LLM —
``pdf.extract_text`` and ``llm.generate_docket_summary`` are monkeypatched,
and the CourtListener client is replaced with ``_FakeCourtListener`` whose ``_get`` returns
pre-canned ``/docket-entries/`` pages.
"""

from __future__ import annotations

from typing import Any

import pytest

from case_calendar import summary
from case_calendar.courtlistener import CourtListener
from case_calendar.summary import (
    _is_disposition_document,
    find_primary_documents,
    is_disposition,
    is_primary_document,
    refresh_stale,
    summarize_case,
    summarize_docket,
)
from case_calendar.sync import CaseConfig as _Case, ExtraDocument


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _BoomCourtListenerBase(CourtListener):
    """Base for one-off CourtListener stubs whose `_get` raises.

    Used by tests that prove a short-circuit path never reaches the
    network. Subclasses just override `_get` with the assertion they
    want to enforce.
    """

    def __init__(self) -> None:
        # Deliberately skip the real `__init__` (no httpx client, no token).
        pass


class _FakeResp:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeCourtListener(CourtListener):
    """Records GETs and replays canned ``/docket-entries/`` pages.

    Pages are keyed by ``(docket_id, order_by)`` — `find_primary_documents`
    makes two such requests per docket (date_filed and -date_filed). Each
    page payload is the raw CourtListener response shape: ``{"results": [...], "next": ...}``.

    Subclasses the real client so it's accepted wherever a `CourtListener`
    is expected, but skips the real `__init__` (no httpx, no token).
    """

    def __init__(self, pages: dict[tuple[int, str], list[dict[str, Any]]]):
        # Each value is the FULL list of entries for that (docket, order_by)
        # combination; we just shove them all on one page since the page_size
        # default is 50 and tests use a handful of entries.
        self._pages = pages
        self.calls: list[dict[str, Any]] = []

    def _get(self, url: str, params: dict[str, Any] | None = None) -> _FakeResp:  # type: ignore[override]
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
# Primary-document / disposition classifiers
# ---------------------------------------------------------------------------


class TestPrimaryDocumentDetection:
    @pytest.mark.parametrize("description", [
        "INDICTMENT as to John Doe",
        "SUPERSEDING INDICTMENT (Count Three)",
        "SECOND AMENDED COMPLAINT for Damages",
        "INFORMATION",
        "Petition for Writ of Habeas Corpus",
        "COMPLAINT and Demand for Jury Trial",
    ])
    def test_matches_primary_documents(self, description):
        assert is_primary_document({"description": description})

    @pytest.mark.parametrize("description", [
        "Response to Motion to Dismiss the Indictment",
        "Notice of Appearance",
        "Order on Motion for Discovery",
        "",
    ])
    def test_rejects_non_primary(self, description):
        assert not is_primary_document({"description": description})

    def test_falls_back_to_short_description(self):
        entry = {"description": "", "short_description": "INDICTMENT"}
        assert is_primary_document(entry)

    def test_falls_back_to_recap_document_description(self):
        entry = {
            "description": "",
            "short_description": "",
            "recap_documents": [{"description": "INDICTMENT"}],
        }
        assert is_primary_document(entry)

    def test_empty_entry_returns_false(self):
        assert not is_primary_document({})


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


class TestDispositionDocumentDetection:
    """The stricter sibling of ``is_disposition`` used inside
    ``find_primary_documents`` to pick which documents reach the LLM.

    ``is_disposition`` is broad on purpose (motion-on-disposition still
    flips the case_summaries row stale). The stricter predicate must
    keep the actual orders / judgments / minute-entries-of-decision but
    reject the motions, briefs, and notices that surround them — those
    are not the disposition document itself, and feeding their text to
    the summary LLM as if they were causes the case-summary regression
    that shipped this fix.
    """

    @pytest.mark.parametrize("description", [
        # The exact pair that failed in Anthropic v. DoW: an "ORDER
        # GRANTING MOTION FOR PRELIMINARY INJUNCTION" and a separately
        # docketed "PRELIMINARY INJUNCTION ORDER" — both must reach the
        # LLM document set so the summary can mention that the
        # injunction was issued.
        "ORDER GRANTING MOTION FOR PRELIMINARY INJUNCTION 6",
        "PRELIMINARY INJUNCTION ORDER. Signed by Judge Lin on 3/26/2026.",
        # Head-anchored disposition phrases — accepted regardless.
        "JUDGMENT in a Criminal Case",
        "FINAL JUDGMENT",
        "VERDICT FORM",
        "ORDER OF DISMISSAL",
        "STIPULATION OF DISMISSAL",
        "NOTICE OF VOLUNTARY DISMISSAL",
        "PLEA AGREEMENT",
        "MEMORANDUM OPINION and Order",
        # Order-class entries that carry disposition vocabulary.
        "ORDER granting Motion for TRO",
        "ORDER denying Motion for Permanent Injunction",
        "ORDER granting Motion for Class Certification",
        "ORDER of REMAND to State Court",
        "ORDER declaring mistrial sua sponte",
        "Preliminary Order of Forfeiture as to defendant",
        "Final Order of Forfeiture",
        # Minute entries that are themselves the disposition.
        "Minute Entry for proceedings held before Judge X: "
        "Sentencing held on 2/19/2026 as to OLEKSANDR DIDENKO (1). "
        "Imprisonment for a total term of 36 months...",
        "PAPERLESS Minute Entry for proceedings held before Judge Y: "
        "Sentencing held on 5/6/2026 as to ERICK PRINCE...",
    ])
    def test_accepts_actual_disposition_documents(self, description):
        assert _is_disposition_document({"description": description})

    @pytest.mark.parametrize("description", [
        # Motions / requests — these are PAPERS, not the disposition.
        # The summary LLM must not see these in the disposition slot.
        "MOTION for Temporary Restraining Order, MOTION for "
        "Preliminary Injunction, MOTION to Stay Pursuant to Section 705 "
        "filed by Anthropic PBC",
        "Motion for TRO",
        "Motion for Preliminary Injunction",
        "Motion for Permanent Injunction",
        "Motion for Judgment as a Matter of Law",
        "Motion for Judgment on the Pleadings",
        "Motion for Class Certification",
        "ADMINISTRATIVE MOTION for Leave to File Amicus Brief in "
        "Support of Preliminary Injunction",
        # Briefs / responses / status reports — same idea.
        "Memorandum in opposition to Motion for TRO",
        "Memorandum supporting Judgement on the Pleadings",
        "Plaintiff's response to Motion for Injunction Pending Appeal",
        "Government's Sentencing Memorandum",
        "Defendant's Sentencing Memorandum",
        "Reply in support of Motion to Dismiss",
        "Joint Status Report regarding discovery",
        # Notices of filing / appearance — not the disposition either.
        "NOTICE of Appearance filed by Celine Georges Purcell",
        "Notice of Filing of Judgments rendered against codefendants",
        # ORDER but no disposition vocabulary — discovery / procedural.
        "ORDER granting Motion to Compel Production",
        "ORDER. By April 21, 2026, the parties shall submit a joint "
        "stipulation and proposed order setting a case schedule.",
        # Conference negative still wins.
        "ORDER scheduling Status Conference on Motion for Preliminary "
        "Injunction",
        # Minute entries of MOTION HEARINGS — these contain disposition
        # vocabulary in passing ("Motion Hearing re: 6 Motion for
        # Preliminary Injunction held on 3/24/2026") but the disposition
        # itself comes from a SEPARATE order issued days later. Feeding
        # the minute-entry text as if it were the ruling produces summary
        # prose like "PI taken under submission" forever — which is the
        # Anthropic v. DoW regression that motivated this filter.
        "Minute Entry for proceedings held before Judge Rita F. Lin:"
        "Motion Hearing re: 6 Motion for Preliminary Injunction held "
        "on 3/24/2026. Parties stated appearances and proffered "
        "argument. Court took the matter under submission.",
        # Case-schedule orders that reference upcoming dispositive
        # motions in passing.
        "ORDER RE 149 STIPULATION. Signed by Judge Rita F. Lin on "
        "4/23/2026. The following deadlines were ordered: "
        "Defendants' Answer due 6/8/2026. Anthropic's Motion for "
        "Summary Judgment due 6/10/2026.",
        # Scheduling orders that set future dispositive proceedings.
        "PAPERLESS ORDER SETTING SENTENCING HEARING as to John Doe...",
        "ORDER SETTING CASE SCHEDULE",
        "ORDER SETTING BRIEFING SCHEDULE on Motion for Summary "
        "Judgment",
        # Continuance orders / motions.
        "PAPERLESS ORDER granting Unopposed Motion to Continue "
        "Sentencing Hearing as to John Doe",
        "MOTION to Continue Trial Date",
        "",
    ])
    def test_rejects_papers_and_non_dispositions(self, description):
        assert not _is_disposition_document({"description": description})


# ---------------------------------------------------------------------------
# find_primary_documents
# ---------------------------------------------------------------------------


class TestFindPrimaryDocuments:
    def test_returns_primary_and_disposition_lists(self):
        cl = _FakeCourtListener({
            (1, "date_filed"): [
                {"id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01"},
                {"id": 11, "description": "Motion to Dismiss", "date_filed": "2024-02-01"},
            ],
            (1, "-date_filed"): [
                {"id": 99, "description": "JUDGMENT in a Criminal Case", "date_filed": "2025-06-15"},
            ],
        })
        primary, dispositions = find_primary_documents(cl, 1)
        assert [e["id"] for e in primary] == [10]
        assert [e["id"] for e in dispositions] == [99]

    def test_dedups_overlap_between_oldest_and_newest_pages(self):
        # Same entry appearing in both order_bys is folded to one row.
        same = {"id": 10, "description": "COMPLAINT", "date_filed": "2024-01-01"}
        cl = _FakeCourtListener({
            (1, "date_filed"): [same],
            (1, "-date_filed"): [same],
        })
        primary, _ = find_primary_documents(cl, 1)
        assert [e["id"] for e in primary] == [10]

    def test_sorts_oldest_first_within_each_group(self):
        cl = _FakeCourtListener({
            (1, "date_filed"): [
                {"id": 20, "description": "SUPERSEDING INDICTMENT", "date_filed": "2024-06-01"},
                {"id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01"},
            ],
            (1, "-date_filed"): [],
        })
        primary, _ = find_primary_documents(cl, 1)
        assert [e["id"] for e in primary] == [10, 20]

    def test_local_store_short_circuits_cl_call(self, store):
        # Warm cache: sync has already persisted primary + disposition
        # entries on this docket. find_primary_documents must read them
        # from the store and never touch CourtListener — otherwise normal syncs burn
        # duplicate docket-entries calls right after sync wrote the data.
        store.mark_entry(
            1, 10, "2024-01-01T00:00:00Z", "fp-op", date_filed="2024-01-01",
            entry_number=1, description="INDICTMENT",
            recap_documents=[{"id": 500, "plain_text": "indictment body"}],
        )
        store.mark_entry(
            1, 99, "2025-06-15T00:00:00Z", "fp-disp", date_filed="2025-06-15",
            entry_number=37, description="JUDGMENT in a Criminal Case",
            recap_documents=[{"id": 600, "plain_text": "judgment body"}],
        )
        # CourtListener is wired to raise if called — proves the short-circuit hit.
        class _BoomCourtListener(_BoomCourtListenerBase):
            def _get(self, *a, **kw):
                raise AssertionError("CourtListener must not be called when local cache is warm")
        primary, dispositions = find_primary_documents(_BoomCourtListener(), 1, store=store)
        assert [e["id"] for e in primary] == [10]
        assert [e["id"] for e in dispositions] == [99]
        # Recap document payload (with plain_text) is preserved end-to-end
        # so pdf.extract_text can short-circuit on it.
        assert primary[0]["recap_documents"][0]["plain_text"] == "indictment body"

    def test_cold_local_store_falls_back_to_cl(self, store):
        # No body-bearing entries cached — fall back to CourtListener (first sync,
        # or pre-fix data where primary/disp entries were stub-only).
        cl = _FakeCourtListener({
            (1, "date_filed"): [
                {"id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01"},
            ],
            (1, "-date_filed"): [
                {"id": 99, "description": "JUDGMENT in a Criminal Case",
                 "date_filed": "2025-06-15"},
            ],
        })
        primary, dispositions = find_primary_documents(cl, 1, store=store)
        assert [e["id"] for e in primary] == [10]
        assert [e["id"] for e in dispositions] == [99]

    def test_disposition_only_cache_does_not_short_circuit(self, store):
        # us-v-chapman / us-v-mcgonigal shape: pre-fix sync stored the
        # INDICTMENT as a NULL-description stub (filter-failed under the
        # old logic that didn't persist primary/disp bodies), while later
        # dispositive orders were processed under the post-fix logic and
        # ARE body-bearing. The local cache thus has dispositions but no
        # primary document. The old short-circuit (`if primary_list or
        # dispositions`) returned the disposition list with `primary=[]`,
        # `summarize_docket` then bailed with "no primary document text
        # could be extracted" and the summary went stale. The cache hit
        # must only short-circuit when a primary document is found;
        # otherwise we go to CourtListener to recover the indictment text.
        store.mark_entry(
            1, 99, "2025-06-15T00:00:00Z", "fp-disp",
            date_filed="2025-06-15", entry_number=37,
            description="JUDGMENT in a Criminal Case",
            recap_documents=[{"id": 600, "plain_text": "judgment body"}],
        )
        cl = _FakeCourtListener({
            (1, "date_filed"): [
                {"id": 10, "description": "INDICTMENT",
                 "date_filed": "2024-01-01", "entry_number": 1,
                 "recap_documents": [{"id": 500,
                                      "plain_text": "indictment body"}]},
            ],
            (1, "-date_filed"): [
                {"id": 99, "description": "JUDGMENT in a Criminal Case",
                 "date_filed": "2025-06-15", "entry_number": 37,
                 "recap_documents": [{"id": 600,
                                      "plain_text": "judgment body"}]},
            ],
        })
        primary, dispositions = find_primary_documents(cl, 1, store=store)
        # CourtListener fallback gave us the indictment that the local cache lacked.
        assert [e["id"] for e in primary] == [10]
        assert [e["id"] for e in dispositions] == [99]

    def test_motion_in_cache_does_not_short_circuit_cl_fallback(self, store):
        # Anthropic v. DoW regression: pre-fix data had the actual
        # PI-order entries stored as NULL-description fingerprint stubs
        # (filter-failed under the old logic), while the original
        # "MOTION for ... Preliminary Injunction" was body-bearing
        # because it matched the hearing pre-filter. The old short-
        # circuit then declared `dispositions = [motion]` (the broad
        # `is_disposition` matches "injunction" anywhere in the head)
        # and never fell back to CourtListener, so the summary LLM saw the motion
        # text as the disposition and wrote "PI taken under submission"
        # forever — even after the court actually granted the
        # injunction. The strict `_is_disposition_document` must reject
        # the motion so the short-circuit lapses and CourtListener is consulted.
        store.mark_entry(
            1, 6, "2026-03-09T00:00:00Z", "fp-motion", date_filed="2026-03-09",
            entry_number=6,
            description=(
                "MOTION for Temporary Restraining Order, MOTION for "
                "Preliminary Injunction, MOTION to Stay Pursuant to "
                "Section 705 filed by Anthropic PBC."
            ),
            recap_documents=[{"id": 600}],
        )
        cl = _FakeCourtListener({
            (1, "date_filed"): [
                {"id": 1, "description": "COMPLAINT for Declaratory and "
                 "Injunctive Relief", "date_filed": "2026-03-09",
                 "entry_number": 1},
            ],
            (1, "-date_filed"): [
                {"id": 134, "description": "ORDER GRANTING MOTION FOR "
                 "PRELIMINARY INJUNCTION 6", "date_filed": "2026-03-26",
                 "entry_number": 134},
                {"id": 135, "description": "PRELIMINARY INJUNCTION ORDER. "
                 "Signed by Judge Lin on 3/26/2026.",
                 "date_filed": "2026-03-26", "entry_number": 135},
            ],
        })
        primary, dispositions = find_primary_documents(cl, 1, store=store)
        assert [e["id"] for e in primary] == [1]
        # Both real orders reach the LLM; the motion (entry 6) is NOT
        # in the disposition set even though it's cached body-bearing.
        assert sorted(e["id"] for e in dispositions) == [134, 135]

    def test_stub_only_rows_dont_satisfy_the_cache(self, store):
        # Filter-failed entries land as fingerprint stubs with description
        # IS NULL. They must NOT satisfy the local-cache check — otherwise
        # a docket with only stubs would silently return zero primary/disp and
        # skip the CourtListener fallback, when CourtListener might actually have a primary
        # document filed before the cutoff.
        store.mark_entry(
            1, 42, "2024-01-01T00:00:00Z", "fp-stub", date_filed="2024-01-01",
            entry_number=2, description=None,  # filter-failed stub
        )
        cl = _FakeCourtListener({
            (1, "date_filed"): [
                {"id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01"},
            ],
            (1, "-date_filed"): [],
        })
        primary, _ = find_primary_documents(cl, 1, store=store)
        assert [e["id"] for e in primary] == [10]

    def test_stale_cache_falls_through_to_cl_and_self_heals(self, store):
        # The us-v-moucka shape: cache has the indictment as a body-bearing
        # entry, but the stored recap_documents have empty plain_text on
        # the available main doc (the row was written before plain_text
        # was a stored field). CourtListener has the full text. The function must:
        # (1) detect the stale cache, (2) fall through to CourtListener, (3) rewrite
        # the local store's recap_documents so the next call short-
        # circuits with the fresh data.
        store.mark_entry(
            1, 10, "2024-01-01T00:00:00Z", "fp-stale",
            date_filed="2024-01-01", entry_number=1,
            description="INDICTMENT",
            # Available main doc with NO plain_text — the staleness
            # signature.
            recap_documents=[{
                "id": 500,
                "document_number": "1",
                "attachment_number": None,
                "is_available": True,
                "is_sealed": False,
                "plain_text": None,
            }],
        )
        fresh_indictment = {
            "id": 10, "description": "INDICTMENT",
            "date_filed": "2024-01-01", "entry_number": 1,
            "recap_documents": [{
                "id": 500,
                "document_number": "1",
                "attachment_number": None,
                "is_available": True,
                "is_sealed": False,
                "plain_text": "Body of indictment with 39k chars of text...",
            }],
        }
        cl = _FakeCourtListener({
            (1, "date_filed"): [fresh_indictment],
            (1, "-date_filed"): [fresh_indictment],
        })
        primary, _ = find_primary_documents(cl, 1, store=store)
        # CourtListener fallback returned the indictment with full plain_text.
        assert [e["id"] for e in primary] == [10]
        assert (
            primary[0]["recap_documents"][0]["plain_text"]
            == "Body of indictment with 39k chars of text..."
        )
        # AND the local store was repaired: the cached recap_documents
        # now has plain_text populated, so the next call short-circuits.
        refreshed = store.get_entries_with_body(1)
        moucka = next(e for e in refreshed if e["id"] == 10)
        assert (
            moucka["recap_documents"][0]["plain_text"]
            == "Body of indictment with 39k chars of text..."
        )
        # Sanity: a follow-up call with a CourtListener that would raise on any
        # _get must now succeed entirely from the (repaired) cache.
        class _BoomCourtListener(_BoomCourtListenerBase):
            def _get(self, *a, **kw):
                raise AssertionError("repaired cache must short-circuit")
        primary2, _ = find_primary_documents(_BoomCourtListener(), 1, store=store)
        assert [e["id"] for e in primary2] == [10]

    def test_sealed_or_unavailable_main_doc_does_not_count_as_stale(self, store):
        # A cached primary whose available main doc legitimately has no
        # text (sealed indictment, or not yet uploaded to RECAP) must
        # NOT trigger the staleness fallback — that's a real "no text on
        # CourtListener either" condition, not a stale cache.
        store.mark_entry(
            1, 10, "2024-01-01T00:00:00Z", "fp-sealed",
            date_filed="2024-01-01", entry_number=1,
            description="INDICTMENT",
            recap_documents=[{
                "id": 500,
                "document_number": "1",
                "attachment_number": None,
                "is_available": False,  # not on RECAP
                "is_sealed": False,
                "plain_text": None,
            }],
        )
        class _BoomCourtListener(_BoomCourtListenerBase):
            def _get(self, *a, **kw):
                raise AssertionError(
                    "is_available=False is not a staleness signal — "
                    "the short-circuit must hold"
                )
        primary, _ = find_primary_documents(_BoomCourtListener(), 1, store=store)
        assert [e["id"] for e in primary] == [10]

    def test_sealed_main_doc_does_not_count_as_stale(self, store):
        # is_sealed=True on the main doc is a legitimate "no text"
        # state, not a stale cache. The short-circuit must hold without
        # a CourtListener round-trip.
        store.mark_entry(
            1, 10, "2024-01-01T00:00:00Z", "fp-sealed-main",
            date_filed="2024-01-01", entry_number=1,
            description="INDICTMENT",
            recap_documents=[{
                "id": 500,
                "document_number": "1",
                "attachment_number": None,
                "is_available": True,
                "is_sealed": True,
                "plain_text": None,
            }],
        )
        class _BoomCourtListener(_BoomCourtListenerBase):
            def _get(self, *a, **kw):
                raise AssertionError(
                    "is_sealed=True is not a staleness signal — "
                    "the short-circuit must hold"
                )
        primary, _ = find_primary_documents(_BoomCourtListener(), 1, store=store)
        assert [e["id"] for e in primary] == [10]

    def test_attachment_with_empty_plain_text_does_not_count_as_stale(self, store):
        # The staleness detector skips attachments — they often have
        # empty plain_text on purpose (exhibits pypdf can't read,
        # signature pages, etc.). A cached primary whose MAIN doc has
        # full plain_text but whose attachment has empty plain_text
        # must NOT trigger the fallback.
        store.mark_entry(
            1, 10, "2024-01-01T00:00:00Z", "fp-attach",
            date_filed="2024-01-01", entry_number=1,
            description="INDICTMENT",
            recap_documents=[
                {
                    "id": 500,
                    "document_number": "1",
                    "attachment_number": None,
                    "is_available": True,
                    "is_sealed": False,
                    "plain_text": "Body of the indictment with charges.",
                },
                {
                    "id": 501,
                    "document_number": "1",
                    "attachment_number": 1,  # attachment — must be skipped
                    "is_available": True,
                    "is_sealed": False,
                    "plain_text": None,
                },
            ],
        )
        class _BoomCourtListener(_BoomCourtListenerBase):
            def _get(self, *a, **kw):
                raise AssertionError(
                    "an attachment's empty plain_text is not a staleness "
                    "signal — the short-circuit must hold"
                )
        primary, _ = find_primary_documents(_BoomCourtListener(), 1, store=store)
        assert [e["id"] for e in primary] == [10]


# ---------------------------------------------------------------------------
# detect_sealing
# ---------------------------------------------------------------------------


class TestDetectSealing:
    """Phase 2 sealing-detection heuristic. Used by ``summarize_docket`` to
    surface a DOCKET VISIBILITY ADVISORY to the LLM when the docket has a
    granted sealing order with no contradicting public signals.

    The motivating case is us-v-dubranova (2:25-cr-00578, C.D. Cal.): the
    indictment was unsealed for RECAP capture, then the court granted an
    ex parte application to seal the indictment and related documents,
    and the docket has been quiet on the public side ever since. Routine
    seal-then-unseal criminal cases — where the indictment was sealed at
    filing and then unsealed at arrest — produce a sealing order entry
    too, so the heuristic has to discriminate between the two without
    false-positives. The four kill signals are: subsequent unsealing
    order, any disposition document, or substantial post-sealing
    publicly-available activity.
    """

    def _dubranova_shape(self) -> dict:
        """A docket whose visible entries match the Dubranova pattern:
        a granted sealing order on the indictment, no unsealing order,
        no disposition, and almost no available post-seal entries."""
        return {
            (72013021, "date_filed"): [
                {
                    "id": 1, "entry_number": 33, "date_filed": "2025-08-21",
                    "description": "FIRST SUPERSEDING INDICTMENT filed as to ...",
                    "recap_documents": [{"is_available": True}],
                },
                {
                    "id": 2, "entry_number": 43, "date_filed": "2025-08-21",
                    "description": "EX PARTE APPLICATION to Seal Indictment and Related Documents Filed by Plaintiff USA",
                    "recap_documents": [{"is_available": False}],
                },
                {
                    "id": 3, "entry_number": 44, "date_filed": "2025-08-21",
                    "description": "ORDER by Magistrate Judge Steve Kim granting 43 EX PARTE APPLICATION to Seal Indictment and Related Documents",
                    "recap_documents": [{"is_available": False}],
                },
                {
                    "id": 4, "entry_number": 45, "date_filed": "2025-08-21",
                    "description": "CASE SUMMARY filed by AUSA as to Defendant Dubranova",
                    "recap_documents": [{"is_available": True}],
                },
                {
                    "id": 5, "entry_number": 54, "date_filed": "2025-08-21",
                    "description": "NOTICE OF REQUEST FOR DETENTION as to Dubranova",
                    "recap_documents": [{"is_available": False}],
                },
                {
                    "id": 6, "entry_number": 32, "date_filed": "2025-08-28",
                    "description": "SEALED DOCUMENT - UNDER SEAL DOCUMENT",
                    "recap_documents": [{"is_available": False}],
                },
            ],
            (72013021, "-date_filed"): [],
        }

    def test_dubranova_shape_returns_advisory(self):
        cl = _FakeCourtListener(self._dubranova_shape())
        result = summary.detect_sealing(cl, 72013021, dispositions=[])
        assert result is not None
        assert result["sealing_entry_number"] == 44
        assert result["sealing_date_filed"] == "2025-08-21"
        assert "granting 43 EX PARTE APPLICATION to Seal" in result["sealing_description"]

    def test_unsealing_order_kills_signal(self):
        pages = self._dubranova_shape()
        # Add an unsealing order DATED AFTER the sealing order. The dates
        # in the Dubranova shape are all 2025-08-21; bump the unsealing
        # entry to 2025-09-15 so the post-seal check fires correctly.
        pages[(72013021, "date_filed")].append({
            "id": 99, "entry_number": 80, "date_filed": "2025-09-15",
            "description": "ORDER by Magistrate Judge granting 78 MOTION to Unseal Indictment",
            "recap_documents": [{"is_available": True}],
        })
        cl = _FakeCourtListener(pages)
        assert summary.detect_sealing(cl, 72013021, dispositions=[]) is None

    def test_disposition_presence_kills_signal_without_an_api_call(self):
        # When a disposition is in the docket, the dispositive ruling
        # landed publicly. Don't bother walking — just refuse to flag.
        # Also asserts we make zero CourtListener calls in this short-circuit path.
        cl = _FakeCourtListener(self._dubranova_shape())
        result = summary.detect_sealing(
            cl, 72013021,
            dispositions=[{"id": 99, "description": "JUDGMENT"}],
        )
        assert result is None
        assert cl.calls == []

    def test_substantial_post_seal_public_activity_kills_signal(self):
        pages = self._dubranova_shape()
        # Add 4 publicly-available entries dated AFTER the sealing order
        # — that's above the default threshold of 3, so the seal is
        # functionally lifted even without an explicit unsealing entry.
        for i, day in enumerate(("2025-09-01", "2025-09-15", "2025-10-01", "2025-10-15")):
            pages[(72013021, "date_filed")].append({
                "id": 100 + i,
                "entry_number": 60 + i,
                "date_filed": day,
                "description": f"Status Conference {i+1}",
                "recap_documents": [{"is_available": True}],
            })
        cl = _FakeCourtListener(pages)
        assert summary.detect_sealing(cl, 72013021, dispositions=[]) is None

    def test_no_sealing_order_returns_none(self):
        cl = _FakeCourtListener({
            (1, "date_filed"): [
                {
                    "id": 1, "entry_number": 1, "date_filed": "2024-01-01",
                    "description": "INDICTMENT",
                    "recap_documents": [{"is_available": True}],
                },
                {
                    "id": 2, "entry_number": 2, "date_filed": "2024-02-01",
                    "description": "MINUTE ENTRY for arraignment",
                    "recap_documents": [{"is_available": True}],
                },
            ],
            (1, "-date_filed"): [],
        })
        assert summary.detect_sealing(cl, 1, dispositions=[]) is None

    def test_narrow_sealing_order_with_high_public_activity_does_not_trigger(self):
        # A "Seal Plea Agreement" order is narrow scope; combined with
        # plenty of publicly-available post-sealing activity, this should
        # NOT flag the docket as currently sealed.
        cl = _FakeCourtListener({
            (1, "date_filed"): [
                {
                    "id": 1, "entry_number": 1, "date_filed": "2024-01-01",
                    "description": "INDICTMENT", "recap_documents": [{"is_available": True}],
                },
                {
                    "id": 2, "entry_number": 30, "date_filed": "2024-05-01",
                    "description": "ORDER granting Motion to Seal Plea Agreement",
                    "recap_documents": [{"is_available": True}],
                },
                {
                    "id": 3, "entry_number": 31, "date_filed": "2024-06-01",
                    "description": "Sentencing hearing held",
                    "recap_documents": [{"is_available": True}],
                },
                {
                    "id": 4, "entry_number": 32, "date_filed": "2024-06-02",
                    "description": "Status Conference",
                    "recap_documents": [{"is_available": True}],
                },
                {
                    "id": 5, "entry_number": 33, "date_filed": "2024-06-15",
                    "description": "Notice of Appeal",
                    "recap_documents": [{"is_available": True}],
                },
                {
                    "id": 6, "entry_number": 34, "date_filed": "2024-07-01",
                    "description": "Minute Entry",
                    "recap_documents": [{"is_available": True}],
                },
            ],
            (1, "-date_filed"): [],
        })
        assert summary.detect_sealing(cl, 1, dispositions=[]) is None

    def test_latest_sealing_order_is_the_operative_one(self):
        # If a docket has two granted sealing orders (e.g., a narrow
        # earlier seal followed by a broader later one), the advisory
        # should reference the LATER one — that's the one currently in
        # effect on the visible public docket.
        cl = _FakeCourtListener({
            (1, "date_filed"): [
                {
                    "id": 1, "entry_number": 5, "date_filed": "2024-01-01",
                    "description": "ORDER granting Motion to Seal Exhibit A",
                    "recap_documents": [{"is_available": False}],
                },
                {
                    "id": 2, "entry_number": 20, "date_filed": "2024-06-01",
                    "description": "ORDER granting Motion to Seal Case",
                    "recap_documents": [{"is_available": False}],
                },
            ],
            (1, "-date_filed"): [],
        })
        result = summary.detect_sealing(cl, 1, dispositions=[])
        assert result is not None
        assert result["sealing_entry_number"] == 20
        assert result["sealing_date_filed"] == "2024-06-01"

    def test_description_is_truncated(self):
        long_desc = (
            "ORDER by Judge X granting 42 MOTION to Seal Indictment "
            + "and Related Documents " * 30
        )
        cl = _FakeCourtListener({
            (1, "date_filed"): [{
                "id": 1, "entry_number": 1, "date_filed": "2024-01-01",
                "description": long_desc,
                "recap_documents": [{"is_available": False}],
            }],
            (1, "-date_filed"): [],
        })
        result = summary.detect_sealing(cl, 1, dispositions=[])
        assert result is not None
        # Truncated to <= 240 chars + the ellipsis.
        assert len(result["sealing_description"]) <= 243
        assert result["sealing_description"].endswith("...")

    def test_overlapping_oldest_and_newest_pages_dedup(self):
        # On a tiny docket, the oldest-first and newest-first walks
        # return the same entries. The dedup-on-id guard inside
        # detect_sealing keeps us from counting the same row twice in
        # the post-seal available count.
        seal_order = {
            "id": 44, "entry_number": 44, "date_filed": "2025-08-21",
            "description": "ORDER granting 43 EX PARTE APPLICATION to Seal Indictment",
            "recap_documents": [{"is_available": False}],
        }
        cl = _FakeCourtListener({
            (1, "date_filed"): [seal_order],
            (1, "-date_filed"): [seal_order],  # same row, returned by both walks
        })
        result = summary.detect_sealing(cl, 1, dispositions=[])
        assert result is not None
        assert result["sealing_entry_number"] == 44

    def test_earlier_sealing_order_does_not_displace_an_already_later_one(self):
        # The latest-sealing-order picker iterates in walk order and
        # updates `sealing_order` only when the candidate has a strictly
        # later date. Cover the case where the first match is already
        # the latest and subsequent matches don't displace it (the
        # if-branch goes False).
        cl = _FakeCourtListener({
            (1, "date_filed"): [
                {
                    "id": 1, "entry_number": 20, "date_filed": "2024-06-01",
                    "description": "ORDER granting Motion to Seal Indictment",
                    "recap_documents": [{"is_available": False}],
                },
                {
                    "id": 2, "entry_number": 5, "date_filed": "2024-01-01",
                    "description": "ORDER granting Motion to Seal Exhibit A",
                    "recap_documents": [{"is_available": False}],
                },
            ],
            (1, "-date_filed"): [],
        })
        result = summary.detect_sealing(cl, 1, dispositions=[])
        assert result is not None
        assert result["sealing_entry_number"] == 20  # later date wins


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
    def test_writes_summary_when_primary_text_available(self, store, patch_llm, patch_pdf):
        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {500: "INDICTMENT body text..."}
        cl = _FakeCourtListener({
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
        assert [d["entry_id"] for d in call["primary_documents"]] == [10]

    def test_returns_none_when_no_primary_text_extractable(self, store, patch_llm, patch_pdf):
        _seed_docket_meta(store, 1)
        # Primary document present but PDF text is empty for every doc.
        patch_pdf["texts"] = {}
        cl = _FakeCourtListener({
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

    def test_insufficient_documents_fallback_is_stored_and_warns(
        self, store, patch_pdf, monkeypatch, caplog,
    ):
        # When the LLM exercises its prompt-level refusal — emitting the
        # canonical `SUMMARY_INSUFFICIENT_DOCUMENTS` sentence — the
        # response IS stored (subscribers see the explicit refusal on
        # the index, not silence), and a warning is logged so the
        # operator can investigate. Mirrors the us-v-dubranova garbled
        # path: extraction was bad, the model honestly refused, and we
        # surface the situation visibly without confabulating.
        from case_calendar.llm import SUMMARY_INSUFFICIENT_DOCUMENTS

        def _refusal(**kwargs):
            return (SUMMARY_INSUFFICIENT_DOCUMENTS, "fake/model-v1")

        monkeypatch.setattr(summary.llm, "generate_docket_summary", _refusal)

        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {500: "INDICTMENT body text..."}  # text passes _attach_text
        cl = _FakeCourtListener({
            (1, "date_filed"): [{
                "id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01",
                "recap_documents": [{"id": 500}],
            }],
            (1, "-date_filed"): [],
        })
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")

        import logging
        with caplog.at_level(logging.WARNING, logger="case_calendar.summary"):
            row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)

        assert row is not None
        assert row["summary"] == SUMMARY_INSUFFICIENT_DOCUMENTS
        persisted = store.get_docket_summary("us-v-doe", 1)
        assert persisted["summary"] == SUMMARY_INSUFFICIENT_DOCUMENTS
        # And the warning fired so the operator can find the docket.
        assert any(
            "insufficient-documents fallback" in r.message and "docket 1" in r.message
            for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_warns_when_primary_document_text_is_suspiciously_short(
        self, store, patch_llm, patch_pdf, caplog,
    ):
        import logging
        _seed_docket_meta(store, 1)
        # Primary document extracts to text under the threshold — the
        # us-v-moucka shape, where pypdf returned only the page headers
        # and caption from a full multi-count indictment. The summary
        # still gets generated (the LLM has the prompt rule that says
        # work around partial inputs silently), but a WARNING fires so
        # the operator can find the docket and investigate the
        # underlying parsing failure separately.
        short_text = "UNITED STATES OF AMERICA v. DOE\nPage 1 of 12"
        patch_pdf["texts"] = {500: short_text}
        cl = _FakeCourtListener({
            (1, "date_filed"): [{
                "id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01",
                "entry_number": 7,
                "recap_documents": [{"id": 500}],
            }],
            (1, "-date_filed"): [],
        })
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")

        with caplog.at_level(logging.WARNING, logger="case_calendar.summary"):
            row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)

        # Summary was still produced — the LLM works around partial
        # inputs (the stubbed `patch_llm` returns a clean summary).
        assert row is not None
        # And the warning fired with the specific docket / entry
        # references the operator needs to find it.
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        matched = [
            m for m in warning_messages
            if "docket 1" in m and "entry #7" in m and "extracted to only" in m
        ]
        assert matched, warning_messages

    def test_no_warning_when_primary_document_text_is_full_length(
        self, store, patch_llm, patch_pdf, caplog,
    ):
        import logging
        _seed_docket_meta(store, 1)
        # A realistic indictment runs many KB. Pad past the 1500-char
        # threshold so the short-doc warning doesn't fire.
        long_text = "INDICTMENT body. " * 200  # ~3400 chars
        patch_pdf["texts"] = {500: long_text}
        cl = _FakeCourtListener({
            (1, "date_filed"): [{
                "id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01",
                "recap_documents": [{"id": 500}],
            }],
            (1, "-date_filed"): [],
        })
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")

        with caplog.at_level(logging.WARNING, logger="case_calendar.summary"):
            summarize_docket(cl=cl, store=store, case=case, docket_id=1)

        # No "extracted to only N chars" warnings on a normal-length doc.
        suspect = [
            r.message for r in caplog.records
            if "extracted to only" in r.message
        ]
        assert suspect == [], suspect

    def test_logs_sealing_advisory_when_detect_sealing_fires(
        self, store, patch_llm, patch_pdf, caplog,
    ):
        # Wire a docket that matches the Phase-2 sealing-detected shape:
        # a granted sealing order entry, no unsealing order, no
        # disposition, no significant post-seal public activity. The
        # summarize_docket call must surface a `sealing advisory` info
        # log line so operators can spot which dockets the LLM saw the
        # advisory on.
        import logging
        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {500: "INDICTMENT body text..."}
        cl = _FakeCourtListener({
            (1, "date_filed"): [
                {
                    "id": 10, "description": "INDICTMENT",
                    "date_filed": "2025-08-21", "entry_number": 33,
                    "recap_documents": [{"id": 500}],
                },
                {
                    "id": 11, "entry_number": 44, "date_filed": "2025-08-21",
                    "description": "ORDER granting 43 EX PARTE APPLICATION to Seal Indictment",
                    "recap_documents": [{"is_available": False}],
                },
            ],
            (1, "-date_filed"): [],
        })
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")

        with caplog.at_level(logging.INFO, logger="case_calendar.summary"):
            row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)

        assert row is not None
        advisory_logs = [
            r.message for r in caplog.records
            if "sealing advisory" in r.message and "entry #44" in r.message
        ]
        assert advisory_logs, [r.message for r in caplog.records]
        # The advisory rode through to the LLM call as well.
        assert patch_llm[0]["sealing_advisory"] is not None
        assert patch_llm[0]["sealing_advisory"]["sealing_entry_number"] == 44

    def test_borrows_from_sibling_when_primary_docket_has_no_primary_document(
        self, store, patch_llm, patch_pdf,
    ):
        # Primary docket 1 has no primary document (appellate-style).
        # Sibling docket 2 has the indictment.
        _seed_docket_meta(store, 1, docket_number="24-1234", court_id="ca9")
        _seed_docket_meta(store, 2, docket_number="1:24-cr-100", court_id="dcd")
        patch_pdf["texts"] = {500: "INDICTMENT body text..."}
        cl = _FakeCourtListener({
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
        primary_documents = patch_llm[0]["primary_documents"]
        assert primary_documents[0]["description"].endswith("[from sibling 1:24-cr-100 D.D.C.]")

    def test_borrowing_swallows_sibling_failure_and_continues(
        self, store, patch_llm, patch_pdf, caplog,
    ):
        _seed_docket_meta(store, 1, docket_number="24-1234", court_id="ca9")
        _seed_docket_meta(store, 2, docket_number="1:24-cr-100", court_id="dcd")
        _seed_docket_meta(store, 3, docket_number="1:24-cr-200", court_id="dcd")
        patch_pdf["texts"] = {600: "INDICTMENT body..."}

        # Sibling 2 raises on its docket-entries call; sibling 3 succeeds.
        original_get = _FakeCourtListener._get

        def _flaky_get(self, url, params=None):
            if params and params.get("docket") == 2:
                raise RuntimeError("transient CourtListener outage")
            return original_get(self, url, params)

        cl = _FakeCourtListener({
            (1, "date_filed"): [],
            (1, "-date_filed"): [],
            (3, "date_filed"): [{
                "id": 30, "description": "INDICTMENT",
                "date_filed": "2024-01-01",
                "recap_documents": [{"id": 600}],
            }],
            (3, "-date_filed"): [],
        })
        cl._get = _flaky_get.__get__(cl, _FakeCourtListener)
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1, 2, 3], calendar="cyber")

        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None

    def test_returns_none_when_no_sibling_has_primary(
        self, store, patch_llm, patch_pdf,
    ):
        _seed_docket_meta(store, 1, docket_number="24-1", court_id="ca9")
        _seed_docket_meta(store, 2, docket_number="24-2", court_id="ca9")
        cl = _FakeCourtListener({
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
        cl = _FakeCourtListener({
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
        assert [d["entry_id"] for d in patch_llm[0]["disposition_documents"]] == [99]

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
        cl = _FakeCourtListener({
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

        dispositions = patch_llm[0]["disposition_documents"]
        assert [d["entry_id"] for d in dispositions] == [99]
        # The sentence figures must reach the LLM — that's the whole point.
        assert "92 months imprisonment" in dispositions[0]["text"]

    def test_primary_document_without_pdf_is_still_dropped(
        self, store, patch_llm, patch_pdf,
    ):
        # By design the description fallback is scoped to dispositions only.
        # Primary documents are indictments / complaints — a clerk's
        # minute-entry stub isn't an acceptable substitute, and feeding one
        # in would produce a vacuous summary. Confirm the asymmetry.
        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {}  # no PDF text extracts
        cl = _FakeCourtListener({
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
        cl = _FakeCourtListener({
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


class TestExtraDocuments:
    """The ``extra_documents`` workaround for CourtListener data gaps (e.g. unsealed
    indictments whose docket entries are still hidden by CourtListener bug #7345).
    Each entry is fetched at summary time and fed to the LLM as part of a
    distinct "EXTRA DOCUMENTS" section, alongside the primary-document
    and disposition slots that the CourtListener walk fills. The operator's required
    ``note`` describes what the document is and why it was added — that
    natural-language description carries the meaning a rigid role
    taxonomy couldn't."""

    def test_feeds_extras_when_cl_has_no_indictment(
        self, store, patch_llm, patch_pdf, monkeypatch,
    ):
        # The canonical Zewei case: CourtListener has no primary document
        # (entries 1-4 missing), so the only document the summary LLM
        # sees is the operator-provided one — in the extras section,
        # with its note describing what it is.
        _seed_docket_meta(store, 1, docket_number="4:23-cr-00523", court_id="txsd")
        store.upsert_court("txsd", "S.D. Tex.", "TXSD", "Southern District of Texas")
        monkeypatch.setattr(
            summary.pdf, "extract_text_from_url",
            lambda url, allow_ocr=True: "REDACTED INDICTMENT body from DoJ PR PDF...",
        )
        cl = _FakeCourtListener({(1, "date_filed"): [], (1, "-date_filed"): []})
        case = _Case(
            case_id="us-v-zewei", name="US v. Zewei",
            dockets=[1], calendar="cyber",
            extra_documents=[ExtraDocument(
                docket=1,
                url="https://www.justice.gov/opa/media/1407196/dl",
                note="Indictment was filed under seal but the seal has "
                     "since been lifted; treat as the primary document.",
            )],
        )
        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None
        # CourtListener-sourced slots are empty; the extras section carries the doc.
        assert patch_llm[0]["primary_documents"] == []
        assert patch_llm[0]["disposition_documents"] == []
        extras = patch_llm[0]["extra_documents"]
        assert len(extras) == 1
        doc = extras[0]
        assert doc["source_url"] == "https://www.justice.gov/opa/media/1407196/dl"
        assert doc["operator_note"].startswith("Indictment was filed under seal")
        assert doc["entry_id"] is None  # not a CourtListener entry
        assert "REDACTED INDICTMENT" in doc["text"]

    def test_feeds_extras_alongside_cl_documents(
        self, store, patch_llm, patch_pdf, monkeypatch,
    ):
        # Overlap window: CourtListener has the primary document AND an operator
        # also listed an extra. CourtListener doc fills the primary slot; the
        # extra rides in its own section (the LLM sees both, with the
        # provenance distinction explicit).
        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {500: "CourtListener INDICTMENT body"}
        monkeypatch.setattr(
            summary.pdf, "extract_text_from_url",
            lambda url, allow_ocr=True: "OPERATOR INDICTMENT body",
        )
        cl = _FakeCourtListener({
            (1, "date_filed"): [{
                "id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01",
                "recap_documents": [{"id": 500}],
            }],
            (1, "-date_filed"): [],
        })
        case = _Case(
            case_id="us-v-x", name="US v. X", dockets=[1], calendar="cyber",
            extra_documents=[ExtraDocument(
                docket=1, url="https://example.gov/i.pdf",
                note="overlap-window operator copy of the indictment",
            )],
        )
        summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert [d["entry_id"] for d in patch_llm[0]["primary_documents"]] == [10]
        extras = patch_llm[0]["extra_documents"]
        assert len(extras) == 1
        assert extras[0]["source_url"] == "https://example.gov/i.pdf"

    def test_failed_fetch_is_dropped(
        self, store, patch_llm, patch_pdf, monkeypatch, caplog,
    ):
        # URL is down / the PDF won't extract. The summary pipeline still
        # runs on whatever CourtListener did surface — extra_documents failures are
        # logged but not fatal.
        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {500: "CourtListener INDICTMENT body"}
        monkeypatch.setattr(
            summary.pdf, "extract_text_from_url",
            lambda url, allow_ocr=True: None,
        )
        cl = _FakeCourtListener({
            (1, "date_filed"): [{
                "id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01",
                "recap_documents": [{"id": 500}],
            }],
            (1, "-date_filed"): [],
        })
        case = _Case(
            case_id="us-v-x", name="US v. X", dockets=[1], calendar="cyber",
            extra_documents=[ExtraDocument(
                docket=1, url="https://broken.example/x.pdf",
                note="will fail to fetch",
            )],
        )
        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None
        # Only the CourtListener doc reaches the LLM; the dropped one isn't appended.
        assert [d["entry_id"] for d in patch_llm[0]["primary_documents"]] == [10]
        assert patch_llm[0]["extra_documents"] == []

    def test_only_extras_for_target_docket_are_fetched(
        self, store, patch_llm, patch_pdf, monkeypatch,
    ):
        # extra_documents scope to ONE docket via the `docket` field. When
        # summarize_docket runs on docket A, extras pointing at docket B
        # must NOT be fetched / folded in.
        _seed_docket_meta(store, 1)
        _seed_docket_meta(store, 2)
        patch_pdf["texts"] = {500: "DOCKET 1 INDICTMENT"}
        fetched: list[str] = []

        def _fake_fetch(url, allow_ocr=True):
            fetched.append(url)
            return "OPERATOR doc text"

        monkeypatch.setattr(summary.pdf, "extract_text_from_url", _fake_fetch)
        cl = _FakeCourtListener({
            (1, "date_filed"): [{
                "id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01",
                "recap_documents": [{"id": 500}],
            }],
            (1, "-date_filed"): [],
        })
        case = _Case(
            case_id="us-v-x", name="US v. X", dockets=[1, 2], calendar="cyber",
            extra_documents=[
                ExtraDocument(docket=2, url="https://x.com/wrong.pdf",
                              note="wrong docket"),
                ExtraDocument(docket=1, url="https://x.com/right.pdf",
                              note="right docket"),
            ],
        )
        summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert fetched == ["https://x.com/right.pdf"]

    def test_extras_alone_satisfy_content_gate(
        self, store, patch_llm, patch_pdf, monkeypatch,
    ):
        # Without the extras-aware content gate, this docket would hit
        # the "no primary document text could be extracted" branch and
        # return None. With the extras-aware gate, the operator's doc
        # satisfies the content check on its own and the summary
        # proceeds — the canonical Zewei flow.
        _seed_docket_meta(store, 1)
        monkeypatch.setattr(
            summary.pdf, "extract_text_from_url",
            lambda url, allow_ocr=True: "operator-supplied indictment text",
        )
        cl = _FakeCourtListener({(1, "date_filed"): [], (1, "-date_filed"): []})
        case = _Case(
            case_id="us-v-x", name="US v. X", dockets=[1], calendar="cyber",
            extra_documents=[ExtraDocument(
                docket=1, url="https://x.com/i.pdf",
                note="unsealed indictment, sourced from DoJ PR attachment",
            )],
        )
        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None
        assert patch_llm[0]["primary_documents"] == []
        assert len(patch_llm[0]["extra_documents"]) == 1


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
        cl = _FakeCourtListener({})  # never queried
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")

        written = refresh_stale(cl=cl, store=store, cases=[case])

        assert written == {}
        assert patch_llm == []

    def test_regenerates_when_missing(self, store, patch_llm, patch_pdf):
        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {500: "INDICTMENT body"}
        cl = _FakeCourtListener({
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
        cl = _FakeCourtListener({
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
        cl = _FakeCourtListener({
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

    def test_force_regenerates_non_stale_rows(self, store, patch_llm, patch_pdf):
        # Default behavior is to skip non-stale rows. force=True bypasses
        # the stale check so a single sync can pick up a model upgrade or
        # prompt change without a separate `summarize --force` invocation
        # that would hit CourtListener all over again.
        _seed_docket_meta(store, 1)
        store.upsert_case_summary(
            "us-v-doe", 1, summary="old", model="prev/model", source_entry_ids=[],
        )
        # Row is fresh — is_summary_stale would return False.
        assert not store.is_summary_stale("us-v-doe", 1)
        patch_pdf["texts"] = {500: "INDICTMENT body"}
        cl = _FakeCourtListener({
            (1, "date_filed"): [{
                "id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01",
                "recap_documents": [{"id": 500}],
            }],
            (1, "-date_filed"): [],
        })
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")
        written = refresh_stale(cl=cl, store=store, cases=[case], force=True)
        assert written == {"us-v-doe": {1}}
        assert len(patch_llm) == 1

    def test_uses_aggregation_note_override(self, store, patch_llm, patch_pdf):
        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {500: "INDICTMENT body"}
        cl = _FakeCourtListener({
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

    def test_skipped_docket_not_added_to_written_set(
        self, store, patch_llm, patch_pdf,
    ):
        # When `summarize_docket` returns None (no extractable primary
        # document text), the row stays out of the `written` map so the
        # caller's emit logic doesn't think the index needs re-rendering
        # for that docket. Covers the `if row:` falsy branch.
        _seed_docket_meta(store, 1)
        # Empty PDF text → summarize_docket returns None.
        patch_pdf["texts"] = {}
        cl = _FakeCourtListener({
            (1, "date_filed"): [{
                "id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01",
                "recap_documents": [{"id": 500}],
            }],
            (1, "-date_filed"): [],
        })
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")
        written = refresh_stale(cl=cl, store=store, cases=[case])
        # Nothing summarized → empty written map (or no entry for this case).
        assert "us-v-doe" not in written or written["us-v-doe"] == set()


class TestSummarizeCase:
    def test_force_overwrites_existing_summary(self, store, patch_llm, patch_pdf):
        _seed_docket_meta(store, 1)
        store.upsert_case_summary(
            "us-v-doe", 1, summary="old", model="prev/model", source_entry_ids=[],
        )
        patch_pdf["texts"] = {500: "INDICTMENT body"}
        cl = _FakeCourtListener({
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
        cl = _FakeCourtListener({})
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")
        rows = summarize_case(cl=cl, store=store, case=case)
        assert rows == []
        assert patch_llm == []

    def test_skipped_docket_not_in_returned_rows(self, store, patch_llm, patch_pdf):
        # `summarize_docket` returns None when no primary document text
        # could be extracted. `summarize_case` must skip that docket
        # rather than append None to the result list. Covers the
        # `if row:` falsy branch.
        _seed_docket_meta(store, 1)
        # Empty PDF text → summarize_docket returns None.
        patch_pdf["texts"] = {}
        cl = _FakeCourtListener({
            (1, "date_filed"): [{
                "id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01",
                "recap_documents": [{"id": 500}],
            }],
            (1, "-date_filed"): [],
        })
        case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")
        rows = summarize_case(cl=cl, store=store, case=case, force=True)
        # No row added because summarize_docket returned None.
        assert rows == []
