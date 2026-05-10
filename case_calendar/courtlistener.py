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


class CourtListener:
    def __init__(self, token: Optional[str] = None, timeout: float = 30.0):
        token = token or os.environ.get("COURTLISTENER_TOKEN")
        if not token:
            raise RuntimeError("COURTLISTENER_TOKEN env var required")
        self.client = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Token {token}"},
        )

    def _get(self, url: str, params: Optional[dict[str, Any]] = None) -> httpx.Response:
        """GET with retry on 429 (Retry-After) and 5xx (exponential backoff).

        Honors any Retry-After value, even multi-hour ones — CL's free tier
        caps at 300/day and the daily bucket can legitimately ask for a wait
        of nearly 24h once exhausted. Sleeping through that lets the script
        resume on its own rather than requiring a manual restart per cycle.
        We still log the URL / body / rate-limit headers on every 429 so
        you can see in the log which bucket tripped.
        """
        delay = 2.0
        last_response: Optional[httpx.Response] = None
        for attempt in range(6):
            r = self.client.get(url, params=params)
            last_response = r
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", delay))
                rate_headers = {
                    k: v for k, v in r.headers.items() if k.lower().startswith(("x-ratelimit", "retry-after"))
                }
                body_excerpt = r.text[:500]
                log.warning(
                    "courtlistener 429; sleeping %.0fs (attempt %d). url=%s headers=%s body=%s",
                    wait,
                    attempt + 1,
                    r.request.url,
                    rate_headers,
                    body_excerpt,
                )
                time.sleep(wait)
                delay = min(delay * 2, 60)
                continue
            if 500 <= r.status_code < 600:
                log.warning("courtlistener %s; retrying in %.1fs", r.status_code, delay)
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

        We page the CL API newest-first (so incremental syncs stop cheaply
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
