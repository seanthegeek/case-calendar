"""Tests for the static index.html renderer."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from case_calendar.calendars.index import (
    _case_search_text,
    _cl_full_url,
    _esc,
    _format_date,
    _ics_links,
    _normalize_tags,
    _render_case,
    _render_cl_records,
    _render_summaries,
    _render_summary_body,
    _render_tags,
    _summary_plain_text,
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
        # The Akhter shape: three CourtListener docket_ids share one (docket_number,
        # court_id) — the index renders a SINGLE paragraph for the group,
        # NOT three labeled near-duplicates.
        html = _render_summaries(
            {
                "summaries": [
                    {
                        "docket_number": "1:25-cr-00307",
                        "court_id": "vaed",
                        "summary": "Pooled summary across all three CourtListener siblings.",
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
        # One summary → no docket label, even though three CourtListener siblings.
        assert "Pooled summary across all three CourtListener siblings." in html
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

    def test_inline_links_render_as_anchors(self):
        html = _render_summaries(
            {
                "summaries": [
                    {
                        "docket_number": "1:24-cr-1",
                        "court_id": "nysd",
                        "summary": "He was [charged](https://ia/ind.pdf) with fraud.",
                    }
                ]
            },
            dockets=[],
        )
        assert '<a href="https://ia/ind.pdf"' in html
        assert ">charged</a>" in html


class TestRenderSummaryBody:
    """Inline document links: ``[words](https://...)`` markers become anchors
    on the words themselves; everything else is escaped literal text."""

    def test_resolves_inline_link_to_anchor_on_the_words(self):
        out = _render_summary_body(
            "The defendants were [charged with wire fraud](https://ia/ind.pdf) in NY."
        )
        assert (
            '<a href="https://ia/ind.pdf" target="_blank" rel="noopener">'
            "charged with wire fraud</a>" in out
        )
        assert out.startswith("The defendants were ")
        assert out.endswith(" in NY.")

    def test_non_http_marker_renders_literally(self):
        # A leftover ``doc:`` token (shouldn't happen — the resolver strips
        # them) or any non-http(s) parenthetical is NOT a link; render the
        # bracket text verbatim rather than dropping it.
        out = _render_summary_body("a [not a link](doc:D1) here")
        assert "[not a link](doc:D1)" in out
        assert "<a " not in out

    def test_surrounding_markup_is_escaped(self):
        out = _render_summary_body("a <b>x</b> & y")
        assert "<b>" not in out
        assert "&lt;b&gt;x&lt;/b&gt;" in out
        assert "&amp;" in out

    def test_anchor_words_and_href_are_escaped(self):
        # Model output is untrusted: both the linked words and the href are
        # HTML-escaped so neither can break out of the tag.
        out = _render_summary_body('was [<i>sentenced</i>](https://x/"y) today')
        assert "<i>sentenced</i>" not in out
        assert "&lt;i&gt;sentenced&lt;/i&gt;" in out
        assert 'href="https://x/&quot;y"' in out


class TestSummaryPlainText:
    def test_strips_markers_to_their_words(self):
        assert (
            _summary_plain_text("He [pled guilty](https://ia/plea.pdf) today.")
            == "He pled guilty today."
        )

    def test_leaves_non_marker_text_unchanged(self):
        assert _summary_plain_text("No links here (2024).") == "No links here (2024)."


class TestCourtListenerRecords:
    """The "CourtListener records (same docket)" line for a logical docket
    split across multiple CourtListener docket_id records."""

    def test_full_url_promotes_path(self):
        assert (
            _cl_full_url("/docket/123/x/")
            == "https://www.courtlistener.com/docket/123/x/"
        )
        assert _cl_full_url("https://x/y") == "https://x/y"

    def test_single_record_renders_no_line(self):
        d = {"cl_records": [{"docket_id": 1, "absolute_url": "/docket/1/x/"}]}
        assert _render_cl_records(d, multi_docket=False) == ""
        # Missing cl_records is also a no-op.
        assert _render_cl_records({}, multi_docket=False) == ""

    def test_multiple_records_render_labeled_links(self):
        d = {
            "docket_number": "1:25-cr-00307",
            "cl_records": [
                {"docket_id": 71989485, "absolute_url": "/docket/71989485/a/"},
                {"docket_id": 73333500, "absolute_url": "/docket/73333500/a/"},
                {"docket_id": 73320754, "absolute_url": "/docket/73320754/a/"},
            ],
        }
        html = _render_cl_records(d, multi_docket=False)
        # The "(same docket)" label disambiguates without a tooltip (mobile).
        assert "CourtListener records (same docket):" in html
        # Three individually-clickable records, numbered, full-URL promoted.
        assert (
            '<a href="https://www.courtlistener.com/docket/71989485/a/" '
            'target="_blank" rel="noopener">1</a>' in html
        )
        assert ">2</a>" in html and ">3</a>" in html
        # Single-logical-docket case: no docket-number prefix on the label.
        assert "1:25-cr-00307 —" not in html

    def test_multi_docket_case_prefixes_label_with_number(self):
        d = {
            "docket_number": "1:25-cr-00307",
            "cl_records": [
                {"docket_id": 1, "absolute_url": "/docket/1/a/"},
                {"docket_id": 2, "absolute_url": "/docket/2/a/"},
            ],
        }
        html = _render_cl_records(d, multi_docket=True)
        # Prefixed so it's clear which logical docket the records belong to.
        assert "1:25-cr-00307 — CourtListener records (same docket):" in html

    def test_record_without_url_renders_unlinked_number(self):
        d = {
            "docket_number": "x",
            "cl_records": [
                {"docket_id": 1, "absolute_url": "/docket/1/a/"},
                {"docket_id": 2, "absolute_url": None},
            ],
        }
        html = _render_cl_records(d, multi_docket=False)
        assert ">1</a>" in html
        assert "<span>2</span>" in html  # no URL -> un-linked number

    def test_render_case_includes_records_line(self):
        # End-to-end through _render_case: the records line appears after the
        # dockets list for a split docket.
        html = _render_case(
            {
                "name": "US v. Akhter",
                "dockets": [
                    {
                        "docket_number": "1:25-cr-00307",
                        "court_citation": "E.D. Va.",
                        "absolute_url": "/docket/71989485/a/",
                        "docket_id": 71989485,
                        "cl_records": [
                            {
                                "docket_id": 71989485,
                                "absolute_url": "/docket/71989485/a/",
                            },
                            {
                                "docket_id": 73333500,
                                "absolute_url": "/docket/73333500/a/",
                            },
                        ],
                    }
                ],
                "date_filed": None,
                "last_filing_date": None,
            }
        )
        # The docket number is the primary link (shown once as a label, not
        # repeated as a separate docket)...
        assert ">1:25-cr-00307 (E.D. Va.)</a>" in html
        # ...the records line is rendered after the dockets list...
        assert 'class="cl-records"' in html
        assert "CourtListener records (same docket):" in html
        # ...and a single-logical-docket case carries no number prefix on it.
        assert "1:25-cr-00307 — CourtListener" not in html


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

    def test_summary_link_markup_stripped_from_haystack(self):
        # Subscribers search the words they read, not the embedded URLs: the
        # ``[words](url)`` markers collapse to their words in the haystack.
        text = _case_search_text(
            {
                "summaries": [
                    {"summary": "He [pled guilty](https://ia/plea.pdf) to fraud."}
                ]
            }
        )
        assert "pled guilty to fraud" in text
        assert "ia/plea.pdf" not in text
        assert "https" not in text

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


class TestRenderTags:
    def test_chip_carries_data_tag_for_click_handler(self):
        # The runtime JS reads `data-tag` to know what to append to the
        # search box; the visible label is the same string. Both must be
        # HTML-escaped (the operator picks the casing, but a hostile tag
        # value still needs to survive).
        html = _render_tags(["DPRK", "IT-worker"])
        assert html.startswith('<ul class="tags">')
        assert 'data-tag="DPRK"' in html
        assert ">DPRK</button>" in html
        assert 'data-tag="IT-worker"' in html

    def test_empty_list_renders_nothing(self):
        assert _render_tags([]) == ""

    def test_escapes_html_in_tag_text(self):
        # Tags are operator-supplied but flow through unsanitized YAML;
        # the renderer must HTML-escape both the visible text and the
        # data-tag attribute.
        html = _render_tags(['<script>"x"'])
        assert "&lt;script&gt;&quot;x&quot;" in html
        assert "<script>" not in html


class TestNormalizeTags:
    def test_strips_whitespace_and_dedupes_case_insensitively(self):
        # build_calendar_models reads tags off the raw cfg dict, bypassing
        # the CLI parser. _normalize_tags mirrors _tags_from_config's
        # strip+dedupe so the index sees the same shape the calendar
        # event descriptions do.
        assert _normalize_tags(["  DPRK  ", "dprk", "ransomware"]) == [
            "DPRK",
            "ransomware",
        ]

    def test_drops_non_string_and_empty_entries(self):
        # Defensive read — validation has happened earlier, but malformed
        # input shouldn't crash the renderer.
        assert _normalize_tags(["DPRK", "", "  ", 42, None]) == ["DPRK"]

    def test_non_list_returns_empty(self):
        for bad in (None, "DPRK", {"tag": "DPRK"}):
            assert _normalize_tags(bad) == []


class TestCaseSearchTextTags:
    def test_tags_join_the_search_haystack(self):
        # Clicking a tag chip appends the tag (verbatim, possibly quoted)
        # to the search bar; for the resulting AND-substring match to
        # hit the case, the tag must also appear in data-search.
        text = _case_search_text(
            {
                "name": "US v. X",
                "dockets": [],
                "summaries": [],
                "tags": ["DPRK", "ransomware"],
            }
        )
        assert "dprk" in text
        assert "ransomware" in text

    def test_falsy_tag_entries_are_skipped(self):
        # _normalize_tags strips falsy entries at the boundary, but the
        # search-haystack helper is also called with raw dicts in tests
        # and renders directly — assert the inner filter holds too.
        text = _case_search_text(
            {
                "name": "X",
                "dockets": [],
                "summaries": [],
                "tags": ["", None, "DPRK"],
            }
        )
        assert "dprk" in text


class TestRenderCaseTags:
    def test_tags_block_rendered_when_present(self):
        html = _render_case(
            {
                "name": "US v. X",
                "dockets": [],
                "summaries": [],
                "tags": ["DPRK", "IT-worker"],
                "date_filed": None,
                "last_filing_date": None,
            }
        )
        assert 'class="tags"' in html
        assert ">DPRK</button>" in html
        assert ">IT-worker</button>" in html

    def test_no_tags_block_when_absent(self):
        html = _render_case(
            {
                "name": "US v. X",
                "dockets": [],
                "summaries": [],
                "date_filed": None,
                "last_filing_date": None,
            }
        )
        assert 'class="tags"' not in html


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

    def test_sort_dropdown_default_is_last_filing_date(self, calendars):
        # The default sort option (selected on page load) is the filing key,
        # labeled "Last filing date" (the value stays "last-filing", backing
        # data-last-filing; the JS reads 'data-' + option.value).
        html = render_index(calendars=calendars)
        assert '<option value="last-filing" selected>Last filing date</option>' in html
        # The old "Last activity" wording is gone — that label was the bug
        # this change fixes.
        assert "Last activity" not in html

    def test_sort_dropdown_offers_next_event(self, calendars):
        # "Next event" sorts by data-next-event (the soonest upcoming event's
        # ISO start). The option value must match the attribute the renderer
        # emits, since the JS reads 'data-' + option.value.
        html = render_index(calendars=calendars)
        assert '<option value="next-event">Next event</option>' in html
        # The backing attribute is emitted on every case row — empty here,
        # since these hand-built cases carry no next_event (empty sorts last).
        assert 'data-next-event=""' in html

    def test_direction_labels_adapt_per_sort_key(self, calendars):
        # The Direction dropdown is rebuilt client-side per sort key: each key
        # carries its own ordered [value, label] pairs in DIR_OPTIONS, with the
        # FIRST entry being that key's default direction. Pin the metadata the
        # same way the other JS handlers are guarded so the per-key labels +
        # defaults can't silently regress.
        html = render_index(calendars=calendars)
        # Date keys: newest-first default; name: A–Z (ascending) default;
        # next-event: soonest-first (ascending) default.
        assert (
            "'last-filing': [['desc', 'Newest first'], ['asc', 'Oldest first']]" in html
        )
        assert (
            "'filed':       [['desc', 'Newest first'], ['asc', 'Oldest first']]" in html
        )
        assert (
            "'next-event':  [['asc', 'Soonest first'], ['desc', 'Latest first']]"
            in html
        )
        assert "'name':        [['asc', 'A–Z'], ['desc', 'Z–A']]" in html
        # Picking a sort key resets the direction to that key's default.
        assert "populateDir(section, this.value);" in html
        # The old generic Descending/Ascending wording is gone, and the
        # next-event direction-inversion hack was replaced by the per-key
        # default (asc) so the comparator stays a plain string compare.
        assert ">Descending<" not in html
        assert "asc = !asc" not in html

    def test_direction_no_js_fallback_matches_default_key(self, calendars):
        # The server-rendered Direction options are the no-JS fallback and must
        # match the default sort key ("Last filing" → Newest/Oldest first); JS
        # repopulates them on load.
        html = render_index(calendars=calendars)
        assert '<option value="desc" selected>Newest first</option>' in html
        assert '<option value="asc">Oldest first</option>' in html

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

    def test_propagates_normalized_tags_onto_case_row(self, store):
        store.upsert_docket_meta(
            100,
            {
                "court_id": "nysd",
                "docket_number": "1:24-cr-1",
                "case_name": "US v. X",
                "absolute_url": "/docket/100/x/",
                "date_last_filing": "2026-05-10",
            },
        )
        store.set_docket_last_modified(100, "2026-05-10T12:00:00Z")
        store.upsert_court("nysd", "S.D.N.Y.", "nysd", "Southern District of NY")
        cfg = {
            "calendars": {"cyber": {"name": "Cybercrime", "ics_path": "out/cyber.ics"}},
            "cases": [
                {
                    "id": "us-v-x",
                    "name": "US v. X",
                    "calendar": "cyber",
                    "dockets": [100],
                    # Whitespace + casing-duplicate to confirm the normalize
                    # boundary is wired up.
                    "tags": ["  DPRK  ", "dprk", "ransomware"],
                },
            ],
        }
        models = build_calendar_models(cfg, store)
        case = models[0]["cases"][0]
        assert case["tags"] == ["DPRK", "ransomware"]

    def test_collapses_sibling_docket_ids_into_one_dockets_meta_entry(self, store):
        # When `case.dockets` lists multiple CourtListener docket_ids that share
        # (docket_number, court_id) — the Akhter-shape PR #3 case where
        # one logical PACER docket lives under three CourtListener docket_ids —
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
        # And every record (primary first) is carried in `cl_records` with its
        # own absolute_url, so the renderer can link each one.
        assert [r["docket_id"] for r in d["cl_records"]] == [
            71989485,
            73333500,
            73320754,
        ]
        assert d["cl_records"][0]["absolute_url"] == "/docket/71989485/akhter/"
        assert d["cl_records"][2]["absolute_url"] == "/docket/73320754/akhter/"

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
        assert 'class="copy-feed" data-url="https://example.invalid/c.ics"' in html


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
