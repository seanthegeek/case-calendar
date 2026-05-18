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


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # `_request_with_retry` calls `time.sleep` between attempts on
    # retryable transport errors. Tests that raise ConnectError or
    # ReadTimeout would otherwise stall on the backoff window.
    monkeypatch.setattr(url_validator.time, "sleep", lambda _s: None)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


class TestValidateURL:
    def test_ok_url_passes_through(self):
        def handler(req):
            assert str(req.url) == "https://example.com/foo/bar/"
            return httpx.Response(200)

        out = url_validator.validate_url(
            "https://example.com/foo/bar/",
            client=_client(handler),
        )
        assert out == "https://example.com/foo/bar/"

    def test_truncates_one_path_component_on_404(self):
        # /foo/bar/junk → 404; parent /foo/bar/ → 200. Should return parent.
        def handler(req):
            if str(req.url).endswith("/junk"):
                return httpx.Response(404)
            return httpx.Response(200)

        out = url_validator.validate_url(
            "https://example.com/foo/bar/junk",
            client=_client(handler),
        )
        assert out == "https://example.com/foo/bar/"

    def test_does_not_truncate_to_bare_domain(self):
        # /typo only has one path segment. We don't fall back to the domain
        # root — the home page is a meaningless "valid" answer.
        def handler(req):
            return httpx.Response(404)

        out = url_validator.validate_url(
            "https://example.com/typo",
            client=_client(handler),
        )
        assert out is None

    def test_both_paths_404_returns_none(self):
        def handler(req):
            return httpx.Response(404)

        out = url_validator.validate_url(
            "https://example.com/a/b/c",
            client=_client(handler),
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
            "https://example.com/foo/bar/",
            client=_client(handler),
        )
        assert out == "https://example.com/foo/bar/"
        assert calls == {"head": 1, "get": 1}

    def test_network_error_fails_open(self):
        def handler(req):
            raise httpx.ConnectError("simulated network down")

        out = url_validator.validate_url(
            "https://example.com/foo/bar/",
            client=_client(handler),
        )
        # Fail-open: return the input rather than dropping the field.
        assert out == "https://example.com/foo/bar/"

    def test_retries_transport_error_then_succeeds(self, monkeypatch):
        # End-to-end: validate_url constructs its own httpx.Client and
        # `_check` calls `_request_with_retry`, which retries on
        # ReadTimeout / ConnectError / RemoteProtocolError. Intercept
        # the Client constructor to swap in our MockTransport; first
        # attempt raises ReadTimeout, second returns 200. Recovery
        # means no `dial_in` field gets blanked over a transient blip.
        monkeypatch.setattr(url_validator.time, "sleep", lambda _s: None)

        attempts = [0]

        def handler(req):
            attempts[0] += 1
            if attempts[0] == 1:
                raise httpx.ReadTimeout("slow host", request=req)
            return httpx.Response(200)

        real_client = httpx.Client

        def patched_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "Client", patched_client)

        out = url_validator.validate_url("https://example.com/foo/bar/")
        assert out == "https://example.com/foo/bar/"
        # Two transport-level calls: the failed first attempt then the
        # retry that succeeded.
        assert attempts[0] == 2

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
            "https://example.com/foo/bar/",
            client=_client(handler),
        )
        url_validator.validate_url(
            "https://example.com/foo/bar/",
            client=_client(handler),
        )
        # Second call hits the cache, so only one HTTP request was made.
        assert calls[0] == 1

    def test_failures_not_cached_so_transient_errors_retry(self):
        outcomes = iter(
            [404, 404, 200, 200]
        )  # first call: bad URL + bad parent; second: ok + ok

        def handler(req):
            return httpx.Response(next(outcomes))

        first = url_validator.validate_url(
            "https://example.com/a/b/c",
            client=_client(handler),
        )
        second = url_validator.validate_url(
            "https://example.com/a/b/c",
            client=_client(handler),
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
            "https://example.com/foo/junk?id=1",
            client=_client(handler),
        )
        assert out == "https://example.com/foo/"


