"""PDF text extraction with fallbacks.

CourtListener's ``plain_text`` is the success path. When it's empty for an
otherwise-available PDF, we try, in order:

  1. Pull the PDF from Internet Archive (``filepath_ia``) and run pypdf —
     handles any PDF that has embedded text.
  2. If pypdf yields nothing usable AND the local system has ``tesseract``
     and ``pdftoppm`` (poppler) installed, OCR each page.

If neither path works we return ``None`` and the caller skips this PDF; the
entry will be re-fingerprinted next sync, so we'll retry once CourtListener has the
text or the user installs OCR tools.
"""

from __future__ import annotations

import io
import logging
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger(__name__)

# Transport-level exceptions worth retrying when fetching a PDF. Without
# retry, a single mid-fetch read timeout on the IA mirror would push us
# straight to the CourtListener storage fallback (which has stricter
# rate limits and is more likely to fail too), and a blip on both would
# surface as a permanent fetch failure for the entry.
_PDF_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)
# Transient HTTP status codes worth retrying — same set httpx-retries'
# default ``status_forcelist`` covered (429 + the gateway/proxy 5xxs).
# A 500 from the origin probably won't fix itself; a 502 / 503 / 504
# from a CDN or rate limiter often will.
_PDF_RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 502, 503, 504})
# Up to four retry attempts with 0.5s / 1s / 2s / 4s backoff before
# giving up — matches the prior httpx-retries `total=4, backoff_factor=0.5`.
_PDF_RETRY_TOTAL = 4
_PDF_RETRY_INITIAL_BACKOFF = 0.5

# Heuristic: a 2-page+ document with under 100 chars of text probably failed
# extraction. Don't burn OCR cycles on one-pagers though.
_MIN_USEFUL_CHARS = 100

# Some PDFs use custom font encodings (subsetted fonts with no /ToUnicode
# map) that pypdf can't decode — it emits glyph-index tokens like ``/i255``
# or maps the bytes 1:1 into Latin-1 control codepoints (``ÿ`` etc.). The
# result is a multi-KB "text" string that's non-empty and has the page
# header/footer bits in real ASCII, but the body is gibberish. Upstream
# CourtListener's pipeline runs pypdf the same way, so its ``plain_text`` field can
# carry the same broken extraction — see the us-v-dubranova first
# superseding indictment where CourtListener's plain_text was 27KB of `ÿ`-noise and
# the summary LLM hallucinated a fictitious "CSRERI / Roskomnadzor"
# organization to fill the gap, when the actual indictment named a
# real-world group ("CISM") that OCR reads cleanly. The fix: detect
# garbled output (alpha-letter ratio under ``_MIN_ALPHA_RATIO``) and
# fall through to the next stage of the extraction chain — local pypdf
# from plain_text, OCR from pypdf — until we get something clean.
#
# Threshold rationale: real English prose runs 70-80% alpha even when
# punctuation-dense; the most number-heavy legal headers ("Case
# 2:25-cr-00578-SRM Document 33 Filed 08/21/25 Page 1 of 15 Page ID
# #:305") still come in over 50% alpha. Garbled extracts reliably land
# under 10%. 0.4 leaves a comfortable margin on both sides.
_MIN_ALPHA_RATIO = 0.4


# PACER page-header boilerplate. Every page in every PACER-source PDF
# carries a stamp of this shape across the top:
#
#   Case 1:24-cr-00234-RMB     Document 1     Filed 04/03/24     Page 1 of 18 PageID: 32
#   Case 2:25-cr-00578-SRM Document 33 Filed 08/21/25 Page 1 of 15 Page ID #:305
#
# (The Central District of California writes "Page ID #:" instead of
# "PageID:"; some courts append a judge initials suffix to the docket
# number; some don't include all four trailing fields.) When an
# indictment is an image-only scan with a thin OCR overlay covering
# only the page-header band, pypdf reads the overlay as several KB of
# clean ASCII — passes the alpha-ratio gate — but the document body
# (which lives only in the page images, not in any text layer) never
# reaches the caller. The document itself has full content; the
# EXTRACTION is the part that's stamps-only. ``is_usable_text`` rejects
# both this case and font-encoding gibberish so callers fall through
# to the OCR stage.
_PACER_PAGE_HEADER_RE = re.compile(
    r"Case\s+\d+:\d+-[a-z]+-\d+(?:-\w+)?\s+"
    r"Document\s+\d+(?:-\d+)?"
    r"(?:\s+Filed\s+\d+/\d+/\d+)?"
    r"(?:\s+Page\s+\d+\s+of\s+\d+)?"
    r"(?:\s+Page\s*ID:?\s*\#?:?\s*\d+)?",
    re.IGNORECASE,
)


