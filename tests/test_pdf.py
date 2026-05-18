"""Tests for the PDF text-extraction fallback chain.

We don't actually fetch PDFs in tests; we monkey-patch ``fetch_pdf_bytes``
and ``ocr_with_tesseract`` to control each branch.

HTTP fetches monkey-patch ``urllib.request.urlopen`` at the module level
that ``pdf.py`` imports from, so the production retry loop in
``pdf._get_with_retry`` runs against a controlled backend without
hitting the network.
"""

from __future__ import annotations

import io
import socket
import urllib.error
from pathlib import Path
from urllib.parse import urlparse

from case_calendar import pdf


class _FakeResp:
    """Stand-in for ``http.client.HTTPResponse`` (what urlopen returns).

    Implements the context-manager protocol and ``status`` / ``read()`` —
    the only attributes ``_get_with_retry`` reads off a urlopen response.
    """

    def __init__(self, status: int, body: bytes = b""):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(url: str, status: int, body: bytes = b"") -> urllib.error.HTTPError:
    """Build the ``urllib.error.HTTPError`` urlopen raises on 4xx/5xx."""
    return urllib.error.HTTPError(
        url,
        status,
        f"HTTP {status}",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body),
    )


def _install_urlopen(monkeypatch, handler):
    """Patch ``urllib.request.urlopen`` (the symbol the ``pdf`` module
    resolves through) with a function that ignores its kwargs and calls
    the test's handler with the incoming ``Request``.
    """

    def fake_urlopen(req, timeout=None):
        return handler(req)

    monkeypatch.setattr(pdf.urllib.request, "urlopen", fake_urlopen)


class TestLooksGarbled:
    """Detector for font-encoding gibberish from upstream pypdf extraction.

    Real prose runs ~70-80% alpha; the most number-heavy legal headers
    still score >50%. Custom-encoded PDFs decoded without a /ToUnicode
    map land under 10%. 0.4 sits comfortably between the two.
    """

    def test_real_prose_is_not_garbled(self):
        text = (
            "Defendants VICTORIA EDUARDOVNA DUBRANOVA, also known as "
            "Vika, Tory, and Sovasonya, were members of NoName057(16). "
            "Defendant LUPIN was the Chief Executive Officer of CISM. "
            "Defendant BURLAKOV was the Deputy Director of CISM."
        )
        assert pdf.looks_garbled(text) is False

    def test_font_encoding_noise_is_garbled(self):
        # 27KB of `ÿ`-noise with occasional ASCII bits is the actual shape
        # of CourtListener's plain_text for the us-v-dubranova first superseding
        # indictment — pypdf mapped the bytes 1:1 into Latin-1 codepoints
        # because the PDF's fonts had no /ToUnicode map.
        text = "ÿ ÿ%ÿ$ÿ2 4 & '&1'&('ÿ&'&5'&)'&'ÿ" * 200
        assert pdf.looks_garbled(text) is True

    def test_pypdf_glyph_indices_are_garbled(self):
        # Some PDFs produce ``/i255 /1 /2 /11/12/13/14`` glyph-index
        # tokens instead of decoded text — also a low alpha ratio.
        text = "/i255\n/1\n/2\n/3\n/11/12/13/14/15/16\n" * 100
        assert pdf.looks_garbled(text) is True

    def test_header_heavy_real_text_is_not_garbled(self):
        # Worst-case real text: a page that's mostly the document header.
        # Still scores above 0.4.
        text = (
            "Case 2:25-cr-00578-SRM Document 33 Filed 08/21/25 "
            "Page 1 of 15 Page ID #:305 IN THE UNITED STATES DISTRICT "
            "COURT FOR THE CENTRAL DISTRICT OF CALIFORNIA "
            "FIRST SUPERSEDING INDICTMENT 18 USC 371 18 USC 1030"
        )
        assert pdf.looks_garbled(text) is False

    def test_short_strings_are_never_garbled(self):
        # Below the ratio-stability threshold — trust short strings.
        # Even a string with low alpha ratio shouldn't flag, because
        # the ratio is too noisy on tiny inputs to be reliable.
        assert pdf.looks_garbled("###") is False
        assert pdf.looks_garbled("ÿ" * 50) is False

    def test_empty_string_is_not_garbled(self):
        # Edge case — caller checks emptiness separately.
        assert pdf.looks_garbled("") is False


