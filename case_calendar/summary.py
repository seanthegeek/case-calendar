"""Per-docket case summary generation.

A separate, opt-in pass from the per-entry extractor. For each docket, we:

  1. Walk the docket via the CourtListener API to find the primary document
     (latest indictment / superseding indictment / information for criminal
     dockets; latest amended complaint / complaint / petition for civil).
  2. Look for any disposition documents (judgment, plea agreement, verdict,
     dismissal, dispositive memo/order) on the newest end of the docket.
  3. Pull PDF text for those entries through the existing pdf.py fallback
     chain (CourtListener plain_text → IA mirror via pypdf → optional tesseract OCR).
  4. Feed the docs plus a structured-events scaffold (hearings/deadlines the
     extractor already recorded, with their statuses) to a higher-tier LLM
     (Sonnet by default) and persist the resulting prose to the
     ``case_summaries`` table.

The pipeline is intentionally independent of the cheap extractor pipeline:
primary documents rarely match the hearing-relevance regex, so we don't
have their text in the local entries table. Hitting CourtListener directly here keeps
the extractor's storage shape unchanged.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Iterable, Optional
from zoneinfo import ZoneInfo

from . import llm, pdf
from .courtlistener import API_BASE, CourtListener
from .courts import tz_for
from .store import Store

if TYPE_CHECKING:
    # ``CaseConfig`` lives in sync.py, which itself imports this module to
    # call ``is_primary_document`` / ``is_disposition`` on each entry.
    # The runtime import would form a cycle, but ``from __future__ import
    # annotations`` (above) defers signature evaluation to strings so we
    # only need it for type checkers.
    from .sync import CaseConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prime-document detection
# ---------------------------------------------------------------------------

# Match the leading word(s) of an entry description. CourtListener descriptions are
# typically uppercase or title-case sentences. We anchor at the start to
# avoid catching mentions inside other entries ("Response to Motion to
# Dismiss the Indictment" should NOT match the primary-document pattern).
# The optional "amended / superseding" qualifier captures the latest-wins
# logic naturally: each variant gets its own entry, and we sort by
# entry_number descending when picking.
_PRIMARY_DOCUMENT_RE = re.compile(
    r"""^\s*
    (?:\d+\s+)?                                              # sometimes prefixed with entry number
    (?:[A-Z]+\s+)?                                           # optional REDACTED/SEALED prefix
    (?:
        (?:SUPERSEDING|AMENDED|FIRST\sAMENDED|SECOND\sAMENDED|
           THIRD\sAMENDED|FOURTH\sAMENDED|CORRECTED|REDACTED|
           SEALED|UNSEALED)\s+
    )?
    (?:
        INDICTMENT
        | INFORMATION
        | COMPLAINT
        | PETITION\sFOR\sWRIT
        | PETITION
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

_DISPOSITION_RE = re.compile(
    r"""^\s*
    (?:\d+\s+)?
    (?:
        JUDGMENT
        | FINAL\sJUDGMENT
        | VERDICT\sFORM
        | VERDICT
        | ORDER\sOF\sDISMISSAL
        | ORDER\sGRANTING\sMOTION\sTO\sDISMISS
        | STIPULATION\sOF\sDISMISSAL
        | STIPULATED\sDISMISSAL
        | NOTICE\sOF\sVOLUNTARY\sDISMISSAL
        | PLEA\sAGREEMENT
        | (?:FACTUAL\s+)?PROFFER\s+STATEMENT
        | (?:AMENDED\s+)?REPORT\s+AND\s+RECOMMENDATIONS?\s+ON\s+
          (?:PLEA\s+OF\s+GUILTY|CHANGE\s+OF\s+PLEA)
        | SENTENCING\sJUDGMENT
        | SENTENCE
        | SETTLEMENT\sAGREEMENT
        | MEMORANDUM\sOPINION
        | MEMORANDUM\sORDER
        | OPINION\sAND\sORDER
        | OPINION
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Anything mentioning one of these keywords anywhere in the description is
# treated as a disposition-class signal — events that materially change
# "where does the case stand". Covers both criminal and civil practice plus
# appellate post-decision events. Each keyword is word-boundary anchored so
# we don't split-match inside unrelated words. The anchored regex above is
# now mostly redundant once these keyword matches are in play, but kept for
# explicit head-anchored coverage of dispositions that don't contain any of
# these keywords.
_DISPOSITION_KEYWORD_RE = re.compile(
    r"\b(?:"
    # Criminal — sentencing-phase and verdict-phase events.
    r"sentencing"
    r"|mistrials?"
    r"|acquit(?:tals?|ted)"
    r"|forfeitures?"
    r"|nolle\s+prosequi"
    r"|nolle\s+prossed"
    # Guilty-plea phrasings — paperless minute orders for change-of-plea
    # hearings and R&Rs that document the plea typically say "pled guilty"
    # or "plea of guilty" rather than carrying any of the head-anchored
    # disposition vocabulary. We avoid bare "guilty plea" because the
    # arraignment phrase "not guilty plea entered" would slip through.
    r"|pled\s+guilty"
    r"|pleads?\s+guilty"
    r"|plea\s+of\s+guilty"
    # The trial-court order adopting the magistrate's R&R on the plea is
    # a disposition-class doc but its head is just "ORDER ADOPTING REPORT
    # AND RECOMMENDATION" — the plea-specific phrasing only appears in
    # the body. Match the R&R-on-plea reference there. Scoped to
    # plea/change-of-plea R&Rs so adoption of procedural R&Rs (IFP,
    # discovery sanctions) doesn't slip through as a disposition doc.
    r"|(?:report\s+and\s+recommendations?|r&r)\s+on\s+"
    r"(?:plea\s+of\s+guilty|change\s+of\s+plea)"
    # Civil — class action, removal, default, and injunctive relief.
    r"|class\s+certification"
    r"|remand(?:ed)?"
    r"|entry\s+of\s+default"
    r"|tros?"
    r"|temporary\s+restraining\s+orders?"
    r"|injunctions?"
    # Cross-domain — judgments, dismissals, appellate dispositions.
    r"|judg(?:e)?ments?"
    r"|decrees?"
    r"|dismiss(?:al|als|ed)"
    r"|mandates?"
    r"|affirm(?:s|ed|ance|ances)?"
    r"|reversed"
    r"|vacated"
    r")\b",
    re.IGNORECASE,
)

# Negative override: any mention of "conference" in the description disables
# the disposition match. Scheduling entries (Status Conference, Pretrial
# Conference, Settlement Conference, Telephonic Conference re: Sentencing,
# etc.) are exactly the false-positive class we want to filter — they're
# logistics, not the disposition itself. The keyword regex above is broad on
# purpose, so we lean on this negative to reject the scheduling chatter.
_DISPOSITION_NEGATIVE_RE = re.compile(r"\bconferences?\b", re.IGNORECASE)


def _entry_description_head(entry: dict[str, Any]) -> str:
    """Get the most informative leading text for an entry."""
    for key in ("short_description", "description"):
        v = (entry.get(key) or "").strip()
        if v:
            return v
    # Fall back to the first recap_document's description.
    for rd in entry.get("recap_documents") or []:
        v = (rd.get("description") or "").strip()
        if v:
            return v
    return ""


# ---------------------------------------------------------------------------
# Document-type predicates
# ---------------------------------------------------------------------------
#
# Each predicate below classifies ONE piece of text (an entry's description,
# or a single recap_document's description) and returns True iff it looks
# like that document type. They're pure text functions so the generic
# helpers below (entry-level classifier + per-recap_document scan) and the
# extractor can share the exact same matching logic.
#
# Adding a new document type — for instance, if we ever wanted to single
# out "operative scheduling order" or "motion for preliminary injunction"
# specifically — is just defining one more `_matches_*` predicate and
# wrapping it with the generic helpers; the entry-level classifier and
# extractor priority don't need to change.


def _matches_primary_document(text: str) -> bool:
    """Indictment / Information / Complaint / Petition at the head."""
    return bool(_PRIMARY_DOCUMENT_RE.match((text or "").strip()))


def _matches_disposition_broad(text: str) -> bool:
    """Broad disposition signal — used for stale-flag flipping.

    Wider than :func:`_matches_disposition_document` — a "Motion for
    Preliminary Injunction" matches here (the motion's outcome is a
    disposition-class signal worth refreshing the summary on), but the
    motion itself is NOT the ruling document and the stricter
    predicate rejects it.
    """
    text = (text or "").strip()
    if not text or _DISPOSITION_NEGATIVE_RE.search(text):
        return False
    if _DISPOSITION_RE.match(text):
        return True
    return bool(_DISPOSITION_KEYWORD_RE.search(text))


def _matches_disposition_document(text: str) -> bool:
    """Strict disposition-document classifier — picks the documents the
    LLM document set should include. Rejects motions / briefs / status
    reports / notices-of-filing that mention disposition vocabulary in
    passing; accepts actual orders / judgments / minute-entries-of-
    decision whose head IS a disposition phrase.
    """
    text = (text or "").strip()
    if not text:
        return False
    if _DISPOSITION_NEGATIVE_RE.search(text):
        return False
    if _DISPOSITION_DOCUMENT_NEGATIVE_RE.search(text):
        return False
    if _DISPOSITION_RE.match(text):
        return True
    if not _DISPOSITION_DOCUMENT_HEAD_RE.match(text):
        return False
    return bool(_DISPOSITION_KEYWORD_RE.search(text))


# ---------------------------------------------------------------------------
# Generic helpers — entry-level classifier + per-recap_document scan
# ---------------------------------------------------------------------------
#
# Both helpers take a predicate and apply it consistently to the entry's
# head (description / short_description / first recap_doc description fallback)
# and/or each recap_document's own description. This is the abstraction that
# replaces what used to be three parallel hand-coded entry classifiers and
# two parallel per-attachment scans (one for primaries, one for dispositions).
#
# Detecting substance documents filed as attachments on procedural parents
# (the us-v-stryzhak Rule 20 transfer-with-attached-indictment shape, and
# the disposition analogue with plea agreements attached to "Notice of
# Filing" parents) requires checking BOTH the entry head AND each
# recap_document's own description; ``_entry_matches`` is the shared
# implementation, ``_recap_documents_matching`` is the per-document
# version the extractor uses to pick which recap_doc to read text from.


def _entry_matches(entry: dict[str, Any], predicate: Callable[[str], bool]) -> bool:
    """True if the entry's head OR any of its recap_documents'
    descriptions matches the given predicate."""
    if predicate(_entry_description_head(entry)):
        return True
    for rd in entry.get("recap_documents") or []:
        if predicate(rd.get("description") or ""):
            return True
    return False


def _recap_documents_matching(
    entry: dict[str, Any], predicate: Callable[[str], bool]
) -> list[dict[str, Any]]:
    """Return the recap_documents on ``entry`` whose own description
    matches the predicate. Used by the extractor when a procedural
    parent has the substance doc filed as an attachment."""
    return [
        rd
        for rd in (entry.get("recap_documents") or [])
        if predicate(rd.get("description") or "")
    ]


# ---------------------------------------------------------------------------
# Named classifiers — thin wrappers over the generic helpers
# ---------------------------------------------------------------------------


def is_primary_document(entry: dict[str, Any]) -> bool:
    return _entry_matches(entry, _matches_primary_document)


def is_disposition(entry: dict[str, Any]) -> bool:
    return _entry_matches(entry, _matches_disposition_broad)


# Stricter sibling used by ``find_primary_documents`` to pick the documents
# that the case-summary LLM actually reads. ``is_disposition`` (above) is
# intentionally broad — a "Motion for Preliminary Injunction" matches because
# the motion's outcome is a disposition signal worth refreshing the summary
# on. But that motion is NOT the order itself; feeding its 200-page brief to
# the LLM as "the disposition" drowns out the actual ruling. This predicate
# requires the entry to LOOK like an order / judgment / minute-entry — not a
# motion, brief, response, status report, or notice of filing about one.
_DISPOSITION_DOCUMENT_HEAD_RE = re.compile(
    r"""^\s*
    (?:\d+\s+)?
    # Optional adjective modifiers that legitimately precede the doc-type
    # word on actual orders (PAPERLESS ORDER, FINAL JUDGMENT, AMENDED ORDER,
    # PRELIMINARY INJUNCTION ORDER, STIPULATED INJUNCTION, etc.).
    (?:
        (?:PAPERLESS|TEXT[\s-]?ONLY|TEXT|AMENDED|FINAL|PRELIMINARY|
           STIPULATED|CONSENT|DEFAULT|CORRECTED|REDACTED|SEALED|UNSEALED|
           SUPERSEDING|INTERIM|TEMPORARY|PERMANENT)\s+
    )*
    (?:
        ORDER
        | JUDGMENT
        | JUDGEMENT
        | VERDICT
        | OPINION
        | MEMORANDUM\s+OPINION
        | MEMORANDUM\s+ORDER
        | MINUTE\s+ENTRY
        | MINUTE\s+ORDER
        | ELECTRONIC\s+CLERK['’]?S\s+NOTES
        | CLERK['’]?S\s+NOTES
        | PLEA\s+AGREEMENT
        | SETTLEMENT\s+AGREEMENT
        | DECREE
        | INJUNCTION
        | MANDATE
        | NOLLE\s+(?:PROSEQUI|PROSSED)
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Additional negative patterns for the narrow predicate. ``is_disposition``
# (broad) still matches these so the case_summaries row flips stale — a
# motion hearing held / new scheduling order / continued sentencing still
# changes "where the case stands". But these entries are SCHEDULING /
# PROCEDURAL records about future or referenced dispositions, not the
# disposition itself, so they must not reach the summary LLM as
# disposition documents — they have no holding to draw from, just
# references that look dispositive to the keyword regex.
_DISPOSITION_DOCUMENT_NEGATIVE_RE = re.compile(
    r"""\b(?:
        # Minute entries of MOTION HEARINGS (vs minute entries of
        # sentencings / verdicts, which ARE dispositions). Keys off the
        # "Motion Hearing re: …" / "Motion Hearing held on …" phrasing
        # that CourtListener clerks use for the procedural recording.
        motion\s+hearing\s+(?:re:?|held|on)
        # Case-schedule orders that reference upcoming dispositive motions
        # ("Motion for Summary Judgment due 6/10/2026") — these have
        # disposition vocabulary in passing but the order itself is
        # procedural.
        | order\s+(?:re(?:garding|\s+\d+)?|on)\s+(?:joint\s+)?stipulation
        # Orders SETTING a future hearing/trial/sentencing — scheduling,
        # not the disposition. ("ORDER SETTING SENTENCING HEARING …",
        # "ORDER SETTING CASE SCHEDULE …".) The hearing itself, when
        # held, will produce a separate minute-entry / judgment.
        | (?:order\s+|paperless\s+order\s+)?
          setting\s+(?:case|trial|sentencing|hearing|status|motion|briefing)
        # Continuances — the motion / order moves a date; it doesn't
        # decide anything.
        | motion\s+(?:to|for)\s+continu
        | continu(?:e|ing|ed|ance)\s+(?:sentencing|hearing|trial)
        # Notices of sentencing / hearing dates — also procedural.
        | notice\s+of\s+(?:sentencing|hearing)\s+date
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def _is_disposition_document(entry: dict[str, Any]) -> bool:
    return _entry_matches(entry, _matches_disposition_document)


# ---------------------------------------------------------------------------
# CourtListener helpers (lightweight wrappers around the existing client)
# ---------------------------------------------------------------------------


def _list_entries_ordered(
    cl: CourtListener,
    docket_id: int,
    *,
    order_by: str,
    page_size: int = 50,
    max_pages: int = 2,
) -> list[dict[str, Any]]:
    """Return up to ``page_size * max_pages`` entries on a docket in the given order.

    Uses CourtListener's native order_by so we don't have to scan the whole docket
    when we only want a head or tail slice. The first page alone covers
    primary documents on nearly every docket (entries 1-50 oldest-first)
    and dispositions on nearly every concluded case (entries 50 newest-first).
    """
    out: list[dict[str, Any]] = []
    url: Optional[str] = f"{API_BASE}/docket-entries/"
    params: Optional[dict[str, Any]] = {
        "docket": docket_id,
        "order_by": order_by,
        "page_size": page_size,
    }
    pages = 0
    while url and pages < max_pages:
        r = cl._get(url, params=params if pages == 0 else None)
        data = r.json()
        out.extend(data.get("results") or [])
        url = data.get("next")
        pages += 1
    return out


def _entry_looks_stale(entry: dict[str, Any]) -> bool:
    """True if ``entry``'s stored recap_documents look stale.

    The signature: a non-sealed, available MAIN recap_document whose
    ``plain_text`` is empty in the local copy. That's an entry whose row
    in the local store was written by a sync that predates the
    `plain_text`-as-stored-field feature (or any future change to the
    set of fields we keep locally with the same effect): structural
    fields are present, but the extracted text body the summary pipeline
    relies on is absent.

    The us-v-moucka regression that drove the original detector had
    CourtListener holding 39 KB of clean ``plain_text`` for the
    indictment recap_document while the local store's copy had
    ``plain_text`` empty. The entry-fingerprint check ignores
    ``plain_text`` *content* (only ``bool`` presence — which was the
    same then and now), so a regular sync couldn't naturally refresh
    the stored copy. The same shape can occur on disposition entries
    (judgments, plea agreements, verdicts), so the check is generic
    over entry type.

    Detection is intentionally conservative — sealed and unavailable
    recap_documents have legitimately empty ``plain_text`` and don't
    count as staleness signals. Attachments are also skipped
    (attachments often have empty ``plain_text`` on purpose; the
    summary cares about the main doc).
    """
    for rd in entry.get("recap_documents") or []:
        if rd.get("attachment_number"):
            continue
        if not rd.get("is_available"):
            continue
        if rd.get("is_sealed"):
            continue
        if not (rd.get("plain_text") or "").strip():
            return True
    return False


def _cached_entries_look_stale(entries: Iterable[dict[str, Any]]) -> bool:
    """True if any entry in the iterable looks stale.

    Applies to both primary documents (indictments, complaints) and
    disposition documents (judgments, plea agreements, verdicts) — the
    failure shape is the same regardless of how the entry is
    classified.
    """
    return any(_entry_looks_stale(e) for e in entries)


def _primary_failure_state(entry: dict[str, Any]) -> str:
    """Classify why a primary entry's text didn't reach the LLM.

    Returns one of three labels used by ``summarize_docket`` to pick
    the correct subscriber-facing fallback message (and to log the
    operator-facing per-doc breakdown). Inspection is bounded to the
    main recap_document (attachments are skipped — primary documents'
    body lives on the main doc):

      ``"sealed"``        — main recap_doc has ``is_sealed=True``. The
        document exists but PACER doesn't expose it. A fetch would 401/
        403; it can be unsealed later, at which point the fingerprint
        flips and the next sync picks it up automatically.
      ``"not-available"`` — main recap_doc has neither ``filepath_ia``
        nor ``filepath_local`` AND empty ``plain_text``. There was
        literally nothing for the extraction chain to fetch — the PDF
        hasn't been uploaded to RECAP yet (or our local cache is stale
        in a way the fingerprint missed).
      ``"unreadable"``    — anything else. The chain had something to
        work with (a URL or upstream plain_text) but couldn't produce
        usable text. Typically: image-only scan where OCR tools aren't
        installed, fetch ran but all URLs returned 4xx/5xx, or
        upstream pypdf produced font-encoding gibberish that our local
        pypdf can't decode either.

    The catch-all ``"unreadable"`` is intentionally the least specific
    — any state we don't recognize as cleanly sealed or no-source maps
    here, since "could not be read" is the most general framing that
    stays accurate.
    """
    for rd in entry.get("recap_documents") or []:
        if rd.get("attachment_number"):
            continue
        if rd.get("is_sealed"):
            return "sealed"
        has_url = bool(rd.get("filepath_ia") or rd.get("filepath_local"))
        has_plain = bool((rd.get("plain_text") or "").strip())
        if not has_url and not has_plain:
            return "not-available"
        return "unreadable"
    # No main recap_document at all (paperless entry tagged as primary
    # by description text alone — rare, since paperless minute orders
    # don't match is_primary_document head-anchored patterns, but
    # possible for short_description-only entries). Treat as
    # not-available — there's nothing to fetch.
    return "not-available"


def find_primary_documents(
    cl: CourtListener,
    docket_id: int,
    *,
    store: Optional[Store] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Find the primary document(s) and disposition documents on a docket.

    Returns ``(primary_documents, dispositions)``. Both lists are sorted
    chronologically (oldest first); callers pick the latest primary document
    for the LLM (superseding indictment beats original) and pass all
    dispositions through (each adds context).

    When ``store`` is provided AND the docket has previously been synced
    (i.e. it has body-bearing entries cached), this reads from the local
    `entries` table instead of re-fetching the same docket-entries pages
    from CourtListener — `sync.process_entry` now persists description + plain_text
    for primary/disp matches alongside hearing-relevant ones precisely
    so this short-circuit lands. Falls back to CourtListener for cold dockets
    (first ever sync on this docket or pre-fix data lacking
    primary/disp body text), AND when any cached entry — primary or
    disposition — looks stale (see `_cached_entries_look_stale`). In
    that case we also rewrite the local store's cached recap_documents
    with the fresh CourtListener data so the cache is rebuilt from
    fresh data and subsequent calls short-circuit normally. The
    staleness sweep covers both classifications because the same
    empty-plain_text shape can hit indictments and judgments alike.
    """
    primary: list[dict[str, Any]] = []
    dispositions: list[dict[str, Any]] = []
    cache_was_stale = False

    if store is not None:
        cached = store.get_entries_with_body(docket_id)
        for entry in cached:
            if is_primary_document(entry):
                primary.append(entry)
            elif _is_disposition_document(entry):
                dispositions.append(entry)
        # Short-circuit only when the cache yielded a primary document
        # AND no cached entry (primary OR disposition) looks stale. A
        # primary-document hit is the strong signal that this docket
        # has been (re)synced under the post-fix code path that
        # persists primary/disp bodies — at which point we trust the
        # rest of the cache too. If only dispositions came back, the
        # cache may be holding post-fix judgment bodies alongside a
        # pre-fix indictment stub (NULL description) — exactly the
        # us-v-chapman / us-v-mcgonigal shape — and the summary
        # pipeline needs CourtListener to recover the primary document
        # text. If any cached entry looks stale (empty plain_text on an
        # available main doc — the us-v-moucka / us-v-schmitz shape),
        # we ALSO fall through to CourtListener and rewrite the local
        # copy below so the cache is rebuilt from fresh data. The
        # staleness check covers both classifications: the same empty-
        # plain_text shape that hits indictments can hit judgments too.
        cached_stale = _cached_entries_look_stale(primary + dispositions)
        if primary and not cached_stale:
            primary.sort(key=lambda e: e.get("date_filed") or "")
            dispositions.sort(key=lambda e: e.get("date_filed") or "")
            return primary, dispositions
        if cached_stale:
            cache_was_stale = True
            log.info(
                "summary: docket %s — cached recap_documents look stale "
                "(available main doc has empty plain_text on at least one "
                "primary or disposition entry); falling through to "
                "CourtListener and refreshing the local cache",
                docket_id,
            )
            # Drop ONLY the stale cached entries — keep the fresh ones
            # so we don't re-fetch their text from CourtListener
            # unnecessarily. The CourtListener walk below will surface the missing
            # bodies and ``refresh_entry_recap_documents`` rewrites the
            # local copy from fresh data.
            primary = [e for e in primary if not _entry_looks_stale(e)]
            dispositions = [e for e in dispositions if not _entry_looks_stale(e)]

    # Cold cache OR cached primary looked stale — fall back to CourtListener.
    # Prime documents are almost always at entry #1 (criminal) or within
    # the first few amended-complaint entries (civil). Two pages
    # oldest-first covers 99% of cases. For dispositions we go three
    # pages newest-first: in a heavily-briefed civil case, the dispositive
    # order (e.g. an order granting a preliminary injunction) can be
    # followed by months of compliance briefing, status reports, and
    # transcript orders that push it well past the first newest-first page.
    # The extra two requests are cheap; the alternative (missing the
    # disposition) makes the summary state the wrong posture.
    oldest = _list_entries_ordered(cl, docket_id, order_by="date_filed", max_pages=2)
    newest = _list_entries_ordered(cl, docket_id, order_by="-date_filed", max_pages=3)

    # Seed the dedup set with anything the local cache already supplied
    # (the disposition-only fallthrough case): we WANT to keep those
    # cached entries — their plain_text was harvested at sync time, so
    # pdf.extract_text short-circuits on them — and just augment with
    # whatever else CourtListener turns up that the cache didn't see (typically the
    # missing primary document, sitting as a NULL stub locally).
    seen_ids: set[int] = {
        e["id"] for e in primary + dispositions if e.get("id") is not None
    }

    def _dedup_add(target: list[dict[str, Any]], entry: dict[str, Any]) -> None:
        eid = entry.get("id")
        if eid is None or eid in seen_ids:
            return
        seen_ids.add(eid)
        target.append(entry)

    for entry in oldest + newest:
        if is_primary_document(entry):
            _dedup_add(primary, entry)
            # Rebuild the local cache from fresh data: when we detected
            # stale cached data above and have just pulled fresh
            # CourtListener data for a primary entry, rewrite the
            # stored recap_documents so the next summary call short-
            # circuits normally. The fingerprint is intentionally not
            # touched — see `Store.refresh_entry_recap_documents`.
            if cache_was_stale and store is not None:
                store.refresh_entry_recap_documents(entry, docket_id=docket_id)
        elif _is_disposition_document(entry):
            _dedup_add(dispositions, entry)
            # Same cache-rebuild logic for dispositions: a stale
            # judgment row gets the same treatment as a stale
            # indictment so the next call short-circuits on the
            # populated copy.
            if cache_was_stale and store is not None:
                store.refresh_entry_recap_documents(entry, docket_id=docket_id)

    if cache_was_stale and store is not None:
        # Commit the cache refreshes — `refresh_entry_recap_documents`
        # doesn't commit per-call, matching the rest of the Store
        # methods that share `mark_entry`'s commit-via-caller pattern.
        store.conn.commit()
        # Log the outcome so the staleness-detection trail is closed
        # out — the earlier "falling through" line told operators we
        # were ABOUT to refresh; this line tells them whether anything
        # came back and got refreshed. A zero-primary / zero-disposition
        # outcome here means the CourtListener walk didn't find what
        # the cache was missing either (rare — usually a CL-side data
        # gap rather than our cache being wrong), and the next sync
        # will try again.
        log.info(
            "summary: docket %s — CourtListener fallback complete: "
            "%d primary, %d disposition documents in the final set "
            "(cache rewrite committed)",
            docket_id,
            len(primary),
            len(dispositions),
        )

    primary.sort(key=lambda e: e.get("date_filed") or "")
    dispositions.sort(key=lambda e: e.get("date_filed") or "")
    return primary, dispositions


def _logical_entry_dedup_key(entry: dict[str, Any]) -> tuple:
    """Stable key for the same PACER entry across CourtListener docket siblings.

    CourtListener assigns its own ``id`` per docket_id, so the same logical
    PACER entry on two CourtListener dockets has different ``id`` values. Within a
    ``(docket_number, court_id)`` group the PACER ``entry_number`` is the
    same, so it's the natural dedup key. For paperless minute orders that
    have no entry_number, falls back to ``(date_filed, description prefix)``
    — two CourtListener dockets in the same group should agree on those fields for
    the same paperless event.
    """
    enum = entry.get("entry_number")
    if enum is not None:
        return ("num", int(enum))
    date = entry.get("date_filed") or ""
    desc = (entry.get("description") or "").strip()[:200]
    return ("desc", date, desc)


def find_primary_documents_for_group(
    cl: CourtListener,
    group_docket_ids: list[int],
    *,
    store: Optional[Store] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pool primary documents + dispositions across every CourtListener docket in the group.

    CourtListener can split one logical PACER docket into multiple
    docket_id rows when the upstream ``pacer_case_id`` changed mid-life;
    each CourtListener row carries a partial slice of the PACER entries. This helper
    calls :func:`find_primary_documents` per CourtListener docket_id and unions the
    results, deduping with :func:`_logical_entry_dedup_key` so the same
    PACER entry (different CourtListener ``id``, same PACER ``entry_number``) is only
    returned once.

    ``group_docket_ids`` should be ordered freshest-first (which
    :meth:`Store.get_docket_group_ids` already does via
    ``date_modified DESC``) so first-seen-wins prefers the most-recently-
    ingested CourtListener row when a logical entry appears on multiple siblings.

    Exception: if a later sibling's copy of an already-seen entry carries
    populated ``plain_text`` on its main recap_document and the
    first-seen copy doesn't, the later copy wins. CourtListener can
    leave one CourtListener row's recap_document with an empty ``plain_text``
    while another CourtListener row in the same logical PACER group has the
    extracted body — without this upgrade the summary LLM would be
    fed an empty document. The us-v-schmitz indictment landed on the
    freshest CourtListener row with no ``plain_text`` while the older sibling had
    20 KB of text; the dedup needs to pick the populated copy. This
    doesn't add PDF reads (we choose between copies already in hand)
    and still keeps the rule that each logical PACER entry is fed to
    the LLM exactly once.
    """
    primary_by_key: dict[tuple, dict[str, Any]] = {}
    dispositions_by_key: dict[tuple, dict[str, Any]] = {}

    def _take(target: dict[tuple, dict[str, Any]], entry: dict[str, Any]) -> None:
        key = _logical_entry_dedup_key(entry)
        prior = target.get(key)
        if prior is None:
            target[key] = entry
            return
        if not _entry_main_doc_has_plain_text(prior) and _entry_main_doc_has_plain_text(
            entry
        ):
            target[key] = entry

    for did in group_docket_ids:
        p, d = find_primary_documents(cl, did, store=store)
        for e in p:
            _take(primary_by_key, e)
        for e in d:
            _take(dispositions_by_key, e)

    primary = sorted(primary_by_key.values(), key=lambda e: e.get("date_filed") or "")
    dispositions = sorted(
        dispositions_by_key.values(), key=lambda e: e.get("date_filed") or ""
    )
    return primary, dispositions


def _entry_main_doc_has_plain_text(entry: dict[str, Any]) -> bool:
    """True if ``entry`` carries non-empty ``plain_text`` on at least one
    non-attachment recap_document.

    Used by :func:`find_primary_documents_for_group` to choose between
    two CourtListener siblings' copies of the same logical PACER entry: a populated
    main document beats an empty one regardless of which sibling's
    ``date_modified`` is fresher.
    """
    for rd in entry.get("recap_documents") or []:
        if rd.get("attachment_number"):
            continue
        if (rd.get("plain_text") or "").strip():
            return True
    return False


# ---------------------------------------------------------------------------
# Sealing detection
# ---------------------------------------------------------------------------

# An "order granting [a motion/application] to seal" entry, on the docket's
# own description line. PACER vocabulary varies on the verb ("granting",
# "grants", "granted") and on whether it's a motion or an application, but
# the shape is always ORDER + grant-tense + ... + to seal.
_SEAL_ORDER_RE = re.compile(
    r"\border\b[^.]*\bgrant(?:s|ed|ing)\b[^.]*\bto\s+seal\b",
    re.IGNORECASE,
)
# The matching unsealing-order pattern — its presence after the sealing
# order is the canonical "seal has been lifted" signal.
_UNSEAL_ORDER_RE = re.compile(
    r"\border\b[^.]*\bgrant(?:s|ed|ing)\b[^.]*\bto\s+unseal\b",
    re.IGNORECASE,
)


def detect_sealing(
    cl: CourtListener,
    docket_id: int,
    *,
    dispositions: list[dict[str, Any]],
    available_post_seal_threshold: int = 3,
) -> Optional[dict[str, Any]]:
    """Detect whether this docket carries a granted sealing order with no
    contradicting public signals — i.e., is currently sealed-in-effect.

    Returns a sealing-advisory dict the summary pipeline forwards to the LLM,
    or ``None`` when there's no clear sealing signal (the common case for
    routine criminal cases where the indictment was sealed at filing and
    then unsealed at arrest — those have either an explicit unsealing order
    or substantial post-sealing public activity, both of which suppress
    the advisory).

    Heuristic:
      1. Find the LATEST entry whose description matches "ORDER granting
         ... to seal" on this docket.
      2. If a subsequent "ORDER granting ... to unseal" exists, the seal
         has been lifted — return None.
      3. If any disposition document exists post-sealing, the case is
         publicly disposed (an unavailable disposition wouldn't have been
         classified as one by ``find_primary_documents``) — return None.
      4. Count post-sealing entries with at least one publicly-available
         recap_document. If that count exceeds the threshold (default 3),
         the seal has been effectively lifted even without an explicit
         order — return None.
      5. Otherwise, surface the sealing advisory.

    The walk is small (1 page oldest-first + 1 page newest-first, 100
    entries total) — sealing orders are filed near the top of the docket
    and unsealing orders show up reliably in newest-first when present.
    """
    # Step 3 (cheap, no API call): disposition presence by itself is
    # decisive. A disposition document being publicly available means the
    # dispositive ruling landed in the open, which is incompatible with
    # the docket being currently sealed-in-effect.
    if dispositions:
        return None

    oldest = _list_entries_ordered(cl, docket_id, order_by="date_filed", max_pages=1)
    newest = _list_entries_ordered(cl, docket_id, order_by="-date_filed", max_pages=1)
    # Dedup on id (the two walks overlap on small dockets).
    seen: set[int] = set()
    entries: list[dict[str, Any]] = []
    for e in oldest + newest:
        eid = e.get("id")
        if eid is None or eid in seen:
            continue
        seen.add(eid)
        entries.append(e)

    # Step 1: find the LATEST granted sealing order (by date_filed). A
    # narrow seal followed by a broad seal both match, but the broad one
    # — typically later — is the operative one for our advisory.
    sealing_order: Optional[dict[str, Any]] = None
    for e in entries:
        desc = e.get("description") or ""
        if not _SEAL_ORDER_RE.search(desc):
            continue
        if sealing_order is None:
            sealing_order = e
            continue
        current = sealing_order.get("date_filed") or ""
        candidate = e.get("date_filed") or ""
        if candidate > current:
            sealing_order = e
    if sealing_order is None:
        return None

    sealing_date = sealing_order.get("date_filed") or ""

    # Step 2: subsequent unsealing order kills the signal.
    for e in entries:
        if (e.get("date_filed") or "") <= sealing_date:
            continue
        if _UNSEAL_ORDER_RE.search(e.get("description") or ""):
            return None

    # Step 4: substantial post-sealing public availability also kills the
    # signal (the seal may exist on paper but be functionally lifted).
    available_post_seal = 0
    for e in entries:
        if (e.get("date_filed") or "") <= sealing_date:
            continue
        for rd in e.get("recap_documents") or []:
            if rd.get("is_available"):
                available_post_seal += 1
                break
        if available_post_seal > available_post_seal_threshold:
            return None

    # Truncate the description; PACER descriptions can run several
    # hundred chars (multi-defendant cases name every defendant). The LLM
    # only needs the order's identity, not the full caption list.
    raw_desc = sealing_order.get("description") or ""
    short_desc = raw_desc.strip()
    if len(short_desc) > 240:
        short_desc = short_desc[:237].rstrip() + "..."

    return {
        "sealing_entry_number": sealing_order.get("entry_number"),
        "sealing_date_filed": sealing_date,
        "sealing_description": short_desc,
        "available_post_seal_entries": available_post_seal,
    }


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Substance-document predicates fed to the extractor
# ---------------------------------------------------------------------------
#
# The document types that the summary LLM should read. Adding a new type
# (a category of motion, a specific class of order, etc.) is just defining
# one more `_matches_*` predicate above and adding it to this tuple; the
# extractor's priority logic doesn't need to change.
_SUBSTANCE_PREDICATES: tuple[Callable[[str], bool], ...] = (
    _matches_primary_document,
    _matches_disposition_document,
)


def _substance_recap_documents(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Recap_documents on ``entry`` that look like a substance document
    under ANY of ``_SUBSTANCE_PREDICATES``. Deduplicated by recap_doc
    id (in case a single doc matches multiple substance predicates).
    """
    seen_ids: set[int] = set()
    out: list[dict[str, Any]] = []
    for predicate in _SUBSTANCE_PREDICATES:
        for rd in _recap_documents_matching(entry, predicate):
            rid = rd.get("id")
            if rid is not None and rid in seen_ids:
                continue
            if rid is not None:
                seen_ids.add(rid)
            out.append(rd)
    return out


def _entry_doc_text(entry: dict[str, Any], *, allow_ocr: bool = True) -> str:
    """Concatenate text from the relevant recap_documents on an entry.

    Priority order — first non-empty wins (the rest aren't consulted):

      1. **Substance-marked recap_documents** — those whose own
         description matches any of the ``_SUBSTANCE_PREDICATES``
         (primary documents and strict-disposition documents today;
         the tuple is the single source of truth for which document
         types we'll prioritize as attachments). When the actual
         charging document or ruling is filed as an attachment to a
         procedural parent (Rule 20 transfer-in with attached
         indictment, Notice of Filing with attached plea agreement,
         order parent with attached memorandum opinion), the parent's
         main doc is just procedural boilerplate; the substance is on
         the attachment. Pulling text from the substance-marked
         attachment keeps the summary LLM's input focused on the
         actual document rather than the wrapper.
      2. **Main recap_document(s)** (``attachment_number`` falsy). The
         common case — the indictment / complaint / judgment IS the
         entry's main filing.
      3. **All attachments**. Last-resort fallback when neither the
         substance-marked docs nor the main doc produced text — covers
         entries whose main doc isn't on RECAP yet but where some
         exhibit happens to be.

    Exhibits not matching any substance pattern at step 1 add noise
    but are truncated downstream by the LLM's character budget anyway,
    so the fallback at step 3 isn't aggressive about filtering.
    """
    rds = entry.get("recap_documents") or []

    # 1. Substance-marked recap_documents (any predicate).
    parts: list[str] = []
    for rd in _substance_recap_documents(entry):
        text = pdf.extract_text(rd, allow_ocr=allow_ocr)
        if text:
            parts.append(text)
    if parts:
        return "\n\n".join(parts)

    # 2. Main recap_document(s).
    for rd in rds:
        if rd.get("attachment_number"):
            continue
        text = pdf.extract_text(rd, allow_ocr=allow_ocr)
        if text:
            parts.append(text)
    if parts:
        return "\n\n".join(parts)

    # 3. Last-resort: any remaining attachment.
    for rd in rds:
        if not rd.get("attachment_number"):
            continue
        text = pdf.extract_text(rd, allow_ocr=allow_ocr)
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _entry_document_url(entry: dict[str, Any]) -> Optional[str]:
    """Public URL of the document a summary citation should open for ``entry``.

    Mirrors :func:`_entry_doc_text`'s priority so the link points at the
    SAME filing whose text the LLM read: a substance-marked recap_document
    first (the indictment / judgment filed as an attachment to a procedural
    parent), then the entry's main document, then any other attachment.
    Sealed documents are skipped — they have no openable URL — and the
    first recap_document that yields a URL wins. Returns None when nothing
    on the entry is reachable (paperless minute orders, not-yet-uploaded
    PDFs), in which case the caller leaves the cited phrase unlinked.
    """
    candidates = list(_substance_recap_documents(entry))
    rds = entry.get("recap_documents") or []
    candidates += [rd for rd in rds if not rd.get("attachment_number")]
    candidates += [rd for rd in rds if rd.get("attachment_number")]
    for rd in candidates:
        if rd.get("is_sealed"):
            continue
        url = pdf.recap_document_url(rd)
        if url:
            return url
    return None


def _attach_text(
    entries: Iterable[dict[str, Any]],
    *,
    allow_ocr: bool = True,
    allow_description_fallback: bool = False,
) -> list[dict[str, Any]]:
    """Pull document text for each entry; drop entries with no extractable text.

    When ``allow_description_fallback`` is true, entries with no PDF text fall
    back to the entry description. Used for dispositions, where paperless
    "Electronic Clerk's Notes" can record the full sentence imposed inline in
    the docket text without ever attaching a separate judgment PDF.
    """
    out: list[dict[str, Any]] = []
    for entry in entries:
        text = _entry_doc_text(entry, allow_ocr=allow_ocr)
        if not text and allow_description_fallback:
            text = _entry_description_head(entry)
        if not text:
            # Drop the entry from the document set. The PER-recap_document
            # reason (sealed / not available / fetched-but-pipeline-failed)
            # is logged by ``pdf.extract_text`` at INFO or WARNING and
            # carries the recap_doc id; this entry-level line is just the
            # summary outcome ("we couldn't get text for THIS entry from
            # any source") so the operator can correlate the per-doc
            # diagnostic with the per-entry drop. Stay neutral on the
            # cause — saying "no PDF text extractable" here was wrong
            # when the actual reason was "we never fetched because the
            # cached flag said sealed / not available", which is what
            # the us-v-lytvynenko diagnostic trail kept blurring.
            log.info(
                "summary: dropping entry %s (%s) from document set — "
                "no usable text from any of its recap_documents (see "
                "pdf.extract_text logs above for the per-recap_document "
                "cause)",
                entry.get("id"),
                _entry_description_head(entry)[:80],
            )
            continue
        out.append(
            {
                "entry_id": entry.get("id"),
                "entry_number": entry.get("entry_number"),
                "description": _entry_description_head(entry),
                "date_filed": entry.get("date_filed"),
                "text": text,
                # Public URL for the inline-citation link (None when the
                # document isn't reachable — paperless minute orders, a
                # description-fallback disposition with no PDF, a sealed
                # doc). A doc that reaches the summary LLM with no URL can
                # still be summarized; the cited phrase just renders unlinked.
                "url": _entry_document_url(entry),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Structured-events scaffold
# ---------------------------------------------------------------------------


def _hearings_for_group(
    store: Store, case_id: str, group_docket_ids: Iterable[int]
) -> list[dict[str, Any]]:
    """Filter the case's hearings to every docket_id in the group."""
    group_set = set(group_docket_ids)
    return [h for h in store.get_hearings(case_id) if h.get("docket_id") in group_set]


def _deadlines_for_group(
    store: Store, case_id: str, group_docket_ids: Iterable[int]
) -> list[dict[str, Any]]:
    group_set = set(group_docket_ids)
    return [d for d in store.get_deadlines(case_id) if d.get("docket_id") in group_set]


_WS_RE = re.compile(r"\s+")


def _text_fingerprint(text: Optional[str]) -> Optional[str]:
    """Stable fingerprint of an extracted document body for dedup.

    Normalizes to lowercase and collapses runs of whitespace before
    hashing, so minor extraction-pipeline variation (one path returning
    CourtListener's ``plain_text``, another running pypdf locally on
    the same bytes) doesn't break equality. Returns ``None`` for empty /
    trivially-short input so callers can skip dedup on it.

    Hash is sha256 of the full normalized text — full-body match is
    what we want here. The cost is negligible (sha256 on tens of KB) and
    it's the most precise signal that an ``extra_documents`` entry is
    serving the same content as something the CourtListener walk
    already surfaced.
    """
    if not text:
        return None
    normalized = _WS_RE.sub(" ", text.strip().lower())
    if len(normalized) < 100:
        # Too short to fingerprint meaningfully; many short PDFs share
        # the same boilerplate. The summary LLM's extra_documents budget
        # is large enough that any sub-100-char body is noise anyway.
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _filter_extras_already_in_cl(
    extras: list[dict[str, Any]],
    cl_documents: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop ``extra_documents`` whose extracted text already appears in
    the CourtListener-walk results for this docket.

    Workaround for the closed-without-fix CourtListener bug #7345 (and
    similar): an ``extra_documents`` URL the operator added to work
    around a CourtListener data gap can later become naturally
    findable through CourtListener — either because someone re-uploaded
    the same PDF to PACER under the new ``pacer_case_id`` that
    CourtListener's reconciler recognizes, or because the upstream
    gap closes on its own. When that happens the SAME document text
    reaches the summary LLM twice: once as a primary/disposition doc,
    once as an extras-block doc. That wastes tokens and gives that
    document outsized influence on the summary.

    Dedup is on a normalized-text fingerprint (see
    :func:`_text_fingerprint`) — robust to URL differences and re-upload
    paths because we compare what the LLM would actually read, not
    where the bytes came from. The drop is logged loudly (one WARN per
    dropped extra) so the operator notices and can remove the now-
    redundant entry from ``config.yaml``.
    """
    cl_fingerprints: set[str] = set()
    for doc in cl_documents:
        fp = _text_fingerprint(doc.get("text"))
        if fp:
            cl_fingerprints.add(fp)
    if not cl_fingerprints:
        return extras
    out: list[dict[str, Any]] = []
    for extra in extras:
        fp = _text_fingerprint(extra.get("text"))
        if fp and fp in cl_fingerprints:
            log.warning(
                "summary: dropping extra_documents entry %s — its extracted "
                "text matches a CourtListener-surfaced document on the same "
                "docket (the upstream gap that justified this entry has "
                "likely closed; consider removing it from config.yaml)",
                extra.get("source_url"),
            )
            continue
        out.append(extra)
    return out


def _fetch_extra_documents(
    case: CaseConfig,
    group_docket_ids: Iterable[int],
    *,
    allow_ocr: bool = True,
) -> list[dict[str, Any]]:
    """Fetch operator-provided ``extra_documents`` for every docket in the group.

    Returns one flat list of doc dicts, each carrying ``source_url`` (the
    LLM prompt's provenance line) and ``operator_note`` (the trusted
    operator-supplied description that tells the LLM what the document is
    and why it was added). Documents that fail to fetch / extract are
    logged and dropped so the rest of the summary pipeline still gets to
    run on whatever CourtListener did surface. The summary LLM sees these in their
    own "EXTRA DOCUMENTS PROVIDED BY OPERATOR" section, distinct from the
    primary-document and disposition slots that the CourtListener-walk fills.

    Pins the operator-supplied ``extra.docket`` against any CourtListener docket_id
    in the group — when a logical PACER docket is split across multiple
    CourtListener siblings, an extra pinned to one of them applies to the group.
    """
    extras = getattr(case, "extra_documents", None) or []
    group_set = set(group_docket_ids)
    out: list[dict[str, Any]] = []
    for extra in extras:
        if extra.docket not in group_set:
            continue
        log.info(
            "summary: docket %s — fetching operator-provided document from %s",
            extra.docket,
            extra.url,
        )
        text = pdf.extract_text_from_url(extra.url, allow_ocr=allow_ocr)
        if not text:
            # The per-URL cause (fetch failed vs. fetched-but-pipeline-
            # failed) is logged by `pdf.extract_text_from_url` and (for
            # the fetch-failed case) `pdf.fetch_url_bytes`. This log line
            # is just the operator-level outcome ("the operator-provided
            # document didn't make it into the document set") so the
            # config-side trail is visible alongside the lower-level
            # cause.
            log.warning(
                "summary: docket %s — operator-provided document %s "
                "produced no usable text and was dropped from the LLM "
                "input (see pdf.extract_text_from_url log above for the "
                "fetch vs. extraction cause)",
                extra.docket,
                extra.url,
            )
            continue
        out.append(
            {
                "entry_id": None,
                "entry_number": None,
                "description": "operator-provided document",
                "date_filed": None,
                "text": text,
                "source_url": extra.url,
                "operator_note": extra.note,
            }
        )
    return out


def _borrow_primary_from_siblings(
    *,
    cl: CourtListener,
    store: Store,
    case: CaseConfig,
    primary_docket_id: int,
    allow_ocr: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pull primary AND disposition document text from a sibling docket.

    Stops at the first sibling that yields extractable primary text and
    returns that sibling's primary documents together with its disposition
    documents (judgment, plea agreement, verdict, dismissal). An appellate
    criminal docket opens with a clerical "case opened / notice of appeal"
    entry and re-files NEITHER the charging document NOR the trial-court
    judgment — so without borrowing both, the appellate summary can only say
    the defendant was "charged", never that he was convicted and sentenced,
    which is the actual subject of the appeal.

    Both lists' entry descriptions are tagged with the sibling's docket
    number and court so the LLM prompt makes the cross-docket borrowing
    explicit — without that label the model writes the summary as if the
    complaint had been filed in the appellate court, or attributes the
    district-court sentence to the court of appeals.

    Returns ``([], [])`` when no sibling yields primary text.
    """
    for sibling_id in case.dockets:
        if sibling_id == primary_docket_id:
            continue
        meta = store.get_docket_meta(sibling_id) or {}
        sibling_docket_number = meta.get("docket_number")
        sibling_court_citation = (
            store.get_court_citation(meta["court_id"]) if meta.get("court_id") else None
        )
        log.info(
            "summary: docket %s has no primary document — borrowing from sibling %s (%s)",
            primary_docket_id,
            sibling_id,
            sibling_docket_number,
        )
        try:
            sibling_primary, sibling_dispositions = find_primary_documents(
                cl, sibling_id, store=store
            )
        except Exception:
            log.exception(
                "summary: failed to scan sibling docket %s for primary documents",
                sibling_id,
            )
            continue
        borrowed_primary = _attach_text(sibling_primary, allow_ocr=allow_ocr)
        if not borrowed_primary:
            continue
        # The disposition is the conviction/sentence on appeal — attach it
        # the same way summarize_docket does its own dispositions (the
        # description fallback covers paperless minute-entry judgments).
        borrowed_dispositions = _attach_text(
            sibling_dispositions,
            allow_ocr=allow_ocr,
            allow_description_fallback=True,
        )
        label_bits = [b for b in (sibling_docket_number, sibling_court_citation) if b]
        label = " ".join(label_bits) or f"docket {sibling_id}"
        for doc in borrowed_primary + borrowed_dispositions:
            existing = doc.get("description") or "document"
            doc["description"] = f"{existing} [from sibling {label}]"
        return borrowed_primary, borrowed_dispositions
    return [], []


# ---------------------------------------------------------------------------
# Post-generation truthfulness guard
# ---------------------------------------------------------------------------
#
# The SUMMARY_SYSTEM_PROMPT rules against asserting facts from docket silence
# (absence-of-record claims, fugitive-by-inference) are SOFT — the model can
# ignore them, and for a brand-new case there is no prior good summary to fall
# back to, so a falsehood would reach subscribers. This is the HARD backstop:
# a deterministic scan of the generated text. ``summarize_docket`` runs it,
# retries generation once with the violation fed back, and keeps the cleaner
# attempt (logging a WARNING) if it still trips. See the AGENTS.md design
# decision "Post-generation truthfulness guard on case summaries."

# Tier 1 — absence-of-record claims. These characterize what is NOT in the
# record as a positive fact; no court document narrates the absence of itself,
# so they are ALWAYS unsupported (sealing / partial-RECAP can hide activity
# the model then wrongly reports as nonexistent). Matched by CONSTRUCTION
# (negation + a procedural-record noun), not by literal strings — the model
# reworded around literal patterns in the us-v-berezhnoy regression ("no
# disposition documents have been FILED" / "the docket does not REFLECT any
# scheduled hearings", neither of which the original entered-only pattern
# caught). Hedging with "in the available record" does NOT exempt these — for
# procedural posture the rule is silence. Custody-status "we don't know"
# phrasings ("cannot be determined", "status is unknown") and speculative
# conditional outcomes ("if convicted", "if a term of imprisonment is
# imposed") get their own patterns below — all the same omit-the-pointless-
# content rule. The procedural-noun list is scoped so "no restitution" / "no
# fewer than three counts" — legitimate documented facts — don't trip it.
_GUARD_PROC_NOUN = (
    r"(?:disposition|judgments?|hearings?|deadlines?|scheduling\s+orders?|"
    r"docket\s+entr(?:y|ies)|trial\s+dates?|recent\s+activity|"
    r"arrests?|initial\s+appearances?)"
)
_GUARD_ABSENCE_RES = [
    # "no <up to 3 words> <procedural noun>" — "no disposition documents",
    # "no scheduled hearings", "no public docket entries", "no recent
    # activity", "no new public scheduling order", "no apparent arrest".
    re.compile(rf"\bno\s+(?:\w+\s+){{0,3}}{_GUARD_PROC_NOUN}\b", re.I),
    # "(docket|record) does/do/did not reflect/show/indicate/contain/list ..."
    re.compile(
        r"\b(?:docket|record)\s+(?:does|do|did)\s+not\s+"
        r"(?:reflect|show|indicate|contain|list|record)\b",
        re.I,
    ),
    # "<procedural noun> ... (have|has) not been filed/entered/recorded/..."
    re.compile(
        rf"{_GUARD_PROC_NOUN}\s+(?:\w+\s+){{0,3}}(?:have|has)\s+not\s+been\s+"
        r"(?:filed|entered|recorded|scheduled|set|reflected)",
        re.I,
    ),
    # closing "the case remains pending" positive claim.
    re.compile(r"\bthe case remains pending\b", re.I),
    # Custody / arrest "we don't know" noise — stating what the record does
    # NOT establish about a defendant's custody is pointless; OMIT it, don't
    # announce "unknown" (us-v-jin / us-v-gholinejad).
    re.compile(r"\bcannot be determined from the\b", re.I),
    re.compile(r"\bit is unknown whether\b", re.I),
    re.compile(
        r"\b(?:custody|arrest|appearance)\s+status\b[^.]{0,40}?"
        r"\b(?:unknown|cannot be|not (?:known|clear|established))\b",
        re.I,
    ),
    # Speculative / conditional future outcomes — hypothetical consequences we
    # don't know and usually obvious boilerplate ("will be remanded to the BOP
    # if a term of imprisonment is imposed", "if convicted, X faces ..."). Keep
    # the scheduled event and its date; drop the conditional consequence
    # clause. (us-v-martino.)
    re.compile(r"\bif (?:convicted|found guilty)\b", re.I),
    re.compile(
        r"\bif a (?:term of )?(?:imprisonment|incarceration|sentence)\s+"
        r"(?:is|were|is to be)\s+(?:imposed|ordered)\b",
        re.I,
    ),
    re.compile(r"\bshould the court impose\b", re.I),
]

# Tier 2 — custody / flight status. Legitimate ONLY when a source document
# says so (the indictment, a press release the operator added via
# extra_documents, etc.), so these are grounded against the document corpus:
# flagged only when no custody-status keyword appears in the source text. The
# canonical regression is us-v-jin / us-v-berezhnoy, where the model wrote
# "remain at large" inferred from missing arrest entries (no document said it).
_GUARD_CUSTODY_RES = [
    re.compile(r"\bremains?\s+(?:a\s+)?fugitive\b", re.I),
    re.compile(r"\bis\s+a\s+fugitive\b", re.I),
    re.compile(
        r"\b(?:remain|remains|remaining|appears?\s+to\s+remain)\s+at\s+large\b", re.I
    ),
]
_GUARD_CUSTODY_GROUND_RE = re.compile(
    r"\b(?:fugitive|at large|apprehend|in custody|remains? abroad|not been arrested)\b",
    re.I,
)


def _audit_summary_text(text: str, *, source_text: str = "") -> list[str]:
    """Return guard violations for one generated summary; empty == clean.

    Tier-1 (absence-of-record) matches always count. Tier-2 (custody / flight)
    counts only when the claim is NOT grounded in ``source_text`` (the
    concatenated document corpus the LLM read), so a fugitive status that a
    real document actually states is allowed through. The canonical refusal
    sentence is exempt — it is an honest "not enough material," not an
    absence-of-record claim about the case.
    """
    if not text or text.strip() == llm.SUMMARY_INSUFFICIENT_DOCUMENTS.strip():
        return []
    violations: list[str] = []
    for rx in _GUARD_ABSENCE_RES:
        m = rx.search(text)
        if m:
            violations.append(f"absence-of-record claim: {m.group(0)!r}")
    grounded = bool(_GUARD_CUSTODY_GROUND_RE.search(source_text or ""))
    if not grounded:
        for rx in _GUARD_CUSTODY_RES:
            m = rx.search(text)
            if m:
                violations.append(f"unsupported custody/flight claim: {m.group(0)!r}")
    return violations


# Tier 3 — fabricated dates / dollar amounts. A hard fact in the prose (a
# "Month D, YYYY" date or a "$N" figure) that appears in NEITHER the
# structured-events scaffold NOR the source documents the LLM was given is a
# possible hallucination. Because summaries auto-generate as a service, a WARN
# alone can sail unread into a subscriber's calendar — so a hit ACTS: retry
# once asking the model to drop the unsupported figure, then deterministically
# strip any sentence STILL carrying one (see _generate_guarded_summary). The
# figure never publishes; the WARN is for the operator to review the removal.
# The matching stays deliberately liberal (many date formats; comma-insensitive
# amounts; "X million" approximations skipped) so it BIASES toward silence —
# under-flagging a real fabrication is preferred to crying wolf on a
# correctly-stated fact written in a different format, which here would cost a
# real sentence rather than only a log line. Formatting variance ("5/6/26" vs
# "May 6, 2026", "$16M" vs "16,000,000") is why the retry comes first: the
# model, re-shown the documents, can keep and reformat a figure it can actually
# support, and only a figure it can't gets stripped.
_GUARD_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        ],
        1,
    )
}
_GUARD_PROSE_DATE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
    re.I,
)
_GUARD_MONEY_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d{1,2})?(?:\s*(?:million|billion))?", re.I
)


