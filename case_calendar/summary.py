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
from typing import TYPE_CHECKING, Any, Iterable, Optional

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


def is_primary_document(entry: dict[str, Any]) -> bool:
    return bool(_PRIMARY_DOCUMENT_RE.match(_entry_description_head(entry)))


def is_disposition(entry: dict[str, Any]) -> bool:
    head = _entry_description_head(entry)
    # Conference-class entries are scheduling, not dispositions. Reject
    # outright so a "Telephonic Status Conference re: Sentencing" doesn't
    # trip the broad keyword match below.
    if _DISPOSITION_NEGATIVE_RE.search(head):
        return False
    if _DISPOSITION_RE.match(head):
        return True
    return bool(_DISPOSITION_KEYWORD_RE.search(head))


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
    """Strict — the LLM document set in ``find_primary_documents``.

    Accepts entries whose head is a head-anchored disposition phrase
    (``_DISPOSITION_RE``) outright, plus any order / minute-entry whose
    body carries disposition keywords. Rejects motions, briefs,
    responses, status reports, and notices of filing that mention
    disposition vocabulary in passing — those still flip the
    case_summaries row stale via the broader ``is_disposition``, but they
    are not themselves the ruling document and must not be fed to the
    summary LLM as one.
    """
    head = _entry_description_head(entry)
    if not head:
        return False
    if _DISPOSITION_NEGATIVE_RE.search(head):
        return False
    if _DISPOSITION_DOCUMENT_NEGATIVE_RE.search(head):
        return False
    if _DISPOSITION_RE.match(head):
        return True
    if not _DISPOSITION_DOCUMENT_HEAD_RE.match(head):
        return False
    return bool(_DISPOSITION_KEYWORD_RE.search(head))


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
            # unnecessarily. The CL walk below will surface the missing
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
    PACER entry on two CL dockets has different ``id`` values. Within a
    ``(docket_number, court_id)`` group the PACER ``entry_number`` is the
    same, so it's the natural dedup key. For paperless minute orders that
    have no entry_number, falls back to ``(date_filed, description prefix)``
    — two CL dockets in the same group should agree on those fields for
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
    """Pool primary documents + dispositions across every CL docket in the group.

    CourtListener can split one logical PACER docket into multiple
    docket_id rows when the upstream ``pacer_case_id`` changed mid-life;
    each CL row carries a partial slice of the PACER entries. This helper
    calls :func:`find_primary_documents` per CL docket_id and unions the
    results, deduping with :func:`_logical_entry_dedup_key` so the same
    PACER entry (different CL ``id``, same PACER ``entry_number``) is only
    returned once.

    ``group_docket_ids`` should be ordered freshest-first (which
    :meth:`Store.get_docket_group_ids` already does via
    ``date_modified DESC``) so first-seen-wins prefers the most-recently-
    ingested CL row when a logical entry appears on multiple siblings.

    Exception: if a later sibling's copy of an already-seen entry carries
    populated ``plain_text`` on its main recap_document and the
    first-seen copy doesn't, the later copy wins. CourtListener can
    leave one CL row's recap_document with an empty ``plain_text``
    while another CL row in the same logical PACER group has the
    extracted body — without this upgrade the summary LLM would be
    fed an empty document. The us-v-schmitz indictment landed on the
    freshest CL row with no ``plain_text`` while the older sibling had
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
    two CL siblings' copies of the same logical PACER entry: a populated
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


def _entry_doc_text(entry: dict[str, Any], *, allow_ocr: bool = True) -> str:
    """Concatenate text from all recap_documents on an entry.

    Some entries (especially amended complaints) attach the primary
    document AND its exhibits. We pull text from each available main+attachment
    in order and return the concatenation. Exhibits add noise but are
    truncated downstream by the LLM's character budget anyway.
    """
    rds = entry.get("recap_documents") or []
    parts: list[str] = []
    for rd in rds:
        # Prefer the main document; only include attachments if the main
        # document is unavailable, to keep the input focused on the primary
        # text rather than exhibits.
        if rd.get("attachment_number"):
            continue
        text = pdf.extract_text(rd, allow_ocr=allow_ocr)
        if text:
            parts.append(text)
    if parts:
        return "\n\n".join(parts)
    # Fall back: if no main doc had text, try attachments.
    for rd in rds:
        if not rd.get("attachment_number"):
            continue
        text = pdf.extract_text(rd, allow_ocr=allow_ocr)
        if text:
            parts.append(text)
    return "\n\n".join(parts)


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

    Pins the operator-supplied ``extra.docket`` against any CL docket_id
    in the group — when a logical PACER docket is split across multiple
    CL siblings, an extra pinned to one of them applies to the group.
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
) -> list[dict[str, Any]]:
    """Pull primary-document text from any sibling docket on the same case.

    Stops at the first sibling that yields extractable primary text.
    Tags the returned entry descriptions with the sibling's docket number
    and court so the LLM prompt makes the cross-docket borrowing explicit
    — without that label, the model writes the summary as if the
    complaint had been filed in the primary docket (e.g. attributing a
    district-court complaint to the appellate court).
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
            sibling_primary, _ = find_primary_documents(cl, sibling_id, store=store)
        except Exception:
            log.exception(
                "summary: failed to scan sibling docket %s for primary documents",
                sibling_id,
            )
            continue
        borrowed = _attach_text(sibling_primary, allow_ocr=allow_ocr)
        if not borrowed:
            continue
        label_bits = [b for b in (sibling_docket_number, sibling_court_citation) if b]
        label = " ".join(label_bits) or f"docket {sibling_id}"
        for doc in borrowed:
            existing = doc.get("description") or "primary document"
            doc["description"] = f"{existing} [from sibling {label}]"
        return borrowed
    return []


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
    ``dockets`` table, then pools entries across every CL ``docket_id``
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
    }

    log.info(
        "summary: scanning %s (%s) for primary documents across %d CL docket(s) %s",
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
        # we still get a summary; this docket's own entries continue to
        # supply dispositions and the structured-events scaffold so the
        # framing stays appellate-perspective rather than collapsing into
        # the trial-court narrative.
        primary_documents = _borrow_primary_from_siblings(
            cl=cl,
            store=store,
            case=case,
            primary_docket_id=docket_id,
            allow_ocr=allow_ocr,
        )

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
            e["id"]
            for e in (primary + dispositions)
            if e.get("id") is not None
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
    # group, which is the freshest-modified CL row). The advisory walks
    # ~one page of entries on that docket; running it across every sibling
    # would multiply CL API calls without strengthening the signal — the
    # sealing order is on the PACER docket itself, not per-CL-row, so
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

    summary_text, model_id = llm.generate_docket_summary(
        case_name=case.name,
        aggregation_note=aggregation_note,
        docket=docket_for_prompt,
        primary_documents=primary_documents,
        disposition_documents=disposition_documents,
        extra_documents=extra_documents,
        hearings=hearings,
        deadlines=deadlines,
        sealing_advisory=sealing_advisory,
        provider=provider,
        model=model,
    )

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
    """Map the case's CL docket_ids onto logical PACER docket groups.

    Returns one ``(docket_number, court_id, canonical_docket_id)`` tuple
    per group. The canonical CL docket_id is the freshest one in the
    group (the head of :meth:`Store.get_docket_group_ids`), used as the
    representative for :func:`summarize_docket` calls. CL docket_ids
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
        # The canonical docket_id can be any CL row in the group — pass
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
    case's CL docket_ids by ``(docket_number, court_id)``, and for each
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

    CL docket_ids that resolve to the same ``(docket_number, court_id)``
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
