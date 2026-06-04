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
        # Mirror the attributes __init__ sets — _wait_for_window reads
        # _no_request_before on every request and _record_request reads the
        # rate-telemetry fields, so without these the __new__ bypass fails on
        # the first call.
        cl._no_request_before = 0.0
        cl._request_total = 0
        cl._request_times = []
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
            return httpx.Response(
                200, json={"id": "mad", "citation_string": "D. Mass."}
            )

        cl = make_client(handler)
        assert cl.get_court("mad")["citation_string"] == "D. Mass."

    def test_get_docket_entry(self, make_client):
        def handler(req):
            assert req.url.path == "/api/rest/v4/docket-entries/466316702/"
            return httpx.Response(
                200,
                json={
                    "id": 466316702,
                    "entry_number": 42,
                    "description": "ENDORSED ORDER ...",
                    "recap_documents": [{"id": 481552307, "is_available": True}],
                },
            )

        cl = make_client(handler)
        entry = cl.get_docket_entry(466316702)
        assert entry["id"] == 466316702
        assert entry["recap_documents"][0]["is_available"] is True

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
            return httpx.Response(200, json=next(responses))

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
            return httpx.Response(200, json=next(pages))

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

    def test_context_manager_closes_client(self):
        with CourtListener(token="x") as cl:
            assert cl.client is not None

    def test_client_follows_redirects(self):
        # httpx defaults follow_redirects=False (unlike requests). The
        # rest of the project's httpx clients (pdf.fetch_pdf_bytes,
        # pdf.fetch_url_bytes, url_validator) all set it True; the
        # CourtListener client must match so a hostname migration,
        # trailing-slash normalization, or similar 301/302 from
        # CourtListener doesn't become a failing request. Pin the
        # attribute so a future refactor of __init__ doesn't quietly
        # regress it.
        with CourtListener(token="x") as cl:
            assert cl.client.follow_redirects is True


class TestPeakInWindow:
    """`_peak_in_window`: busiest count of timestamps in any window-second span."""

    def test_empty(self):
        assert clmod._peak_in_window([], 60.0) == 0

    def test_all_within_window(self):
        assert clmod._peak_in_window([0.0, 1.0, 2.0, 59.0], 60.0) == 4

    def test_sliding_peak_is_two(self):
        # Busiest 60s span holds 2 of these (e.g. 0 & 30, or 61 & 90).
        assert clmod._peak_in_window([0.0, 30.0, 61.0, 90.0], 60.0) == 2

    def test_window_edge_is_exclusive(self):
        # A request exactly `window` apart falls in the NEXT window, not this.
        assert clmod._peak_in_window([0.0, 60.0], 60.0) == 1


class TestRequestRateTelemetry:
    def _ok(self, req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 1})

    def test_counts_each_request(self, make_client):
        cl = make_client(self._ok)
        cl.get_docket(1)
        cl.get_docket(2)
        assert cl._request_total == 2
        assert len(cl._request_times) == 2

    def test_429_then_success_counts_both(self, make_client, monkeypatch):
        # A 429 still hit the rate-limit bucket, so it's counted alongside
        # the retried success — two requests, not one.
        monkeypatch.setattr(clmod.time, "sleep", lambda _s: None)
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "1"})
            return httpx.Response(200, json={"id": 1})

        cl = make_client(handler)
        cl.get_docket(1)
        assert cl._request_total == 2

    def test_record_prunes_buffer_beyond_max_window(self, make_client, monkeypatch):
        # The rolling buffer keeps only the last 24h; the lifetime total does
        # not prune. Drive time.time() through a >24h span.
        seq = iter([0.0, 100.0, 86401.0])
        monkeypatch.setattr(clmod.time, "time", lambda: next(seq))
        cl = make_client(self._ok)
        cl._record_request()  # t=0
        cl._record_request()  # t=100
        cl._record_request()  # t=86401 -> cutoff 1.0 drops the t=0 sample
        assert cl._request_total == 3
        assert cl._request_times == [100.0, 86401.0]

    def test_log_request_stats_reports_total_and_peaks(self, make_client, caplog):
        import logging

        cl = make_client(self._ok)
        for n in range(3):
            cl.get_docket(n)
        with caplog.at_level(logging.INFO, logger="case_calendar.courtlistener"):
            cl.log_request_stats()
        line = next(
            (
                r.getMessage()
                for r in caplog.records
                if "courtlistener-requests" in r.getMessage()
            ),
            None,
        )
        assert line is not None
        # All three requests land within one second in the test, so every
        # rolling window holds all three.
        assert "total=3" in line
        assert "peak/min=3" in line
        assert "peak/hour=3" in line
        assert "peak/day=3" in line

    def test_log_request_stats_noop_when_no_requests(self, make_client, caplog):
        import logging

        cl = make_client(self._ok)
        with caplog.at_level(logging.INFO, logger="case_calendar.courtlistener"):
            cl.log_request_stats()
        assert not any(
            "courtlistener-requests" in r.getMessage() for r in caplog.records
        )

    def test_exit_logs_stats(self, caplog):
        import logging

        # Real __init__ so __exit__ runs the full close path; swap in a mock
        # transport so the request never leaves the process.
        cl = CourtListener(token="x")
        cl.client._transport = httpx.MockTransport(self._ok)
        cl.get_docket(7)
        with caplog.at_level(logging.INFO, logger="case_calendar.courtlistener"):
            cl.__exit__()
        assert any(
            "courtlistener-requests total=1" in r.getMessage() for r in caplog.records
        )