def _grounding_dates(gen_kwargs: dict[str, Any]) -> set[str]:
    """ISO dates the summary may legitimately cite — the scaffold's hearing /
    deadline dates plus each provided document's filing date."""
    dates: set[str] = set()
    for h in gen_kwargs.get("hearings") or []:
        d = h.get("starts_at_utc")
        if d:
            dates.add(str(d)[:10])
    for dl in gen_kwargs.get("deadlines") or []:
        d = dl.get("due_at_utc")
        if d:
            dates.add(str(d)[:10])
    for key in ("primary_documents", "disposition_documents", "extra_documents"):
        for doc in gen_kwargs.get(key) or []:
            d = doc.get("date_filed")
            if d:
                dates.add(str(d)[:10])
    return dates


def _date_in_corpus(iso: str, raw: str, corpus: str) -> bool:
    # ``corpus`` is whitespace-normalized by the caller; normalize ``raw`` the
    # same way so a PDF line-break artifact ("June 26,\n\n2024") still matches
    # the summary's "June 26, 2024" (the us-v-stryzhak false positive).
    if re.sub(r"\s+", " ", raw).lower() in corpus.lower():
        return True
    y, m, d = iso.split("-")
    candidates = [
        f"{int(m)}/{int(d)}/{y}",
        f"{int(m)}/{int(d)}/{y[2:]}",
        f"{int(m):02d}/{int(d):02d}/{y}",
        iso,
    ]
    return any(c in corpus for c in candidates)