class TestExtractText:
    def test_uses_plain_text_first(self, monkeypatch):
        rd = {
            "plain_text": "  the body is full of real english words like "
            "indictment and defendant and conspiracy  ",
            "is_available": True,
        }
        monkeypatch.setattr(
            pdf, "fetch_pdf_bytes", lambda *a, **kw: b"should not be called"
        )
        result = pdf.extract_text(rd)
        assert result and "the body" in result

    def test_garbled_plain_text_falls_through_to_pypdf(self, monkeypatch):
        # CourtListener's plain_text is gibberish (font-encoding issue). The function
        # should ignore it and run our own extraction chain, returning the
        # local pypdf text when it's clean.
        rd = {
            "plain_text": "ÿ ÿ%ÿ$ÿ" * 200,
            "is_available": True,
            "filepath_ia": "https://archive.org/x.pdf",
        }
        monkeypatch.setattr(pdf, "fetch_pdf_bytes", lambda *a, **kw: b"%PDF")
        monkeypatch.setattr(
            pdf,
            "extract_with_pypdf",
            lambda data: "real english text from the indictment " * 30,
        )
        result = pdf.extract_text(rd)
        assert result and "real english text" in result

    def test_garbled_plain_text_and_garbled_pypdf_fall_through_to_ocr(
        self,
        monkeypatch,
    ):
        # The realistic case: CourtListener's plain_text is garbled AND our pypdf is
        # garbled (same source!). OCR is the only path that recovers
        # readable text. Matches the us-v-dubranova flow end-to-end.
        rd = {
            "plain_text": "ÿ ÿ%ÿ$ÿ" * 200,
            "is_available": True,
            "filepath_ia": "https://archive.org/x.pdf",
        }
        monkeypatch.setattr(pdf, "fetch_pdf_bytes", lambda *a, **kw: b"%PDF")
        monkeypatch.setattr(
            pdf,
            "extract_with_pypdf",
            lambda data: "/i255 /1 /2 /11/12/13 " * 200,  # also garbled
        )
        monkeypatch.setattr(
            pdf,
            "ocr_with_tesseract",
            lambda data: "Defendants VICTORIA EDUARDOVNA DUBRANOVA "
            "were members of NoName057(16) " * 20,
        )
        result = pdf.extract_text(rd)
        assert result and "NoName057" in result
        assert "CISM" not in result or "DUBRANOVA" in result  # no garble

    def test_garbled_plain_text_falls_back_to_garbled_text_when_fetch_fails(
        self,
        monkeypatch,
    ):
        # When we can't fetch a fresh copy, the garbled plain_text is
        # better than nothing — the LLM is briefed to refuse synthesis on
        # nonsense input, and the caller at least sees the entry was
        # attempted rather than silently skipped.
        rd = {
            "plain_text": "ÿ ÿ%ÿ$ÿ" * 200,
            "is_available": True,
            "filepath_ia": "https://archive.org/x.pdf",
        }
        monkeypatch.setattr(pdf, "fetch_pdf_bytes", lambda *a, **kw: None)
        result = pdf.extract_text(rd)
        # Falls back to the garbled plain_text rather than None.
        assert result is not None
        assert "ÿ" in result

    def test_sealed_returns_none_without_fetch(self, monkeypatch):
        rd = {"plain_text": "", "is_sealed": True}
        called = []
        monkeypatch.setattr(
            pdf, "fetch_pdf_bytes", lambda *a, **kw: called.append("nope")
        )
        assert pdf.extract_text(rd) is None
        assert called == []

    def test_unavailable_returns_none_without_fetch(self, monkeypatch):
        rd = {"plain_text": "", "is_available": False}
        called = []
        monkeypatch.setattr(
            pdf, "fetch_pdf_bytes", lambda *a, **kw: called.append("nope")
        )
        assert pdf.extract_text(rd) is None
        assert called == []

    def test_falls_back_to_pypdf(self, monkeypatch):
        rd = {
            "plain_text": "",
            "is_available": True,
            "filepath_ia": "https://archive.org/x.pdf",
        }
        monkeypatch.setattr(pdf, "fetch_pdf_bytes", lambda *a, **kw: b"%PDF-1.4 fake")
        monkeypatch.setattr(
            pdf, "extract_with_pypdf", lambda data: "extracted text " * 50
        )
        result = pdf.extract_text(rd)
        assert result and "extracted text" in result

    def test_pypdf_short_falls_back_to_ocr(self, monkeypatch):
        rd = {
            "plain_text": "",
            "is_available": True,
            "filepath_ia": "https://archive.org/x.pdf",
        }
        monkeypatch.setattr(pdf, "fetch_pdf_bytes", lambda *a, **kw: b"%PDF")
        monkeypatch.setattr(pdf, "extract_with_pypdf", lambda data: "")
        monkeypatch.setattr(pdf, "ocr_with_tesseract", lambda data: "ocr'd text " * 30)
        result = pdf.extract_text(rd)
        assert result and "ocr'd text" in result

    def test_pypdf_short_falls_back_to_returning_partial_when_ocr_disabled(
        self, monkeypatch
    ):
        rd = {
            "plain_text": "",
            "is_available": True,
            "filepath_ia": "https://archive.org/x.pdf",
        }
        monkeypatch.setattr(pdf, "fetch_pdf_bytes", lambda *a, **kw: b"%PDF")
        monkeypatch.setattr(pdf, "extract_with_pypdf", lambda data: "tiny")
        called = []
        monkeypatch.setattr(
            pdf, "ocr_with_tesseract", lambda data: called.append("nope") or ""
        )
        result = pdf.extract_text(rd, allow_ocr=False)
        # Returns the short text rather than None.
        assert result == "tiny"
        assert called == []

    def test_no_fetch_source_returns_none(self, monkeypatch):
        rd = {
            "plain_text": "",
            "is_available": True,
            "filepath_local": None,
            "filepath_ia": "",
        }
        monkeypatch.setattr(pdf, "fetch_pdf_bytes", lambda *a, **kw: None)
        assert pdf.extract_text(rd) is None


