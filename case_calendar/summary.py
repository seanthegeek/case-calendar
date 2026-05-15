"""Per-docket case summary generation.

A separate, opt-in pass from the per-entry extractor. For each docket, we:

  1. Walk the docket via the CourtListener API to find the primary document
     (latest indictment / superseding indictment / information for criminal
     dockets; latest amended complaint / complaint / petition for civil).
  2. Look for any disposition documents (judgment, plea agreement, verdict,
     dismissal, dispositive memo/order) on the newest end of the docket.
  3. Pull PDF text for those entries through the existing pdf.py fallback
     chain (CL plain_text → IA mirror via pypdf → optional tesseract OCR).
  4. Feed the docs plus a structured-events scaffold (hearings/deadlines the
     extractor already recorded, with their statuses) to a higher-tier LLM
     (Sonnet by default) and persist the resulting prose to the
     ``case_summaries`` table.

The pipeline is intentionally independent of the cheap extractor pipeline:
primary documents rarely match the hearing-relevance regex, so we don't
have their text in the local entries table. Hitting CL directly here keeps
the extractor's storage shape unchanged.
"""

from __future__ import annotations

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

# Match the leading word(s) of an entry description. CL descriptions are
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
    # Civil — class action, removal, default, and injunctive relief.
    r"|class\s+certification"
    r"|remand(?:ed)?"
    r"|entry\s+of\s+default"
    r"|tros?"
    r"|temporary\s+restraining\s+orders?"
    r"|injunctions?"
    # Cross-domain — judgments, dismissals, appellate dispositions.
    r"|judg(?:e)?ments?"
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
           STIPULATED|CORRECTED|REDACTED|SEALED|UNSEALED|SUPERSEDING|
           INTERIM|TEMPORARY|PERMANENT)\s+
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
        # that CL clerks use for the procedural recording.
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
    cl: CourtListener, docket_id: int, *, order_by: str,
    page_size: int = 50, max_pages: int = 2,
) -> list[dict[str, Any]]:
    """Return up to ``page_size * max_pages`` entries on a docket in the given order.

    Uses CL's native order_by so we don't have to scan the whole docket
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


def find_primary_documents(
    cl: CourtListener, docket_id: int, *,
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
    from CL — `sync.process_entry` now persists description + plain_text
    for primary/disp matches alongside hearing-relevant ones precisely so this
    short-circuit lands. Falls back to CL for cold dockets (first ever
    sync on this docket or pre-fix data lacking primary/disp body text).
    """
    primary: list[dict[str, Any]] = []
    dispositions: list[dict[str, Any]] = []

    if store is not None:
        cached = store.get_entries_with_body(docket_id)
        for entry in cached:
            if is_primary_document(entry):
                primary.append(entry)
            elif _is_disposition_document(entry):
                dispositions.append(entry)
        # Short-circuit only when the cache yielded a primary document.
        # A primary-document hit is the strong signal that this docket
        # has been (re)synced under the post-fix code path that persists
        # primary/disp bodies — at which point we trust the rest of the cache
        # too. If only dispositions came back, the cache may be holding
        # post-fix judgment bodies alongside a pre-fix indictment stub
        # (NULL description) — exactly the us-v-chapman / us-v-mcgonigal
        # shape — and the summary pipeline needs CL to recover the
        # primary document text. Falling through to CL in this case
        # costs at most 3 docket-entries pages; the alternative is
        # silently skipping the docket with "no primary document text
        # could be extracted" even though CL has it.
        if primary:
            primary.sort(key=lambda e: e.get("date_filed") or "")
            dispositions.sort(key=lambda e: e.get("date_filed") or "")
            return primary, dispositions

    # Cold cache — fall back to CL. Prime documents are almost always at
    # entry #1 (criminal) or within the first few amended-complaint entries
    # (civil). Two pages oldest-first covers 99% of cases. For dispositions
    # we go three pages newest-first: in a heavily-briefed civil case, the
    # dispositive order (e.g. an order granting a preliminary injunction)
    # can be followed by months of compliance briefing, status reports, and
    # transcript orders that push it well past the first newest-first page.
    # The extra two requests are cheap; the alternative (missing the
    # disposition) makes the summary state the wrong posture.
    oldest = _list_entries_ordered(cl, docket_id, order_by="date_filed", max_pages=2)
    newest = _list_entries_ordered(cl, docket_id, order_by="-date_filed", max_pages=3)

    # Seed the dedup set with anything the local cache already supplied
    # (the disposition-only fallthrough case): we WANT to keep those
    # cached entries — their plain_text was harvested at sync time, so
    # pdf.extract_text short-circuits on them — and just augment with
    # whatever else CL turns up that the cache didn't see (typically the
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
        elif _is_disposition_document(entry):
            _dedup_add(dispositions, entry)

    primary.sort(key=lambda e: e.get("date_filed") or "")
    dispositions.sort(key=lambda e: e.get("date_filed") or "")
    return primary, dispositions


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
    cl: CourtListener, docket_id: int, *,
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
        for rd in (e.get("recap_documents") or []):
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
    entries: Iterable[dict[str, Any]], *, allow_ocr: bool = True,
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
            log.info(
                "summary: skipping entry %s (%s) — no PDF text extractable",
                entry.get("id"),
                _entry_description_head(entry)[:80],
            )
            continue
        out.append({
            "entry_id": entry.get("id"),
            "entry_number": entry.get("entry_number"),
            "description": _entry_description_head(entry),
            "date_filed": entry.get("date_filed"),
            "text": text,
        })
    return out


