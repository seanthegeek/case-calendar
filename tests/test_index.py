"""Tests for the static index.html renderer."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from case_calendar.calendars.index import (
    _ics_links,
    build_calendar_models,
    render_index,
    write_index,
)


class TestIcsLinks:
    def test_no_base_url_returns_relative_only(self):
        links = _ics_links("out/cyber.ics", public_base_url=None)
        assert links == {
            "webcal": None,
            "https": None,
            "relative": "cyber.ics",
        }

    def test_https_base_url_builds_absolute_and_webcal(self):
        links = _ics_links(
            "out/cyber.ics",
            public_base_url="https://calendars.example.com",
        )
        assert links["https"] == "https://calendars.example.com/cyber.ics"
        # webcal:// strips the scheme — Apple Calendar / Outlook auto-subscribe
        # uses webcal:// even though the underlying fetch is HTTPS.
        assert links["webcal"] == "webcal://calendars.example.com/cyber.ics"
        assert links["relative"] == "cyber.ics"

    def test_trailing_slash_normalized(self):
        links = _ics_links(
            "out/cyber.ics",
            public_base_url="https://x.example.com/",
        )
        assert links["https"] == "https://x.example.com/cyber.ics"

    def test_no_ics_path_yields_none(self):
        # Calendars without an ics_path (gcal-only / m365-only) shouldn't
        # surface a broken "subscribe" link in the index.
        links = _ics_links(None, public_base_url="https://x.example.com")
        assert links == {"webcal": None, "https": None, "relative": None}


class TestRenderIndex:
    @pytest.fixture
    def calendars(self):
        return [{
            "id": "cyber",
            "name": "Cybercrime cases",
            "links": {
                "webcal": "webcal://x/cyber.ics",
                "https": "https://x/cyber.ics",
                "relative": "cyber.ics",
            },
            "cases": [
                {
                    "id": "us-v-x",
                    "name": "US v. X",
                    "dockets": [{
                        "docket_id": 1,
                        "docket_number": "1:24-cr-12345",
                        "court_id": "nysd",
                        "court_citation": "S.D.N.Y.",
                        "absolute_url": "/docket/1/x/",
                    }],
                    "date_filed": "2025-01-15",
                    "activity_date": "2026-05-10T12:00:00Z",
                },
                {
                    "id": "us-v-y",
                    "name": "US v. <evil>",  # XSS canary
                    "dockets": [],
                    "date_filed": None,
                    "activity_date": None,
                },
            ],
        }]

    def test_produces_valid_html_skeleton(self, calendars):
        html = render_index(
            calendars=calendars,
            generated_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
        )
        assert html.startswith("<!doctype html>")
        assert html.rstrip().endswith("</html>")
        assert '<meta charset="utf-8">' in html

    def test_declares_color_scheme_for_darkreader(self, calendars):
        # Darkreader treats a page as dark-aware (and skips its own filter)
        # when it sees the color-scheme meta + matching CSS declaration.
        # Both must ship together for the detection to fire reliably.
        html = render_index(calendars=calendars)
        assert '<meta name="color-scheme" content="light dark">' in html
        assert "color-scheme: light dark" in html

    def test_prefers_color_scheme_media_query_present(self, calendars):
        html = render_index(calendars=calendars)
        assert "prefers-color-scheme: dark" in html

    def test_theme_toggle_button_present(self, calendars):
        html = render_index(calendars=calendars)
        assert 'id="theme-toggle"' in html

    def test_subscribe_links_rendered(self, calendars):
        html = render_index(calendars=calendars)
        assert "webcal://x/cyber.ics" in html
        assert "https://x/cyber.ics" in html
        # Download link uses the relative filename so it works under file:// too.
        assert 'href="cyber.ics" download' in html

    def test_docket_absolute_url_promoted_to_full_cl_url(self, calendars):
        # CL gives us absolute_url as a path. The index renders into pages
        # that may be served from any host, so the link has to be fully
        # qualified or it 404s under Caddy.
        html = render_index(calendars=calendars)
        assert "https://www.courtlistener.com/docket/1/x/" in html

    def test_case_metadata_sort_attributes(self, calendars):
        # Sorting happens client-side off data-* attrs. The renderer has
        # to emit lowercase-name (case-insensitive sort) and ISO dates
        # (lex-sortable) for every case row.
        html = render_index(calendars=calendars)
        assert 'data-name="us v. x"' in html
        assert 'data-filed="2025-01-15"' in html
        assert 'data-activity="2026-05-10"' in html
        # The xss-canary case has no dates; sort attrs are still emitted
        # (empty strings sort last in the JS comparator).
        assert 'data-filed=""' in html

    def test_xss_in_case_name_is_escaped(self, calendars):
        html = render_index(calendars=calendars)
        assert "<evil>" not in html
        assert "&lt;evil&gt;" in html

    def test_empty_calendar_renders_placeholder(self):
        html = render_index(calendars=[{
            "id": "empty",
            "name": "Empty",
            "links": {"webcal": None, "https": None, "relative": None},
            "cases": [],
        }])
        assert "No cases configured" in html


class TestBuildCalendarModels:
    def test_assembles_from_store(self, store):
        # Seed the store with docket metadata + an entry that has date_filed,
        # so build_calendar_models has aggregates to surface.
        store.upsert_docket_meta(100, {
            "court_id": "nysd",
            "docket_number": "1:24-cr-12345",
            "case_name": "US v. X",
            "absolute_url": "/docket/100/x/",
        })
        store.set_docket_last_modified(100, "2026-05-10T12:00:00Z")
        store.upsert_court("nysd", "S.D.N.Y.", "nysd", "Southern District of NY")
        store.mark_entry(100, 1, "2025-01-15T08:00:00Z", "fp",
                         date_filed="2025-01-15", entry_number=1)
        cfg = {
            "calendars": {
                "cyber": {"name": "Cybercrime", "ics_path": "out/cyber.ics"},
            },
            "cases": [
                {"id": "us-v-x", "name": "US v. X",
                 "calendar": "cyber", "dockets": [100]},
            ],
        }
        models = build_calendar_models(
            cfg, store, public_base_url="https://calendars.example.com",
        )
        assert len(models) == 1
        cal = models[0]
        assert cal["id"] == "cyber"
        assert cal["links"]["https"] == "https://calendars.example.com/cyber.ics"
        assert len(cal["cases"]) == 1
        case = cal["cases"][0]
        assert case["date_filed"] == "2025-01-15"
        assert case["activity_date"] == "2026-05-10T12:00:00Z"
        assert case["dockets"][0]["docket_number"] == "1:24-cr-12345"
        assert case["dockets"][0]["court_citation"] == "S.D.N.Y."

    def test_handles_unseen_docket(self, store):
        # Case configured but no sync has happened yet — every aggregate
        # is None and the docket metadata is empty. The renderer still
        # has to cope.
        cfg = {
            "calendars": {"cyber": {"name": "Cybercrime",
                                    "ics_path": "out/cyber.ics"}},
            "cases": [
                {"id": "us-v-x", "name": "US v. X",
                 "calendar": "cyber", "dockets": [999]},
            ],
        }
        models = build_calendar_models(cfg, store)
        case = models[0]["cases"][0]
        assert case["date_filed"] is None
        assert case["activity_date"] is None
        assert case["dockets"][0]["docket_number"] is None


class TestWriteIndex:
    def test_writes_file_and_creates_parent_dir(self, tmp_path):
        target = tmp_path / "site" / "index.html"
        write_index(target, calendars=[{
            "id": "x", "name": "X",
            "links": {"webcal": None, "https": None, "relative": None},
            "cases": [],
        }])
        assert target.exists()
        assert target.read_text(encoding="utf-8").startswith("<!doctype html>")
