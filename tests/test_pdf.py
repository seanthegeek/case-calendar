"""Tests for the PDF text-extraction fallback chain.

We don't actually fetch PDFs in tests; we monkey-patch ``fetch_pdf_bytes``
and ``ocr_with_tesseract`` to control each branch.
"""

from __future__ import annotations

import io

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