# ---------------------------------------------------------------------------
# Structured-events scaffold
# ---------------------------------------------------------------------------


def _hearings_for_docket(store: Store, case_id: str, docket_id: int) -> list[dict[str, Any]]:
    """Filter the case's hearings to a single docket for the LLM scaffold."""
    return [
        h for h in store.get_hearings(case_id)
        if h.get("docket_id") == docket_id
    ]


def _deadlines_for_docket(store: Store, case_id: str, docket_id: int) -> list[dict[str, Any]]:
    return [
        d for d in store.get_deadlines(case_id)
        if d.get("docket_id") == docket_id
    ]


def _fetch_extra_documents(
    case: CaseConfig, docket_id: int, *, allow_ocr: bool = True,
) -> list[dict[str, Any]]:
    """Fetch operator-provided ``extra_documents`` for one docket.

    Returns one flat list of doc dicts, each carrying ``source_url`` (the
    LLM prompt's provenance line) and ``operator_note`` (the trusted
    operator-supplied description that tells the LLM what the document is
    and why it was added). Documents that fail to fetch / extract are
    logged and dropped so the rest of the summary pipeline still gets to
    run on whatever CL did surface. The summary LLM sees these in their
    own "EXTRA DOCUMENTS PROVIDED BY OPERATOR" section, distinct from the
    primary-document and disposition slots that the CL-walk fills.
    """
    extras = getattr(case, "extra_documents", None) or []
    out: list[dict[str, Any]] = []
    for extra in extras:
        if extra.docket != docket_id:
            continue
        log.info(
            "summary: docket %s — fetching operator-provided document from %s",
            docket_id, extra.url,
        )
        text = pdf.extract_text_from_url(extra.url, allow_ocr=allow_ocr)
        if not text:
            log.warning(
                "summary: docket %s — operator-provided document %s yielded no text; skipping",
                docket_id, extra.url,
            )
            continue
        out.append({
            "entry_id": None,
            "entry_number": None,
            "description": "operator-provided document",
            "date_filed": None,
            "text": text,
            "source_url": extra.url,
            "operator_note": extra.note,
        })
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
            store.get_court_citation(meta["court_id"])
            if meta.get("court_id") else None
        )
        log.info(
            "summary: docket %s has no primary document — borrowing from sibling %s (%s)",
            primary_docket_id, sibling_id, sibling_docket_number,
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
    """Generate, persist, and return the summary row for one docket.

    Returns ``None`` when no primary-document text is available — we
    don't want to hallucinate a summary from the docket metadata alone.
    """
    meta = store.get_docket_meta(docket_id) or {}
    docket_number = meta.get("docket_number")
    court_id = meta.get("court_id")
    docket_for_prompt = {
        "docket_id": docket_id,
        "docket_number": docket_number,
        "court_id": court_id,
        "court_citation": (
            store.get_court_citation(court_id) if court_id else None
        ),
        "court_tz": tz_for(court_id) if court_id else None,
    }

    log.info(
        "summary: scanning docket %s (%s) for primary documents",
        docket_id, docket_number,
    )
    primary, dispositions = find_primary_documents(cl, docket_id, store=store)
    log.info(
        "summary: docket %s found %d primary document(s), %d disposition document(s)",
        docket_id, len(primary), len(dispositions),
    )

    primary_documents = _attach_text(primary, allow_ocr=allow_ocr)
    disposition_documents = _attach_text(
        dispositions, allow_ocr=allow_ocr, allow_description_fallback=True,
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
                docket_id, doc.get("entry_number"), doc.get("date_filed"),
                len(text),
            )

    # Fetch any operator-supplied extra_documents for this docket. These
    # don't slot into primary/disposition — they ride into the LLM
    # prompt as their own block, each carrying the operator's note
    # describing what it is.
    extra_documents = _fetch_extra_documents(case, docket_id, allow_ocr=allow_ocr)

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
            cl=cl, store=store, case=case,
            primary_docket_id=docket_id, allow_ocr=allow_ocr,
        )

    if not primary_documents and not extra_documents:
        log.warning(
            "summary: skipping docket %s — no primary document text could be extracted",
            docket_id,
        )
        return None

    hearings = _hearings_for_docket(store, case.case_id, docket_id)
    deadlines = _deadlines_for_docket(store, case.case_id, docket_id)

    sealing_advisory = detect_sealing(cl, docket_id, dispositions=dispositions)
    if sealing_advisory is not None:
        log.info(
            "summary: docket %s — sealing advisory: order at entry #%s "
            "(filed %s), %d post-seal available entries observed",
            docket_id,
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
            docket_id, len(primary_documents), len(disposition_documents), len(extra_documents),
        )

    source_ids = [
        d["entry_id"] for d in primary_documents + disposition_documents
        if d.get("entry_id")
    ]
    store.upsert_case_summary(
        case.case_id, docket_id,
        summary=summary_text,
        model=model_id,
        source_entry_ids=source_ids,
    )

    log.info(
        "summary: wrote docket %s summary (%d chars, model=%s)",
        docket_id, len(summary_text), model_id,
    )
    return {
        "docket_id": docket_id,
        "summary": summary_text,
        "model": model_id,
        "source_entry_ids": source_ids,
    }


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
) -> dict[str, set[int]]:
    """Regenerate any summaries that are missing or marked stale.

    Walks ``cases`` (the parsed CaseConfig list from cli) and checks every
    docket against :meth:`Store.is_summary_stale`; for each stale docket,
    calls :func:`summarize_docket`. Returns ``{case_id: {docket_id, ...}}``
    for the rows that were (re)written so callers can scope the resulting
    emit to the affected calendars.

    This is the agentic path: ``sync.process_entry`` flips ``stale=1`` when
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
    written: dict[str, set[int]] = {}
    for case in cases:
        if only_case_ids is not None and case.case_id not in only_case_ids:
            continue
        aggregation_note = (case_overrides.get(case.case_id) or {}).get("aggregation_note")
        for docket_id in case.dockets:
            if not force and not store.is_summary_stale(case.case_id, docket_id):
                continue
            log.info(
                "summary: docket %s (case %s) %s — regenerating",
                docket_id, case.case_id,
                "force-refresh" if force else "is stale or missing",
            )
            row = summarize_docket(
                cl=cl, store=store, case=case, docket_id=docket_id,
                aggregation_note=aggregation_note,
                provider=provider, model=model, allow_ocr=allow_ocr,
            )
            if row:
                written.setdefault(case.case_id, set()).add(docket_id)
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
    """Summarize every docket on a case.

    With ``force=False`` (the default), dockets that already have a summary
    row are skipped — primary documents are stable, so existing summaries
    are still valid. Pass ``force=True`` after a model upgrade or prompt
    change to overwrite.
    """
    out: list[dict[str, Any]] = []
    for docket_id in case.dockets:
        if not force and store.get_docket_summary(case.case_id, docket_id):
            log.info(
                "summary: skipping docket %s (case %s) — already summarized, "
                "pass force=True to overwrite",
                docket_id, case.case_id,
            )
            continue
        row = summarize_docket(
            cl=cl, store=store, case=case, docket_id=docket_id,
            aggregation_note=aggregation_note,
            provider=provider, model=model, allow_ocr=allow_ocr,
        )
        if row:
            out.append(row)
    return out