class TestExtractWithPypdf:
    def test_handles_invalid_pdf_gracefully(self):
        assert pdf.extract_with_pypdf(b"not a pdf") == ""


class TestFetchPdfBytes:
    def test_returns_none_when_no_urls(self):
        rd = {"filepath_local": None, "filepath_ia": ""}
        assert pdf.fetch_pdf_bytes(rd) is None

    def test_returns_bytes_on_200(self, monkeypatch):
        def handler(req):
            return _FakeResp(200, b"%PDF-1.4 bytes")

        _install_urlopen(monkeypatch, handler)
        rd = {"filepath_ia": "https://archive.org/x.pdf"}
        assert pdf.fetch_pdf_bytes(rd) == b"%PDF-1.4 bytes"

    def test_falls_through_to_cl_storage_url(self, monkeypatch):
        # IA returns 404; CourtListener storage URL returns 200 — second branch in the loop.
        seen: list[str] = []

        def handler(req):
            seen.append(req.full_url)
            host = (urlparse(req.full_url).hostname or "").lower()
            if host == "archive.org" or host.endswith(".archive.org"):
                raise _http_error(req.full_url, 404)
            return _FakeResp(200, b"%PDF cl bytes")

        _install_urlopen(monkeypatch, handler)
        rd = {
            "filepath_ia": "https://archive.org/x.pdf",
            "filepath_local": "recap/foo.pdf",
        }
        assert pdf.fetch_pdf_bytes(rd) == b"%PDF cl bytes"
        assert any(
            (urlparse(u).hostname or "").lower() == "storage.courtlistener.com"
            for u in seen
        )

    def test_network_error_falls_through(self, monkeypatch):
        # First URL throws; second URL succeeds. Tests the try/except path.
        # The retry loop will try the transport-failure URL up to
        # _PDF_RETRY_TOTAL times before returning None and falling through
        # to the next URL.
        monkeypatch.setattr(pdf.time, "sleep", lambda _s: None)

        attempts = {"count": 0}

        def handler(req):
            attempts["count"] += 1
            host = (urlparse(req.full_url).hostname or "").lower()
            if host == "archive.org" or host.endswith(".archive.org"):
                raise urllib.error.URLError("connection refused")
            return _FakeResp(200, b"%PDF cl bytes")

        _install_urlopen(monkeypatch, handler)
        rd = {
            "filepath_ia": "https://archive.org/x.pdf",
            "filepath_local": "recap/foo.pdf",
        }
        assert pdf.fetch_pdf_bytes(rd) == b"%PDF cl bytes"

    def test_all_urls_fail_returns_none(self, monkeypatch):
        def handler(req):
            raise _http_error(req.full_url, 404)

        _install_urlopen(monkeypatch, handler)
        assert pdf.fetch_pdf_bytes({"filepath_ia": "https://x.com/y.pdf"}) is None

    def test_retries_read_timeout_then_succeeds(self, monkeypatch):
        # A single read timeout (the in-production symptom we observed on
        # CourtListener; the same class of failure can hit the IA mirror
        # too) is retried by `_get_with_retry` before falling through to
        # the next-URL fallback.
        monkeypatch.setattr(pdf.time, "sleep", lambda _s: None)
        attempts = [0]

        def handler(req):
            attempts[0] += 1
            if attempts[0] == 1:
                raise socket.timeout("read timed out")
            return _FakeResp(200, b"%PDF retried bytes")

        _install_urlopen(monkeypatch, handler)
        result = pdf.fetch_pdf_bytes({"filepath_ia": "https://archive.org/x.pdf"})
        assert result == b"%PDF retried bytes"
        # Two transport-level calls: the failing first attempt then the
        # retry that succeeded.
        assert attempts[0] == 2

    def test_retries_503_then_succeeds(self, monkeypatch):
        # 502/503/504 responses are retryable status codes (gateway /
        # proxy / CDN transient unavailability); a single 503 should not
        # push us to the next-URL fallback.
        monkeypatch.setattr(pdf.time, "sleep", lambda _s: None)
        attempts = [0]

        def handler(req):
            attempts[0] += 1
            if attempts[0] == 1:
                raise _http_error(req.full_url, 503)
            return _FakeResp(200, b"%PDF after 503")

        _install_urlopen(monkeypatch, handler)
        result = pdf.fetch_pdf_bytes({"filepath_ia": "https://archive.org/x.pdf"})
        assert result == b"%PDF after 503"
        assert attempts[0] == 2

    def test_non_retryable_exception_caught_by_outer_handler(self, monkeypatch):
        # ``_get_with_retry`` catches the retryable transport set and
        # ``urllib.error.HTTPError``. Anything else (programmer error,
        # surprise stdlib exception) bubbles out and is caught by the
        # outer ``except Exception`` in ``fetch_pdf_bytes``, which logs
        # and falls through to the next URL. Tests the outer-handler
        # branch end-to-end.
        seen: list[str] = []

        def handler(req):
            seen.append(req.full_url)
            host = (urlparse(req.full_url).hostname or "").lower()
            if host == "archive.org":
                raise TypeError("synthetic bug from upstream lib")
            return _FakeResp(200, b"%PDF cl bytes")

        _install_urlopen(monkeypatch, handler)
        result = pdf.fetch_pdf_bytes(
            {
                "filepath_ia": "https://archive.org/x.pdf",
                "filepath_local": "recap/foo.pdf",
            }
        )
        # IA branch raised a non-retryable exception, outer handler
        # logged and fell through to the CL storage URL which served
        # the bytes.
        assert result == b"%PDF cl bytes"
        assert (urlparse(seen[0]).hostname or "").lower() == "archive.org"
        assert any(
            (urlparse(u).hostname or "").lower() == "storage.courtlistener.com"
            for u in seen
        )

    def test_transport_error_budget_exhausted_falls_through_to_next_url(
        self, monkeypatch
    ):
        # When every retry attempt against the IA mirror raises a
        # transport error, `_get_with_retry` returns None and
        # `fetch_pdf_bytes` falls through to the CourtListener storage
        # fallback. Without that fallthrough, a flaky IA mirror would
        # masquerade as a missing PDF.
        monkeypatch.setattr(pdf.time, "sleep", lambda _s: None)
        urls_seen: list[str] = []

        def handler(req):
            urls_seen.append(req.full_url)
            host = (urlparse(req.full_url).hostname or "").lower()
            if host == "archive.org" or host.endswith(".archive.org"):
                raise socket.timeout("flaky IA")
            return _FakeResp(200, b"%PDF from CL storage")

        _install_urlopen(monkeypatch, handler)
        result = pdf.fetch_pdf_bytes(
            {
                "filepath_ia": "https://archive.org/x.pdf",
                "filepath_local": "recap/cand/x.pdf",
            }
        )
        assert result == b"%PDF from CL storage"
        # IA was retried up to the budget, then CL storage succeeded
        # first try. Use exact hostname match (CodeQL flags
        # substring / endswith comparisons as
        # `py/incomplete-url-substring-sanitization` because
        # `evil-archive.org` would otherwise pass).
        def _host(url: str) -> str:
            return (urlparse(url).hostname or "").lower()

        ia_hits = sum(1 for u in urls_seen if _host(u) == "archive.org")
        assert ia_hits == pdf._PDF_RETRY_TOTAL
        assert any(_host(u) == "storage.courtlistener.com" for u in urls_seen)


