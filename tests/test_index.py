"""Tests for the static index.html renderer."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from case_calendar.calendars.index import (
    _esc,
    _format_date,
    _ics_links,
    _render_case,
    _render_summaries,
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

    def test_http_scheme_stripped(self):
        # Same webcal:// derivation works for http://-prefixed base URLs.
        links = _ics_links("cyber.ics", public_base_url="http://x.example.com")
        assert links["webcal"] == "webcal://x.example.com/cyber.ics"
        assert links["https"] == "http://x.example.com/cyber.ics"

    def test_unscheme_base_url_kept_as_is(self):
        # If the operator forgets the scheme, we treat the value as the
        # host:path and derive webcal:// from it directly.
        links = _ics_links("cyber.ics", public_base_url="x.example.com")
        assert links["webcal"] == "webcal://x.example.com/cyber.ics"


class TestEsc:
    def test_none_returns_empty(self):
        assert _esc(None) == ""

    def test_escapes_html_special_chars(self):
        assert _esc('<script>"a"') == "&lt;script&gt;&quot;a&quot;"


class TestFormatDate:
    def test_truncates_iso_to_date(self):
        assert _format_date("2026-05-10T12:00:00Z") == "2026-05-10"

    def test_empty_returns_empty(self):
        assert _format_date(None) == ""
        assert _format_date("") == ""


class TestRenderSummaries:
    def test_empty_returns_empty(self):
        assert _render_summaries({"summaries": []}, dockets=[]) == ""
        assert _render_summaries({}, dockets=[]) == ""

    def test_single_summary_no_docket_label(self):
        html = _render_summaries(
            {"summaries": [{"docket_id": 1, "summary": "It is a case."}]},
            dockets=[{"docket_id": 1, "docket_number": "1:24-cv-1",
                       "court_citation": "S.D.N.Y."}],
        )
        # Single summary -> no docket-label span (multi==False).
        assert '<div class="summary">' in html
        assert "It is a case." in html
        assert "docket-label" not in html

    def test_multiple_summaries_get_docket_labels(self):
        html = _render_summaries(
            {"summaries": [
                {"docket_id": 1, "summary": "Trial court matter."},
                {"docket_id": 2, "summary": "On appeal."},
            ]},
            dockets=[
                {"docket_id": 1, "docket_number": "1:24-cv-1",
                 "court_citation": "S.D.N.Y."},
                {"docket_id": 2, "docket_number": "24-1234",
                 "court_citation": "2d Cir."},
            ],
        )
        assert 'class="docket-label"' in html
        assert "1:24-cv-1" in html and "24-1234" in html
        assert "Trial court matter." in html and "On appeal." in html

    def test_empty_summary_strings_are_skipped(self):
        html = _render_summaries(
            {"summaries": [
                {"docket_id": 1, "summary": ""},
                {"docket_id": 2, "summary": "   "},
            ]},
            dockets=[],
        )
        # Both empty/whitespace summaries skipped -> nothing rendered.
        assert html == ""

    def test_summary_text_is_escaped(self):
        html = _render_summaries(
            {"summaries": [{"docket_id": 1, "summary": "<script>"}]},
            dockets=[],
        )
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestRenderCaseEdges:
    def test_docket_without_absolute_url_renders_plain_label(self):
        # No absolute_url -> the docket label is plain text, not a link.
        html = _render_case({
            "name": "US v. X",
            "dockets": [{"docket_id": 42, "docket_number": "1:24-cr-42",
                          "court_citation": "S.D.N.Y."}],
            "date_filed": None, "last_filing_date": None,
        })
        # Label appears as a bare <li>, not wrapped in <a>.
        assert "1:24-cr-42" in html
        assert "<a href" not in html

    def test_docket_with_only_id_renders_id_as_label(self):
        # No docket_number, no court_citation -> the label falls back to the
        # docket_id as a stringified value (no link either).
        html = _render_case({
            "name": "X", "dockets": [{"docket_id": 999}],
            "date_filed": None, "last_filing_date": None,
        })
        assert "999" in html

    def test_last_filing_renders_as_last_filing_label(self):
        # The visible label was historically "Last activity" and sourced
        # from docket.date_modified, which conflates filings with OCR /
        # metadata churn. The renderer now reads ``last_filing_date`` and
        # labels it "Last filing".
        html = _render_case({
            "name": "US v. X",
            "dockets": [{"docket_id": 1, "docket_number": "1:24-cr-1"}],
            "date_filed": "2025-01-15",
            "last_filing_date": "2026-05-10",
        })
        assert "<b>Last filing</b> 2026-05-10" in html
        assert "Last activity" not in html
        assert 'data-last-filing="2026-05-10"' in html

    def test_no_last_filing_skips_label(self):
        # Cases with no captured date_last_filing yet still render — the
        # date row simply omits the "Last filing" span.
        html = _render_case({
            "name": "US v. Y",
            "dockets": [],
            "date_filed": None,
            "last_filing_date": None,
        })
        assert "Last filing" not in html


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
                    "last_filing_date": "2026-05-10",
                },
                {
                    "id": "us-v-y",
                    "name": "US v. <evil>",  # XSS canary
                    "dockets": [],
                    "date_filed": None,
                    "last_filing_date": None,
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

    def test_meta_description_default_and_override(self, calendars):
        # Default is the function-based description that survives the
        # case list changing; override flows through unescaped-for-content
        # but HTML-escaped for attribute safety.
        html = render_index(calendars=calendars)
        assert (
            '<meta name="description" content="Subscribable calendar feeds '
            'for federal court hearings and filing deadlines, sourced from '
            'CourtListener and RECAP.">'
        ) in html
        html = render_index(
            calendars=calendars,
            site_description='Custom "feed" description',
        )
        assert (
            '<meta name="description" '
            'content="Custom &quot;feed&quot; description">'
        ) in html

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
        assert 'data-last-filing="2026-05-10"' in html
        # The xss-canary case has no dates; sort attrs are still emitted
        # (empty strings sort last in the JS comparator).
        assert 'data-filed=""' in html

    def test_sort_dropdown_default_is_last_filing(self, calendars):
        # The default sort option (selected on page load) is "Last filing",
        # backed by data-last-filing. The JS reads 'data-' + option.value,
        # so the value here must match the attribute the renderer emits.
        html = render_index(calendars=calendars)
        assert '<option value="last-filing" selected>Last filing</option>' in html
        # The old "Last activity" wording is gone — that label was the bug
        # this change fixes.
        assert "Last activity" not in html

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
            "date_last_filing": "2026-05-10",
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
        assert case["last_filing_date"] == "2026-05-10"
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
        assert case["last_filing_date"] is None
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
