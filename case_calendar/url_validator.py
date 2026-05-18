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

Uses ``urllib.request`` from the stdlib — no third-party HTTP dependency.
``urlopen`` follows redirects by default via its ``HTTPRedirectHandler``.
"""

from __future__ import annotations

import http.client
import logging
import socket
import time
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import urlparse

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
# Transport-level exceptions the retry loop should retry. Mirrors the
# narrow set used in `courtlistener.py` and `pdf.py` — transient
# network blips only, not malformed URLs or unsupported schemes (those
# return None on first hit to avoid hammering a hopeless input).
_VALIDATE_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    socket.timeout,
    http.client.HTTPException,
    ConnectionError,
)
_cache: dict[str, str] = {}


def clear_cache() -> None:
    """Reset the per-process cache. Mainly for tests."""
    _cache.clear()


class _ValidateResponse:
    """Minimal duck-typed response from ``_request_with_retry``.

    ``_check`` only reads ``status_code``, so that's all we expose.
    """

    __slots__ = ("status_code",)

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def validate_url(url: str) -> Optional[str]:
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

    try:
        result, definite_failure = _walk_candidates(url)
    except Exception as e:
        log.warning(
            "URL validation unexpected error for %r (%s); keeping URL as-is",
            url,
            e,
        )
        return url

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


def _walk_candidates(url: str) -> tuple[Optional[str], bool]:
    """Returns (working_url_or_None, saw_definite_4xx).

    ``saw_definite_4xx`` is True if at least one candidate returned a 4xx
    HTTP response (= "the URL isn't there"). False means every attempt was
    either a network error or a 5xx (= "couldn't tell"), so the caller
    should fail-open rather than blanking the field.
    """
    saw_4xx = False
    for cand in _candidates(url):
        outcome = _check(cand)
        if outcome == "ok":
            return cand, saw_4xx
        if outcome == "4xx":
            saw_4xx = True
    return None, saw_4xx


def _request_with_retry(method: str, url: str) -> Optional[_ValidateResponse]:
    """Issue ``method`` against ``url`` with retry on transport errors.

    Returns a response carrying the final status code on any HTTP
    response (success or failure), or ``None`` when every attempt
    raised a transport error. ``urllib.error.URLError`` covers the
    same NetworkError class we retry elsewhere; non-retryable malformed
    URLs (``ValueError`` from `Request` construction) are also treated
    as a flake rather than crashing validation.

    urllib raises ``HTTPError`` (a subclass of ``URLError``) for 4xx /
    5xx responses; we catch that explicitly to surface the status code
    instead of treating those as transport flakes.
    """
    backoff = _VALIDATE_RETRY_INITIAL_BACKOFF
    # `while True` instead of `for attempt in range(...)` so every exit
    # is an explicit `return` — no loop-fall-off branch for coverage to
    # flag as unreachable.
    attempt = 1
    while True:
        try:
            req = urllib.request.Request(url, method=method)
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                return _ValidateResponse(status_code=resp.status)
        except urllib.error.HTTPError as e:
            # 4xx / 5xx — definite HTTP response, surface the status.
            return _ValidateResponse(status_code=e.code)
        except _VALIDATE_RETRYABLE_EXCEPTIONS as e:
            if attempt >= _VALIDATE_RETRY_TOTAL:
                log.info(
                    "URL validate transport error budget exhausted for %s %s: %s",
                    method,
                    url,
                    e,
                )
                return None
            time.sleep(backoff)
            backoff = min(backoff * 2, 4)
            attempt += 1
        except urllib.error.URLError:
            # Non-retryable URLError (e.g. unsupported scheme, refused
            # connection that isn't worth retrying). Fail open so a
            # transient transport blip never blanks a valid `dial_in`.
            return None
        except ValueError:
            # `Request(url)` raises ValueError on malformed URLs (the
            # LLM-synthesized garbage case the validator exists to catch).
            return None


def _check(url: str) -> str:
    """Returns 'ok', '4xx', or 'flake'."""
    r = _request_with_retry("HEAD", url)
    if r is None:
        return "flake"
    if r.status_code == 405:  # some servers don't implement HEAD
        r = _request_with_retry("GET", url)
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