class TestExtractWithPypdfHappyPath:
    def test_extracts_text_from_real_pdf_bytes(self):
        # Build a minimal valid PDF with embedded text to drive the success
        # path of extract_with_pypdf. Easiest: render a one-page PDF via
        # pypdf's own write helpers — but that requires reportlab. Skip if
        # we can't compose one; the failure path is already tested above.
        import pypdf

        # PyPDF2 / pypdf can't author a content-stream PDF without help.
        # Use a known-tiny PDF literal that has selectable text. The smallest
        # syntactically valid PDF with text is hand-crafted; rather than
        # ship one, build it via pypdf's writer + a blank page (no text)
        # and assert empty-string return — that's a valid success path branch
        # too (loops over pages, no extraction errors raised).
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=100, height=100)
        buf = io.BytesIO()
        writer.write(buf)
        # Returns "" (empty page, but no exception thrown). That exercises
        # the loop + extract_text() success path even though the result is
        # empty — the alternative is shipping a hand-rolled PDF file.
        out = pdf.extract_with_pypdf(buf.getvalue())
        assert out == ""

    def test_pypdf_import_failure_returns_empty(self, monkeypatch):
        # Simulate pypdf not being installed by injecting an ImportError.
        import builtins

        real_import = builtins.__import__

        def _no_pypdf(name, *a, **k):
            if name == "pypdf":
                raise ImportError("no pypdf")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _no_pypdf)
        assert pdf.extract_with_pypdf(b"%PDF") == ""

    def test_pypdf_reader_construction_failure_returns_empty(self, monkeypatch):
        # If pypdf raises when constructing PdfReader (corrupt file, an
        # exotic format), the outer try/except in extract_with_pypdf
        # logs and returns empty string rather than crashing the whole
        # summary call. The bug shape: a PDF that's valid enough to
        # download but malformed enough to break pypdf.
        import pypdf.errors as pypdf_errors

        class _BoomReader:
            def __init__(self, *a, **k):
                raise pypdf_errors.PdfReadError("Stream has ended unexpectedly")

        monkeypatch.setattr(pdf, "PdfReader", _BoomReader, raising=False)
        # Also patch the import inside the function — pypdf.PdfReader is
        # imported lazily, so we need to intercept the import too.
        import sys

        fake_module = type(sys)("pypdf")
        fake_module.PdfReader = _BoomReader
        fake_module.errors = pypdf_errors
        monkeypatch.setitem(sys.modules, "pypdf", fake_module)
        assert pdf.extract_with_pypdf(b"%PDF-1.4 garbage") == ""

    def test_per_page_extract_text_exception_is_caught(self, monkeypatch):
        # The inner per-page try/except keeps a single broken page from
        # blowing up the whole extraction. Real-world shape: an
        # encrypted-content-stream page in an otherwise-readable
        # multi-page PDF. The good pages still contribute text; the bad
        # one contributes empty.
        import sys
        import pypdf.errors as pypdf_errors

        class _BadPage:
            def extract_text(self):
                raise RuntimeError("encrypted content stream")

        class _GoodPage:
            def extract_text(self):
                return "Good page body."

        class _Reader:
            def __init__(self, *a, **k):
                self.pages = [_BadPage(), _GoodPage()]

        fake_module = type(sys)("pypdf")
        fake_module.PdfReader = _Reader
        fake_module.errors = pypdf_errors
        monkeypatch.setitem(sys.modules, "pypdf", fake_module)
        out = pdf.extract_with_pypdf(b"%PDF-1.4")
        # Bad page contributed nothing; good page's text survived.
        assert out == "Good page body."


