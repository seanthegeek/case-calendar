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
    _assign_document_refs,
    _entry_description_head,
    _entry_doc_text,
    _entry_document_url,
    _is_disposition_document,
    _resolve_document_links,
    find_primary_documents,
    find_primary_documents_for_group,
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
    @pytest.mark.parametrize(
        "description",
        [
            "INDICTMENT as to John Doe",
            "SUPERSEDING INDICTMENT (Count Three)",
            "SECOND AMENDED COMPLAINT for Damages",
            "INFORMATION",
            "Petition for Writ of Habeas Corpus",
            "COMPLAINT and Demand for Jury Trial",
        ],
    )
    def test_matches_primary_documents(self, description):
        assert is_primary_document({"description": description})

    @pytest.mark.parametrize(
        "description",
        [
            "Response to Motion to Dismiss the Indictment",
            "Notice of Appearance",
            "Order on Motion for Discovery",
            "",
        ],
    )
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
    @pytest.mark.parametrize(
        "description",
        [
            "JUDGMENT in a Criminal Case",
            "FINAL JUDGMENT",
            "VERDICT FORM",
            "ORDER OF DISMISSAL",
            "STIPULATION OF DISMISSAL",
            "NOTICE OF VOLUNTARY DISMISSAL",
            "PLEA AGREEMENT",
            "MEMORANDUM OPINION and Order",
            "OPINION AND ORDER on Motion to Suppress",
        ],
    )
    def test_matches_dispositions(self, description):
        assert is_disposition({"description": description})

    @pytest.mark.parametrize(
        "description",
        [
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
        ],
    )
    def test_matches_sentencing_keyword(self, description):
        assert is_disposition({"description": description})

    @pytest.mark.parametrize(
        "description",
        [
            # Any mention of judgment is treated as notable — Rule 50 motions,
            # judgments on the pleadings, amended judgments, judgment orders.
            "Motion for Judgment as a Matter of Law",
            "Motion for Judgment on the Pleadings",
            "ORDER denying Motion for Judgment as a Matter of Law",
            "Amended Judgment in a Criminal Case",
            "Notice of Filing of Judgments rendered against codefendants",
            # British spelling, in case it ever shows up.
            "Memorandum supporting Judgement on the Pleadings",
        ],
    )
    def test_matches_judgment_keyword(self, description):
        assert is_disposition({"description": description})

    @pytest.mark.parametrize(
        "description",
        [
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
        ],
    )
    def test_matches_tro_and_injunction_keywords(self, description):
        assert is_disposition({"description": description})

    @pytest.mark.parametrize(
        "description",
        [
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
        ],
    )
    def test_matches_extended_disposition_keywords(self, description):
        assert is_disposition({"description": description})

    @pytest.mark.parametrize(
        "description",
        [
            # Plea documents themselves — head-anchored, including the
            # "FACTUAL " variant clerks prefix in the S.D. Fla. and elsewhere.
            "FACTUAL PROFFER STATEMENT as to Angelo Martino",
            "PROFFER STATEMENT as to John Doe",
            # The magistrate's R&R after a change-of-plea colloquy. Other
            # R&Rs (suppression, 2255, IFP) must NOT match — covered below
            # in the negative test.
            "REPORT AND RECOMMENDATIONS on Plea of Guilty as to Angelo Martino",
            "REPORT AND RECOMMENDATION on Change of Plea",
            "AMENDED REPORT AND RECOMMENDATION on Plea of Guilty",
            # Paperless minute orders that record the plea event. Head is
            # "PAPERLESS Minute Order" / "Minute Order"; the keyword that
            # tips it into disposition territory is "pled guilty".
            "Minute Order for proceedings held before Magistrate Judge X: "
            "Change of Plea Hearing as to John Doe held on 4/14/2026. "
            "The defendant pled guilty to Count 1 of the Information.",
            "PAPERLESS Minute Order: defendant pleads guilty to Count 1",
        ],
    )
    def test_matches_plea_documents(self, description):
        assert is_disposition({"description": description})

    @pytest.mark.parametrize(
        "description",
        [
            # The arraignment phrasing must NOT trip the plea-of-guilty
            # keyword — defendants enter "not guilty" pleas at arraignment
            # all the time, and that is the opposite of a disposition.
            "NOT GUILTY PLEA entered as to John Doe",
            "Defendant entered a not guilty plea at arraignment",
            # Other R&Rs aren't plea R&Rs — they're rulings on procedural
            # motions and must not match. (The motions themselves do flip
            # the case_summaries row stale via other keywords; this is
            # specifically about the plea-R&R-only addition.)
            "REPORT AND RECOMMENDATIONS on Motion to Suppress",
            "REPORT AND RECOMMENDATION on 2255 motion",
            "REPORT AND RECOMMENDATIONS on Application to Proceed In Forma Pauperis",
            # Adoption of non-plea R&Rs must not slip into the plea
            # branch — the LLM would otherwise treat a discovery-sanctions
            # adoption as a disposition document.
            "PAPERLESS ORDER ADOPTING REPORT AND RECOMMENDATION on "
            "Application to Proceed In Forma Pauperis",
            "ORDER ADOPTING REPORT AND RECOMMENDATION on Discovery Dispute",
        ],
    )
    def test_plea_keywords_do_not_overmatch(self, description):
        assert not is_disposition({"description": description})

    @pytest.mark.parametrize(
        "description",
        [
            # Conference is the negative keyword — scheduling entries that
            # mention disposition vocabulary must NOT trip the keyword match.
            "Notice of Settlement Conference",
            "ORDER setting Telephonic Status Conference re: Sentencing",
            "Final Pretrial Conference held; further conference set",
            "ORDER scheduling Status Conference on Motion for Preliminary Injunction",
        ],
    )
    def test_conference_overrides_disposition_match(self, description):
        assert not is_disposition({"description": description})

    @pytest.mark.parametrize(
        "description",
        [
            "Notice of Filing of Plea Agreement Reply",
            "Reply in support of Motion to Dismiss",
            # No keyword anywhere — stay un-flagged.
            "Joint Status Report regarding discovery",
            "ORDER granting Motion to Compel Production",
        ],
    )
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

    @pytest.mark.parametrize(
        "description",
        [
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
            # Plea documents — head-anchored variants accepted directly.
            "FACTUAL PROFFER STATEMENT as to Angelo Martino",
            "PROFFER STATEMENT as to John Doe",
            "REPORT AND RECOMMENDATIONS on Plea of Guilty as to Angelo Martino",
            "AMENDED REPORT AND RECOMMENDATION on Change of Plea",
            # Trial-court adoption order — head is just ORDER, the
            # plea-specific R&R phrasing lives in the body.
            "PAPERLESS ORDER ADOPTING REPORT AND RECOMMENDATION. THIS "
            "CAUSE is before the Court on the Amended Report and "
            "Recommendation on Change of Plea issued by United States "
            "Magistrate Judge X.",
            # Civil judgment variants that previously fell through the
            # strict doc-head adjective slot.
            "CONSENT JUDGMENT entered as to all defendants",
            "DEFAULT JUDGMENT in favor of plaintiff",
            "CONSENT DECREE entered between the parties",
        ],
    )
    def test_accepts_actual_disposition_documents(self, description):
        assert _is_disposition_document({"description": description})

    @pytest.mark.parametrize(
        "description",
        [
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
            "ORDER scheduling Status Conference on Motion for Preliminary Injunction",
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
            "ORDER SETTING BRIEFING SCHEDULE on Motion for Summary Judgment",
            # Continuance orders / motions.
            "PAPERLESS ORDER granting Unopposed Motion to Continue "
            "Sentencing Hearing as to John Doe",
            "MOTION to Continue Trial Date",
            "",
        ],
    )
    def test_rejects_papers_and_non_dispositions(self, description):
        assert not _is_disposition_document({"description": description})


# ---------------------------------------------------------------------------
# find_primary_documents
# ---------------------------------------------------------------------------


class TestFindPrimaryDocuments:
    def test_returns_primary_and_disposition_lists(self):
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {"id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01"},
                    {
                        "id": 11,
                        "description": "Motion to Dismiss",
                        "date_filed": "2024-02-01",
                    },
                ],
                (1, "-date_filed"): [
                    {
                        "id": 99,
                        "description": "JUDGMENT in a Criminal Case",
                        "date_filed": "2025-06-15",
                    },
                ],
            }
        )
        primary, dispositions = find_primary_documents(cl, 1)
        assert [e["id"] for e in primary] == [10]
        assert [e["id"] for e in dispositions] == [99]

    def test_dedups_overlap_between_oldest_and_newest_pages(self):
        # Same entry appearing in both order_bys is folded to one row.
        same = {"id": 10, "description": "COMPLAINT", "date_filed": "2024-01-01"}
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [same],
                (1, "-date_filed"): [same],
            }
        )
        primary, _ = find_primary_documents(cl, 1)
        assert [e["id"] for e in primary] == [10]

    def test_sorts_oldest_first_within_each_group(self):
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 20,
                        "description": "SUPERSEDING INDICTMENT",
                        "date_filed": "2024-06-01",
                    },
                    {"id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01"},
                ],
                (1, "-date_filed"): [],
            }
        )
        primary, _ = find_primary_documents(cl, 1)
        assert [e["id"] for e in primary] == [10, 20]

    def test_local_store_short_circuits_cl_call(self, store):
        # Warm cache: sync has already persisted primary + disposition
        # entries on this docket. find_primary_documents must read them
        # from the store and never touch CourtListener — otherwise normal syncs burn
        # duplicate docket-entries calls right after sync wrote the data.
        store.mark_entry(
            1,
            10,
            "2024-01-01T00:00:00Z",
            "fp-op",
            date_filed="2024-01-01",
            entry_number=1,
            description="INDICTMENT",
            recap_documents=[{"id": 500, "plain_text": "indictment body"}],
        )
        store.mark_entry(
            1,
            99,
            "2025-06-15T00:00:00Z",
            "fp-disp",
            date_filed="2025-06-15",
            entry_number=37,
            description="JUDGMENT in a Criminal Case",
            recap_documents=[{"id": 600, "plain_text": "judgment body"}],
        )

        # CourtListener is wired to raise if called — proves the short-circuit hit.
        class _BoomCourtListener(_BoomCourtListenerBase):
            def _get(self, *a, **kw):
                raise AssertionError(
                    "CourtListener must not be called when local cache is warm"
                )

        primary, dispositions = find_primary_documents(
            _BoomCourtListener(), 1, store=store
        )
        assert [e["id"] for e in primary] == [10]
        assert [e["id"] for e in dispositions] == [99]
        # Recap document payload (with plain_text) is preserved end-to-end
        # so pdf.extract_text can short-circuit on it.
        assert primary[0]["recap_documents"][0]["plain_text"] == "indictment body"

    def test_cold_local_store_falls_back_to_cl(self, store):
        # No body-bearing entries cached — fall back to CourtListener (first sync,
        # or pre-fix data where primary/disp entries were stub-only).
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {"id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01"},
                ],
                (1, "-date_filed"): [
                    {
                        "id": 99,
                        "description": "JUDGMENT in a Criminal Case",
                        "date_filed": "2025-06-15",
                    },
                ],
            }
        )
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
            1,
            99,
            "2025-06-15T00:00:00Z",
            "fp-disp",
            date_filed="2025-06-15",
            entry_number=37,
            description="JUDGMENT in a Criminal Case",
            recap_documents=[{"id": 600, "plain_text": "judgment body"}],
        )
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "entry_number": 1,
                        "recap_documents": [
                            {"id": 500, "plain_text": "indictment body"}
                        ],
                    },
                ],
                (1, "-date_filed"): [
                    {
                        "id": 99,
                        "description": "JUDGMENT in a Criminal Case",
                        "date_filed": "2025-06-15",
                        "entry_number": 37,
                        "recap_documents": [{"id": 600, "plain_text": "judgment body"}],
                    },
                ],
            }
        )
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
            1,
            6,
            "2026-03-09T00:00:00Z",
            "fp-motion",
            date_filed="2026-03-09",
            entry_number=6,
            description=(
                "MOTION for Temporary Restraining Order, MOTION for "
                "Preliminary Injunction, MOTION to Stay Pursuant to "
                "Section 705 filed by Anthropic PBC."
            ),
            recap_documents=[{"id": 600}],
        )
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 1,
                        "description": "COMPLAINT for Declaratory and "
                        "Injunctive Relief",
                        "date_filed": "2026-03-09",
                        "entry_number": 1,
                    },
                ],
                (1, "-date_filed"): [
                    {
                        "id": 134,
                        "description": "ORDER GRANTING MOTION FOR "
                        "PRELIMINARY INJUNCTION 6",
                        "date_filed": "2026-03-26",
                        "entry_number": 134,
                    },
                    {
                        "id": 135,
                        "description": "PRELIMINARY INJUNCTION ORDER. "
                        "Signed by Judge Lin on 3/26/2026.",
                        "date_filed": "2026-03-26",
                        "entry_number": 135,
                    },
                ],
            }
        )
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
            1,
            42,
            "2024-01-01T00:00:00Z",
            "fp-stub",
            date_filed="2024-01-01",
            entry_number=2,
            description=None,  # filter-failed stub
        )
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {"id": 10, "description": "INDICTMENT", "date_filed": "2024-01-01"},
                ],
                (1, "-date_filed"): [],
            }
        )
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
            1,
            10,
            "2024-01-01T00:00:00Z",
            "fp-stale",
            date_filed="2024-01-01",
            entry_number=1,
            description="INDICTMENT",
            # Available main doc with NO plain_text — the staleness
            # signature.
            recap_documents=[
                {
                    "id": 500,
                    "document_number": "1",
                    "attachment_number": None,
                    "is_available": True,
                    "is_sealed": False,
                    "plain_text": None,
                }
            ],
        )
        fresh_indictment = {
            "id": 10,
            "description": "INDICTMENT",
            "date_filed": "2024-01-01",
            "entry_number": 1,
            "recap_documents": [
                {
                    "id": 500,
                    "document_number": "1",
                    "attachment_number": None,
                    "is_available": True,
                    "is_sealed": False,
                    "plain_text": "Body of indictment with 39k chars of text...",
                }
            ],
        }
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [fresh_indictment],
                (1, "-date_filed"): [fresh_indictment],
            }
        )
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
            1,
            10,
            "2024-01-01T00:00:00Z",
            "fp-sealed",
            date_filed="2024-01-01",
            entry_number=1,
            description="INDICTMENT",
            recap_documents=[
                {
                    "id": 500,
                    "document_number": "1",
                    "attachment_number": None,
                    "is_available": False,  # not on RECAP
                    "is_sealed": False,
                    "plain_text": None,
                }
            ],
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
            1,
            10,
            "2024-01-01T00:00:00Z",
            "fp-sealed-main",
            date_filed="2024-01-01",
            entry_number=1,
            description="INDICTMENT",
            recap_documents=[
                {
                    "id": 500,
                    "document_number": "1",
                    "attachment_number": None,
                    "is_available": True,
                    "is_sealed": True,
                    "plain_text": None,
                }
            ],
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
            1,
            10,
            "2024-01-01T00:00:00Z",
            "fp-attach",
            date_filed="2024-01-01",
            entry_number=1,
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

    def test_stale_disposition_falls_through_and_refreshes_cache(self, store):
        # Symmetric to the stale-primary test, but the empty plain_text
        # is on a DISPOSITION (judgment). The staleness detector covers
        # both classifications; the disposition's cached row must get
        # rewritten with the fresh CourtListener data so subsequent
        # summary calls short-circuit.
        store.mark_entry(
            1,
            20,
            "2024-06-01T00:00:00Z",
            "fp-fresh-prim",
            date_filed="2024-01-01",
            entry_number=1,
            description="INDICTMENT",
            recap_documents=[
                {
                    "id": 500,
                    "document_number": "1",
                    "attachment_number": None,
                    "is_available": True,
                    "is_sealed": False,
                    "plain_text": "Indictment body text — non-empty.",
                }
            ],
        )
        store.mark_entry(
            1,
            30,
            "2024-06-01T00:00:00Z",
            "fp-stale-disp",
            date_filed="2024-06-01",
            entry_number=99,
            description="JUDGMENT in a Criminal Case",
            # Stale: available main doc with no plain_text.
            recap_documents=[
                {
                    "id": 600,
                    "document_number": "99",
                    "attachment_number": None,
                    "is_available": True,
                    "is_sealed": False,
                    "plain_text": None,
                }
            ],
        )
        fresh_indictment = {
            "id": 20,
            "description": "INDICTMENT",
            "date_filed": "2024-01-01",
            "entry_number": 1,
            "recap_documents": [
                {
                    "id": 500,
                    "document_number": "1",
                    "attachment_number": None,
                    "is_available": True,
                    "is_sealed": False,
                    "plain_text": "Indictment body text — non-empty.",
                }
            ],
        }
        fresh_judgment = {
            "id": 30,
            "description": "JUDGMENT in a Criminal Case",
            "date_filed": "2024-06-01",
            "entry_number": 99,
            "recap_documents": [
                {
                    "id": 600,
                    "document_number": "99",
                    "attachment_number": None,
                    "is_available": True,
                    "is_sealed": False,
                    "plain_text": "Defendant sentenced to 60 months.",
                }
            ],
        }
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [fresh_indictment, fresh_judgment],
                (1, "-date_filed"): [fresh_judgment, fresh_indictment],
            }
        )
        primary, dispositions = find_primary_documents(cl, 1, store=store)
        assert [e["id"] for e in primary] == [20]
        assert [e["id"] for e in dispositions] == [30]
        # The disposition's local cache got rewritten with the fresh
        # plain_text — next summary call would short-circuit.
        refreshed = store.get_entries_with_body(1)
        judgment = next(e for e in refreshed if e["id"] == 30)
        assert (
            judgment["recap_documents"][0]["plain_text"]
            == "Defendant sentenced to 60 months."
        )


