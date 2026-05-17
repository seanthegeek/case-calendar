"""Microsoft 365 / Outlook calendar sync tests.

The real Graph SDK is async and lives behind an InteractiveBrowserCredential
that wants a real Entra app registration, so these tests bypass
``M365CalendarSync.__init__`` entirely (via ``__new__``) and inject a fake
async client. That keeps the tests hermetic — no network, no credentials,
no event loop quirks beyond what asyncio.run already sets up.
"""

from __future__ import annotations

from typing import Any
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
    to open a browser — none of which we can do hermetically. Tests
    never read token_path / credential, so we leave those unset.
    """
    s = M365CalendarSync.__new__(M365CalendarSync)
    s.client_id = "test-client-id"
    s.client = client
    return s


class _StatusError(Exception):
    """Stand-in for Graph SDK exceptions that carry `response_status_code`.

    `_is_404` reads the attribute via `getattr`; this subclass mirrors
    the shape so tests can construct an error with a known status code.
    """

    def __init__(self, message: str, status: int | None) -> None:
        super().__init__(message)
        self.response_status_code = status


def _fake_404():
    return _StatusError("not found", 404)


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
        assert _is_404(_StatusError("nope", 404)) is True

    def test_other_codes_not_404(self):
        for code in (400, 401, 403, 500, None):
            assert _is_404(_StatusError("x", code)) is False

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
        # No direct all-day toggle in Outlook for a non-UTC tz; we
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
        # Automatic-recovery fallback: every event we create carries a stable
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


class TestUpsertErrorPaths:
    def test_cached_id_non_404_error_propagates(self, fake_client):
        store = MagicMock()
        err = _StatusError("server boom", 500)
        item = MagicMock()
        item.patch = AsyncMock(side_effect=err)
        fake_client.me.events.by_event_id.side_effect = lambda eid: item

        s = _make_syncer(fake_client)
        with pytest.raises(Exception, match="server boom"):
            s.sync(hearings=[_hearing(m365_event_id="CACHED")], store=store)

    def test_post_returns_no_id_raises(self, fake_client):
        store = MagicMock()
        # No cached id, no recovery hit, post returns an object with id=None.
        fake_client.me.events.get.return_value = MagicMock(value=[])
        created = MagicMock()
        created.id = None
        fake_client.me.events.post.return_value = created

        s = _make_syncer(fake_client)
        with pytest.raises(RuntimeError, match="no event id"):
            s.sync(hearings=[_hearing()], store=store)

    def test_delete_non_404_error_propagates(self, fake_client):
        store = MagicMock()
        err = _StatusError("server boom", 500)
        item = MagicMock()
        item.delete = AsyncMock(side_effect=err)
        item.patch = AsyncMock()
        fake_client.me.events.by_event_id.side_effect = lambda eid: item

        s = _make_syncer(fake_client)
        with pytest.raises(Exception, match="server boom"):
            s.sync(
                hearings=[_hearing(status="cancelled", m365_event_id="X")],
                store=store,
            )

    def test_delete_404_swallowed(self, fake_client):
        # The remote event is already gone (404). Delete is a noop and
        # _delete_if_present returns early — the cache stays as-is rather
        # than getting cleared, since there was nothing on Graph to detach.
        store = MagicMock()
        item = MagicMock()
        item.delete = AsyncMock(side_effect=_fake_404())
        fake_client.me.events.by_event_id.side_effect = lambda eid: item

        s = _make_syncer(fake_client)
        s.sync(
            hearings=[_hearing(status="cancelled", m365_event_id="GONE")],
            store=store,
        )
        # No exception raised; no store mutation since we early-returned.
        store.set_m365_id_for_hearing.assert_not_called()

    def test_find_by_correlation_404_returns_none(self, fake_client):
        # $filter 404 → no recovered id; we POST a new event instead.
        store = MagicMock()
        fake_client.me.events.get.side_effect = _fake_404()
        created = MagicMock()
        created.id = "AAMk-FRESH"
        fake_client.me.events.post.return_value = created

        s = _make_syncer(fake_client)
        s.sync(hearings=[_hearing()], store=store)
        fake_client.me.events.post.assert_awaited_once()

    def test_find_by_correlation_non_404_propagates(self, fake_client):
        store = MagicMock()
        err = _StatusError("server boom", 500)
        fake_client.me.events.get.side_effect = err

        s = _make_syncer(fake_client)
        with pytest.raises(Exception, match="server boom"):
            s.sync(hearings=[_hearing()], store=store)

    def test_skips_hearings_without_start(self, fake_client):
        store = MagicMock()
        s = _make_syncer(fake_client)
        s.sync(hearings=[_hearing(starts_at_utc=None)], store=store)
        fake_client.me.events.post.assert_not_awaited()
        fake_client.me.events.by_event_id.assert_not_called()

    def test_cache_id_with_no_store_is_noop(self, fake_client):
        # store=None is a legal path (callers may opt out of caching);
        # _cache_id must short-circuit before touching the (None) store.
        fake_client.me.events.get.return_value = MagicMock(value=[])
        created = MagicMock()
        created.id = "AAMk-NEW"
        fake_client.me.events.post.return_value = created

        s = _make_syncer(fake_client)
        # No exception raised even though store is None.
        s.sync(hearings=[_hearing()], store=None)


class TestEventBodyEdgeCases:
    def test_location_None_when_unset(self):
        s = _make_syncer(client=None)
        body = s._event_body(_hearing(location=None))
        assert body.location is None

    def test_naive_iso_treated_as_utc(self):
        # A naive ISO timestamp (no tzinfo) takes the explicit-UTC branch
        # in _event_body's datetime handling.
        s = _make_syncer(client=None)
        body = s._event_body(_hearing(starts_at_utc="2026-04-14T19:00:00"))
        # Same wall-clock outcome as the tz-aware fixture: 15:00 EDT.
        assert body.start.date_time == "2026-04-14T15:00:00"


class TestBuildCredential:
    """Cover the auth-setup paths in M365CalendarSync.__init__ /
    ``_build_credential``. We stub the azure-identity classes the SDK
    imports lazily so no keyring or browser is needed."""

    def _stub_azure_identity(self, monkeypatch):
        """Replace the azure-identity / msgraph names the constructor imports."""
        import sys
        from unittest.mock import MagicMock

        # Build a fake azure.identity module with the three names the
        # constructor imports.
        record_obj = MagicMock(name="AuthenticationRecord")
        record_obj.serialize.return_value = '{"record": "data"}'

        class _FakeAuthenticationRecord:
            @staticmethod
            def deserialize(text):
                return record_obj

        # The InteractiveBrowserCredential instances get .authenticate()
        # called only on first-run; daemon-path silent refresh constructs
        # but doesn't call .authenticate().
        cred_instances: list[Any] = []

        class _FakeCred:
            def __init__(self, **kw):
                self.kw = kw
                cred_instances.append(self)

            def authenticate(self, *, scopes):
                return record_obj

        fake_az_id = MagicMock(name="azure.identity")
        fake_az_id.AuthenticationRecord = _FakeAuthenticationRecord
        fake_az_id.InteractiveBrowserCredential = _FakeCred
        fake_az_id.TokenCachePersistenceOptions = MagicMock()
        monkeypatch.setitem(sys.modules, "azure", MagicMock(identity=fake_az_id))
        monkeypatch.setitem(sys.modules, "azure.identity", fake_az_id)

        # GraphServiceClient — we only need the constructor to swallow kwargs.
        fake_msgraph = MagicMock(name="msgraph")
        fake_msgraph.GraphServiceClient = MagicMock(name="GraphServiceClient")
        monkeypatch.setitem(sys.modules, "msgraph", fake_msgraph)

        return cred_instances, record_obj, _FakeCred

    def test_first_run_writes_auth_record(self, monkeypatch, tmp_path):
        cred_instances, record_obj, _ = self._stub_azure_identity(monkeypatch)
        token = tmp_path / "m365.json"

        # No token file -> interactive flow path. authenticate() is called
        # and the returned record is serialized to disk.
        M365CalendarSync(client_id="cid", token_path=token)
        assert token.exists()
        assert len(cred_instances) == 1
        # First-run path doesn't pass disable_automatic_authentication.
        assert "disable_automatic_authentication" not in cred_instances[0].kw

    def test_subsequent_run_uses_silent_refresh(self, monkeypatch, tmp_path):
        cred_instances, _, _ = self._stub_azure_identity(monkeypatch)
        token = tmp_path / "m365.json"
        token.write_text('{"record":"existing"}')

        M365CalendarSync(client_id="cid", token_path=token)
        assert len(cred_instances) == 1
        # Daemon path passes disable_automatic_authentication so a stale
        # cache fails fast rather than trying to open a browser headless.
        assert cred_instances[0].kw.get("disable_automatic_authentication") is True
        # The serialized record is loaded back and passed as the
        # authentication_record kwarg.
        assert cred_instances[0].kw.get("authentication_record") is not None


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
