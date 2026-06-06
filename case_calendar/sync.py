"""Per-case sync logic.

For each case we:
  1. Pull docket entries newer than the last seen ``date_modified``.
  2. Run the keyword pre-filter; skip irrelevant entries cheaply.
  3. For relevant entries, optionally pull the linked PDF plain-text from the
     RECAP API.
  4. Hand entry + known hearings to the LLM extractor.
  5. Apply the returned actions to the SQLite hearing store.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import llm, pdf, summary as summary_mod, url_validator
from .courtlistener import CourtListener
from .courts import tz_for
from .extractor import is_extractable
from .store import Store, compact_recap_documents

log = logging.getLogger(__name__)

# Verify pass: how far on either side of a hearing's own date to look for the
# entry that records its outcome (minute entry / verdict / transcript /
# judgment). The lookback absorbs the timezone skew between a hearing's UTC
# timestamp and a minute entry's court-local filing date; the forward window
# covers a judgment that lands days-to-weeks after a sentencing or trial.
_VERIFY_OUTCOME_LOOKBACK_DAYS = 2
_VERIFY_OUTCOME_WINDOW_DAYS = 45


@dataclass
class ExtraDocument:
    """A document the operator points the case-summary pipeline at directly.

    The case-summary pipeline normally finds primary documents + dispositions
    on each docket by walking CourtListener. When CourtListener / PACER is missing a
    document the public should be able to see — e.g. an indictment that was
    ordered unsealed but where entries 1-4 still show as missing in the API
    (see CourtListener bug #7345) — the operator lists the out-of-band URL
    here and the summary pipeline feeds the text to the LLM as a separate
    "extra documents" block, alongside whatever the normal pipeline found.

    The required ``note`` is trusted operator-supplied context: it describes
    what the document IS (e.g. "the unsealed indictment in S.D. Tex.
    4:23-cr-00523") and any necessary caveats (e.g. "bears SEALED stamps
    because it was filed under seal, but the seal has since been lifted").
    The note carries the meaning that a structural ``role`` field couldn't —
    real documents don't always slot cleanly into "pleading" vs
    "disposition", and the operator's natural-language description of what
    the document is and why it was added is the right primary signal.
    """

    docket: int  # which docket this document belongs to
    url: str  # PDF URL to fetch
    note: str  # required — trusted operator context shown to the LLM


@dataclass
class CaseConfig:
    case_id: str  # stable ID used as primary key in the store
    name: str  # human title
    dockets: list[int]
    calendar: str  # which output calendar this case belongs to

    extra_documents: list[ExtraDocument] = field(default_factory=list)
    """Out-of-band documents to feed into the case-summary LLM as a
    distinct supplementary block. Empty by default. Each entry is scoped
    to one ``docket`` id on this case and carries a required ``note``
    that describes what the document is and why it was added."""

    tags: list[str] = field(default_factory=list)
    """Topical labels (e.g. ``DPRK``, ``PRC``, ``ransomware``) used to
    group cases by theme. Rendered on each calendar event's description
    AND on the HTML index, where they double as click-to-filter chips
    against the global search."""


def fingerprint_entry(entry: dict[str, Any]) -> str:
    """Hash that changes when meaningful entry state changes.

    We include PDF availability and presence of extracted text so that an
    entry whose PDF was missing on a prior sync is re-processed once the
    PDF (or its OCR text) shows up.
    """
    parts = [
        entry.get("description") or "",
        entry.get("short_description") or "",
        entry.get("date_filed") or "",
    ]
    for rd in entry.get("recap_documents", []) or []:
        parts.append(rd.get("description") or "")
        parts.append(str(bool(rd.get("is_available"))))
        parts.append(str(bool(rd.get("is_sealed"))))
        parts.append("1" if (rd.get("plain_text") or "").strip() else "0")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


# A pulled PDF is worth the cost when the on-docket text doesn't include the
# specifics. We only fetch when the entry looks like a hearing notice but its
# description is essentially empty.
_DETAIL_HINTS = re.compile(
    r"\b(\d{1,2}:\d{2}\s*(AM|PM)?|courtroom|judge|via\s+(zoom|teleconf|video))",
    re.IGNORECASE,
)

# CourtListener appends a clerk-side timestamp like "[Entered: 05/06/2026 01:51 PM]" or
# "(Entered: 05/06/2026)" to most docket-entry descriptions. The HH:MM there
# is when the clerk filed it, NEVER the hearing time. Without stripping it,
# _DETAIL_HINTS matches on every such entry and we skip the PDF that does
# carry the actual hearing time.
_ENTERED_FOOTER = re.compile(r"[\(\[]Entered:[^\)\]]*[\)\]]", re.IGNORECASE)

# Orders granting a *scheduling/hearing* motion are a trap: the brief
# description references the underlying motion only by docket position, so
# the substance — what kind of hearing was being requested — lives in the
# motion entry and the order's PDF, not in the order's text. Even when the
# order inlines a date+time (which would otherwise let _DETAIL_HINTS
# short-circuit the PDF fetch), we still need the PDF body because it
# typically lists ALL the dates the order set, including ones not echoed
# in the brief description (e.g. the CIPA conference itself).
#
# Limited to scheduling/hearing motions on purpose. Orders granting
# substantive motions ("granting 50 Motion to Suppress / Dismiss / Compel")
# don't move the docket and don't justify the extra LLM tokens.
_ORDER_GRANTS_SCHEDULING_MOTION = re.compile(
    r"\bgranting\b[^.]{0,80}?\bMotion\s+"
    r"(?:for\s+Hearing|for\s+Continuance|for\s+Status\s+Conference|"
    r"to\s+(?:Continue|Reschedule|Vacate|Set|Schedule|Adjourn))",
    re.IGNORECASE,
)

# Cross-reference pattern: PACER-style "ORDER granting 65 Motion ..." or
# "DENYING 42 Motion" or just "see [12]". The verb tells us this is a
# reference to another docket entry we may have already seen; the bare
# number is the docket-position number (entries.entry_number). We only
# resolve refs we've already stored, so this is purely a context boost
# at the LLM call — no extra CourtListener traffic.
_DOCKET_REF = re.compile(
    r"\b(?:granting|denying|grants|denies|granted|denied|ruling\s+on|"
    r"see|re|response\s+to)\s+(?:in\s+part\s+)?(?:\[)?(\d{1,4})(?:\])?\b",
    re.IGNORECASE,
)


def _extract_docket_refs(entry: dict[str, Any]) -> list[int]:
    """Pull docket-position numbers referenced by this entry's description.

    Returns a deduplicated list of integers. We strip the "(Entered: ...)"
    footer first so a clerk timestamp's day-of-month doesn't get parsed as
    a referenced motion.
    """
    desc = (
        (entry.get("description") or "") + " " + (entry.get("short_description") or "")
    )
    desc = _ENTERED_FOOTER.sub("", desc)
    seen: list[int] = []
    for m in _DOCKET_REF.finditer(desc):
        n = int(m.group(1))
        if n not in seen:
            seen.append(n)
    return seen


def _needs_pdf(entry: dict[str, Any]) -> bool:
    desc = (
        (entry.get("description") or "") + " " + (entry.get("short_description") or "")
    )
    desc = _ENTERED_FOOTER.sub("", desc)
    if _ORDER_GRANTS_SCHEDULING_MOTION.search(desc):
        return True
    if _DETAIL_HINTS.search(desc):
        return False
    # No specific details inline — go fetch the PDFs.
    return True


def _is_fetchable(rd: dict[str, Any]) -> bool:
    """True if this recap_document points at a real PDF we could pull text from.

    Paperless orders, minute entries, and entries whose document hasn't been
    contributed to RECAP yet all show up as recap_documents with
    ``is_available: false`` and no ``filepath_local`` / ``filepath_ia``.
    They have no body to fetch — only a description in the docket text — so
    we should never try to download them.
    """
    if rd.get("is_sealed"):
        return False
    # CourtListener-extracted plain_text is itself a fetchable source.
    if (rd.get("plain_text") or "").strip():
        return True
    if not rd.get("is_available"):
        return False
    return bool(rd.get("filepath_local") or rd.get("filepath_ia"))


def is_pending_enrichment(entry: dict[str, Any]) -> bool:
    """True if the entry has no readable body yet but a document that could
    still arrive — the placeholder shape a docket-alert webhook delivers at
    entry creation.

    Concretely: the entry's own ``description`` is empty AND it carries at
    least one unsealed ``recap_document`` whose text we don't have yet
    (``is_available`` false, ``plain_text`` empty). The substance lives in a
    document CourtListener hasn't made available, so re-fetching later —
    once the upload / text extraction lands — is what surfaces it. This is
    the us-v-kejia-wang #42 shape: an endorsed order delivered as a stub
    (``description=""``, an ``is_available=False`` doc) whose text only
    appeared upstream afterward, with no second webhook to deliver it
    (CourtListener re-fires an *email* alert on such an update but not a
    webhook — reported upstream as CourtListener issue #7423).

    Deliberately tight. An entry whose ``description`` already carries the
    order text (a paperless electronic order) is NOT pending — its
    recap_document may be permanently ``is_available=False`` and there is
    nothing to enrich. The empty-body guard is exactly what separates the
    two, and without it the predicate matches every paperless order ever
    stored. The age cutoff that bounds the reconcile sweep lives in the
    caller (``filed_after``), not here.
    """
    if (entry.get("description") or "").strip():
        return False
    for rd in entry.get("recap_documents") or []:
        if rd.get("is_sealed"):
            continue
        if rd.get("is_available"):
            continue
        if (rd.get("plain_text") or "").strip():
            continue
        return True
    return False


def _validate_action_dial_in(action: dict[str, Any]) -> None:
    """Verify action.dial_in resolves; on failure move it to notes.

    LLM URL extraction occasionally swallows trailing prose into the URL when
    the source text has no separator. We do a one-step parent-path repair;
    if that fails too, we keep the broken text accessible to the human reader
    by appending it to ``notes`` and clearing ``dial_in``.
    """
    original = (action.get("dial_in") or "").strip()
    if not original:
        return
    repaired = url_validator.validate_url(original)
    if repaired == original:
        return
    if repaired:
        action["dial_in"] = repaired
        return
    # Validation failed entirely — preserve the URL text in notes so a human
    # can salvage it.
    addendum = f"Dial-in (unverified): {original}"
    existing_notes = action.get("notes")
    action["notes"] = f"{existing_notes}\n\n{addendum}" if existing_notes else addendum
    action["dial_in"] = None


# Action-category coercion mappings. The LLM occasionally picks a hearing
# action type for a deadline-shaped payload (e.g. ``UPDATE_DETAILS`` with
# ``deadline_key`` on the us-v-ding 2025-07-11 government status-report
# reiteration) or vice versa. The KEY is the more specific signal — the
# model had to know about that exact row to use its key — so we trust the
# key and coerce the type to its other-category equivalent. There's no
# ``UPDATE_DETAILS_DEADLINE``: deadlines have a much simpler shape (no
# judge, courtroom, dial-in to update), so a RESCHEDULE_DEADLINE with the
# same date covers the same intent.
_HEARING_TO_DEADLINE_TYPE: dict[str, str] = {
    "ADD_HEARING": "ADD_DEADLINE",
    "RESCHEDULE_HEARING": "RESCHEDULE_DEADLINE",
    "UPDATE_DETAILS": "RESCHEDULE_DEADLINE",
    "CANCEL_HEARING": "CANCEL_DEADLINE",
    "MARK_HELD": "MARK_FILED",
}
_DEADLINE_TO_HEARING_TYPE: dict[str, str] = {
    "ADD_DEADLINE": "ADD_HEARING",
    "RESCHEDULE_DEADLINE": "RESCHEDULE_HEARING",
    "CANCEL_DEADLINE": "CANCEL_HEARING",
    "MARK_FILED": "MARK_HELD",
}


def _normalize_action_category(action: dict[str, Any]) -> dict[str, Any]:
    """Coerce hearing/deadline type-vs-key mismatches.

    The LLM sometimes picks an action type for the wrong category but
    still uses the right kind of KEY for the row it meant to touch. We
    trust the key (it's specific evidence the model knew about that
    exact row) and rewrite the type to match. Returns a NEW dict when
    a coercion fires so the caller's action list isn't mutated under
    them; returns the input unchanged otherwise. Logs at INFO so the
    prompt-violation rate stays visible without spamming WARN.

    Pairs with the deadline portion of ``llm.SYSTEM_PROMPT`` telling the
    model that ``UPDATE_DETAILS`` is hearings-only — this is the belt-and-
    suspenders downstream.
    """
    atype = (action.get("type") or "").upper()
    if not atype:
        return action
    has_hearing_key = bool(action.get("hearing_key"))
    has_deadline_key = bool(action.get("deadline_key"))

    if atype in _HEARING_TO_DEADLINE_TYPE and has_deadline_key and not has_hearing_key:
        coerced = _HEARING_TO_DEADLINE_TYPE[atype]
        log.info(
            "coerced hearing action %s -> %s (had deadline_key=%r, no hearing_key)",
            atype,
            coerced,
            action.get("deadline_key"),
        )
        out = dict(action)
        out["type"] = coerced
        return out

    if atype in _DEADLINE_TO_HEARING_TYPE and has_hearing_key and not has_deadline_key:
        coerced = _DEADLINE_TO_HEARING_TYPE[atype]
        log.info(
            "coerced deadline action %s -> %s (had hearing_key=%r, no deadline_key)",
            atype,
            coerced,
            action.get("hearing_key"),
        )
        out = dict(action)
        out["type"] = coerced
        return out

    return action


def _local_to_utc(
    date_str: Optional[str], time_str: Optional[str], tz: str
) -> Optional[str]:
    # Some LLMs emit the literal string ``"null"`` / ``"None"`` for a missing
    # date instead of JSON null — observed on a conditional ADD_DEADLINE where
    # the model wrote ``"local_date": "null"`` and crashed the column with
    # ``ValueError: Invalid isoformat string: 'nullT16:00'`` (the date-side
    # twin of the ``local_time`` "null" case handled just below). Treat those
    # as a missing date so the row is stored date-less — the same end state as
    # a conditional deadline — rather than letting them reach ``fromisoformat``.
    if not date_str or date_str.strip().lower() in ("null", "none"):
        return None
    # The schema says ``local_time`` is ``"HH:MM" | null`` but some LLMs
    # occasionally emit the literal string ``"null"`` (or ``"None"``) when
    # they would otherwise emit no time — gpt-5.4-mini did this on a
    # verify-pass response and crashed the column with
    # ``ValueError: Invalid isoformat string: '2026-01-07Tnull'``.
    # Treat those as no-time (date-only) rather than letting them through
    # to ``fromisoformat``.
    if time_str and time_str.strip().lower() in ("null", "none", ""):
        time_str = None
    # The model can emit a syntactically-plausible but calendar-INVALID date or
    # time — e.g. "2023-03-34" (day out of range), which crashed a real sync via
    # an uncaught ``ValueError: day is out of range for month`` out of
    # ``_apply_deadline_action`` (the build-comparison replay only survived
    # because it wraps each entry in its own try/except). Parse defensively: on a
    # bad time, fall back to date-only (midnight local — the calendar layer turns
    # this into an all-day event); on a bad date, store the row date-less (the
    # same end state as the null-date case above) rather than letting the
    # exception abort the case sync or 500 a webhook on every retry.
    iso = f"{date_str}T{time_str}" if time_str else f"{date_str}T00:00"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        if time_str:
            try:
                dt = datetime.fromisoformat(f"{date_str}T00:00")
            except ValueError:
                log.warning(
                    "unparseable local date %r (time %r); storing date-less",
                    date_str,
                    time_str,
                )
                return None
            log.warning(
                "unparseable local time %r for date %r; storing date-only",
                time_str,
                date_str,
            )
        else:
            log.warning("unparseable local date %r; storing date-less", date_str)
            return None
    dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(timezone.utc).isoformat()


# Filing deadlines without an explicit clock time fire at 4 PM court time —
# the hour most federal clerk's offices close (the sampled districts close at
# 4:00 PM almost uniformly; e-filing itself runs to midnight under FRCP
# 6(a)(4)). A 4 PM anchor lands the reminder as close to when a filing would
# actually hit the docket as possible, so the watcher can check PACER right
# then (and still has the evening to follow up if it isn't on RECAP yet)
# rather than getting a midnight alert nobody acts on.
DEADLINE_DEFAULT_LOCAL_TIME = "16:00"


def _deadline_local_to_utc(
    date_str: Optional[str], time_str: Optional[str], tz: str
) -> Optional[str]:
    """Same as _local_to_utc but defaults missing times to 16:00 court-local
    rather than midnight. Used by the deadline path so the stored UTC
    timestamp already reflects the clerk's-office-close anchor."""
    # Normalize the same way ``_local_to_utc`` does — the literal
    # ``"null"`` / ``"None"`` strings are treated as missing so the
    # 16:00 default fires instead of crashing in ``fromisoformat``.
    if time_str and time_str.strip().lower() in ("null", "none", ""):
        time_str = None
    return _local_to_utc(date_str, time_str or DEADLINE_DEFAULT_LOCAL_TIME, tz)


def _append_audit_line(
    existing_audit: Optional[str],
    source: str,
    note: str,
) -> str:
    """Append a new ``[<source>]`` audit line to a row's existing audit_notes.

    ``source`` is the writer tag ("verify-pass" or "dedupe") that lets a
    future reader (or a future migration) tell which audit pass wrote the
    line. Audit paragraphs are separated by blank lines so they stay
    readable when concatenated over multiple sync runs. Note: this column
    is NEVER fed back to any LLM call — see the verify-pass user message
    builders in ``llm.py``.
    """
    line = f"[{source}] {note}".strip()
    if not existing_audit:
        return line
    return f"{existing_audit.rstrip()}\n\n{line}"


def _mark_held_date_matches(
    action: dict[str, Any], existing: dict[str, Any], tolerance_days: int = 2
) -> bool:
    """True if a MARK_HELD action's date is close enough to the existing hearing.

    Returns True when:
    - the action carries no local_date (older LLM responses; trust the match)
    - the existing hearing has no starts_at_utc (we have no date to compare)
    - the dates are within ``tolerance_days`` of each other

    A 2-day window covers same-week reschedules where the minute entry might
    be filed a day or two after the hearing; anything wider is almost certainly
    the LLM stapling a held proceeding onto the wrong logical hearing.
    """
    action_date_str = action.get("local_date")
    existing_starts = existing.get("starts_at_utc")
    if not action_date_str or not existing_starts:
        return True
    try:
        from datetime import date

        existing_date = datetime.fromisoformat(existing_starts).date()
        action_date = date.fromisoformat(action_date_str)
    except (ValueError, TypeError):
        return True
    return abs((existing_date - action_date).days) <= tolerance_days


# A docket entry that RECORDS a proceeding that already happened — a minute
# entry / minute order / "minutes of", an electronic clerk's-notes entry, or a
# transcript "of proceedings held" — is the authoritative account of what
# occurred at the hearing. A PRE-HEARING administrative notice (clerk's notice
# of Zoom access / courtroom change / scheduling) sets the hearing up but says
# nothing about what happened at it. The regexes below let the notes-selection
# rules prefer the proceeding record over the setup notice. Without this,
# MARK_HELD preserved whatever notes were last written (often the setup notice)
# and a dedupe MERGE_INTO kept the canonical row's notes regardless of which
# sibling actually described the proceeding — so a calendar description could
# freeze at "Clerk's notice providing Zoom access information" even after the
# docket recorded the argument (the anthropic-v-dow pi-motion-hearing
# regression).
#
# Two false-positive traps the patterns deliberately avoid:
#   - The standard clerk's-notice Zoom boilerplate ("...access to court
#     proceedings held BY telephone or videoconference are reminded...") would
#     match a bare "proceedings held", wrongly tagging a scheduling notice as a
#     record. So the "proceedings held" alternative requires "held BEFORE" or
#     "held ON <date>" — a record's phrasing, not the boilerplate's "held by".
#   - A bare "transcript order" / "order for transcript" is a private purchase
#     request, not a record, so the transcript alternative requires the
#     "...proceedings held" tail.
_PROCEEDING_RECORD_RE = re.compile(
    r"minute entry"
    r"|minute order"
    r"|minutes of"
    r"|clerk'?s? notes"
    r"|transcript\s+of\s+(?:[a-z ]*?\s)?proceedings?\s+held"
    r"|proceedings?\s+(?:were\s+)?held\s+(?:before|on)\b"
    r"|held\s+before"
    r"|stated\s+appearances"
    r"|under\s+submission",
    re.IGNORECASE,
)

# A transcript FILING also records a proceeding, but its text is filing
# logistics (court reporter, redaction-request and release deadlines), so it
# ranks BELOW a substantive minute-entry/clerk's-notes record when both are
# available for the same hearing — see _proceeding_record_rank.
_TRANSCRIPT_RECORD_RE = re.compile(
    r"transcript\s+of\s+(?:[a-z ]*?\s)?proceedings?\s+held",
    re.IGNORECASE,
)

_ADMIN_NOTICE_RE = re.compile(
    r"clerk'?s? notice"
    r"|notice\s+(?:re|of)\b"
    r"|zoom\s+(?:access|webinar|information|link)"
    r"|providing\s+zoom"
    r"|courtroom\s+change"
    r"|audio\s+observation"
    r"|access\s+information",
    re.IGNORECASE,
)


def _describes_proceeding(text: Optional[str]) -> bool:
    """True when ``text`` reads like the RECORD of a held proceeding."""
    return bool(text) and bool(_PROCEEDING_RECORD_RE.search(text))


def _proceeding_record_rank(text: Optional[str]) -> int:
    """Selection rank among proceeding records — lower is better.

    0 = a substantive account (minute entry / clerk's notes), 1 = a transcript
    filing (whose text is filing logistics). When a hearing has both kinds of
    record among its sources, the substantive one is the better description, so
    callers sort by ``(rank, -length)``.
    """
    return 1 if (text and _TRANSCRIPT_RECORD_RE.search(text)) else 0


def _is_admin_notice(text: Optional[str]) -> bool:
    """True when ``text`` reads like a pre-hearing administrative notice.

    A proceeding record can itself mention "clerk's notes", so the record
    check wins — text that describes what happened is never treated as a mere
    setup notice.
    """
    return (
        bool(text)
        and bool(_ADMIN_NOTICE_RE.search(text))
        and not _describes_proceeding(text)
    )


def _entry_records_proceeding(entry: dict[str, Any]) -> bool:
    """True when the docket ENTRY is the record of a held proceeding.

    Covers minute entries, electronic clerk's notes, and transcripts of
    proceedings held — the entries whose own text describes what happened at a
    hearing, as opposed to a notice that merely scheduled or set it up.
    """
    parts = [entry.get("description"), entry.get("short_description")]
    parts.extend(rd.get("description") for rd in (entry.get("recap_documents") or []))
    text = " | ".join(p for p in parts if p)
    return _describes_proceeding(text)


def _best_proceeding_notes(
    target: dict[str, Any], cluster: list[dict[str, Any]]
) -> Optional[str]:
    """Choose the most informative notes for a dedupe survivor.

    The dedupe MERGE_INTO target is chosen by key descriptiveness / source
    count, which is unrelated to which row actually describes the proceeding.
    When the target's notes do NOT record the proceeding but an absorbed
    sibling's do, return that sibling's notes so the surviving row carries the
    account of what happened rather than the survivor's setup notice. Returns
    ``None`` to leave the target's notes unchanged — either the target already
    records the proceeding, or no sibling describes it any better.
    """
    if _describes_proceeding(target.get("notes")):
        return None
    target_key = target.get("hearing_key")
    candidates = [
        h
        for h in cluster
        if h.get("hearing_key") != target_key and _describes_proceeding(h.get("notes"))
    ]
    if not candidates:
        return None
    # Prefer a substantive record over a transcript filing; then the richest
    # description; stable tie-break on hearing_key.
    best = min(
        candidates,
        key=lambda h: (
            _proceeding_record_rank(h.get("notes")),
            -len(h.get("notes") or ""),
            h.get("hearing_key") or "",
        ),
    )
    return best.get("notes")


# Trailing clerk / court-staff metadata PACER appends to a docket entry's text
# — "(afm, COURT STAFF)", "(Date Filed: 3/24/2026)", "(Entered: 03/24/2026)",
# "(This is a text-only entry ... There is no document associated ...)". Useful
# to nobody reading a calendar, so we strip it when promoting a record entry's
# own text into a hearing's notes.
_DOCKET_TEXT_TRAILER_RE = re.compile(
    r"\s*\((?:[^()]*\b(?:COURT STAFF|Date Filed|Entered|text-only entry|"
    r"no document associated)\b[^()]*)\)\s*$",
    re.IGNORECASE,
)


def _proceeding_notes_from_entry(entry: dict[str, Any]) -> Optional[str]:
    """The record entry's own text, lightly trimmed, for use as hearing notes.

    The docket text of a minute entry / transcript / clerk's notes IS the
    account of the proceeding. This is the fallback the MARK_HELD supersede
    rule and the heal sweep use when no cleaner LLM-written notes are available
    — the LLM routinely omits ``notes`` on a MARK_HELD action for an
    already-held row. Only the trailing clerk/court-staff metadata
    parentheticals are stripped; the substantive text is left verbatim.
    """
    text = (entry.get("description") or entry.get("short_description") or "").strip()
    # Strip the trailing metadata groups one at a time (there are usually
    # several stacked: the text-only note, the staff initials, Date Filed,
    # Entered).
    while True:
        stripped = _DOCKET_TEXT_TRAILER_RE.sub("", text).rstrip()
        if stripped == text:
            break
        text = stripped
    return text or None


_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _hearing_date_tokens(hearing: dict[str, Any]) -> list[str]:
    """Court-local date of a hearing, rendered the ways a docket entry writes it.

    A minute entry for a held proceeding restates the date ("...held on
    3/24/2026"), so the heal sweep uses these tokens to confirm a candidate
    record actually describes THIS hearing rather than a sibling proceeding
    that merely shares the row's source list (a sentencing row must not adopt a
    status-conference record). Returns numeric (padded / unpadded, 4- and
    2-digit year) and spelled-month forms; empty when the hearing has no start.
    """
    starts = hearing.get("starts_at_utc")
    if not starts:
        return []
    try:
        dt = datetime.fromisoformat(starts)
    except (ValueError, TypeError):
        return []
    tzname = hearing.get("timezone")
    if tzname:
        try:
            dt = dt.astimezone(ZoneInfo(tzname))
        except (ZoneInfoNotFoundError, ValueError):
            pass  # fall back to the UTC date when the tz is unrecognized
    d = dt.date()
    m, day, y, yy = d.month, d.day, d.year, d.year % 100
    return [
        f"{m}/{day}/{y}",
        f"{m:02d}/{day:02d}/{y}",
        f"{m}/{day}/{yy:02d}",
        f"{m:02d}/{day:02d}/{yy:02d}",
        f"{_MONTH_NAMES[m - 1]} {day}, {y}",
    ]


# Proceeding-type vocabulary: each canonical tag plus the pattern that signals
# it in a lowercased, hyphen-normalized blob. The heal sweep reduces both the
# hearing (key + title) and a candidate record to type tags and requires them
# to overlap, so a "sentencing" row can't adopt a "status conference" record
# that merely fell on the same date (date-matching alone can't tell them
# apart). An untyped row imposes no type constraint.
_PROCEEDING_TYPE_SIGNALS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("sentencing", re.compile(r"sentenc")),
    ("status conference", re.compile(r"status conf")),
    ("initial appearance", re.compile(r"initial appearance")),
    ("arraignment", re.compile(r"arraign")),
    ("plea", re.compile(r"\bplea")),
    ("pretrial", re.compile(r"pre ?trial")),
    ("trial", re.compile(r"\btrial")),
    ("motion", re.compile(r"motion|in limine|daubert|suppress")),
    ("show cause", re.compile(r"show cause|\bosc\b")),
    ("charging conference", re.compile(r"charging conf")),
    ("oral argument", re.compile(r"oral arg")),
    ("evidentiary", re.compile(r"evidentiary")),
    ("detention", re.compile(r"detention")),
)


def _proceeding_types(text: Optional[str]) -> set[str]:
    """The proceeding-type tags a hearing key/title or a record text signals.

    Used by the heal sweep to require a candidate record to describe the SAME
    KIND of proceeding the row is keyed for. An untyped row (no recognizable
    type) returns an empty set, imposing no constraint.
    """
    if not text:
        return set()
    blob = text.lower().replace("-", " ")
    tags = {tag for tag, rx in _PROCEEDING_TYPE_SIGNALS if rx.search(blob)}
    if "trial" in tags and "pretrial" in tags:
        # "pretrial" contains "trial"; keep the bare "trial" tag only if a
        # standalone "trial" survives removing the pretrial phrase.
        if not re.search(r"\btrial", re.sub(r"pre ?trial\w*", " ", blob)):
            tags.discard("trial")
    return tags


# The proceeding a minute entry RECORDS is named at its head — the phrase right
# after "...before Judge <name>:" (the dominant format) or right after
# "MINUTES OF" — and ends at "as to <party>" / "held" / "before" / a period or
# colon. Matching the proceeding type against ONLY this name (not the whole
# text) separates "recorded a status conference" from a status conference whose
# body says "...Sentencing set for <date>"; the latter mention is in the body,
# never the name. (The sentencing-ding class: a sentencing row whose only
# same-date source is the status-conference minute entry that SET the
# sentencing date — that record names "Status Conference", so a
# sentencing-typed row correctly declines it.)
_PROCEEDING_NAME_RE = re.compile(
    r"minutes?\s+of\s+(?P<a>.+?)(?:\s+(?:as to|held|before)\b|[.:]|$)"
    r"|:\s*(?P<b>.+?)(?:\s+(?:as to|held|before)\b|[.:]|$)",
    re.IGNORECASE,
)


def _record_proceeding_name(text: str) -> str:
    """The proceeding a record NAMES (for type matching), or the full text.

    Returns just the named proceeding ("Status Conference", "Change of Plea
    Hearing") so a body mention of a different, future proceeding can't
    masquerade as the record's own type. Falls back to the whole text for the
    rare record that fits neither preamble.
    """
    m = _PROCEEDING_NAME_RE.search(text)
    if not m:
        return text
    return m.group("a") or m.group("b") or text


def heal_proceeding_notes(store: Store, *, apply: bool = False) -> list[dict[str, Any]]:
    """Retroactively fix hearing notes that regressed to a setup notice.

    Deterministic, no LLM. For every hearing whose notes are empty or a
    pre-hearing administrative notice (clerk's notice of Zoom access /
    courtroom change / scheduling) but whose source entries include the RECORD
    of the proceeding (minute entry / transcript / clerk's notes of proceedings
    held), replace the notes with that record's own text — the same preference
    the MARK_HELD supersede and dedupe-aware notes-selection rules apply at
    sync time, here applied to rows already collapsed in the store (the
    sibling that held the good notes may already have been deleted by a dedupe
    merge, so re-running sync can't recover them).

    Two safeguards keep the blind source-list scan from attaching the WRONG
    proceeding's record (a row's ``source_entry_ids`` legitimately pools
    related proceedings — the status conference that scheduled a hearing is one
    of its sources): the chosen record must (1) restate the row's own date
    (``_hearing_date_tokens``) and (2) describe the same KIND of proceeding the
    row is keyed for (``_proceeding_types``). A row no source record both
    corroborates by date AND matches by type is left alone rather than guessed.

    Hearings whose notes already describe the proceeding, and hearings that
    carry a non-empty NON-administrative note (a deliberately curated
    scheduling note that isn't the regression), are left untouched. Returns one
    dict per changed row — ``case_id`` / ``hearing_key`` / ``old`` / ``new`` —
    so a dry run can report what it would do. When ``apply`` is True the
    changes are written and committed.
    """
    rows = store.conn.execute("SELECT * FROM hearings").fetchall()
    changes: list[dict[str, Any]] = []
    for row in rows:
        h = Store._row_to_hearing(row)
        notes = h.get("notes")
        # Already describes the proceeding, or carries a curated non-setup note
        # — neither is the regression class, so leave it alone.
        if _describes_proceeding(notes):
            continue
        if notes and notes.strip() and not _is_admin_notice(notes):
            continue
        sids = h.get("source_entry_ids") or []
        if not sids:
            continue
        # Look the source entries up by id directly — a hearing's sources can
        # live on sibling dockets, so don't constrain by docket_id.
        placeholders = ",".join("?" * len(sids))
        entry_rows = store.conn.execute(
            f"SELECT entry_id, description, short_description FROM entries "
            f"WHERE entry_id IN ({placeholders})",
            [int(s) for s in sids],
        ).fetchall()
        # A record only heals this row if it CORROBORATES the row's own date —
        # the minute entry restates "...held on <date>". Without this, a row
        # could adopt a sibling proceeding's record that merely shares its
        # source list (a sentencing row picking up the status-conference minute
        # entry that scheduled it). A row whose date no source record restates
        # is left alone rather than guessed.
        date_tokens = _hearing_date_tokens(h)
        hearing_types = _proceeding_types(
            f"{h.get('hearing_key') or ''} {h.get('title') or ''}"
        )
        candidates: list[str] = []
        for er in entry_rows:
            ed = dict(er)
            if not _entry_records_proceeding(ed):
                continue
            text = _proceeding_notes_from_entry(ed)
            if not text:
                continue
            if date_tokens and not any(tok in text for tok in date_tokens):
                continue
            # The record must describe the SAME KIND of proceeding the row is
            # keyed for — a sentencing row must not adopt a status-conference
            # record that happens to share its date + source list. Match the
            # type against the proceeding the record NAMES, not its body (which
            # may schedule a different, future proceeding).
            if hearing_types and not (
                hearing_types & _proceeding_types(_record_proceeding_name(text))
            ):
                continue
            candidates.append(text)
        if not candidates:
            continue
        # Prefer a substantive record (minute entry / clerk's notes) over a
        # transcript filing, then the richest text. `best` is always a
        # non-empty record text and `notes` is empty or an administrative
        # notice here (the gates above), so it can never equal `notes` — no
        # same-value guard needed.
        best = min(candidates, key=lambda t: (_proceeding_record_rank(t), -len(t)))
        changes.append(
            {
                "case_id": h.get("case_id"),
                "hearing_key": h.get("hearing_key"),
                "old": notes,
                "new": best,
            }
        )
        if apply:
            merged = dict(h)
            merged["notes"] = best
            store.upsert_hearing(merged)
    if apply and changes:
        store.conn.commit()
    return changes


def _default_duration(hearing_type: str | None, time_set: bool) -> int:
    if not time_set:
        return 0  # all-day
    return {
        "sentencing": 90,
        "trial": 240,
        "oral_argument": 60,
        "evidentiary_hearing": 120,
        "motion_hearing": 60,
        "plea_hearing": 45,
        "change_of_plea": 45,
        "arraignment": 30,
        "initial_appearance": 30,
        "status_conference": 30,
        "telephonic_conference": 30,
    }.get(hearing_type or "", 60)


class CaseSyncer:
    def __init__(self, cl: CourtListener, store: Store):
        self.cl = cl
        self.store = store

    # --- shared helpers (used by polling sync_case AND the webhook server) ---

    def _is_cross_court_mutation(
        self,
        existing: Optional[dict[str, Any]],
        current_docket_id: int,
    ) -> Optional[tuple[Optional[str], Optional[str]]]:
        """Detect when an action would mutate a row owned by another court.

        The per-entry LLM context filter (``get_hearings_in_court`` /
        ``get_deadlines_in_court``) prevents the LLM from *seeing* sibling-
        court rows on the same case, but the action-apply layer looks up
        ``existing`` by ``(case_id, key)`` only — so when the LLM in court
        B independently invents a kebab-case key that collides with an
        existing court-A row (a generic slug like
        ``petitioner-reply-brief-appellate`` is hit-prone), the court-B
        entry pollutes the court-A row's ``source_entry_ids`` and can
        clobber its fields. This guard reproduces the same-court
        principle at the apply step.

        Returns ``(existing_court, current_court)`` when both can be
        resolved and they differ; ``None`` otherwise (no existing row,
        same docket, or either side's court metadata isn't cached — fall
        through and behave as before).
        """
        if not existing:
            return None
        existing_docket = existing.get("docket_id")
        if not existing_docket or existing_docket == current_docket_id:
            return None
        existing_court = (self.store.get_docket_meta(existing_docket) or {}).get(
            "court_id"
        )
        current_court = (self.store.get_docket_meta(current_docket_id) or {}).get(
            "court_id"
        )
        if not existing_court or not current_court:
            return None
        if existing_court == current_court:
            return None
        return (existing_court, current_court)

    def ensure_docket_cached(self, docket_id: int) -> dict[str, Any]:
        """Return cached docket meta, fetching from CourtListener exactly once if missing.

        Webhook payloads don't include parent-docket metadata, so the first
        time we see a docket via webhook we have to do one /dockets/ GET to
        learn its court_id (and therefore its timezone). After that everything
        is cached and incoming webhooks make zero CourtListener calls.
        """
        meta = self.store.get_docket_meta(docket_id)
        if meta and meta.get("court_id"):
            return meta
        docket = self.cl.get_docket(docket_id)
        self.store.upsert_docket_meta(
            docket_id,
            {
                "court_id": docket.get("court_id"),
                "docket_number": docket.get("docket_number"),
                "case_name": docket.get("case_name"),
                "absolute_url": docket.get("absolute_url"),
                "date_last_filing": docket.get("date_last_filing"),
            },
        )
        self._ensure_court(docket.get("court_id") or "")
        return self.store.get_docket_meta(docket_id) or {}

    def process_entry(
        self,
        case: CaseConfig,
        docket_id: int,
        entry: dict[str, Any],
        *,
        stats: Optional[dict[str, int]] = None,
    ) -> bool:
        """End-to-end processing for one entry: filter, LLM extract, store.

        Used by both polling ``sync_case`` and the webhook receiver, so the
        two paths produce identical hearing rows.
        """
        if stats is None:
            stats = {"entries_seen": 0, "entries_processed": 0, "actions": 0}

        eid = entry["id"]
        fp = fingerprint_entry(entry)
        if self.store.entry_seen(docket_id, eid, fp):
            return False

        meta = self.ensure_docket_cached(docket_id)
        court_id = meta.get("court_id") or ""
        tz = tz_for(court_id)

        processed = self._handle_entry(case, docket_id, court_id, tz, entry, stats)
        # Persist the full description body when the entry is either:
        #   (a) hearing/deadline-relevant — already LLM-processed, body is
        #       needed for `get_recent_relevant_entries` cross-entry context
        #       and emit-time description rendering; or
        #   (b) primary-document or disposition — needed so the summary
        #       pipeline can find these entries locally instead of
        #       re-fetching the same docket-entries pages from CourtListener right
        #       after sync wrote them down.
        # Everything else (notices, briefs, attorney appearances, etc.) gets
        # a fingerprint-only stub: dedup keeps working, but no dead-weight
        # body text.
        summary_relevant = summary_mod.is_primary_document(
            entry
        ) or summary_mod.is_disposition(entry)
        store_full = processed or summary_relevant
        self.store.mark_entry(
            docket_id,
            eid,
            entry.get("date_modified") or "",
            fp,
            date_filed=entry.get("date_filed"),
            entry_number=entry.get("entry_number"),
            description=entry.get("description") if store_full else None,
            short_description=entry.get("short_description") if store_full else None,
            recap_documents=compact_recap_documents(entry) if store_full else None,
        )
        # Advance the docket's date_modified to this entry's value if newer.
        # date_modified is the docket-level short-circuit cutoff — the
        # polling path sets it from the parent docket at end-of-loop, but
        # the webhook path never sees the parent docket per delivery, so
        # without this conditional bump webhook-only deployments would
        # never short-circuit unchanged dockets on subsequent polls.
        entry_dm = entry.get("date_modified") or ""
        if entry_dm:
            self.store.bump_docket_last_modified(docket_id, entry_dm)
        # Same idea for the index page's "Last filing" date: webhook
        # deliveries don't refetch the parent docket, so CourtListener's
        # ``date_last_filing`` would lag the entry we just processed.
        # Use the entry's own ``date_filed`` as a forward-only stand-in;
        # the next docket fetch overwrites it with CourtListener's authoritative
        # value via ``upsert_docket_meta``.
        entry_df = entry.get("date_filed") or ""
        if entry_df:
            self.store.bump_docket_last_filing(docket_id, entry_df)
        # Flag the case_summaries row stale when this entry looks like a
        # primary document (superseding indictment, amended complaint,
        # etc.) or a disposition (judgment, plea agreement, verdict,
        # dismissal, dispositive memo). These are exactly the entries that
        # change the substantive answer to "what is this case about, and
        # where does it stand?" — so the next sync/webhook auto-emit will
        # regenerate the summary before re-rendering the index. We check
        # this independently of `processed` because primary documents
        # and judgments rarely match the hearing-relevance regex but are
        # the most important signals for the summary. The stale flag
        # targets the LOGICAL PACER docket (docket_number, court_id)
        # rather than the CourtListener docket_id — CourtListener can split one
        # PACER docket across multiple docket_id rows (see the docket
        # grouping design decision in AGENTS.md), and we want the next
        # refresh to regenerate the single pooled summary, not three
        # near-duplicates.
        if summary_relevant:
            meta = self.store.get_docket_meta(docket_id) or {}
            docket_number = meta.get("docket_number")
            court_id = meta.get("court_id")
            if docket_number and court_id:
                self.store.mark_summary_stale(case.case_id, docket_number, court_id)
        return processed

    # --- placeholder reconcile (enrichment of webhook-delivered stubs) ---

    def reconcile_placeholders(
        self, case: CaseConfig, *, filed_after: str
    ) -> dict[str, int]:
        """Re-check this case's placeholder entries for upstream enrichment.

        A docket-alert webhook delivers an entry once, at creation — often
        as a stub whose document text isn't available yet (see
        :func:`is_pending_enrichment`). CourtListener later fills in the
        description / makes the PDF available but does NOT fire a second
        webhook (only an updated email alert — CourtListener issue #7423),
        so the webhook path never re-delivers the enriched entry, and the
        per-docket polling short-circuit can take until the next full sync
        to notice. This sweep closes that gap cheaply: for each
        still-pending placeholder on the case's dockets filed on/after
        ``filed_after``, it re-fetches the entry BY ID (one request) and
        re-runs ``process_entry``. If the entry enriched, its fingerprint
        flips and the normal pipeline reschedules / updates from it (and
        flips the summary stale via the upsert chokepoint); if not, the
        fingerprint matches and it's a no-op.

        Cost is one CourtListener request per pending placeholder —
        O(pending placeholders), independent of the docket count — so it
        scales with filing activity like the webhook path, not with the
        size of the caseload like a full per-docket poll. ``filed_after``
        (an ISO date the caller derives from an age cutoff) bounds the
        retries so a stub that never enriches drops out of scope rather
        than being re-checked forever.
        """
        stats: dict[str, int] = {
            "checked": 0,
            "entries_processed": 0,
            "actions": 0,
        }
        rows = self.store.get_empty_body_entries_since(
            case.dockets, filed_after=filed_after
        )
        for row in rows:
            # The store query guarantees a non-null recap_documents JSON and
            # an empty description; apply the doc-level half of the predicate
            # here (the SQL can't reach into the JSON), so a row whose docs
            # are all available/sealed is skipped without a fetch.
            docs = json.loads(row["recap_documents"])
            if not is_pending_enrichment({"description": "", "recap_documents": docs}):
                continue
            stats["checked"] += 1
            entry = self.cl.get_docket_entry(row["entry_id"])
            self.process_entry(case, row["docket_id"], entry, stats=stats)
            with self.store.tx() as _:
                pass  # commit per entry so partial progress sticks
        return stats

    # --- polling entry point ---

    def sync_case(self, case: CaseConfig, *, reverify: bool = False) -> dict[str, int]:
        stats = {
            "dockets_skipped": 0,
            "entries_seen": 0,
            "entries_processed": 0,
            "actions": 0,
            "verified": 0,
            "deduped": 0,
            "deduped_held": 0,
            "deduped_nearslot": 0,
            "deadlines_verified": 0,
            "auto_passed": 0,
        }
        # Did any docket in this case get past the date_modified
        # short-circuit (i.e. land new entries) this sync? When none did,
        # the store is unchanged for this case and the LLM-backed verify /
        # dedupe sweeps would re-read identical rows and — every domain
        # call pins temperature=0 — return identical verdicts. We skip
        # them in that case; see the gate below.
        any_docket_advanced = False
        for docket_id in case.dockets:
            log.info("Syncing docket %s for case %s", docket_id, case.case_id)
            docket = self.cl.get_docket(docket_id)
            docket_mod = docket.get("date_modified") or ""
            last_mod = self.store.docket_last_modified(docket_id)
            if last_mod and docket_mod and docket_mod <= last_mod:
                log.info(
                    "docket %s unchanged since %s; skipping (no API/LLM calls)",
                    docket_id,
                    last_mod,
                )
                # We've already paid the get_docket call above; capturing
                # date_last_filing here costs nothing extra and is the only
                # path that populates the column for quiet dockets after
                # the column was added.
                if docket.get("date_last_filing"):
                    self.store.bump_docket_last_filing(
                        docket_id,
                        docket["date_last_filing"],
                    )
                stats["dockets_skipped"] += 1
                continue

            # This docket cleared the short-circuit: it has new entries to
            # walk, so the verify / dedupe sweeps must run for this case.
            any_docket_advanced = True

            # Persist meta + court so process_entry has what it needs.
            self.store.upsert_docket_meta(
                docket_id,
                {
                    "court_id": docket.get("court_id"),
                    "docket_number": docket.get("docket_number"),
                    "case_name": docket.get("case_name"),
                    "absolute_url": docket.get("absolute_url"),
                    "date_last_filing": docket.get("date_last_filing"),
                },
            )
            self._ensure_court(docket.get("court_id") or "")
            cutoff = self.store.latest_entry_modified(docket_id)

            for entry in self.cl.iter_entries(docket_id, modified_after=cutoff):
                stats["entries_seen"] += 1
                self.process_entry(case, docket_id, entry, stats=stats)
                with self.store.tx() as _:
                    pass  # commit per entry so partial progress sticks

            # Reached only on a clean iteration through every entry. Any
            # exception that escapes the loop — including BaseException
            # subclasses like KeyboardInterrupt (Ctrl+C) and SystemExit —
            # propagates past this point without advancing the cutoff,
            # so the next sync re-walks the docket from its prior
            # last-modified value. The earlier try/except/finally form
            # only caught `Exception`, which silently let Ctrl+C bump
            # the cutoff to the docket's current value and made the
            # docket-level short-circuit on the next sync skip the
            # unprocessed entries entirely (the documented "cutoff is
            # only advanced on a clean run" invariant was broken in the
            # implementation).
            if docket_mod:
                self.store.set_docket_last_modified(docket_id, docket_mod)
                with self.store.tx() as _:
                    pass

        # End-of-case sweeps:
        #   1. Confidence pass — for each future scheduled hearing, ask the
        #      LLM whether recent docket entries support it staying on the
        #      calendar. Catches missed reschedules/cancellations and the
        #      hallucination class (rows extracted from tangentially-related
        #      entries with no actual scheduling order behind them).
        #   2. Auto-held — any 'scheduled' row whose start time is in the
        #      past flips to 'held'. starts_at_utc is already UTC, so the
        #      comparison is timezone-free.
        # Verify first so a "rescheduled to past date" outcome can't get
        # double-flipped to held by the second sweep.
        # _verify_scheduled_hearings audits BOTH future and past 'scheduled'
        # rows. There is no separate auto-held sweep: a past hearing is only
        # marked 'held' when the LLM cites a minute entry / verdict /
        # transcript / judgment-after as evidence of occurrence. Past-dated
        # rows without that evidence stay 'scheduled' — accurately
        # reflecting "the docket has not confirmed this happened" rather
        # than guessing 'held' because the calendar date passed. (Trials
        # get continued or vacated by plea without an explicit cancellation
        # entry; the auto-held heuristic was wrong by default for them.)
        #
        # GATE: skip the LLM-backed sweeps when no docket in this case
        # advanced past the short-circuit. Their inputs (the candidate
        # rows + the docket-entry context window) come entirely from the
        # store, which is unchanged when nothing landed; at temperature=0
        # the verdicts would be byte-identical to the last sync, so
        # re-running is pure cost. `serve` (webhooks) never advances a
        # docket's stored date_modified, so a webhook-touched docket
        # always fails the short-circuit on the next poll and runs its
        # sweeps then — the gate cannot strand a webhook-added row. The
        # `reverify` override forces the sweeps regardless, for the two
        # cases where store rows changed WITHOUT a docket advancing:
        # after a verify-prompt / model change, and after an out-of-band
        # store edit (scripts/reprocess_entries.py,
        # scripts/classify_significance.py).
        if reverify or any_docket_advanced:
            stats["verified"] = self._verify_scheduled_hearings(case)
            # Run dedupe AFTER verify so any RESCHEDULE_HEARING / CANCEL_HEARING from verify
            # gets a chance to clear concurrency before we ask the LLM to
            # resolve it.
            stats["deduped"] = self._dedupe_concurrent_hearings(case)
            # Held-row dedup is a separate sweep: two `held` rows on the
            # same logical PACER docket at the same UTC slot cannot be
            # legitimate (the court physically can't have held two hearings
            # simultaneously), so we merge them deterministically without an
            # LLM call. The motivating case is cross-CourtListener-sibling drift —
            # didenko's `sentencing-didenko` (from CourtListener docket A) and
            # `sentencing-didenko-2` (from CourtListener docket B) at the same UTC slot
            # were created by the per-entry extractor allocating a fresh
            # key on the new sibling instead of reusing the existing key.
            stats["deduped_held"] = self._dedupe_concurrent_held_hearings(case)
            # Near-slot dedup runs LAST of the hearing sweeps: the exact-slot
            # sweeps above have already merged identical-slot dups, so this
            # only sees the NEAR-slot key-drift the extractor produces (same
            # court day at a different time; a once-only proceeding at a
            # slightly different date) — LLM-gated, so a genuine two-distinct-
            # hearings-same-day pair is kept.
            stats["deduped_nearslot"] = self._dedupe_nearslot_hearings(case)
            stats["deadlines_verified"] = self._verify_pending_deadlines(case)
        # _auto_mark_passed_stale runs UNCONDITIONALLY — it is the one
        # sweep driven by the wall clock rather than by docket changes
        # (it flips a 'pending' deadline to 'passed' once its due date
        # elapses) and it makes no LLM call. Gating it would leave a
        # deadline on a quiet docket stuck at 'pending' forever after its
        # date passed.
        stats["auto_passed"] = self._auto_mark_passed_stale(case.case_id)
        return stats

    def _verify_context_entries(
        self,
        docket_id: int,
        target_date_utc: Optional[str],
        source_entry_ids: Optional[list[int]] = None,
    ) -> list[dict[str, Any]]:
        """Docket entries to hand the verify-pass LLM for one hearing.

        Combines three sets, de-duplicated by entry id:

        1. The 15 most-recent hearing-relevant entries — "what's happening
           now" (continuances, reschedules, the current posture).
        2. Hearing-relevant entries filed AROUND the hearing's own date —
           where the entry that records its OUTCOME lives (a minute entry,
           verdict, transcript, or judgment is filed on or shortly after the
           hearing). On a docket that kept moving afterward those entries
           fall outside set 1, so without this the verify LLM cannot cite
           the evidence it needs to MARK_HELD and the row wrongly stays
           'scheduled'. For a FUTURE hearing the window is in the future and
           returns nothing, so this only adds signal for past-dated rows.
        3. The hearing's SOURCE entries (``source_entry_ids``) — the docket
           entries that originally allocated the row. Without these the
           model's DELETE_HALLUCINATION rule ("you've seen the original
           source entry and concluded it does NOT actually schedule this
           hearing") is unsatisfiable when the scheduling order is older
           than the recent window AND outside the around-date window, and
           the model breaks the rule rather than picking UNCLEAR. The
           McGonigal-trial regression — a 2024 jury trial scheduled by an
           older order, then mooted by a plea without a formal vacatur
           entry, that the verify pass marked DELETE_HALLUCINATION because
           the scheduling order wasn't visible — is the canonical case.
           Including source entries makes the rule satisfiable; the
           matching deterministic guard in :meth:`_apply_verify_action`
           enforces the rule the other way (downgrade
           DELETE_HALLUCINATION to UNCLEAR if any source entry was
           absent from what the LLM saw, which can still happen if a
           source row was deleted or its id is malformed).

        This widens what the LLM can SEE; it does not loosen the bar for
        marking a hearing held — the prompt still requires a cited record.
        """
        recent = self.store.get_recent_relevant_entries(
            docket_id, "9999-12-31T00:00:00", limit=15
        )
        near: list[dict[str, Any]] = []
        if target_date_utc:
            try:
                anchor_date = datetime.fromisoformat(
                    target_date_utc.replace("Z", "+00:00")
                ).date()
            except ValueError:
                anchor_date = None
            if anchor_date is not None:
                start = (
                    anchor_date - timedelta(days=_VERIFY_OUTCOME_LOOKBACK_DAYS)
                ).isoformat()
                end = (
                    anchor_date + timedelta(days=_VERIFY_OUTCOME_WINDOW_DAYS)
                ).isoformat()
                near = self.store.get_relevant_entries_in_date_range(
                    docket_id, start, end, limit=20
                )
        sources: list[dict[str, Any]] = []
        if source_entry_ids:
            sources = self.store.get_entries_by_ids(docket_id, source_entry_ids)

        seen: set[int] = set()
        merged: list[dict[str, Any]] = []
        for entry in (*recent, *near, *sources):
            eid = entry["entry_id"]  # always present from the store query
            if eid in seen:
                continue
            seen.add(eid)
            merged.append(entry)
        # Newest-first by filing date so the LLM reads them in a coherent order.
        merged.sort(key=lambda e: e.get("date_filed") or "", reverse=True)
        return merged

    def _verify_scheduled_hearings(self, case: CaseConfig) -> int:
        """Audit non-terminal hearings against recent docket entries.

        Returns the number of hearings whose row was modified by the audit.

        Scope: every ``scheduled`` row (past and future) plus every PAST
        ``cancelled`` row. The action grid:

        - For ``scheduled`` rows the LLM returns CONFIRM (no-op),
          RESCHEDULE_HEARING, CANCEL_HEARING, MARK_HELD, DELETE_HALLUCINATION, or
          UNCLEAR. Past-dated rows require explicit evidence
          (minute entry / verdict / transcript / judgment-after) for
          MARK_HELD — date-passed alone is not enough. Trials and other
          hearings can pass their scheduled date without an explicit
          cancellation entry, so UNCLEAR (no change) is the correct
          default for past rows the docket doesn't confirm.

        - For PAST ``cancelled`` rows the LLM additionally returns
          REINSTATE when the cancellation isn't supported by docket
          evidence (no vacatur entry, no plea agreement, no dismissal —
          just an absence of activity that a prior LLM misread as
          cancellation). The caller flips the row back to ``scheduled``
          so the next sync can MARK_HELD it on real evidence or leave it
          UNCLEAR. This catches the inverse-Moucka failure mode where a
          live trial got falsely marked cancelled.

          Future cancelled rows are NOT verified — a deliberately
          cancelled future hearing should stay cancelled until something
          actively un-cancels it.
        """
        from . import llm as llm_mod

        now_iso = datetime.now(timezone.utc).isoformat()
        rows = self.store.conn.execute(
            """
            SELECT * FROM hearings
            WHERE case_id=?
              AND starts_at_utc IS NOT NULL
              AND (
                status='scheduled'
                OR (status='cancelled' AND starts_at_utc < ?)
              )
            """,
            (case.case_id, now_iso),
        ).fetchall()
        if not rows:
            return 0

        n_changed = 0
        for r in rows:
            hearing = Store._row_to_hearing(r)

            docket_id = hearing.get("docket_id")
            if not docket_id:
                continue
            meta = self.ensure_docket_cached(docket_id)
            court_id = meta.get("court_id") or ""
            tz = tz_for(court_id)

            recent = self._verify_context_entries(
                docket_id,
                hearing.get("starts_at_utc"),
                source_entry_ids=hearing.get("source_entry_ids"),
            )
            action = llm_mod.verify_hearing(
                case_name=case.name,
                court_id=court_id,
                court_tz=tz,
                hearing=hearing,
                recent_entries=recent,
            )
            if self._apply_verify_action(
                case, docket_id, tz, hearing, action, recent_entries=recent
            ):
                n_changed += 1
        if n_changed:
            self.store.conn.commit()
        return n_changed

    @staticmethod
    def _delete_hallucination_allowed(
        row_label: str,
        case_id: str,
        key: Optional[str],
        source_entry_ids: list[int],
        recent_entries: list[dict[str, Any]],
    ) -> bool:
        """Deterministic guard: only allow DELETE_HALLUCINATION when the
        verify-pass LLM was shown every one of the row's source entries.

        The model's rule (from VERIFY_SYSTEM_PROMPT) is "only emit
        DELETE_HALLUCINATION when you've SEEN the original source entry
        and concluded it does NOT actually schedule this hearing." When
        the source entry isn't in the recent_entries the model saw, the
        rule is unsatisfiable: the model can't have read what it didn't
        receive. At temperature=0 the model breaks the rule anyway —
        the McGonigal trial regression (a 2024 jury trial scheduled by
        a 2023 order that fell outside both the recent and around-date
        windows, then mooted by a plea without a vacatur entry) was
        emitted as DELETE_HALLUCINATION because the model saw no
        scheduling evidence and concluded the row was hallucinated. The
        Fix #2 context enrichment now always passes source entries into
        the context, but a source row could still be missing for
        legitimate reasons (the row was deleted from the local store,
        the source_entry_ids list is malformed, the entries query
        returned fewer rows than requested). This guard makes the
        contract explicit: if any source entry was absent from what the
        model saw, the verdict is downgraded to UNCLEAR (no-op,
        WARN-logged) regardless of what the LLM emitted.

        An empty ``source_entry_ids`` list is vacuously satisfied — the
        guard returns True. (A row with no source entries is suspicious
        regardless of which verdict the model emits, but that's a
        separate concern; this guard only checks "did the model see
        what it claims to have seen.")
        """
        if not source_entry_ids:
            return True
        shown = {e["entry_id"] for e in recent_entries}
        missing = [eid for eid in source_entry_ids if eid not in shown]
        if not missing:
            return True
        log.warning(
            "verify-pass rejecting DELETE_HALLUCINATION on %s: model didn't see "
            "source entries %s for case=%s key=%r — downgrading to UNCLEAR",
            row_label,
            missing,
            case_id,
            key,
        )
        return False

    def _apply_verify_action(
        self,
        case: CaseConfig,
        docket_id: int,
        tz: str,
        hearing: dict[str, Any],
        action: dict[str, Any],
        recent_entries: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        """Apply a single verify-pass action to the hearing row.

        Returns True if the row changed. Uses the same upsert path as the
        regular extraction pipeline so source_entry_ids and audit fields
        stay consistent.

        ``recent_entries`` is the same context payload that was sent to
        the LLM, used by the DELETE_HALLUCINATION guard to verify the
        model saw what its rule says it must have seen. Defaults to
        ``None``/empty so the guard is permissive when no context was
        recorded (any non-default test path); production always passes
        the real context.
        """
        atype = (action.get("type") or "UNCLEAR").upper()
        if atype in ("CONFIRM", "UNCLEAR"):
            return False

        merged = dict(hearing)
        sources = list(hearing.get("source_entry_ids") or [])
        audit_note: Optional[str] = None

        if atype == "CANCEL_HEARING":
            merged.update(status="cancelled")
            audit_note = action.get("reason") or "Cancelled per recent docket entries"
        elif atype == "DELETE_HALLUCINATION":
            if not self._delete_hallucination_allowed(
                "hearing",
                case.case_id,
                hearing.get("hearing_key"),
                sources,
                recent_entries or [],
            ):
                return False
            # Don't actually delete — preserve the audit trail by marking
            # cancelled with an explanatory note. Renderers skip cancelled
            # rows so the calendar shows the right thing.
            merged.update(status="cancelled")
            audit_note = action.get("reason") or "No docket entry supports this hearing"
        elif atype == "MARK_HELD":
            merged.update(status="held")
        elif atype == "REINSTATE":
            # Issued for a 'cancelled' row whose cancellation is not
            # supported by an explicit docket entry. Revert to
            # 'scheduled' so the next verify pass can MARK_HELD it on
            # real evidence (or leave it UNCLEAR if the outcome still
            # isn't documented). The McGonigal-shape regression — a
            # past trial row marked cancelled even though the case
            # continued to be actively briefed after the trial date —
            # is the canonical case.
            merged.update(status="scheduled")
            audit_note = action.get("reason") or (
                "Cancellation not supported by docket; reinstated to scheduled"
            )
        elif atype == "RESCHEDULE_HEARING":
            local_date = action.get("local_date")
            local_time = action.get("local_time")
            if not local_date:
                log.warning(
                    "verify RESCHEDULE_HEARING without local_date: case=%s key=%r",
                    case.case_id,
                    hearing.get("hearing_key"),
                )
                return False
            convert_tz = hearing.get("timezone") or tz
            merged["starts_at_utc"] = _local_to_utc(local_date, local_time, convert_tz)
            audit_note = action.get("reason") or "Rescheduled per recent docket entries"
        else:
            log.warning(
                "verify-pass unknown action type %s for case=%s key=%r",
                atype,
                case.case_id,
                hearing.get("hearing_key"),
            )
            return False

        # Audit text lands in `audit_notes`, NOT `notes`. The split is
        # essential: the verify-pass LLM is fed `notes` as docket
        # context but NEVER `audit_notes`, so it cannot read its own
        # prior conclusions and self-confirm. The McGonigal trial
        # regression — a row marked 'cancelled' by an earlier pass whose
        # synthesized "[Trial vacated by guilty plea...]" line then read
        # like docket testimony on the next sync — is the canonical
        # circular-reasoning shape this column split eliminates.
        if audit_note is not None:
            merged["audit_notes"] = _append_audit_line(
                hearing.get("audit_notes"),
                "verify-pass",
                audit_note,
            )
        log.info(
            "verify-pass applying %s case=%s key=%r reason=%s",
            atype,
            case.case_id,
            hearing.get("hearing_key"),
            (action.get("reason") or "")[:120],
        )
        merged["source_entry_ids"] = sources
        self.store.upsert_hearing(merged)
        return True

    def _dedupe_concurrent_hearings(self, case: CaseConfig) -> int:
        """Resolve future scheduled hearings sharing (docket_id, starts_at_utc).

        A single court cannot hold two hearings on one docket at the same
        date and time, so equal ``(docket_id, starts_at_utc)`` across
        ``status='scheduled'`` rows is a signal the per-entry extractor
        split one logical event across keys (e.g. a stipulation said
        "Hearing on Motion for Summary Judgment" and the signed order
        called it "Motion Hearing" — same slot, two ``hearing_key``s).
        The verify pass operates on one row in isolation and has no view
        of sibling future hearings; this sweep closes that gap.

        For each cluster the LLM returns MERGE_INTO (DELETE duplicates,
        merge their source_entry_ids into the target row, append their
        keys to the target's audit_notes) or KEEP_BOTH / UNCLEAR (no-op
        — used for the rare case where two distinct proceedings really
        are scheduled back-to-back at the same time). Returns the count
        of rows deleted by merge.
        """
        from . import llm as llm_mod

        clusters = self.store.find_concurrent_hearing_clusters(case.case_id)
        if not clusters:
            return 0

        n_merged = 0
        for cluster in clusters:
            # find_concurrent_hearing_clusters guarantees docket_id NOT
            # NULL via its SQL filter — no defensive check needed here.
            docket_id = cluster[0]["docket_id"]
            meta = self.ensure_docket_cached(docket_id)
            court_id = meta.get("court_id") or ""
            tz = tz_for(court_id)
            recent = self.store.get_recent_relevant_entries(
                docket_id,
                "9999-12-31T00:00:00",
                limit=15,
            )
            action = llm_mod.resolve_duplicate_hearings(
                case_name=case.name,
                court_id=court_id,
                court_tz=tz,
                cluster=cluster,
                recent_entries=recent,
            )
            n_merged += self._apply_dedupe_action(case, cluster, action)

        if n_merged:
            self.store.conn.commit()
        return n_merged

    def _apply_dedupe_action(
        self,
        case: CaseConfig,
        cluster: list[dict[str, Any]],
        action: dict[str, Any],
        *,
        source: str = "dedupe",
    ) -> int:
        """Apply one MERGE_INTO / KEEP_BOTH / UNCLEAR action to a cluster.

        ``source`` is the audit-trail writer tag recorded on the surviving
        row ("dedupe" for the exact-slot scheduled sweep, "dedupe-nearslot"
        for the near-slot sweep) so a future reader can tell which sweep
        absorbed the siblings.

        Returns the number of rows that were absorbed (merged into and
        then deleted in favor of the target).
        """
        atype = (action.get("type") or "UNCLEAR").upper()
        if atype != "MERGE_INTO":
            log.info(
                "%s: keys=%s -> %s reason=%r",
                source,
                [h.get("hearing_key") for h in cluster],
                atype,
                (action.get("reason") or "")[:120],
            )
            return 0

        target_key = action.get("target_key")
        target = next(
            (h for h in cluster if h.get("hearing_key") == target_key),
            None,
        )
        if not target:
            log.warning(
                "dedupe MERGE_INTO target_key %r not in cluster %s: leaving cluster alone",
                target_key,
                [h.get("hearing_key") for h in cluster],
            )
            return 0

        # Merge source_entry_ids from all duplicates into the target.
        merged_sources: list[Any] = list(target.get("source_entry_ids") or [])
        seen: set[Any] = set(merged_sources)
        sibling_keys: list[str] = []
        for dup in cluster:
            dup_key = dup["hearing_key"]
            if dup_key == target_key:
                continue
            sibling_keys.append(dup_key)
            for sid in dup.get("source_entry_ids") or []:
                if sid not in seen:
                    seen.add(sid)
                    merged_sources.append(sid)
        target["source_entry_ids"] = merged_sources

        # Notes: the target was chosen for key descriptiveness / source count,
        # which says nothing about which row actually describes the proceeding.
        # If an absorbed sibling's notes record what happened and the target's
        # don't, adopt the sibling's so the survivor carries the account of the
        # proceeding rather than a pre-hearing setup notice.
        better_notes = _best_proceeding_notes(target, cluster)
        if better_notes is not None:
            target["notes"] = better_notes

        # Record absorbed sibling keys + the LLM's reason on the
        # canonical's audit_notes so the audit trail of WHICH keys were
        # absorbed (and why) survives the sibling deletions.
        reason = action.get("reason") or f"Duplicate of {target_key}"
        target["audit_notes"] = _append_audit_line(
            target.get("audit_notes"),
            source,
            f"Absorbed sibling key(s) {', '.join(sibling_keys)}: {reason}",
        )
        self.store.upsert_hearing(target)

        # Delete each duplicate outright. Earlier behavior flipped them
        # to status='cancelled', which inflated H_canc deviation in the
        # provider scorer (each absorbed sibling counted as a spurious
        # cancellation even though it was just a key-drift artifact).
        n_deleted = 0
        for dup in cluster:
            dup_key = dup["hearing_key"]
            if dup_key == target_key:
                continue
            self.store.delete_hearing(case.case_id, dup_key)
            n_deleted += 1

        log.info(
            "%s: absorbed %d hearing(s) into %r on docket %s (case=%s)",
            source,
            n_deleted,
            target_key,
            target.get("docket_id"),
            case.case_id,
        )
        return n_deleted

    def _dedupe_concurrent_held_hearings(self, case: CaseConfig) -> int:
        """Merge held hearings sharing the same logical PACER slot.

        Two ``status='held'`` rows on the same logical PACER docket at
        the same UTC slot are unambiguously a key-drift duplicate — a
        court cannot have physically held two hearings simultaneously,
        so the per-entry extractor must have allocated two different
        ``hearing_key`` values for one logical event. Common cause:
        cross-CourtListener-sibling drift (the didenko sentencing-didenko vs
        sentencing-didenko-2 shape) where the per-entry extractor on
        the newly-synced sibling didn't reuse the existing key it was
        given in ``known_hearings``.

        Resolution is deterministic — no LLM call needed:
        - Canonical row = most ``source_entry_ids`` (more sync passes
          built up its audit trail), tie-broken by oldest
          ``last_updated`` (the original row), tie-broken by
          ``hearing_key`` alphabetically (stable ordering).
        - Merge sibling rows' ``source_entry_ids`` into the canonical,
          AND append the absorbed sibling key(s) to the canonical's
          ``audit_notes`` so the audit trail of WHICH keys were absorbed
          stays attached to the surviving row.
        - DELETE the sibling rows outright. Earlier behavior flipped
          siblings to ``status='cancelled'`` and kept the row, which
          inflated H_canc deviation in the provider scorer (each
          collapsed sibling counted as a spurious cancellation even
          though it was just a key-drift artifact, never a real
          court-ordered cancellation). The audit trail now lives on
          the canonical row instead of being split across the sibling.

        Returns the count of rows deleted.
        """
        clusters = self.store.find_concurrent_held_hearing_clusters(case.case_id)
        if not clusters:
            return 0

        def _rank(h: dict[str, Any]) -> tuple[int, str, str]:
            # Higher source_entry_ids count first (so we sort descending
            # via negation), oldest last_updated next (ascending), then
            # alphabetical hearing_key for stable tie-break.
            n = len(h.get("source_entry_ids") or [])
            return (-n, h.get("last_updated") or "", h.get("hearing_key") or "")

        n_deleted = 0
        for cluster in clusters:
            ranked = sorted(cluster, key=_rank)
            target = ranked[0]
            target_key = target.get("hearing_key")
            slot = target.get("starts_at_utc")
            # Merge source_entry_ids from siblings into the canonical.
            merged_sources: list[Any] = list(target.get("source_entry_ids") or [])
            seen: set[Any] = set(merged_sources)
            for dup in ranked[1:]:
                for sid in dup.get("source_entry_ids") or []:
                    if sid not in seen:
                        seen.add(sid)
                        merged_sources.append(sid)
            target["source_entry_ids"] = merged_sources
            # Adopt an absorbed sibling's proceeding-record notes when the
            # canonical's notes don't describe the proceeding — same reasoning
            # as the LLM-gated sweep: the canonical is picked by source count /
            # recency, not by which row records what happened.
            better_notes = _best_proceeding_notes(target, cluster)
            if better_notes is not None:
                target["notes"] = better_notes
            # Record the absorbed sibling keys on the canonical's
            # audit_notes so the audit trail of WHICH keys were absorbed
            # stays attached to the surviving row after the siblings are
            # deleted.
            sibling_keys: list[str] = [dup["hearing_key"] for dup in ranked[1:]]
            target["audit_notes"] = _append_audit_line(
                target.get("audit_notes"),
                "dedupe-held",
                f"Absorbed sibling key(s) {', '.join(sibling_keys)} at same UTC slot {slot}",
            )
            self.store.upsert_hearing(target)

            for dup in ranked[1:]:
                self.store.delete_hearing(case.case_id, dup["hearing_key"])
                n_deleted += 1
            log.info(
                "dedupe-held: absorbed %d hearing(s) into %r at %s (case=%s)",
                len(ranked) - 1,
                target_key,
                slot,
                case.case_id,
            )

        # Unconditional commit: the early-return at the top of this
        # function guarantees we only reach here when `clusters` was
        # non-empty, which means at least one row was deleted.
        # Guarding on `n_deleted` would be dead code (the AGENTS.md
        # testing philosophy treats unreachable defensive code as a
        # test smell).
        self.store.conn.commit()
        return n_deleted

    def _dedupe_nearslot_hearings(self, case: CaseConfig) -> int:
        """Resolve NEAR-slot duplicate hearings the exact-slot sweeps miss.

        The per-entry extractor (Gemini especially) allocates a fresh
        ``hearing_key`` for a proceeding it already has at a NEAR slot — the
        same court day at a different time (a date-only midnight row + a timed
        row of one proceeding), or the same once-only proceeding (sentencing /
        arraignment / change-of-plea / initial-appearance) recorded at both its
        scheduled date and the date it was actually held. Those render TWICE on
        subscriber calendars, and the exact-slot sweeps above — which require an
        identical ``starts_at_utc`` — don't catch them.

        ``Store.find_nearslot_hearing_clusters`` surfaces the candidates; each
        goes to the LLM resolver. MERGE_INTO DELETEs the absorbed rows and folds
        their ``source_entry_ids`` into the target (with a ``[dedupe-nearslot]``
        audit line); KEEP_BOTH / UNCLEAR are no-ops. This stays LLM-gated rather
        than deterministic precisely because a court CAN hold two DIFFERENT
        hearings on one day at different times — only the model (seeing the
        titles + docket text) should decide. Runs AFTER the exact-slot sweeps.
        Returns the count of rows deleted by merge.
        """
        from . import llm as llm_mod

        clusters = self.store.find_nearslot_hearing_clusters(case.case_id)
        if not clusters:
            return 0

        # find_nearslot_hearing_clusters returns DISJOINT clusters (its
        # singular-type tier skips rows the same-date tier already claimed), so
        # a row never appears in two clusters — no cross-cluster
        # already-absorbed bookkeeping is needed here.
        n_merged = 0
        for cluster in clusters:
            docket_id = cluster[0]["docket_id"]
            meta = self.ensure_docket_cached(docket_id)
            court_id = meta.get("court_id") or ""
            tz = tz_for(court_id)
            recent = self.store.get_recent_relevant_entries(
                docket_id,
                "9999-12-31T00:00:00",
                limit=15,
            )
            action = llm_mod.resolve_duplicate_hearings(
                case_name=case.name,
                court_id=court_id,
                court_tz=tz,
                cluster=cluster,
                recent_entries=recent,
            )
            n_merged += self._apply_dedupe_action(
                case, cluster, action, source="dedupe-nearslot"
            )

        if n_merged:
            self.store.conn.commit()
        return n_merged

    def _verify_pending_deadlines(self, case: CaseConfig) -> int:
        """Audit every future pending deadline against recent docket entries.

        Mirrors :meth:`_verify_scheduled_hearings` for filing deadlines:
        catches missed extensions, vacaturs, and the hallucination class.
        Returns the count of rows modified.
        """
        from . import llm as llm_mod

        now_iso = datetime.now(timezone.utc).isoformat()
        rows = self.store.conn.execute(
            "SELECT * FROM deadlines "
            "WHERE case_id=? AND status='pending' "
            "AND due_at_utc IS NOT NULL AND due_at_utc >= ?",
            (case.case_id, now_iso),
        ).fetchall()
        if not rows:
            return 0

        n_changed = 0
        for r in rows:
            d = Store._row_to_deadline(r)

            docket_id = d.get("docket_id")
            if not docket_id:
                continue
            meta = self.ensure_docket_cached(docket_id)
            court_id = meta.get("court_id") or ""
            tz = tz_for(court_id)

            recent = self._verify_context_entries(
                docket_id,
                d.get("due_at_utc"),
                source_entry_ids=d.get("source_entry_ids"),
            )
            action = llm_mod.verify_deadline(
                case_name=case.name,
                court_id=court_id,
                court_tz=tz,
                deadline=d,
                recent_entries=recent,
            )
            if self._apply_verify_deadline_action(
                case, docket_id, tz, d, action, recent_entries=recent
            ):
                n_changed += 1
        if n_changed:
            self.store.conn.commit()
        return n_changed

    def _apply_verify_deadline_action(
        self,
        case: CaseConfig,
        docket_id: int,
        tz: str,
        deadline: dict[str, Any],
        action: dict[str, Any],
        recent_entries: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        atype = (action.get("type") or "UNCLEAR").upper()
        if atype in ("CONFIRM", "UNCLEAR"):
            return False

        merged = dict(deadline)
        sources = list(deadline.get("source_entry_ids") or [])
        audit_note: Optional[str] = None

        if atype == "CANCEL_HEARING":
            merged["status"] = "cancelled"
            audit_note = action.get("reason") or "Vacated per recent docket entries"
        elif atype == "DELETE_HALLUCINATION":
            if not self._delete_hallucination_allowed(
                "deadline",
                case.case_id,
                deadline.get("deadline_key"),
                sources,
                recent_entries or [],
            ):
                return False
            merged["status"] = "cancelled"
            audit_note = (
                action.get("reason") or "No docket entry supports this deadline"
            )
        elif atype == "MARK_FILED":
            merged["status"] = "met"
        elif atype == "RESCHEDULE_HEARING":
            local_date = action.get("local_date")
            if not local_date:
                log.warning(
                    "verify_deadline RESCHEDULE_HEARING without local_date: case=%s key=%r",
                    case.case_id,
                    deadline.get("deadline_key"),
                )
                return False
            convert_tz = deadline.get("timezone") or tz
            merged["due_at_utc"] = _deadline_local_to_utc(
                local_date, action.get("local_time"), convert_tz
            )
            audit_note = action.get("reason") or "Extended per recent docket entries"
        else:
            log.warning(
                "verify_deadline unknown action type %s: case=%s key=%r",
                atype,
                case.case_id,
                deadline.get("deadline_key"),
            )
            return False

        if audit_note is not None:
            merged["audit_notes"] = _append_audit_line(
                deadline.get("audit_notes"),
                "verify-pass",
                audit_note,
            )
        log.info(
            "verify_deadline applying %s case=%s key=%r reason=%s",
            atype,
            case.case_id,
            deadline.get("deadline_key"),
            (action.get("reason") or "")[:120],
        )
        merged["source_entry_ids"] = sources
        self.store.upsert_deadline(merged)
        return True

    def _auto_mark_passed_stale(self, case_id: str) -> int:
        """Flip past-dated 'pending' deadlines to 'passed'. Returns count flipped.

        Mirrors :meth:`_auto_mark_held_stale` for hearings: ``due_at_utc`` is
        UTC and the server clock is UTC, so the comparison is timezone-free.
        If a later entry shows the filing was actually made, MARK_FILED on a
        subsequent sync flips the row to 'met'; otherwise it stays 'passed'
        and the operator knows to go check PACER.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        rows = self.store.conn.execute(
            "SELECT case_id, deadline_key, due_at_utc FROM deadlines "
            "WHERE case_id=? AND status='pending' "
            "AND due_at_utc IS NOT NULL AND due_at_utc < ?",
            (case_id, now_iso),
        ).fetchall()
        n = 0
        for r in rows:
            log.info(
                "auto-marking deadline passed: case=%s key=%r due=%s",
                r["case_id"],
                r["deadline_key"],
                r["due_at_utc"],
            )
            self.store.conn.execute(
                "UPDATE deadlines SET status='passed', last_updated=? "
                "WHERE case_id=? AND deadline_key=?",
                (now_iso, r["case_id"], r["deadline_key"]),
            )
            n += 1
        if n:
            self.store.conn.commit()
        return n

    def _insert_terminal_hearing(
        self,
        case: CaseConfig,
        docket_id: int,
        tz: str,
        entry: dict[str, Any],
        action: dict[str, Any],
        *,
        status: str,
        prev_sources: list[int],
    ) -> None:
        """Insert a brand-new hearing directly into a terminal status.

        Used when CANCEL_HEARING or MARK_HELD targets a hearing_key that isn't in
        the store — typically because its original scheduling entry was
        filtered out by the prefilter, but a later memo/minute entry
        explicitly tells us a hearing existed and is now adjourned/held.
        Preserves the audit trail without depending on the LLM emitting
        an ADD_HEARING-then-CANCEL_HEARING pair.
        """
        local_date = action.get("local_date")
        local_time = action.get("local_time")
        starts_utc = _local_to_utc(local_date, local_time, tz)
        duration = action.get("duration_minutes") or _default_duration(
            action.get("hearing_type"), bool(local_time)
        )
        self.store.upsert_hearing(
            {
                "case_id": case.case_id,
                "hearing_key": action["hearing_key"],
                "title": action.get("title")
                or action["hearing_key"].replace("-", " ").title(),
                "starts_at_utc": starts_utc,
                "duration_minutes": duration,
                "timezone": tz,
                "location": action.get("location"),
                "judge": action.get("judge"),
                "notes": action.get("notes"),
                "dial_in": action.get("dial_in"),
                "status": status,
                "significance": action.get("significance") or "major",
                "gcal_event_id": None,
                "docket_id": docket_id,
                "source_entry_ids": prev_sources,
            }
        )

    # --- per-entry logic ---

    def _ensure_court(self, court_id: Optional[str]) -> None:
        """Cache court metadata (citation_string) on first sight."""
        if not court_id:
            return
        if self.store.get_court_citation(court_id) is not None:
            return
        try:
            c = self.cl.get_court(court_id)
        except Exception as e:
            # Surface the exception type alongside the message so an
            # operator can tell network / transport (httpx.* errors) from
            # API-side problems (CourtListener returning 4xx/5xx that
            # bubbles as HTTPStatusError) from parsing problems (JSON
            # decode errors). The court_id is the only fetch-identifying
            # piece worth logging; it doubles as the operator's pointer
            # back to the configured docket that needs investigation.
            log.warning(
                "court fetch failed id=%s: %s: %s",
                court_id,
                type(e).__name__,
                e,
            )
            return
        self.store.upsert_court(
            court_id,
            c.get("citation_string"),
            c.get("short_name"),
            c.get("full_name"),
        )

    def _handle_entry(
        self,
        case: CaseConfig,
        docket_id: int,
        court_id: str,
        tz: str,
        entry: dict[str, Any],
        stats: dict[str, int],
    ) -> bool:
        """True iff the entry made it through the regex filter and reached the LLM."""
        if not is_extractable(entry):
            log.debug("entry %s skipped by regex pre-filter", entry.get("id"))
            return False
        stats["entries_processed"] += 1

        pdf_texts = self._maybe_fetch_pdfs(entry)
        # Restrict known-events context to siblings in the same court.
        # Parallel proceedings in different venues must not feed each other's
        # context — a "stay appellate proceedings" order in court B would
        # otherwise contaminate court A's events with bogus CANCEL_HEARING actions.
        # Co-defendant dockets in the same court (multi-defendant criminal
        # cases) still aggregate correctly.
        known = self.store.get_hearings_in_court(case.case_id, court_id)
        referenced = self._resolve_docket_refs(docket_id, entry)
        known_deadlines = self.store.get_deadlines_in_court(case.case_id, court_id)

        actions = llm.extract_actions(
            case_name=case.name,
            court_id=court_id,
            court_tz=tz,
            entry=entry,
            pdf_texts=pdf_texts,
            known_hearings=known,
            docket_id=docket_id,
            referenced_entries=referenced,
            known_deadlines=known_deadlines,
        )

        for action in actions:
            action = _normalize_action_category(action)
            atype = (action.get("type") or "").upper()
            stats["actions"] += 1
            if atype.endswith("_DEADLINE") or atype == "MARK_FILED":
                self._apply_deadline_action(case, docket_id, tz, entry, action)
            else:
                _validate_action_dial_in(action)
                self._apply_action(case, docket_id, tz, entry, action)

        return True

    def _resolve_docket_refs(
        self, docket_id: int, entry: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Look up entries that give the LLM context for the current entry.

        Combines two channels:

        1. *Explicit references* — when the entry text says "granting 65
           Motion" or "see [42]", we pull entry 65 / 42 by docket position.
        2. *Temporal proximity* — the most recent few hearing-relevant
           entries on the same docket. Many orders that schedule a hearing
           don't cite the underlying motion by docket number ("PAPERLESS
           Order Setting Telephonic Pretrial Conference..." with no "65"
           anywhere), so without this the LLM would title the hearing only
           from what the order says — losing details like "CIPA" that live
           in the originating motion just a few entries earlier.

        Misses (entries with no stored description, e.g. filter-failed
        notices) are silently skipped. Entries that appear in both channels
        are included once.
        """
        out: list[dict[str, Any]] = []
        seen_ids: set[int] = set()

        for n in _extract_docket_refs(entry):
            row = self.store.get_entry_by_number(docket_id, n)
            if row and (row.get("description") or row.get("short_description")):
                eid = row["entry_id"]
                seen_ids.add(eid)
                out.append({"entry_number": n, **row})

        recent = self.store.get_recent_relevant_entries(
            docket_id, entry.get("date_modified") or "", limit=5
        )
        for row in recent:
            eid = row["entry_id"]
            if eid not in seen_ids:
                seen_ids.add(eid)
                out.append({"entry_number": row.get("entry_number"), **row})

        return out

    def _maybe_fetch_pdfs(self, entry: dict[str, Any]) -> list[str]:
        """Pull PDF text for the LLM. Called only when an entry's fingerprint
        flips, i.e. on first sight or when CourtListener has changed something. PDFs are
        immutable once attached and we don't cache the extracted text — the
        rare fingerprint flip pays one extra round-trip."""
        if not _needs_pdf(entry):
            return []

        rds = entry.get("recap_documents") or []
        # Paperless / minute-entry placeholders: the entry has recap_document
        # rows but none of them point to anything fetchable.
        if rds and not any(_is_fetchable(rd) for rd in rds):
            log.debug(
                "entry %s has %d recap_document(s) but none are fetchable "
                "(paperless / not-yet-uploaded); skipping PDF stage",
                entry.get("id"),
                len(rds),
            )
            return []

        out: list[str] = []
        for rd in rds:
            doc_id = rd.get("id")
            if not doc_id:
                continue
            if not _is_fetchable(rd):
                # Single paperless doc inside an otherwise-fetchable entry —
                # skip it but don't bail on the entry as a whole.
                continue

            text = pdf.extract_text(rd)
            if text:
                out.append(text)
            elif not rd.get("is_available"):
                log.info(
                    "recap_doc %s not yet on PACER (entry %s); will retry next sync",
                    doc_id,
                    entry.get("id"),
                )
            else:
                log.info(
                    "recap_doc %s available but text extraction yielded "
                    "nothing; install pdftoppm + tesseract for OCR fallback",
                    doc_id,
                )
        return out

    def _apply_action(
        self,
        case: CaseConfig,
        docket_id: int,
        tz: str,
        entry: dict[str, Any],
        action: dict[str, Any],
    ) -> None:
        atype = (action.get("type") or "IGNORE").upper()
        if atype == "IGNORE":
            return

        key = action.get("hearing_key")
        if not key:
            log.warning("action without hearing_key: %s", action)
            return

        # ADD_HEARING requires a date. Date-less ADD_HEARINGs come from entries
        # that anticipate a hearing without scheduling it (motion-for-hearing,
        # plea agreement, etc.) — they create ghost rows that never get a
        # starts_at_utc and never appear on the calendar. Drop them; the actual
        # scheduling order will come through later as its own entry.
        if atype == "ADD_HEARING" and not action.get("local_date"):
            log.warning(
                "skipping date-less ADD_HEARING: case=%s key=%r entry=%s "
                "(LLM should have IGNOREd; treating as such)",
                case.case_id,
                key,
                entry["id"],
            )
            return

        log.info(
            "applying %s case=%s key=%r entry=%s date=%s time=%s",
            atype,
            case.case_id,
            key,
            entry["id"],
            action.get("local_date"),
            action.get("local_time"),
        )

        existing = self.store.get_hearing(case.case_id, key)
        cross = self._is_cross_court_mutation(existing, docket_id)
        if cross:
            log.warning(
                "rejecting cross-court hearing action: case=%s key=%r "
                "existing_court=%s current_court=%s entry=%s type=%s "
                "(LLM in current court reused a key already owned by another court)",
                case.case_id,
                key,
                cross[0],
                cross[1],
                entry["id"],
                atype,
            )
            return
        eid = entry["id"]
        prev_sources = list(existing.get("source_entry_ids", [])) if existing else []
        if eid not in prev_sources:
            prev_sources.append(eid)

        # docket_id is sticky after first ADD_HEARING — sibling-docket entries can
        # touch a hearing (CANCEL_HEARING, MARK_HELD, etc.) but the hearing's
        # canonical home docket (which feeds the description's case citation
        # and CourtListener link) shouldn't drift to whichever docket touched it last.
        sticky_docket_id = existing.get("docket_id") if existing else docket_id

        if atype == "CANCEL_HEARING":
            if existing:
                merged = dict(existing)
                merged.update(
                    status="cancelled",
                    notes=action.get("notes") or existing.get("notes"),
                    source_entry_ids=prev_sources,
                    docket_id=sticky_docket_id,
                )
                self.store.upsert_hearing(merged)
            elif action.get("local_date"):
                # Hearing was never in our store (its scheduling entry was
                # filtered out by the prefilter) but a memo endorsement is
                # adjourning it. Insert a fresh row directly into
                # 'cancelled' so the audit trail captures the event.
                self._insert_terminal_hearing(
                    case,
                    docket_id,
                    tz,
                    entry,
                    action,
                    status="cancelled",
                    prev_sources=prev_sources,
                )
            else:
                log.warning(
                    "CANCEL_HEARING on unknown key with no local_date: "
                    "case=%s key=%r entry=%s — dropping",
                    case.case_id,
                    key,
                    entry["id"],
                )
            return

        if atype == "MARK_HELD":
            if existing:
                # Date-proximity validation: if the action specifies a date
                # and it's > 2 days off from the existing hearing's date,
                # the LLM probably matched the wrong key. Reject so the
                # auto-held sweep can mark the real hearing later, and so
                # we don't poison the wrong row's source list.
                if not _mark_held_date_matches(action, existing):
                    log.warning(
                        "MARK_HELD date mismatch: case=%s key=%r "
                        "existing_starts=%s action_local_date=%s — rejecting",
                        case.case_id,
                        key,
                        existing.get("starts_at_utc"),
                        action.get("local_date"),
                    )
                    return
                merged = dict(existing)
                merged.update(
                    status="held",
                    source_entry_ids=prev_sources,
                    docket_id=sticky_docket_id,
                )
                # MARK_HELD normally preserves notes. But when the triggering
                # entry is the RECORD of the proceeding (a minute entry /
                # transcript / clerk's notes of proceedings held) and the
                # existing notes don't already describe the proceeding —
                # they're empty, or a pre-hearing administrative notice that
                # merely set the hearing up — adopt the record's account so the
                # calendar shows what happened, not the setup notice (the
                # anthropic-v-dow pi-motion-hearing regression, where the notes
                # were stuck on a clerk's Zoom-access notice). Prefer the LLM's
                # own notes when it wrote any; fall back to the record entry's
                # text, since the LLM routinely omits notes on a MARK_HELD for
                # an already-held row.
                new_notes = action.get("notes") or _proceeding_notes_from_entry(entry)
                if (
                    new_notes
                    and _entry_records_proceeding(entry)
                    and not _describes_proceeding(existing.get("notes"))
                ):
                    merged["notes"] = new_notes
                self.store.upsert_hearing(merged)
            elif action.get("local_date"):
                # Same as CANCEL_HEARING-on-unknown: a minute entry for a hearing
                # we never saw scheduled. Insert directly into 'held'.
                self._insert_terminal_hearing(
                    case,
                    docket_id,
                    tz,
                    entry,
                    action,
                    status="held",
                    prev_sources=prev_sources,
                )
            else:
                log.warning(
                    "MARK_HELD on unknown key with no local_date: "
                    "case=%s key=%r entry=%s — dropping",
                    case.case_id,
                    key,
                    entry["id"],
                )
            return

        # ADD_HEARING / RESCHEDULE_HEARING / UPDATE_DETAILS — figure out the new field set.
        local_date = action.get("local_date")
        local_time = action.get("local_time")

        starts_utc = existing.get("starts_at_utc") if existing else None
        if local_date:
            # Convert using the existing tz when one is set — a reschedule
            # via a sibling docket shouldn't move the wall-clock time.
            convert_tz = (existing.get("timezone") if existing else None) or tz
            starts_utc = _local_to_utc(local_date, local_time, convert_tz)

        # 0 from the LLM means "not specified" — same as null. Falling through
        # to the default keeps zero-length blips out of subscribers' calendars.
        # We also treat an EXISTING duration of 0 as "unknown" so a follow-up
        # UPDATE_DETAILS can repair a hearing that was first inserted by an
        # entry that didn't know the duration (e.g. an ADD_HEARING whose entry didn't
        # specify length, then a later UPDATE_DETAILS that also doesn't —
        # without this, the row stays pinned at 0 forever). For date-only
        # all-day events, _default_duration with time_set=False returns 0,
        # so we still land on 0 in that case — no regression.
        duration = action.get("duration_minutes") or None
        if duration is None and existing and atype != "RESCHEDULE_HEARING":
            duration = existing.get("duration_minutes") or None
        if duration is None:
            duration = _default_duration(action.get("hearing_type"), bool(local_time))

        # Timezone is sticky after first insertion. A hearing happens in one
        # courthouse; later entries from sibling dockets in different
        # timezones (e.g. an N.D. Cal entry referencing a D.C. Cir oral
        # argument) shouldn't shift the displayed tz, especially since the
        # stored UTC was computed from the original docket's tz.
        sticky_tz = existing.get("timezone") if existing else tz

        # Significance: stickier than other fields because the LLM rarely sets
        # it on UPDATE_DETAILS / RESCHEDULE_HEARING. If the new action has it, prefer
        # that; otherwise keep what we already had.
        significance = action.get("significance") or (
            existing.get("significance") if existing else None
        )

        merged: dict[str, Any] = {
            "case_id": case.case_id,
            "hearing_key": key,
            "title": action.get("title")
            or (existing.get("title") if existing else key.replace("-", " ").title()),
            "starts_at_utc": starts_utc,
            "duration_minutes": duration,
            "timezone": sticky_tz,
            "location": action.get("location")
            or (existing.get("location") if existing else None),
            "judge": action.get("judge")
            or (existing.get("judge") if existing else None),
            "notes": action.get("notes")
            or (existing.get("notes") if existing else None),
            "dial_in": action.get("dial_in")
            or (existing.get("dial_in") if existing else None),
            "status": "scheduled",
            "significance": significance,
            "gcal_event_id": existing.get("gcal_event_id") if existing else None,
            "docket_id": sticky_docket_id,
            "source_entry_ids": prev_sources,
        }
        self.store.upsert_hearing(merged)

    # --- deadlines ---

    def _apply_deadline_action(
        self,
        case: CaseConfig,
        docket_id: int,
        tz: str,
        entry: dict[str, Any],
        action: dict[str, Any],
    ) -> None:
        atype = (action.get("type") or "").upper()
        key = action.get("deadline_key")
        if not key:
            log.warning("deadline action without deadline_key: %s", action)
            return

        # ADD_DEADLINE with no date is allowed ONLY when the LLM explicitly
        # marks it conditional (deadline runs from an unknown future event,
        # e.g. "21 days after resolution of [related case]"). The row is
        # persisted with due_at_utc=NULL so the renderers skip it (no fake
        # calendar entry) while the summary scaffold still surfaces the
        # verbatim court text from `notes`. A date-less ADD_DEADLINE without
        # the conditional flag is a motion-anticipating-a-deadline that the
        # LLM should have IGNOREd — drop it.
        if atype == "ADD_DEADLINE" and not action.get("local_date"):
            if not action.get("conditional"):
                log.warning(
                    "skipping date-less ADD_DEADLINE: case=%s key=%r entry=%s "
                    "(LLM should have IGNOREd or marked conditional)",
                    case.case_id,
                    key,
                    entry["id"],
                )
                return
            log.info(
                "applying conditional ADD_DEADLINE: case=%s key=%r entry=%s notes=%r",
                case.case_id,
                key,
                entry["id"],
                (action.get("notes") or "")[:120],
            )

        log.info(
            "applying %s case=%s key=%r entry=%s date=%s",
            atype,
            case.case_id,
            key,
            entry["id"],
            action.get("local_date"),
        )

        existing = self.store.get_deadline(case.case_id, key)
        cross = self._is_cross_court_mutation(existing, docket_id)
        if cross:
            log.warning(
                "rejecting cross-court deadline action: case=%s key=%r "
                "existing_court=%s current_court=%s entry=%s type=%s "
                "(LLM in current court reused a key already owned by another court)",
                case.case_id,
                key,
                cross[0],
                cross[1],
                entry["id"],
                atype,
            )
            return
        eid = entry["id"]
        prev_sources = list(existing.get("source_entry_ids", [])) if existing else []
        if eid not in prev_sources:
            prev_sources.append(eid)
        sticky_docket_id = existing.get("docket_id") if existing else docket_id
        sticky_tz = existing.get("timezone") if existing else tz

        if atype == "CANCEL_DEADLINE":
            if existing:
                merged = dict(existing)
                merged.update(
                    status="cancelled",
                    notes=action.get("notes") or existing.get("notes"),
                    source_entry_ids=prev_sources,
                    docket_id=sticky_docket_id,
                )
                self.store.upsert_deadline(merged)
            elif action.get("local_date"):
                # Same fallback as hearings: insert a fresh row directly into
                # 'cancelled' so the audit trail captures the vacatur.
                due_at_utc = _deadline_local_to_utc(
                    action["local_date"], action.get("local_time"), tz
                )
                self.store.upsert_deadline(
                    {
                        "case_id": case.case_id,
                        "deadline_key": key,
                        "title": action.get("title") or key.replace("-", " ").title(),
                        "due_at_utc": due_at_utc,
                        "timezone": tz,
                        "notes": action.get("notes"),
                        "status": "cancelled",
                        "significance": action.get("significance") or "major",
                        "deadline_type": action.get("deadline_type"),
                        "docket_id": docket_id,
                        "source_entry_ids": prev_sources,
                    }
                )
            else:
                log.warning(
                    "CANCEL_DEADLINE on unknown key with no local_date: "
                    "case=%s key=%r entry=%s — dropping",
                    case.case_id,
                    key,
                    entry["id"],
                )
            return

        if atype == "MARK_FILED":
            if existing:
                merged = dict(existing)
                merged.update(
                    status="met",
                    source_entry_ids=prev_sources,
                    docket_id=sticky_docket_id,
                )
                self.store.upsert_deadline(merged)
            else:
                log.info(
                    "MARK_FILED on unknown key (no row to update): "
                    "case=%s key=%r entry=%s",
                    case.case_id,
                    key,
                    entry["id"],
                )
            return

        # ADD_DEADLINE / RESCHEDULE_DEADLINE
        due_at_utc = existing.get("due_at_utc") if existing else None
        if action.get("local_date"):
            # Convert via the existing row's tz (sticky) so a sibling-docket
            # extension doesn't shift the deadline's wall-clock by tz.
            convert_tz = (existing.get("timezone") if existing else None) or tz
            due_at_utc = _deadline_local_to_utc(
                action["local_date"], action.get("local_time"), convert_tz
            )
        elif atype == "ADD_DEADLINE" and action.get("conditional"):
            # Explicitly conditional — clear any prior date so the renderer
            # skips this row and the summary scaffold surfaces the verbatim
            # court text in `notes` instead of an estimated date.
            due_at_utc = None
        significance = action.get("significance") or (
            existing.get("significance") if existing else None
        )
        merged: dict[str, Any] = {
            "case_id": case.case_id,
            "deadline_key": key,
            "title": action.get("title")
            or (existing.get("title") if existing else key.replace("-", " ").title()),
            "due_at_utc": due_at_utc,
            "timezone": sticky_tz,
            "notes": action.get("notes")
            or (existing.get("notes") if existing else None),
            "status": "pending",
            "significance": significance,
            "deadline_type": action.get("deadline_type")
            or (existing.get("deadline_type") if existing else None),
            "gcal_event_id": existing.get("gcal_event_id") if existing else None,
            "docket_id": sticky_docket_id,
            "source_entry_ids": prev_sources,
        }
        self.store.upsert_deadline(merged)