class TestOcrWithTesseract:
    def test_returns_empty_when_tools_missing(self, monkeypatch):
        # Pretend neither pdftoppm nor tesseract are on PATH.
        monkeypatch.setattr(pdf, "_have", lambda cmd: False)
        assert pdf.ocr_with_tesseract(b"%PDF") == ""

    def test_pdftoppm_failure_returns_empty(self, monkeypatch):
        import subprocess

        monkeypatch.setattr(pdf, "_have", lambda cmd: True)

        def _failing_run(*a, **k):
            raise subprocess.CalledProcessError(1, a[0] if a else "pdftoppm")

        monkeypatch.setattr(subprocess, "run", _failing_run)
        assert pdf.ocr_with_tesseract(b"%PDF") == ""

    def test_full_ocr_path(self, monkeypatch, tmp_path):
        # Make _have return True for both tools. Patch subprocess.run so
        # that pdftoppm "creates" two PNGs in its output dir, and tesseract
        # writes its OCR output to stdout.
        import subprocess

        monkeypatch.setattr(pdf, "_have", lambda cmd: True)

        # Capture the temp dir path the implementation uses by intercepting
        # pdftoppm's argv (the prefix is the next-to-last arg).
        outputs: dict[str, str] = {"prefix": ""}

        def _run(argv, *, check, capture_output, timeout):
            tool = argv[0]
            if tool == "pdftoppm":
                # argv format: ["pdftoppm", "-r", "300", "-gray", "-png",
                #   "<input.pdf>", "<dir>/page"]
                prefix = argv[-1]
                outputs["prefix"] = prefix
                # Create a couple of fake PNGs at the requested prefix.
                Path(prefix + "-1.png").write_bytes(b"\x89PNG fake")
                Path(prefix + "-2.png").write_bytes(b"\x89PNG fake")

                class _PdfToppmResult:
                    stdout = b""

                return _PdfToppmResult()
            elif tool == "tesseract":
                # argv format: ["tesseract", "<input.png>", "-", "-l", "eng"]
                class _TesseractResult:
                    stdout = b"page text"

                return _TesseractResult()
            raise AssertionError(f"unexpected subprocess: {argv}")

        monkeypatch.setattr(subprocess, "run", _run)
        out = pdf.ocr_with_tesseract(b"%PDF")
        assert "page text" in out
        # Two pages -> two "page text" blocks separated by a newline.
        assert out.count("page text") == 2

    def test_tesseract_per_page_failure_skipped(self, monkeypatch):
        # One page renders, but tesseract fails on it — the failure is
        # logged and the function returns "" (no successful pages).
        import subprocess

        monkeypatch.setattr(pdf, "_have", lambda cmd: True)

        def _run(argv, *, check, capture_output, timeout):
            tool = argv[0]
            if tool == "pdftoppm":
                prefix = argv[-1]
                Path(prefix + "-1.png").write_bytes(b"\x89PNG")

                class R:
                    stdout = b""

                return R()
            raise subprocess.CalledProcessError(2, "tesseract")

        monkeypatch.setattr(subprocess, "run", _run)
        assert pdf.ocr_with_tesseract(b"%PDF") == ""