class TestEntryMainDocHasPlainText:
    """Direct unit tests for the helper used by the group-dedup upgrade
    rule. Same shape as the staleness detector but checks for text
    presence rather than its absence."""

    def test_returns_true_when_main_doc_has_text(self):
        from case_calendar.summary import _entry_main_doc_has_plain_text

        entry = {
            "recap_documents": [
                {"attachment_number": None, "plain_text": "real text"},
            ]
        }
        assert _entry_main_doc_has_plain_text(entry) is True

    def test_returns_false_when_main_doc_is_empty(self):
        from case_calendar.summary import _entry_main_doc_has_plain_text

        entry = {
            "recap_documents": [
                {"attachment_number": None, "plain_text": ""},
            ]
        }
        assert _entry_main_doc_has_plain_text(entry) is False

    def test_attachment_with_text_does_not_count(self):
        # Attachments are skipped — the dedup decision is about the
        # main document body, not exhibit text. An entry whose only
        # populated text is on an attachment shouldn't outrank an
        # entry whose main doc has text elsewhere.
        from case_calendar.summary import _entry_main_doc_has_plain_text

        entry = {
            "recap_documents": [
                {"attachment_number": None, "plain_text": ""},
                {"attachment_number": 1, "plain_text": "exhibit text"},
            ]
        }
        assert _entry_main_doc_has_plain_text(entry) is False

    def test_entry_without_recap_documents_returns_false(self):
        from case_calendar.summary import _entry_main_doc_has_plain_text

        assert _entry_main_doc_has_plain_text({}) is False
        assert _entry_main_doc_has_plain_text({"recap_documents": None}) is False


class TestFindPrimaryDocumentsForGroup:
    """Pool primary documents and dispositions across every CourtListener docket_id in
    a (docket_number, court_id) group. The canonical case is Akhter
    (1:25-cr-00307, E.D. Va.): three CourtListener docket_ids each carry a partial,
    non-overlapping slice of the PACER entries — only the pool sees the
    full picture.
    """

    def test_pools_non_overlapping_entries(self):
        # Two CourtListener docket_ids in the same group, each holding a different
        # entry. The group view shows both.
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2025-11-13",
                        "entry_number": 1,
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
                (2, "date_filed"): [
                    {
                        "id": 99,
                        "description": "JUDGMENT in a Criminal Case",
                        "date_filed": "2026-04-15",
                        "entry_number": 120,
                        "recap_documents": [{"id": 600}],
                    }
                ],
                (2, "-date_filed"): [],
            }
        )
        primary, dispositions = find_primary_documents_for_group(cl, [1, 2])
        assert [e["id"] for e in primary] == [10]
        assert [e["id"] for e in dispositions] == [99]

    def test_dedupes_by_entry_number(self):
        # Same logical PACER entry (entry_number=1) on two CourtListener siblings
        # under DIFFERENT CourtListener ids. Pool returns it ONCE — first-seen wins.
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2025-11-13",
                        "entry_number": 1,
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
                (2, "date_filed"): [
                    {
                        "id": 999,  # DIFFERENT CourtListener id, SAME PACER entry_number
                        "description": "INDICTMENT",
                        "date_filed": "2025-11-13",
                        "entry_number": 1,
                        "recap_documents": [{"id": 501}],
                    }
                ],
                (2, "-date_filed"): [],
            }
        )
        primary, _ = find_primary_documents_for_group(cl, [1, 2])
        # ONE entry — fresh CourtListener docket (id 1, walked first) wins.
        assert len(primary) == 1
        assert primary[0]["id"] == 10

    def test_paperless_dedup_uses_description_and_date(self):
        # Paperless entries have null entry_number. Dedup falls back to
        # (date_filed, description prefix) — same logical event on two CourtListener
        # siblings collapses to one.
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "PAPERLESS Minute Order: defendant pleads guilty",
                        "date_filed": "2026-04-30",
                        "entry_number": None,
                    }
                ],
                (1, "-date_filed"): [],
                (2, "date_filed"): [
                    {
                        "id": 999,
                        "description": "PAPERLESS Minute Order: defendant pleads guilty",
                        "date_filed": "2026-04-30",
                        "entry_number": None,
                    }
                ],
                (2, "-date_filed"): [],
            }
        )
        # "pleads guilty" matches the disposition keyword regex.
        _, dispositions = find_primary_documents_for_group(cl, [1, 2])
        assert len(dispositions) == 1

    def test_empty_group_returns_empty_lists(self):
        cl = _FakeCourtListener({})
        primary, dispositions = find_primary_documents_for_group(cl, [])
        assert primary == []
        assert dispositions == []

    def test_later_sibling_with_plain_text_replaces_earlier_empty(self):
        # The us-v-schmitz regression: the freshest CourtListener sibling carries
        # the indictment recap_document with an EMPTY plain_text while
        # an older sibling has the same logical entry populated. The
        # dedup must pick the populated copy so the summary LLM gets
        # the document body, not just the metadata.
        cl = _FakeCourtListener(
            {
                # First CourtListener docket walked — entry #1 with empty plain_text.
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT as to defendant",
                        "date_filed": "2024-04-03",
                        "entry_number": 1,
                        "recap_documents": [
                            {
                                "id": 500,
                                "document_number": "1",
                                "is_available": True,
                                "is_sealed": False,
                                "plain_text": "",  # empty!
                                "filepath_ia": "https://archive.org/a.pdf",
                            }
                        ],
                    }
                ],
                (1, "-date_filed"): [],
                # Second CourtListener docket walked — same logical entry, populated.
                (2, "date_filed"): [
                    {
                        "id": 999,
                        "description": "INDICTMENT as to defendant",
                        "date_filed": "2024-04-03",
                        "entry_number": 1,
                        "recap_documents": [
                            {
                                "id": 501,
                                "document_number": "1",
                                "is_available": True,
                                "is_sealed": False,
                                "plain_text": "Full text of the indictment.",
                                "filepath_ia": "https://archive.org/b.pdf",
                            }
                        ],
                    }
                ],
                (2, "-date_filed"): [],
            }
        )
        primary, _ = find_primary_documents_for_group(cl, [1, 2])
        assert len(primary) == 1
        # Second sibling's copy wins because it has plain_text on the
        # main recap_document.
        assert primary[0]["id"] == 999
        assert (
            primary[0]["recap_documents"][0]["plain_text"]
            == "Full text of the indictment."
        )

    def test_first_seen_wins_when_both_copies_have_plain_text(self):
        # The original first-seen-wins rule still applies when neither
        # sibling has an edge on data completeness. The freshest CourtListener
        # docket_id is walked first; its copy wins.
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT as to defendant",
                        "date_filed": "2024-04-03",
                        "entry_number": 1,
                        "recap_documents": [
                            {
                                "id": 500,
                                "document_number": "1",
                                "is_available": True,
                                "plain_text": "Fresh copy text.",
                            }
                        ],
                    }
                ],
                (1, "-date_filed"): [],
                (2, "date_filed"): [
                    {
                        "id": 999,
                        "description": "INDICTMENT as to defendant",
                        "date_filed": "2024-04-03",
                        "entry_number": 1,
                        "recap_documents": [
                            {
                                "id": 501,
                                "document_number": "1",
                                "is_available": True,
                                "plain_text": "Older copy text.",
                            }
                        ],
                    }
                ],
                (2, "-date_filed"): [],
            }
        )
        primary, _ = find_primary_documents_for_group(cl, [1, 2])
        assert len(primary) == 1
        assert primary[0]["id"] == 10  # first-seen wins

    def test_first_seen_wins_when_both_copies_have_empty_plain_text(self):
        # If neither sibling has plain_text, the upgrade rule has no
        # signal to act on — first-seen still wins (no churn between
        # equally-empty copies).
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT as to defendant",
                        "date_filed": "2024-04-03",
                        "entry_number": 1,
                        "recap_documents": [
                            {"id": 500, "is_available": True, "plain_text": ""}
                        ],
                    }
                ],
                (1, "-date_filed"): [],
                (2, "date_filed"): [
                    {
                        "id": 999,
                        "description": "INDICTMENT as to defendant",
                        "date_filed": "2024-04-03",
                        "entry_number": 1,
                        "recap_documents": [
                            {"id": 501, "is_available": True, "plain_text": ""}
                        ],
                    }
                ],
                (2, "-date_filed"): [],
            }
        )
        primary, _ = find_primary_documents_for_group(cl, [1, 2])
        assert len(primary) == 1
        assert primary[0]["id"] == 10  # first-seen wins


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
                    "id": 1,
                    "entry_number": 33,
                    "date_filed": "2025-08-21",
                    "description": "FIRST SUPERSEDING INDICTMENT filed as to ...",
                    "recap_documents": [{"is_available": True}],
                },
                {
                    "id": 2,
                    "entry_number": 43,
                    "date_filed": "2025-08-21",
                    "description": "EX PARTE APPLICATION to Seal Indictment and Related Documents Filed by Plaintiff USA",
                    "recap_documents": [{"is_available": False}],
                },
                {
                    "id": 3,
                    "entry_number": 44,
                    "date_filed": "2025-08-21",
                    "description": "ORDER by Magistrate Judge Steve Kim granting 43 EX PARTE APPLICATION to Seal Indictment and Related Documents",
                    "recap_documents": [{"is_available": False}],
                },
                {
                    "id": 4,
                    "entry_number": 45,
                    "date_filed": "2025-08-21",
                    "description": "CASE SUMMARY filed by AUSA as to Defendant Dubranova",
                    "recap_documents": [{"is_available": True}],
                },
                {
                    "id": 5,
                    "entry_number": 54,
                    "date_filed": "2025-08-21",
                    "description": "NOTICE OF REQUEST FOR DETENTION as to Dubranova",
                    "recap_documents": [{"is_available": False}],
                },
                {
                    "id": 6,
                    "entry_number": 32,
                    "date_filed": "2025-08-28",
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
        assert (
            "granting 43 EX PARTE APPLICATION to Seal" in result["sealing_description"]
        )

    def test_unsealing_order_kills_signal(self):
        pages = self._dubranova_shape()
        # Add an unsealing order DATED AFTER the sealing order. The dates
        # in the Dubranova shape are all 2025-08-21; bump the unsealing
        # entry to 2025-09-15 so the post-seal check fires correctly.
        pages[(72013021, "date_filed")].append(
            {
                "id": 99,
                "entry_number": 80,
                "date_filed": "2025-09-15",
                "description": "ORDER by Magistrate Judge granting 78 MOTION to Unseal Indictment",
                "recap_documents": [{"is_available": True}],
            }
        )
        cl = _FakeCourtListener(pages)
        assert summary.detect_sealing(cl, 72013021, dispositions=[]) is None

    def test_disposition_presence_kills_signal_without_an_api_call(self):
        # When a disposition is in the docket, the dispositive ruling
        # landed publicly. Don't bother walking — just refuse to flag.
        # Also asserts we make zero CourtListener calls in this short-circuit path.
        cl = _FakeCourtListener(self._dubranova_shape())
        result = summary.detect_sealing(
            cl,
            72013021,
            dispositions=[{"id": 99, "description": "JUDGMENT"}],
        )
        assert result is None
        assert cl.calls == []

    def test_substantial_post_seal_public_activity_kills_signal(self):
        pages = self._dubranova_shape()
        # Add 4 publicly-available entries dated AFTER the sealing order
        # — that's above the default threshold of 3, so the seal is
        # functionally lifted even without an explicit unsealing entry.
        for i, day in enumerate(
            ("2025-09-01", "2025-09-15", "2025-10-01", "2025-10-15")
        ):
            pages[(72013021, "date_filed")].append(
                {
                    "id": 100 + i,
                    "entry_number": 60 + i,
                    "date_filed": day,
                    "description": f"Status Conference {i + 1}",
                    "recap_documents": [{"is_available": True}],
                }
            )
        cl = _FakeCourtListener(pages)
        assert summary.detect_sealing(cl, 72013021, dispositions=[]) is None

    def test_no_sealing_order_returns_none(self):
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 1,
                        "entry_number": 1,
                        "date_filed": "2024-01-01",
                        "description": "INDICTMENT",
                        "recap_documents": [{"is_available": True}],
                    },
                    {
                        "id": 2,
                        "entry_number": 2,
                        "date_filed": "2024-02-01",
                        "description": "MINUTE ENTRY for arraignment",
                        "recap_documents": [{"is_available": True}],
                    },
                ],
                (1, "-date_filed"): [],
            }
        )
        assert summary.detect_sealing(cl, 1, dispositions=[]) is None

    def test_narrow_sealing_order_with_high_public_activity_does_not_trigger(self):
        # A "Seal Plea Agreement" order is narrow scope; combined with
        # plenty of publicly-available post-sealing activity, this should
        # NOT flag the docket as currently sealed.
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 1,
                        "entry_number": 1,
                        "date_filed": "2024-01-01",
                        "description": "INDICTMENT",
                        "recap_documents": [{"is_available": True}],
                    },
                    {
                        "id": 2,
                        "entry_number": 30,
                        "date_filed": "2024-05-01",
                        "description": "ORDER granting Motion to Seal Plea Agreement",
                        "recap_documents": [{"is_available": True}],
                    },
                    {
                        "id": 3,
                        "entry_number": 31,
                        "date_filed": "2024-06-01",
                        "description": "Sentencing hearing held",
                        "recap_documents": [{"is_available": True}],
                    },
                    {
                        "id": 4,
                        "entry_number": 32,
                        "date_filed": "2024-06-02",
                        "description": "Status Conference",
                        "recap_documents": [{"is_available": True}],
                    },
                    {
                        "id": 5,
                        "entry_number": 33,
                        "date_filed": "2024-06-15",
                        "description": "Notice of Appeal",
                        "recap_documents": [{"is_available": True}],
                    },
                    {
                        "id": 6,
                        "entry_number": 34,
                        "date_filed": "2024-07-01",
                        "description": "Minute Entry",
                        "recap_documents": [{"is_available": True}],
                    },
                ],
                (1, "-date_filed"): [],
            }
        )
        assert summary.detect_sealing(cl, 1, dispositions=[]) is None

    def test_latest_sealing_order_is_the_operative_one(self):
        # If a docket has two granted sealing orders (e.g., a narrow
        # earlier seal followed by a broader later one), the advisory
        # should reference the LATER one — that's the one currently in
        # effect on the visible public docket.
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 1,
                        "entry_number": 5,
                        "date_filed": "2024-01-01",
                        "description": "ORDER granting Motion to Seal Exhibit A",
                        "recap_documents": [{"is_available": False}],
                    },
                    {
                        "id": 2,
                        "entry_number": 20,
                        "date_filed": "2024-06-01",
                        "description": "ORDER granting Motion to Seal Case",
                        "recap_documents": [{"is_available": False}],
                    },
                ],
                (1, "-date_filed"): [],
            }
        )
        result = summary.detect_sealing(cl, 1, dispositions=[])
        assert result is not None
        assert result["sealing_entry_number"] == 20
        assert result["sealing_date_filed"] == "2024-06-01"

    def test_description_is_truncated(self):
        long_desc = (
            "ORDER by Judge X granting 42 MOTION to Seal Indictment "
            + "and Related Documents " * 30
        )
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 1,
                        "entry_number": 1,
                        "date_filed": "2024-01-01",
                        "description": long_desc,
                        "recap_documents": [{"is_available": False}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
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
            "id": 44,
            "entry_number": 44,
            "date_filed": "2025-08-21",
            "description": "ORDER granting 43 EX PARTE APPLICATION to Seal Indictment",
            "recap_documents": [{"is_available": False}],
        }
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [seal_order],
                (1, "-date_filed"): [seal_order],  # same row, returned by both walks
            }
        )
        result = summary.detect_sealing(cl, 1, dispositions=[])
        assert result is not None
        assert result["sealing_entry_number"] == 44

    def test_earlier_sealing_order_does_not_displace_an_already_later_one(self):
        # The latest-sealing-order picker iterates in walk order and
        # updates `sealing_order` only when the candidate has a strictly
        # later date. Cover the case where the first match is already
        # the latest and subsequent matches don't displace it (the
        # if-branch goes False).
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 1,
                        "entry_number": 20,
                        "date_filed": "2024-06-01",
                        "description": "ORDER granting Motion to Seal Indictment",
                        "recap_documents": [{"is_available": False}],
                    },
                    {
                        "id": 2,
                        "entry_number": 5,
                        "date_filed": "2024-01-01",
                        "description": "ORDER granting Motion to Seal Exhibit A",
                        "recap_documents": [{"is_available": False}],
                    },
                ],
                (1, "-date_filed"): [],
            }
        )
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
    store.upsert_docket_meta(
        docket_id,
        {
            "docket_number": docket_number,
            "case_name": "United States v. Doe",
            "court_id": court_id,
            "absolute_url": f"/docket/{docket_id}/foo/",
        },
    )
    store.upsert_court(
        court_id, "D.D.C.", "DDC", "U.S. District Court for the District of Columbia"
    )


