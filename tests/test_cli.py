"""Tests for the cli emit-time helpers (title composition, deadline mapping).

Title composition lives at the cli/emit layer, not in the renderers, so the
ICS and gcal outputs receive a fully-built title and write it through.
"""

from __future__ import annotations

from case_calendar.cli import _compose_title, _deadline_to_hearing


class TestComposeTitle:
    def test_timed_hearing_no_time_status_prefix(self):
        out = _compose_title(
            raw_title="Sentencing",
            kind="HEARING",
            case_name="US v. X",
            starts_at_utc="2099-04-14T15:00:00+00:00",
            duration_minutes=90,
        )
        assert out == "[HEARING] US v. X: Sentencing"

    def test_future_date_only_hearing_gets_time_tbd(self):
        out = _compose_title(
            raw_title="Sentencing",
            kind="HEARING",
            case_name="US v. X",
            starts_at_utc="2099-04-14T04:00:00+00:00",
            duration_minutes=0,
        )
        # Category first, then time-status, then case name. Subscribers
        # scanning a shared calendar can spot the kind ([HEARING]) at a
        # glance regardless of whether a time-status flag is present.
        assert out == "[HEARING] [time TBD] US v. X: Sentencing"

    def test_past_date_only_hearing_gets_time_unknown(self):
        out = _compose_title(
            raw_title="Sentencing",
            kind="HEARING",
            case_name="US v. X",
            starts_at_utc="2020-04-14T04:00:00+00:00",
            duration_minutes=0,
        )
        assert out == "[HEARING] [time unknown] US v. X: Sentencing"

    def test_deadline_kind_prefix(self):
        out = _compose_title(
            raw_title="Reply ISO MTD",
            kind="DEADLINE",
            case_name="Anthropic v. DOW",
            starts_at_utc="2026-05-31T21:00:00+00:00",
            duration_minutes=15,
        )
        assert out == "[DEADLINE] Anthropic v. DOW: Reply ISO MTD"

    def test_null_duration_treated_as_no_time(self):
        out = _compose_title(
            raw_title="Trial",
            kind="HEARING",
            case_name="US v. Y",
            starts_at_utc="2099-04-14T04:00:00+00:00",
            duration_minutes=None,
        )
        assert "[time TBD]" in out


class TestDeadlineToHearing:
    def _row(self, **over):
        base = {
            "case_id": "anthropic-v-dow",
            "deadline_key": "reply-mtd",
            "title": "Reply ISO MTD",
            "due_at_utc": "2026-05-31T21:00:00+00:00",
            "timezone": "America/New_York",
            "notes": None,
            "status": "pending",
            "significance": "major",
            "deadline_type": "reply",
            "gcal_event_id": None,
            "docket_id": 72380208,
            "source_entry_ids": [1, 2],
        }
        base.update(over)
        return base

    def test_returns_none_without_due_timestamp(self):
        assert _deadline_to_hearing(self._row(due_at_utc=None)) is None

    def test_uid_namespace_is_prefixed(self):
        # The "deadline:" prefix on the hearing_key keeps the ICS UID and
        # gcal deterministic ID separate from any real hearing's namespace —
        # otherwise a hearing and a deadline sharing a slug would collide.
        out = _deadline_to_hearing(self._row())
        assert out["hearing_key"] == "deadline:reply-mtd"

    def test_does_not_pre_prefix_title(self):
        # _compose_title is responsible for prefixing — _deadline_to_hearing
        # returns the raw title so cli.py's compose step has clean inputs.
        out = _deadline_to_hearing(self._row())
        assert out["title"] == "Reply ISO MTD"

    def test_passed_status_maps_to_held(self):
        # Past-due pending deadlines flip to 'passed' in the store; for
        # rendering they map to 'held' so they stay visible in the ICS feed.
        out = _deadline_to_hearing(self._row(status="passed"))
        assert out["status"] == "held"

    def test_met_status_maps_to_cancelled(self):
        # 'met' = the filing was made. Renderers skip cancelled rows so
        # they fall off the calendar — exactly what we want for met
        # deadlines, which no longer need a reminder.
        out = _deadline_to_hearing(self._row(status="met"))
        assert out["status"] == "cancelled"
