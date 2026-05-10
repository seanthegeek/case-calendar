"""Generate ICS calendar files from the hearing store.

ICS is the path that works with any calendar app, including Proton's
"subscribe to URL" feature, so it's the primary output. We hand-build the
file rather than depending on the ``ics`` library so we have full control
over UIDs (which must be stable across regenerations) and tz tagging.

Timezone handling: we emit ``DTSTART;TZID=<IANA tz>:<local time>`` and
rely on the receiving calendar app's bundled IANA tz database to resolve
DST. All modern apps (Google, Apple, Proton, Outlook 2019+, Thunderbird)
accept bare IANA TZIDs without a VTIMEZONE block, and the tz database
itself ships with the Python ``tzdata`` dependency so our local conversion
is always correct.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..courts import DEFAULT_TZ
from .description import build as build_description


def _tz_is_known(tz: str) -> bool:
    """Cheap check that tzdata recognizes the IANA name. Falls back to UTC
    if the name is bogus rather than letting datetime crash later."""
    try:
        ZoneInfo(tz)
        return True
    except ZoneInfoNotFoundError:
        return False


def _fmt_local(iso_utc: str, tz: str) -> str:
    """Convert a stored UTC ISO timestamp to a local DATE-TIME for use under
    a TZID parameter, e.g. ``20260414T150000``."""
    dt = datetime.fromisoformat(iso_utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(tz)).strftime("%Y%m%dT%H%M%S")


def _fmt_date(iso: str, tz: str) -> str:
    dt = datetime.fromisoformat(iso).astimezone(ZoneInfo(tz))
    return dt.strftime("%Y%m%d")


def _escape(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545 line folding (75 octets per line)."""
    out = []
    while len(line.encode("utf-8")) > 75:
        # Find a safe split point that keeps multi-byte chars intact.
        i = 1
        while len(line[:i].encode("utf-8")) <= 75:
            i += 1
        i -= 1
        out.append(line[:i])
        line = " " + line[i:]
    out.append(line)
    return "\r\n".join(out)


def render_ics(*, calendar_name: str, hearings: Iterable[dict]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//case-calendar//EN",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_escape(calendar_name)}",
    ]
    for h in hearings:
        if not h.get("starts_at_utc"):
            continue  # nothing to put on a calendar without a date
        if h.get("significance") == "minor":
            # Procedural-only events (phone calls to grant a continuance,
            # scheduling-only conferences) are stored for the audit trail
            # but kept off the calendar. The user only wants major moments
            # — substantive proceedings and dialable events.
            continue
        if h.get("status") == "cancelled":
            # A trial cancelled by a plea (or any other cancellation) is
            # not happening; the new event of record (the plea hearing,
            # the rescheduled trial, etc.) lives on its own row. Keeping
            # cancelled events on subscribers' calendars is just noise.
            continue
        uid = f"{h['case_id']}--{h['hearing_key']}@case-calendar"
        title = h["title"]
        no_time = not (h.get("duration_minutes") and h["duration_minutes"] > 0)

        location = h.get("location") or ""

        description = build_description(
            notes=h.get("notes"),
            dial_in=h.get("dial_in"),
            docket_number=h.get("docket_number"),
            court_citation=h.get("court_citation"),
            docket_absolute_url=h.get("docket_absolute_url"),
            source_entry_ids=h.get("source_entry_ids"),
            docket_entry_numbers=h.get("docket_entry_numbers"),
            judge=h.get("judge"),
        )

        lines.append("BEGIN:VEVENT")
        lines.append(_fold(f"UID:{uid}"))
        lines.append(f"DTSTAMP:{now}")
        lines.append(_fold(f"SUMMARY:{_escape(title)}"))
        if location:
            lines.append(_fold(f"LOCATION:{_escape(location)}"))
        if description:
            lines.append(_fold(f"DESCRIPTION:{_escape(description)}"))

        if h.get("duration_minutes") and h["duration_minutes"] > 0:
            tz = h.get("timezone") or DEFAULT_TZ
            start_dt = datetime.fromisoformat(h["starts_at_utc"])
            end_dt = start_dt + timedelta(minutes=h["duration_minutes"])
            if _tz_is_known(tz):
                # Local-time + IANA TZID. The receiving calendar resolves
                # DST against its bundled tz database; viewer's app shows
                # the event in the viewer's tz while the event preserves
                # "this is a 3 PM Pacific hearing" semantics.
                start = _fmt_local(h["starts_at_utc"], tz)
                end_local = end_dt.astimezone(ZoneInfo(tz)).strftime("%Y%m%dT%H%M%S")
                lines.append(f"DTSTART;TZID={tz}:{start}")
                lines.append(f"DTEND;TZID={tz}:{end_local}")
            else:
                # Bogus tz string — fall back to UTC so the event still
                # renders correctly (just without court-tz context).
                start = start_dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                end = end_dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                lines.append(f"DTSTART:{start}")
                lines.append(f"DTEND:{end}")
        else:
            # All-day event in court-local TZ.
            tz = h.get("timezone") or DEFAULT_TZ
            d = _fmt_date(h["starts_at_utc"], tz)
            next_day = (
                datetime.fromisoformat(h["starts_at_utc"]).astimezone(ZoneInfo(tz))
                + timedelta(days=1)
            ).strftime("%Y%m%d")
            lines.append(f"DTSTART;VALUE=DATE:{d}")
            lines.append(f"DTEND;VALUE=DATE:{next_day}")

        if h["status"] == "cancelled":
            lines.append("STATUS:CANCELLED")
        elif h["status"] == "held":
            lines.append("STATUS:CONFIRMED")
        else:
            lines.append("STATUS:CONFIRMED")

        # Date-only hearings shouldn't block the user's whole day — the
        # hearing has a SPECIFIC time we don't know yet. TRANSP:TRANSPARENT
        # keeps the event visible in the calendar while marking it as free
        # (most clients render with an outline / muted style rather than a
        # solid block).
        if no_time:
            lines.append("TRANSP:TRANSPARENT")

        for email in h.get("notify_emails") or []:
            lines.append(_fold(f"ATTENDEE;RSVP=TRUE:mailto:{email}"))

        # VALARM blocks for reminders. Most calendar apps respect DISPLAY
        # (popup); ACTION:EMAIL is hit-or-miss in clients.
        for r in h.get("reminders") or []:
            method = (r.get("method") or "popup").lower()
            mins = int(r.get("minutes") or 0)
            if mins <= 0:
                continue
            lines.append("BEGIN:VALARM")
            lines.append(f"TRIGGER:-PT{mins}M")
            if method == "email":
                lines.append("ACTION:EMAIL")
                lines.append(f"DESCRIPTION:{_escape(title)}")
                lines.append(f"SUMMARY:Reminder: {_escape(title)}")
                for email in h.get("notify_emails") or []:
                    lines.append(_fold(f"ATTENDEE:mailto:{email}"))
            else:
                lines.append("ACTION:DISPLAY")
                lines.append(f"DESCRIPTION:{_escape(title)}")
            lines.append("END:VALARM")

        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def write_ics(path: str | Path, calendar_name: str, hearings: Iterable[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_ics(calendar_name=calendar_name, hearings=hearings))
