"""Shared event-description builder.

Both the ICS and Google Calendar outputs assemble the event body from the
same fields, so the formatting lives here. Each event gets, in order:

  * notes (free-form, from the LLM — the actual event description)
  * the case's tags (topical labels from the YAML config), rendered as a
    comma-separated ``Tags:`` line. Sits right under the description so a
    subscriber scanning their calendar sees the topic before the
    docket-keeping metadata that follows.
  * judge or appellate panel
  * dial-in / video link
  * case citation: "<docket_number> (<court citation>)"
  * link to the CourtListener docket page
  * direct URLs to each attached document on the source docket entries
    (IA mirror preferred, CourtListener storage fallback) so subscribers can open the
    filing without re-navigating the docket. Sealed / not-yet-uploaded docs
    show their status instead of a URL.
  * the list of source PACER docket entry numbers (what subscribers see in
    the CourtListener UI — "[65]")
  * the list of source CourtListener entry IDs (audit trail; the docket URL
    is one click away for anyone who wants the raw prose)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

CL_BASE = "https://www.courtlistener.com"


def no_time_title_prefix(
    starts_at_utc: Optional[str], *, now: Optional[datetime] = None
) -> str:
    """Title prefix for hearings without a known clock time.

    Date-only events render as all-day, but the title still tells the
    subscriber whether the time is "still to be set" (future) or "we never
    learned what time this happened" (past). "TBD" is wrong on past dates.
    """
    if not starts_at_utc:
        return "[time unknown]"
    try:
        when = datetime.fromisoformat(starts_at_utc)
    except ValueError:
        return "[time unknown]"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    return "[time unknown]" if when < now else "[time TBD]"


def is_calendar_visible(h: dict) -> bool:
    """Filter applied at the top of every backend's render loop.

    A row is calendar-visible when it has a start timestamp AND its
    significance isn't ``minor``. ``cancelled`` rows are deliberately
    NOT filtered here — each backend handles them differently (ICS
    skips, gcal patches with ``status=cancelled``, m365 issues a
    DELETE) — so the cancellation check stays in the backend bodies.
    """
    if not h.get("starts_at_utc"):
        return False
    if h.get("significance") == "minor":
        return False
    return True


def has_no_time(h: dict) -> bool:
    """Date-only hearing — no specific clock time was scheduled.

    Renderers use this to switch between a timed VEVENT and an all-day
    or 9-5 fallback. A row is date-only when its duration is zero or
    missing — the LLM emits ``duration_minutes`` only on rows that
    carry an actual time.
    """
    return not (h.get("duration_minutes") and h["duration_minutes"] > 0)


def build_description_for_row(h: dict) -> str:
    """Build a description string from a hearing/deadline row dict.

    Wraps :func:`build` so each backend (ICS, gcal, m365) doesn't have
    to repeat the same 9-key ``h.get(...)`` argument list — adding a
    new field to event descriptions is now a one-file change here
    instead of touching every backend in lockstep.
    """
    return build(
        notes=h.get("notes"),
        dial_in=h.get("dial_in"),
        docket_number=h.get("docket_number"),
        court_citation=h.get("court_citation"),
        docket_absolute_url=h.get("docket_absolute_url"),
        source_entry_ids=h.get("source_entry_ids"),
        docket_entry_numbers=h.get("docket_entry_numbers"),
        judge=h.get("judge"),
        documents=h.get("documents"),
        tags=h.get("tags"),
    )


def build(
    *,
    notes: Optional[str],
    dial_in: Optional[str],
    docket_number: Optional[str],
    court_citation: Optional[str],
    docket_absolute_url: Optional[str],
    source_entry_ids: Iterable[int] | None,
    docket_entry_numbers: Iterable[int] | None = None,
    judge: Optional[str] = None,
    documents: Iterable[dict] | None = None,
    tags: Iterable[str] | None = None,
) -> str:
    parts: list[str] = []

    if notes:
        parts.append(notes)

    tag_list = [t.strip() for t in (tags or []) if isinstance(t, str) and t.strip()]
    if tag_list:
        parts.append("Tags: " + ", ".join(tag_list))

    if judge:
        # "Panel:" reads naturally for an appellate bench (comma-separated
        # names); "Judge:" for a single trial-court judge.
        label = "Panel" if "," in judge else "Judge"
        parts.append(f"{label}: {judge}")

    if dial_in:
        parts.append(f"Dial-in / link:\n{dial_in}")

    citation_bits = []
    if docket_number:
        citation_bits.append(docket_number)
    if court_citation:
        citation_bits.append(f"({court_citation})")
    if citation_bits:
        parts.append("Case: " + " ".join(citation_bits))

    if docket_absolute_url:
        url = (
            docket_absolute_url
            if docket_absolute_url.startswith("http")
            else f"{CL_BASE}{docket_absolute_url}"
        )
        parts.append(f"Docket: {url}")

    doc_lines = _document_lines(documents)
    if doc_lines:
        parts.append("Documents:\n" + "\n".join(doc_lines))

    nums = list(docket_entry_numbers or [])
    if nums:
        parts.append("Docket entries: " + ", ".join(str(n) for n in nums))

    ids = list(source_entry_ids or [])
    if ids:
        parts.append("CourtListener entry IDs: " + ", ".join(str(i) for i in ids))

    return "\n\n".join(parts)


def _document_lines(documents: Iterable[dict] | None) -> list[str]:
    """Render one line per attached document.

    Format: "65: https://..." for the main doc, "65-1: https://..." for
    attachment 1. Sealed and not-yet-uploaded docs show their status in
    place of a URL — the row is still listed so subscribers can see the
    document was filed even if they can't open it yet.
    """
    lines: list[str] = []
    for d in documents or []:
        label = _document_label(d)
        if not label:
            continue
        if d.get("is_sealed"):
            lines.append(f"{label}: (sealed)")
            continue
        url = _document_url(d)
        if url:
            lines.append(f"{label}: {url}")
            continue
        if not d.get("is_available"):
            lines.append(f"{label}: (not yet uploaded to RECAP)")
    return lines


def _document_label(d: dict) -> Optional[str]:
    """`65` for the main doc; `65-1` for attachment 1 on entry 65."""
    docnum = d.get("document_number")
    if docnum in (None, ""):
        return None
    att = d.get("attachment_number")
    try:
        att_n = int(att) if att not in (None, "") else 0
    except (TypeError, ValueError):
        att_n = 0
    return f"{docnum}-{att_n}" if att_n else str(docnum)


def _document_url(d: dict) -> Optional[str]:
    """Prefer IA mirror (public, stable); fall back to CourtListener storage."""
    ia = d.get("filepath_ia")
    if ia:
        return ia
    fp = d.get("filepath_local")
    if fp:
        return f"https://storage.courtlistener.com/{fp}"
    return None
