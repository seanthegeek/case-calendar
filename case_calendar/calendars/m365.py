"""Microsoft 365 / Outlook calendar sync via Microsoft Graph.

Auth: ``azure-identity``'s ``InteractiveBrowserCredential`` with a
``TokenCachePersistenceOptions`` keyring-backed cache and a serialized
``AuthenticationRecord``. First run prompts a browser; subsequent runs
(including the unattended daemon) load the record back and refresh
silently. The daemon path uses ``disable_automatic_authentication=True``
so a missing/invalid cache fails fast instead of trying to open a browser
on a server with no display.

Idempotency: Graph generates the event id server-side, unlike Google
Calendar where we control the id. We persist the server id on the
``hearings`` / ``deadlines`` row as ``m365_event_id`` so subsequent
syncs patch in place. As a automatic-recovery fallback (e.g. after a DB
restore) we also stamp every event with a single-value extended
property keyed to ``case_id::hearing_key``, so we can recover the
server id by ``$filter`` query when the cache is stale.

Calendars: pushes to the user's default calendar by default; set
``m365_calendar_id`` on a calendar config to route to a specific
Outlook calendar.

Cancellations: Graph's DELETE on an organizer's event already sends a
cancellation message to attendees, so there's no separate cancel verb.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from ..courts import DEFAULT_TZ
from .description import build as build_description

log = logging.getLogger(__name__)

GRAPH_SCOPES = ["https://graph.microsoft.com/Calendars.ReadWrite"]
TOKEN_CACHE_NAME = "case-calendar"

# Stable extended-property GUID and name. Pick one and never change them —
# rotating the GUID would orphan every previously-pushed event from the
# `$filter` recovery path.
_EXT_PROP_GUID = "8a6ff1f8-2a8a-4a5f-9d3a-c5f0e9bb4cdb"
_EXT_PROP_NAME = "CaseCalendarKey"
_EXT_PROP_ID = f"String {{{_EXT_PROP_GUID}}} Name {_EXT_PROP_NAME}"


def _correlation_key(case_id: str, hearing_key: str) -> str:
    return hashlib.sha1(f"{case_id}::{hearing_key}".encode()).hexdigest()


class M365CalendarSync:
    """Pushes hearings + deadlines to a Microsoft 365 / Outlook calendar.

    Mirrors the surface of :class:`GoogleCalendarSync` — same ``sync(...)``
    entry point, same significance / cancellation handling — so callers
    flow either renderer through ``cli.emit_calendars`` without branching.
    """

    def __init__(
        self,
        *,
        client_id: str,
        token_path: str | Path = "tokens/m365-token.json",
    ):
        # Local imports so the SDK only loads when M365 push is actually
        # configured. Keeps cold-start cost off the polling-only / ICS-only
        # paths and avoids forcing every user to install the dependency tree.
        from azure.identity import (
            AuthenticationRecord,
            InteractiveBrowserCredential,
            TokenCachePersistenceOptions,
        )
        from msgraph import GraphServiceClient

        self._AuthenticationRecord = AuthenticationRecord
        self._InteractiveBrowserCredential = InteractiveBrowserCredential
        self._TokenCachePersistenceOptions = TokenCachePersistenceOptions
        self.client_id = client_id
        self.token_path = Path(token_path).expanduser()
        self.credential = self._build_credential()
        self.client = GraphServiceClient(
            credentials=self.credential, scopes=GRAPH_SCOPES,
        )

    def _build_credential(self):
        cache_opts = self._TokenCachePersistenceOptions(name=TOKEN_CACHE_NAME)
        if self.token_path.exists():
            record = self._AuthenticationRecord.deserialize(
                self.token_path.read_text()
            )
            # Daemon path: silent refresh only. If the cache is stale the
            # caller gets a clean error instead of a hung browser prompt.
            return self._InteractiveBrowserCredential(
                client_id=self.client_id,
                cache_persistence_options=cache_opts,
                authentication_record=record,
                disable_automatic_authentication=True,
            )
        # First-run: interactive. The operator runs ``case-calendar setup
        # m365`` once to stage the AuthenticationRecord; the daemon path
        # then reads the record back and refreshes silently.
        cred = self._InteractiveBrowserCredential(
            client_id=self.client_id,
            cache_persistence_options=cache_opts,
        )
        record = cred.authenticate(scopes=GRAPH_SCOPES)
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(record.serialize())
        log.info("staged M365 auth record at %s", self.token_path)
        return cred

    # --- public surface ---

    def sync(
        self,
        *,
        hearings: Iterable[dict],
        store=None,
        calendar_id: Optional[str] = None,
    ) -> None:
        """Render every hearing-shaped row to the M365 calendar.

        ``store`` is optional; when supplied we cache the server-assigned
        event id back on the hearings/deadlines row so subsequent syncs
        skip the lookup. ``calendar_id`` routes to a specific calendar;
        omit to push to the user's default calendar.
        """
        asyncio.run(self._sync_async(
            hearings=list(hearings), store=store, calendar_id=calendar_id,
        ))

    async def _sync_async(
        self, *, hearings: list[dict], store, calendar_id: Optional[str],
    ) -> None:
        for h in hearings:
            if h.get("significance") == "minor":
                # Procedural-only events (phone calls about scheduling
                # motions) stay in the DB for audit but are kept off the
                # user-facing calendar.
                continue
            if not h.get("starts_at_utc"):
                continue
            if h.get("status") == "cancelled":
                # Cancelled rows: delete from the calendar so subscribers
                # stop seeing them. Graph's DELETE already mails an
                # attendee cancellation, no separate verb needed.
                await self._delete_if_present(h, calendar_id, store)
                continue
            await self._upsert(h, calendar_id, store)

    # --- per-event helpers ---

    async def _upsert(
        self, h: dict, calendar_id: Optional[str], store,
    ) -> None:
        body = self._event_body(h)
        events_rb = self._events_rb(calendar_id)
        cached_id = h.get("m365_event_id")

        if cached_id:
            try:
                await events_rb.by_event_id(cached_id).patch(body)
                log.info("patched %s on %s", cached_id, calendar_id or "(default)")
                return
            except Exception as e:
                if not _is_404(e):
                    raise
                log.info(
                    "cached m365 id %s gone (deleted on calendar?); falling back",
                    cached_id,
                )

        # Recover the server id by extended-property lookup before
        # inserting, so a wiped DB doesn't orphan every prior event.
        recovered = await self._find_by_correlation(h, calendar_id)
        if recovered:
            await events_rb.by_event_id(recovered).patch(body)
            self._cache_id(store, h, recovered)
            log.info("recovered+patched %s on %s", recovered, calendar_id or "(default)")
            return

        created = await events_rb.post(body)
        new_id = getattr(created, "id", None)
        if new_id is None:
            raise RuntimeError("Graph create returned no event id")
        self._cache_id(store, h, new_id)
        log.info("created %s on %s", new_id, calendar_id or "(default)")

    async def _delete_if_present(
        self, h: dict, calendar_id: Optional[str], store,
    ) -> None:
        events_rb = self._events_rb(calendar_id)
        target_id = h.get("m365_event_id") or await self._find_by_correlation(
            h, calendar_id,
        )
        if not target_id:
            return  # never created, nothing to cancel
        try:
            await events_rb.by_event_id(target_id).delete()
            log.info("deleted %s on %s", target_id, calendar_id or "(default)")
        except Exception as e:
            if _is_404(e):
                return
            raise
        # Clear the cached id — the event is gone and a future revival
        # of this row should create a fresh event rather than 404 against
        # a deleted id.
        self._cache_id(store, h, None)

    async def _find_by_correlation(
        self, h: dict, calendar_id: Optional[str],
    ) -> Optional[str]:
        """Look up the Graph event id by our ``CaseCalendarKey`` extended
        property. Returns None when no event has the property set —
        either because it was never created, or because an older release
        created it without the property and we should treat it as new.
        """
        from kiota_abstractions.base_request_configuration import (
            RequestConfiguration,
        )
        from msgraph.generated.users.item.calendars.item.events.events_request_builder import (
            EventsRequestBuilder as CalEventsRB,
        )
        from msgraph.generated.users.item.events.events_request_builder import (
            EventsRequestBuilder as MeEventsRB,
        )

        key = _correlation_key(h["case_id"], h["hearing_key"])
        filter_expr = (
            f"singleValueExtendedProperties/any("
            f"ep: ep/id eq '{_EXT_PROP_ID}' and ep/value eq '{key}')"
        )
        events_rb = self._events_rb(calendar_id)
        if calendar_id:
            qp = CalEventsRB.EventsRequestBuilderGetQueryParameters(
                filter=filter_expr, top=1,
            )
        else:
            qp = MeEventsRB.EventsRequestBuilderGetQueryParameters(
                filter=filter_expr, top=1,
            )
        config = RequestConfiguration(query_parameters=qp)
        try:
            result = await events_rb.get(request_configuration=config)
        except Exception as e:
            if _is_404(e):
                return None
            raise
        rows = getattr(result, "value", None) or []
        if not rows:
            return None
        return getattr(rows[0], "id", None)

    def _events_rb(self, calendar_id: Optional[str]):
        # Unifies the "default calendar" and "specific calendar" fluent
        # paths so the rest of the class doesn't branch.
        me = self.client.me
        if calendar_id:
            return me.calendars.by_calendar_id(calendar_id).events
        return me.events

    @staticmethod
    def _cache_id(store, h: dict, m365_id: Optional[str]) -> None:
        if store is None:
            return
        # Deadlines arrive here pre-mapped via cli._deadline_to_hearing,
        # which namespaces their hearing_key as "deadline:<key>". Use
        # that prefix to route the cache write to the correct table —
        # avoids plumbing a separate kind field through the renderer
        # pipeline.
        key = h["hearing_key"]
        if key.startswith("deadline:"):
            store.set_m365_id_for_deadline(
                h["case_id"], key[len("deadline:"):], m365_id,
            )
        else:
            store.set_m365_id_for_hearing(h["case_id"], key, m365_id)

    # --- event body builder ---

    def _event_body(self, h: dict) -> Any:
        from msgraph.generated.models.attendee import Attendee
        from msgraph.generated.models.attendee_type import AttendeeType
        from msgraph.generated.models.body_type import BodyType
        from msgraph.generated.models.date_time_time_zone import (
            DateTimeTimeZone,
        )
        from msgraph.generated.models.email_address import EmailAddress
        from msgraph.generated.models.event import Event
        from msgraph.generated.models.item_body import ItemBody
        from msgraph.generated.models.single_value_legacy_extended_property import (
            SingleValueLegacyExtendedProperty,
        )

        no_time = not (h.get("duration_minutes") and h["duration_minutes"] > 0)
        tz = h.get("timezone") or DEFAULT_TZ
        description = build_description(
            notes=h.get("notes"),
            dial_in=h.get("dial_in"),
            docket_number=h.get("docket_number"),
            court_citation=h.get("court_citation"),
            docket_absolute_url=h.get("docket_absolute_url"),
            source_entry_ids=h.get("source_entry_ids"),
            docket_entry_numbers=h.get("docket_entry_numbers"),
            judge=h.get("judge"),
            documents=h.get("documents"),
        )

        start_dt = datetime.fromisoformat(h["starts_at_utc"])
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if no_time:
            # Date-only fallback: render as a 9–5 court-tz block. Outlook
            # has no direct all-day toggle that preserves a non-UTC
            # tz cleanly across DST, and a 24-hour block competes
            # visually with timed rows. The "[time TBD]" / "[time
            # unknown]" prefix on the title carries the real semantics.
            local_date = start_dt.astimezone(ZoneInfo(tz)).date().isoformat()
            start_local = f"{local_date}T09:00:00"
            end_local = f"{local_date}T17:00:00"
        else:
            end_dt = start_dt + timedelta(minutes=h["duration_minutes"])
            start_local = start_dt.astimezone(ZoneInfo(tz)).strftime(
                "%Y-%m-%dT%H:%M:%S",
            )
            end_local = end_dt.astimezone(ZoneInfo(tz)).strftime(
                "%Y-%m-%dT%H:%M:%S",
            )

        attendees = []
        for addr in h.get("notify_emails") or []:
            attendees.append(Attendee(
                email_address=EmailAddress(address=addr),
                type=AttendeeType.Required,
            ))

        # Shortest popup-reminder minutes wins (Graph supports a single
        # `reminderMinutesBeforeStart`, unlike Google's per-event override
        # array). Email reminders aren't directly representable in Graph,
        # so they're dropped here — subscribers configure their own
        # client-side reminders or use the popup.
        popup_mins = [
            r["minutes"] for r in (h.get("reminders") or [])
            if r.get("method") == "popup" and r.get("minutes") is not None
        ]
        is_reminder_on = bool(popup_mins)
        reminder_minutes = min(popup_mins) if popup_mins else None

        ext_prop = SingleValueLegacyExtendedProperty(
            id=_EXT_PROP_ID,
            value=_correlation_key(h["case_id"], h["hearing_key"]),
        )

        # `transaction_id` protects against retried POSTs from CourtListener webhook
        # storms. Graph ignores it on PATCH; only meaningful on create.
        return Event(
            subject=h["title"],
            body=ItemBody(content_type=BodyType.Text, content=description),
            location=_location(h.get("location")),
            start=DateTimeTimeZone(date_time=start_local, time_zone=tz),
            end=DateTimeTimeZone(date_time=end_local, time_zone=tz),
            attendees=attendees or None,
            is_reminder_on=is_reminder_on,
            reminder_minutes_before_start=reminder_minutes,
            single_value_extended_properties=[ext_prop],
            transaction_id=_correlation_key(h["case_id"], h["hearing_key"]),
        )


def _location(text: Optional[str]):
    if not text:
        return None
    from msgraph.generated.models.location import Location
    return Location(display_name=text)


def _is_404(exc: BaseException) -> bool:
    code = getattr(exc, "response_status_code", None)
    return code == 404
