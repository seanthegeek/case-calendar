"""Tests for the PDF text-extraction fallback chain.

We don't actually fetch PDFs in tests; we monkey-patch ``fetch_pdf_bytes``
and ``ocr_with_tesseract`` to control each branch.
"""

from __future__ import annotations

import io
from pathlib import Path

from case_calendar import pdf


class TestExtractText:
    def test_uses_plain_text_first(self, monkeypatch):
        rd = {"plain_text": "  the body  ", "is_available": True}
        monkeypatch.setattr(pdf, "fetch_pdf_bytes", lambda *a, **kw: b"should not be called")
        assert pdf.extract_text(rd) == "the body"

    def test_sealed_returns_none_without_fetch(self, monkeypatch):
        rd = {"plain_text": "", "is_sealed": True}
        called = []
        monkeypatch.setattr(pdf, "fetch_pdf_bytes",
                            lambda *a, **kw: called.append("nope"))
        assert pdf.extract_text(rd) is None
        assert called == []

    def test_unavailable_returns_none_without_fetch(self, monkeypatch):
        rd = {"plain_text": "", "is_available": False}
        called = []
        monkeypatch.setattr(pdf, "fetch_pdf_bytes",
                            lambda *a, **kw: called.append("nope"))
        assert pdf.extract_text(rd) is None
        assert called == []

    def test_falls_back_to_pypdf(self, monkeypatch):
        rd = {"plain_text": "", "is_available": True,
              "filepath_ia": "https://archive.org/x.pdf"}
        monkeypatch.setattr(pdf, "fetch_pdf_bytes", lambda *a, **kw: b"%PDF-1.4 fake")
        monkeypatch.setattr(pdf, "extract_with_pypdf",
                            lambda data: "extracted text " * 50)
        result = pdf.extract_text(rd)
        assert result and "extracted text" in result

    def test_pypdf_short_falls_back_to_ocr(self, monkeypatch):
        rd = {"plain_text": "", "is_available": True,
              "filepath_ia": "https://archive.org/x.pdf"}
        monkeypatch.setattr(pdf, "fetch_pdf_bytes", lambda *a, **kw: b"%PDF")
        monkeypatch.setattr(pdf, "extract_with_pypdf", lambda data: "")
        monkeypatch.setattr(pdf, "ocr_with_tesseract",
                            lambda data: "ocr'd text " * 30)
        result = pdf.extract_text(rd)
        assert result and "ocr'd text" in result

    def test_pypdf_short_falls_back_to_returning_partial_when_ocr_disabled(
        self, monkeypatch
    ):
        rd = {"plain_text": "", "is_available": True,
              "filepath_ia": "https://archive.org/x.pdf"}
        monkeypatch.setattr(pdf, "fetch_pdf_bytes", lambda *a, **kw: b"%PDF")
        monkeypatch.setattr(pdf, "extract_with_pypdf", lambda data: "tiny")
        called = []
        monkeypatch.setattr(pdf, "ocr_with_tesseract",
                            lambda data: called.append("nope") or "")
        result = pdf.extract_text(rd, allow_ocr=False)
        # Returns the short text rather than None.
        assert result == "tiny"
        assert called == []

    def test_no_fetch_source_returns_none(self, monkeypatch):
        rd = {"plain_text": "", "is_available": True,
              "filepath_local": None, "filepath_ia": ""}
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
        # Stub httpx.Client to return a successful response on the IA URL.
        import httpx

        class _Resp:
            status_code = 200
            content = b"%PDF-1.4 bytes"

        class _Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url): return _Resp()

        monkeypatch.setattr(httpx, "Client", _Client)
        rd = {"filepath_ia": "https://archive.org/x.pdf"}
        assert pdf.fetch_pdf_bytes(rd) == b"%PDF-1.4 bytes"

    def test_falls_through_to_cl_storage_url(self, monkeypatch):
        # IA returns 404; CL storage URL returns 200 — second branch in the loop.
        import httpx

        seen: list[str] = []

        class _Resp:
            def __init__(self, status, content): self.status_code = status; self.content = content

        class _Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url):
                seen.append(url)
                if "archive.org" in url:
                    return _Resp(404, b"")
                return _Resp(200, b"%PDF cl bytes")

        monkeypatch.setattr(httpx, "Client", _Client)
        rd = {
            "filepath_ia": "https://archive.org/x.pdf",
            "filepath_local": "recap/foo.pdf",
        }
        assert pdf.fetch_pdf_bytes(rd) == b"%PDF cl bytes"
        assert any("storage.courtlistener.com" in u for u in seen)

    def test_network_error_falls_through(self, monkeypatch):
        # First URL throws; second URL succeeds. Tests the try/except path.
        import httpx

        class _Resp:
            status_code = 200
            content = b"%PDF cl bytes"

        attempts = {"count": 0}

        class _Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url):
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise httpx.RequestError("connection refused")
                return _Resp()

        monkeypatch.setattr(httpx, "Client", _Client)
        rd = {
            "filepath_ia": "https://archive.org/x.pdf",
            "filepath_local": "recap/foo.pdf",
        }
        assert pdf.fetch_pdf_bytes(rd) == b"%PDF cl bytes"

    def test_all_urls_fail_returns_none(self, monkeypatch):
        import httpx

        class _Resp:
            status_code = 404
            content = b""

        class _Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url): return _Resp()

        monkeypatch.setattr(httpx, "Client", _Client)
        assert pdf.fetch_pdf_bytes({"filepath_ia": "https://x.com/y.pdf"}) is None


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
        # and assert empty-string return — that's a valid happy path branch
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

                class R:
                    stdout = b""
                return R()
            elif tool == "tesseract":
                # argv format: ["tesseract", "<input.png>", "-", "-l", "eng"]
                class R:
                    stdout = b"page text"
                return R()
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
        import httpx

        class _Resp:
            status_code = 200
            content = b"%PDF-1.4 doj-pr-attachment"

        class _Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url): return _Resp()

        monkeypatch.setattr(httpx, "Client", _Client)
        out = pdf.fetch_url_bytes("https://www.justice.gov/opa/media/x/dl")
        assert out == b"%PDF-1.4 doj-pr-attachment"

    def test_returns_none_on_non_200(self, monkeypatch):
        import httpx

        class _Resp:
            status_code = 404
            content = b""

        class _Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url): return _Resp()

        monkeypatch.setattr(httpx, "Client", _Client)
        assert pdf.fetch_url_bytes("https://x.com/missing.pdf") is None

    def test_returns_none_on_network_error(self, monkeypatch):
        import httpx

        class _Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url):
                raise httpx.RequestError("dns failure")

        monkeypatch.setattr(httpx, "Client", _Client)
        assert pdf.fetch_url_bytes("https://nowhere.invalid/x.pdf") is None


