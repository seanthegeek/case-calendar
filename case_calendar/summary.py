"""Per-docket case summary generation.

A separate, opt-in pass from the per-entry extractor. For each docket, we:

  1. Walk the docket via the CourtListener API to find the operative pleading
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
operative pleadings rarely match the hearing-relevance regex, so we don't
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
    # call ``is_operative_pleading`` / ``is_disposition`` on each entry.
    # The runtime import would form a cycle, but ``from __future__ import
    # annotations`` (above) defers signature evaluation to strings so we
    # only need it for type checkers.
    from .sync import CaseConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Operative-document detection
# ---------------------------------------------------------------------------

# Match the leading word(s) of an entry description. CL descriptions are
# typically uppercase or title-case sentences. We anchor at the start to
# avoid catching mentions inside other entries ("Response to Motion to
# Dismiss the Indictment" should NOT match the operative-pleading pattern).
# The optional "amended / superseding" qualifier captures the latest-wins
# logic naturally: each variant gets its own entry, and we sort by
# entry_number descending when picking.
_OPERATIVE_PLEADING_RE = re.compile(
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


def is_operative_pleading(entry: dict[str, Any]) -> bool:
    return bool(_OPERATIVE_PLEADING_RE.match(_entry_description_head(entry)))


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
    operative pleadings on nearly every docket (entries 1-50 oldest-first)
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


def find_operative_documents(
    cl: CourtListener, docket_id: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Find the operative pleading(s) and disposition documents on a docket.

    Returns ``(operative_pleadings, dispositions)``. Both lists are sorted
    chronologically (oldest first); callers pick the latest operative
    pleading for the LLM (superseding indictment beats original) and pass
    all dispositions through (each adds context).
    """
    # Operative pleadings are almost always at entry #1 (criminal) or within
    # the first few amended-complaint entries (civil). Two pages of 50
    # oldest-first covers 99% of cases. Dispositions on concluded cases are
    # near the newest end; one page newest-first is enough.
    oldest = _list_entries_ordered(cl, docket_id, order_by="date_filed", max_pages=2)
    newest = _list_entries_ordered(cl, docket_id, order_by="-date_filed", max_pages=1)

    # Operative pleadings are almost always on the oldest end; dispositions
    # on the newest. But scan both for resilience — an amended complaint can
    # land halfway through a long-running case, and a clerk's entry for an
    # original complaint can occasionally show up out of order.
    seen_ids: set[int] = set()

    def _dedup_add(target: list[dict[str, Any]], entry: dict[str, Any]) -> None:
        eid = entry.get("id")
        if eid is None or eid in seen_ids:
            return
        seen_ids.add(eid)
        target.append(entry)

    operative: list[dict[str, Any]] = []
    dispositions: list[dict[str, Any]] = []
    for entry in oldest + newest:
        if is_operative_pleading(entry):
            _dedup_add(operative, entry)
        elif is_disposition(entry):
            _dedup_add(dispositions, entry)

    operative.sort(key=lambda e: e.get("date_filed") or "")
    dispositions.sort(key=lambda e: e.get("date_filed") or "")
    return operative, dispositions


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------


def _entry_doc_text(entry: dict[str, Any], *, allow_ocr: bool = True) -> str:
    """Concatenate text from all recap_documents on an entry.

    Some entries (especially amended complaints) attach the operative
    document AND its exhibits. We pull text from each available main+attachment
    in order and return the concatenation. Exhibits add noise but are
    truncated downstream by the LLM's character budget anyway.
    """
    rds = entry.get("recap_documents") or []
    parts: list[str] = []
    for rd in rds:
        # Prefer the main document; only include attachments if the main
        # document is unavailable, to keep the input focused on the operative
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
) -> list[dict[str, Any]]:
    """Pull PDF text for each entry; drop entries with no extractable text."""
    out: list[dict[str, Any]] = []
    for entry in entries:
        text = _entry_doc_text(entry, allow_ocr=allow_ocr)
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


