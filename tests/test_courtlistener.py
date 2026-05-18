"""Tests for the CourtListener REST client.

Monkey-patches ``urllib.request.urlopen`` so no network calls happen.
Every other layer of the production pipeline (URL building, auth header
threading, JSON decoding, the 429 / 5xx / transport retry loop, the
``_no_request_before`` cooldown) runs for real.
"""

from __future__ import annotations

import http.client
import io
import json
import socket
import urllib.error
import urllib.request

import pytest

import case_calendar.courtlistener as clmod
from case_calendar.courtlistener import CourtListener, HTTPStatusError


class _FakeResponse:
    """Stand-in for the ``http.client.HTTPResponse`` urlopen returns.

    Implements the context-manager protocol + ``status``, ``headers``,
    and ``read()`` — the only attributes ``_get`` reads. ``headers`` is
    a real ``http.client.HTTPMessage`` so case-insensitive lookups and
    ``.items()`` iteration behave the same as in production.
    """

    def __init__(self, status: int, body: bytes = b"", headers: dict | None = None):
        self.status = status
        self._body = body
        msg = http.client.HTTPMessage()
        for k, v in (headers or {}).items():
            msg[k] = v
        self.headers = msg

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _resp(status: int, json_body: dict | None = None, *, headers: dict | None = None) -> _FakeResponse:
    body = json.dumps(json_body or {}).encode()
    return _FakeResponse(status, body, headers)


def _http_error(req_url: str, status: int, json_body: dict | None = None, *, headers: dict | None = None) -> urllib.error.HTTPError:
    """Build the urllib.error.HTTPError urlopen raises on 4xx/5xx responses."""
    body = json.dumps(json_body or {}).encode()
    msg = http.client.HTTPMessage()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError(
        req_url,
        status,
        f"HTTP {status}",
        hdrs=msg,  # type: ignore[arg-type]
        fp=io.BytesIO(body),
    )


@pytest.fixture
def make_client(monkeypatch):
    """Build a CourtListener client with urlopen monkey-patched to invoke
    a per-test handler that receives the ``urllib.request.Request`` and
    returns either a ``_FakeResponse`` or raises an exception.
    """

    def _make(handler):
        cl = CourtListener(token="test")

        def fake_urlopen(req, timeout=None):
            return handler(req)

        monkeypatch.setattr(clmod.urllib.request, "urlopen", fake_urlopen)
        return cl

    return _make


class TestSimpleGets:
    def test_get_docket(self, make_client):
        def handler(req):
            assert "/api/rest/v4/dockets/42/" in req.full_url
            return _resp(200, {"id": 42, "case_name": "X"})

        cl = make_client(handler)
        assert cl.get_docket(42)["id"] == 42

    def test_auth_header_sent(self, make_client):
        seen = []

        def handler(req):
            seen.append(req.get_header("Authorization"))
            return _resp(200, {})

        cl = make_client(handler)
        cl.get_docket(1)
        assert seen == ["Token test"]

    def test_get_court(self, make_client):
        def handler(req):
            assert "/api/rest/v4/courts/mad/" in req.full_url
            return _resp(200, {"id": "mad", "citation_string": "D. Mass."})

        cl = make_client(handler)
        assert cl.get_court("mad")["citation_string"] == "D. Mass."

    def test_get_recap_document(self, make_client):
        def handler(req):
            assert "/api/rest/v4/recap-documents/99/" in req.full_url
            return _resp(200, {"id": 99, "plain_text": "hi"})

        cl = make_client(handler)
        assert cl.get_recap_document(99)["plain_text"] == "hi"


