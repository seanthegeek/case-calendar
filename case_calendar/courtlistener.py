"""Thin CourtListener REST v4 client.

Only covers the endpoints we need: dockets, docket-entries, recap-documents.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Iterator, Optional

import httpx

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
# ReadTimeout / ConnectError / RemoteProtocolError mid-sync (the
# CourtListener server going briefly quiet, a DNS blip, a TLS reset) used
# to propagate up through `iter_entries` and kill the whole run. We retry
# them in the same loop that handles 429 / 5xx so there's exactly one
# place to reason about backoff and the cross-request cooldown.
_RETRYABLE_TRANSPORT_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)
# Cap on transport-exception retries within a single `_get` call. Picked
# to roughly match the prior httpx-retries `total=5` budget; the cap
# applies independently of the response-status retry budget so a stretch
# of transient transport errors followed by a real 429 still has the
# whole 429-handling budget available.
_TRANSPORT_RETRY_BUDGET = 5


class CourtListener:
    def __init__(self, token: Optional[str] = None, timeout: float = 30.0):
        token = token or os.environ.get("COURTLISTENER_TOKEN")
        if not token:
            raise RuntimeError("COURTLISTENER_TOKEN env var required")
        self.client = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Token {token}"},
            # httpx defaults to follow_redirects=False (unlike requests),
            # so a 301/302 from CourtListener would otherwise become an
            # error rather than transparently following to the new URL.
            # Match the rest of the project's httpx clients (the PDF
            # fetch chain in pdf.py and the URL validator) so a future
            # hostname migration, trailing-slash normalization, or
            # similar reshape doesn't break the API client.
            follow_redirects=True,
        )
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

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> httpx.Response:
        """Issue a request with retry on 429 (Retry-After), 5xx (exponential
        backoff), and transient transport exceptions (ReadTimeout /
        ConnectError / RemoteProtocolError).

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

        ``method`` is "GET" or "POST"; both share the same retry shape
        because CourtListener applies the same rate-limit + transient-
        failure characteristics to either verb.
        """
        delay = 2.0
        transport_delay = 0.5
        transport_attempts = 0
        last_response: Optional[httpx.Response] = None
        for attempt in range(6):
            self._wait_for_window()
            try:
                r = self.client.request(method, url, params=params, json=json_body)
            except _RETRYABLE_TRANSPORT_EXCEPTIONS as e:
                transport_attempts += 1
                if transport_attempts > _TRANSPORT_RETRY_BUDGET:
                    # Transport errors are network-layer (DNS,
                    # connection refused, read timeout, TLS handshake
                    # failure) — surface the exception type so the
                    # operator can tell e.g. ConnectTimeout (firewall /
                    # CourtListener degraded) from ReadTimeout (large
                    # response timed out, increase timeout) from
                    # ConnectError (CourtListener fully down).
                    log.warning(
                        "courtlistener transport error budget exhausted "
                        "(%d attempts) for %s: %s: %s",
                        transport_attempts,
                        url,
                        type(e).__name__,
                        e,
                    )
                    raise
                log.warning(
                    "courtlistener transport error (attempt %d/%d) for %s: "
                    "%s: %s; retrying in %.1fs",
                    transport_attempts,
                    _TRANSPORT_RETRY_BUDGET,
                    url,
                    type(e).__name__,
                    e,
                    transport_delay,
                )
                time.sleep(transport_delay)
                transport_delay = min(transport_delay * 2, 30)
                continue
            last_response = r
            if r.status_code == 429:
                base_wait = float(r.headers.get("Retry-After", delay))
                wait = base_wait + _RETRY_AFTER_BUFFER_SECONDS
                rate_headers = {
                    k: v
                    for k, v in r.headers.items()
                    if k.lower().startswith(("x-ratelimit", "retry-after"))
                }
                body_excerpt = r.text[:500]
                log.warning(
                    "courtlistener 429; sleeping %.0fs (Retry-After=%.0f + %.0fs buffer, attempt %d). url=%s headers=%s body=%s",
                    wait,
                    base_wait,
                    _RETRY_AFTER_BUFFER_SECONDS,
                    attempt + 1,
                    r.request.url,
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
            if 500 <= r.status_code < 600:
                # 5xx means CourtListener's server is having trouble.
                # Distinguish 500 (origin app error, less likely to fix
                # itself) from 502 / 503 / 504 (gateway / load-balancer
                # transient — usually resolves within a few seconds).
                # Operators triaging recurring 5xxs should know which.
                category = (
                    "origin app error"
                    if r.status_code == 500
                    else "gateway / load-balancer transient"
                )
                log.warning(
                    "courtlistener %s (%s) for %s; retrying in %.1fs",
                    r.status_code,
                    category,
                    r.request.url,
                    delay,
                )
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            r.raise_for_status()
            return r
        # Retries exhausted. Surface the underlying HTTP error so callers can
        # branch on response status / headers (httpx.HTTPStatusError carries
        # the response object).
        if last_response is not None:
            last_response.raise_for_status()
        raise RuntimeError(f"courtlistener: no response from {url}")

    def _get(self, url: str, params: Optional[dict[str, Any]] = None) -> httpx.Response:
        """Thin GET wrapper around :meth:`_request`."""
        return self._request("GET", url, params=params)

    def _post(self, url: str, json_body: dict[str, Any]) -> httpx.Response:
        """Thin POST wrapper around :meth:`_request`."""
        return self._request("POST", url, json_body=json_body)

    def close(self) -> None:
        self.client.close()

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
        params = {
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

    # --- Docket alerts (webhook subscriptions) ---

    def iter_docket_alerts(
        self, *, page_size: int = 100, max_pages: int = 20
    ) -> Iterator[dict]:
        """Iterate the authenticated user's docket-alert subscriptions.

        Each result has at minimum ``docket`` (int), ``alert_type`` (int —
        1 means subscribed), ``date_created`` / ``date_modified``, and
        ``secret_key``. Paginated; we walk forward via the ``next`` link
        so the caller sees everything that fits within ``max_pages``.
        """
        params: Optional[dict[str, Any]] = {"page_size": page_size}
        url: Optional[str] = f"{API_BASE}/docket-alerts/"
        pages = 0
        while url and pages < max_pages:
            r = self._get(url, params=params if pages == 0 else None)
            data = r.json()
            for alert in data.get("results", []):
                yield alert
            url = data.get("next")
            pages += 1

    def create_docket_alert(self, docket_id: int, *, alert_type: int = 1) -> dict:
        """Create a docket-alert subscription. Returns the new alert row.

        ``alert_type`` defaults to 1 (subscribe). CourtListener also
        accepts 0 (unsubscribe) but this client doesn't expose that —
        the project's only need is the subscribe-on-startup feature.
        """
        r = self._post(
            f"{API_BASE}/docket-alerts/",
            json_body={"docket": docket_id, "alert_type": alert_type},
        )
        return r.json()