# Default group key used by tests that call _seed_docket_meta(store, X) with no
# overrides — the (docket_number, court_id) the resulting case_summaries row is
# keyed by post-grouping refactor.
_DEFAULT_GROUP = ("1:24-cr-100", "dcd")


class TestEntryDocumentUrl:
    """Link target for an entry's inline citation — mirrors _entry_doc_text
    priority (substance-marked, then main, then attachment), skips sealed."""

    def test_substance_marked_doc_wins_over_main(self, make_recap_doc):
        # An indictment filed as an attachment to a procedural parent: the
        # link points at the doc that IS the indictment, not the parent's
        # boilerplate main doc that happens to be listed first.
        entry = {
            "recap_documents": [
                make_recap_doc(
                    doc_id=1,
                    description="Notice of Filing",
                    filepath_ia="https://ia/parent.pdf",
                ),
                make_recap_doc(
                    doc_id=2,
                    description="Indictment",
                    filepath_ia="https://ia/indictment.pdf",
                ),
            ]
        }
        assert _entry_document_url(entry) == "https://ia/indictment.pdf"

    def test_skips_sealed_document(self, make_recap_doc):
        entry = {
            "recap_documents": [
                make_recap_doc(
                    doc_id=1, is_sealed=True, filepath_ia="https://ia/sealed.pdf"
                ),
                make_recap_doc(doc_id=2, filepath_ia="https://ia/ok.pdf"),
            ]
        }
        assert _entry_document_url(entry) == "https://ia/ok.pdf"

    def test_main_doc_storage_fallback(self, make_recap_doc):
        entry = {
            "recap_documents": [make_recap_doc(doc_id=1, filepath_local="recap/x.pdf")]
        }
        assert (
            _entry_document_url(entry)
            == "https://storage.courtlistener.com/recap/x.pdf"
        )

    def test_none_when_no_reachable_url(self, make_recap_doc):
        assert (
            _entry_document_url({"recap_documents": [make_recap_doc(doc_id=1)]}) is None
        )
        assert _entry_document_url({"recap_documents": []}) is None


class TestAssignDocumentRefs:
    def test_sequential_tokens_across_lists(self):
        primary = [{"url": "https://ia/ind.pdf"}]
        disp = [{"url": "https://ia/judg.pdf"}, {"url": None}]
        extras = [{"source_url": "https://op/doc.pdf"}]
        link_map = _assign_document_refs(primary, disp, extras)
        # Tokens run D1.. in render order (primary, then disposition, then extra).
        assert primary[0]["ref"] == "D1"
        assert disp[0]["ref"] == "D2"
        assert disp[1]["ref"] == "D3"
        assert extras[0]["ref"] == "D4"
        # The map keys EVERY assigned ref; a URL-less doc maps to None so the
        # resolver can tell "known but unreachable" from "invented token".
        assert link_map == {
            "D1": "https://ia/ind.pdf",
            "D2": "https://ia/judg.pdf",
            "D3": None,
            "D4": "https://op/doc.pdf",
        }

    def test_empty_lists_produce_empty_map(self):
        assert _assign_document_refs([], [], []) == {}

    def test_verdict_form_is_not_a_link_target(self):
        # A "Verdict Form" doc still gets a ref (it rides into the prompt as
        # context) but maps to None even though it has a URL, so the model
        # can't turn "returned a verdict" into a link to a checkbox template.
        primary = [{"url": "https://ia/ind.pdf", "description": "INDICTMENT"}]
        disp = [
            {
                "url": "https://ia/verdict.pdf",
                "description": "Verdict Form. Signed by Judge ...",
            }
        ]
        link_map = _assign_document_refs(primary, disp)
        assert link_map["D1"] == "https://ia/ind.pdf"
        assert link_map["D2"] is None  # verdict form: known ref, no link
        assert disp[0]["ref"] == "D2"

    def test_verdict_form_marker_drops_to_plain_text(self):
        # End-to-end through the resolver: a verdict-form ref (url None) makes
        # "returned a verdict" render as plain prose.
        link_map = _assign_document_refs(
            [
                {
                    "url": "https://ia/verdict.pdf",
                    "description": "Verdict Form. Signed ...",
                }
            ]
        )
        out = _resolve_document_links(
            "The jury [returned a verdict](doc:D1) on all counts.", link_map
        )
        assert out == "The jury returned a verdict on all counts."

    def test_completed_verdict_minute_entry_is_still_linkable(self):
        # Only "Verdict Form" is excluded; a result-bearing "Jury Verdict"
        # minute entry / judgment is not, so it keeps its URL.
        link_map = _assign_document_refs(
            [{"url": "https://ia/judgment.pdf", "description": "JUDGMENT as to X"}]
        )
        assert link_map["D1"] == "https://ia/judgment.pdf"


class TestResolveDocumentLinks:
    def test_known_token_with_url_becomes_link(self):
        # Clean span (verb already inside, no trailing preposition) so this
        # isolates token->URL resolution from the span-tidy below.
        out = _resolve_document_links(
            "The defendants [were charged](doc:D1) in NY.",
            {"D1": "https://ia/ind.pdf"},
        )
        assert out == "The defendants [were charged](https://ia/ind.pdf) in NY."

    def test_known_token_without_url_drops_to_bare_words(self):
        out = _resolve_document_links("He [pled guilty](doc:D2) today.", {"D2": None})
        assert out == "He pled guilty today."

    def test_unknown_token_drops_to_bare_words_and_warns(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="case_calendar.summary"):
            out = _resolve_document_links(
                "She was [sentenced to 60 months](doc:D9) on Friday.",
                {"D1": "https://ia/x.pdf"},
            )
        assert out == "She was sentenced to 60 months on Friday."
        assert "unknown document token" in caplog.text

    def test_plain_parentheticals_and_brackets_untouched(self):
        text = "Filed in 2024 (see also Smith) [sic] with no links."
        assert _resolve_document_links(text, {}) == text

    def test_leading_auxiliary_verb_pulled_into_link(self):
        # "was [charged]" -> "[was charged]" so the verb is part of the link.
        out = _resolve_document_links(
            "He was [charged](doc:D1) with fraud.", {"D1": "https://ia/x.pdf"}
        )
        assert out == "He [was charged](https://ia/x.pdf) with fraud."

    def test_compound_auxiliary_fully_absorbed(self):
        out = _resolve_document_links(
            "She has been [charged](doc:D1).", {"D1": "https://ia/x.pdf"}
        )
        assert out == "She [has been charged](https://ia/x.pdf)."

    def test_trailing_preposition_pushed_out_of_link(self):
        # "[convicted at trial of]" -> "[convicted at trial] of" — the prep
        # that introduces the charges stays plain.
        out = _resolve_document_links(
            "He [was convicted at trial of](doc:D1) all counts.",
            {"D1": "https://ia/x.pdf"},
        )
        assert out == "He [was convicted at trial](https://ia/x.pdf) of all counts."

    def test_verb_in_and_preposition_out_together(self):
        out = _resolve_document_links(
            "He was [charged with](doc:D1) wire fraud.", {"D1": "https://ia/x.pdf"}
        )
        assert out == "He [was charged](https://ia/x.pdf) with wire fraud."

    def test_non_auxiliary_preceding_word_left_alone(self):
        # "the court [sentenced him]" — "court" is not an auxiliary verb, and
        # the anchor doesn't end in a preposition, so the span is unchanged.
        out = _resolve_document_links(
            "The court [sentenced him](doc:D1) to 60 months.",
            {"D1": "https://ia/x.pdf"},
        )
        assert out == "The court [sentenced him](https://ia/x.pdf) to 60 months."