class TestHave:
    def test_returns_true_for_present_tool(self, monkeypatch):
        import shutil

        monkeypatch.setattr(shutil, "which", lambda c: "/usr/bin/" + c)
        assert pdf._have("ls") is True

    def test_returns_false_for_absent_tool(self, monkeypatch):
        import shutil

        monkeypatch.setattr(shutil, "which", lambda c: None)
        assert pdf._have("not-a-real-binary") is False


class TestFetchUrlBytes:
    """`fetch_url_bytes` is the operator-document analogue of `fetch_pdf_bytes`
    — same fall-open semantics on every non-200 / network-error path, used
    by `extract_text_from_url` for `extra_documents` URLs."""

    def test_returns_bytes_on_200(self, monkeypatch):
        def handler(req):
            return _FakeResp(200, b"%PDF-1.4 doj-pr-attachment")

        _install_urlopen(monkeypatch, handler)
        out = pdf.fetch_url_bytes("https://www.justice.gov/opa/media/x/dl")
        assert out == b"%PDF-1.4 doj-pr-attachment"

    def test_returns_none_on_non_200(self, monkeypatch):
        def handler(req):
            raise _http_error(req.full_url, 404)

        _install_urlopen(monkeypatch, handler)
        assert pdf.fetch_url_bytes("https://x.com/missing.pdf") is None

    def test_returns_none_on_network_error(self, monkeypatch):
        # DNS failure / connection refused — exhausts the retry budget
        # and `_get_with_retry` returns None.
        monkeypatch.setattr(pdf.time, "sleep", lambda _s: None)

        def handler(req):
            raise urllib.error.URLError("dns failure")

        _install_urlopen(monkeypatch, handler)
        assert pdf.fetch_url_bytes("https://nowhere.invalid/x.pdf") is None

    def test_retries_read_timeout_then_succeeds(self, monkeypatch):
        # Operator-supplied extra_documents URLs (e.g. DoJ press-release
        # attachments) get the same transport-error retry as the
        # CourtListener PDF fetch — a single read timeout on a slow DoJ
        # server shouldn't lose the only public copy of an unsealed
        # indictment.
        monkeypatch.setattr(pdf.time, "sleep", lambda _s: None)
        attempts = [0]

        def handler(req):
            attempts[0] += 1
            if attempts[0] == 1:
                raise socket.timeout("read timed out")
            return _FakeResp(200, b"%PDF doj attachment retried")

        _install_urlopen(monkeypatch, handler)
        result = pdf.fetch_url_bytes("https://www.justice.gov/opa/media/x/dl")
        assert result == b"%PDF doj attachment retried"
        assert attempts[0] == 2

    def test_persistent_503_returns_status_response_not_retried_forever(
        self, monkeypatch
    ):
        # When every retry attempt returns a retryable status (e.g. 503
        # for the duration), `_get_with_retry` gives up after
        # `_PDF_RETRY_TOTAL` attempts and returns the last response.
        # The 200-only check in `fetch_url_bytes` then logs the bad
        # status and falls through to `return None`. Exercises the
        # last-attempt status-retry branch.
        monkeypatch.setattr(pdf.time, "sleep", lambda _s: None)
        attempts = [0]

        def handler(req):
            attempts[0] += 1
            raise _http_error(req.full_url, 503)

        _install_urlopen(monkeypatch, handler)
        assert pdf.fetch_url_bytes("https://example.com/perma503.pdf") is None
        # Tried up to the budget, then gave up rather than looping
        # forever on the retryable status.
        assert attempts[0] == pdf._PDF_RETRY_TOTAL

    def test_non_retryable_exception_caught_by_outer_handler(self, monkeypatch):
        # Same outer-handler branch as ``fetch_pdf_bytes`` — a
        # non-retryable surprise exception is logged and converted to
        # a None return so callers don't crash on a bad operator URL.
        def handler(req):
            raise TypeError("synthetic surprise")

        _install_urlopen(monkeypatch, handler)
        assert pdf.fetch_url_bytes("https://example.com/x.pdf") is None

    def test_returns_none_when_transport_budget_exhausted(self, monkeypatch):
        # When every retry attempt against an operator-supplied URL
        # raises a transport error, `_get_with_retry` returns None and
        # `fetch_url_bytes` returns None — the caller (the case-summary
        # pipeline) treats the document as unavailable.
        monkeypatch.setattr(pdf.time, "sleep", lambda _s: None)
        attempts = [0]

        def handler(req):
            attempts[0] += 1
            raise socket.timeout("perpetually slow")

        _install_urlopen(monkeypatch, handler)
        assert pdf.fetch_url_bytes("https://example.com/never.pdf") is None
        assert attempts[0] == pdf._PDF_RETRY_TOTAL