class TestRetryLogic:
    def test_429_then_success(self, make_client, monkeypatch):
        slept = []
        monkeypatch.setattr(clmod.time, "sleep", lambda s: slept.append(s))

        calls = [0]

        def handler(req):
            calls[0] += 1
            if calls[0] == 1:
                raise _http_error(req.full_url, 429, {}, headers={"Retry-After": "3"})
            return _resp(200, {"ok": True})

        cl = make_client(handler)
        assert cl._get("https://x/y").json() == {"ok": True}
        # Retry-After=3 + _RETRY_AFTER_BUFFER_SECONDS=5 → first nap is 8s.
        # The barrier is then re-checked at the top of the next iteration
        # and waits again (a fraction less than the first wait, since the
        # monkeypatched sleep doesn't advance the monotonic clock). Both
        # naps are recorded; we just care that the first one cleared the
        # buffered Retry-After.
        assert calls[0] == 2
        assert len(slept) >= 1
        assert slept[0] == 3.0 + clmod._RETRY_AFTER_BUFFER_SECONDS

    def test_500_retries(self, make_client, monkeypatch):
        slept = []
        monkeypatch.setattr(clmod.time, "sleep", lambda s: slept.append(s))

        calls = [0]

        def handler(req):
            calls[0] += 1
            if calls[0] < 3:
                raise _http_error(req.full_url, 503, {})
            return _resp(200, {"ok": True})

        cl = make_client(handler)
        assert cl._get("https://x/y").json() == {"ok": True}
        assert calls[0] == 3
        assert len(slept) == 2  # one nap before each retry

    def test_4xx_other_than_429_raises(self, make_client):
        def handler(req):
            raise _http_error(req.full_url, 404, {"error": "no"})

        cl = make_client(handler)
        with pytest.raises(HTTPStatusError) as exc_info:
            cl._get("https://x/y")
        assert exc_info.value.status_code == 404

    def test_429_with_long_retry_after_sleeps_through(self, monkeypatch, make_client):
        # CourtListener's daily 300/day bucket can return Retry-After of nearly 24h.
        # We honor it rather than aborting — manual restart-per-cycle was
        # painful enough that we'd rather sleep and resume automatically.
        slept = []
        monkeypatch.setattr(clmod.time, "sleep", lambda s: slept.append(s))

        calls = [0]

        def handler(req):
            calls[0] += 1
            if calls[0] == 1:
                raise _http_error(req.full_url, 429, {}, headers={"Retry-After": "85774"})
            return _resp(200, {"ok": True})

        cl = make_client(handler)
        assert cl._get("https://x/y").json() == {"ok": True}
        assert calls[0] == 2
        # No cap on Retry-After — the full ~24h value plus the buffer must
        # be slept through, not clamped, or a daily-bucket hit would abort
        # the sync instead of resuming after the reset.
        assert slept[0] == 85774.0 + clmod._RETRY_AFTER_BUFFER_SECONDS

    def test_giveup_after_max_attempts(self, make_client, monkeypatch):
        monkeypatch.setattr(clmod.time, "sleep", lambda s: None)
        calls = [0]

        def handler(req):
            calls[0] += 1
            raise _http_error(req.full_url, 429, {}, headers={"Retry-After": "1"})

        cl = make_client(handler)
        with pytest.raises(HTTPStatusError) as exc_info:
            cl._get("https://x/y")
        assert exc_info.value.status_code == 429
        # Implementation does up to 6 attempts then re-raises.
        assert calls[0] >= 6


class TestPagination:
    def test_iter_entries_follows_next(self, make_client):
        page1 = {
            "results": [
                {"id": 1, "date_modified": "2026-05-01T00:00:00Z"},
                {"id": 2, "date_modified": "2026-04-01T00:00:00Z"},
            ],
            "next": "https://www.courtlistener.com/api/rest/v4/docket-entries/?page=2",
        }
        page2 = {
            "results": [{"id": 3, "date_modified": "2026-03-01T00:00:00Z"}],
            "next": None,
        }

        responses = iter([page1, page2])

        def handler(req):
            return _resp(200, next(responses))

        cl = make_client(handler)
        ids = [e["id"] for e in cl.iter_entries(42)]
        # Yielded oldest-first: #3 (2026-03) → #2 (2026-04) → #1 (2026-05).
        assert ids == [3, 2, 1]

    def test_iter_entries_stops_when_below_modified_after(self, make_client):
        # API returns newest-first; we still want the early-stop optimization
        # so we don't page past the cutoff. Within the entries we DO yield,
        # order is oldest-first.
        pages = iter(
            [
                {
                    "results": [
                        {"id": 1, "date_modified": "2026-05-01T00:00:00Z"},
                        {"id": 2, "date_modified": "2026-04-15T00:00:00Z"},
                    ],
                    "next": "https://x/y?page=2",
                },
                {
                    "results": [
                        {"id": 3, "date_modified": "2026-04-05T00:00:00Z"},
                        {"id": 4, "date_modified": "2026-01-01T00:00:00Z"},  # below
                        {"id": 5, "date_modified": "2025-12-01T00:00:00Z"},  # below
                    ],
                    "next": "https://x/y?page=3",
                },
            ]
        )
        page_count = [0]

        def handler(req):
            page_count[0] += 1
            return _resp(200, next(pages))

        cl = make_client(handler)
        ids = [
            e["id"] for e in cl.iter_entries(42, modified_after="2026-04-01T00:00:00Z")
        ]
        # #1, #2, #3 are above cutoff; #4 stops paging. Yielded oldest-first.
        assert ids == [3, 2, 1]
        # Should have stopped after page 2 — never fetched page 3.
        assert page_count[0] == 2


