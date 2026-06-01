"""Tests for the per-case "Upcoming events" preview rendered into index.html.

The preview is a windowed view onto the SAME event set the ICS feed carries,
so the load-bearing test here is filter-parity: the events the preview would
show for a case must equal the events the ICS renderer emits for that
calendar. The rest cover parsing, timezone fallback, windowing, court-local
decoration, rendering, search folding, and the build_calendar_models
integration.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from case_calendar import cli
from case_calendar.calendars import ics, index

NOW = datetime(2026, 5, 31, tzinfo=timezone.utc)
TZ = "America/New_York"  # EDT (UTC-4) on the dates used below


def _hearing(
    key,
    title,
    starts_at_utc,
    *,
    duration=60,
    significance="major",
    status="scheduled",
    docket_id=101,
    tz=TZ,
):
    return {
        "case_id": "c1",
        "hearing_key": key,
        "title": title,
        "starts_at_utc": starts_at_utc,
        "duration_minutes": duration,
        "timezone": tz,
        "location": None,
        "judge": None,
        "notes": None,
        "audit_notes": None,
        "dial_in": None,
        "status": status,
        "significance": significance,
        "gcal_event_id": None,
        "docket_id": docket_id,
        "source_entry_ids": [9001],
    }


def _deadline(
    key,
    title,
    due_at_utc,
    *,
    significance="major",
    status="pending",
    docket_id=101,
    tz=TZ,
):
    return {
        "case_id": "c1",
        "deadline_key": key,
        "title": title,
        "due_at_utc": due_at_utc,
        "timezone": tz,
        "notes": None,
        "audit_notes": None,
        "status": status,
        "significance": significance,
        "deadline_type": "brief",
        "gcal_event_id": None,
        "docket_id": docket_id,
        "source_entry_ids": [9001],
    }


def _seed_docket(store, docket_id=101, court_id="mad", citation="D. Mass."):
    # Mirrors the store API the existing TestBuildCalendarModels tests use:
    # upsert_docket_meta(id, meta_dict) and the 4-arg positional upsert_court.
    store.upsert_docket_meta(
        docket_id,
        {
            "court_id": court_id,
            "docket_number": f"1:24-cr-{docket_id}",
            "case_name": "USA v. Tester",
            "absolute_url": f"/docket/{docket_id}/x/",
            "date_last_filing": "2026-05-01",
        },
    )
    store.set_docket_last_modified(docket_id, "2026-05-01T12:00:00Z")
    store.upsert_court(court_id, citation, court_id, f"{citation} (full)")


def _cfg(dockets=(101,)):
    return {
        "calendars": {"cyber": {"name": "Cyber", "ics_path": "out/cyber.ics"}},
        "cases": [
            {
                "id": "c1",
                "name": "USA v. Tester",
                "calendar": "cyber",
                "dockets": list(dockets),
            }
        ],
    }


def _ics_vevent_count(store, case_id="c1"):
    """Render the calendar's ICS exactly as cli.emit_calendars does and count
    its VEVENTs — the independent oracle for the filter-parity test."""
    rows = []
    for h in store.get_hearings(case_id):
        rows.append(("HEARING", dict(h)))
    for d in store.get_deadlines(case_id):
        mapped = cli._deadline_to_hearing(d)
        if mapped is not None:
            rows.append(("DEADLINE", mapped))
    composed = []
    for kind, h in rows:
        h = dict(h)
        h["title"] = cli._compose_title(
            raw_title=h["title"],
            kind=kind,
            case_name="Case",
            starts_at_utc=h.get("starts_at_utc"),
            duration_minutes=h.get("duration_minutes"),
        )
        composed.append(h)
    return ics.render_ics(calendar_name="C", hearings=composed).count("BEGIN:VEVENT")


class TestParseUtc:
    def test_none_returns_none(self):
        assert index._parse_utc(None) is None

    def test_empty_returns_none(self):
        assert index._parse_utc("") is None

    def test_garbage_returns_none(self):
        assert index._parse_utc("not-a-date") is None

    def test_naive_iso_assumed_utc(self):
        dt = index._parse_utc("2026-06-12T13:00:00")
        assert dt is not None
        assert dt.utcoffset() == timedelta(0)
        assert dt.hour == 13

    def test_aware_iso_preserved(self):
        dt = index._parse_utc("2026-06-12T13:00:00+00:00")
        assert dt is not None
        assert dt.hour == 13


class TestEventZone:
    def test_none_falls_back_to_utc(self):
        assert index._event_zone(None) is timezone.utc

    def test_empty_string_falls_back_to_utc(self):
        assert index._event_zone("") is timezone.utc

    def test_bogus_name_falls_back_to_utc(self):
        assert index._event_zone("Not/AZone") is timezone.utc

    def test_valid_name_returns_zoneinfo(self):
        assert index._event_zone("America/New_York") == ZoneInfo("America/New_York")


class TestFilterParityWithIcsFeed:
    """The preview must show EXACTLY the events the ICS feed carries."""

    def test_visible_set_count_equals_ics_vevent_count(self, store):
        _seed_docket(store)
        # A spectrum that exercises every filter branch in both paths.
        store.upsert_hearing(
            _hearing("trial", "Jury Trial", "2026-06-12T13:00:00+00:00")
        )
        store.upsert_hearing(
            _hearing(
                "minor",
                "Status (minor)",
                "2026-06-20T13:00:00+00:00",
                significance="minor",
            )
        )
        store.upsert_hearing(
            _hearing(
                "canc", "Cancelled", "2026-07-01T13:00:00+00:00", status="cancelled"
            )
        )
        store.upsert_hearing(
            _hearing("held", "Status Conf", "2026-05-22T13:00:00+00:00", status="held")
        )
        store.upsert_hearing(_hearing("nodate", "No date hearing", None))
        store.upsert_deadline(
            _deadline("memo", "Memo due", "2026-07-09T21:00:00+00:00")
        )
        # `met` deadlines map to 'cancelled' in cli._deadline_to_hearing, so the
        # ICS feed hides them — the preview must hide them too.
        store.upsert_deadline(
            _deadline("filed", "Filed memo", "2026-06-15T21:00:00+00:00", status="met")
        )

        visible = index._visible_events_for_case(store, "c1")
        assert len(visible) == _ics_vevent_count(store)

    def test_excludes_minor_cancelled_met_and_dateless(self, store):
        _seed_docket(store)
        store.upsert_hearing(
            _hearing("trial", "Jury Trial", "2026-06-12T13:00:00+00:00")
        )
        store.upsert_hearing(
            _hearing(
                "minor",
                "Minor Status",
                "2026-06-20T13:00:00+00:00",
                significance="minor",
            )
        )
        store.upsert_hearing(
            _hearing(
                "canc",
                "Cancelled Trial",
                "2026-07-01T13:00:00+00:00",
                status="cancelled",
            )
        )
        store.upsert_hearing(_hearing("nodate", "Dateless", None))
        store.upsert_deadline(
            _deadline("memo", "Pending Memo", "2026-07-09T21:00:00+00:00")
        )
        store.upsert_deadline(
            _deadline("filed", "Filed Memo", "2026-06-15T21:00:00+00:00", status="met")
        )

        titles = {e["title"] for e in index._visible_events_for_case(store, "c1")}
        assert titles == {"Jury Trial", "Pending Memo"}

    def test_includes_past_held_and_passed_deadline(self, store):
        _seed_docket(store)
        store.upsert_hearing(
            _hearing("held", "Held Conf", "2026-05-22T13:00:00+00:00", status="held")
        )
        # A 'passed' (past-due, unmet) deadline maps to 'held' and is shown.
        store.upsert_deadline(
            _deadline(
                "passed",
                "Lapsed deadline",
                "2026-05-20T21:00:00+00:00",
                status="passed",
            )
        )
        titles = {e["title"] for e in index._visible_events_for_case(store, "c1")}
        assert titles == {"Held Conf", "Lapsed deadline"}

    def test_sorted_ascending_by_start(self, store):
        _seed_docket(store)
        store.upsert_hearing(_hearing("c", "Third", "2026-08-01T13:00:00+00:00"))
        store.upsert_hearing(_hearing("a", "First", "2026-06-01T13:00:00+00:00"))
        store.upsert_hearing(_hearing("b", "Second", "2026-07-01T13:00:00+00:00"))
        order = [e["title"] for e in index._visible_events_for_case(store, "c1")]
        assert order == ["First", "Second", "Third"]


class TestWindowEvents:
    def _ev(self, title, start, no_time=False):
        return {
            "kind": "HEARING",
            "title": title,
            "starts_at_utc": start,
            "timezone": TZ,
            "no_time": no_time,
            "docket_id": 101,
        }

    def test_empty_returns_empty(self):
        assert index._window_events([], NOW) == ([], [])

    def test_drops_events_older_than_grace_window(self):
        events = [
            self._ev("Ancient", "2026-01-01T13:00:00+00:00"),  # > 14 days past
            self._ev("Recent", "2026-05-22T13:00:00+00:00"),  # within grace
            self._ev("Future", "2026-06-12T13:00:00+00:00"),
        ]
        shown, overflow = index._window_events(events, NOW)
        titles = [e["title"] for _, e in shown]
        assert "Ancient" not in titles
        assert titles == ["Recent", "Future"]
        assert overflow == []

    def test_recent_past_first_then_upcoming(self):
        events = [
            self._ev("Future", "2026-06-12T13:00:00+00:00"),
            self._ev("Recent", "2026-05-25T13:00:00+00:00"),
        ]
        shown, _ = index._window_events(events, NOW)
        assert [e["title"] for _, e in shown] == ["Recent", "Future"]

    def test_caps_recent_past_to_two_most_recent(self):
        events = [
            self._ev("P1", "2026-05-19T13:00:00+00:00"),
            self._ev("P2", "2026-05-21T13:00:00+00:00"),
            self._ev("P3", "2026-05-29T13:00:00+00:00"),
        ]
        shown, overflow = index._window_events(events, NOW)
        # Only the two MOST-RECENT past rows survive the cap.
        assert [e["title"] for _, e in shown] == ["P2", "P3"]
        assert overflow == []  # past rows never go into the upcoming overflow

    def test_caps_upcoming_and_overflows_the_rest(self):
        events = [
            self._ev(f"U{i}", f"2026-06-{10 + i:02d}T13:00:00+00:00") for i in range(9)
        ]
        shown, overflow = index._window_events(events, NOW)
        cap = index._EVENT_MAX_UPCOMING
        assert [e["title"] for _, e in shown] == [f"U{i}" for i in range(cap)]
        # The rest spill into the overflow, in order, for the expandable block.
        assert [e["title"] for _, e in overflow] == [f"U{i}" for i in range(cap, 9)]

    def test_unparseable_timestamp_skipped(self):
        events = [
            self._ev("Bad", "not-a-date"),
            self._ev("Good", "2026-06-12T13:00:00+00:00"),
        ]
        shown, _ = index._window_events(events, NOW)
        assert [e["title"] for _, e in shown] == ["Good"]


class TestDecorateEvent:
    def _ev(self, **kw):
        base = {
            "kind": "HEARING",
            "title": "Jury Trial",
            "starts_at_utc": "2026-06-12T13:00:00+00:00",
            "timezone": TZ,
            "no_time": False,
        }
        base.update(kw)
        return base

    def _decorate(self, ev, court=None):
        dt = index._parse_utc(ev["starts_at_utc"])
        assert dt is not None
        return index._decorate_event(ev, dt, NOW, court)

    def test_court_local_date_and_time(self):
        dec = self._decorate(self._ev(), court="D. Mass.")
        assert dec["month"] == "JUN"
        assert dec["day"] == "12"
        # 13:00 UTC -> 9:00 AM EDT
        assert dec["time_label"] == "9:00 AM EDT"
        assert dec["court_citation"] == "D. Mass."
        assert dec["is_past"] is False

    def test_date_only_future_is_time_tbd(self):
        dec = self._decorate(
            self._ev(starts_at_utc="2026-08-21T00:00:00+00:00", no_time=True)
        )
        assert dec["time_label"] == "time TBD"

    def test_date_only_past_is_time_unknown(self):
        dec = self._decorate(
            self._ev(starts_at_utc="2026-04-01T00:00:00+00:00", no_time=True)
        )
        assert dec["time_label"] == "time unknown"
        assert dec["is_past"] is True

    def test_bogus_timezone_falls_back_without_crashing(self):
        dec = self._decorate(self._ev(timezone="Not/AZone"))
        # Falls back to UTC: 13:00 UTC renders as 1:00 PM.
        assert "1:00 PM" in dec["time_label"]


class TestRenderEvents:
    def _dec(self, **kw):
        base = {
            "kind": "HEARING",
            "title": "Jury Trial",
            "month": "JUN",
            "day": "12",
            "time_label": "9:00 AM EDT",
            "is_past": False,
            "court_citation": "D. Mass.",
        }
        base.update(kw)
        return base

    def test_empty_renders_nothing(self):
        assert index._render_events({}) == ""
        assert index._render_events({"events": []}) == ""

    def test_renders_date_chip_badge_time_and_court(self):
        html = index._render_events({"events": [self._dec()]})
        assert '<div class="events">' in html
        assert "Upcoming events" in html
        assert ">JUN<" in html and ">12<" in html
        assert "9:00 AM EDT" in html
        assert "D. Mass." in html
        assert "Jury Trial" in html
        assert "event-badge-hearing" in html
        assert ">Hearing<" in html

    def test_deadline_gets_deadline_badge_and_class(self):
        html = index._render_events(
            {"events": [self._dec(kind="DEADLINE", title="Memo due")]}
        )
        assert "event-deadline" in html
        assert "event-badge-deadline" in html
        assert ">Deadline<" in html

    def test_past_event_gets_muted_class(self):
        html = index._render_events({"events": [self._dec(is_past=True)]})
        assert "event-past" in html

    def test_overflow_renders_as_expandable_details(self):
        overflow = [
            self._dec(title="Overflow A", day="20"),
            self._dec(title="Overflow B", day="21"),
            self._dec(title="Overflow C", day="22"),
        ]
        html = index._render_events(
            {"events": [self._dec()], "events_overflow": overflow}
        )
        # Native <details> disclosure — no JS — with the count in the summary.
        assert '<details class="events-more">' in html
        assert "<summary>+3 more upcoming</summary>" in html
        # The hidden rows are in the markup, revealed on click.
        assert "Overflow A" in html and "Overflow C" in html

    def test_no_overflow_block_when_empty(self):
        html = index._render_events({"events": [self._dec()], "events_overflow": []})
        assert "more upcoming" not in html
        assert "events-more" not in html

    def test_no_court_block_when_absent(self):
        html = index._render_events({"events": [self._dec(court_citation=None)]})
        assert "event-court" not in html

    def test_title_is_html_escaped(self):
        html = index._render_events({"events": [self._dec(title="<script>x</script>")]})
        assert "<script>x</script>" not in html
        assert "&lt;script&gt;" in html


class TestBuildCalendarModelsEvents:
    def test_attaches_windowed_events_and_search_titles(self, store):
        _seed_docket(store)
        store.upsert_hearing(
            _hearing("trial", "Jury Trial", "2026-06-12T13:00:00+00:00")
        )
        store.upsert_hearing(
            _hearing(
                "minor",
                "Minor Status",
                "2026-06-20T13:00:00+00:00",
                significance="minor",
            )
        )
        store.upsert_deadline(
            _deadline("memo", "Memo due", "2026-07-09T21:00:00+00:00")
        )

        case = index.build_calendar_models(_cfg(), store, now=NOW)[0]["cases"][0]
        titles = [e["title"] for e in case["events"]]
        assert titles == ["Jury Trial", "Memo due"]
        assert case["events_overflow"] == []
        # Search titles carry the full visible set (minor excluded).
        assert set(case["event_search_titles"]) == {"Jury Trial", "Memo due"}
        # Court citation decorated from the docket metadata.
        assert all(e["court_citation"] == "D. Mass." for e in case["events"])

    def test_overflow_is_populated_and_searchable(self, store):
        _seed_docket(store)
        # 8 upcoming hearings — more than the display cap, so the tail spills
        # into the expandable overflow.
        for i in range(8):
            store.upsert_hearing(
                _hearing(
                    f"h{i}", f"Hearing {i}", f"2026-06-{10 + i:02d}T13:00:00+00:00"
                )
            )
        models = index.build_calendar_models(_cfg(), store, now=NOW)
        case = models[0]["cases"][0]
        cap = index._EVENT_MAX_UPCOMING
        assert len(case["events"]) == cap
        assert len(case["events_overflow"]) == 8 - cap
        # Every event — shown or overflow — is in the search haystack.
        assert {f"Hearing {i}" for i in range(8)} <= set(case["event_search_titles"])
        # The overflow rows render inside the expandable block on the full page.
        html = index.render_index(calendars=models)
        assert '<details class="events-more">' in html
        assert "Hearing 7" in html  # an overflow row (revealed on click)

    def test_quiet_case_has_no_events(self, store):
        _seed_docket(store)
        # Only an out-of-window past event -> nothing to show.
        store.upsert_hearing(
            _hearing("old", "Old Conf", "2026-01-01T13:00:00+00:00", status="held")
        )
        case = index.build_calendar_models(_cfg(), store, now=NOW)[0]["cases"][0]
        assert case["events"] == []
        assert index._render_events(case) == ""

    def test_multi_docket_events_labeled_by_their_own_court(self, store):
        _seed_docket(store, docket_id=101, court_id="mad", citation="D. Mass.")
        _seed_docket(store, docket_id=201, court_id="cand", citation="N.D. Cal.")
        store.upsert_hearing(
            _hearing(
                "dist", "District Hearing", "2026-06-12T13:00:00+00:00", docket_id=101
            )
        )
        store.upsert_hearing(
            _hearing(
                "app", "Appellate Argument", "2026-06-20T17:00:00+00:00", docket_id=201
            )
        )
        case = index.build_calendar_models(_cfg(dockets=(101, 201)), store, now=NOW)[0][
            "cases"
        ][0]
        by_title = {e["title"]: e["court_citation"] for e in case["events"]}
        assert by_title == {
            "District Hearing": "D. Mass.",
            "Appellate Argument": "N.D. Cal.",
        }


class TestCaseSearchTextEvents:
    def test_event_titles_join_haystack(self):
        case = {
            "name": "USA v. X",
            "summaries": [],
            "event_search_titles": ["Suppression Hearing", "Daubert briefing due"],
        }
        hay = index._case_search_text(case)
        assert "suppression hearing" in hay
        assert "daubert briefing due" in hay

    def test_falsy_event_titles_skipped(self):
        case = {
            "name": "USA v. X",
            "summaries": [],
            "event_search_titles": ["", None, "Real Hearing"],
        }
        hay = index._case_search_text(case)
        assert "real hearing" in hay

    def test_missing_event_titles_is_safe(self):
        # Hand-built case dicts (as in other tests) lack the key entirely.
        assert index._case_search_text({"name": "x", "summaries": []}) == "x"


class TestRenderIndexIncludesEvents:
    def test_events_block_appears_in_full_page(self, store):
        _seed_docket(store)
        store.upsert_hearing(
            _hearing("trial", "Jury Trial", "2026-06-12T13:00:00+00:00")
        )
        models = index.build_calendar_models(_cfg(), store, now=NOW)
        html = index.render_index(calendars=models)
        assert '<div class="events">' in html
        assert "Jury Trial" in html
        # Event title is searchable from the page's data-search haystack.
        assert "jury trial" in html