def is_usable_text(text: str) -> bool:
    """Return True when ``text`` is long enough and clean enough to use.

    Single positive predicate that callers in the extraction chain use
    to decide whether to keep an extraction result or fall through to
    the next stage (CourtListener ``plain_text`` → local pypdf → OCR).
    Bundles three checks the callers used to do separately:

    1. **Length** — at least ``_MIN_USEFUL_CHARS``. A 2-page+ document
       extracting to under 100 chars almost certainly failed; don't
       burn the cost of feeding it to the summary LLM.
    2. **Font-encoding gibberish** — PDFs using custom font subsets
       without a `/ToUnicode` map produce text whose alpha-character
       ratio falls below ``_MIN_ALPHA_RATIO`` (us-v-dubranova case).
    3. **PACER stamps with no body** — image-only scans with a thin OCR
       overlay covering just the page-header band let pypdf read clean
       ASCII headers off every page but the document body never
       reaches the caller. The text passes the alpha-ratio gate
       trivially (page stamps are mostly letters and digits) but
       contains nothing the summary LLM can use. Strip the stamps; if
       less than ``_MIN_USEFUL_CHARS`` of residue remains, reject. The
       us-v-schmitz indictment was the canonical case.

    The function was historically named ``looks_garbled`` and returned
    True for unusable text. Inverted so callers read positively
    (``if is_usable_text(text): return text`` reads more naturally than
    the old ``len(text) >= _MIN_USEFUL_CHARS and not looks_garbled(text)``
    pairing) and so the name reflects every check, not just the original
    garbled-detection one.
    """
    if len(text) < _MIN_USEFUL_CHARS:
        return False
    nonws = "".join(text.split())
    if nonws:
        alpha = sum(1 for c in nonws if c.isascii() and c.isalpha())
        if (alpha / len(nonws)) < _MIN_ALPHA_RATIO:
            return False
    residue = _PACER_PAGE_HEADER_RE.sub("", text).strip()
    if len(residue) < _MIN_USEFUL_CHARS:
        return False
    return True


def _http_status_category(status: int) -> str:
    """One-word classification of an HTTP status code for log clarity.

    Operators triaging a flood of pdf-fetch warnings need to know at a
    glance whether the failure is transient (retry next sync will
    likely succeed) or permanent (something needs to change upstream).
    Worth a small helper rather than embedding the same conditional in
    every log site.
    """
    if status == 404 or status == 410:
        return "not found"
    if status in (401, 403):
        return "access denied"
    if status == 429:
        return "rate limited"
    if 400 <= status < 500:
        return "client error — won't retry"
    if 500 <= status < 600:
        return "server error — retry next sync"
    return "unexpected"


def _get_with_retry(client: httpx.Client, url: str) -> Optional[httpx.Response]:
    """GET with retry on transient transport errors and retryable status codes.

    Returns the final response (which may still be non-200 if all attempts
    landed on a retryable error code), or ``None`` if every attempt raised
    a transport error. Callers treat ``None`` and non-200 the same way —
    fall through to the next URL or give up — so the distinction is only
    in the log.
    """
    backoff = _PDF_RETRY_INITIAL_BACKOFF
    last_response: Optional[httpx.Response] = None
    # `while True` instead of `for attempt in range(...)` so every exit is
    # an explicit `return` — no loop-fall-off path for coverage to flag as
    # an unreachable branch.
    attempt = 1
    while True:
        try:
            r = client.get(url)
        except _PDF_RETRYABLE_EXCEPTIONS as e:
            if attempt >= _PDF_RETRY_TOTAL:
                log.warning(
                    "pdf fetch transport error budget exhausted for %s: %s",
                    url,
                    e,
                )
                return last_response
            log.info(
                "pdf fetch transport error (attempt %d/%d) for %s: %s; retrying in %.1fs",
                attempt,
                _PDF_RETRY_TOTAL,
                url,
                e,
                backoff,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 16)
            attempt += 1
            continue
        last_response = r
        if r.status_code not in _PDF_RETRYABLE_STATUSES:
            return r
        if attempt >= _PDF_RETRY_TOTAL:
            return r
        log.info(
            "pdf fetch %s -> %s (attempt %d/%d); retrying in %.1fs",
            url,
            r.status_code,
            attempt,
            _PDF_RETRY_TOTAL,
            backoff,
        )
        time.sleep(backoff)
        backoff = min(backoff * 2, 16)
        attempt += 1