class TestSummarizeDocket:
    def test_returns_none_when_docket_metadata_missing(
        self, store, patch_llm, patch_pdf
    ):
        # No upsert_docket_meta — the docket_id has no row in the `dockets`
        # table, so we can't resolve to a (docket_number, court_id) group.
        # summarize_docket bails with a warning rather than trying to write
        # a row keyed by null.
        cl = _FakeCourtListener({})
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )
        assert summarize_docket(cl=cl, store=store, case=case, docket_id=1) is None
        assert patch_llm == []

    def test_pools_entries_across_cl_docket_group(self, store, patch_llm, patch_pdf):
        # The canonical Akhter case: three CourtListener docket_ids share the same
        # (docket_number, court_id) group. Each carries a partial slice of
        # the entries — the indictment on one, the judgment on another.
        # summarize_docket(docket_id=N) for ANY N in the group pools all
        # three slices and writes ONE summary keyed by the group.
        for did, court_id, docket_number in [
            (71989485, "vaed", "1:25-cr-00307"),
            (73333500, "vaed", "1:25-cr-00307"),
            (73320754, "vaed", "1:25-cr-00307"),
        ]:
            _seed_docket_meta(
                store, did, docket_number=docket_number, court_id=court_id
            )
        patch_pdf["texts"] = {500: "INDICTMENT body", 600: "JUDGMENT body"}
        cl = _FakeCourtListener(
            {
                # CourtListener docket 71989485 has the indictment (early entries).
                (71989485, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2025-11-13",
                        "entry_number": 1,
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (71989485, "-date_filed"): [],
                # CourtListener docket 73333500 has the judgment (later entries).
                (73333500, "date_filed"): [],
                (73333500, "-date_filed"): [
                    {
                        "id": 99,
                        "description": "JUDGMENT in a Criminal Case",
                        "date_filed": "2026-05-15",
                        "entry_number": 136,
                        "recap_documents": [{"id": 600}],
                    }
                ],
                (73320754, "date_filed"): [],
                (73320754, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-akhter",
            name="US v. Akhter",
            dockets=[71989485, 73333500, 73320754],
            calendar="cyber",
        )
        # Call with ANY docket_id in the group — should produce one summary.
        row = summarize_docket(cl=cl, store=store, case=case, docket_id=71989485)
        assert row is not None
        assert row["docket_number"] == "1:25-cr-00307"
        assert row["court_id"] == "vaed"
        # The LLM received BOTH the indictment and the judgment, drawn
        # from two different CourtListener siblings.
        assert len(patch_llm) == 1
        call = patch_llm[0]
        primary_descs = [p["description"] for p in call["primary_documents"]]
        disp_descs = [d["description"] for d in call["disposition_documents"]]
        assert "INDICTMENT" in primary_descs
        assert any("JUDGMENT" in d for d in disp_descs)
        # Persisted as ONE row keyed by the group, not three.
        rows = store.get_case_summaries("us-v-akhter")
        assert len(rows) == 1

    def test_writes_summary_when_primary_text_available(
        self, store, patch_llm, patch_pdf
    ):
        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {500: "INDICTMENT body text..."}
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "entry_number": 1,
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )

        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)

        assert row is not None
        assert row["summary"] == "A two-sentence summary of the matter."
        assert row["model"] == "fake/model-v1"
        # Persisted to the store under the LOGICAL PACER docket key.
        persisted = store.get_docket_summary("us-v-doe", *_DEFAULT_GROUP)
        assert persisted["summary"] == row["summary"]
        # LLM received the expected scaffold.
        assert len(patch_llm) == 1
        call = patch_llm[0]
        assert call["case_name"] == "US v. Doe"
        assert call["docket"]["court_citation"] == "D.D.C."
        assert call["docket"]["court_tz"] is not None
        assert [d["entry_id"] for d in call["primary_documents"]] == [10]

    def test_inline_links_resolved_in_stored_summary(
        self, store, patch_pdf, monkeypatch
    ):
        # The model links an action phrase to the indictment by its prompt
        # token (D1); summarize_docket resolves the token to the document's
        # real URL before persisting, so the stored prose carries the link
        # on the words themselves.
        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {500: "INDICTMENT body text..."}

        captured: dict[str, Any] = {}

        def _fake(**kwargs):
            captured.update(kwargs)
            # Short action phrase linked; the trailing detail stays plain.
            return (
                "The defendants [were charged](doc:D1) with wire fraud.",
                "fake/model-v1",
            )

        monkeypatch.setattr(summary.llm, "generate_docket_summary", _fake)
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "entry_number": 1,
                        "recap_documents": [
                            {
                                "id": 500,
                                "is_available": True,
                                "filepath_ia": "https://ia/ind.pdf",
                            }
                        ],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )

        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)

        assert row is not None
        assert row["summary"] == (
            "The defendants [were charged](https://ia/ind.pdf) with wire fraud."
        )
        persisted = store.get_docket_summary("us-v-doe", *_DEFAULT_GROUP)
        assert "https://ia/ind.pdf" in persisted["summary"]
        # The primary doc was shown to the LLM with a D1 token and its URL,
        # so the token the model emitted resolves.
        assert captured["primary_documents"][0]["ref"] == "D1"
        assert captured["primary_documents"][0]["url"] == "https://ia/ind.pdf"

    def test_writes_not_available_message_when_primary_has_no_source(
        self, store, patch_llm, patch_pdf, caplog
    ):
        # us-v-lytvynenko shape: CourtListener returned a primary entry
        # whose main recap_doc has NO source for text — no filepath_ia,
        # no filepath_local, no plain_text, not sealed. Nothing for the
        # extraction chain to fetch. Subscribers see the specific
        # SUMMARY_PRIMARY_DOCUMENT_NOT_AVAILABLE message ("the primary
        # document(s) are not yet available on RECAP"), distinct from
        # the "sealed" and "could not be read" sibling messages.
        from case_calendar.llm import SUMMARY_PRIMARY_DOCUMENT_NOT_AVAILABLE

        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {}
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        # No filepath_ia, no filepath_local, no plain_text —
                        # truly no source. Maps to "not-available".
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )

        import logging

        with caplog.at_level(logging.WARNING, logger="case_calendar.summary"):
            row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)

        # LLM is NOT called — we synthesize the message directly.
        assert patch_llm == []
        # Row IS returned and persisted with the "not yet available" message.
        assert row is not None
        assert row["summary"] == SUMMARY_PRIMARY_DOCUMENT_NOT_AVAILABLE
        # source_entry_ids preserves the entry we tried to summarize from
        # so the audit trail captures it.
        assert row["source_entry_ids"] == [10]
        persisted = store.get_docket_summary("us-v-doe", *_DEFAULT_GROUP)
        assert persisted["summary"] == SUMMARY_PRIMARY_DOCUMENT_NOT_AVAILABLE
        # WARN fired with the per-doc breakdown showing not-available=1.
        assert any(
            "not-available=1" in r.message and "docket 1" in r.message
            for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_writes_sealed_message_when_primary_is_sealed(
        self, store, patch_llm, patch_pdf, caplog
    ):
        # Primary entry whose main recap_doc has is_sealed=True. PACER
        # blocks access; subscribers see the specific "currently sealed"
        # message so they know it's a legal-posture wait, not a pipeline
        # gap.
        from case_calendar.llm import SUMMARY_PRIMARY_DOCUMENT_SEALED

        _seed_docket_meta(store, 1)
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [
                            {
                                "id": 500,
                                "attachment_number": None,
                                "is_sealed": True,
                            }
                        ],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )

        import logging

        with caplog.at_level(logging.WARNING, logger="case_calendar.summary"):
            row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)

        assert patch_llm == []
        assert row is not None
        assert row["summary"] == SUMMARY_PRIMARY_DOCUMENT_SEALED
        # WARN breakdown shows sealed=1.
        assert any(
            "sealed=1" in r.message and "docket 1" in r.message for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_writes_unreadable_message_when_fetch_returns_no_usable_text(
        self, store, patch_llm, patch_pdf, caplog
    ):
        # Primary entry's main recap_doc had a fetchable URL (so it's
        # neither sealed nor not-available), but the extraction chain
        # couldn't produce usable text — typically an image-only PDF on
        # a host without OCR tools installed, or a fetch that 4xx'd
        # across all URLs. Subscribers see the "could not be read"
        # catch-all message.
        from case_calendar.llm import SUMMARY_PRIMARY_DOCUMENT_UNREADABLE

        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {}  # PDF text extracts to empty
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [
                            {
                                "id": 500,
                                "attachment_number": None,
                                "is_sealed": False,
                                "filepath_local": "recap/x.pdf",  # has URL
                            }
                        ],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )

        import logging

        with caplog.at_level(logging.WARNING, logger="case_calendar.summary"):
            row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)

        assert patch_llm == []
        assert row is not None
        assert row["summary"] == SUMMARY_PRIMARY_DOCUMENT_UNREADABLE
        # WARN breakdown shows unreadable=1.
        assert any(
            "unreadable=1" in r.message and "docket 1" in r.message
            for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_writes_refusal_when_no_primary_or_disposition_identified(
        self, store, patch_llm, patch_pdf, caplog
    ):
        # Truly cold docket: no entry on the docket matches
        # is_primary_document or _is_disposition_document. The behavior
        # matches the identified-but-no-text case — both write the
        # canonical SUMMARY_INSUFFICIENT_DOCUMENTS refusal so subscribers
        # always see SOMETHING under the docket link regardless of which
        # failure mode produced the empty document set. The WARN log
        # carries counts so the operator can still distinguish the two.
        from case_calendar.llm import SUMMARY_INSUFFICIENT_DOCUMENTS

        _seed_docket_meta(store, 1)
        cl = _FakeCourtListener(
            {
                # Only a procedural notice — neither primary nor disposition.
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "NOTICE of attorney appearance",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )

        import logging

        with caplog.at_level(logging.WARNING, logger="case_calendar.summary"):
            row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)

        assert patch_llm == []
        assert row is not None
        assert row["summary"] == SUMMARY_INSUFFICIENT_DOCUMENTS
        # source_entry_ids is empty because no primary/disposition entry
        # was identified to attribute the refusal to.
        assert row["source_entry_ids"] == []
        persisted = store.get_docket_summary("us-v-doe", *_DEFAULT_GROUP)
        assert persisted["summary"] == SUMMARY_INSUFFICIENT_DOCUMENTS
        # WARN log carries the "primary=0" signal so the operator can
        # tell this is the matcher-missed-everything shape, not the
        # identified-but-unreadable shape.
        assert any(
            "primary=0" in r.message and "docket 1" in r.message for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_insufficient_documents_fallback_is_stored_and_warns(
        self,
        store,
        patch_pdf,
        monkeypatch,
        caplog,
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
        patch_pdf["texts"] = {
            500: "INDICTMENT body text..."
        }  # text passes _attach_text
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )

        import logging

        with caplog.at_level(logging.WARNING, logger="case_calendar.summary"):
            row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)

        assert row is not None
        assert row["summary"] == SUMMARY_INSUFFICIENT_DOCUMENTS
        persisted = store.get_docket_summary("us-v-doe", *_DEFAULT_GROUP)
        assert persisted["summary"] == SUMMARY_INSUFFICIENT_DOCUMENTS
        # And the warning fired so the operator can find the docket.
        assert any(
            "insufficient-documents fallback" in r.message and "docket 1" in r.message
            for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_warns_when_primary_document_text_is_suspiciously_short(
        self,
        store,
        patch_llm,
        patch_pdf,
        caplog,
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
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "entry_number": 7,
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )

        with caplog.at_level(logging.WARNING, logger="case_calendar.summary"):
            row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)

        # Summary was still produced — the LLM works around partial
        # inputs (the stubbed `patch_llm` returns a clean summary).
        assert row is not None
        # And the warning fired with the specific docket / entry
        # references the operator needs to find it.
        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        matched = [
            m
            for m in warning_messages
            if "docket 1" in m and "entry #7" in m and "extracted to only" in m
        ]
        assert matched, warning_messages

    def test_no_warning_when_primary_document_text_is_full_length(
        self,
        store,
        patch_llm,
        patch_pdf,
        caplog,
    ):
        import logging

        _seed_docket_meta(store, 1)
        # A realistic indictment runs many KB. Pad past the 1500-char
        # threshold so the short-doc warning doesn't fire.
        long_text = "INDICTMENT body. " * 200  # ~3400 chars
        patch_pdf["texts"] = {500: long_text}
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )

        with caplog.at_level(logging.WARNING, logger="case_calendar.summary"):
            summarize_docket(cl=cl, store=store, case=case, docket_id=1)

        # No "extracted to only N chars" warnings on a normal-length doc.
        suspect = [
            r.message for r in caplog.records if "extracted to only" in r.message
        ]
        assert suspect == [], suspect

    def test_logs_sealing_advisory_when_detect_sealing_fires(
        self,
        store,
        patch_llm,
        patch_pdf,
        caplog,
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
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2025-08-21",
                        "entry_number": 33,
                        "recap_documents": [{"id": 500}],
                    },
                    {
                        "id": 11,
                        "entry_number": 44,
                        "date_filed": "2025-08-21",
                        "description": "ORDER granting 43 EX PARTE APPLICATION to Seal Indictment",
                        "recap_documents": [{"is_available": False}],
                    },
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )

        with caplog.at_level(logging.INFO, logger="case_calendar.summary"):
            row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)

        assert row is not None
        advisory_logs = [
            r.message
            for r in caplog.records
            if "sealing advisory" in r.message and "entry #44" in r.message
        ]
        assert advisory_logs, [r.message for r in caplog.records]
        # The advisory rode through to the LLM call as well.
        assert patch_llm[0]["sealing_advisory"] is not None
        assert patch_llm[0]["sealing_advisory"]["sealing_entry_number"] == 44

    def test_borrows_from_sibling_when_primary_docket_has_no_primary_document(
        self,
        store,
        patch_llm,
        patch_pdf,
    ):
        # Primary docket 1 has no primary document (appellate-style).
        # Sibling docket 2 has the indictment.
        _seed_docket_meta(store, 1, docket_number="24-1234", court_id="ca9")
        _seed_docket_meta(store, 2, docket_number="1:24-cr-100", court_id="dcd")
        patch_pdf["texts"] = {500: "INDICTMENT body text..."}
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [],
                (1, "-date_filed"): [],
                (2, "date_filed"): [
                    {
                        "id": 20,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (2, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1, 2], calendar="cyber"
        )

        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)

        assert row is not None
        # The borrowed-from label was appended to the description.
        primary_documents = patch_llm[0]["primary_documents"]
        assert primary_documents[0]["description"].endswith(
            "[from sibling 1:24-cr-100 D.D.C.]"
        )

    def test_borrows_dispositions_from_sibling_for_criminal_appeal(
        self,
        store,
        patch_llm,
        patch_pdf,
    ):
        # The 26-5455 / Knoot regression: an appellate criminal docket has no
        # documents of its own, and the conviction + sentence being appealed
        # live only on the trial-court sibling. Borrowing just the indictment
        # left the appellate summary saying only "was charged" — it never saw
        # the outcome under review. The borrow must pull the sibling's
        # DISPOSITIONS (judgment / plea) too, so the LLM can state the
        # conviction and sentence on appeal.
        _seed_docket_meta(store, 1, docket_number="24-1234", court_id="ca9")
        _seed_docket_meta(store, 2, docket_number="1:24-cr-100", court_id="dcd")
        patch_pdf["texts"] = {
            500: "INDICTMENT body text...",
            600: "JUDGMENT body: 18 months imprisonment.",
        }
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [],
                (1, "-date_filed"): [],
                (2, "date_filed"): [
                    {
                        "id": 20,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 500}],
                    }
                ],
                # Dispositions are walked newest-first (-date_filed).
                (2, "-date_filed"): [
                    {
                        "id": 30,
                        "description": "JUDGMENT in a Criminal Case",
                        "date_filed": "2025-05-01",
                        "recap_documents": [{"id": 600}],
                    }
                ],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1, 2], calendar="cyber"
        )

        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)

        assert row is not None
        # The borrowed indictment reached the LLM, tagged with the sibling.
        primary_documents = patch_llm[0]["primary_documents"]
        assert primary_documents[0]["description"].endswith(
            "[from sibling 1:24-cr-100 D.D.C.]"
        )
        # And so did the borrowed judgment — the whole point of the fix.
        disposition_documents = patch_llm[0]["disposition_documents"]
        assert disposition_documents, "borrowed judgment should reach the LLM"
        assert any("JUDGMENT" in d["description"] for d in disposition_documents)
        assert all(
            d["description"].endswith("[from sibling 1:24-cr-100 D.D.C.]")
            for d in disposition_documents
        )
        assert any("18 months" in (d.get("text") or "") for d in disposition_documents)

    def test_borrowing_swallows_sibling_failure_and_continues(
        self,
        store,
        patch_llm,
        patch_pdf,
        caplog,
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

        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [],
                (1, "-date_filed"): [],
                (3, "date_filed"): [
                    {
                        "id": 30,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 600}],
                    }
                ],
                (3, "-date_filed"): [],
            }
        )
        cl._get = _flaky_get.__get__(cl, _FakeCourtListener)
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1, 2, 3], calendar="cyber"
        )

        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None

    def test_writes_refusal_when_no_sibling_has_primary(
        self,
        store,
        patch_llm,
        patch_pdf,
    ):
        # Multi-docket case where neither the canonical docket nor any
        # sibling carries a primary-document entry — the borrow path
        # produces nothing either. Under the unified refusal behavior we
        # write SUMMARY_INSUFFICIENT_DOCUMENTS (no primary was identified
        # anywhere) rather than dropping the docket from the index.
        from case_calendar.llm import SUMMARY_INSUFFICIENT_DOCUMENTS

        _seed_docket_meta(store, 1, docket_number="24-1", court_id="ca9")
        _seed_docket_meta(store, 2, docket_number="24-2", court_id="ca9")
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [],
                (1, "-date_filed"): [],
                (2, "date_filed"): [],
                (2, "-date_filed"): [],
            }
        )
        case = _Case(case_id="case-x", name="X", dockets=[1, 2], calendar="cyber")

        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None
        assert row["summary"] == SUMMARY_INSUFFICIENT_DOCUMENTS
        assert patch_llm == []

    def test_attaches_dispositions_when_present(self, store, patch_llm, patch_pdf):
        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {500: "INDICTMENT body", 600: "JUDGMENT body"}
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [
                    {
                        "id": 99,
                        "description": "JUDGMENT in a Criminal Case",
                        "date_filed": "2025-06-15",
                        "recap_documents": [{"id": 600}],
                    }
                ],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )
        summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert [d["entry_id"] for d in patch_llm[0]["disposition_documents"]] == [99]

    def test_paperless_disposition_falls_back_to_description(
        self,
        store,
        patch_llm,
        patch_pdf,
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
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [
                    {
                        "id": 99,
                        "description": clerk_notes,
                        "date_filed": "2026-04-15",
                        "entry_number": 37,
                        "recap_documents": [],  # paperless — no attachments
                    }
                ],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )
        summarize_docket(cl=cl, store=store, case=case, docket_id=1)

        dispositions = patch_llm[0]["disposition_documents"]
        assert [d["entry_id"] for d in dispositions] == [99]
        # The sentence figures must reach the LLM — that's the whole point.
        assert "92 months imprisonment" in dispositions[0]["text"]

    def test_primary_document_without_pdf_does_not_use_description_fallback(
        self,
        store,
        patch_llm,
        patch_pdf,
    ):
        # By design the description fallback is scoped to dispositions only.
        # Primary documents are indictments / complaints — a clerk's
        # minute-entry stub isn't an acceptable substitute, and feeding one
        # in would produce a vacuous summary. Confirm the asymmetry: when
        # the PDF text is empty AND the recap_doc has no source for text,
        # the primary entry's description is NOT used as a synthetic
        # body — instead we write a fallback message directly without an
        # LLM call. This specific setup (no URL fields, no plain_text,
        # not sealed) maps to the NOT_AVAILABLE state; the asymmetry
        # holds across all three failure states (none of them use the
        # description-as-text fallback).
        from case_calendar.llm import SUMMARY_PRIMARY_DOCUMENT_NOT_AVAILABLE

        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {}  # no PDF text extracts
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )
        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        # LLM is NOT called — the description is NOT used as a fallback
        # body to manufacture a summary. The asymmetry stands.
        assert patch_llm == []
        # The fallback message is what gets persisted, not a
        # description-derived synthetic summary.
        assert row is not None
        assert row["summary"] == SUMMARY_PRIMARY_DOCUMENT_NOT_AVAILABLE

    def test_falls_back_to_attachments_when_main_doc_has_no_text(
        self,
        store,
        patch_llm,
        patch_pdf,
    ):
        _seed_docket_meta(store, 1)
        # Main doc (id=500, no attachment_number) extracts to empty;
        # attachment (id=501) has text. The helper should fall through.
        patch_pdf["texts"] = {501: "INDICTMENT body via attachment"}
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [
                            {"id": 500},
                            {"id": 501, "attachment_number": 1},
                        ],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )
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
        self,
        store,
        patch_llm,
        patch_pdf,
        monkeypatch,
    ):
        # The canonical Zewei case: CourtListener has no primary document
        # (entries 1-4 missing), so the only document the summary LLM
        # sees is the operator-provided one — in the extras section,
        # with its note describing what it is.
        _seed_docket_meta(store, 1, docket_number="4:23-cr-00523", court_id="txsd")
        store.upsert_court("txsd", "S.D. Tex.", "TXSD", "Southern District of Texas")
        monkeypatch.setattr(
            summary.pdf,
            "extract_text_from_url",
            lambda url, allow_ocr=True: "REDACTED INDICTMENT body from DoJ PR PDF...",
        )
        cl = _FakeCourtListener({(1, "date_filed"): [], (1, "-date_filed"): []})
        case = _Case(
            case_id="us-v-zewei",
            name="US v. Zewei",
            dockets=[1],
            calendar="cyber",
            extra_documents=[
                ExtraDocument(
                    docket=1,
                    url="https://www.justice.gov/opa/media/1407196/dl",
                    note="Indictment was filed under seal but the seal has "
                    "since been lifted; treat as the primary document.",
                )
            ],
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
        self,
        store,
        patch_llm,
        patch_pdf,
        monkeypatch,
    ):
        # Overlap window: CourtListener has the primary document AND an operator
        # also listed an extra. CourtListener doc fills the primary slot; the
        # extra rides in its own section (the LLM sees both, with the
        # provenance distinction explicit).
        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {500: "CourtListener INDICTMENT body"}
        monkeypatch.setattr(
            summary.pdf,
            "extract_text_from_url",
            lambda url, allow_ocr=True: "OPERATOR INDICTMENT body",
        )
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-x",
            name="US v. X",
            dockets=[1],
            calendar="cyber",
            extra_documents=[
                ExtraDocument(
                    docket=1,
                    url="https://example.gov/i.pdf",
                    note="overlap-window operator copy of the indictment",
                )
            ],
        )
        summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert [d["entry_id"] for d in patch_llm[0]["primary_documents"]] == [10]
        extras = patch_llm[0]["extra_documents"]
        assert len(extras) == 1
        assert extras[0]["source_url"] == "https://example.gov/i.pdf"

    def test_failed_fetch_is_dropped(
        self,
        store,
        patch_llm,
        patch_pdf,
        monkeypatch,
        caplog,
    ):
        # URL is down / the PDF won't extract. The summary pipeline still
        # runs on whatever CourtListener did surface — extra_documents failures are
        # logged but not fatal.
        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {500: "CourtListener INDICTMENT body"}
        monkeypatch.setattr(
            summary.pdf,
            "extract_text_from_url",
            lambda url, allow_ocr=True: None,
        )
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-x",
            name="US v. X",
            dockets=[1],
            calendar="cyber",
            extra_documents=[
                ExtraDocument(
                    docket=1,
                    url="https://broken.example/x.pdf",
                    note="will fail to fetch",
                )
            ],
        )
        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None
        # Only the CourtListener doc reaches the LLM; the dropped one isn't appended.
        assert [d["entry_id"] for d in patch_llm[0]["primary_documents"]] == [10]
        assert patch_llm[0]["extra_documents"] == []

    def test_only_extras_for_target_group_are_fetched(
        self,
        store,
        patch_llm,
        patch_pdf,
        monkeypatch,
    ):
        # extra_documents scope to one LOGICAL PACER docket via the
        # `docket` field. When summarize_docket runs on group A, extras
        # pointing at a docket_id in a DIFFERENT group must NOT be
        # fetched. An extra pinned to a sibling CourtListener docket_id in the SAME
        # group applies (the canonical Akhter-style CourtListener-split case).
        # Group A: docket_id 1 + 2 share (docket_number, court_id) (CourtListener split).
        _seed_docket_meta(store, 1)  # 1:24-cr-100 / dcd
        _seed_docket_meta(store, 2)  # 1:24-cr-100 / dcd (same group)
        # Group B: docket_id 3 is a separate logical docket.
        _seed_docket_meta(store, 3, docket_number="1:24-cr-200", court_id="dcd")
        patch_pdf["texts"] = {500: "INDICTMENT"}
        fetched: list[str] = []

        def _fake_fetch(url, allow_ocr=True):
            fetched.append(url)
            return "OPERATOR doc text"

        monkeypatch.setattr(summary.pdf, "extract_text_from_url", _fake_fetch)
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
                (2, "date_filed"): [],
                (2, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-x",
            name="US v. X",
            dockets=[1, 2, 3],
            calendar="cyber",
            extra_documents=[
                # Pinned to docket_id 3 (different group) — NOT fetched.
                ExtraDocument(
                    docket=3, url="https://x.com/wrong.pdf", note="wrong group"
                ),
                # Pinned to docket_id 1 (target group) — fetched.
                ExtraDocument(
                    docket=1, url="https://x.com/right.pdf", note="right group"
                ),
                # Pinned to docket_id 2 (sibling in target group) — also fetched.
                ExtraDocument(
                    docket=2, url="https://x.com/sibling.pdf", note="sibling in group"
                ),
            ],
        )
        summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert sorted(fetched) == [
            "https://x.com/right.pdf",
            "https://x.com/sibling.pdf",
        ]

    def test_extra_deduped_when_cl_surfaces_same_text(
        self,
        store,
        patch_llm,
        patch_pdf,
        monkeypatch,
        caplog,
    ):
        # The CourtListener-bug-#7345 follow-up case: the operator added
        # an extra_documents entry to work around a CourtListener data
        # gap, and CourtListener later started surfacing the same
        # document naturally (someone re-uploaded the PDF to PACER under
        # the new pacer_case_id, or the upstream reconciler caught up).
        # Without dedup, the same document body reaches the summary LLM
        # twice — once via the primary slot, once via the extras block —
        # wasting tokens and giving that document outsized influence.
        # The fingerprint-based filter drops the duplicate extra and
        # warns the operator to remove the now-redundant config entry.
        _seed_docket_meta(store, 1)
        body = (
            "INDICTMENT against Xu Zewei, charging conspiracy to commit "
            "computer fraud and abuse under 18 U.S.C. § 1030. Count One "
            "alleges that beginning in or about February 2021, the "
            "defendants conspired to access protected computers without "
            "authorization. Count Two alleges aggravated identity theft "
            "under 18 U.S.C. § 1028A." * 4
        )
        patch_pdf["texts"] = {500: body}
        monkeypatch.setattr(
            summary.pdf,
            "extract_text_from_url",
            lambda url, allow_ocr=True: body,
        )
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-zewei",
            name="US v. Zewei",
            dockets=[1],
            calendar="cyber",
            extra_documents=[
                ExtraDocument(
                    docket=1,
                    url="https://storage.courtlistener.com/recap/gov.uscourts.txsd.OLD/indictment.pdf",
                    note="Pre-fix workaround for the old pacer_case_id; "
                    "CourtListener's reconciler couldn't find this entry.",
                )
            ],
        )
        with caplog.at_level("WARNING", logger="case_calendar.summary"):
            row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None
        assert [d["entry_id"] for d in patch_llm[0]["primary_documents"]] == [10]
        # The extras block is empty — the duplicate was dropped.
        assert patch_llm[0]["extra_documents"] == []
        # And the operator gets a loud warning naming the URL to remove.
        assert any(
            "dropping extra_documents entry" in r.message
            and "storage.courtlistener.com/recap/gov.uscourts.txsd.OLD" in r.message
            for r in caplog.records
        )

    def test_extra_kept_when_text_differs_from_cl(
        self,
        store,
        patch_llm,
        patch_pdf,
        monkeypatch,
    ):
        # Negative case for the fingerprint dedup: when the extra's
        # extracted text doesn't match any CourtListener-surfaced doc,
        # the extra stays. Whitespace-only differences DO dedup (see
        # the _text_fingerprint normalization), but substantive content
        # differences (different PDFs, different documents) do not.
        # Both bodies are intentionally well above the 100-char
        # fingerprint threshold so the dedup compare actually runs
        # end-to-end (and falls through to the keep-the-extra branch).
        _seed_docket_meta(store, 1)
        cl_body = (
            "CourtListener INDICTMENT body, charging conspiracy to commit "
            "computer fraud and abuse under 18 U.S.C. § 1030. Count One "
            "alleges that the defendant accessed protected computers "
            "without authorization. Count Two alleges aggravated identity "
            "theft under 18 U.S.C. § 1028A."
        )
        op_body = (
            "Operator-supplied SENTENCING MEMORANDUM in aid of sentencing. "
            "The Government submits this memorandum to assist the Court in "
            "fashioning a sentence under 18 U.S.C. § 3553(a). The defendant "
            "stands convicted of one count of wire fraud and faces a "
            "guidelines range of 24-30 months."
        )
        patch_pdf["texts"] = {500: cl_body}
        monkeypatch.setattr(
            summary.pdf,
            "extract_text_from_url",
            lambda url, allow_ocr=True: op_body,
        )
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-x",
            name="US v. X",
            dockets=[1],
            calendar="cyber",
            extra_documents=[
                ExtraDocument(
                    docket=1,
                    url="https://example.gov/memo.pdf",
                    note="distinct document, should not be deduped",
                )
            ],
        )
        summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert len(patch_llm[0]["extra_documents"]) == 1
        assert patch_llm[0]["extra_documents"][0]["source_url"] == (
            "https://example.gov/memo.pdf"
        )

    def test_text_fingerprint_short_circuits_on_falsy_or_short_input(self):
        # Direct unit test for the two short-circuits in _text_fingerprint:
        # falsy input returns None outright (we never call sha256 on
        # nothing), and too-short normalized text also returns None (short
        # bodies are mostly boilerplate, false-positive matches would be
        # noisy). Both branches matter for `_filter_extras_already_in_cl`
        # — without the falsy short-circuit it would crash on docs whose
        # text key is missing, and without the length floor it would
        # treat any two short PDFs that share a caption as duplicates.
        assert summary._text_fingerprint(None) is None
        assert summary._text_fingerprint("") is None
        assert summary._text_fingerprint("   \n\t  ") is None
        # Whitespace-only but long enough to survive normalization is
        # still falsy after `.strip()`, so we fall into the length check
        # and bail.
        assert summary._text_fingerprint("a short string") is None
        # Identical-after-normalization inputs produce identical hashes,
        # which is what makes the dedup tolerant of pypdf-vs-CourtListener
        # extraction nits. Bodies are well over 100 chars.
        body = (
            "This is an INDICTMENT against John Doe charging one count of "
            "conspiracy to commit wire fraud in violation of 18 U.S.C. § 1349 "
            "and one count of aggravated identity theft."
        )
        assert summary._text_fingerprint(body) == summary._text_fingerprint(
            "  " + body.upper() + "  \n"
        )

    def test_extras_alone_satisfy_content_gate(
        self,
        store,
        patch_llm,
        patch_pdf,
        monkeypatch,
    ):
        # Without the extras-aware content gate, this docket would hit
        # the "no primary document text could be extracted" branch and
        # return None. With the extras-aware gate, the operator's doc
        # satisfies the content check on its own and the summary
        # proceeds — the canonical Zewei flow.
        _seed_docket_meta(store, 1)
        monkeypatch.setattr(
            summary.pdf,
            "extract_text_from_url",
            lambda url, allow_ocr=True: "operator-supplied indictment text",
        )
        cl = _FakeCourtListener({(1, "date_filed"): [], (1, "-date_filed"): []})
        case = _Case(
            case_id="us-v-x",
            name="US v. X",
            dockets=[1],
            calendar="cyber",
            extra_documents=[
                ExtraDocument(
                    docket=1,
                    url="https://x.com/i.pdf",
                    note="unsealed indictment, sourced from DoJ PR attachment",
                )
            ],
        )
        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None
        assert patch_llm[0]["primary_documents"] == []
        assert len(patch_llm[0]["extra_documents"]) == 1