def _audit_summary_grounding(
    text: str, *, known_dates: set[str], source_text: str = ""
) -> list[str]:
    """Return grounding findings — dates / dollar amounts in the summary not
    traceable to the scaffold or the source documents. Detection only; the
    caller (:func:`_generate_guarded_summary`) decides what to do with a hit
    (retry, then strip the offending sentence).

    Biased toward silence: month-only ranges (no day) aren't matched at all,
    "X million" approximations are skipped, and amounts are matched
    comma-insensitively as substrings (so a real figure formatted differently
    still passes). The refusal sentence is exempt.
    """
    if not text or text.strip() == llm.SUMMARY_INSUFFICIENT_DOCUMENTS.strip():
        return []
    # Collapse whitespace runs (PDF extraction sprinkles newlines mid-phrase)
    # so a real date/amount split across lines still matches.
    corpus = re.sub(r"\s+", " ", source_text or "")
    corpus_nc = corpus.replace(",", "")
    out: list[str] = []
    for m in _GUARD_PROSE_DATE_RE.finditer(text):
        mon = _GUARD_MONTHS[m.group(1).lower()[:3]]
        iso = f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}"
        if iso in known_dates or _date_in_corpus(iso, m.group(0), corpus):
            continue
        out.append(f"ungrounded date: {m.group(0)!r}")
    for m in _GUARD_MONEY_RE.finditer(text):
        raw = m.group(0)
        if re.search(r"million|billion", raw, re.I):
            continue  # approximate figure — too unreliable to verify
        amt = raw.lstrip("$").strip().replace(",", "")
        if amt and amt in corpus_nc:
            continue
        out.append(f"ungrounded amount: {raw!r}")
    return out