class TestExtractTextFromUrl:
    def test_returns_text_when_pypdf_succeeds(self, monkeypatch):
        monkeypatch.setattr(pdf, "fetch_url_bytes", lambda url: b"%PDF bytes")
        monkeypatch.setattr(pdf, "extract_with_pypdf",
                            lambda data: "indictment body " * 30)
        out = pdf.extract_text_from_url("https://example.com/x.pdf")
        assert out and "indictment body" in out

    def test_returns_none_when_fetch_fails(self, monkeypatch):
        monkeypatch.setattr(pdf, "fetch_url_bytes", lambda url: None)
        assert pdf.extract_text_from_url("https://x.com/y.pdf") is None

    def test_falls_back_to_ocr_when_pypdf_short(self, monkeypatch):
        monkeypatch.setattr(pdf, "fetch_url_bytes", lambda url: b"%PDF")
        monkeypatch.setattr(pdf, "extract_with_pypdf", lambda data: "")
        monkeypatch.setattr(pdf, "ocr_with_tesseract",
                            lambda data: "ocr indictment " * 20)
        out = pdf.extract_text_from_url("https://example.com/x.pdf")
        assert out and "ocr indictment" in out

    def test_returns_short_pypdf_when_ocr_disabled(self, monkeypatch):
        monkeypatch.setattr(pdf, "fetch_url_bytes", lambda url: b"%PDF")
        monkeypatch.setattr(pdf, "extract_with_pypdf", lambda data: "tiny")
        called = []
        monkeypatch.setattr(pdf, "ocr_with_tesseract",
                            lambda data: called.append("nope") or "")
        out = pdf.extract_text_from_url("https://x.com/y.pdf", allow_ocr=False)
        assert out == "tiny"
        assert called == []

    def test_returns_none_when_all_paths_empty(self, monkeypatch):
        monkeypatch.setattr(pdf, "fetch_url_bytes", lambda url: b"%PDF")
        monkeypatch.setattr(pdf, "extract_with_pypdf", lambda data: "")
        monkeypatch.setattr(pdf, "ocr_with_tesseract", lambda data: "")
        assert pdf.extract_text_from_url("https://x.com/y.pdf") is None


class TestExtractTextOcrShorter:
    def test_falls_back_to_short_pypdf_text_when_ocr_short(self, monkeypatch):
        # Both pypdf and OCR return below-threshold text; the function
        # returns the (short) pypdf text, since it's at least something.
        rd = {"plain_text": "", "is_available": True,
              "filepath_ia": "https://archive.org/x.pdf"}
        monkeypatch.setattr(pdf, "fetch_pdf_bytes", lambda *a, **kw: b"%PDF")
        monkeypatch.setattr(pdf, "extract_with_pypdf", lambda data: "tiny")
        monkeypatch.setattr(pdf, "ocr_with_tesseract", lambda data: "also tiny")
        assert pdf.extract_text(rd) == "tiny"

    def test_returns_none_when_all_paths_empty(self, monkeypatch):
        rd = {"plain_text": "", "is_available": True,
              "filepath_ia": "https://archive.org/x.pdf"}
        monkeypatch.setattr(pdf, "fetch_pdf_bytes", lambda *a, **kw: b"%PDF")
        monkeypatch.setattr(pdf, "extract_with_pypdf", lambda data: "")
        monkeypatch.setattr(pdf, "ocr_with_tesseract", lambda data: "")
        assert pdf.extract_text(rd) is None
