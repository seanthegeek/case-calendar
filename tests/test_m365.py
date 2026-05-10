"""Microsoft 365 / Outlook calendar sync tests.

The real Graph SDK is async and lives behind an InteractiveBrowserCredential
that wants a real Entra app registration, so these tests bypass
``M365CalendarSync.__init__`` entirely (via ``__new__``) and inject a fake
async client. That keeps the tests hermetic — no network, no credentials,
no event loop quirks beyond what asyncio.run already sets up.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from case_calendar.calendars import m365 as m365_mod
from case_calendar.calendars.m365 import (
    M365CalendarSync,
    _correlation_key,
    _is_404,
)


def _hearing(**over):
    base = {
        "case_id": "us-v-x",
        "hearing_key": "sentencing-x",
        "title": "[HEARING] US v. X: Sentencing",
        "starts_at_utc": "2026-04-14T19:00:00+00:00",  # 15:00 ET
        "duration_minutes": 90,
        "timezone": "America/New_York",
        "location": "Courtroom 4",
        "judge": None,
        "notes": None,
        "dial_in": None,
        "status": "scheduled",
        "significance": "major",
        "docket_id": 100,
        "source_entry_ids": [1],
        "m365_event_id": None,
    }
    base.update(over)
    return base


def _make_syncer(client) -> M365CalendarSync:
    """Build an M365CalendarSync without invoking the real __init__.

    The constructor would import azure-identity, hit the keyring, and try
    to open a browser — none of which we can do hermetically.
    """
    s = M365CalendarSync.__new__(M365CalendarSync)
    s.client_id = "test-client-id"
    s.token_path = None
    s.credential = None
    s.client = client
    return s


def _fake_404():
    err = Exception("not found")
    err.response_status_code = 404
    return err


# --- pure helpers ---


class TestCorrelationKey:
    def test_stable(self):
        a = _correlation_key("us-v-x", "sentencing-x")
        b = _correlation_key("us-v-x", "sentencing-x")
        assert a == b
        assert len(a) == 40  # sha1 hex

    def test_different_per_input(self):
        assert (
            _correlation_key("us-v-x", "sentencing-x")
            != _correlation_key("us-v-y", "sentencing-x")
        )
        assert (
            _correlation_key("us-v-x", "sentencing-x")
            != _correlation_key("us-v-x", "trial-x")
        )


class TestIs404:
    def test_404_detection(self):
        e = Exception("nope")
        e.response_status_code = 404
        assert _is_404(e) is True

    def test_other_codes_not_404(self):
        for code in (400, 401, 403, 500, None):
            e = Exception("x")
            e.response_status_code = code
            assert _is_404(e) is False

    def test_no_attr_not_404(self):
        assert _is_404(Exception("x")) is False


# --- event body builder ---


class TestEventBody:
    def test_timed_event_has_local_time_with_iana_tz(self):
        s = _make_syncer(client=None)
        body = s._event_body(_hearing())
        assert body.subject == "[HEARING] US v. X: Sentencing"
        # 19:00 UTC → 15:00 EDT on 2026-04-14 (DST in effect).
        assert body.start.date_time == "2026-04-14T15:00:00"
        assert body.start.time_zone == "America/New_York"
        # 15:00 + 90 min = 16:30.
        assert body.end.date_time == "2026-04-14T16:30:00"
        assert body.end.time_zone == "America/New_York"

    def test_date_only_event_renders_9_to_5(self):
        s = _make_syncer(client=None)
        body = s._event_body(_hearing(duration_minutes=None))
        # No first-class all-day toggle in Outlook for a non-UTC tz; we
        # render a 9–5 court-tz block with the [time TBD]/[time unknown]
        # title prefix carrying the real semantics.
        assert body.start.date_time == "2026-04-14T09:00:00"
        assert body.end.date_time == "2026-04-14T17:00:00"

    def test_attendees_from_notify_emails(self):
        s = _make_syncer(client=None)
        body = s._event_body(_hearing(notify_emails=["a@x", "b@x"]))
        assert len(body.attendees) == 2
        assert {a.email_address.address for a in body.attendees} == {"a@x", "b@x"}

    def test_no_attendees_when_no_notify_emails(self):
        s = _make_syncer(client=None)
        body = s._event_body(_hearing())
        assert body.attendees is None

    def test_reminder_uses_shortest_popup(self):
        # Graph supports a single reminderMinutesBeforeStart, unlike Google's
        # per-event override array. Take the most-immediate popup since it's
        # the most useful pre-event nudge.
        s = _make_syncer(client=None)
        body = s._event_body(_hearing(reminders=[
            {"method": "popup", "minutes": 1440},
            {"method": "popup", "minutes": 30},
            {"method": "email", "minutes": 60},  # email reminders dropped
        ]))
        assert body.is_reminder_on is True
        assert body.reminder_minutes_before_start == 30

    def test_no_reminder_when_no_popup_configured(self):
        s = _make_syncer(client=None)
        body = s._event_body(_hearing(reminders=[
            {"method": "email", "minutes": 60},  # email-only → no popup
        ]))
        assert body.is_reminder_on is False
        assert body.reminder_minutes_before_start is None

    def test_extended_property_is_correlation_key(self):
        # Self-healing fallback: every event we create carries a stable
        # extended property keyed to case_id::hearing_key. If the local
        # cache is wiped we can $filter for it and recover the server id
        # rather than orphan the event and create a duplicate.
        s = _make_syncer(client=None)
        body = s._event_body(_hearing())
        props = body.single_value_extended_properties
        assert len(props) == 1
        assert props[0].id == m365_mod._EXT_PROP_ID
        assert props[0].value == _correlation_key("us-v-x", "sentencing-x")

    def test_transaction_id_set_on_create_body(self):
        # transactionId is Graph's POST-retry dedup mechanism. We feed it
        # the same correlation key so a webhook re-delivery within
        # minutes can't double-create the event.
        s = _make_syncer(client=None)
        body = s._event_body(_hearing())
        assert body.transaction_id == _correlation_key("us-v-x", "sentencing-x")


# --- upsert flow ---


@pytest.fixture
def fake_client():
    """Build a minimal Graph client surface that the syncer touches."""
    client = MagicMock()
    # Default-calendar path: client.me.events.post / .by_event_id(id).patch / .get
    client.me.events.post = AsyncMock()
    client.me.events.get = AsyncMock()

    def by_id(_event_id):
        item = MagicMock()
        item.patch = AsyncMock()
        item.delete = AsyncMock()
        return item

    client.me.events.by_event_id = MagicMock(side_effect=by_id)

    # Specific-calendar path: client.me.calendars.by_calendar_id(cid).events.{post,get,by_event_id}
    cal_events = MagicMock()
    cal_events.post = AsyncMock()
    cal_events.get = AsyncMock()
    cal_events.by_event_id = MagicMock(side_effect=by_id)
    cal = MagicMock()
    cal.events = cal_events
    client.me.calendars.by_calendar_id = MagicMock(return_value=cal)
    return client


class TestUpsertFlow:
    def test_first_push_creates_and_caches_id(self, fake_client):
        # No cached id, no existing event → POST to create. The new server
        # id must be written back to the store so the next push patches.
        store = MagicMock()
        fake_client.me.events.get.return_value = MagicMock(value=[])
        created = MagicMock()
        created.id = "AAMkSERVERID-NEW"
        fake_client.me.events.post.return_value = created

        s = _make_syncer(fake_client)
        s.sync(hearings=[_hearing()], store=store)

        fake_client.me.events.post.assert_awaited_once()
        store.set_m365_id_for_hearing.assert_called_once_with(
            "us-v-x", "sentencing-x", "AAMkSERVERID-NEW",
        )

    def test_cached_id_patches_directly(self, fake_client):
        # Has cached id → straight to PATCH, no recovery lookup, no insert.
        store = MagicMock()
        s = _make_syncer(fake_client)
        s.sync(hearings=[_hearing(m365_event_id="CACHED-ID")], store=store)

        fake_client.me.events.by_event_id.assert_called_with("CACHED-ID")
        fake_client.me.events.post.assert_not_awaited()
        fake_client.me.events.get.assert_not_awaited()
        # No id change on a happy-path patch — no need to re-cache.
        store.set_m365_id_for_hearing.assert_not_called()

    def test_stale_cached_id_falls_back_to_correlation_lookup(self, fake_client):
        # Cached id 404s (event was deleted on the calendar). We $filter
        # for the correlation key and patch the recovered id rather than
        # blindly inserting and orphaning the existing event.
        store = MagicMock()
        recovered = MagicMock()
        recovered.id = "AAMkRECOVERED"
        fake_client.me.events.get.return_value = MagicMock(value=[recovered])

        # Make the patch on the cached id raise 404.
        item = MagicMock()
        item.patch = AsyncMock(side_effect=_fake_404())
        item.delete = AsyncMock()

        # Different mock for the recovered-id patch (must succeed).
        recovered_item = MagicMock()
        recovered_item.patch = AsyncMock()
        recovered_item.delete = AsyncMock()

        def by_id(eid):
            return item if eid == "STALE" else recovered_item

        fake_client.me.events.by_event_id.side_effect = by_id

        s = _make_syncer(fake_client)
        s.sync(hearings=[_hearing(m365_event_id="STALE")], store=store)

        recovered_item.patch.assert_awaited_once()
        store.set_m365_id_for_hearing.assert_called_once_with(
            "us-v-x", "sentencing-x", "AAMkRECOVERED",
        )

    def test_cancelled_row_deletes_existing_event(self, fake_client):
        # status='cancelled' → DELETE. Graph's DELETE on an organizer
        # event already mails an attendee cancellation, so there's no
        # separate cancel verb to prefer. The cached id is cleared
        # afterward so a future revival creates a fresh event.
        store = MagicMock()
        s = _make_syncer(fake_client)
        s.sync(
            hearings=[_hearing(status="cancelled", m365_event_id="WILL-BE-DELETED")],
            store=store,
        )

        fake_client.me.events.by_event_id.assert_called_with("WILL-BE-DELETED")
        store.set_m365_id_for_hearing.assert_called_once_with(
            "us-v-x", "sentencing-x", None,
        )

    def test_cancelled_row_with_no_cached_id_noops(self, fake_client):
        # Cancelled row that was never pushed: a $filter recovery returns
        # nothing, so we noop rather than creating-and-deleting.
        store = MagicMock()
        fake_client.me.events.get.return_value = MagicMock(value=[])

        s = _make_syncer(fake_client)
        s.sync(hearings=[_hearing(status="cancelled")], store=store)

        # No DELETE call, no PATCH, no POST.
        store.set_m365_id_for_hearing.assert_not_called()

    def test_minor_significance_skipped(self, fake_client):
        # Procedural-only events (phone calls about scheduling motions)
        # stay in the DB for audit but don't reach the user-facing
        # calendar.
        store = MagicMock()
        s = _make_syncer(fake_client)
        s.sync(
            hearings=[_hearing(significance="minor", m365_event_id="X")],
            store=store,
        )
        fake_client.me.events.by_event_id.assert_not_called()
        fake_client.me.events.post.assert_not_awaited()

    def test_specific_calendar_id_routes_to_calendar_events(self, fake_client):
        # When the calendar config sets m365_calendar_id, push goes to
        # /me/calendars/{id}/events, not /me/events.
        store = MagicMock()
        cal = fake_client.me.calendars.by_calendar_id.return_value
        cal.events.get.return_value = MagicMock(value=[])
        created = MagicMock()
        created.id = "AAMk-IN-SPECIFIC-CAL"
        cal.events.post.return_value = created

        s = _make_syncer(fake_client)
        s.sync(hearings=[_hearing()], store=store, calendar_id="CAL-ID")

        fake_client.me.calendars.by_calendar_id.assert_called_with("CAL-ID")
        cal.events.post.assert_awaited_once()
        # Default-calendar surface untouched.
        fake_client.me.events.post.assert_not_awaited()


class TestKindRouting:
    def test_deadline_prefix_routes_to_deadlines_table(self, fake_client):
        # Deadlines get mapped via cli._deadline_to_hearing into a
        # hearing-shaped dict with a "deadline:" key prefix. The cache
        # writeback strips that prefix and updates the deadlines table
        # rather than the hearings table.
        store = MagicMock()
        created = MagicMock()
        created.id = "AAMk-DEADLINE"
        fake_client.me.events.post.return_value = created
        fake_client.me.events.get.return_value = MagicMock(value=[])

        h = _hearing(hearing_key="deadline:reply-mtd")
        s = _make_syncer(fake_client)
        s.sync(hearings=[h], store=store)

        store.set_m365_id_for_deadline.assert_called_once_with(
            "us-v-x", "reply-mtd", "AAMk-DEADLINE",
        )
        store.set_m365_id_for_hearing.assert_not_called()