# ---------------------------------------------------------------------------
# refresh_stale + summarize_case
# ---------------------------------------------------------------------------


class TestGroupDocketsOnCase:
    """Direct coverage for `summary._group_dockets_on_case`'s sibling dedup.

    When `case.dockets` lists multiple CourtListener docket_ids that map to the same
    logical PACER docket `(docket_number, court_id)` — the Akhter
    `1:25-cr-00307` shape where one PACER docket lives under three CourtListener
    docket_ids — the loop must yield ONE group entry, not N. Without
    the dedup we'd run `summarize_docket` once per CourtListener sibling and write
    near-duplicate summary rows for the same logical docket.
    """

    def test_collapses_sibling_docket_ids_to_one_group_entry(self, store):
        # Three CourtListener docket_ids share `(docket_number, court_id)`.
        for did in (100, 101, 102):
            store.upsert_docket_meta(
                did,
                {
                    "court_id": "vaed",
                    "docket_number": "1:25-cr-00307",
                    "case_name": "United States v. Akhter",
                    "absolute_url": f"/d/{did}/",
                },
            )
        case = _Case(
            case_id="us-v-akhter",
            name="US v. Akhter",
            dockets=[100, 101, 102],
            calendar="cyber",
        )
        groups = summary._group_dockets_on_case(store, case)
        # ONE group entry across all three sibling docket_ids.
        assert len(groups) == 1
        docket_number, court_id, canonical = groups[0]
        assert (docket_number, court_id) == ("1:25-cr-00307", "vaed")
        # Canonical is the first sibling in config order (the second + third
        # iterations hit the "key in seen — skip" branch at line 1246).
        assert canonical == 100

    def test_mixes_distinct_and_sibling_dockets(self, store):
        # Two CourtListener docket_ids on a 1:25-cr-00307 group + one standalone
        # docket on a different group. The standalone gets its own entry;
        # the siblings collapse to one. Distinct group order matches
        # config order (100 first, then 200).
        for did, dn in [
            (100, "1:25-cr-00307"),
            (101, "1:25-cr-00307"),
            (200, "1:24-cv-12345"),
        ]:
            court_id = "vaed" if dn.endswith("00307") else "nyed"
            store.upsert_docket_meta(
                did,
                {
                    "court_id": court_id,
                    "docket_number": dn,
                    "case_name": "test",
                    "absolute_url": f"/d/{did}/",
                },
            )
        case = _Case(
            case_id="us-v-test",
            name="test",
            dockets=[100, 101, 200],
            calendar="cyber",
        )
        groups = summary._group_dockets_on_case(store, case)
        assert len(groups) == 2
        assert [g[0] for g in groups] == ["1:25-cr-00307", "1:24-cv-12345"]


