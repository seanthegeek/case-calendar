"""Tests for the CourtListener REST client.

Uses ``httpx.MockTransport`` so no network calls happen, but the rest of the
httpx pipeline (auth header, JSON decoding, retry-on-429) runs for real.
"""

from __future__ import annotations

import httpx
import pytest

import case_calendar.courtlistener as clmod
from case_calendar.courtlistener import CourtListener


@pytest.fixture
def make_client():
    """Return a function that builds a CourtListener client with a programmable transport."""

    def _make(handler):
        transport = httpx.MockTransport(handler)
        cl = CourtListener.__new__(CourtListener)
        cl.client = httpx.Client(
            transport=transport,
            headers={"Authorization": "Token test"},
        )
        # Mirror the attribute __init__ sets — _wait_for_window reads it on
        # every request, so without an explicit reset the bypass fails on
        # the first call.
        cl._no_request_before = 0.0
        return cl

    return _make


class TestSimpleGets:
    def test_get_docket(self, make_client):
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.path == "/api/rest/v4/dockets/42/"
            return httpx.Response(200, json={"id": 42, "case_name": "X"})

        cl = make_client(handler)
        assert cl.get_docket(42)["id"] == 42

    def test_auth_header_sent(self, make_client):
        seen = []
        def handler(req):
            seen.append(req.headers.get("Authorization"))
            return httpx.Response(200, json={})
        cl = make_client(handler)
        cl.get_docket(1)
        assert seen == ["Token test"]

    def test_get_court(self, make_client):
        def handler(req):
            assert req.url.path == "/api/rest/v4/courts/mad/"
            return httpx.Response(200, json={"id": "mad", "citation_string": "D. Mass."})
        cl = make_client(handler)
        assert cl.get_court("mad")["citation_string"] == "D. Mass."

    def test_get_recap_document(self, make_client):
        def handler(req):
            assert req.url.path == "/api/rest/v4/recap-documents/99/"
            return httpx.Response(200, json={"id": 99, "plain_text": "hi"})
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
                return httpx.Response(429, headers={"Retry-After": "3"}, json={})
            return httpx.Response(200, json={"ok": True})

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
                return httpx.Response(503, json={})
            return httpx.Response(200, json={"ok": True})

        cl = make_client(handler)
        assert cl._get("https://x/y").json() == {"ok": True}
        assert calls[0] == 3
        assert len(slept) == 2  # one nap before each retry

    def test_4xx_other_than_429_raises(self, make_client):
        def handler(req):
            return httpx.Response(404, json={"error": "no"})

        cl = make_client(handler)
        with pytest.raises(httpx.HTTPStatusError):
            cl._get("https://x/y")

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
                return httpx.Response(429, headers={"Retry-After": "85774"}, json={})
            return httpx.Response(200, json={"ok": True})

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
            return httpx.Response(429, headers={"Retry-After": "1"}, json={})

        cl = make_client(handler)
        with pytest.raises(httpx.HTTPStatusError):
            cl._get("https://x/y")
        # Implementation does up to 6 attempts then re-raises.
        assert calls[0] >= 6


class TestPagination:
    def test_iter_entries_follows_next(self, make_client):
        page1 = {"results": [{"id": 1, "date_modified": "2026-05-01T00:00:00Z"},
                              {"id": 2, "date_modified": "2026-04-01T00:00:00Z"}],
                 "next": "https://www.courtlistener.com/api/rest/v4/docket-entries/?page=2"}
        page2 = {"results": [{"id": 3, "date_modified": "2026-03-01T00:00:00Z"}],
                 "next": None}

        responses = iter([page1, page2])

        def handler(req):
            return httpx.Response(200, json=next(responses))

        cl = make_client(handler)
        ids = [e["id"] for e in cl.iter_entries(42)]
        # Yielded oldest-first: #3 (2026-03) → #2 (2026-04) → #1 (2026-05).
        assert ids == [3, 2, 1]

    def test_iter_entries_stops_when_below_modified_after(self, make_client):
        # API returns newest-first; we still want the early-stop optimization
        # so we don't page past the cutoff. Within the entries we DO yield,
        # order is oldest-first.
        pages = iter([
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
        ])
        page_count = [0]

        def handler(req):
            page_count[0] += 1
            return httpx.Response(200, json=next(pages))

        cl = make_client(handler)
        ids = [e["id"] for e in cl.iter_entries(42, modified_after="2026-04-01T00:00:00Z")]
        # #1, #2, #3 are above cutoff; #4 stops paging. Yielded oldest-first.
        assert ids == [3, 2, 1]
        # Should have stopped after page 2 — never fetched page 3.
        assert page_count[0] == 2


class TestInit:
    def test_requires_token(self, monkeypatch):
        monkeypatch.delenv("COURTLISTENER_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="COURTLISTENER_TOKEN"):
            CourtListener()

    def test_context_manager_closes_client(self):
        with CourtListener(token="x") as cl:
            assert cl.client is not None