class TestSyncIntegration:
    """Covers _validate_action_dial_in's behavior on action dicts."""

    def test_repair_updates_dial_in(self, monkeypatch):
        from case_calendar import sync

        monkeypatch.setattr(
            url_validator,
            "validate_url",
            lambda u: "https://example.com/repaired/",
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


class TestImplicitClient:
    """Covers the own_client branch in validate_url where the caller
    doesn't pass an httpx.Client and we build/close one ourselves."""

    def test_owns_client_when_not_passed(self, monkeypatch):
        # Use httpx's MockTransport-aware Client by patching httpx.Client
        # to return a transport-mock-backed instance.
        from unittest.mock import MagicMock

        closed = MagicMock()

        class _MockClient:
            def __init__(self, *a, **k):
                pass

            def head(self, url):
                r = MagicMock()
                r.status_code = 200
                return r

            def close(self):
                closed()

        monkeypatch.setattr(httpx, "Client", _MockClient)
        out = url_validator.validate_url("https://example.com/foo/")
        assert out == "https://example.com/foo/"
        closed.assert_called_once()

    def test_unexpected_exception_returns_input(self, monkeypatch):
        # If something inside _walk_candidates raises a non-RequestError
        # exception (e.g. a programming error), validate_url logs and
        # returns the input unchanged (fail-open).
        def _boom(*a, **kw):
            raise ValueError("unexpected boom")

        monkeypatch.setattr(url_validator, "_walk_candidates", _boom)
        out = url_validator.validate_url("https://example.com/foo/")
        assert out == "https://example.com/foo/"


class TestCheckHttpCodes:
    def test_5xx_is_flake_not_404(self):
        # A 5xx response is "couldn't tell", not "URL gone". The result
        # bubbles up as fail-open (return the URL unchanged) when every
        # candidate flakes.
        def handler(req):
            return httpx.Response(503)

        out = url_validator.validate_url(
            "https://example.com/a/b/c",
            client=_client(handler),
        )
        # No 4xx ever observed → fail-open: keep the input URL.
        assert out == "https://example.com/a/b/c"

    def test_get_fallback_network_error_returns_flake(self):
        # HEAD returns 405; the GET fallback then errors out — overall
        # outcome is "flake", and fail-open returns the input URL.
        def handler(req):
            if req.method == "HEAD":
                return httpx.Response(405)
            raise httpx.ConnectError("GET fallback down")

        out = url_validator.validate_url(
            "https://example.com/foo/bar/",
            client=_client(handler),
        )
        assert out == "https://example.com/foo/bar/"

    def test_non_retryable_request_error_returns_flake_immediately(self):
        # `_request_with_retry` retries narrow transport classes
        # (TimeoutException / NetworkError / RemoteProtocolError) but
        # catches the wider `httpx.RequestError` parent and returns None
        # immediately for anything outside that set — e.g. a malformed
        # response that fails decoding. The outcome should still be
        # "flake" → fail-open returns the input.
        attempts = [0]

        def handler(req):
            attempts[0] += 1
            # Not a TimeoutException / NetworkError / RemoteProtocolError,
            # but still a RequestError subclass — exercises the wider
            # `except httpx.RequestError` fall-through path.
            raise httpx.LocalProtocolError("malformed request")

        # Single-segment URL so `_candidates` returns just one entry
        # and the handler is hit exactly once per attempt (no parent
        # fallback to muddy the retry count).
        out = url_validator.validate_url(
            "https://example.com/only/",
            client=_client(handler),
        )
        # Single call: non-retryable RequestError returns None on first
        # hit, no retries; the narrow retryable set was deliberately
        # bypassed.
        assert attempts[0] == 1
        # Fail-open: keep the input URL.
        assert out == "https://example.com/only/"