class TestRefreshStale:
    def test_skips_dockets_with_no_metadata_yet(self, store, patch_llm, patch_pdf):
        # case.dockets references a CourtListener docket_id that has no `dockets`
        # row yet (sync hasn't run, or interrupted before
        # upsert_docket_meta). _group_dockets_on_case can't resolve the
        # group key, so the docket is skipped with a warning.
        # No _seed_docket_meta — store has no metadata for docket_id 1.
        cl = _FakeCourtListener({})
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )
        written = refresh_stale(cl=cl, store=store, cases=[case])
        assert written == {}
        assert patch_llm == []

    def test_skips_dockets_that_are_not_stale(self, store, patch_llm, patch_pdf):
        _seed_docket_meta(store, 1)
        # Pre-seed a fresh (non-stale) summary row.
        store.upsert_case_summary(
            "us-v-doe",
            *_DEFAULT_GROUP,
            summary="existing",
            model="prev/model",
            source_entry_ids=[],
        )
        cl = _FakeCourtListener({})  # never queried
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )

        written = refresh_stale(cl=cl, store=store, cases=[case])

        assert written == {}
        assert patch_llm == []

    def test_regenerates_when_missing(self, store, patch_llm, patch_pdf):
        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {500: "INDICTMENT body"}
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )
        written = refresh_stale(cl=cl, store=store, cases=[case])
        assert written == {"us-v-doe": {_DEFAULT_GROUP}}

    def test_regenerates_when_stale_flag_set(self, store, patch_llm, patch_pdf):
        _seed_docket_meta(store, 1)
        store.upsert_case_summary(
            "us-v-doe",
            *_DEFAULT_GROUP,
            summary="old",
            model="prev/model",
            source_entry_ids=[],
        )
        store.mark_summary_stale("us-v-doe", *_DEFAULT_GROUP)
        patch_pdf["texts"] = {500: "INDICTMENT body"}
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )
        written = refresh_stale(cl=cl, store=store, cases=[case])
        assert written == {"us-v-doe": {_DEFAULT_GROUP}}

    def test_per_docket_failure_does_not_abort_remaining(self, store, monkeypatch):
        # A transient summarize_docket failure on one group (e.g. a 503 that
        # exhausts retries, or a provider empty-response) must NOT skip the
        # other groups in the same refresh — it's logged, left stale, and the
        # remaining dockets still get summarized. (Regression: a single Gemini
        # "No content" aborted the whole batch, producing zero summaries.)
        _seed_docket_meta(store, 1, docket_number="1:24-cr-100", court_id="dcd")
        _seed_docket_meta(store, 2, docket_number="2:24-cr-200", court_id="cand")
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1, 2], calendar="cyber"
        )
        seen: list[int] = []

        def fake_summarize(*, cl, store, case, docket_id, **kw):
            seen.append(docket_id)
            if docket_id == 1:
                raise RuntimeError("transient 503 exhausted retries")
            return {"summary": "ok for docket 2"}

        monkeypatch.setattr(summary, "summarize_docket", fake_summarize)
        cl = _FakeCourtListener({})
        written = refresh_stale(cl=cl, store=store, cases=[case])
        # Both groups were attempted despite docket 1 raising...
        assert set(seen) == {1, 2}
        # ...and only the successful group is recorded as written.
        assert written == {"us-v-doe": {("2:24-cr-200", "cand")}}

    def test_only_case_ids_scopes_the_walk(self, store, patch_llm, patch_pdf):
        _seed_docket_meta(store, 1)
        _seed_docket_meta(store, 2)
        patch_pdf["texts"] = {500: "INDICTMENT"}
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case_a = _Case(case_id="a", name="A", dockets=[1], calendar="cyber")
        case_b = _Case(case_id="b", name="B", dockets=[2], calendar="cyber")
        written = refresh_stale(
            cl=cl,
            store=store,
            cases=[case_a, case_b],
            only_case_ids={"a"},
        )
        assert written == {"a": {_DEFAULT_GROUP}}

    def test_force_regenerates_non_stale_rows(self, store, patch_llm, patch_pdf):
        # Default behavior is to skip non-stale rows. force=True bypasses
        # the stale check so a single sync can pick up a model upgrade or
        # prompt change without a separate `summarize --force` invocation
        # that would hit CourtListener all over again.
        _seed_docket_meta(store, 1)
        store.upsert_case_summary(
            "us-v-doe",
            *_DEFAULT_GROUP,
            summary="old",
            model="prev/model",
            source_entry_ids=[],
        )
        # Row is fresh — is_summary_stale would return False.
        assert not store.is_summary_stale("us-v-doe", *_DEFAULT_GROUP)
        patch_pdf["texts"] = {500: "INDICTMENT body"}
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )
        written = refresh_stale(cl=cl, store=store, cases=[case], force=True)
        assert written == {"us-v-doe": {_DEFAULT_GROUP}}
        assert len(patch_llm) == 1

    def test_uses_aggregation_note_override(self, store, patch_llm, patch_pdf):
        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {500: "INDICTMENT body"}
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )
        refresh_stale(
            cl=cl,
            store=store,
            cases=[case],
            case_overrides={
                "us-v-doe": {"aggregation_note": "Parallel district + appellate."}
            },
        )
        assert patch_llm[0]["aggregation_note"] == "Parallel district + appellate."

    def test_truly_cold_docket_writes_refusal_and_is_in_written_set(
        self,
        store,
        patch_llm,
        patch_pdf,
    ):
        # Under the unified refusal behavior, even a truly-cold docket
        # (no entry matches is_primary_document or _is_disposition_document)
        # produces a row — the SUMMARY_INSUFFICIENT_DOCUMENTS refusal —
        # so the index never shows a docket link without a summary block
        # underneath. refresh_stale therefore adds the group to the
        # written set so the caller knows to re-render the index, the
        # same way it would for any other newly-landed summary.
        _seed_docket_meta(store, 1)
        cl = _FakeCourtListener(
            {
                # Only a procedural notice — no primary or disposition match.
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "NOTICE of attorney appearance",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )
        written = refresh_stale(cl=cl, store=store, cases=[case])
        # The refusal row landed in the store and refresh_stale flagged
        # the group for re-emit.
        assert written.get("us-v-doe") == {_DEFAULT_GROUP}


class TestSummarizeCase:
    def test_force_overwrites_existing_summary(self, store, patch_llm, patch_pdf):
        _seed_docket_meta(store, 1)
        store.upsert_case_summary(
            "us-v-doe",
            *_DEFAULT_GROUP,
            summary="old",
            model="prev/model",
            source_entry_ids=[],
        )
        patch_pdf["texts"] = {500: "INDICTMENT body"}
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )
        rows = summarize_case(cl=cl, store=store, case=case, force=True)
        assert [(r["docket_number"], r["court_id"]) for r in rows] == [_DEFAULT_GROUP]
        assert (
            store.get_docket_summary("us-v-doe", *_DEFAULT_GROUP)["summary"]
            == "A two-sentence summary of the matter."
        )

    def test_default_skips_when_summary_already_present(
        self,
        store,
        patch_llm,
        patch_pdf,
    ):
        _seed_docket_meta(store, 1)
        store.upsert_case_summary(
            "us-v-doe",
            *_DEFAULT_GROUP,
            summary="existing",
            model="prev/model",
            source_entry_ids=[],
        )
        cl = _FakeCourtListener({})
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )
        rows = summarize_case(cl=cl, store=store, case=case)
        assert rows == []
        assert patch_llm == []

    def test_truly_cold_docket_returns_refusal_row(self, store, patch_llm, patch_pdf):
        # Under the unified refusal behavior, even a truly-cold docket
        # (no entry matches is_primary_document / _is_disposition_document)
        # produces a row — the SUMMARY_INSUFFICIENT_DOCUMENTS refusal.
        # summarize_case forwards the row through, so the caller still
        # sees ONE row per docket on the case regardless of whether the
        # documents were readable, identified-but-unreadable, or absent.
        from case_calendar.llm import SUMMARY_INSUFFICIENT_DOCUMENTS

        _seed_docket_meta(store, 1)
        cl = _FakeCourtListener(
            {
                # Only a procedural notice — no primary or disposition match.
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "NOTICE of attorney appearance",
                        "date_filed": "2024-01-01",
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )
        rows = summarize_case(cl=cl, store=store, case=case, force=True)
        # ONE row — the refusal — even on a truly-cold docket.
        assert len(rows) == 1
        assert rows[0]["summary"] == SUMMARY_INSUFFICIENT_DOCUMENTS


class TestEntryDescriptionHeadFallback:
    """``_entry_description_head`` prefers the entry's short_description /
    description; only if both are empty does it walk the recap_documents.
    Inside that walk, an empty recap_document description is skipped."""

    def test_skips_empty_recap_doc_descriptions_and_returns_first_populated(self):
        entry = {
            "short_description": "",
            "description": "",
            "recap_documents": [
                {"description": ""},  # skipped — branch under test
                {"description": "Real description"},
            ],
        }
        assert _entry_description_head(entry) == "Real description"

    def test_returns_empty_when_every_source_is_empty(self):
        entry = {
            "short_description": "",
            "description": "",
            "recap_documents": [
                {"description": ""},
                {"description": ""},
            ],
        }
        assert _entry_description_head(entry) == ""


class TestEntryDocTextAttachmentFallback:
    """When the main recap_document yields no text, ``_entry_doc_text``
    falls back to scanning attachments. Inside that fallback, the empty
    attachment is skipped and the next-attachment loop continues."""

    def test_attachment_with_empty_extract_is_skipped(self, monkeypatch):
        # Patch pdf.extract_text to return empty for the first attachment
        # and real text for the second.
        from case_calendar import pdf as pdf_mod

        rds = [
            {"id": 1, "attachment_number": None},  # main, returns ""
            {"id": 2, "attachment_number": 1},  # attachment 1, returns ""
            {"id": 3, "attachment_number": 2},  # attachment 2, returns text
        ]

        def fake_extract(rd, **_):
            return {1: "", 2: "", 3: "exhibit text"}[rd["id"]]

        monkeypatch.setattr(pdf_mod, "extract_text", fake_extract)
        out = _entry_doc_text({"recap_documents": rds})
        assert "exhibit text" in out


class TestIndictmentAttachedToProceduralParent:
    """us-v-stryzhak shape: the indictment is attached to a "CONSENT TO
    TRANSFER JURISDICTION (Rule 20)" parent entry. The parent's
    ``description`` heads with the transfer notice and never matches
    ``_PRIMARY_DOCUMENT_RE``, but the attachment's own description is
    ``"Indictment"``. The matcher AND the extractor must both recognize
    the attachment so the summary LLM gets the charging document body
    rather than the transfer-procedural body.
    """

    def _stryzhak_rule20_entry(self):
        # The literal CourtListener-returned shape for Stryzhak entry 1:
        # parent description starts with "CONSENT TO TRANSFER JURISDICTION",
        # main recap_doc carries "Rule 20 - Transfer In", indictment is
        # attachment 1.
        return {
            "id": 446651345,
            "description": (
                "CONSENT TO TRANSFER JURISDICTION (Rule 20) from Middle "
                "District of Florida by Artem Aleksandrovych Stryzhak. "
                "(Attachments: # 1 Indictment) (AM) (Additional attachment "
                "(MDFL Docket sheet) added on 12/9/2025: (AM))"
            ),
            "short_description": None,
            "recap_documents": [
                {
                    "id": 500,
                    "document_number": "1",
                    "attachment_number": None,
                    "description": "Rule 20 - Transfer In",
                },
                {
                    "id": 501,
                    "document_number": "1",
                    "attachment_number": 1,
                    "description": "Indictment",
                },
                {
                    "id": 502,
                    "document_number": "1",
                    "attachment_number": 2,
                    "description": "MDFL Docket sheet",
                },
            ],
        }

    def test_is_primary_document_matches_via_attachment(self):
        from case_calendar.summary import is_primary_document

        # The parent description doesn't match _PRIMARY_DOCUMENT_RE,
        # and the first recap_doc's description ("Rule 20 - Transfer In")
        # doesn't either — but the matcher should still return True
        # because attachment #1 carries description="Indictment".
        assert is_primary_document(self._stryzhak_rule20_entry()) is True

    def test_entry_doc_text_extracts_from_indictment_attachment(self, monkeypatch):
        # When an attachment carries the primary-document signal, the
        # extractor must pull text from THAT attachment in preference
        # to the parent's main doc — otherwise the summary LLM sees the
        # Rule 20 procedural text instead of the indictment body.
        from case_calendar import pdf as pdf_mod

        texts = {
            500: "Rule 20 transfer notice procedural text " * 20,
            501: "Real indictment body charging defendant with " * 20,
            502: "MDFL docket sheet listing entries 1-42 " * 20,
        }

        def fake_extract(rd, **_):
            return texts[rd["id"]]

        monkeypatch.setattr(pdf_mod, "extract_text", fake_extract)

        out = _entry_doc_text(self._stryzhak_rule20_entry())
        # Indictment attachment is the priority; main + non-primary
        # attachment are skipped.
        assert "Real indictment body" in out
        assert "Rule 20 transfer notice" not in out
        assert "MDFL docket sheet" not in out

    def test_entry_doc_text_falls_through_when_primary_attachment_empty(
        self, monkeypatch
    ):
        # Defensive: if the primary-marked attachment's extraction
        # produces nothing (PDF not on RECAP, etc.), we fall through
        # to the main doc — better the procedural text than nothing,
        # and the summary LLM is briefed to refuse on weak inputs.
        from case_calendar import pdf as pdf_mod

        texts = {
            500: "Rule 20 transfer notice body that is at least usable",
            501: "",  # indictment attachment extracts to nothing
            502: "",
        }

        def fake_extract(rd, **_):
            return texts[rd["id"]]

        monkeypatch.setattr(pdf_mod, "extract_text", fake_extract)
        out = _entry_doc_text(self._stryzhak_rule20_entry())
        assert "Rule 20 transfer notice" in out


class TestDispositionAttachedToProceduralParent:
    """The disposition analogue of TestIndictmentAttachedToProceduralParent.

    Rarer than the primary case but does occur: a "Notice of Filing
    of Plea Agreement" parent entry with the actual plea agreement as
    ``attachment_number=1``; a parent order with a memorandum opinion
    filed as a separate attachment. The matcher AND the extractor
    must both recognize the attachment so the summary LLM gets the
    ruling document's body rather than the procedural wrapper.
    """

    def _notice_of_plea_entry(self):
        # "Notice of Filing" with plea agreement as attachment 1. The
        # parent description doesn't head-match disposition patterns
        # (in fact "Notice of Filing" reads like a procedural notice),
        # but attachment 1's description IS "Plea Agreement" which
        # head-matches _DISPOSITION_RE.
        return {
            "id": 9001,
            "description": (
                "Notice of Filing of Plea Agreement by USA as to "
                "Defendant Doe. (Attachments: # 1 Plea Agreement) (AM)"
            ),
            "short_description": None,
            "recap_documents": [
                {
                    "id": 700,
                    "document_number": "12",
                    "attachment_number": None,
                    "description": "Notice of Filing",
                },
                {
                    "id": 701,
                    "document_number": "12",
                    "attachment_number": 1,
                    "description": "Plea Agreement",
                },
            ],
        }

    def test_is_disposition_document_matches_via_attachment(self):
        from case_calendar.summary import _is_disposition_document

        # Parent description heads with "Notice of Filing" — the
        # _DISPOSITION_DOCUMENT_NEGATIVE_RE rejects "notice of filing"
        # at the entry level. But the attached "Plea Agreement"
        # head-matches _DISPOSITION_RE, so the strict matcher must
        # return True via the attachment.
        assert _is_disposition_document(self._notice_of_plea_entry()) is True

    def test_is_disposition_broad_matches_via_attachment(self):
        from case_calendar.summary import is_disposition

        # Broad version (stale-flag): same behavior — attachment
        # carrying the broad signal flips the entry.
        assert is_disposition(self._notice_of_plea_entry()) is True

    def test_entry_doc_text_extracts_from_disposition_attachment(self, monkeypatch):
        # Same priority as primaries: substance-marked attachment wins
        # over the parent's procedural main doc.
        from case_calendar import pdf as pdf_mod

        texts = {
            700: "Notice of Filing — procedural cover sheet " * 20,
            701: "Plea Agreement body — defendant agrees to plead guilty " * 20,
        }

        def fake_extract(rd, **_):
            return texts[rd["id"]]

        monkeypatch.setattr(pdf_mod, "extract_text", fake_extract)
        out = _entry_doc_text(self._notice_of_plea_entry())
        assert "Plea Agreement body" in out
        assert "Notice of Filing — procedural" not in out

    def test_entry_doc_text_falls_through_when_disposition_attachment_empty(
        self, monkeypatch
    ):
        # Symmetric to the primary fallthrough: when the substance-
        # marked attachment extracts to nothing, fall through to the
        # main doc.
        from case_calendar import pdf as pdf_mod

        texts = {
            700: "Notice of Filing body that is at least usable",
            701: "",  # plea agreement extracts to nothing
        }

        def fake_extract(rd, **_):
            return texts[rd["id"]]

        monkeypatch.setattr(pdf_mod, "extract_text", fake_extract)
        out = _entry_doc_text(self._notice_of_plea_entry())
        assert "Notice of Filing body" in out


class TestPrimaryFailureStateEdgeCases:
    """Coverage for the recap_doc-level state classifier's edge branches."""

    def test_attachment_only_recap_documents_falls_through_to_no_main(self):
        # If an entry's recap_documents are ALL attachments (no main
        # doc), the per-rd loop skips all of them and the function
        # falls through to the no-main-recap_document branch, which
        # returns 'not-available'. This shape is uncommon but possible
        # for entries where someone treated an attachment as primary
        # via description-only matching.
        from case_calendar.summary import _primary_failure_state

        entry = {
            "recap_documents": [
                {"id": 1, "attachment_number": 1, "is_sealed": False},
                {"id": 2, "attachment_number": 2, "is_sealed": False},
            ],
        }
        assert _primary_failure_state(entry) == "not-available"

    def test_entry_with_no_recap_documents_returns_not_available(self):
        # The function defaults to 'not-available' when there are no
        # recap_documents at all on the entry (paperless primary entry
        # tagged by description text alone — rare). Documents the
        # no-main-doc fallthrough.
        from case_calendar.summary import _primary_failure_state

        assert _primary_failure_state({}) == "not-available"
        assert _primary_failure_state({"recap_documents": []}) == "not-available"


class TestSubstanceRecapDocumentsDedup:
    """Coverage for the dedup logic in _substance_recap_documents."""

    def test_dedup_skips_same_id_seen_under_another_predicate(self):
        # A recap_document whose description head-matches BOTH the
        # primary regex AND the disposition regex (rare but possible
        # — e.g. someone files something they call 'INDICTMENT AND
        # JUDGMENT' on a single doc). The dedup loop sees it under
        # the first predicate, marks the id seen, and skips on the
        # second pass.
        from case_calendar.summary import _substance_recap_documents

        # Construct two recap_docs: one matches primary only, one
        # would match BOTH primary and disposition (the dedup target).
        # Note: this is a synthetic edge case — actual filings rarely
        # have descriptions that hit both regexes. The point is to
        # exercise the dedup branch even if natural data wouldn't.
        entry = {
            "recap_documents": [
                {
                    "id": 100,
                    "attachment_number": None,
                    "description": "INDICTMENT",
                },
            ],
        }
        # Patch the disposition predicate to claim the same recap_doc
        # also looks dispositive, exercising the same-id-twice dedup
        # branch.
        import case_calendar.summary as s_mod

        orig = s_mod._SUBSTANCE_PREDICATES
        try:
            s_mod._SUBSTANCE_PREDICATES = (
                s_mod._matches_primary_document,
                lambda text: "INDICTMENT" in text,  # also matches the same doc
            )
            out = _substance_recap_documents(entry)
        finally:
            s_mod._SUBSTANCE_PREDICATES = orig
        # The single recap_doc appears exactly once despite matching
        # both predicates.
        assert [rd["id"] for rd in out] == [100]

    def test_dedup_handles_recap_doc_without_id(self):
        # Defensive: a recap_doc with no 'id' field — possible on
        # malformed test fixtures or older entries — still ends up
        # in the result (the dedup set tracks by id, so no-id docs
        # can't deduplicate; they pass through). Exercises the
        # 'if rid is not None' branch.
        from case_calendar.summary import _substance_recap_documents

        entry = {
            "recap_documents": [
                # No 'id' field at all.
                {"attachment_number": None, "description": "INDICTMENT"},
            ],
        }
        out = _substance_recap_documents(entry)
        assert len(out) == 1


class TestRefreshStaleSummarizeDocketReturnsNone:
    """Coverage for the 'if row:' falsy branch in refresh_stale and
    summarize_case. summarize_docket's None return is in principle
    defensive (the no-metadata case is filtered by _group_dockets_on_case
    before the call), but the check exists in case a future refactor
    relaxes that invariant — we patch summarize_docket to return None
    to exercise the branch."""

    def test_refresh_stale_skips_falsy_row(self, store, monkeypatch):
        from case_calendar import summary as summary_mod
        from case_calendar.summary import refresh_stale

        _seed_docket_meta(store, 1)
        # Mark stale so the regen path runs.
        store.mark_summary_stale("us-v-doe", *_DEFAULT_GROUP)
        monkeypatch.setattr(summary_mod, "summarize_docket", lambda **kw: None)
        cl = _FakeCourtListener({})
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )
        written = refresh_stale(cl=cl, store=store, cases=[case])
        # summarize_docket returned None → no entry added to written.
        assert "us-v-doe" not in written or written["us-v-doe"] == set()

    def test_summarize_case_skips_falsy_row(self, store, monkeypatch):
        from case_calendar import summary as summary_mod
        from case_calendar.summary import summarize_case

        _seed_docket_meta(store, 1)
        monkeypatch.setattr(summary_mod, "summarize_docket", lambda **kw: None)
        cl = _FakeCourtListener({})
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )
        rows = summarize_case(cl=cl, store=store, case=case, force=True)
        assert rows == []


