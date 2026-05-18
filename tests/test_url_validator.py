"""Tests for HTTP-validated URL repair.

We monkey-patch ``urllib.request.urlopen`` so no real network calls
happen, but the rest of the production pipeline (HEAD → GET fallback,
retry-on-transport-error, parent-path walk, fail-open semantics) runs
for real.
"""

from __future__ import annotations

import http.client
import io
import socket
import urllib.error
import urllib.request

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
    # retryable transport errors. Tests that raise a timeout or
    # protocol error would otherwise stall on the backoff window.
    monkeypatch.setattr(url_validator.time, "sleep", lambda _s: None)


class _FakeResp:
    """Stand-in for ``http.client.HTTPResponse`` (what urlopen returns)."""

    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(url: str, status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url,
        status,
        f"HTTP {status}",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b""),
    )


def _install_urlopen(monkeypatch, handler):
    """Patch ``urllib.request.urlopen`` (the symbol the ``url_validator``
    module resolves through) with a function that calls the test's
    handler with the incoming ``Request``.
    """

    def fake_urlopen(req, timeout=None):
        return handler(req)

    monkeypatch.setattr(url_validator.urllib.request, "urlopen", fake_urlopen)


class TestValidateURL:
    def test_ok_url_passes_through(self, monkeypatch):
        def handler(req):
            assert req.full_url == "https://example.com/foo/bar/"
            return _FakeResp(200)

        _install_urlopen(monkeypatch, handler)
        assert url_validator.validate_url("https://example.com/foo/bar/") == (
            "https://example.com/foo/bar/"
        )

    def test_truncates_one_path_component_on_404(self, monkeypatch):
        # /foo/bar/junk → 404; parent /foo/bar/ → 200. Should return parent.
        def handler(req):
            if req.full_url.endswith("/junk"):
                raise _http_error(req.full_url, 404)
            return _FakeResp(200)

        _install_urlopen(monkeypatch, handler)
        out = url_validator.validate_url("https://example.com/foo/bar/junk")
        assert out == "https://example.com/foo/bar/"

    def test_does_not_truncate_to_bare_domain(self, monkeypatch):
        # /typo only has one path segment. We don't fall back to the domain
        # root — the home page is a meaningless "valid" answer.
        def handler(req):
            raise _http_error(req.full_url, 404)

        _install_urlopen(monkeypatch, handler)
        assert url_validator.validate_url("https://example.com/typo") is None

    def test_both_paths_404_returns_none(self, monkeypatch):
        def handler(req):
            raise _http_error(req.full_url, 404)

        _install_urlopen(monkeypatch, handler)
        assert url_validator.validate_url("https://example.com/a/b/c") is None

    def test_falls_back_to_get_on_405(self, monkeypatch):
        # Some servers don't implement HEAD.
        calls = {"head": 0, "get": 0}

        def handler(req):
            if req.get_method() == "HEAD":
                calls["head"] += 1
                raise _http_error(req.full_url, 405)
            calls["get"] += 1
            return _FakeResp(200)

        _install_urlopen(monkeypatch, handler)
        out = url_validator.validate_url("https://example.com/foo/bar/")
        assert out == "https://example.com/foo/bar/"
        assert calls == {"head": 1, "get": 1}

    def test_network_error_fails_open(self, monkeypatch):
        # urllib.error.URLError without HTTPError shape — non-retryable
        # path returns None immediately, _walk_candidates sees no 4xx,
        # validate_url fails open.
        def handler(req):
            raise urllib.error.URLError("simulated network down")

        _install_urlopen(monkeypatch, handler)
        # Fail-open: return the input rather than dropping the field.
        assert url_validator.validate_url("https://example.com/foo/bar/") == (
            "https://example.com/foo/bar/"
        )

    def test_retries_transport_error_then_succeeds(self, monkeypatch):
        # End-to-end: `_check` calls `_request_with_retry`, which retries
        # on socket timeout / HTTPException / ConnectionError. First
        # attempt raises socket.timeout, second returns 200. Recovery
        # means no `dial_in` field gets blanked over a transient blip.
        attempts = [0]

        def handler(req):
            attempts[0] += 1
            if attempts[0] == 1:
                raise socket.timeout("slow host")
            return _FakeResp(200)

        _install_urlopen(monkeypatch, handler)
        assert url_validator.validate_url("https://example.com/foo/bar/") == (
            "https://example.com/foo/bar/"
        )
        # Two transport-level calls: the failed first attempt then the
        # retry that succeeded.
        assert attempts[0] == 2

    def test_non_http_returned_as_is(self):
        out = url_validator.validate_url("tel:+15551234567")
        assert out == "tel:+15551234567"
        out = url_validator.validate_url("")
        assert out is None

    def test_successful_results_cached(self, monkeypatch):
        calls = [0]

        def handler(req):
            calls[0] += 1
            return _FakeResp(200)

        _install_urlopen(monkeypatch, handler)
        url_validator.validate_url("https://example.com/foo/bar/")
        url_validator.validate_url("https://example.com/foo/bar/")
        # Second call hits the cache, so only one HTTP request was made.
        assert calls[0] == 1

    def test_failures_not_cached_so_transient_errors_retry(self, monkeypatch):
        # First validate_url call: parent + child both 404 → returns
        # None (definite 4xx). Second call: child is 200 → returns the URL.
        # Failures are NOT cached, so the second call hits the network.
        outcomes = iter([404, 404, 200])

        def handler(req):
            code = next(outcomes)
            if code >= 400:
                raise _http_error(req.full_url, code)
            return _FakeResp(code)

        _install_urlopen(monkeypatch, handler)
        first = url_validator.validate_url("https://example.com/a/b/c")
        second = url_validator.validate_url("https://example.com/a/b/c")
        assert first is None
        assert second == "https://example.com/a/b/c"

    def test_strips_query_and_fragment_from_truncated_parent(self, monkeypatch):
        # If the original URL has ?query, the parent fallback drops it —
        # the query was probably attached to the broken final segment.
        def handler(req):
            url = req.full_url
            if "junk" in url:
                raise _http_error(url, 404)
            assert "?" not in url
            return _FakeResp(200)

        _install_urlopen(monkeypatch, handler)
        out = url_validator.validate_url("https://example.com/foo/junk?id=1")
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