def fetch_pdf_bytes(rd: dict, *, timeout: float = 30.0) -> Optional[bytes]:
    """Try to download the PDF for a recap_document. Returns None if no source.

    We avoid CourtListener's storage URL (which can require auth or rate
    limits) and prefer the Internet Archive mirror, then fall back to
    constructing a CourtListener storage URL from ``filepath_local``.
    """
    urls: list[str] = []
    ia = rd.get("filepath_ia")
    if ia:
        urls.append(ia)
    fp_local = rd.get("filepath_local")
    if fp_local:
        urls.append(f"https://storage.courtlistener.com/{fp_local}")

    for url in urls:
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                r = _get_with_retry(client, url)
                if r is None:
                    continue
                if r.status_code == 200 and r.content:
                    return r.content
                log.warning(
                    "pdf fetch %s -> %s (%s)",
                    url,
                    r.status_code,
                    _http_status_category(r.status_code),
                )
        except Exception as e:
            log.warning("pdf fetch %s failed: %s: %s", url, type(e).__name__, e)
    return None


def extract_with_pypdf(pdf_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        log.warning("pypdf not installed; skipping embedded-text extraction")
        return ""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        parts: list[str] = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(parts).strip()
    except Exception as e:
        log.warning("pypdf failed: %s", e)
        return ""


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def ocr_with_tesseract(pdf_bytes: bytes) -> str:
    """OCR a PDF using poppler's pdftoppm + tesseract. Returns "" if unavailable."""
    if not (_have("pdftoppm") and _have("tesseract")):
        log.info("ocr tools not installed (need pdftoppm + tesseract); skipping OCR")
        return ""

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        pdf_path = tmp_path / "in.pdf"
        pdf_path.write_bytes(pdf_bytes)

        # Render each page to a 300-DPI grayscale PNG.
        try:
            subprocess.run(
                [
                    "pdftoppm",
                    "-r",
                    "300",
                    "-gray",
                    "-png",
                    str(pdf_path),
                    str(tmp_path / "page"),
                ],
                check=True,
                capture_output=True,
                timeout=300,
            )
        except subprocess.SubprocessError as e:
            log.warning("pdftoppm failed: %s", e)
            return ""

        text_parts: list[str] = []
        for png in sorted(tmp_path.glob("page-*.png")):
            try:
                r = subprocess.run(
                    ["tesseract", str(png), "-", "-l", "eng"],
                    check=True,
                    capture_output=True,
                    timeout=120,
                )
                text_parts.append(r.stdout.decode("utf-8", errors="ignore"))
            except subprocess.SubprocessError as e:
                log.warning("tesseract failed on %s: %s", png.name, e)
                continue
        return "\n".join(text_parts).strip()


def fetch_url_bytes(url: str, *, timeout: float = 60.0) -> Optional[bytes]:
    """Fetch arbitrary bytes from an absolute URL.

    Used by the case-summary pipeline for operator-provided documents
    (``extra_documents`` in config) — sources outside CourtListener/PACER such as DoJ
    press release attachments that work around CourtListener data gaps.
    Returns ``None`` on any non-200 / network error so callers can fall
    open the same way they do on a missing recap_document.
    """
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            r = _get_with_retry(client, url)
            if r is None:
                return None
            if r.status_code == 200 and r.content:
                return r.content
            log.warning(
                "url fetch %s -> %s (%s)",
                url,
                r.status_code,
                _http_status_category(r.status_code),
            )
    except Exception as e:
        log.warning("url fetch %s failed: %s: %s", url, type(e).__name__, e)
    return None


def extract_text_from_url(url: str, *, allow_ocr: bool = True) -> Optional[str]:
    """Fetch a PDF by URL and run the pypdf → OCR fallback chain on it.

    The operator-supplied analogue of :func:`extract_text` — same extraction
    pipeline, different fetch source. Returns ``None`` if the URL is
    unreachable or every extraction path yields nothing usable. Logs
    distinct messages for the two failure modes (fetch failed vs.
    fetched-but-pipeline-failed) so operators can tell from the log
    whether to retry the URL (transient fetch) or revisit the document
    itself (image-only without OCR tools, corrupted, etc.).
    """
    pdf_bytes = fetch_url_bytes(url)
    if not pdf_bytes:
        log.info(
            "extract_text_from_url: %s — fetch produced no bytes (see "
            "per-URL fetch warning above for the status code / error)",
            url,
        )
        return None
    text = extract_with_pypdf(pdf_bytes)
    if is_usable_text(text):
        return text
    if allow_ocr:
        ocr = ocr_with_tesseract(pdf_bytes)
        if is_usable_text(ocr):
            return ocr
    log.warning(
        "extract_text_from_url: %s — fetched %d bytes but could not "
        "extract usable text (pypdf produced %d chars %s, OCR %s)",
        url,
        len(pdf_bytes),
        len(text),
        "(rejected by is_usable_text)" if text else "(empty)",
        "ran but rejected" if allow_ocr else "disabled by caller",
    )
    return text or None


def extract_text(rd: dict, *, allow_ocr: bool = True) -> Optional[str]:
    """Extract text from a recap_document, handling all the gap cases.

    Order of operations:
      1. Use ``plain_text`` from CourtListener if ``is_usable_text``
         passes. CourtListener does not OCR documents — their
         ``plain_text`` is whatever their upstream pypdf pass
         produced, so it inherits all the same failure modes our
         local pypdf would produce on the same PDF:
         font-encoding gibberish (custom font subsets with no
         ``/ToUnicode`` map), PACER-stamps-only output (image-only
         scan with a thin OCR overlay covering just the page-header
         band), or empty / too-short text. ``is_usable_text`` bundles
         length + font-encoding-ratio + stamp-strip detection in one
         check, so all of those upstream pypdf failures fail this
         gate the same way and we fall through to our local
         extraction (where the OCR fallback can actually recover the
         image-only cases).
      2. If the PDF is sealed per the cache, return None — PACER
         doesn't expose sealed documents over the public API, so
         attempting a fetch would just fail. The seal is not necessarily
         permanent: if the document is unsealed later, the recap_doc's
         ``is_sealed`` flag flips False upstream, our entry fingerprint
         changes (``is_sealed`` is included), the next sync's
         ``process_entry`` re-runs and refreshes the cached state, and
         this gate stops firing — at which point the normal fetch +
         pypdf + OCR chain runs.
      3. Otherwise ALWAYS try the local pipeline: ``fetch_pdf_bytes``
         decides whether the document is fetchable from the URL fields
         (``filepath_ia`` / ``filepath_local``) — when both are empty
         it returns ``None`` cleanly without any HTTP round-trip — then
         pypdf (gated by ``is_usable_text``), then optionally tesseract
         OCR (also gated by ``is_usable_text``).

    ``is_available`` is intentionally NOT a gate. The us-v-lytvynenko
    regression is the canonical example of why: the cached recap_doc
    had ``is_available=False`` (correct at the time we synced this
    entry — the PDF wasn't on RECAP yet), but upstream had since
    flipped the flag to ``True`` AND populated the URL fields. Gating
    on the stale cached flag meant we never even attempted the fetch,
    so pypdf and OCR never ran on a document we could perfectly well
    have read. When the cache's URL fields really are empty
    ``fetch_pdf_bytes`` returns ``None`` after just looking at the
    dict, so the cost of removing the gate is bounded — when the
    underlying storage truly has nothing, we behave the same way we
    used to but with one extra in-process function call. When the
    storage state has drifted ahead of the ``is_available`` flag
    (which CourtListener can exhibit between an upstream RECAP upload
    and the next flag flip), we successfully recover instead of
    bailing.
    """
    rd_id = rd.get("id")
    plain = (rd.get("plain_text") or "").strip()
    if is_usable_text(plain):
        return plain
    if plain:
        log.info(
            "extract_text: rd %s — CourtListener plain_text not usable "
            "(len=%d), falling through to local extraction",
            rd_id,
            len(plain),
        )

    if rd.get("is_sealed"):
        # Currently sealed per the cache. PACER doesn't expose sealed
        # documents over the public API, so a fetch would just fail —
        # we skip it. The seal isn't permanent though: if the document
        # is unsealed later (common in extradition cases after the
        # defendant is arrested or appears, or after a stipulation to
        # unseal), CourtListener flips is_sealed back to False on the
        # recap_document. Our entry fingerprint includes is_sealed, so
        # the flip changes the fingerprint, the next sync's process_entry
        # path re-runs, and the cache picks up the unsealed state
        # automatically — at which point this gate stops firing and the
        # fetch + pypdf + OCR chain runs normally.
        log.info(
            "extract_text: rd %s — currently sealed; skipping fetch "
            "until upstream unseals (cache fingerprint will flip)",
            rd_id,
        )
        return None

    pdf_bytes = fetch_pdf_bytes(rd)
    if not pdf_bytes:
        # We never got bytes — distinguish the two subcases for the log.
        # The "no URL" case is normal for entries CourtListener hasn't
        # uploaded to RECAP yet (paperless orders, recently-filed docs
        # awaiting RECAP capture); the "fetch failed" case is unusual
        # and worth a warning so operators notice flaky RECAP / IA. Both
        # paths return the (unusable) plain text if any so callers see
        # the entry was attempted, otherwise None.
        if not (rd.get("filepath_ia") or rd.get("filepath_local")):
            log.info(
                "extract_text: rd %s — document is not available "
                "(no filepath_ia or filepath_local on the recap_document; "
                "not on RECAP yet)",
                rd_id,
            )
        else:
            log.warning(
                "extract_text: rd %s — fetch failed across all URLs "
                "(filepath_ia=%r, filepath_local=%r); see per-URL pdf "
                "fetch warnings above for the status codes / errors",
                rd_id,
                rd.get("filepath_ia"),
                rd.get("filepath_local"),
            )
        return plain or None

    # We downloaded the PDF. From this point on the result MUST come
    # from our own pipeline (pypdf → OCR) — never fall back to
    # CourtListener's ``plain_text``. CourtListener's plain_text on
    # this same PDF came from the same upstream pypdf pass that we're
    # about to run locally; if our local extraction reveals the PDF is
    # image-only (pypdf yields PACER stamps with no body) or font-
    # encoding-garbled, the upstream plain_text was lying with that
    # exact same noise. Returning it here would re-inject the garbage
    # we already rejected at the top of this function and feed the
    # summary LLM a worse-than-OCR version of the same document.
    text = extract_with_pypdf(pdf_bytes)
    if is_usable_text(text):
        return text

    if allow_ocr:
        ocr = ocr_with_tesseract(pdf_bytes)
        if is_usable_text(ocr):
            return ocr

    # Neither pypdf nor OCR produced usable text. Distinguish in the
    # log: this is the genuine "could not extract text" case, not the
    # "not available" or "sealed" cases — we DID get the PDF bytes,
    # and ran our full pipeline on them, and neither pypdf (text-layer
    # extraction) nor tesseract (image OCR) produced anything that
    # cleared ``is_usable_text``. This is the operator's signal to
    # check that poppler / tesseract are installed (or that the PDF
    # itself isn't legitimately a non-textual artifact).
    log.warning(
        "extract_text: rd %s — fetched %d bytes but could not extract "
        "usable text (pypdf produced %d chars %s, OCR %s)",
        rd_id,
        len(pdf_bytes),
        len(text),
        "(rejected by is_usable_text)" if text else "(empty)",
        "ran but rejected" if allow_ocr else "disabled by caller",
    )
    # Return whatever OUR pipeline got (pypdf's output — may be page
    # stamps only) so the caller still has something attributable to
    # our local extraction; do NOT fall back to CL's ``plain_text``.
    return text or None