# Sentence splitter for the grounding backstop. A naive ". "-split mangles
# legal prose: "U.S. Attorney", a middle initial ("John A. Smith"), "Mr.",
# "No.", "Inc.", and case captions ("United States v. Doe") all carry a period
# followed by a capital that is NOT a sentence boundary. So we split on
# sentence punctuation + whitespace + an opening capital / quote / link-bracket,
# then merge back any fragment whose tail is one of those known abbreviations.
# The backstop only runs when the model defied an explicit correction (rare),
# so "good enough" segmentation plus a WARN is the right cost — the retry
# already gave the model its shot at clean prose.
_SENT_SPLIT_RE = re.compile(r'(?<=[.!?])\s+(?=[A-Z"“‘\[])')
_NO_SPLIT_TAIL_RE = re.compile(
    r"(?:\b[A-Z]|U\.S|U\.S\.C|U\.S\.A|Mr|Mrs|Ms|Dr|Jr|Sr|St|No|Inc|Corp|Co|Ltd"
    r"|vs?|et al|e\.g|i\.e|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec"
    r"|Cir|Fed|art|sec)\.\s*$",
    re.I,
)


def _split_sentences(text: str) -> list[str]:
    """Split prose into sentences, keeping abbreviations intact.

    See :data:`_SENT_SPLIT_RE` / :data:`_NO_SPLIT_TAIL_RE` for the heuristic.
    """
    merged: list[str] = []
    for piece in _SENT_SPLIT_RE.split(text):
        if merged and _NO_SPLIT_TAIL_RE.search(merged[-1]):
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)
    return merged


