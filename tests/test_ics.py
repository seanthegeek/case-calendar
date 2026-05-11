"""ICS output tests.

We render hearings to an ICS string and check structural properties
rather than diffing against a fixture (line folding / DTSTAMP would make
fixture diffs flaky).
"""

from __future__ import annotations

from case_calendar.calendars.ics import render_ics


def _h(**over):
    base = {
        "case_id": "us-v-x", "hearing_key": "sentencing",
        "title": "Sentencing", "starts_at_utc": "2026-04-14T15:00:00+00:00",
        "duration_minutes": 90, "timezone": "America/New_York",
        "location": "Courtroom 4", "judge": "Judge X",
        "notes": None, "dial_in": None,
        "status": "scheduled", "source_entry_ids": [1],
    }
    base.update(over)
    return base


class TestRenderIcs:
    def test_basic_structure(self):
        ics = render_ics(calendar_name="Test", hearings=[_h()])
        assert ics.startswith("BEGIN:VCALENDAR\r\n")
        assert ics.endswith("END:VCALENDAR\r\n")
        assert "BEGIN:VEVENT" in ics
        assert "END:VEVENT" in ics
        assert "X-WR-CALNAME:Test" in ics

    def test_uid_stable_across_renders(self):
        a = render_ics(calendar_name="X", hearings=[_h()])
        b = render_ics(calendar_name="X", hearings=[_h()])
        # DTSTAMP differs but UID should be identical.
        for chunk in a.split("\r\n"):
            if chunk.startswith("UID:"):
                assert chunk in b
                break
        else:
            assert False, "no UID line"

    def test_uid_uses_case_and_key(self):
        ics = render_ics(calendar_name="X", hearings=[_h()])
        assert "UID:us-v-x--sentencing@case-calendar" in ics

    def test_summary_and_location(self):
        ics = render_ics(calendar_name="X", hearings=[_h()])
        assert "SUMMARY:Sentencing" in ics
        # LOCATION holds only the physical/virtual location; the judge name
        # belongs in the description (rendered as "Judge:" / "Panel:").
        assert "LOCATION:Courtroom 4" in ics
        assert "LOCATION:Courtroom 4 — Judge X" not in ics
        assert "Judge: Judge X" in ics

    def test_dtstart_dtend_local_with_tzid(self):
        # Stored UTC 2026-04-14T15:00:00Z is 11:00 EDT. The event should
        # carry that local time + the court's tz, so a viewer in any tz
        # sees "11 AM Eastern" semantics.
        ics = render_ics(calendar_name="X", hearings=[_h()])
        assert "DTSTART;TZID=America/New_York:20260414T110000" in ics
        assert "DTEND;TZID=America/New_York:20260414T123000" in ics  # +90 min

    def test_no_vtimezone_block(self):
        # We rely on receivers' bundled IANA tz databases instead of
        # shipping VTIMEZONE blocks ourselves.
        ics = render_ics(calendar_name="X", hearings=[_h()])
        assert "BEGIN:VTIMEZONE" not in ics

    def test_multiple_tzs_each_get_own_tzid(self):
        ics = render_ics(calendar_name="X", hearings=[
            _h(hearing_key="a", timezone="America/New_York"),
            _h(hearing_key="b", timezone="America/Los_Angeles"),
        ])
        assert "TZID=America/New_York:" in ics
        assert "TZID=America/Los_Angeles:" in ics

    def test_unknown_tz_falls_back_to_utc(self):
        ics = render_ics(calendar_name="X", hearings=[_h(timezone="Bogus/Tz")])
        # DTSTART reverts to a bare UTC stamp.
        assert "TZID=Bogus" not in ics
        # The DTSTART line ends with Z (UTC suffix).
        line = next(l for l in ics.split("\r\n") if l.startswith("DTSTART:"))
        assert line.endswith("Z")

    def test_cancelled_event_is_filtered_out(self):
        # Cancelled trials (e.g. via a plea) are not events of record — the
        # plea hearing or rescheduled trial lives on its own row. Drop them
        # rather than leave [CANCELLED] entries lingering on subscribers'
        # calendars.
        ics = render_ics(calendar_name="X", hearings=[_h(status="cancelled")])
        assert "BEGIN:VEVENT" not in ics
        assert "[CANCELLED]" not in ics

    def test_held_status_no_prefix(self):
        # The date itself tells subscribers the event is past; we don't need
        # a "[HELD]" prefix to repeat that.
        ics = render_ics(calendar_name="X", hearings=[_h(status="held")])
        assert "SUMMARY:Sentencing" in ics
        assert "[HELD]" not in ics

    def test_all_day_future_date_renders_transparent(self):
        # Date-only hearings shouldn't block the user's day. Title prefixing
        # ("[time TBD]" / "[time unknown]") is the cli emit layer's job; the
        # renderer just passes the title through.
        ics = render_ics(
            calendar_name="X",
            hearings=[_h(duration_minutes=0,
                         starts_at_utc="2099-04-14T04:00:00+00:00")],
        )
        assert "DTSTART;VALUE=DATE:20990414" in ics
        assert "DTEND;VALUE=DATE:20990415" in ics
        assert "TRANSP:TRANSPARENT" in ics
        assert "SUMMARY:Sentencing" in ics

    def test_held_date_only_still_renders_transparent(self):
        # Past held date-only hearing still shows up tentative — date alone
        # doesn't represent a real all-day block.
        ics = render_ics(
            calendar_name="X",
            hearings=[_h(duration_minutes=0, status="held",
                         starts_at_utc="2026-04-14T04:00:00+00:00")],
        )
        assert "TRANSP:TRANSPARENT" in ics
        assert "[HELD]" not in ics

    def test_skips_hearing_without_starts_at_utc(self):
        ics = render_ics(calendar_name="X",
                         hearings=[_h(starts_at_utc=None)])
        assert "BEGIN:VEVENT" not in ics

    def test_skips_minor_significance(self):
        # A phone call set just to rule on a Motion to Continue is minor;
        # subscribers shouldn't see procedural noise in their calendar.
        ics = render_ics(
            calendar_name="X",
            hearings=[_h(significance="minor",
                         title="Telephonic Conference Call - Motion to Continue")],
        )
        assert "BEGIN:VEVENT" not in ics

    def test_includes_major_significance(self):
        ics = render_ics(
            calendar_name="X", hearings=[_h(significance="major")],
        )
        assert "BEGIN:VEVENT" in ics

    def test_null_significance_treated_as_major(self):
        # Existing pre-significance rows have NULL — render them as major
        # so the upgrade doesn't silently empty the calendar.
        ics = render_ics(calendar_name="X", hearings=[_h(significance=None)])
        assert "BEGIN:VEVENT" in ics

    def test_description_includes_dial_in(self):
        ics = render_ics(calendar_name="X",
                         hearings=[_h(dial_in="https://meet.example/abc")])
        assert "DESCRIPTION:" in ics
        # The colon-and-newline gets escaped to \n
        assert "Dial-in / link" in ics

    def test_special_chars_escaped_in_summary(self):
        ics = render_ics(calendar_name="X",
                         hearings=[_h(title="A, B; C")])
        assert "SUMMARY:A\\, B\\; C" in ics

    def test_line_folding_for_long_summary(self):
        long_title = "Long " * 50  # well over 75 chars
        ics = render_ics(calendar_name="X", hearings=[_h(title=long_title)])
        # No single line should exceed 75 octets.
        for line in ics.split("\r\n"):
            assert len(line.encode("utf-8")) <= 75 or line.startswith(" "), \
                f"unfolded line: {line!r}"

    def test_multiple_hearings(self):
        ics = render_ics(calendar_name="X", hearings=[
            _h(hearing_key="a"), _h(hearing_key="b"),
        ])
        assert ics.count("BEGIN:VEVENT") == 2

    def test_attendees_rendered(self):
        ics = render_ics(calendar_name="X",
                         hearings=[_h(notify_emails=["a@x.com", "b@y.com"])])
        assert "ATTENDEE;RSVP=TRUE:mailto:a@x.com" in ics
        assert "ATTENDEE;RSVP=TRUE:mailto:b@y.com" in ics

    def test_popup_valarm(self):
        ics = render_ics(
            calendar_name="X",
            hearings=[_h(reminders=[{"method": "popup", "minutes": 30}])],
        )
        assert "BEGIN:VALARM" in ics
        assert "ACTION:DISPLAY" in ics
        assert "TRIGGER:-PT30M" in ics
        assert "END:VALARM" in ics

    def test_email_valarm_includes_attendees(self):
        ics = render_ics(
            calendar_name="X",
            hearings=[_h(reminders=[{"method": "email", "minutes": 60}],
                         notify_emails=["a@x.com"])],
        )
        # Email VALARM should list recipients.
        valarm_block = ics.split("BEGIN:VALARM")[1].split("END:VALARM")[0]
        assert "ACTION:EMAIL" in valarm_block
        assert "TRIGGER:-PT60M" in valarm_block
        assert "ATTENDEE:mailto:a@x.com" in valarm_block

    def test_zero_minute_reminder_skipped(self):
        ics = render_ics(
            calendar_name="X",
            hearings=[_h(reminders=[{"method": "popup", "minutes": 0}])],
        )
        assert "BEGIN:VALARM" not in ics

    def test_no_attendees_or_alarms_by_default(self):
        ics = render_ics(calendar_name="X", hearings=[_h()])
        assert "ATTENDEE" not in ics
        assert "VALARM" not in ics

    def test_naive_iso_treated_as_utc(self):
        # The _fmt_local helper promotes naive timestamps to UTC; result
        # in court tz is the same wall-clock as a tz-aware "+00:00" input.
        ics = render_ics(
            calendar_name="X",
            hearings=[_h(starts_at_utc="2026-04-14T15:00:00",
                          timezone="America/New_York")],
        )
        # 15:00Z → 11:00 EDT.
        assert "DTSTART;TZID=America/New_York:20260414T110000" in ics


class TestWriteIcs:
    def test_creates_parent_dir(self, tmp_path):
        from case_calendar.calendars.ics import write_ics

        target = tmp_path / "site" / "cyber.ics"
        write_ics(target, calendar_name="Cybercrime", hearings=[_h()])
        assert target.exists()
        content = target.read_text()
        assert "BEGIN:VCALENDAR" in content