class TestInit:
    def test_requires_token(self, monkeypatch):
        monkeypatch.delenv("COURTLISTENER_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="COURTLISTENER_TOKEN"):
            CourtListener()

    def test_context_manager(self):
        # `close()` is a no-op now (no persistent client state), but the
        # context-manager idiom is preserved so call sites
        # (cmd_sync, cmd_serve) don't need to change.
        with CourtListener(token="x") as cl:
            assert cl.token == "x"


class TestTransportErrorRetry:
    """`_get` retries transient transport errors (socket timeout,
    connection refused, protocol errors) in the same loop that handles
    429 / 5xx. Before in-house retry, a single read timeout mid-sync —
    the CourtListener server going quiet for a few seconds — propagated
    all the way up through `iter_entries` and killed the whole run.
    These tests monkey-patch ``urllib.request.urlopen`` to raise the
    same stdlib transport exceptions production would see.
    """

    @pytest.fixture(autouse=True)
    def _no_real_sleep(self, monkeypatch):
        # _get's transport-error path calls time.sleep between attempts;
        # skip it so tests run at memory speed.
        monkeypatch.setattr(clmod.time, "sleep", lambda _s: None)

    def test_retries_read_timeout_then_succeeds(self, make_client):
        attempts = [0]

        def handler(req):
            attempts[0] += 1
            if attempts[0] == 1:
                raise socket.timeout("timed out")
            return _resp(200, {"id": 42})

        cl = make_client(handler)
        assert cl.get_docket(42)["id"] == 42
        assert attempts[0] == 2

    def test_retries_connection_refused_then_succeeds(self, make_client):
        attempts = [0]

        def handler(req):
            attempts[0] += 1
            if attempts[0] < 3:
                raise urllib.error.URLError("Connection refused")
            return _resp(200, {"id": 7})

        cl = make_client(handler)
        assert cl.get_docket(7)["id"] == 7
        assert attempts[0] == 3

    def test_retries_remote_disconnected(self, make_client):
        # http.client.RemoteDisconnected fires when the server closes a
        # keep-alive connection mid-request; we want the same retry
        # behavior as a socket timeout.
        attempts = [0]

        def handler(req):
            attempts[0] += 1
            if attempts[0] == 1:
                raise http.client.RemoteDisconnected("server hung up")
            return _resp(200, {"id": 11})

        cl = make_client(handler)
        assert cl.get_docket(11)["id"] == 11
        assert attempts[0] == 2

    def test_exhausted_retries_propagate_last_transport_error(self, make_client):
        # When every attempt fails, the last TransportError surfaces.
        # `sync_case`'s linear-control-flow shape means this propagation
        # does NOT advance the docket's date_last_modified cutoff, so
        # the next sync re-walks the docket — exactly what we want.
        def handler(req):
            raise socket.timeout("perpetual timeout")

        cl = make_client(handler)
        with pytest.raises(socket.timeout):
            cl.get_docket(42)

    def test_429_response_reaches_get_logging_and_cooldown(self, monkeypatch, caplog, make_client):
        """Regression: 429 responses must reach ``_get``'s logging and
        cooldown machinery instead of being silently slept-through by a
        lower layer. Three signals fire on every 429:
          1. ``_get``'s "courtlistener 429" warning (URL / body /
             rate-limit headers — operator visibility into which bucket
             fired).
          2. The cross-request cooldown barrier ``_no_request_before`` is
             advanced.
          3. The first sleep equals ``Retry-After + _RETRY_AFTER_BUFFER_SECONDS``.
        """
        slept: list[float] = []
        # Override the autouse no-op sleep with one that records.
        monkeypatch.setattr(clmod.time, "sleep", lambda s: slept.append(s))

        calls = [0]

        def handler(req):
            calls[0] += 1
            if calls[0] == 1:
                raise _http_error(
                    req.full_url,
                    429,
                    {"detail": "Throttled"},
                    headers={"Retry-After": "7", "X-RateLimit-Remaining": "0"},
                )
            return _resp(200, {"id": 42})

        cl = make_client(handler)

        with caplog.at_level("WARNING", logger="case_calendar.courtlistener"):
            assert cl.get_docket(42)["id"] == 42

        warning_messages = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any("courtlistener 429" in m for m in warning_messages), warning_messages
        assert any("Retry-After" in m for m in warning_messages), warning_messages
        assert cl._no_request_before > 0.0
        assert slept, "expected at least one sleep call from _get"
        assert slept[0] == 7.0 + clmod._RETRY_AFTER_BUFFER_SECONDS

    def test_transport_retry_budget_caps_attempts(self, make_client):
        # `_TRANSPORT_RETRY_BUDGET` is a separate counter from the
        # response-status retry loop, so a long stretch of transport
        # errors gives up after `_TRANSPORT_RETRY_BUDGET + 1` attempts
        # instead of consuming the 429-handling budget.
        attempts = [0]

        def handler(req):
            attempts[0] += 1
            raise socket.timeout("timed out")

        cl = make_client(handler)
        with pytest.raises(socket.timeout):
            cl.get_docket(42)
        # The first attempt counts as 1; subsequent attempts up to the
        # budget cap then a final raise. Exact count: budget + 1 (the
        # attempt that exhausts the budget is the one that re-raises).
        assert attempts[0] == clmod._TRANSPORT_RETRY_BUDGET + 1