def _strip_ungrounded_sentences(
    text: str, *, known_dates: set[str], source_text: str = ""
) -> str:
    """Remove whole sentences that still carry an ungrounded date / amount.

    The deterministic backstop behind the grounding retry: after the model has
    already had one correction retry to drop an unsupported figure, any
    sentence STILL carrying one is removed so the figure cannot reach
    subscribers. Per-sentence detection reuses :func:`_audit_summary_grounding`,
    so a sentence is dropped iff it would itself flag — the surviving sentences
    are exactly the grounded ones. If every sentence is stripped, the refusal
    sentence is returned (the honest "nothing we can stand behind" output).
    """
    kept = [
        s
        for s in _split_sentences(text)
        if not _audit_summary_grounding(
            s, known_dates=known_dates, source_text=source_text
        )
    ]
    result = " ".join(s.strip() for s in kept).strip()
    return result or llm.SUMMARY_INSUFFICIENT_DOCUMENTS


# Tier 4 — redundant forfeiture money judgment. When the prose states a
# forfeiture MONEY JUDGMENT whose dollar amount equals a restitution amount it
# also states, a lay reader sums the two into a fictional larger liability: the
# us-v-knoot case — "$15,100 in restitution" plus "a forfeiture money judgment
# of $15,100" reads as $30,200. The SUMMARY_SYSTEM_PROMPT omission rule is meant
# to drop the forfeiture in exactly this case, but it is soft and the model
# applies it inconsistently — and the appellate-summary borrow now feeds the
# trial-court judgment (which carries the forfeiture order) into appellate
# summaries, widening where the rule has to hold. This deterministic backstop
# detects the equal-amount pair and, after a correction retry, strips the
# forfeiture clause. Scoped to forfeiture MONEY JUDGMENTS only (the literal
# "forfeiture money judgment" wording) — identified-property forfeiture (a named
# house, vehicle, or cryptocurrency wallet) takes things, not a dollar sum, and
# stays. CAVEAT: detection is amount-equality only, so two CO-DEFENDANTS who
# each happen to draw the same-dollar restitution and forfeiture could trip it;
# that is rare, the retry gives the model a chance to keep them separate first,
# and a WARNING is always logged for operator review (matching the grounding
# guard's risk posture).
_FORF_AMOUNT_RE = r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)"
# The phrase may be wrapped in an inline-link marker ("[forfeiture money
# judgment](doc:D5)") because the guard runs on the token-bearing prose BEFORE
# link resolution; tolerate the optional brackets + (doc:..) target.
_FMJ_PHRASE = r"\[?forfeiture\s+money\s+judgment\]?(?:\(doc:[^)]*\))?"
_FORF_JUDGMENT_AMOUNT_RES = [
    re.compile(
        _FMJ_PHRASE + r"\s+(?:of|in\s+the\s+amount\s+of|for)\s+" + _FORF_AMOUNT_RE,
        re.I,
    ),
    re.compile(_FORF_AMOUNT_RE + r"\s+forfeiture\s+money\s+judgment", re.I),
]
_RESTITUTION_AMOUNT_RES = [
    re.compile(_FORF_AMOUNT_RE + r"\s+in\s+restitution", re.I),
    re.compile(
        r"restitution\s+(?:of|in\s+the\s+amount\s+of|totaling)?\s*" + _FORF_AMOUNT_RE,
        re.I,
    ),
]
# The forfeiture STATEMENT — the money-judgment phrase, its amount, and the
# usual "entered against <defendant>" tail — as one removable span.
_FORF_STATEMENT = (
    _FMJ_PHRASE
    + r"\s+(?:of|in\s+the\s+amount\s+of|for)\s+\$\s?\d[\d,]*(?:\.\d{1,2})?"
    + r"(?:\s+(?:was|is|were))?(?:\s+also)?(?:\s+entered)?"
    + r"(?:\s+against\s+(?:him|her|them|the\s+defendants?))?"
)
# Embedded / trailing clause: ", and a forfeiture money judgment ... against him".
_FORF_EMBEDDED_RE = re.compile(
    r"[,;]\s*(?:and\s+|with\s+)?(?:a\s+)?" + _FORF_STATEMENT, re.I
)
# Leading clause followed by ", and <rest>": "A forfeiture money judgment ...
# against him, and he is appealing" -> "He is appealing".
_FORF_LEADING_RE = re.compile(
    r"\b(?:A\s+)?" + _FORF_STATEMENT + r",?\s+and\s+(\w)", re.I
)


def _norm_money(raw: str) -> str:
    """Canonical form of a dollar-amount string for equality compares
    (commas dropped, trailing cents zeros trimmed): "15,100" / "15100.00" ->
    "15100"."""
    s = raw.replace(",", "").strip()
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _money_amounts(text: str, patterns: list[re.Pattern[str]]) -> set[str]:
    out: set[str] = set()
    for rx in patterns:
        for m in rx.finditer(text):
            out.add(_norm_money(m.group(1)))
    return out


def _audit_summary_forfeiture(text: str) -> list[str]:
    """Return a violation when the prose states a forfeiture money judgment
    whose amount equals a restitution amount it also states (empty == clean).

    Detection only — :func:`_strip_redundant_forfeiture` does the removal and
    the matching SUMMARY_SYSTEM_PROMPT omission rule explains the why.
    """
    if not text:
        return []
    restitution = _money_amounts(text, _RESTITUTION_AMOUNT_RES)
    if not restitution:
        return []
    forfeiture = _money_amounts(text, _FORF_JUDGMENT_AMOUNT_RES)
    dup = sorted(restitution & forfeiture)
    if dup:
        return [
            f"redundant forfeiture money judgment of ${dup[0]} duplicates the "
            "restitution amount"
        ]
    return []


def _strip_redundant_forfeiture(text: str) -> str:
    """Deterministically remove a redundant forfeiture-money-judgment clause.

    The backstop behind the forfeiture retry: after the model has had one
    correction retry and STILL states a forfeiture money judgment equal to the
    restitution, the forfeiture clause is excised so the doubled figure cannot
    reach subscribers. Removes the clause surgically when it is joined to other
    content (keeping that content — e.g. the trailing "is appealing" clause on
    an appellate summary), and drops a whole sentence only when the sentence is
    solely the forfeiture statement.
    """
    if not _audit_summary_forfeiture(text):
        return text
    redundant = _money_amounts(text, _RESTITUTION_AMOUNT_RES) & _money_amounts(
        text, _FORF_JUDGMENT_AMOUNT_RES
    )
    # Leading-clause shape first (keeps the trailing clause, capitalized).
    new = _FORF_LEADING_RE.sub(lambda m: m.group(1).upper(), text)
    # Then embedded / trailing clauses.
    new = _FORF_EMBEDDED_RE.sub("", new)

    # Any remaining standalone forfeiture-money-judgment sentence carrying a
    # redundant amount → drop the whole sentence (a per-sentence forfeiture
    # check, since such a sentence has no restitution amount of its own to
    # pair with).
    def _redundant_forf_sentence(s: str) -> bool:
        for rx in _FORF_JUDGMENT_AMOUNT_RES:
            if any(_norm_money(m.group(1)) in redundant for m in rx.finditer(s)):
                return True
        return False

    if any(_redundant_forf_sentence(s) for s in _split_sentences(new)):
        new = " ".join(
            s.strip() for s in _split_sentences(new) if not _redundant_forf_sentence(s)
        ).strip()

    # Tidy stranded punctuation / doubled spaces left by the excision.
    new = re.sub(r"\s+([,;.])", r"\1", new)
    new = re.sub(r"\s{2,}", " ", new).strip()
    return new or llm.SUMMARY_INSUFFICIENT_DOCUMENTS


# Tier 5 — stale scheduled-event date. The structured-events scaffold outranks
# document text for an event's CURRENT date (the matching CRITICAL rule in
# SUMMARY_SYSTEM_PROMPT): a continuance stated in a document may itself have
# been superseded by a later notice that is NOT in the document set, because a
# plain scheduling notice is neither a primary document nor a disposition. The
# us-v-ding case is the canonical instance: the 6/16/2026 minute entry
# ("Sentencing continued to 8/4/2026") rode into the disposition set, the
# 7/22/2026 clerk's notice that moved sentencing to 9/1/2026 did not, and the
# model wrote "Sentencing is scheduled for August 4, 2026" while the
# scaffold's Sentencing row said September 1 — so the published summary
# contradicted the calendar feed rendered from the same store. The grounding
# guard cannot catch this: the stale date IS in the corpus. This guard
# compares each "<event> is scheduled/set/continued for <date>" claim against
# the scaffold's scheduled rows for that event type and fires only when NO
# scheduled row of that type falls on the claimed date (court-local or UTC).
# It BIASES toward silence, matching the grounding guard's risk posture: no
# scheduled row of the claimed type -> silent (the claim may narrate history,
# or describe a deadline), historical phrasing ("was scheduled for",
# "previously set for") -> silent, a date matching ANY row of the type
# (co-defendants each with their own sentencing) -> silent. Enforcement is
# retry -> strip -> WARN, the same shape as the grounding guard.
_STALE_EVENT_CLAIM_RE = re.compile(
    r"\b(?P<event>sentencing|jury\s+trial|trial|arraignment|oral\s+argument"
    r"|change\s+of\s+plea)\b"
    r"(?:\s+hearing)?"
    r"(?P<gap>[^.!?;:]{0,60}?)"
    r"\b(?:(?:re)?scheduled|set|continued)\s+(?:for|to|until)\s+"
    r"(?:begin\s+)?(?:on\s+)?"
    r"(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<year>\d{4})\b",
    re.I,
)
# Words between the event and the scheduling verb that mark the claim as
# narrated HISTORY ("sentencing was scheduled for ...", "sentencing,
# originally set for ...") rather than current posture — those never flag.
_STALE_EVENT_HISTORICAL_RE = re.compile(
    r"\b(?:was|were|had\s+been|previously|originally|initially|earlier|then)\b",
    re.I,
)
# Prose event phrase -> the word a scaffold hearing TITLE must contain for the
# row to govern that claim. "jury trial" and "trial" both map to "trial" so
# either prose form matches a "Jury Trial" or "Trial" row.
_STALE_EVENT_TITLE_WORDS = {
    "sentencing": "sentencing",
    "jury trial": "trial",
    "trial": "trial",
    "arraignment": "arraignment",
    "oral argument": "argument",
    "change of plea": "plea",
}


def _hearing_start_dates(
    hearing: dict[str, Any],
) -> tuple[Optional[str], Optional[str]]:
    """(court-local ISO date, UTC ISO date) of the hearing's start.

    Either slot is ``None`` when it can't be derived: a missing or
    unparseable ``starts_at_utc`` yields ``(None, None)``; a missing or
    unknown ``timezone`` yields a ``None`` local date. A naive timestamp is
    treated as UTC, matching how the store writes ``starts_at_utc``. Both
    dates are returned because prose normally states the court-local date but
    the scaffold shows the model UTC timestamps — a claim matching either is
    considered in agreement.
    """
    raw = hearing.get("starts_at_utc")
    if not raw:
        return (None, None)
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return (None, None)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    utc_date = dt.astimezone(timezone.utc).date().isoformat()
    local_date: Optional[str] = None
    tz_name = hearing.get("timezone")
    if tz_name:
        try:
            local_date = dt.astimezone(ZoneInfo(str(tz_name))).date().isoformat()
        except (KeyError, ValueError):
            # Unknown / malformed timezone name — fall back to UTC-only.
            local_date = None
    return (local_date, utc_date)


def _audit_summary_stale_event_dates(
    text: str, *, hearings: Iterable[dict[str, Any]]
) -> list[str]:
    """Return findings for scheduled-event dates contradicting the scaffold
    (empty == clean). Detection only — see the tier comment on
    :data:`_STALE_EVENT_CLAIM_RE` for the matching rules and the bias. Each
    finding quotes the claim and the governing scaffold row(s) with their
    current dates, so the retry correction can feed the right date back.
    """
    if not text or text.strip() == llm.SUMMARY_INSUFFICIENT_DOCUMENTS.strip():
        return []
    scheduled: list[tuple[str, str, set[str]]] = []
    for h in hearings or []:
        if (h.get("status") or "").lower() != "scheduled":
            continue
        local_date, utc_date = _hearing_start_dates(h)
        if not utc_date:
            continue
        dates = {utc_date} | ({local_date} if local_date else set())
        scheduled.append((str(h.get("title") or ""), local_date or utc_date, dates))
    if not scheduled:
        return []
    out: list[str] = []
    for m in _STALE_EVENT_CLAIM_RE.finditer(text):
        if _STALE_EVENT_HISTORICAL_RE.search(m.group("gap")):
            continue
        event = re.sub(r"\s+", " ", m.group("event").lower())
        title_word = _STALE_EVENT_TITLE_WORDS[event]
        matching = [row for row in scheduled if title_word in row[0].lower()]
        if not matching:
            continue
        mon = _GUARD_MONTHS[m.group("mon").lower()[:3]]
        claimed = f"{int(m.group('year')):04d}-{mon:02d}-{int(m.group('day')):02d}"
        if any(claimed in dates for _title, _display, dates in matching):
            continue
        current = "; ".join(
            f"the scaffold's current {title!r} row (status=scheduled) is on {display}"
            for title, display, _dates in matching
        )
        out.append(f"stale scheduled-event date: {m.group(0)!r} — {current}")
    return out


