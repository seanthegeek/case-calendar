"""HTTP validation + repair for LLM-extracted URLs.

Court docket text occasionally concatenates a URL with the next sentence or
citation token without a separator (e.g. ``.../lin-rita-f-rfl/Civ LR 77-3(d)``
where the real URL ends at the trailing slash and ``Civ LR 77-3(d)`` is a
local-rule citation). The LLM faithfully copies the malformed string into
``dial_in``. We HEAD the URL; if it 4xx's we try the parent path once. If
nothing works, the caller drops ``dial_in`` and stashes the raw URL in notes
so the human reader can salvage it.

Fail-open on network errors — a court site being briefly unreachable should
not blank the field. Successful validations are cached per process; failures
are not (so a transient flake gets retried on the next sync).
"""

from __future__ import annotations

import logging
import time
from typing import Optional
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

_TIMEOUT = 5.0
# Short retry budget — validation runs once per LLM-extracted `dial_in`
# URL on the sync hot path. Three attempts with 0.25s / 0.5s / 1s
# backoff bound the worst case at roughly the validator's existing
# per-URL timeout × 4 ~ 20s, which keeps a flaky host from stalling
# sync without abandoning a real transient blip too eagerly. The
# validator already fails open when every attempt yields a transport
# error, so the only cost of retry exhaustion is keeping the original
# URL unchanged — same as the pre-retry behavior.
_VALIDATE_RETRY_TOTAL = 3
_VALIDATE_RETRY_INITIAL_BACKOFF = 0.25
_VALIDATE_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)
_cache: dict[str, str] = {}


def clear_cache() -> None:
    """Reset the per-process cache. Mainly for tests."""
    _cache.clear()


def validate_url(url: str, *, client: Optional[httpx.Client] = None) -> Optional[str]:
    """Return a working URL (the original or a one-step parent) or None.

    Returns the original URL unchanged on network failure (fail-open).
    Successful results are cached; failures are not, so transient errors get
    retried.
    """
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return url or None
    if url in _cache:
        return _cache[url]

    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=_TIMEOUT, follow_redirects=True)
    try:
        result, definite_failure = _walk_candidates(url, client)
    except Exception as e:
        log.warning(
            "URL validation unexpected error for %r (%s); keeping URL as-is",
            url,
            e,
        )
        return url
    finally:
        if own_client:
            client.close()

    if result is not None:
        _cache[url] = result
        if result != url:
            log.info("URL repair: %r -> %r", url, result)
        return result
    if not definite_failure:
        # Every attempt was a network error or 5xx — server's flaky, not the
        # URL. Don't blank the field over a transient flake.
        log.warning("URL %r unreachable (no definite 4xx); keeping as-is", url)
        return url
    log.info("URL %r returned 4xx and no parent path worked", url)
    return None


def _walk_candidates(url: str, client: httpx.Client) -> tuple[Optional[str], bool]:
    """Returns (working_url_or_None, saw_definite_4xx).

    ``saw_definite_4xx`` is True if at least one candidate returned a 4xx
    HTTP response (= "the URL isn't there"). False means every attempt was
    either a network error or a 5xx (= "couldn't tell"), so the caller
    should fail-open rather than blanking the field.
    """
    saw_4xx = False
    for cand in _candidates(url):
        outcome = _check(cand, client)
        if outcome == "ok":
            return cand, saw_4xx
        if outcome == "4xx":
            saw_4xx = True
    return None, saw_4xx


def _request_with_retry(
    method: str, url: str, client: httpx.Client
) -> Optional[httpx.Response]:
    """Issue ``method`` against ``url`` with retry on transport errors.

    Returns the response on success (any status), or ``None`` when every
    attempt raised a transport error. ``httpx.RequestError`` covers the
    same NetworkError / TimeoutException / RemoteProtocolError set we
    retry elsewhere in the codebase; we catch the wider parent here so a
    less common protocol-level error (e.g. an invalid URL synthesized by
    the LLM) is also treated as a flake rather than crashing validation.
    """
    backoff = _VALIDATE_RETRY_INITIAL_BACKOFF
    for attempt in range(1, _VALIDATE_RETRY_TOTAL + 1):
        try:
            return client.request(method, url)
        except _VALIDATE_RETRYABLE_EXCEPTIONS as e:
            if attempt == _VALIDATE_RETRY_TOTAL:
                log.info(
                    "URL validate transport error budget exhausted for %s %s: %s",
                    method,
                    url,
                    e,
                )
                return None
            time.sleep(backoff)
            backoff = min(backoff * 2, 4)
        except httpx.RequestError:
            return None
    return None  # unreachable: every path inside the loop returns


def _check(url: str, client: httpx.Client) -> str:
    """Returns 'ok', '4xx', or 'flake'."""
    r = _request_with_retry("HEAD", url, client)
    if r is None:
        return "flake"
    if r.status_code == 405:  # some servers don't implement HEAD
        r = _request_with_retry("GET", url, client)
        if r is None:
            return "flake"
    if r.status_code < 400:
        return "ok"
    if r.status_code >= 500:
        return "flake"
    return "4xx"


def _candidates(url: str) -> list[str]:
    """The original URL plus, optionally, ONE parent-path truncation.

    We don't walk all the way down to the domain — that would land on an
    unrelated home page and falsely look "valid" while pointing the user
    nowhere useful. The clerk-typo failure mode is "real_url/junk" — strip
    the junk, get back to the real URL, stop.
    """
    out = [url]
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        # /single-segment paths get tried once; we won't fall back to /.
        return out
    parts.pop()
    path = "/" + "/".join(parts) + "/"
    out.append(parsed._replace(path=path, query="", fragment="").geturl())
    return out
