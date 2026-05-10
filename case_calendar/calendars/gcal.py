"""Google Calendar sync.

Auth follows the standard OAuth-installed-app flow from Google's quickstart.
First run prompts the user in a browser; tokens are cached in
``~/.case-calendar/google-token.json`` thereafter.

We sync per calendar: each hearing has a stable client-side ID derived from
``case_id`` + ``hearing_key``; we PATCH that ID on every sync, so reschedules
and detail updates are idempotent.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, cast
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ..courts import DEFAULT_TZ
from .description import build as build_description

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _gcal_id(case_id: str, hearing_key: str) -> str:
    # Google calendar event IDs accept [a-v0-9]{5,1024}. Hash and base32-ish.
    raw = f"{case_id}::{hearing_key}".encode()
    h = hashlib.sha1(raw).hexdigest()  # 40 chars, [0-9a-f]
    # Convert hex letters into a-f (already valid) — so id matches [a-v0-9].
    return f"cc{h}"


def _to_rfc3339(iso_utc: str) -> str:
    dt = datetime.fromisoformat(iso_utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_local_rfc3339(iso_utc: str, tz: str) -> str:
    """Convert a stored UTC timestamp to a Google Calendar dateTime expressed
    in the target IANA timezone (no Z suffix, no offset)."""
    dt = datetime.fromisoformat(iso_utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%dT%H:%M:%S")


class GoogleCalendarSync:
    def __init__(
        self,
        *,
        credentials_path: str,
        token_path: str | Path = "~/.case-calendar/google-token.json",
    ):
        self.credentials_path = Path(credentials_path).expanduser()
        self.token_path = Path(token_path).expanduser()
        self.service = self._build_service()

    def _build_service(self):
        creds: Credentials | None = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(
                str(self.token_path), SCOPES
            )
        if creds and creds.valid:
            return build("calendar", "v3", credentials=creds, cache_discovery=False)

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.credentials_path), SCOPES
            )
            creds = cast(Credentials, flow.run_local_server(port=0))

        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json())
        return build("calendar", "v3", credentials=creds, cache_discovery=False)

    def sync(
        self,
        *,
        calendar_id: str,
        hearings: Iterable[dict],
        send_updates: str = "externalOnly",
    ) -> None:
        for h in hearings:
            if not h.get("starts_at_utc"):
                continue
            if h.get("significance") == "minor":
                # Procedural-only events (phone calls to grant a continuance,
                # scheduling-only conferences) stay in the DB for audit but
                # are kept off the user-facing calendar.
                continue
            if h.get("status") == "cancelled":
                # Cancelled trials (e.g. via a plea) are no longer events
                # of record — the plea hearing or rescheduled trial lives
                # on its own row. Issue a delete/cancel against any prior
                # gcal event so subscribers don't see a stale entry, and
                # skip the upsert.
                self._cancel_if_present(calendar_id, h, send_updates=send_updates)
                continue
            self._upsert(calendar_id, h, send_updates=send_updates)

    def _upsert(
        self, calendar_id: str, h: dict, *, send_updates: str = "externalOnly"
    ) -> None:
        eid = _gcal_id(h["case_id"], h["hearing_key"])
        body = self._event_body(eid, h)
        try:
            self.service.events().patch(
                calendarId=calendar_id, eventId=eid, body=body,
                sendUpdates=send_updates,
            ).execute()
            log.info("patched %s on %s", eid, calendar_id)
        except HttpError as e:
            if e.resp.status == 404:
                # Create with the same id so future patches are idempotent.
                body["id"] = eid
                self.service.events().insert(
                    calendarId=calendar_id, body=body,
                    sendUpdates=send_updates,
                ).execute()
                log.info("created %s on %s", eid, calendar_id)
            else:
                raise

    def _cancel_if_present(
        self, calendar_id: str, h: dict, *, send_updates: str = "externalOnly"
    ) -> None:
        """Mark an existing gcal event as cancelled (so subscribers stop
        seeing it) and noop if the event was never created — useful for
        rows that flipped to status='cancelled' after a prior render and
        for rows that were always cancelled and don't need creating just
        to be cancelled."""
        eid = _gcal_id(h["case_id"], h["hearing_key"])
        try:
            self.service.events().patch(
                calendarId=calendar_id, eventId=eid,
                body={"status": "cancelled"},
                sendUpdates=send_updates,
            ).execute()
            log.info("cancelled %s on %s", eid, calendar_id)
        except HttpError as e:
            if e.resp.status == 404:
                return  # never existed, nothing to cancel
            raise

    @staticmethod
    def _event_body(eid: str, h: dict) -> dict:
        title = h["title"]
        status = "confirmed"
        no_time = not (h.get("duration_minutes") and h["duration_minutes"] > 0)

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

        body: dict = {
            "summary": title,
            "description": description,
            "location": h.get("location") or None,
            "status": status,
        }
        if no_time:
            # Date-only hearings shouldn't mark the user busy for the whole
            # day; the actual hearing slot is unknown but specific.
            body["transparency"] = "transparent"

        # Optional notification config — emails added as attendees so that
        # arbitrary addresses get the invite + see the event on their own
        # calendars (Google reminders only fire for the calendar owner).
        notify_emails = h.get("notify_emails") or []
        if notify_emails:
            body["attendees"] = [{"email": e} for e in notify_emails]

        # Per-event reminder overrides (owner-only — for popup or for email
        # to the calendar owner). E.g. [{"method": "popup", "minutes": 30}].
        reminders = h.get("reminders")
        if reminders:
            body["reminders"] = {"useDefault": False, "overrides": reminders}

        if h.get("duration_minutes") and h["duration_minutes"] > 0:
            tz = h.get("timezone") or DEFAULT_TZ
            start_dt = datetime.fromisoformat(h["starts_at_utc"])
            end_dt = start_dt + timedelta(minutes=h["duration_minutes"])
            # Send local time + court tz so Google preserves the "this is a
            # 3 PM Pacific hearing" semantics across DST boundaries and
            # viewer-tz displays.
            body["start"] = {
                "dateTime": _to_local_rfc3339(h["starts_at_utc"], tz),
                "timeZone": tz,
            }
            body["end"] = {
                "dateTime": end_dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": tz,
            }
        else:
            tz = h.get("timezone") or DEFAULT_TZ
            d = (
                datetime.fromisoformat(h["starts_at_utc"])
                .astimezone(ZoneInfo(tz))
                .date()
                .isoformat()
            )
            next_day = (
                datetime.fromisoformat(d) + timedelta(days=1)
            ).date().isoformat()
            body["start"] = {"date": d}
            body["end"] = {"date": next_day}

        return body
