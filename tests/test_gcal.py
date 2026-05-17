"""Google Calendar output tests.

We don't drive the Google API in tests; we test the body-shape function
directly and stub the discovery service for the upsert flow.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from case_calendar.calendars import gcal


def _h(**over):
    base = {
        "case_id": "us-v-x", "hearing_key": "sentencing",
        "title": "Sentencing", "starts_at_utc": "2026-04-14T15:00:00+00:00",
        "duration_minutes": 90, "timezone": "America/New_York",
        "location": "Courtroom 4", "judge": "Judge X",
        "notes": "Sentencing notes.", "dial_in": None,
        "status": "scheduled", "source_entry_ids": [1],
    }
    base.update(over)
    return base


class TestGcalId:
    def test_deterministic(self):
        assert gcal._gcal_id("a", "b") == gcal._gcal_id("a", "b")

    def test_different_inputs_different_ids(self):
        assert gcal._gcal_id("a", "b") != gcal._gcal_id("a", "c")
        assert gcal._gcal_id("a", "b") != gcal._gcal_id("b", "b")

    def test_id_format(self):
        eid = gcal._gcal_id("us-v-wang", "sentencing-wang")
        # Google calendar event IDs accept [a-v0-9]{5,1024}; we use 'cc' + sha1.
        assert eid.startswith("cc")
        assert len(eid) == 42
        assert all(c in "0123456789abcdef" for c in eid[2:])


class TestEventBody:
    def test_basic_body(self):
        body = gcal.GoogleCalendarSync._event_body("eid", _h())
        assert body["summary"] == "Sentencing"
        assert body["status"] == "confirmed"
        # location holds the physical/virtual location only; judge belongs in
        # the description.
        assert body["location"] == "Courtroom 4"
        assert "Judge X" not in body["location"]
        assert "Judge: Judge X" in body["description"]
        # New format: local time + court tz (NOT UTC), so viewers see the
        # event in their own tz but the event remembers the courthouse.
        assert body["start"]["timeZone"] == "America/New_York"
        assert body["start"]["dateTime"] == "2026-04-14T11:00:00"  # 15Z = 11 EDT
        assert body["end"]["timeZone"] == "America/New_York"
        assert body["end"]["dateTime"] == "2026-04-14T12:30:00"  # +90 min

    def test_pacific_court_uses_pacific_tz(self):
        body = gcal.GoogleCalendarSync._event_body(
            "eid", _h(timezone="America/Los_Angeles"),
        )
        assert body["start"]["timeZone"] == "America/Los_Angeles"
        # 15:00Z in April is 08:00 PDT.
        assert body["start"]["dateTime"] == "2026-04-14T08:00:00"

    def test_held_no_prefix(self):
        # The date itself tells subscribers the event is past; we don't
        # repeat that with a "[HELD]" prefix in the title.
        body = gcal.GoogleCalendarSync._event_body("eid", _h(status="held"))
        assert body["summary"] == "Sentencing"
        assert body["status"] == "confirmed"

    def test_all_day_future_date_renders_transparent(self):
        # Title prefixing ("[time TBD]" / "[time unknown]") is the cli emit
        # layer's job now; the renderer just passes the title through.
        body = gcal.GoogleCalendarSync._event_body(
            "eid", _h(duration_minutes=0,
                      starts_at_utc="2099-04-14T04:00:00+00:00"),
        )
        assert "date" in body["start"]
        assert "dateTime" not in body["start"]
        assert body["transparency"] == "transparent"
        assert body["summary"] == "Sentencing"

    def test_timed_event_does_not_get_transparency(self):
        body = gcal.GoogleCalendarSync._event_body("eid", _h())  # 90-min event
        assert "transparency" not in body

    def test_description_includes_notes(self):
        body = gcal.GoogleCalendarSync._event_body("eid", _h(notes="Important note."))
        assert "Important note." in body["description"]

    def test_attendees_added_when_notify_emails_set(self):
        body = gcal.GoogleCalendarSync._event_body(
            "eid", _h(notify_emails=["a@x.com", "b@y.com"]),
        )
        assert body["attendees"] == [{"email": "a@x.com"}, {"email": "b@y.com"}]

    def test_no_attendees_when_unset(self):
        body = gcal.GoogleCalendarSync._event_body("eid", _h())
        assert "attendees" not in body

    def test_reminder_overrides_set(self):
        body = gcal.GoogleCalendarSync._event_body(
            "eid", _h(reminders=[{"method": "popup", "minutes": 30}]),
        )
        assert body["reminders"] == {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 30}],
        }

    def test_no_reminders_when_unset(self):
        body = gcal.GoogleCalendarSync._event_body("eid", _h())
        assert "reminders" not in body


class TestSync:
    def _stub_service(self, *, exists: bool):
        """Build a Mock that mimics googleapiclient.discovery's chain."""
        events_obj = MagicMock(name="events")
        if exists:
            patch_obj = MagicMock()
            patch_obj.execute.return_value = {"id": "eid"}
            events_obj.patch.return_value = patch_obj
        else:
            err = HttpError(
                resp=MagicMock(status=404), content=b'{"error":"not found"}'
            )
            patch_obj = MagicMock()
            patch_obj.execute.side_effect = err
            events_obj.patch.return_value = patch_obj
            insert_obj = MagicMock()
            insert_obj.execute.return_value = {"id": "eid"}
            events_obj.insert.return_value = insert_obj
        service = MagicMock(name="service")
        service.events.return_value = events_obj
        return service, events_obj

    def test_existing_event_is_patched(self):
        service, events = self._stub_service(exists=True)
        gcs = gcal.GoogleCalendarSync.__new__(gcal.GoogleCalendarSync)
        gcs.service = service
        gcs.sync(calendar_id="cal-x", hearings=[_h()])
        assert events.patch.called
        assert not events.insert.called

    def test_send_updates_default_externalonly(self):
        service, events = self._stub_service(exists=True)
        gcs = gcal.GoogleCalendarSync.__new__(gcal.GoogleCalendarSync)
        gcs.service = service
        gcs.sync(calendar_id="cal-x", hearings=[_h(notify_emails=["a@b.com"])])
        kw = events.patch.call_args.kwargs
        assert kw["sendUpdates"] == "externalOnly"

    def test_missing_event_is_inserted_with_explicit_id(self):
        service, events = self._stub_service(exists=False)
        gcs = gcal.GoogleCalendarSync.__new__(gcal.GoogleCalendarSync)
        gcs.service = service
        gcs.sync(calendar_id="cal-x", hearings=[_h()])
        # patch is tried first, then insert with id set.
        assert events.patch.called
        assert events.insert.called
        body = events.insert.call_args.kwargs["body"]
        assert body["id"] == gcal._gcal_id("us-v-x", "sentencing")

    def test_skips_minor_significance(self):
        service, events = self._stub_service(exists=True)
        gcs = gcal.GoogleCalendarSync.__new__(gcal.GoogleCalendarSync)
        gcs.service = service
        gcs.sync(calendar_id="cal-x", hearings=[_h(significance="minor")])
        assert not events.patch.called
        assert not events.insert.called

    def test_cancelled_event_is_marked_cancelled_on_remote(self):
        # A cancelled hearing should not be upserted normally — instead we
        # patch the existing remote event with status='cancelled' so it
        # disappears from subscribers' calendars.
        service, events = self._stub_service(exists=True)
        gcs = gcal.GoogleCalendarSync.__new__(gcal.GoogleCalendarSync)
        gcs.service = service
        gcs.sync(calendar_id="cal-x", hearings=[_h(status="cancelled")])
        # Patched once, with body={'status': 'cancelled'} only — no event_body upsert.
        assert events.patch.called
        body = events.patch.call_args.kwargs["body"]
        assert body == {"status": "cancelled"}
        assert not events.insert.called

    def test_cancelled_event_with_no_remote_is_noop(self):
        service, events = self._stub_service(exists=False)  # 404 on patch
        gcs = gcal.GoogleCalendarSync.__new__(gcal.GoogleCalendarSync)
        gcs.service = service
        gcs.sync(calendar_id="cal-x", hearings=[_h(status="cancelled")])
        # Patch is attempted (it's how we cancel) but 404 means it never
        # existed; we should NOT insert just to mark cancelled.
        assert events.patch.called
        assert not events.insert.called

    def test_includes_major_significance(self):
        service, events = self._stub_service(exists=True)
        gcs = gcal.GoogleCalendarSync.__new__(gcal.GoogleCalendarSync)
        gcs.service = service
        gcs.sync(calendar_id="cal-x", hearings=[_h(significance="major")])
        assert events.patch.called

    def test_null_significance_treated_as_major(self):
        service, events = self._stub_service(exists=True)
        gcs = gcal.GoogleCalendarSync.__new__(gcal.GoogleCalendarSync)
        gcs.service = service
        gcs.sync(calendar_id="cal-x", hearings=[_h(significance=None)])
        assert events.patch.called

    def test_skips_hearings_without_dates(self):
        service, events = self._stub_service(exists=True)
        gcs = gcal.GoogleCalendarSync.__new__(gcal.GoogleCalendarSync)
        gcs.service = service
        gcs.sync(calendar_id="cal-x", hearings=[_h(starts_at_utc=None)])
        assert not events.patch.called

    def test_non_404_http_error_propagates(self):
        events_obj = MagicMock()
        err = HttpError(resp=MagicMock(status=500), content=b'{"error":"x"}')
        patch_obj = MagicMock()
        patch_obj.execute.side_effect = err
        events_obj.patch.return_value = patch_obj
        service = MagicMock()
        service.events.return_value = events_obj

        gcs = gcal.GoogleCalendarSync.__new__(gcal.GoogleCalendarSync)
        gcs.service = service
        with pytest.raises(HttpError):
            gcs.sync(calendar_id="cal-x", hearings=[_h()])

    def test_cancelled_non_404_error_propagates(self):
        # _cancel_if_present should re-raise any non-404 HttpError.
        events_obj = MagicMock()
        err = HttpError(resp=MagicMock(status=500), content=b'{"error":"x"}')
        patch_obj = MagicMock()
        patch_obj.execute.side_effect = err
        events_obj.patch.return_value = patch_obj
        service = MagicMock()
        service.events.return_value = events_obj
        gcs = gcal.GoogleCalendarSync.__new__(gcal.GoogleCalendarSync)
        gcs.service = service
        with pytest.raises(HttpError):
            gcs.sync(calendar_id="cal-x", hearings=[_h(status="cancelled")])