def _borrow_operative_from_siblings(
    *,
    cl: CourtListener,
    store: Store,
    case: CaseConfig,
    primary_docket_id: int,
    allow_ocr: bool = True,
) -> list[dict[str, Any]]:
    """Pull operative-pleading text from any sibling docket on the same case.

    Stops at the first sibling that yields extractable operative text.
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
            "summary: docket %s has no operative pleading — borrowing from sibling %s (%s)",
            primary_docket_id, sibling_id, sibling_docket_number,
        )
        try:
            sibling_operative, _ = find_operative_documents(cl, sibling_id)
        except Exception:
            log.exception(
                "summary: failed to scan sibling docket %s for operative documents",
                sibling_id,
            )
            continue
        borrowed = _attach_text(sibling_operative, allow_ocr=allow_ocr)
        if not borrowed:
            continue
        label_bits = [b for b in (sibling_docket_number, sibling_court_citation) if b]
        label = " ".join(label_bits) or f"docket {sibling_id}"
        for doc in borrowed:
            existing = doc.get("description") or "operative pleading"
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

    Returns ``None`` when no operative-pleading text is available — we
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
        "summary: scanning docket %s (%s) for operative documents",
        docket_id, docket_number,
    )
    operative, dispositions = find_operative_documents(cl, docket_id)
    log.info(
        "summary: docket %s found %d operative pleading(s), %d disposition doc(s)",
        docket_id, len(operative), len(dispositions),
    )

    operative_docs = _attach_text(operative, allow_ocr=allow_ocr)
    disposition_docs = _attach_text(dispositions, allow_ocr=allow_ocr)

    if not operative_docs and len(case.dockets) > 1:
        # Appellate dockets (and parallel filings that pivot off a sibling)
        # don't re-file their own complaint — the opener is a clerical
        # "case opened, notice of appeal from <lower court>" entry, which
        # the operative-pleading regex correctly refuses to match. Borrow
        # the operative text from a sibling docket on the same case_id so
        # we still get a summary; this docket's own entries continue to
        # supply dispositions and the structured-events scaffold so the
        # framing stays appellate-perspective rather than collapsing into
        # the trial-court narrative.
        operative_docs = _borrow_operative_from_siblings(
            cl=cl, store=store, case=case,
            primary_docket_id=docket_id, allow_ocr=allow_ocr,
        )

    if not operative_docs:
        log.warning(
            "summary: skipping docket %s — no operative pleading text could be extracted",
            docket_id,
        )
        return None

    hearings = _hearings_for_docket(store, case.case_id, docket_id)
    deadlines = _deadlines_for_docket(store, case.case_id, docket_id)

    summary_text, model_id = llm.generate_docket_summary(
        case_name=case.name,
        aggregation_note=aggregation_note,
        docket=docket_for_prompt,
        operative_docs=operative_docs,
        disposition_docs=disposition_docs,
        hearings=hearings,
        deadlines=deadlines,
        provider=provider,
        model=model,
    )

    source_ids = [d["entry_id"] for d in operative_docs + disposition_docs if d.get("entry_id")]
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
) -> dict[str, set[int]]:
    """Regenerate any summaries that are missing or marked stale.

    Walks ``cases`` (the parsed CaseConfig list from cli) and checks every
    docket against :meth:`Store.is_summary_stale`; for each stale docket,
    calls :func:`summarize_docket`. Returns ``{case_id: {docket_id, ...}}``
    for the rows that were (re)written so callers can scope the resulting
    emit to the affected calendars.

    This is the agentic path: ``sync.process_entry`` flips ``stale=1`` when
    it sees an operative-pleading or disposition entry, and ``cmd_sync`` /
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
            if not store.is_summary_stale(case.case_id, docket_id):
                continue
            log.info(
                "summary: docket %s (case %s) is stale or missing — regenerating",
                docket_id, case.case_id,
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
    row are skipped — operative pleadings are stable, so existing summaries
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
