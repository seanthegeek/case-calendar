"""Tests for HTTP-validated URL repair.

We use httpx.MockTransport so no real network calls happen.
"""

from __future__ import annotations

import httpx
import pytest

from case_calendar import url_validator


@pytest.fixture(autouse=True)
def _clear_cache():
    url_validator.clear_cache()
    yield
    url_validator.clear_cache()


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


class TestValidateURL:
    def test_ok_url_passes_through(self):
        def handler(req):
            assert str(req.url) == "https://example.com/foo/bar/"
            return httpx.Response(200)

        out = url_validator.validate_url(
            "https://example.com/foo/bar/", client=_client(handler),
        )
        assert out == "https://example.com/foo/bar/"

    def test_truncates_one_path_component_on_404(self):
        # /foo/bar/junk → 404; parent /foo/bar/ → 200. Should return parent.
        def handler(req):
            if str(req.url).endswith("/junk"):
                return httpx.Response(404)
            return httpx.Response(200)

        out = url_validator.validate_url(
            "https://example.com/foo/bar/junk", client=_client(handler),
        )
        assert out == "https://example.com/foo/bar/"

    def test_does_not_truncate_to_bare_domain(self):
        # /typo only has one path segment. We don't fall back to the domain
        # root — the home page is a meaningless "valid" answer.
        def handler(req):
            return httpx.Response(404)

        out = url_validator.validate_url(
            "https://example.com/typo", client=_client(handler),
        )
        assert out is None

    def test_both_paths_404_returns_none(self):
        def handler(req):
            return httpx.Response(404)

        out = url_validator.validate_url(
            "https://example.com/a/b/c", client=_client(handler),
        )
        assert out is None

    def test_falls_back_to_get_on_405(self):
        # Some servers don't implement HEAD.
        calls = {"head": 0, "get": 0}

        def handler(req):
            if req.method == "HEAD":
                calls["head"] += 1
                return httpx.Response(405)
            calls["get"] += 1
            return httpx.Response(200)

        out = url_validator.validate_url(
            "https://example.com/foo/bar/", client=_client(handler),
        )
        assert out == "https://example.com/foo/bar/"
        assert calls == {"head": 1, "get": 1}

    def test_network_error_fails_open(self):
        def handler(req):
            raise httpx.ConnectError("simulated network down")

        out = url_validator.validate_url(
            "https://example.com/foo/bar/", client=_client(handler),
        )
        # Fail-open: return the input rather than dropping the field.
        assert out == "https://example.com/foo/bar/"

    def test_non_http_returned_as_is(self):
        out = url_validator.validate_url("tel:+15551234567")
        assert out == "tel:+15551234567"
        out = url_validator.validate_url("")
        assert out is None

    def test_successful_results_cached(self):
        calls = [0]

        def handler(req):
            calls[0] += 1
            return httpx.Response(200)

        url_validator.validate_url(
            "https://example.com/foo/bar/", client=_client(handler),
        )
        url_validator.validate_url(
            "https://example.com/foo/bar/", client=_client(handler),
        )
        # Second call hits the cache, so only one HTTP request was made.
        assert calls[0] == 1

    def test_failures_not_cached_so_transient_errors_retry(self):
        outcomes = iter([404, 404, 200, 200])  # first call: bad URL + bad parent; second: ok + ok

        def handler(req):
            return httpx.Response(next(outcomes))

        first = url_validator.validate_url(
            "https://example.com/a/b/c", client=_client(handler),
        )
        second = url_validator.validate_url(
            "https://example.com/a/b/c", client=_client(handler),
        )
        assert first is None
        assert second == "https://example.com/a/b/c"

    def test_strips_query_and_fragment_from_truncated_parent(self):
        # If the original URL has ?query, the parent fallback drops it —
        # the query was probably attached to the broken final segment.
        def handler(req):
            url = str(req.url)
            if "junk" in url:
                return httpx.Response(404)
            assert "?" not in url
            return httpx.Response(200)

        out = url_validator.validate_url(
            "https://example.com/foo/junk?id=1", client=_client(handler),
        )
        assert out == "https://example.com/foo/"


class TestSyncIntegration:
    """Covers _validate_action_dial_in's behavior on action dicts."""

    def test_repair_updates_dial_in(self, monkeypatch):
        from case_calendar import sync

        monkeypatch.setattr(
            url_validator, "validate_url", lambda u: "https://example.com/repaired/",
        )
        action = {"dial_in": "https://example.com/broken", "notes": "n"}
        sync._validate_action_dial_in(action)
        assert action["dial_in"] == "https://example.com/repaired/"
        assert action["notes"] == "n"  # unchanged

    def test_failed_validation_moves_url_to_notes(self, monkeypatch):
        from case_calendar import sync

        monkeypatch.setattr(url_validator, "validate_url", lambda u: None)
        action = {"dial_in": "https://example.com/broken", "notes": "Existing notes."}
        sync._validate_action_dial_in(action)
        assert action["dial_in"] is None
        assert "Existing notes." in action["notes"]
        assert "Dial-in (unverified): https://example.com/broken" in action["notes"]

    def test_failed_validation_with_no_existing_notes(self, monkeypatch):
        from case_calendar import sync

        monkeypatch.setattr(url_validator, "validate_url", lambda u: None)
        action = {"dial_in": "https://example.com/x"}
        sync._validate_action_dial_in(action)
        assert action["dial_in"] is None
        assert action["notes"] == "Dial-in (unverified): https://example.com/x"

    def test_no_dial_in_is_a_noop(self):
        from case_calendar import sync

        action = {"notes": "n"}
        sync._validate_action_dial_in(action)
        assert action == {"notes": "n"}
