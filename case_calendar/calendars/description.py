"""Shared event-description builder.

Both the ICS and Google Calendar outputs assemble the event body from the
same fields, so the formatting lives here. Each event gets:

  * notes (free-form, from the LLM)
  * judge or appellate panel
  * dial-in / video link
  * case citation: "<docket_number> (<court citation>)"
  * link to the CourtListener docket page
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


def build(
    *,
    notes: Optional[str],
    dial_in: Optional[str],
    docket_number: Optional[str],
    court_citation: Optional[str],
    docket_absolute_url: Optional[str],
    source_entry_ids: Iterable[int] | None,
    judge: Optional[str] = None,
) -> str:
    parts: list[str] = []

    if notes:
        parts.append(notes)

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

    ids = list(source_entry_ids or [])
    if ids:
        parts.append("CourtListener entry IDs: " + ", ".join(str(i) for i in ids))

    return "\n\n".join(parts)