class TestSummaryTruthfulnessGuard:
    """Deterministic post-generation guard — the hard backstop for the soft
    SUMMARY_SYSTEM_PROMPT rules against asserting facts from docket silence.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "X is charged. No disposition documents have been entered on the docket.",
            "X is charged. No disposition has been entered.",
            "X is charged; the case remains pending.",
            "X is charged. No hearings have been recorded.",
            "X is charged. No deadlines are set.",
            "X is charged. The docket shows no recent activity.",
            "Trial was cancelled and no new public scheduling order is reflected.",
            "X is charged with no apparent arrest reflected.",
            "X is charged; no public docket entries reflect an arrest.",
            # Reworded variants the original literal patterns missed (the
            # us-v-berezhnoy regression): "filed" not "entered", and the
            # "docket does not reflect any hearings" form. Hedging with "in
            # the available record" must not exempt procedural posture.
            "X is charged. No disposition documents have been filed in the available record.",
            "X is charged. The public docket does not reflect any scheduled hearings.",
            "X is charged; no judgment is reflected in the available record.",
            # Custody "we don't know" noise — pointless, must be OMITTED, not
            # announced (us-v-jin / us-v-gholinejad).
            "The custody status of the remaining defendants cannot be "
            "determined from the available record.",
            "It is unknown whether the defendant has been arrested.",
            # Speculative / conditional outcomes + obvious boilerplate — keep
            # the scheduled event, drop the hypothetical consequence
            # (us-v-martino).
            "Sentencing is set for June 3, 2026; X will be remanded to the "
            "Bureau of Prisons if a term of imprisonment is imposed.",
            "If convicted, the defendant faces up to 20 years.",
        ],
    )
    def test_absence_claims_flagged(self, text):
        assert summary._audit_summary_text(text) != []

    @pytest.mark.parametrize(
        "text",
        [
            # Documented custody status (past-tense, attributed to a source) —
            # not an absence-of-record construction, and not a "we don't know".
            "The information sheet indicates he had not been arrested as of that date.",
            # Ordinary documented financial fact — "restitution" is not a
            # procedural-posture noun, so "no restitution" must not trip.
            "He was sentenced to 60 months with no restitution ordered.",
            # A scheduled event WITH its date and no conditional consequence.
            "Sentencing is scheduled for June 3, 2026.",
        ],
    )
    def test_documented_or_scheduled_facts_not_flagged(self, text):
        assert summary._audit_summary_text(text, source_text="") == []

    def test_custody_claim_flagged_when_ungrounded(self):
        # No source document mentions custody status -> "at large" is an
        # inference from docket silence -> flagged.
        v = summary._audit_summary_text(
            "All four defendants remain at large.", source_text="indictment body text"
        )
        assert v and "custody/flight" in v[0]

    def test_fugitive_claim_flagged_when_ungrounded(self):
        assert (
            summary._audit_summary_text("X remains a fugitive.", source_text="") != []
        )

    def test_custody_claim_allowed_when_grounded_in_documents(self):
        # A source document actually states the defendants are at large ->
        # the summary is permitted to repeat it -> NOT flagged.
        assert (
            summary._audit_summary_text(
                "The defendants remain at large.",
                source_text="The indictment notes the defendants remain at large abroad.",
            )
            == []
        )

    def test_clean_summary_has_no_violations(self):
        assert (
            summary._audit_summary_text(
                "X was charged with wire fraud and pled guilty on May 1, 2026.",
                source_text="plea agreement text",
            )
            == []
        )

    def test_refusal_sentence_is_exempt(self):
        assert (
            summary._audit_summary_text(summary.llm.SUMMARY_INSUFFICIENT_DOCUMENTS)
            == []
        )

    def test_empty_text_has_no_violations(self):
        assert summary._audit_summary_text("") == []


def _queue_llm(monkeypatch, *texts):
    """Stub generate_docket_summary to return queued texts per call.

    The last queued text repeats if called more times than provided. Records
    each call's kwargs so tests can assert whether `correction` was passed on
    the retry.
    """
    calls: list[dict[str, Any]] = []

    def _fake(**kwargs):
        calls.append(kwargs)
        text = texts[min(len(calls) - 1, len(texts) - 1)]
        return (text, "fake/model-v1")

    monkeypatch.setattr(summary.llm, "generate_docket_summary", _fake)
    return calls


def _indictment_case(store, patch_pdf, *, primary_text="INDICTMENT body text..."):
    """A one-docket case whose sole entry is an indictment carrying
    ``primary_text`` (extracted as recap_document 500). Cold local cache, so
    ``summarize_docket`` reads the entry from the FakeCourtListener pages."""
    _seed_docket_meta(store, 1)
    patch_pdf["texts"] = {500: primary_text}
    cl = _FakeCourtListener(
        {
            (1, "date_filed"): [
                {
                    "id": 10,
                    "description": "INDICTMENT",
                    "date_filed": "2024-01-01",
                    "entry_number": 1,
                    "recap_documents": [{"id": 500}],
                }
            ],
            (1, "-date_filed"): [],
        }
    )
    case = _Case(case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber")
    return cl, case


class TestSummaryGuardRetry:
    """End-to-end retry-then-keep+WARN behavior through summarize_docket."""

    def _setup(self, store, patch_pdf, *, primary_text="INDICTMENT body text..."):
        return _indictment_case(store, patch_pdf, primary_text=primary_text)

    def test_clean_draft_no_retry(self, store, patch_pdf, monkeypatch):
        cl, case = self._setup(store, patch_pdf)
        # No absence/custody claim and no ungrounded date/amount -> clean on
        # every guard axis, so neither retry path fires.
        calls = _queue_llm(monkeypatch, "X was charged and later pled guilty.")
        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None
        assert len(calls) == 1  # no retry
        assert "correction" not in calls[0]
        assert row["summary"].startswith("X was charged")

    def test_tripping_draft_retried_and_cleared(self, store, patch_pdf, monkeypatch):
        cl, case = self._setup(store, patch_pdf)
        calls = _queue_llm(
            monkeypatch,
            "X is charged. No disposition has been entered.",  # draft trips
            "X is charged with wire fraud; the status is unknown.",  # clean retry
        )
        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None
        assert len(calls) == 2  # retried once
        assert calls[1].get("correction")  # retry carried the correction
        # The clean retry is what gets stored.
        assert "No disposition" not in row["summary"]
        assert row["summary"].endswith("the status is unknown.")

    def test_persistent_violation_keeps_fewer_and_warns(
        self, store, patch_pdf, monkeypatch, caplog
    ):
        import logging

        cl, case = self._setup(store, patch_pdf)
        # Draft has TWO violations (distinct patterns: no-hearings + remains-
        # pending); retry still trips but with ONE -> keep the retry.
        calls = _queue_llm(
            monkeypatch,
            "No hearings have been recorded; the case remains pending.",
            "X is charged. No disposition has been entered.",
        )
        with caplog.at_level(logging.WARNING):
            row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None
        assert len(calls) == 2
        assert "No disposition" in row["summary"]  # the fewer-violation retry
        assert any("STILL tripped" in r.message for r in caplog.records)

    def test_retry_worse_keeps_original(self, store, patch_pdf, monkeypatch):
        cl, case = self._setup(store, patch_pdf)
        # Draft: ONE violation; retry: TWO (distinct patterns: no-hearings +
        # docket-does-not-reflect) -> keep the original draft.
        calls = _queue_llm(
            monkeypatch,
            "X is charged. The case remains pending.",
            "No hearings have been recorded. The docket does not reflect any deadlines.",
        )
        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None
        assert len(calls) == 2
        assert "remains pending" in row["summary"]  # original kept

    def test_custody_claim_grounded_in_doc_not_retried(
        self, store, patch_pdf, monkeypatch
    ):
        # The indictment text itself says the defendants are at large, so the
        # summary repeating it is grounded -> guard does not fire -> no retry.
        cl, case = self._setup(
            store,
            patch_pdf,
            primary_text="Indictment: defendants remain at large abroad.",
        )
        calls = _queue_llm(monkeypatch, "The defendants remain at large.")
        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None
        assert len(calls) == 1  # no retry — grounded in the document
        assert "at large" in row["summary"]


class TestSummaryForfeitureGuard:
    """Tier-4 redundant-forfeiture guard — a forfeiture money judgment whose
    amount equals a restitution amount also stated reads as a doubled total
    (us-v-knoot: $15,100 + $15,100 = "$30,200"). Detect, retry to omit it, and
    deterministically strip the forfeiture clause if the retry still states it.
    """

    @pytest.mark.parametrize(
        "text",
        [
            # Leading clause, with the inline-link marker still on the phrase
            # (the guard runs before link resolution).
            "He was ordered to pay $15,100 in restitution. A [forfeiture money "
            "judgment](doc:D5) of $15,100 was also entered against him, and he "
            "is appealing his conviction.",
            # Embedded clause.
            "He was ordered to pay $15,100 in restitution, with a forfeiture "
            "money judgment of $15,100 also entered against him.",
            # Reversed wording "$X forfeiture money judgment".
            "Restitution of $15,100 was ordered, and a $15,100 forfeiture money "
            "judgment was entered.",
        ],
    )
    def test_redundant_pair_flagged(self, text):
        assert summary._audit_summary_forfeiture(text) != []

    @pytest.mark.parametrize(
        "text",
        [
            # Different amounts — a real, distinct obligation; keep both.
            "He was sentenced to a $40,000 fine, with a forfeiture money "
            "judgment of $17,500 entered against him.",
            # Identified-property forfeiture (not a money judgment) — stays.
            "He was ordered to pay $15,100 in restitution and to forfeit a 2019 "
            "BMW and a cryptocurrency wallet.",
            # Forfeiture money judgment but no restitution at all — nothing to
            # double up with.
            "A forfeiture money judgment of $15,100 was entered against him.",
            "",
        ],
    )
    def test_non_redundant_not_flagged(self, text):
        assert summary._audit_summary_forfeiture(text) == []

    def test_strip_leading_clause_keeps_trailing_appeal(self):
        text = (
            "Knoot was sentenced to 18 months, $15,100 in restitution, and a "
            "$100 special assessment. A [forfeiture money judgment](doc:D5) of "
            "$15,100 was also entered against him, and he is appealing his "
            "conviction."
        )
        out = summary._strip_redundant_forfeiture(text)
        assert "forfeiture" not in out.lower()
        assert "$15,100 in restitution" in out
        assert out.endswith("He is appealing his conviction.")

    def test_strip_embedded_clause_keeps_restitution(self):
        text = (
            "He was ordered to pay $15,100 in restitution, and a $100 special "
            "assessment, with a forfeiture money judgment of $15,100 also "
            "entered against him."
        )
        out = summary._strip_redundant_forfeiture(text)
        assert "forfeiture" not in out.lower()
        assert (
            out
            == "He was ordered to pay $15,100 in restitution, and a $100 special assessment."
        )

    def test_strip_standalone_sentence_dropped(self):
        text = (
            "He was sentenced to $15,100 in restitution. A forfeiture money "
            "judgment of $15,100 was entered against him. He has appealed."
        )
        out = summary._strip_redundant_forfeiture(text)
        assert "forfeiture" not in out.lower()
        assert out == "He was sentenced to $15,100 in restitution. He has appealed."

    def test_strip_noop_when_not_redundant(self):
        text = "A forfeiture money judgment of $17,500 was entered; restitution was $15,100."
        assert summary._strip_redundant_forfeiture(text) == text

    @pytest.mark.parametrize(
        "raw,norm",
        [
            ("15,100", "15100"),
            ("15100.00", "15100"),  # trailing cents zeros + the dot trimmed
            ("15100.50", "15100.5"),
            ("14,655,096.24", "14655096.24"),
        ],
    )
    def test_norm_money(self, raw, norm):
        assert summary._norm_money(raw) == norm

    def test_decimal_amounts_compare_equal_across_formats(self):
        # "$15,100.00" (a judgment's printed cents) vs "$15,100" (restitution
        # prose) must still be recognized as the same redundant amount.
        text = (
            "He was ordered to pay $15,100 in restitution, with a forfeiture "
            "money judgment of $15,100.00 also entered against him."
        )
        assert summary._audit_summary_forfeiture(text) != []

    def test_redundant_draft_retried_and_cleared(self, store, patch_pdf, monkeypatch):
        # The $15,100 figure is in the indictment text, so the grounding guard
        # is satisfied and only the forfeiture guard fires.
        cl, case = _indictment_case(
            store, patch_pdf, primary_text="INDICTMENT. Loss and restitution: $15,100."
        )
        calls = _queue_llm(
            monkeypatch,
            "X pled guilty and was ordered to pay $15,100 in restitution; a "
            "forfeiture money judgment of $15,100 was also entered against him.",
            "X pled guilty and was ordered to pay $15,100 in restitution.",
        )
        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None
        assert len(calls) == 2  # retried once
        assert calls[1].get("correction")  # retry carried the forfeiture correction
        assert "forfeiture" not in row["summary"].lower()  # clean retry adopted
        assert "$15,100 in restitution" in row["summary"]

    def test_persistent_redundancy_stripped_and_warns(
        self, store, patch_pdf, monkeypatch, caplog
    ):
        import logging

        cl, case = _indictment_case(
            store, patch_pdf, primary_text="INDICTMENT. Loss and restitution: $15,100."
        )
        # Both draft AND retry state the redundant forfeiture -> the original
        # draft is kept and the forfeiture clause is deterministically stripped.
        calls = _queue_llm(
            monkeypatch,
            "X pled guilty and was ordered to pay $15,100 in restitution; a "
            "forfeiture money judgment of $15,100 was also entered against him.",
            "X pled guilty and was ordered to pay $15,100 in restitution; a "
            "forfeiture money judgment of $15,100 was also entered against him.",
        )
        with caplog.at_level(logging.WARNING):
            row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None
        assert len(calls) == 2
        assert "forfeiture" not in row["summary"].lower()  # stripped
        assert "$15,100 in restitution" in row["summary"]
        assert any("forfeiture-redundancy" in r.message for r in caplog.records)


class TestSummaryOperatorAttributionGuard:
    """The aggregation_note / NOTE FROM OPERATOR are context FOR the model, not
    material to attribute to subscribers. The model occasionally leaks the
    provenance ("the operator notes that he has appealed his conviction" —
    us-v-knoot); the fact is trusted, so the deterministic cleanup strips the
    attribution lead-in and keeps the clause.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "He was sentenced to 18 months; the operator notes that he has appealed.",
            "According to the operator, the defendant has fled.",
            "The aggregation note indicates that the cases are related.",
            "Per the operator's note, X was extradited.",
            "The operator-provided note states that the docket was reopened.",
        ],
    )
    def test_attribution_flagged(self, text):
        assert summary._audit_summary_attribution(text) != []

    @pytest.mark.parametrize(
        "text",
        [
            # "operator" in an unrelated sense — must NOT trip.
            "Chapman ran a laptop-farm operator network and pled guilty.",
            "",
            "He has appealed his conviction.",
        ],
    )
    def test_non_attribution_not_flagged(self, text):
        assert summary._audit_summary_attribution(text) == []

    def test_strip_keeps_fact_drops_attribution_midsentence(self):
        text = (
            "Knoot was sentenced to 18 months and ordered to self-surrender by "
            "June 30, 2026; the operator notes that he has appealed his conviction."
        )
        out = summary._strip_operator_attribution(text)
        assert "operator" not in out.lower()
        assert out.endswith("; he has appealed his conviction.")

    def test_strip_capitalizes_at_sentence_start(self):
        out = summary._strip_operator_attribution(
            "According to the operator, the defendant has fled. He remains abroad."
        )
        assert out == "The defendant has fled. He remains abroad."

    def test_strip_at_sentence_start_followed_by_non_letter(self):
        # Lead-in opens the text but the kept clause starts with a non-letter
        # ("$"), so there is nothing to capitalize — the clause is kept as-is.
        out = summary._strip_operator_attribution(
            "According to the operator, $2 million was forfeited."
        )
        assert out == "$2 million was forfeited."

    def test_strip_noop_when_clean(self):
        text = "He has appealed his conviction."
        assert summary._strip_operator_attribution(text) == text

    def test_leak_removed_end_to_end(self, store, patch_pdf, monkeypatch, caplog):
        import logging

        cl, case = _indictment_case(store, patch_pdf)
        # The model leaks the aggregation-note provenance; the deterministic
        # cleanup removes the lead-in and keeps the fact (no retry needed).
        _queue_llm(
            monkeypatch,
            "X pled guilty; the operator notes that he has appealed his conviction.",
        )
        with caplog.at_level(logging.WARNING):
            row = summarize_docket(
                cl=cl,
                store=store,
                case=case,
                docket_id=1,
                aggregation_note="The same defendant is appealing his conviction.",
            )
        assert row is not None
        assert "operator" not in row["summary"].lower()
        assert "he has appealed his conviction" in row["summary"]
        assert any("attribution leak" in r.message for r in caplog.records)


