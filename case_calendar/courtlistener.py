"""Thin CourtListener REST v4 client.

Only covers the endpoints we need: dockets, docket-entries, recap-documents.

Uses ``urllib.request`` from the stdlib — no third-party HTTP dependency.
Connection pooling isn't currently used (every call opens a fresh TLS
connection); for this workload, per-call LLM latency dwarfs the TLS
handshake cost, so the added complexity of an http.client.HTTPSConnection
pool isn't worth it. Add one later if profiling shows otherwise.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator, Optional

API_BASE = "https://www.courtlistener.com/api/rest/v4"

log = logging.getLogger(__name__)

# Buffer added to every Retry-After value, AND enforced as a no-go-before
# barrier across *all* requests on the client (not just the one that 429'd).
# Without this, two things go wrong:
#   1. The server's rate-limit window and our local clock drift just enough
#      that sleeping exactly Retry-After lands us back in the SAME window —
#      so we get another 429, sleep again, get another, and never make
#      progress. Backoffs pile up indefinitely.
#   2. After a single call honors Retry-After and succeeds, the very next
#      call (different URL) immediately re-tripleups the window because we
#      haven't tracked that quota is exhausted globally on the client.
# The buffer (a few seconds past Retry-After) plus the shared barrier
# `_no_request_before` solve both. Tune up if 429 cascades come back.
_RETRY_AFTER_BUFFER_SECONDS = 5.0

# Transport-level exceptions worth retrying inside `_get`. A single
# socket timeout / refused connection / dropped TLS mid-sync used to
# propagate up through `iter_entries` and kill the whole run. We retry
# them in the same loop that handles 429 / 5xx so there's exactly one
# place to reason about backoff and the cross-request cooldown.
#
# ``urllib.error.URLError`` is the stdlib parent for most network
# failures (DNS resolution, connection refused, etc.). ``socket.timeout``
# (== ``TimeoutError`` in 3.10+) covers connect-/read-timeout cases.
# ``http.client.HTTPException`` covers protocol-level errors like
# ``BadStatusLine`` and ``RemoteDisconnected``. ``ConnectionError`` is
# kept for completeness (e.g. ``ConnectionResetError`` from the OS
# socket layer).
_RETRYABLE_TRANSPORT_EXCEPTIONS: tuple[type[Exception], ...] = (
    urllib.error.URLError,
    socket.timeout,
    http.client.HTTPException,
    ConnectionError,
)
# Cap on transport-exception retries within a single `_get` call. The
# cap applies independently of the response-status retry budget so a
# stretch of transient transport errors followed by a real 429 still
# has the whole 429-handling budget available.
_TRANSPORT_RETRY_BUDGET = 5


class HTTPStatusError(RuntimeError):
    """Raised when CourtListener returns a non-success status that the
    retry loop in ``_get`` couldn't recover from.

    Carries the HTTP status code, the response body (decoded as best
    effort), and the request URL so callers can branch on the failure
    shape. Subclasses ``RuntimeError`` to fit the project convention.
    """

    def __init__(self, status_code: int, body: str, url: str):
        super().__init__(f"HTTP {status_code} from {url}: {body[:200]}")
        self.status_code = status_code
        self.body = body
        self.url = url


class _Response:
    """Tiny duck-typed response wrapper for ``_get``'s return value.

    Exposes ``status_code``, ``headers``, ``text``, ``content``, and
    ``json()`` — the subset call sites in the rest of the project read.
    """

    __slots__ = ("status_code", "headers", "_body", "url")

    def __init__(
        self,
        *,
        status_code: int,
        headers: Any,
        body: bytes,
        url: str,
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self._body = body
        self.url = url

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self._body)


class CourtListener:
    def __init__(self, token: Optional[str] = None, timeout: float = 30.0):
        token = token or os.environ.get("COURTLISTENER_TOKEN")
        if not token:
            raise RuntimeError("COURTLISTENER_TOKEN env var required")
        self.token = token
        self.timeout = timeout
        # Earliest monotonic time at which the next request may be issued —
        # set when any call hits a 429, so subsequent calls on the same
        # client share the cooldown rather than each one independently
        # tripping the same window.
        self._no_request_before: float = 0.0

    def _wait_for_window(self) -> None:
        """Block until the shared no-go-before timestamp has passed."""
        now = time.monotonic()
        if self._no_request_before > now:
            time.sleep(self._no_request_before - now)

    def _build_url(self, url: str, params: Optional[dict[str, Any]] = None) -> str:
        """Return ``url`` with ``params`` appended as a query string.

        If the URL already has a query string, the new params are
        appended with ``&``; otherwise they start the query with ``?``.
        Empty / None params are a no-op.
        """
        if not params:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}{urllib.parse.urlencode(params)}"

    def _get(self, url: str, params: Optional[dict[str, Any]] = None) -> _Response:
        """GET with retry on 429 (Retry-After), 5xx (exponential backoff), and
        transient transport exceptions (socket timeout, connection refused,
        protocol errors).

        Honors any Retry-After value, even multi-hour ones — CourtListener's free tier
        caps at 300/day and the daily bucket can legitimately ask for a wait
        of nearly 24h once exhausted. Sleeping through that lets the script
        resume on its own rather than requiring a manual restart per cycle.
        We still log the URL / body / rate-limit headers on every 429 so
        you can see in the log which bucket tripped.

        Every Retry-After sleep adds ``_RETRY_AFTER_BUFFER_SECONDS`` so our
        next request lands safely past the server's window-reset clock
        (otherwise sub-second drift causes the same window to re-trip), and
        the resulting cooldown is recorded on the client so subsequent
        calls also wait — without that, one call honors the backoff, the
        next call immediately re-trips it.

        Transport-exception retries use a separate budget (``_TRANSPORT_RETRY_BUDGET``)
        so a stretch of transient transport errors doesn't consume the
        429/5xx retry budget that may still be needed for response-status
        handling on the same call.

        ``urllib.request.urlopen`` follows 3xx redirects automatically
        via its default ``HTTPRedirectHandler``.
        """
        full_url = self._build_url(url, params)
        delay = 2.0
        transport_delay = 0.5
        transport_attempts = 0
        last_response: Optional[_Response] = None
        for attempt in range(6):
            self._wait_for_window()
            req = urllib.request.Request(
                full_url,
                headers={"Authorization": f"Token {self.token}"},
                method="GET",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    status = resp.status
                    headers = resp.headers
                    body = resp.read()
            except urllib.error.HTTPError as e:
                # urlopen raises HTTPError for 4xx/5xx responses; we still
                # want to inspect status / body / headers from the
                # response, so peel them off the exception.
                status = e.code
                headers = e.headers
                body = e.read()
            except _RETRYABLE_TRANSPORT_EXCEPTIONS as e:
                transport_attempts += 1
                if transport_attempts > _TRANSPORT_RETRY_BUDGET:
                    log.warning(
                        "courtlistener transport error budget exhausted (%d attempts) for %s: %s",
                        transport_attempts,
                        full_url,
                        e,
                    )
                    raise
                log.warning(
                    "courtlistener transport error (attempt %d/%d) for %s: %s; retrying in %.1fs",
                    transport_attempts,
                    _TRANSPORT_RETRY_BUDGET,
                    full_url,
                    e,
                    transport_delay,
                )
                time.sleep(transport_delay)
                transport_delay = min(transport_delay * 2, 30)
                continue

            last_response = _Response(
                status_code=status,
                headers=headers,
                body=body,
                url=full_url,
            )
            if status == 429:
                # `headers` is the stdlib's `http.client.HTTPMessage`,
                # which supports `.get` / dict-like access.
                base_wait = float(headers.get("Retry-After", delay))
                wait = base_wait + _RETRY_AFTER_BUFFER_SECONDS
                rate_headers = {
                    k: v
                    for k, v in headers.items()
                    if k.lower().startswith(("x-ratelimit", "retry-after"))
                }
                body_excerpt = last_response.text[:500]
                log.warning(
                    "courtlistener 429; sleeping %.0fs (Retry-After=%.0f + %.0fs buffer, attempt %d). url=%s headers=%s body=%s",
                    wait,
                    base_wait,
                    _RETRY_AFTER_BUFFER_SECONDS,
                    attempt + 1,
                    full_url,
                    rate_headers,
                    body_excerpt,
                )
                self._no_request_before = max(
                    self._no_request_before,
                    time.monotonic() + wait,
                )
                time.sleep(wait)
                delay = min(delay * 2, 60)
                continue
            if 500 <= status < 600:
                log.warning("courtlistener %s; retrying in %.1fs", status, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if 400 <= status < 500:
                # Non-429 4xx: surface immediately, like the prior
                # `r.raise_for_status()` call did.
                raise HTTPStatusError(status, last_response.text, full_url)
            return last_response
        # Retries exhausted. Surface the last status as an HTTPStatusError
        # so callers can branch on it.
        assert last_response is not None, "loop must have populated last_response"
        raise HTTPStatusError(
            last_response.status_code, last_response.text, full_url
        )

    def close(self) -> None:
        """Kept for the ``with CourtListener() as cl: ...`` idiom.

        No persistent state to release in the stdlib path; this is a
        no-op preserved so existing call sites (cmd_sync, cmd_serve)
        can keep their context-manager shape.
        """
        return None

    def __enter__(self) -> "CourtListener":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # --- Dockets ---

    def get_docket(self, docket_id: int) -> dict:
        return self._get(f"{API_BASE}/dockets/{docket_id}/").json()

    # --- Docket entries ---

    def iter_entries(
        self,
        docket_id: int,
        *,
        modified_after: Optional[str] = None,
        page_size: int = 50,
        max_pages: int = 20,
    ) -> Iterator[dict]:
        """Iterate docket entries oldest first.

        We page the CourtListener API newest-first (so incremental syncs stop cheaply
        once they cross ``modified_after``) but buffer the result and yield
        oldest-first. Per-entry processing depends on referenced motions
        already being in the local store before the orders that cite them
        are handled — yielding oldest-first guarantees that within a sync.

        ``modified_after`` is an ISO-8601 timestamp. When provided, we stop
        paging once entries fall below it, which keeps incremental syncs cheap.
        """
        params: Optional[dict[str, Any]] = {
            "docket": docket_id,
            "order_by": "-date_modified",
            "page_size": page_size,
        }
        url: Optional[str] = f"{API_BASE}/docket-entries/"
        pages = 0
        buffer: list[dict] = []
        while url and pages < max_pages:
            r = self._get(url, params=params if pages == 0 else None)
            data = r.json()
            below_cutoff = False
            for entry in data["results"]:
                if modified_after and entry.get("date_modified", "") < modified_after:
                    below_cutoff = True
                    break
                buffer.append(entry)
            if below_cutoff:
                break
            url = data.get("next")
            pages += 1
        buffer.sort(key=lambda e: e.get("date_modified") or "")
        yield from buffer

    # --- RECAP documents (PDF metadata + extracted plain text) ---

    def get_recap_document(self, doc_id: int) -> dict:
        return self._get(f"{API_BASE}/recap-documents/{doc_id}/").json()

    # --- Courts ---

    def get_court(self, court_id: str) -> dict:
        return self._get(f"{API_BASE}/courts/{court_id}/").json()