def _strip_stale_event_date_sentences(
    text: str, *, hearings: Iterable[dict[str, Any]]
) -> str:
    """Remove whole sentences still carrying a stale scheduled-event date.

    The deterministic backstop behind the stale-date retry, mirroring
    :func:`_strip_ungrounded_sentences`: per-sentence detection reuses
    :func:`_audit_summary_stale_event_dates`, so a sentence is dropped iff it
    would itself flag — the calendar feed carries the event's current date, so
    losing the sentence never loses the schedule. If every sentence is
    stripped, the refusal sentence is returned.
    """
    hearings = list(hearings or [])
    kept = [
        s
        for s in _split_sentences(text)
        if not _audit_summary_stale_event_dates(s, hearings=hearings)
    ]
    result = " ".join(s.strip() for s in kept).strip()
    return result or llm.SUMMARY_INSUFFICIENT_DOCUMENTS


# Operator-note provenance leak. The AGGREGATION NOTE and any NOTE FROM
# OPERATOR are trusted CONTEXT for the model, not material to attribute to the
# subscriber — the fact they carry should be stated directly, never sourced to
# "the operator" or "the note". The model occasionally leaks the attribution
# anyway (us-v-knoot: "...; the operator notes that he has appealed his
# conviction"). Removing the lead-in while keeping the clause is ALWAYS safe
# (the note is trusted, so the fact stands on its own), so this is a plain
# deterministic cleanup — no retry, applied unconditionally. Scoped to the
# operator-AS-SOURCE sense ("the operator notes that ...", "according to the
# operator", "per the operator's note") so an unrelated "operator" in the prose
# (a "laptop-farm operator") is untouched.
_ATTRIB_LEADIN_RE = re.compile(
    r"(?:the\s+)?(?:operator(?:'s)?(?:[-\s]provided)?\s+note[sd]?|aggregation\s+note)"
    r"\s+(?:that|states?\s+that|indicates?\s+that|says?\s+that|notes?\s+that"
    r"|reflects?\s+that)\s+"
    r"|according\s+to\s+the\s+(?:operator(?:'s)?|(?:provided\s+)?note[s]?)\s*,?\s*"
    r"|as\s+noted\s+by\s+the\s+operator\s*,?\s*"
    r"|per\s+the\s+operator(?:'s)?\s+note\s*,?\s*",
    re.I,
)


def _audit_summary_attribution(text: str) -> list[str]:
    """Return a violation when the prose attributes a fact to the operator /
    note as a source (empty == clean). See :func:`_strip_operator_attribution`.
    """
    m = _ATTRIB_LEADIN_RE.search(text or "")
    return [f"operator-note attribution leak: {m.group(0).strip()!r}"] if m else []


def _strip_operator_attribution(text: str) -> str:
    """Remove operator/note attribution lead-ins, keeping the clause they
    introduce (capitalized when the lead-in opened a sentence).

    "...; the operator notes that he has appealed" -> "...; he has appealed".
    Always safe — the note is trusted context, so the underlying fact stands on
    its own; this just strips the provenance the reader should never see.
    """
    out: list[str] = []
    pos = 0
    for m in _ATTRIB_LEADIN_RE.finditer(text):
        out.append(text[pos : m.start()])
        prefix = "".join(out)
        at_sentence_start = (not prefix.strip()) or bool(
            re.search(r"[.!?]\s*$", prefix)
        )
        pos = m.end()
        if at_sentence_start:
            # Capitalize the first letter of the clause we kept.
            mm = re.match(r"(\s*)([a-zA-Z])", text[pos:])
            if mm:
                out.append(mm.group(1) + mm.group(2).upper())
                pos += mm.end()
    out.append(text[pos:])
    new = "".join(out)
    new = re.sub(r"\s+([,;.])", r"\1", new)
    new = re.sub(r"\s{2,}", " ", new).strip()
    return new or llm.SUMMARY_INSUFFICIENT_DOCUMENTS


# A clean, substantial dollar figure ($NNN and up). Used to tell whether a
# restitution order's amount actually extracted vs came through as garbled
# hand-filled-form OCR.
_RESTITUTION_FIGURE_RE = re.compile(r"\$\s?\d[\d,]{2,}(?:\.\d{2})?")


def _restitution_amount_unreadable(
    disposition_documents: list[dict[str, Any]],
) -> bool:
    """True when a granted restitution order is among the dispositions but no
    clean dollar figure extracts from any of them.

    The amount is hand-filled / garbled (CourtListener / our OCR produces
    noise for handwriting), so the summary must NOT state specific monetary
    figures: reporting only the OTHER, legible monetary penalties (e.g. a
    printed forfeiture order) would read to a subscriber as the defendant's
    total liability while a larger, unknown restitution exists. The
    us-v-chapman case is canonical — the entered restitution order (#49) has
    handwritten amounts that OCR'd to noise ("Total AD2, O52. 1S"), while the
    separate forfeiture order (#43) carries clean printed figures.

    ``disposition_documents`` is already the strict-classifier-granted set
    (``_is_disposition_document`` excludes motions / notices / *proposed*
    orders), so this only ever keys off ACTUAL granted restitution orders —
    never the typed proposed order a motion attaches.

    This also covers a restitution order whose document isn't uploaded to
    RECAP yet (or is sealed): ``_attach_text`` falls back to the docket
    description for dispositions with no extractable PDF text, and that
    description carries no dollar amount — so there's no clean figure and
    the order is treated as unreadable, exactly like the hand-filled case.
    The only time a no-document order reads as "readable" is when its docket
    description itself states the amount, in which case the figure genuinely
    IS available and stating it is correct.
    """
    restitution_orders = [
        d
        for d in disposition_documents
        if re.search(r"restitution", d.get("description") or "", re.I)
    ]
    if not restitution_orders:
        return False
    return not any(
        _RESTITUTION_FIGURE_RE.search(d.get("text") or "") for d in restitution_orders
    )


def _generate_guarded_summary(
    *,
    source_text: str,
    docket_id: Optional[int],
    **gen_kwargs: Any,
) -> tuple[str, str]:
    """Call ``llm.generate_docket_summary`` behind the truthfulness guard.

    Generates once; if the draft trips :func:`_audit_summary_text`, regenerates
    ONCE with the violations fed back as a correction, then keeps whichever
    attempt has fewer violations (ties favor the corrected retry). Persistent
    violations are logged at WARNING for operator review — the summary is never
    blocked, matching the "keep + WARN" enforcement choice.

    Finally, the chosen summary is run through :func:`_audit_summary_grounding`
    (dates / amounts not traceable to the scaffold or documents). A hit retries
    generation ONCE asking the model to drop the unsupported figures; if the
    retry comes back clean it is adopted, otherwise any sentence STILL carrying
    an ungrounded figure is deterministically removed via
    :func:`_strip_ungrounded_sentences`. The ungrounded figure never publishes;
    a WARNING is always logged so an operator can review the removal (a
    correctly-stated figure in an unusual format can trip this).

    Two more retry-then-strip tiers follow the same shape: the redundant-
    forfeiture guard (:func:`_audit_summary_forfeiture`) and the stale
    scheduled-event-date guard (:func:`_audit_summary_stale_event_dates` —
    a "scheduled for <date>" claim contradicting the scaffold's current row
    for that event). Finally the operator-note attribution cleanup runs
    unconditionally (always-safe, no retry).
    """
    known_dates = _grounding_dates(gen_kwargs)
    summary_text, model_id = llm.generate_docket_summary(**gen_kwargs)
    violations = _audit_summary_text(summary_text, source_text=source_text)
    if violations:
        log.warning(
            "summary: docket %s draft tripped truthfulness guard %s; "
            "retrying generation once with correction",
            docket_id,
            violations,
        )
        retry_text, retry_model = llm.generate_docket_summary(
            correction="; ".join(violations), **gen_kwargs
        )
        retry_violations = _audit_summary_text(retry_text, source_text=source_text)
        if not retry_violations:
            log.info(
                "summary: docket %s retry cleared the truthfulness guard", docket_id
            )
            summary_text, model_id = retry_text, retry_model
        else:
            log.warning(
                "summary: docket %s STILL tripped truthfulness guard after retry "
                "(draft=%s retry=%s); keeping the attempt with fewer violations "
                "for operator review — investigate the source documents",
                docket_id,
                violations,
                retry_violations,
            )
            if len(retry_violations) <= len(violations):
                summary_text, model_id = retry_text, retry_model

    ungrounded = _audit_summary_grounding(
        summary_text, known_dates=known_dates, source_text=source_text
    )
    if ungrounded:
        # A date / amount traces to none of the documents, the scaffold, or the
        # operator notes — a possible fabrication. Retry once asking the model
        # to drop it; the model, re-shown the documents, can keep and reformat
        # a figure it can actually support, so a clean retry both fixes a real
        # fabrication and recovers a false positive's correct figure.
        correction = (
            "These specific dates / dollar amounts in your draft do not appear "
            "in any provided document, the structured-events scaffold, or the "
            "operator notes: "
            + "; ".join(ungrounded)
            + ". Remove each unsupported date / amount (and any clause that "
            "depends on it), or restate only the figures the documents "
            "actually contain. Do not invent a substitute figure."
        )
        log.warning(
            "summary: docket %s draft carried ungrounded facts %s; retrying "
            "generation once to remove them",
            docket_id,
            ungrounded,
        )
        retry_text, retry_model = llm.generate_docket_summary(
            correction=correction, **gen_kwargs
        )
        retry_ungrounded = _audit_summary_grounding(
            retry_text, known_dates=known_dates, source_text=source_text
        )
        retry_tier12 = _audit_summary_text(retry_text, source_text=source_text)
        if not retry_ungrounded and not retry_tier12:
            log.info("summary: docket %s retry cleared the grounding guard", docket_id)
            summary_text, model_id = retry_text, retry_model
        else:
            # Retry didn't fully clear. Adopt it only if it removed ungrounded
            # facts WITHOUT introducing an absence/custody regression; otherwise
            # keep the original draft. Then deterministically strip any sentence
            # that still carries an ungrounded figure so it cannot publish.
            if not retry_tier12 and len(retry_ungrounded) < len(ungrounded):
                summary_text, model_id = retry_text, retry_model
            summary_text = _strip_ungrounded_sentences(
                summary_text, known_dates=known_dates, source_text=source_text
            )
            log.warning(
                "summary: docket %s STILL carried ungrounded facts after retry "
                "(%s); removed the offending sentence(s) and kept the rest — "
                "verify the removal against the docket (a correctly-stated "
                "figure in an unusual format can trip this)",
                docket_id,
                retry_ungrounded or ungrounded,
            )

    # Tier 4 — redundant forfeiture money judgment (forfeiture == restitution).
    # Retry once asking the model to omit it per the prompt's omission rule; if
    # it still states the doubled figure, deterministically strip the forfeiture
    # clause from the known-good draft so the inflated total can't publish.
    forfeiture = _audit_summary_forfeiture(summary_text)
    if forfeiture:
        log.warning(
            "summary: docket %s draft tripped forfeiture-redundancy guard %s; "
            "retrying generation once with correction",
            docket_id,
            forfeiture,
        )
        correction = (
            "Your draft states a forfeiture money judgment whose dollar amount "
            "EQUALS the restitution amount. Per the redundant-forfeiture rule, "
            "OMIT the forfeiture money judgment entirely — report only the "
            "restitution (and the special assessment, if any). Do not mention "
            "the forfeiture money judgment or restate its amount."
        )
        retry_text, retry_model = llm.generate_docket_summary(
            correction=correction, **gen_kwargs
        )
        retry_clean = (
            not _audit_summary_forfeiture(retry_text)
            and not _audit_summary_text(retry_text, source_text=source_text)
            and not _audit_summary_grounding(
                retry_text, known_dates=known_dates, source_text=source_text
            )
        )
        if retry_clean:
            log.info(
                "summary: docket %s retry cleared the forfeiture-redundancy guard",
                docket_id,
            )
            summary_text, model_id = retry_text, retry_model
        else:
            # The retry didn't come back fully clean — keep the known-good draft
            # and excise the forfeiture clause deterministically rather than
            # adopt a retry that may have regressed elsewhere.
            summary_text = _strip_redundant_forfeiture(summary_text)
            log.warning(
                "summary: docket %s STILL stated a redundant forfeiture money "
                "judgment after retry; stripped the forfeiture clause from the "
                "draft — verify against the docket",
                docket_id,
            )

    # Tier 5 — stale scheduled-event date (the prose contradicts the
    # scaffold's current row for the event; see the tier comment on
    # _STALE_EVENT_CLAIM_RE — the us-v-ding superseded-continuance case).
    # Retry once quoting the scaffold's current date back; if the stale date
    # persists, deterministically strip the sentence(s) carrying it so the
    # summary cannot contradict the calendar feed rendered from the same
    # store.
    hearings_rows = list(gen_kwargs.get("hearings") or [])
    stale_events = _audit_summary_stale_event_dates(
        summary_text, hearings=hearings_rows
    )
    if stale_events:
        log.warning(
            "summary: docket %s draft carried stale scheduled-event date(s) %s; "
            "retrying generation once with the scaffold's current date",
            docket_id,
            stale_events,
        )
        correction = (
            "Your draft states a date for a scheduled event that does not "
            "match the structured-events scaffold's CURRENT row for that "
            "event: "
            + "; ".join(stale_events)
            + ". The scaffold outranks document text for an event's current "
            "date — a continuance stated in a document may itself have been "
            "superseded by a later notice not in your document set. Restate "
            "the event using the scaffold row's date, or omit the specific "
            "date."
        )
        retry_text, retry_model = llm.generate_docket_summary(
            correction=correction, **gen_kwargs
        )
        retry_clean = (
            not _audit_summary_stale_event_dates(retry_text, hearings=hearings_rows)
            and not _audit_summary_text(retry_text, source_text=source_text)
            and not _audit_summary_grounding(
                retry_text, known_dates=known_dates, source_text=source_text
            )
            and not _audit_summary_forfeiture(retry_text)
        )
        if retry_clean:
            log.info(
                "summary: docket %s retry cleared the stale-event-date guard",
                docket_id,
            )
            summary_text, model_id = retry_text, retry_model
        else:
            # The retry didn't come back fully clean — keep the known-good
            # draft and remove the stale sentence(s) deterministically rather
            # than adopt a retry that may have regressed elsewhere.
            summary_text = _strip_stale_event_date_sentences(
                summary_text, hearings=hearings_rows
            )
            log.warning(
                "summary: docket %s STILL carried a stale scheduled-event date "
                "after retry; removed the offending sentence(s) — the calendar "
                "feed carries the event's current date",
                docket_id,
            )

    # Operator-note provenance leak — the aggregation_note / NOTE FROM OPERATOR
    # are context FOR the model, not material to attribute to subscribers. The
    # model sometimes writes "the operator notes that ..." (us-v-knoot). The
    # fact is trusted, so stripping just the attribution lead-in (keeping the
    # clause) is always safe — a deterministic cleanup, no retry needed.
    if _audit_summary_attribution(summary_text):
        # The audit matched the lead-in, so the strip is guaranteed to change
        # the text (it removes that match) — no need to re-compare.
        log.warning(
            "summary: docket %s removed operator-note attribution leak "
            "('the operator notes ...') — the note is context, not "
            "subscriber-facing material",
            docket_id,
        )
        summary_text = _strip_operator_attribution(summary_text)
    return summary_text, model_id