class TestTimeHelpers:
    def test_to_rfc3339_normalizes_naive_to_utc(self):
        # A naive ISO string is interpreted as UTC and re-emitted with Z.
        assert gcal._to_rfc3339("2026-04-14T15:00:00") == "2026-04-14T15:00:00Z"

    def test_to_rfc3339_normalizes_offset_to_utc(self):
        # -04:00 -> +00:00, ie 19:00Z for 15:00 EDT.
        assert gcal._to_rfc3339("2026-04-14T15:00:00-04:00") == "2026-04-14T19:00:00Z"

    def test_to_local_rfc3339_naive_input_treated_as_utc(self):
        # Naive timestamps fall through the tzinfo-is-None branch (line 55).
        out = gcal._to_local_rfc3339("2026-04-14T15:00:00", "America/New_York")
        assert out == "2026-04-14T11:00:00"  # 15Z = 11 EDT


class TestBuildService:
    """The OAuth-aware constructor path. We don't actually hit Google — we
    monkey-patch Credentials / InstalledAppFlow / build to record what
    branch fired."""

    def test_existing_valid_token_skips_flow(self, monkeypatch, tmp_path):
        from case_calendar.calendars import gcal as gcal_mod

        token = tmp_path / "tok.json"
        token.write_text("{}")
        # build_service uses Credentials.from_authorized_user_file -> returns
        # a creds object with .valid=True so the flow is skipped entirely.
        fake_creds = MagicMock()
        fake_creds.valid = True
        monkeypatch.setattr(
            gcal_mod.Credentials, "from_authorized_user_file",
            lambda path, scopes: fake_creds,
        )
        built = MagicMock(name="service")
        monkeypatch.setattr(gcal_mod, "build", lambda *a, **kw: built)

        gcs = gcal_mod.GoogleCalendarSync(
            credentials_path="/c.json", token_path=token,
        )
        assert gcs.service is built

    def test_expired_token_refreshes(self, monkeypatch, tmp_path):
        from case_calendar.calendars import gcal as gcal_mod

        token = tmp_path / "tok.json"
        token.write_text("{}")
        fake_creds = MagicMock()
        fake_creds.valid = False
        fake_creds.expired = True
        fake_creds.refresh_token = "refresh"
        fake_creds.to_json.return_value = "{}"  # must be str for Path.write_text

        monkeypatch.setattr(
            gcal_mod.Credentials, "from_authorized_user_file",
            lambda path, scopes: fake_creds,
        )
        monkeypatch.setattr(gcal_mod, "Request", MagicMock())
        monkeypatch.setattr(gcal_mod, "build", lambda *a, **kw: MagicMock())

        gcal_mod.GoogleCalendarSync(
            credentials_path="/c.json", token_path=token,
        )
        fake_creds.refresh.assert_called_once()
        # Token cache rewritten with refreshed creds.
        assert token.exists()

    def test_no_token_runs_installed_app_flow(self, monkeypatch, tmp_path):
        from case_calendar.calendars import gcal as gcal_mod

        # No token file exists. _build_service must run InstalledAppFlow.
        token = tmp_path / "missing.json"
        flow_obj = MagicMock()
        new_creds = MagicMock()
        new_creds.to_json.return_value = "{}"
        flow_obj.run_local_server.return_value = new_creds
        monkeypatch.setattr(
            gcal_mod.InstalledAppFlow, "from_client_secrets_file",
            lambda path, scopes: flow_obj,
        )
        monkeypatch.setattr(gcal_mod, "build", lambda *a, **kw: MagicMock())

        gcal_mod.GoogleCalendarSync(
            credentials_path="/c.json", token_path=token,
        )
        flow_obj.run_local_server.assert_called_once()
        # Token cache written after flow completes.
        assert token.exists()