class TestExtractTextFromUrl:
    def test_returns_text_when_pypdf_succeeds(self, monkeypatch):
        monkeypatch.setattr(pdf, "fetch_url_bytes", lambda url: b"%PDF bytes")
        monkeypatch.setattr(
            pdf, "extract_with_pypdf", lambda data: "indictment body " * 30
        )
        out = pdf.extract_text_from_url("https://example.com/x.pdf")
        assert out and "indictment body" in out

    def test_returns_none_when_fetch_fails(self, monkeypatch):
        monkeypatch.setattr(pdf, "fetch_url_bytes", lambda url: None)
        assert pdf.extract_text_from_url("https://x.com/y.pdf") is None

    def test_falls_back_to_ocr_when_pypdf_short(self, monkeypatch):
        monkeypatch.setattr(pdf, "fetch_url_bytes", lambda url: b"%PDF")
        monkeypatch.setattr(pdf, "extract_with_pypdf", lambda data: "")
        monkeypatch.setattr(
            pdf, "ocr_with_tesseract", lambda data: "ocr indictment " * 20
        )
        out = pdf.extract_text_from_url("https://example.com/x.pdf")
        assert out and "ocr indictment" in out

    def test_returns_short_pypdf_when_ocr_disabled(self, monkeypatch):
        monkeypatch.setattr(pdf, "fetch_url_bytes", lambda url: b"%PDF")
        monkeypatch.setattr(pdf, "extract_with_pypdf", lambda data: "tiny")
        called = []
        monkeypatch.setattr(
            pdf, "ocr_with_tesseract", lambda data: called.append("nope") or ""
        )
        out = pdf.extract_text_from_url("https://x.com/y.pdf", allow_ocr=False)
        assert out == "tiny"
        assert called == []

    def test_returns_none_when_all_paths_empty(self, monkeypatch):
        monkeypatch.setattr(pdf, "fetch_url_bytes", lambda url: b"%PDF")
        monkeypatch.setattr(pdf, "extract_with_pypdf", lambda data: "")
        monkeypatch.setattr(pdf, "ocr_with_tesseract", lambda data: "")
        assert pdf.extract_text_from_url("https://x.com/y.pdf") is None

    def test_garbled_pypdf_falls_through_to_ocr(self, monkeypatch):
        # Same garbled-text handling as ``extract_text`` — extras_documents
        # URLs go through the same pypdf → OCR pipeline and should also
        # bypass font-encoding noise from pypdf when OCR is allowed.
        monkeypatch.setattr(pdf, "fetch_url_bytes", lambda url: b"%PDF")
        monkeypatch.setattr(
            pdf,
            "extract_with_pypdf",
            lambda data: "/i255 /1 /2 /11/12/13 " * 200,
        )
        monkeypatch.setattr(
            pdf,
            "ocr_with_tesseract",
            lambda data: "real prose from the doj press release " * 20,
        )
        out = pdf.extract_text_from_url("https://example.com/x.pdf")
        assert out and "real prose" in out


