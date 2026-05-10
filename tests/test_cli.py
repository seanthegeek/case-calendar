"""Tests for the cli emit-time helpers (title composition, deadline mapping).

Title composition lives at the cli/emit layer, not in the renderers, so the
ICS and gcal outputs receive a fully-built title and write it through.
"""

from __future__ import annotations

import pytest

from case_calendar.cli import _compose_title, _deadline_to_hearing, emit_calendars


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


class TestEmitCalendars:
    """``emit_calendars`` is shared by cmd_emit, cmd_sync's auto-emit, and
    the webhook auto-emit. The scoping (only_calendars) is what lets the
    webhook path skip calendars unaffected by a given delivery."""

    @pytest.fixture
    def cfg(self, tmp_path):
        return {
            "store_path": str(tmp_path / "x.sqlite"),
            "calendars": {
                "cyber": {
                    "name": "Cybercrime",
                    "ics_path": str(tmp_path / "cyber.ics"),
                },
                "tech": {
                    "name": "Tech",
                    "ics_path": str(tmp_path / "tech.ics"),
                },
            },
            "cases": [
                {"id": "us-v-x", "name": "US v. X",
                 "calendar": "cyber", "dockets": [100]},
                {"id": "acme-v-widget", "name": "Acme v. Widget",
                 "calendar": "tech", "dockets": [200]},
            ],
        }

    def _seed_hearing(self, store, *, case_id, key, calendar_unused="cyber"):
        store.upsert_hearing({
            "case_id": case_id,
            "hearing_key": key,
            "title": "Sentencing",
            "hearing_type": "sentencing",
            "starts_at_utc": "2099-04-14T15:00:00+00:00",
            "duration_minutes": 90,
            "timezone": "America/New_York",
            "status": "scheduled",
            "significance": "major",
            "docket_id": 100,
            "source_entry_ids": [1],
        })

    def test_writes_ics_for_each_calendar(self, store, cfg):
        self._seed_hearing(store, case_id="us-v-x", key="sentencing-x")
        self._seed_hearing(store, case_id="acme-v-widget", key="hearing-acme")
        results = emit_calendars(cfg, store)
        assert set(results) == {"cyber", "tech"}
        assert results["cyber"]["events"] == 1
        assert results["tech"]["events"] == 1
        # ICS files are real on disk.
        for cal in ("cyber", "tech"):
            text = open(results[cal]["ics_path"]).read()
            assert "BEGIN:VCALENDAR" in text and "END:VCALENDAR" in text

    def test_only_calendars_scopes_writes(self, store, cfg, tmp_path):
        # Pre-write the tech ICS with a sentinel string. Scoped emit on
        # {"cyber"} must not touch tech.ics.
        sentinel = tmp_path / "tech.ics"
        sentinel.write_text("SHOULD-NOT-BE-OVERWRITTEN")
        self._seed_hearing(store, case_id="us-v-x", key="sentencing-x")
        results = emit_calendars(cfg, store, only_calendars={"cyber"})
        assert set(results) == {"cyber"}
        assert sentinel.read_text() == "SHOULD-NOT-BE-OVERWRITTEN"

    def test_docket_entry_numbers_rendered_into_ics(self, store, cfg):
        # The hearing's source_entry_ids should be resolved against the
        # entries table to surface PACER docket positions in the description.
        store.mark_entry(100, 1001, "2026-01-01T00:00:00Z", "fp",
                         entry_number=65, description="ORDER")
        store.mark_entry(100, 1002, "2026-01-02T00:00:00Z", "fp",
                         entry_number=82, description="ORDER")
        store.upsert_hearing({
            "case_id": "us-v-x", "hearing_key": "sentencing-x",
            "title": "Sentencing", "starts_at_utc": "2099-04-14T15:00:00+00:00",
            "duration_minutes": 90, "timezone": "America/New_York",
            "status": "scheduled", "significance": "major",
            "docket_id": 100, "source_entry_ids": [1001, 1002],
        })
        emit_calendars(cfg, store, only_calendars={"cyber"})
        text = open(cfg["calendars"]["cyber"]["ics_path"]).read()
        # ICS folds long lines at 75 octets, so the literal text may be
        # broken across "\r\n " continuations; un-fold before asserting.
        unfolded = text.replace("\r\n ", "")
        assert "Docket entries: 65\\, 82" in unfolded

    def test_gcal_skipped_when_push_gcal_false(self, store, cfg):
        # Adding a gcal id must NOT trigger a push when push_gcal is off
        # (the daemon path defaults to off until the operator opts in).
        cfg["calendars"]["cyber"]["google_calendar_id"] = "abc@group.calendar.google.com"
        cfg["google_credentials_path"] = "/nonexistent.json"  # would crash if used
        self._seed_hearing(store, case_id="us-v-x", key="sentencing-x")
        results = emit_calendars(cfg, store, push_gcal=False)
        assert results["cyber"]["gcal_pushed"] is False