class TestSummaryGroundingGuard:
    """Tier-3 grounding guard — dates / dollar amounts not traceable to the
    scaffold or the source documents trigger a retry, then a deterministic
    sentence strip so the ungrounded figure never publishes, plus a WARN.
    """

    def test_date_in_known_dates_not_flagged(self):
        assert (
            summary._audit_summary_grounding(
                "Sentenced on May 6, 2026.", known_dates={"2026-05-06"}, source_text=""
            )
            == []
        )

    def test_date_in_corpus_various_formats_not_flagged(self):
        for corpus in (
            "judgment dated May 6, 2026",
            "entered 5/6/2026 by the court",
            "filed 2026-05-06",
        ):
            assert (
                summary._audit_summary_grounding(
                    "Sentenced on May 6, 2026.", known_dates=set(), source_text=corpus
                )
                == []
            ), corpus

    def test_fabricated_date_flagged(self):
        out = summary._audit_summary_grounding(
            "A hearing was set for June 9, 2026.", known_dates=set(), source_text="x"
        )
        assert out and "ungrounded date" in out[0]

    def test_date_split_across_lines_in_corpus_not_flagged(self):
        # The us-v-stryzhak false positive: pypdf extracted the forfeiture
        # order's date as "June 26,\n\n2024". Whitespace normalization must
        # let the summary's "June 26, 2024" match it.
        assert (
            summary._audit_summary_grounding(
                "seized on June 26, 2024 in Barcelona",
                known_dates=set(),
                source_text="one Apple MacBook Pro seized on or about June 26,\n\n2024,",
            )
            == []
        )

    def test_month_only_range_not_flagged(self):
        # No day -> not extracted -> never flagged (offense-conduct ranges).
        assert (
            summary._audit_summary_grounding(
                "between December 2020 and October 2022",
                known_dates=set(),
                source_text="",
            )
            == []
        )

    def test_amount_in_corpus_not_flagged(self):
        # Synthetic figure — the test checks "amount present in corpus is not
        # flagged", independent of any real case's restitution.
        assert (
            summary._audit_summary_grounding(
                "ordered $123,456.78 in restitution",
                known_dates=set(),
                source_text="restitution of $123,456.78 to the victims",
            )
            == []
        )

    def test_fabricated_amount_flagged(self):
        out = summary._audit_summary_grounding(
            "a $5,000,000 forfeiture judgment",
            known_dates=set(),
            source_text="no figures here",
        )
        assert out and "ungrounded amount" in out[0]

    def test_million_approximation_skipped(self):
        # "X million" is an approximation we can't reliably verify -> never flag.
        assert (
            summary._audit_summary_grounding(
                "extorted over $16 million in Bitcoin",
                known_dates=set(),
                source_text="",
            )
            == []
        )

    def test_refusal_sentence_exempt(self):
        assert (
            summary._audit_summary_grounding(
                summary.llm.SUMMARY_INSUFFICIENT_DOCUMENTS,
                known_dates=set(),
                source_text="",
            )
            == []
        )

    def test_grounding_dates_collects_scaffold_and_doc_filing_dates(self):
        kd = summary._grounding_dates(
            {
                # A dateless hearing / deadline (null date column) is skipped,
                # not crashed on.
                "hearings": [
                    {"starts_at_utc": "2026-07-06T16:00:00+00:00"},
                    {"starts_at_utc": None},
                ],
                "deadlines": [
                    {"due_at_utc": "2026-06-04T21:00:00+00:00"},
                    {"deadline_key": "conditional-no-date"},
                ],
                "primary_documents": [{"date_filed": "2025-06-24"}],
                "disposition_documents": [],
            }
        )
        assert kd == {"2026-07-06", "2026-06-04", "2025-06-24"}

    def test_ungrounded_date_retry_clears(self, store, patch_pdf, monkeypatch, caplog):
        # Draft cites a date in no document; the retry drops it -> the clean
        # retry is stored, the figure never publishes, and a WARN records the
        # retry for operator review.
        import logging

        cl, case = _indictment_case(
            store, patch_pdf, primary_text="INDICTMENT charging wire fraud, no dates."
        )
        calls = _queue_llm(
            monkeypatch,
            "X was charged; a hearing is set for June 9, 2026.",  # ungrounded date
            "X was charged with wire fraud.",  # clean retry
        )
        with caplog.at_level(logging.WARNING):
            row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None
        assert len(calls) == 2  # retried once
        assert calls[1].get("correction")  # retry carried the grounding correction
        assert "June 9, 2026" not in row["summary"]
        assert row["summary"] == "X was charged with wire fraud."
        assert any(
            "retrying generation once to remove" in r.message for r in caplog.records
        )

    def test_ungrounded_persists_strips_offending_sentence(
        self, store, patch_pdf, monkeypatch, caplog
    ):
        # The model repeats the ungrounded date on the retry. The offending
        # SENTENCE is removed deterministically; the grounded sentence stays.
        import logging

        cl, case = _indictment_case(
            store, patch_pdf, primary_text="INDICTMENT charging wire fraud, no dates."
        )
        calls = _queue_llm(
            monkeypatch,
            "X was charged with wire fraud. A hearing is set for June 9, 2026.",
        )
        with caplog.at_level(logging.WARNING):
            row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None
        assert len(calls) == 2  # one retry, then deterministic strip
        assert "June 9, 2026" not in row["summary"]
        assert row["summary"] == "X was charged with wire fraud."
        assert any(
            "STILL carried ungrounded facts after retry" in r.message
            for r in caplog.records
        )

    def test_ungrounded_strip_empties_falls_back_to_refusal(
        self, store, patch_pdf, monkeypatch
    ):
        # Every sentence carries an ungrounded figure -> stripping empties the
        # summary -> the canonical refusal sentence is stored.
        cl, case = _indictment_case(
            store, patch_pdf, primary_text="INDICTMENT body, no dates or amounts."
        )
        calls = _queue_llm(monkeypatch, "A hearing is set for June 9, 2026.")
        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None
        assert len(calls) == 2
        assert row["summary"] == summary.llm.SUMMARY_INSUFFICIENT_DOCUMENTS

    def test_retry_reduces_ungrounded_then_strips_residual(
        self, store, patch_pdf, monkeypatch
    ):
        # Draft has TWO ungrounded figures; the retry drops one but keeps the
        # other -> the retry is adopted (fewer violations, no tier-1/2
        # regression) and the residual sentence is then stripped.
        cl, case = _indictment_case(
            store, patch_pdf, primary_text="INDICTMENT charging fraud, no figures."
        )
        calls = _queue_llm(
            monkeypatch,
            "A fine of $9,999 was set. A hearing is on June 9, 2026.",  # 2 ungrounded
            "X was charged with fraud. A hearing is on June 9, 2026.",  # 1 ungrounded
        )
        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None
        assert len(calls) == 2
        assert "$9,999" not in row["summary"]
        assert "June 9, 2026" not in row["summary"]
        assert row["summary"] == "X was charged with fraud."

    def test_retry_with_tier12_regression_rejected_keeps_original(
        self, store, patch_pdf, monkeypatch
    ):
        # The retry clears the ungrounded date but introduces an
        # absence-of-record claim. We refuse to adopt a retry that trades a
        # grounding fix for a tier-1/2 regression -> keep the original draft
        # and strip its ungrounded sentence instead.
        cl, case = _indictment_case(
            store, patch_pdf, primary_text="INDICTMENT charging fraud, no figures."
        )
        calls = _queue_llm(
            monkeypatch,
            "X was charged with fraud. A hearing is on June 9, 2026.",  # ungrounded date
            "No disposition has been entered.",  # clean grounding, tier-1/2 regression
        )
        row = summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert row is not None
        assert len(calls) == 2
        assert "No disposition" not in row["summary"]  # the regressing retry rejected
        assert "June 9, 2026" not in row["summary"]  # original's bad sentence stripped
        assert row["summary"] == "X was charged with fraud."

    def test_aggregation_note_date_is_grounded_not_flagged(
        self, store, patch_pdf, monkeypatch, caplog
    ):
        # us-v-gholinejad: a date that lives ONLY in the operator's
        # aggregation_note (a sentencing date from a sibling district docket,
        # absent from this appeal docket's own records) must NOT trip the
        # grounding guard — the note is trusted metadata the model may cite.
        import logging

        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {500: "INDICTMENT body, no dates."}
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "entry_number": 1,
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )
        calls = _queue_llm(
            monkeypatch,
            "X was sentenced on November 3, 2025; this is the direct appeal.",
        )
        with caplog.at_level(logging.WARNING):
            row = summarize_docket(
                cl=cl,
                store=store,
                case=case,
                docket_id=1,
                aggregation_note="Sentenced on November 3, 2025 to 72 months imprisonment.",
            )
        assert row is not None
        # The date is sourced from the aggregation note -> grounded, so no
        # grounding retry fires and the date stays in the stored summary.
        assert len(calls) == 1
        assert "November 3, 2025" in row["summary"]
        assert not any("ungrounded" in r.message for r in caplog.records)


class TestStripUngroundedSentences:
    """`_strip_ungrounded_sentences` / `_split_sentences` — the deterministic
    backstop that removes whole sentences still carrying an ungrounded figure
    after the retry, keeping the grounded ones (refusal if none survive)."""

    def test_drops_only_ungrounded_sentence(self):
        out = summary._strip_ungrounded_sentences(
            "He was charged with fraud. A hearing is set for June 9, 2026.",
            known_dates=set(),
            source_text="",
        )
        assert out == "He was charged with fraud."

    def test_keeps_grounded_date_sentence(self):
        text = "He was charged. Sentencing is set for May 6, 2026."
        out = summary._strip_ungrounded_sentences(
            text, known_dates={"2026-05-06"}, source_text=""
        )
        assert out == text  # the date is grounded -> nothing stripped

    def test_us_abbreviation_not_treated_as_boundary(self):
        # "U.S. Attorney" must not be split into its own sentence: the first
        # sentence (no figure) survives intact; only the ungrounded $9,999
        # sentence is removed.
        out = summary._strip_ungrounded_sentences(
            "The U.S. Attorney charged him with fraud. A penalty of $9,999 was imposed.",
            known_dates=set(),
            source_text="",
        )
        assert out == "The U.S. Attorney charged him with fraud."

    def test_all_ungrounded_returns_refusal(self):
        out = summary._strip_ungrounded_sentences(
            "A fine of $9,999 was set. Another $8,888 was added.",
            known_dates=set(),
            source_text="",
        )
        assert out == summary.llm.SUMMARY_INSUFFICIENT_DOCUMENTS

    def test_split_keeps_abbreviations(self):
        assert summary._split_sentences("The U.S. Attorney acted. He won.") == [
            "The U.S. Attorney acted.",
            "He won.",
        ]
        assert summary._split_sentences("Mr. Smith appeared. He won.") == [
            "Mr. Smith appeared.",
            "He won.",
        ]

    def test_split_breaks_before_link_bracket(self):
        assert summary._split_sentences(
            "He pled guilty. [The court](doc:D1) sentenced him."
        ) == ["He pled guilty.", "[The court](doc:D1) sentenced him."]

    def test_split_single_sentence_unchanged(self):
        assert summary._split_sentences("X was charged with wire fraud.") == [
            "X was charged with wire fraud."
        ]


class TestRestitutionUnreadableDetector:
    """`_restitution_amount_unreadable`: a granted restitution order present
    but with no legibly-extractable dollar figure (us-v-chapman: handwritten
    amounts that OCR to noise)."""

    def test_restitution_order_no_figure_flags(self):
        # Chapman shape: restitution order garbled, forfeiture order clean.
        # Only the restitution order is consulted, so the readable forfeiture
        # figure does NOT make it "readable".
        docs = [
            {
                "description": "ORDER ... Government's motion for order of restitution",
                "text": "restitution as follows: Total AD2, O52. 1S",
            },
            {
                "description": "ORDER OF FORFEITURE",
                "text": "forfeit $284,666.92 in identified funds",
            },
        ]
        assert summary._restitution_amount_unreadable(docs) is True

    def test_restitution_order_with_clean_figure_not_flagged(self):
        docs = [
            {
                "description": "AMENDED JUDGMENT ... restitution",
                "text": "ordered to pay $402,052.15 in restitution to six victims",
            }
        ]
        assert summary._restitution_amount_unreadable(docs) is False

    def test_no_restitution_order_not_flagged(self):
        docs = [
            {"description": "ORDER OF FORFEITURE", "text": "forfeit $284,666.92"},
            {"description": "JUDGMENT in a Criminal Case", "text": "92 months"},
        ]
        assert summary._restitution_amount_unreadable(docs) is False

    def test_empty_not_flagged(self):
        assert summary._restitution_amount_unreadable([]) is False

    def test_not_uploaded_restitution_order_flags(self, patch_pdf):
        # Same suppression when the restitution order's DOCUMENT isn't
        # uploaded to RECAP (or is sealed): _attach_text falls back to the
        # docket description, which carries no dollar amount, so the detector
        # fires exactly as it does for the garbled hand-filled case.
        patch_pdf["texts"] = {}  # no extractable PDF text for the order
        entry = {
            "id": 99,
            "entry_number": 49,
            "description": (
                "ORDER as to Defendant (1): Upon consideration of the "
                "Government's Unopposed Motion for order of restitution, it is "
                "hereby ORDERED that the motion is GRANTED."
            ),
            "short_description": "",
            "recap_documents": [{"id": 600, "is_available": False}],
            "date_filed": "2025-08-25",
        }
        docs = summary._attach_text([entry], allow_description_fallback=True)
        assert docs and summary._RESTITUTION_FIGURE_RE.search(docs[0]["text"]) is None
        assert summary._restitution_amount_unreadable(docs) is True


class TestRestitutionAdvisoryWiring:
    def test_summarize_docket_passes_restitution_flag(
        self, store, patch_pdf, monkeypatch
    ):
        # When the detector fires, summarize_docket must pass
        # restitution_unreadable=True into the generator (which renders the
        # DOCKET FINANCIAL ADVISORY). Detector is forced here so the wiring
        # test is decoupled from the disposition classifier.
        _seed_docket_meta(store, 1)
        patch_pdf["texts"] = {500: "INDICTMENT body text for the case."}
        cl = _FakeCourtListener(
            {
                (1, "date_filed"): [
                    {
                        "id": 10,
                        "description": "INDICTMENT",
                        "date_filed": "2024-01-01",
                        "entry_number": 1,
                        "recap_documents": [{"id": 500}],
                    }
                ],
                (1, "-date_filed"): [],
            }
        )
        case = _Case(
            case_id="us-v-doe", name="US v. Doe", dockets=[1], calendar="cyber"
        )
        monkeypatch.setattr(
            summary, "_restitution_amount_unreadable", lambda docs: True
        )
        calls = []

        def fake(**kw):
            calls.append(kw)
            return ("X was ordered to pay restitution.", "fake/model-v1")

        monkeypatch.setattr(summary.llm, "generate_docket_summary", fake)
        summarize_docket(cl=cl, store=store, case=case, docket_id=1)
        assert calls and calls[0]["restitution_unreadable"] is True
