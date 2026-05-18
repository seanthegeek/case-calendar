"""Tests for the static index.html renderer."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from case_calendar.calendars.index import (
    _case_search_text,
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
            {
                "summaries": [
                    {
                        "docket_number": "1:24-cv-1",
                        "court_id": "nysd",
                        "summary": "It is a case.",
                    }
                ]
            },
            dockets=[
                {
                    "docket_id": 1,
                    "docket_number": "1:24-cv-1",
                    "court_id": "nysd",
                    "court_citation": "S.D.N.Y.",
                }
            ],
        )
        # Single summary -> no docket-label span (multi==False).
        assert '<div class="summary">' in html
        assert "It is a case." in html
        assert "docket-label" not in html

    def test_multiple_summaries_get_docket_labels(self):
        html = _render_summaries(
            {
                "summaries": [
                    {
                        "docket_number": "1:24-cv-1",
                        "court_id": "nysd",
                        "summary": "Trial court matter.",
                    },
                    {
                        "docket_number": "24-1234",
                        "court_id": "ca2",
                        "summary": "On appeal.",
                    },
                ]
            },
            dockets=[
                {
                    "docket_id": 1,
                    "docket_number": "1:24-cv-1",
                    "court_id": "nysd",
                    "court_citation": "S.D.N.Y.",
                },
                {
                    "docket_id": 2,
                    "docket_number": "24-1234",
                    "court_id": "ca2",
                    "court_citation": "2d Cir.",
                },
            ],
        )
        assert 'class="docket-label"' in html
        assert "1:24-cv-1" in html and "24-1234" in html
        assert "Trial court matter." in html and "On appeal." in html

    def test_cl_docket_splits_collapse_to_one_paragraph(self):
        # The Akhter shape: three CL docket_ids share one (docket_number,
        # court_id) — the index renders a SINGLE paragraph for the group,
        # NOT three labeled near-duplicates.
        html = _render_summaries(
            {
                "summaries": [
                    {
                        "docket_number": "1:25-cr-00307",
                        "court_id": "vaed",
                        "summary": "Pooled summary across all three CL siblings.",
                    },
                ]
            },
            dockets=[
                {
                    "docket_id": 71989485,
                    "docket_number": "1:25-cr-00307",
                    "court_id": "vaed",
                    "court_citation": "E.D. Va.",
                    "sibling_docket_ids": [73333500, 73320754],
                }
            ],
        )
        # One summary → no docket label, even though three CL siblings.
        assert "Pooled summary across all three CL siblings." in html
        assert "docket-label" not in html

    def test_empty_summary_strings_are_skipped(self):
        html = _render_summaries(
            {
                "summaries": [
                    {
                        "docket_number": "1:24-cv-1",
                        "court_id": "nysd",
                        "summary": "",
                    },
                    {
                        "docket_number": "1:24-cv-2",
                        "court_id": "nysd",
                        "summary": "   ",
                    },
                ]
            },
            dockets=[],
        )
        # Both empty/whitespace summaries skipped -> nothing rendered.
        assert html == ""

    def test_summary_text_is_escaped(self):
        html = _render_summaries(
            {
                "summaries": [
                    {
                        "docket_number": "1:24-cv-1",
                        "court_id": "nysd",
                        "summary": "<script>",
                    }
                ]
            },
            dockets=[],
        )
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestRenderCaseEdges:
    def test_docket_without_absolute_url_renders_plain_label(self):
        # No absolute_url -> the docket label is plain text, not a link.
        html = _render_case(
            {
                "name": "US v. X",
                "dockets": [
                    {
                        "docket_id": 42,
                        "docket_number": "1:24-cr-42",
                        "court_citation": "S.D.N.Y.",
                    }
                ],
                "date_filed": None,
                "last_filing_date": None,
            }
        )
        # Label appears as a bare <li>, not wrapped in <a>.
        assert "1:24-cr-42" in html
        assert "<a href" not in html

    def test_docket_with_only_id_renders_id_as_label(self):
        # No docket_number, no court_citation -> the label falls back to the
        # docket_id as a stringified value (no link either).
        html = _render_case(
            {
                "name": "X",
                "dockets": [{"docket_id": 999}],
                "date_filed": None,
                "last_filing_date": None,
            }
        )
        assert "999" in html

    def test_last_filing_renders_as_last_filing_label(self):
        # The visible label was historically "Last activity" and sourced
        # from docket.date_modified, which conflates filings with OCR /
        # metadata churn. The renderer now reads ``last_filing_date`` and
        # labels it "Last filing".
        html = _render_case(
            {
                "name": "US v. X",
                "dockets": [{"docket_id": 1, "docket_number": "1:24-cr-1"}],
                "date_filed": "2025-01-15",
                "last_filing_date": "2026-05-10",
            }
        )
        assert "<b>Last filing</b> 2026-05-10" in html
        assert "Last activity" not in html
        assert 'data-last-filing="2026-05-10"' in html

    def test_no_last_filing_skips_label(self):
        # Cases with no captured date_last_filing yet still render — the
        # date row simply omits the "Last filing" span.
        html = _render_case(
            {
                "name": "US v. Y",
                "dockets": [],
                "date_filed": None,
                "last_filing_date": None,
            }
        )
        assert "Last filing" not in html


class TestCaseSearchText:
    def test_includes_name_dockets_courts_summaries_lowercased(self):
        text = _case_search_text(
            {
                "name": "US v. Wang",
                "dockets": [
                    {"docket_number": "1:24-cr-12345", "court_citation": "S.D.N.Y."},
                    {"docket_number": "24-1234", "court_citation": "2d Cir."},
                ],
                "summaries": [
                    {"docket_id": 1, "summary": "Indicted on wire fraud."},
                    {"docket_id": 2, "summary": "Appeal pending."},
                ],
            }
        )
        # Lowercased so the JS comparator can do a single-pass indexOf.
        assert text == text.lower()
        for needle in [
            "us v. wang",
            "1:24-cr-12345",
            "s.d.n.y.",
            "24-1234",
            "2d cir.",
            "indicted on wire fraud.",
            "appeal pending.",
        ]:
            assert needle in text, needle

    def test_skips_empty_or_missing_fields(self):
        # Missing name, no dockets, blank/whitespace summary -> empty string,
        # which the JS treats as "no match for any non-empty query".
        assert _case_search_text({}) == ""
        assert (
            _case_search_text(
                {
                    "name": "",
                    "dockets": [],
                    "summaries": [{"summary": "   "}],
                }
            )
            == ""
        )


class TestRenderIndex:
    @pytest.fixture
    def calendars(self):
        return [
            {
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
                        "dockets": [
                            {
                                "docket_id": 1,
                                "docket_number": "1:24-cr-12345",
                                "court_id": "nysd",
                                "court_citation": "S.D.N.Y.",
                                "absolute_url": "/docket/1/x/",
                            }
                        ],
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
            }
        ]

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
            "for federal court hearings and filing deadlines, sourced from "
            'CourtListener and RECAP.">'
        ) in html
        html = render_index(
            calendars=calendars,
            site_description='Custom "feed" description',
        )
        assert (
            '<meta name="description" content="Custom &quot;feed&quot; description">'
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
        # webcal:// stays as a one-click subscribe anchor.
        assert "webcal://x/cyber.ics" in html
        # The https URL is no longer rendered as its own anchor — it's
        # the data-url payload on the Copy-feed-URL button. The old
        # standalone "HTTPS feed" link AND the redundant Download link
        # are both gone when a public_base_url is configured: clicking
        # Subscribe in a desktop browser already downloads the .ics, and
        # the button copies the same URL Download used to point at.
        assert 'class="copy-feed" data-url="https://x/cyber.ics"' in html
        assert ">Copy feed URL<" in html
        assert ">HTTPS feed<" not in html
        assert 'href="cyber.ics" download' not in html

    def test_no_public_base_url_falls_back_to_download(self):
        # Without a public_base_url, links["https"] / links["webcal"] are
        # both None — copying a bare relative filename like "cyber.ics"
        # would be useless to subscribers, so the renderer falls back to
        # a plain Download link instead of the copy button.
        html = render_index(
            calendars=[
                {
                    "id": "c",
                    "name": "C",
                    "links": {"webcal": None, "https": None, "relative": "c.ics"},
                    "cases": [],
                }
            ]
        )
        assert 'href="c.ics" download' in html
        # The JS still ships its querySelectorAll('button.copy-feed') line
        # — it's harmless when no button matches — so the assertion guards
        # the rendered button element specifically, not the bare string.
        assert 'class="copy-feed"' not in html
        assert ">Copy feed URL<" not in html

    def test_copy_feed_button_handler_present(self, calendars):
        # The runtime JS attaches one click handler per copy-feed button
        # and reads data-url at click time. Both the selector and the
        # success/failure transitions are part of the contract, so guard
        # the strings so a future refactor doesn't silently drop them.
        html = render_index(calendars=calendars)
        assert "button.copy-feed" in html
        assert "navigator.clipboard.writeText" in html
        assert "'Copied!'" in html
        assert "is-copied" in html
        assert "'Copy failed'" in html

    def test_docket_absolute_url_promoted_to_full_cl_url(self, calendars):
        # CourtListener gives us absolute_url as a path. The index renders into pages
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

    def test_search_bar_rendered(self, calendars):
        # The global search input sits in its own bar between <header> and
        # <main>; the JS hooks find it by id, so both the input id and the
        # status span id are part of the contract.
        html = render_index(calendars=calendars)
        assert 'id="case-search"' in html
        assert 'type="search"' in html
        assert 'id="search-status"' in html
        # aria-live makes the result count discoverable to screen readers
        # without requiring focus.
        assert 'aria-live="polite"' in html

    def test_case_rows_carry_data_search_attribute(self):
        # Each case row needs a lowercased haystack the JS can substring-
        # match against. Includes name + docket number + court citation +
        # summary prose.
        html = render_index(
            calendars=[
                {
                    "id": "c",
                    "name": "C",
                    "links": {"webcal": None, "https": None, "relative": "c.ics"},
                    "cases": [
                        {
                            "id": "us-v-wang",
                            "name": "US v. Wang",
                            "dockets": [
                                {
                                    "docket_id": 1,
                                    "docket_number": "1:24-cr-12345",
                                    "court_citation": "S.D.N.Y.",
                                }
                            ],
                            "summaries": [
                                {"docket_id": 1, "summary": "Indicted on wire fraud."},
                            ],
                            "date_filed": None,
                            "last_filing_date": None,
                        }
                    ],
                }
            ]
        )
        assert "data-search=" in html
        # All four search-relevant fields must be present in the haystack
        # so subscribers can find a case by any of them.
        for needle in [
            "us v. wang",
            "1:24-cr-12345",
            "s.d.n.y.",
            "indicted on wire fraud.",
        ]:
            assert needle in html, needle

    def test_data_search_attribute_is_xss_safe(self):
        # Summary text is user-ish (LLM output); a quote or angle bracket
        # in there must not break out of the attribute.
        html = render_index(
            calendars=[
                {
                    "id": "c",
                    "name": "C",
                    "links": {"webcal": None, "https": None, "relative": "c.ics"},
                    "cases": [
                        {
                            "id": "x",
                            "name": 'US v. "X"',
                            "dockets": [],
                            "summaries": [
                                {"docket_id": 1, "summary": "<script>alert(1)</script>"}
                            ],
                            "date_filed": None,
                            "last_filing_date": None,
                        }
                    ],
                }
            ]
        )
        # Raw script tag must not survive into the data-search attribute.
        assert "<script>alert(1)</script>" not in html
        # Quotes in the name must be entity-encoded inside the attribute.
        assert 'data-search="' in html
        assert "us v. &quot;x&quot;" in html

    def test_empty_calendar_renders_placeholder(self):
        html = render_index(
            calendars=[
                {
                    "id": "empty",
                    "name": "Empty",
                    "links": {"webcal": None, "https": None, "relative": None},
                    "cases": [],
                }
            ]
        )
        assert "No cases configured" in html

    def test_footer_includes_powered_by_attribution(self, calendars):
        html = render_index(calendars=calendars)
        assert (
            '<p>Powered by <a href="https://docs.casecalendar.net/">'
            "Case Calendar</a>.</p>"
        ) in html


class TestBuildCalendarModels:
    def test_assembles_from_store(self, store):
        # Seed the store with docket metadata + an entry that has date_filed,
        # so build_calendar_models has aggregates to surface.
        store.upsert_docket_meta(
            100,
            {
                "court_id": "nysd",
                "docket_number": "1:24-cr-12345",
                "case_name": "US v. X",
                "absolute_url": "/docket/100/x/",
                "date_last_filing": "2026-05-10",
            },
        )
        store.set_docket_last_modified(100, "2026-05-10T12:00:00Z")
        store.upsert_court("nysd", "S.D.N.Y.", "nysd", "Southern District of NY")
        store.mark_entry(
            100,
            1,
            "2025-01-15T08:00:00Z",
            "fp",
            date_filed="2025-01-15",
            entry_number=1,
        )
        cfg = {
            "calendars": {
                "cyber": {"name": "Cybercrime", "ics_path": "out/cyber.ics"},
            },
            "cases": [
                {
                    "id": "us-v-x",
                    "name": "US v. X",
                    "calendar": "cyber",
                    "dockets": [100],
                },
            ],
        }
        models = build_calendar_models(
            cfg,
            store,
            public_base_url="https://calendars.example.com",
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

    def test_collapses_sibling_docket_ids_into_one_dockets_meta_entry(self, store):
        # When `case.dockets` lists multiple CL docket_ids that share
        # (docket_number, court_id) — the Akhter-shape PR #3 case where
        # one logical PACER docket lives under three CL docket_ids —
        # build_calendar_models must emit ONE entry in `dockets_meta`
        # with the other docket_ids in a `sibling_docket_ids` list,
        # NOT three near-duplicate entries. This exercises the
        # collapse branch (`if group_key in group_index: append to
        # siblings; continue`) — line 848-850 of index.py.
        for did in (71989485, 73333500, 73320754):
            store.upsert_docket_meta(
                did,
                {
                    "court_id": "vaed",
                    "docket_number": "1:25-cr-00307",
                    "case_name": "United States v. Akhter",
                    "absolute_url": f"/docket/{did}/akhter/",
                    "date_last_filing": "2026-04-01",
                },
            )
            store.set_docket_last_modified(did, "2026-04-01T12:00:00Z")
        store.upsert_court("vaed", "E.D. Va.", "vaed", "E.D. Virginia")
        cfg = {
            "calendars": {"cyber": {"name": "Cybercrime", "ics_path": "out/cyber.ics"}},
            "cases": [
                {
                    "id": "us-v-akhter",
                    "name": "US v. Akhter",
                    "calendar": "cyber",
                    "dockets": [71989485, 73333500, 73320754],
                },
            ],
        }
        models = build_calendar_models(cfg, store)
        case = models[0]["cases"][0]
        # ONE dockets_meta entry, not three.
        assert len(case["dockets"]) == 1
        d = case["dockets"][0]
        # First config docket id wins as the canonical (its absolute_url
        # rides into the rendered link).
        assert d["docket_id"] == 71989485
        assert d["docket_number"] == "1:25-cr-00307"
        assert d["court_citation"] == "E.D. Va."
        # The two siblings are listed under `sibling_docket_ids` (order
        # is config order).
        assert d.get("sibling_docket_ids") == [73333500, 73320754]

    def test_handles_unseen_docket(self, store):
        # Case configured but no sync has happened yet — every aggregate
        # is None and the docket metadata is empty. The renderer still
        # has to cope.
        cfg = {
            "calendars": {"cyber": {"name": "Cybercrime", "ics_path": "out/cyber.ics"}},
            "cases": [
                {
                    "id": "us-v-x",
                    "name": "US v. X",
                    "calendar": "cyber",
                    "dockets": [999],
                },
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
        write_index(
            target,
            calendars=[
                {
                    "id": "x",
                    "name": "X",
                    "links": {"webcal": None, "https": None, "relative": None},
                    "cases": [],
                }
            ],
        )
        assert target.exists()
        assert target.read_text(encoding="utf-8").startswith("<!doctype html>")


class TestRenderSubscribeEdgeCases:
    """``_render_subscribe`` accepts arbitrary ``links`` dicts, so a caller
    that hands it an https URL without a paired webcal URL hits the
    rare "button without a subscribe anchor" path. ``_ics_links`` never
    produces that combination today, but pinning the branch keeps a
    future refactor honest."""

    def test_https_without_webcal_renders_button_only(self):
        html = render_index(
            calendars=[
                {
                    "id": "c",
                    "name": "C",
                    "links": {
                        "webcal": None,
                        "https": "https://example.invalid/c.ics",
                        "relative": "c.ics",
                    },
                    "cases": [],
                }
            ]
        )
        # No Subscribe anchor (webcal absent), but the Copy-feed-URL
        # button still renders against the https URL.
        assert ">Subscribe</a>" not in html
        assert (
            'class="copy-feed" data-url="https://example.invalid/c.ics"' in html
        )


class TestRenderSummariesNoCourtCitation:
    """When a docket's metadata is missing court_citation, the per-docket
    summary label falls back to the bare docket number."""

    def test_multi_docket_summary_label_omits_citation(self):
        case = {
            "case_id": "us-v-x",
            "name": "X",
            "summaries": [
                {
                    "docket_number": "1:25-cr-1",
                    "court_id": "mad",
                    "summary": "First docket summary.",
                },
                {
                    "docket_number": "1:25-cr-2",
                    "court_id": "mad",
                    "summary": "Second docket summary.",
                },
            ],
        }
        dockets = [
            {
                "docket_number": "1:25-cr-1",
                "court_id": "mad",
                "court_citation": "D. Mass.",
            },
            {
                "docket_number": "1:25-cr-2",
                "court_id": "mad",
                # court_citation missing — the branch under test.
            },
        ]
        out = _render_summaries(case, dockets)
        # The first paragraph's label includes the citation; the second
        # is bare docket-number only.
        assert "1:25-cr-1 (D. Mass.)" in out
        assert ">1:25-cr-2</span>" in out
        # And we did NOT print "(None)" or similar for the missing citation.
        assert "1:25-cr-2 (" not in out


class TestDocketAbsoluteUrlAlreadyAbsolute:
    """The docket label promotion only prepends the CourtListener host
    when ``absolute_url`` is a relative path. When it's already a full
    URL (rare but possible for non-CourtListener sources), we link to it
    as-is."""

    def test_absolute_url_starting_with_http_is_passed_through(self):
        html = render_index(
            calendars=[
                {
                    "id": "c",
                    "name": "C",
                    "links": {"webcal": None, "https": None, "relative": "c.ics"},
                    "cases": [
                        {
                            "case_id": "us-v-x",
                            "name": "X",
                            "date_filed": None,
                            "last_filing_date": None,
                            "dockets": [
                                {
                                    "docket_id": 1,
                                    "docket_number": "1:25-cr-1",
                                    "court_id": "mad",
                                    "court_citation": "D. Mass.",
                                    "absolute_url": "https://other.example/d/1/",
                                }
                            ],
                        }
                    ],
                }
            ]
        )
        # Direct passthrough — no CourtListener host prepended.
        assert 'href="https://other.example/d/1/"' in html
        assert "https://www.courtlistener.com/https://other" not in html