class TestTransportErrorRetry:
    """`_get` retries transient transport errors (ReadTimeout, ConnectError,
    RemoteProtocolError) in the same loop that handles 429 / 5xx. Before
    in-house retry, a single ReadTimeout mid-sync — the CourtListener
    server going quiet for a few seconds — propagated all the way up
    through `iter_entries` and killed the whole run (the production
    traceback we observed: `httpx.ReadTimeout: The read operation timed
    out`). These tests go through the real `__init__` and swap the
    httpx.Client's transport with a MockTransport so the retry layer
    being exercised IS the production layer.
    """

    @pytest.fixture(autouse=True)
    def _no_real_sleep(self, monkeypatch):
        # _get's transport-error path calls time.sleep between attempts;
        # skip it so tests run at memory speed.
        monkeypatch.setattr(clmod.time, "sleep", lambda _s: None)

    @staticmethod
    def _install_mock_backend(cl: CourtListener, handler) -> None:
        """Swap the client's transport with a MockTransport so requests go
        through `_get`'s retry loop with a controlled backend.
        """
        cl.client._transport = httpx.MockTransport(handler)

    def test_retries_read_timeout_then_succeeds(self):
        attempts = [0]

        def handler(req: httpx.Request) -> httpx.Response:
            attempts[0] += 1
            if attempts[0] == 1:
                raise httpx.ReadTimeout("timed out", request=req)
            return httpx.Response(200, json={"id": 42})

        cl = CourtListener(token="x")
        self._install_mock_backend(cl, handler)
        assert cl.get_docket(42)["id"] == 42
        assert attempts[0] == 2

    def test_retries_connect_error_then_succeeds(self):
        attempts = [0]

        def handler(req: httpx.Request) -> httpx.Response:
            attempts[0] += 1
            if attempts[0] < 3:
                raise httpx.ConnectError("refused", request=req)
            return httpx.Response(200, json={"id": 7})

        cl = CourtListener(token="x")
        self._install_mock_backend(cl, handler)
        assert cl.get_docket(7)["id"] == 7
        assert attempts[0] == 3

    def test_exhausted_retries_propagate_last_transport_error(self):
        # When every attempt fails, the last TransportError surfaces.
        # `sync_case`'s linear-control-flow shape means this propagation
        # does NOT advance the docket's date_last_modified cutoff, so
        # the next sync re-walks the docket — exactly what we want.
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=req)

        cl = CourtListener(token="x")
        self._install_mock_backend(cl, handler)
        with pytest.raises(httpx.ReadTimeout):
            cl.get_docket(42)

    def test_429_response_reaches_get_logging_and_cooldown(self, monkeypatch, caplog):
        """Regression: when an external retry library handled 429 at the
        transport layer (the prior httpx-retries setup), the response
        never reached ``_get``'s logging or cooldown machinery, and
        operators saw "hang" instead of "rate limited" for the
        ~24h-Retry-After daily-bucket case. With retry now inline in
        ``_get``, three signals must fire on every 429:
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

        def handler(req: httpx.Request) -> httpx.Response:
            calls[0] += 1
            if calls[0] == 1:
                return httpx.Response(
                    429,
                    headers={"Retry-After": "7", "X-RateLimit-Remaining": "0"},
                    json={"detail": "Throttled"},
                )
            return httpx.Response(200, json={"id": 42})

        cl = CourtListener(token="x")
        self._install_mock_backend(cl, handler)

        with caplog.at_level("WARNING", logger="case_calendar.courtlistener"):
            assert cl.get_docket(42)["id"] == 42

        warning_messages = [
            r.getMessage() for r in caplog.records if r.levelname == "WARNING"
        ]
        assert any("courtlistener 429" in m for m in warning_messages), warning_messages
        assert any("Retry-After" in m for m in warning_messages), warning_messages
        assert cl._no_request_before > 0.0
        assert slept, "expected at least one sleep call from _get"
        assert slept[0] == 7.0 + clmod._RETRY_AFTER_BUFFER_SECONDS

    def test_transport_retry_budget_caps_attempts(self, monkeypatch):
        # `_TRANSPORT_RETRY_BUDGET` is a separate counter from the
        # response-status retry loop, so a long stretch of transport
        # errors gives up after `_TRANSPORT_RETRY_BUDGET + 1` attempts
        # instead of consuming the 429-handling budget.
        monkeypatch.setattr(clmod.time, "sleep", lambda _s: None)
        attempts = [0]

        def handler(req: httpx.Request) -> httpx.Response:
            attempts[0] += 1
            raise httpx.ReadTimeout("timed out", request=req)

        cl = CourtListener(token="x")
        self._install_mock_backend(cl, handler)
        with pytest.raises(httpx.ReadTimeout):
            cl.get_docket(42)
        # The first attempt counts as 1; subsequent attempts up to the
        # budget cap then a final raise. Exact count: budget + 1 (the
        # attempt that exhausts the budget is the one that re-raises).
        assert attempts[0] == clmod._TRANSPORT_RETRY_BUDGET + 1

    def test_loop_exhausted_with_no_response_raises_runtime_error(self, monkeypatch):
        # Safety-net coverage. The `_get` retry loop's post-loop tail
        # raises ``RuntimeError`` when every iteration completed via
        # the transport-error path (no `last_response` ever recorded)
        # AND `range(6)` ran out. Under the shipped constants
        # (``_TRANSPORT_RETRY_BUDGET=5`` < loop count 6) this is
        # unreachable — the 6th transport error re-raises before the
        # loop can exit normally. Patch the budget above the loop
        # count so transport errors stop raising mid-loop; that lets
        # the loop iterate all 6 times and fall through to the tail
        # raise, proving the safety net still fires if the constants
        # ever drift apart.
        monkeypatch.setattr(clmod, "_TRANSPORT_RETRY_BUDGET", 100)

        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=req)

        cl = CourtListener(token="x")
        self._install_mock_backend(cl, handler)
        with pytest.raises(RuntimeError, match="no response from"):
            cl.get_docket(42)


class TestFollowsRedirects:
    def test_get_follows_302_to_final_response(self):
        # Behavior-level confirmation that goes through the real
        # `__init__`: a CourtListener endpoint that serves a 302
        # redirect (e.g. a future hostname migration or a
        # trailing-slash normalization layer) lands on the final
        # response transparently rather than turning into a status
        # error and tripping the _get retry path.
        #
        # The MockTransport is swapped into the client built by the
        # real constructor — that way the test fails if the
        # constructor stops setting follow_redirects=True, not just
        # if httpx itself breaks.
        call_count = [0]

        def handler(req: httpx.Request) -> httpx.Response:
            call_count[0] += 1
            if req.url.path == "/api/rest/v4/dockets/42/":
                return httpx.Response(
                    302,
                    headers={
                        "Location": "https://www.courtlistener.com/api/rest/v4/dockets/42/new/"
                    },
                )
            assert req.url.path == "/api/rest/v4/dockets/42/new/"
            return httpx.Response(200, json={"id": 42, "case_name": "Redirected"})

        cl = CourtListener(token="test")
        # Replace the network transport with our mock while keeping
        # every other client setting the real constructor produced —
        # crucially `follow_redirects=True`.
        cl.client._transport = httpx.MockTransport(handler)
        assert cl.get_docket(42)["case_name"] == "Redirected"
        # Two transport calls: the 302 then the redirected 200.
        assert call_count[0] == 2