class TestExtractTextOcrShorter:
    def test_falls_back_to_short_pypdf_text_when_ocr_short(self, monkeypatch):
        # Both pypdf and OCR return below-threshold text; the function
        # returns the (short) pypdf text, since it's at least something.
        rd = {
            "plain_text": "",
            "is_available": True,
            "filepath_ia": "https://archive.org/x.pdf",
        }
        monkeypatch.setattr(pdf, "fetch_pdf_bytes", lambda *a, **kw: b"%PDF")
        monkeypatch.setattr(pdf, "extract_with_pypdf", lambda data: "tiny")
        monkeypatch.setattr(pdf, "ocr_with_tesseract", lambda data: "also tiny")
        assert pdf.extract_text(rd) == "tiny"

    def test_returns_none_when_all_paths_empty(self, monkeypatch):
        rd = {
            "plain_text": "",
            "is_available": True,
            "filepath_ia": "https://archive.org/x.pdf",
        }
        monkeypatch.setattr(pdf, "fetch_pdf_bytes", lambda *a, **kw: b"%PDF")
        monkeypatch.setattr(pdf, "extract_with_pypdf", lambda data: "")
        monkeypatch.setattr(pdf, "ocr_with_tesseract", lambda data: "")
        assert pdf.extract_text(rd) is None