# ---------------------------------------------------------------------------
# Inline document links
# ---------------------------------------------------------------------------
#
# Links in the summary prose are newspaper-style: the action WORD itself is
# the link ("the defendants were <charged> with ...", "<pled guilty> to ..."),
# not a footnote marker or a "(see Doc 1)" citation. The reader sees and taps
# the word.
#
# To let the LLM decide which word links to which document, each document fed
# to it is shown with a short reference token ("[D1]", "[D2]", ...) — a
# prompt-only handle that NEVER reaches the page. The prompt asks the model to
# wrap the linked word in a marker shaped like a markdown link but pointing at
# the token rather than a URL: ``[charged](doc:D1)``, ``[pled guilty](doc:D3)``.
# We assign the tokens HERE so the prompt rendering (llm._append_doc_block) and
# the resolution below agree by construction, then resolve the markers to each
# document's public URL after generation and BEFORE the prose is stored.
#
# The model can link the wrong document for a word, or point at a document
# with no reachable URL, but it can never produce a link to a document that
# isn't in the set we fed it: a token we never assigned is dropped back to its
# bare word and logged. This keeps the feature "dynamic enough to support any
# document referenced in any generated summary" (any document we show the LLM
# is linkable) while staying inside the truthfulness discipline the rest of
# the pipeline follows. See the AGENTS.md design decision "Inline document
# links in case summaries."

_DOC_REF_PREFIX = "D"

# Matches a link marker the LLM emits: ``[linked words](doc:D1)``. The anchor
# admits anything but a closing bracket; the token is alnum / dash / underscore
# so ordinary parentheticals ("(2024)", "[sic]") are left alone.
_DOC_LINK_RE = re.compile(r"\[([^\]]+)\]\(doc:([A-Za-z0-9_-]+)\)")

# A jury VERDICT FORM is a checkbox template — its extracted text is the form
# layout, identical whether blank or filled, because the jury's actual findings
# are mark-ups not in the text layer. So it's a misleading link target ("the
# jury returned a verdict" pointing at a blank-looking form), and we never link
# it. It still rides into the prompt as summary context; the result-bearing
# link target, when one exists, is a judgment (which states the outcome in
# text). The actual verdict is often a paperless minute entry with no PDF.
_VERDICT_FORM_RE = re.compile(r"\bverdict\s+form\b", re.IGNORECASE)


def _is_verdict_form(doc: dict[str, Any]) -> bool:
    return bool(_VERDICT_FORM_RE.search(doc.get("description") or ""))


def _assign_document_refs(
    *doc_lists: list[dict[str, Any]],
) -> dict[str, Optional[str]]:
    """Stamp each document with a reference token and return ``{ref: url}``.

    Mutates the doc dicts in place, setting ``ref`` ("D1", "D2", ...) in the
    order the lists are passed (the same order they're rendered in the
    prompt). The returned map is keyed by EVERY assigned ref so the resolver
    can tell a known-but-unreachable document (ref present, url ``None`` —
    drop the marker silently) from a token the model invented (ref absent —
    drop and warn). A document's URL is its CourtListener ``url`` (set by
    :func:`_attach_text`) or, for an operator extra, its ``source_url``.

    A jury verdict form is deliberately mapped to ``None`` even when it has a
    URL (see :func:`_is_verdict_form`): the document stays in the prompt as
    context but is never a link target, so any marker the model puts on it
    collapses back to plain text.
    """
    link_map: dict[str, Optional[str]] = {}
    n = 0
    for docs in doc_lists:
        for doc in docs:
            n += 1
            ref = f"{_DOC_REF_PREFIX}{n}"
            doc["ref"] = ref
            url = doc.get("url") or doc.get("source_url")
            if url and _is_verdict_form(doc):
                url = None
            link_map[ref] = url
    return link_map


# Span-boundary tidy applied AFTER markers resolve to ``[words](url)``. The
# model is inconsistent about exactly where a link span starts and ends, so two
# deterministic fixes make every link read as a clean action phrase:
#   - pull an immediately-preceding auxiliary / linking verb INTO the link, so
#     "was [charged](url)" becomes "[was charged](url)" (looped, so compound
#     "has been [charged]" fully absorbs);
#   - push a DANGLING trailing preposition that introduces the detail OUT of
#     the link, so "[convicted at trial of](url)" becomes
#     "[convicted at trial](url) of" and "[charged with](url)" becomes
#     "[charged](url) with".
# Only fires on a verb/preposition sitting flush against the link; the charge
# names, counts, amounts, and dates after the preposition stay plain text.
_LINK_LEADING_VERB_RE = re.compile(
    r"\b(was|were|is|are|be|been|has|have|had)\s+\[([^\]]+)\]\((https?://[^\s)]+)\)",
    re.IGNORECASE,
)
_LINK_TRAILING_PREP_RE = re.compile(
    r"\[([^\]]+?)\s+(of|to|with|for|on|in|against|upon|into)\]"
    r"\((https?://[^\s)]+)\)",
    re.IGNORECASE,
)


def _tidy_link_spans(text: str) -> str:
    """Normalize link-span boundaries: leading verb in, trailing preposition out.

    Repeats to a fixpoint so a compound auxiliary ("has been charged") and a
    verb+preposition pair ("was charged with") both fully settle. Termination
    is guaranteed: trailing-preposition stripping only ever shrinks an anchor
    from the back and leading-verb absorption only ever grows it from the
    front, each drawing from a finite supply of flush words, so the text
    reaches a fixpoint after a couple of passes and the loop returns.
    """
    while True:
        new = _LINK_TRAILING_PREP_RE.sub(
            lambda m: f"[{m.group(1)}]({m.group(3)}) {m.group(2)}", text
        )
        new = _LINK_LEADING_VERB_RE.sub(
            lambda m: f"[{m.group(1)} {m.group(2)}]({m.group(3)})", new
        )
        if new == text:
            return text
        text = new


def _resolve_document_links(text: str, link_map: dict[str, Optional[str]]) -> str:
    """Replace ``[words](doc:Dn)`` markers with resolved markdown links.

    A known ref with a URL becomes ``[words](url)`` (the index renderer turns
    that into an ``<a>`` on the words themselves); a known ref with no URL, or
    an unknown ref, collapses back to the bare ``words`` so the prose still
    reads cleanly. Unknown refs are logged — they mean the model linked a
    token we never assigned, the link analogue of the grounding guard's
    warning.

    After resolution, :func:`_tidy_link_spans` normalizes each link's span so
    the leading verb is inside and a dangling trailing preposition is outside.
    """

    def repl(m: "re.Match[str]") -> str:
        anchor, ref = m.group(1), m.group(2)
        if ref not in link_map:
            log.warning(
                "summary: dropping link to unknown document token %r "
                "(linked words=%r) — the model linked a reference that was "
                "not in the document set",
                ref,
                anchor,
            )
            return anchor
        url = link_map[ref]
        if not url:
            return anchor
        return f"[{anchor}]({url})"

    return _tidy_link_spans(_DOC_LINK_RE.sub(repl, text))


# ---------------------------------------------------------------------------
# Per-docket entry point
# ---------------------------------------------------------------------------