class TestValidateUrlEdgeCases:
    def test_unexpected_exception_returns_input(self, monkeypatch):
        # If something inside _walk_candidates raises a non-RequestError
        # exception (e.g. a programming error), validate_url logs and
        # returns the input unchanged (fail-open).
        def _boom(*a, **kw):
            raise ValueError("unexpected boom")

        monkeypatch.setattr(url_validator, "_walk_candidates", _boom)
        assert url_validator.validate_url("https://example.com/foo/") == (
            "https://example.com/foo/"
        )

    def test_malformed_url_returns_none_fast(self, monkeypatch):
        # ``urllib.request.Request`` rejects URLs that contain literal
        # newlines, tabs, etc. via a ValueError. The validator catches
        # that and treats the URL as a flake — fail-open returns the
        # input rather than dropping the field.
        attempts = [0]

        def handler(req):
            attempts[0] += 1
            # Force `Request(url)` to fail before urlopen is invoked.
            raise ValueError("invalid URL")

        # Patch Request itself so we exercise the ValueError-catch path.
        def boom_request(*args, **kwargs):
            raise ValueError("invalid URL")

        monkeypatch.setattr(url_validator.urllib.request, "Request", boom_request)

        out = url_validator.validate_url("https://example.com/only/")
        # Non-retryable: handled by the ValueError branch in
        # `_request_with_retry`, which returns None immediately. No
        # retries because there's nothing transient to retry.
        assert attempts[0] == 0
        # Fail-open: keep the input URL.
        assert out == "https://example.com/only/"


class TestCheckHttpCodes:
    def test_5xx_is_flake_not_404(self, monkeypatch):
        # A 5xx response is "couldn't tell", not "URL gone". The result
        # bubbles up as fail-open (return the URL unchanged) when every
        # candidate flakes.
        def handler(req):
            raise _http_error(req.full_url, 503)

        _install_urlopen(monkeypatch, handler)
        # No 4xx ever observed → fail-open: keep the input URL.
        assert url_validator.validate_url("https://example.com/a/b/c") == (
            "https://example.com/a/b/c"
        )

    def test_get_fallback_network_error_returns_flake(self, monkeypatch):
        # HEAD returns 405; the GET fallback then errors out — overall
        # outcome is "flake", and fail-open returns the input URL.
        def handler(req):
            if req.get_method() == "HEAD":
                raise _http_error(req.full_url, 405)
            raise urllib.error.URLError("GET fallback down")

        _install_urlopen(monkeypatch, handler)
        out = url_validator.validate_url("https://example.com/foo/bar/")
        assert out == "https://example.com/foo/bar/"

    def test_transport_budget_exhausted_returns_flake(self, monkeypatch):
        # When every attempt raises a retryable transport class,
        # ``_request_with_retry`` exhausts its budget and returns None.
        # _check reads that as "flake", _walk_candidates sees no 4xx,
        # validate_url returns the URL (fail-open).
        attempts = [0]

        def handler(req):
            attempts[0] += 1
            raise socket.timeout("perpetually slow")

        _install_urlopen(monkeypatch, handler)
        # Single-segment URL so handler is hit once per attempt within
        # the budget — easier to count.
        out = url_validator.validate_url("https://example.com/only/")
        # Budget exhausted on HEAD → _check returns "flake" → fail-open
        # returns the input.
        assert out == "https://example.com/only/"
        assert attempts[0] == url_validator._VALIDATE_RETRY_TOTAL

    def test_non_retryable_request_error_returns_flake_immediately(self, monkeypatch):
        # `_request_with_retry` retries narrow transport classes
        # (socket.timeout / HTTPException / ConnectionError) but the
        # wider `urllib.error.URLError` catch returns None immediately
        # for any non-retryable network-class error. The outcome should
        # still be "flake" → fail-open returns the input.
        attempts = [0]

        def handler(req):
            attempts[0] += 1
            # URLError without HTTPError shape and not in the retryable
            # set — exercises the fall-through path that returns None
            # on the first hit.
            raise urllib.error.URLError("unreachable host")

        _install_urlopen(monkeypatch, handler)
        # Single-segment URL so `_candidates` returns just one entry
        # and the handler is hit exactly once.
        out = url_validator.validate_url("https://example.com/only/")
        assert attempts[0] == 1
        # Fail-open: keep the input URL.
        assert out == "https://example.com/only/"