def summarize_docket(
    *,
    cl: CourtListener,
    store: Store,
    case: CaseConfig,
    docket_id: int,
    aggregation_note: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    allow_ocr: bool = True,
) -> Optional[dict[str, Any]]:
    """Generate, persist, and return the summary row for the LOGICAL PACER docket.

    Resolves ``docket_id`` to ``(docket_number, court_id)`` via the
    ``dockets`` table, then pools entries across every CourtListener ``docket_id``
    in the same group (see :meth:`Store.get_docket_group_ids` and the
    matching AGENTS.md design decision). The summary row is keyed by
    ``(case_id, docket_number, court_id)``, so calling this multiple
    times with different ``docket_id``s from the same group writes to
    the same row (idempotent — last write wins).

    Returns ``None`` when no primary-document text is available — we
    don't want to hallucinate a summary from the docket metadata alone.
    """
    meta = store.get_docket_meta(docket_id) or {}
    docket_number = meta.get("docket_number")
    court_id = meta.get("court_id")
    if not docket_number or not court_id:
        # The expected reason is "this docket id has never been synced
        # successfully" — the next sync will populate the columns and
        # this gate will clear. But it can also fire when the operator
        # added a docket id that doesn't correspond to a real
        # CourtListener docket (typo in config.yaml) — in that case
        # the sync's `cl.get_docket(docket_id)` 404s, and we never
        # call `upsert_docket_meta` for it, so the row stays empty
        # forever. Logging both possibilities so the operator knows
        # to check the docket id if a sync has already run and this
        # warning is still firing.
        log.warning(
            "summary: skipping docket %s — no (docket_number, court_id) "
            "metadata in the local store. Expected if this docket id has "
            "never been synced; if a sync has already run successfully "
            "for it, verify that the id corresponds to a real CourtListener "
            "docket (check the sync log for a 4xx on /dockets/%s/).",
            docket_id,
            docket_id,
        )
        return None
    group_docket_ids = store.get_docket_group_ids(docket_number, court_id) or [
        docket_id
    ]
    docket_for_prompt = {
        "docket_id": docket_id,
        "docket_number": docket_number,
        "court_id": court_id,
        "court_citation": (store.get_court_citation(court_id) if court_id else None),
        "court_tz": tz_for(court_id) if court_id else None,
        # This docket's OWN caption (e.g. "United States v. Kelly") — the
        # disambiguator for a multi-defendant case whose overall name ("...
        # et al.") and shared charging affidavit name a different lead
        # defendant. The prompt uses it to foreground this docket's defendant.
        "caption": meta.get("case_name"),
    }

    log.info(
        "summary: scanning %s (%s) for primary documents across %d CourtListener docket(s) %s",
        docket_number,
        court_id,
        len(group_docket_ids),
        group_docket_ids,
    )
    primary, dispositions = find_primary_documents_for_group(
        cl, group_docket_ids, store=store
    )
    log.info(
        "summary: %s (%s) found %d primary document(s), %d disposition document(s)",
        docket_number,
        court_id,
        len(primary),
        len(dispositions),
    )

    primary_documents = _attach_text(primary, allow_ocr=allow_ocr)
    disposition_documents = _attach_text(
        dispositions,
        allow_ocr=allow_ocr,
        allow_description_fallback=True,
    )

    # Flag suspiciously-short primary documents so the operator can
    # investigate — an indictment / complaint whose extracted text is
    # only a couple of KB is almost certainly a parser failure (image-
    # only PDF without an embedded text layer; pypdf returning only the
    # page headers and caption from a full document that the extractor
    # couldn't decode; a sealed-but-listed entry whose body never
    # landed in RECAP). Federal indictments and complaints rarely run
    # under several KB even on single-count cases — the boilerplate
    # caption, jurisdictional allegations, and per-count language add
    # up. The us-v-moucka regression that drove this log was exactly
    # that shape: a full multi-count indictment whose extracted text
    # came out to ~4 KB of user_chars total (caption + page headers
    # only), which is why the corresponding summary opened with
    # "The primary document text consists only of page-header citations".
    # The summary LLM is told to work around partial inputs silently in
    # the subscriber-facing prose (see the matching `CRITICAL — work
    # around partial or low-quality source documents SILENTLY` rule in
    # `SUMMARY_SYSTEM_PROMPT`), so without this log a subscriber-visible
    # "generic" summary on a docket with a broken PDF extraction would
    # leave no trace for the operator. With the log, a sweep of the
    # warning stream surfaces broken extractions before subscribers
    # notice the diluted summary. The 1500-char threshold is the
    # empirical floor below which we've seen parser failures rather
    # than genuinely short documents — adjust if real-world data
    # shows the threshold is wrong.
    PRIMARY_DOC_SUSPICIOUSLY_SHORT_CHARS = 1500
    for doc in primary_documents:
        text = doc.get("text") or ""
        if 0 < len(text) < PRIMARY_DOC_SUSPICIOUSLY_SHORT_CHARS:
            log.warning(
                "summary: docket %s — primary document at entry #%s "
                "(filed %s) extracted to only %d chars; PDF parsing "
                "may have failed (image-only PDF, pypdf returning only "
                "page headers, or similar). The summary LLM is told to "
                "work around this silently, so a confusing or generic "
                "subscriber-facing summary on this case is the signal "
                "to investigate the source PDF.",
                docket_id,
                doc.get("entry_number"),
                doc.get("date_filed"),
                len(text),
            )

    # Fetch any operator-supplied extra_documents for this docket group.
    # These don't slot into primary/disposition — they ride into the LLM
    # prompt as their own block, each carrying the operator's note
    # describing what it is. Pinned to any docket_id in the group.
    extra_documents = _fetch_extra_documents(
        case, group_docket_ids, allow_ocr=allow_ocr
    )
    # Drop any extras whose extracted text matches a CourtListener-
    # surfaced doc on the same docket — the upstream gap that justified
    # the entry has likely closed (someone re-uploaded the PDF to PACER,
    # or CourtListener's reconciler caught up). Without this, the same
    # document body would reach the summary LLM twice and exert
    # outsized influence. The dedup logs a warning so the operator
    # knows to remove the now-redundant config entry.
    extra_documents = _filter_extras_already_in_cl(
        extra_documents, primary_documents + disposition_documents
    )

    if not primary_documents and len(case.dockets) > 1:
        # Appellate dockets (and parallel filings that pivot off a sibling)
        # don't re-file their own complaint — the opener is a clerical
        # "case opened, notice of appeal from <lower court>" entry, which
        # the primary-document regex correctly refuses to match. Borrow
        # the primary text from a sibling docket on the same case_id so
        # we still get a summary, AND borrow the sibling's dispositions —
        # the conviction/sentence being appealed lives only on the
        # trial-court docket, so without them the appellate summary can
        # only describe the charges and never the outcome under review.
        # The structured-events scaffold below still supplies this
        # docket's own appellate posture, so the framing stays
        # appellate-perspective rather than collapsing into the
        # trial-court narrative.
        primary_documents, borrowed_dispositions = _borrow_primary_from_siblings(
            cl=cl,
            store=store,
            case=case,
            primary_docket_id=docket_id,
            allow_ocr=allow_ocr,
        )
        if borrowed_dispositions:
            # Merge with any disposition this docket carries on its own
            # (e.g. an appellate ruling), de-duped by entry id — entries
            # from a different docket can't collide, so this is belt-and-
            # suspenders for the future appellate-disposition case.
            seen_ids = {d.get("entry_id") for d in disposition_documents}
            disposition_documents = disposition_documents + [
                d for d in borrowed_dispositions if d.get("entry_id") not in seen_ids
            ]

    if not primary_documents and not extra_documents:
        # No extractable document text on this docket. Two distinct
        # failure modes, two distinct subscriber-facing messages so
        # operators (and readers) can tell them apart at a glance:
        #   - ``primary`` is non-empty but ``primary_documents`` came
        #     back empty: the matcher identified an indictment / complaint
        #     / information on the docket (a recap_document carrying the
        #     matcher signal, e.g. ``description='Indictment'``) but
        #     ``_attach_text`` couldn't extract text from any of them
        #     (PDF not on RECAP / IA yet, image-only scan with no
        #     recoverable layer, OCR tools absent). The us-v-lytvynenko
        #     shape: we know the case has an indictment, we just can't
        #     read it. Write the "primary document(s) could not be read"
        #     message.
        #   - ``primary`` is empty: no entry on the docket matched
        #     ``is_primary_document`` at all (appellate filings whose
        #     opener is a clerical "case opened" entry, dockets carrying
        #     only procedural notices, paperless-only filings). Write
        #     the broader ``SUMMARY_INSUFFICIENT_DOCUMENTS`` refusal.
        # Both paths write directly without an LLM round-trip — there's
        # nothing for the LLM to summarize from. WARN log carries the
        # primary/disposition counts so the operator can investigate the
        # right thing (RECAP availability + OCR-tool install for the
        # first case; CourtListener docket inspection + possible
        # `extra_documents` workaround for the second).
        if primary:
            # Classify each identified primary by its main recap_doc's
            # state so subscribers and operators see the specific
            # failure shape rather than a catch-all "could not be read":
            #   sealed = main recap_doc has is_sealed=True
            #   not-available = main recap_doc has no filepath_ia AND
            #     no filepath_local AND empty plain_text — there was
            #     nothing to fetch
            #   unreadable = anything else (had a URL or text, the
            #     extraction chain just couldn't produce usable output)
            # The catch-all wins when the primaries are in mixed states:
            # unreadable is the most general of the three and accurately
            # describes any failure path.
            states = [_primary_failure_state(e) for e in primary]
            if all(s == "sealed" for s in states):
                summary_text = llm.SUMMARY_PRIMARY_DOCUMENT_SEALED
                state_label = "sealed"
            elif all(s == "not-available" for s in states):
                summary_text = llm.SUMMARY_PRIMARY_DOCUMENT_NOT_AVAILABLE
                state_label = "not-available"
            else:
                summary_text = llm.SUMMARY_PRIMARY_DOCUMENT_UNREADABLE
                state_label = "unreadable"
            counts = {"sealed": 0, "not-available": 0, "unreadable": 0}
            for s in states:
                counts[s] += 1
            log.warning(
                "summary: docket %s — primary document(s) identified "
                "but no text extractable: primary=%d "
                "(sealed=%d, not-available=%d, unreadable=%d) "
                "disposition=%d; writing %s message",
                docket_id,
                len(primary),
                counts["sealed"],
                counts["not-available"],
                counts["unreadable"],
                len(dispositions),
                state_label,
            )
        else:
            summary_text = llm.SUMMARY_INSUFFICIENT_DOCUMENTS
            log.warning(
                "summary: docket %s — no primary document identified "
                "(primary=0 disposition=%d); writing the insufficient-"
                "documents refusal so subscribers see the docket on the index.",
                docket_id,
                len(dispositions),
            )
        model_id = "n/a (no document text)"
        source_ids = [
            e["id"] for e in (primary + dispositions) if e.get("id") is not None
        ]
        store.upsert_case_summary(
            case.case_id,
            docket_number,
            court_id,
            summary=summary_text,
            model=model_id,
            source_entry_ids=source_ids,
        )
        log.info(
            "summary: wrote %s (%s) refusal (%d chars, model=%s)",
            docket_number,
            court_id,
            len(summary_text),
            model_id,
        )
        return {
            "docket_number": docket_number,
            "court_id": court_id,
            "group_docket_ids": list(group_docket_ids),
            "summary": summary_text,
            "model": model_id,
            "source_entry_ids": source_ids,
        }

    hearings = _hearings_for_group(store, case.case_id, group_docket_ids)
    deadlines = _deadlines_for_group(store, case.case_id, group_docket_ids)

    # Sealing detection runs on the canonical docket_id (the first in the
    # group, which is the freshest-modified CourtListener row). The advisory walks
    # ~one page of entries on that docket; running it across every sibling
    # would multiply CourtListener API calls without strengthening the signal — the
    # sealing order is on the PACER docket itself, not per-CourtListener-row, so
    # whichever sibling is checked first will see it.
    sealing_advisory = detect_sealing(
        cl, group_docket_ids[0], dispositions=dispositions
    )
    if sealing_advisory is not None:
        log.info(
            "summary: %s (%s) — sealing advisory: order at entry #%s "
            "(filed %s), %d post-seal available entries observed",
            docket_number,
            court_id,
            sealing_advisory.get("sealing_entry_number"),
            sealing_advisory.get("sealing_date_filed"),
            sealing_advisory.get("available_post_seal_entries"),
        )

    # Corpus the guards ground claims against — everything the LLM was given
    # and may legitimately cite: the document text PLUS operator-supplied
    # trusted metadata (the aggregation note and any extra_documents operator
    # notes). Without the metadata, a date or amount an operator puts in a
    # note — e.g. a sentencing date conveyed via aggregation_note for an
    # appeal docket whose own record holds no judgment (the us-v-gholinejad
    # case) — would be falsely flagged as ungrounded. A custody/flight claim
    # or a date/amount is allowed only if it appears somewhere in here.
    grounding_parts: list[Optional[str]] = [
        d.get("text")
        for d in primary_documents + disposition_documents + (extra_documents or [])
    ]
    grounding_parts += [d.get("operator_note") for d in (extra_documents or [])]
    grounding_parts.append(aggregation_note)
    source_text = "\n".join(p for p in grounding_parts if p)

    # When a granted restitution order is present but its amount didn't
    # extract (hand-filled / garbled OCR), tell the LLM to omit ALL specific
    # monetary figures and say "ordered to pay restitution" — otherwise the
    # legible penalties (e.g. a printed forfeiture order) read as the total
    # liability while the larger unknown restitution is invisible. See the
    # us-v-chapman case and the DOCKET FINANCIAL ADVISORY prompt rule.
    restitution_unreadable = _restitution_amount_unreadable(disposition_documents)
    if restitution_unreadable:
        log.info(
            "summary: %s (%s) — restitution ordered but amount not legibly "
            "extractable; advising the LLM to omit specific monetary figures",
            docket_number,
            court_id,
        )

    # Stamp each document with a citation token (D1, D2, ...) and build the
    # ref->url map. The LLM cites documents by token in the prose; we resolve
    # the tokens to URLs right after generation. Assigning here keeps the
    # prompt rendering and the resolution in lockstep on which token is which
    # document.
    link_map = _assign_document_refs(
        primary_documents, disposition_documents, extra_documents
    )

    try:
        summary_text, model_id = _generate_guarded_summary(
            source_text=source_text,
            docket_id=docket_id,
            case_name=case.name,
            aggregation_note=aggregation_note,
            docket=docket_for_prompt,
            primary_documents=primary_documents,
            disposition_documents=disposition_documents,
            extra_documents=extra_documents,
            hearings=hearings,
            deadlines=deadlines,
            sealing_advisory=sealing_advisory,
            restitution_unreadable=restitution_unreadable,
            provider=provider,
            model=model,
        )
    except llm.ContextWindowExceededError as exc:
        # The assembled prompt was too large for the model's / configured
        # context window. Don't publish a summary built from a silently
        # truncated prompt — store the polite refusal (rendered like any
        # other fallback message) and WARN loudly so the operator can raise
        # the window or trim the per-document char budgets. This is the one
        # refusal driven by prompt SIZE rather than document availability,
        # so it lives here rather than in the no-document-text branch above.
        log.warning(
            "summary: docket %s — prompt exceeds the model's context window "
            "(%s); writing the context-exceeded refusal instead of a summary "
            "built from a truncated prompt. Raise the context window "
            "(OLLAMA_NUM_CTX / a larger model) or lower the per-document "
            "char budgets.",
            docket_id,
            exc,
        )
        summary_text = llm.SUMMARY_CONTEXT_EXCEEDED
        model_id = "n/a (context window exceeded)"

    # Resolve inline document citations to real links before storing. The
    # truthfulness guards inside _generate_guarded_summary run on the
    # token-bearing prose (the tokens carry no dates / amounts / URLs, so
    # they don't perturb the guards); resolution happens after so the stored
    # summary carries real ``[anchor](https://...)`` links the renderer can
    # turn into anchors.
    summary_text = _resolve_document_links(summary_text, link_map)

    if llm.SUMMARY_INSUFFICIENT_DOCUMENTS in summary_text:
        # The model exercised its prompt-level refusal — store and render
        # the fallback prose but surface it loudly so the operator can
        # investigate whether the extraction chain is at fault (e.g., a
        # PDF that needs OCR tools we don't have installed, or an entry
        # whose only document is still sealed).
        log.warning(
            "summary: docket %s — LLM emitted insufficient-documents "
            "fallback (primary=%d disposition=%d extra=%d); store will "
            "show the refusal text. Check whether the extracted document "
            "text actually carried the case's substance.",
            docket_id,
            len(primary_documents),
            len(disposition_documents),
            len(extra_documents),
        )

    source_ids = [
        d["entry_id"]
        for d in primary_documents + disposition_documents
        if d.get("entry_id")
    ]
    store.upsert_case_summary(
        case.case_id,
        docket_number,
        court_id,
        summary=summary_text,
        model=model_id,
        source_entry_ids=source_ids,
    )

    log.info(
        "summary: wrote %s (%s) summary (%d chars, model=%s)",
        docket_number,
        court_id,
        len(summary_text),
        model_id,
    )
    return {
        "docket_number": docket_number,
        "court_id": court_id,
        "group_docket_ids": list(group_docket_ids),
        "summary": summary_text,
        "model": model_id,
        "source_entry_ids": source_ids,
    }


def _group_dockets_on_case(
    store: Store, case: CaseConfig
) -> list[tuple[str, str, int]]:
    """Map the case's CourtListener docket_ids onto logical PACER docket groups.

    Returns one ``(docket_number, court_id, canonical_docket_id)`` tuple
    per group. The canonical CourtListener docket_id is the freshest one in the
    group (the head of :meth:`Store.get_docket_group_ids`), used as the
    representative for :func:`summarize_docket` calls. CourtListener docket_ids
    that have no ``dockets`` metadata yet (never synced) are skipped
    with a warning — the next sync populates the row and the next
    summarize call picks them up.
    """
    seen: set[tuple[str, str]] = set()
    groups: list[tuple[str, str, int]] = []
    for docket_id in case.dockets:
        meta = store.get_docket_meta(docket_id)
        if not meta or not meta.get("docket_number") or not meta.get("court_id"):
            log.warning(
                "summary: docket %s on case %s has no (docket_number, "
                "court_id) metadata yet — skipping until sync populates it",
                docket_id,
                case.case_id,
            )
            continue
        key = (meta["docket_number"], meta["court_id"])
        if key in seen:
            continue
        seen.add(key)
        # The canonical docket_id can be any CourtListener row in the group — pass
        # this one through, and summarize_docket internally re-resolves
        # to the full group via Store.get_docket_group_ids.
        groups.append((key[0], key[1], docket_id))
    return groups


def refresh_stale(
    *,
    cl: CourtListener,
    store: Store,
    cases: Iterable[CaseConfig],
    case_overrides: Optional[dict[str, dict[str, Any]]] = None,
    only_case_ids: Optional[set[str]] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    allow_ocr: bool = True,
    force: bool = False,
) -> dict[str, set[tuple[str, str]]]:
    """Regenerate any summaries that are missing or marked stale.

    Walks ``cases`` (the parsed CaseConfig list from cli), groups each
    case's CourtListener docket_ids by ``(docket_number, court_id)``, and for each
    group checks :meth:`Store.is_summary_stale`. Stale groups get
    regenerated via :func:`summarize_docket`. Returns
    ``{case_id: {(docket_number, court_id), ...}}`` for the groups that
    were (re)written so callers can scope the resulting emit to the
    affected calendars.

    This is the automatic path: ``sync.process_entry`` flips ``stale=1`` when
    it sees a primary-document or disposition entry, and ``cmd_sync`` /
    the webhook ``emit_fn`` call this at the end of a sync, before
    ``emit_calendars``, so a freshly-landed judgment shows up in the
    rendered index in the same cycle without operator intervention.

    Missing rows are treated as stale, so adding a new case to the config
    seeds its summary on the next sync — no separate ``summarize``
    invocation needed.

    ``case_overrides`` is the raw ``cfg['cases']`` list mapped by id, used
    only to pick up the per-case ``aggregation_note`` for multi-docket
    cases (the dataclass CaseConfig doesn't carry that field).
    """
    case_overrides = case_overrides or {}
    written: dict[str, set[tuple[str, str]]] = {}
    for case in cases:
        if only_case_ids is not None and case.case_id not in only_case_ids:
            continue
        aggregation_note = (case_overrides.get(case.case_id) or {}).get(
            "aggregation_note"
        )
        for docket_number, court_id, canonical_docket_id in _group_dockets_on_case(
            store, case
        ):
            if not force and not store.is_summary_stale(
                case.case_id, docket_number, court_id
            ):
                continue
            log.info(
                "summary: %s (%s) on case %s %s — regenerating",
                docket_number,
                court_id,
                case.case_id,
                "force-refresh" if force else "is stale or missing",
            )
            # Isolate per-docket failures: a transient provider error (e.g. a
            # 503 that exhausts retries, or an empty-response on one docket)
            # must not abort the whole batch and lose every other case's
            # summary for this run. Log it and leave the row stale so the next
            # refresh retries just that docket.
            try:
                row = summarize_docket(
                    cl=cl,
                    store=store,
                    case=case,
                    docket_id=canonical_docket_id,
                    aggregation_note=aggregation_note,
                    provider=provider,
                    model=model,
                    allow_ocr=allow_ocr,
                )
            except Exception:
                log.exception(
                    "summary: %s (%s) on case %s failed; leaving stale for the "
                    "next refresh and continuing with the remaining dockets",
                    docket_number,
                    court_id,
                    case.case_id,
                )
                continue
            if row:
                written.setdefault(case.case_id, set()).add((docket_number, court_id))
    return written


def summarize_case(
    *,
    cl: CourtListener,
    store: Store,
    case: CaseConfig,
    aggregation_note: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    allow_ocr: bool = True,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Summarize every logical PACER docket on a case.

    CourtListener docket_ids that resolve to the same ``(docket_number, court_id)``
    group are summarized once — entries are pooled across siblings. With
    ``force=False`` (the default), groups that already have a summary
    row are skipped. Pass ``force=True`` after a model upgrade or prompt
    change to overwrite.
    """
    out: list[dict[str, Any]] = []
    for docket_number, court_id, canonical_docket_id in _group_dockets_on_case(
        store, case
    ):
        if not force and store.get_docket_summary(
            case.case_id, docket_number, court_id
        ):
            log.info(
                "summary: skipping %s (%s) on case %s — already summarized, "
                "pass force=True to overwrite",
                docket_number,
                court_id,
                case.case_id,
            )
            continue
        row = summarize_docket(
            cl=cl,
            store=store,
            case=case,
            docket_id=canonical_docket_id,
            aggregation_note=aggregation_note,
            provider=provider,
            model=model,
            allow_ocr=allow_ocr,
        )
        if row:
            out.append(row)
    return out
