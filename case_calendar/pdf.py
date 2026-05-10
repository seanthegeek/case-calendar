"""PDF text extraction with fallbacks.

CourtListener's ``plain_text`` is the happy path. When it's empty for an
otherwise-available PDF, we try, in order:

  1. Pull the PDF from Internet Archive (``filepath_ia``) and run pypdf —
     handles any PDF that has embedded text.
  2. If pypdf yields nothing usable AND the local system has ``tesseract``
     and ``pdftoppm`` (poppler) installed, OCR each page.

If neither path works we return ``None`` and the caller skips this PDF; the
entry will be re-fingerprinted next sync, so we'll retry once CL has the
text or the user installs OCR tools.
"""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger(__name__)

# Heuristic: a 2-page+ document with under 100 chars of text probably failed
# extraction. Don't burn OCR cycles on one-pagers though.
_MIN_USEFUL_CHARS = 100


def fetch_pdf_bytes(rd: dict, *, timeout: float = 30.0) -> Optional[bytes]:
    """Try to download the PDF for a recap_document. Returns None if no source.

    We avoid CourtListener's storage URL (which can require auth or rate
    limits) and prefer the Internet Archive mirror, then fall back to
    constructing a CL storage URL from ``filepath_local``.
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
                r = client.get(url)
                if r.status_code == 200 and r.content:
                    return r.content
                log.warning("pdf fetch %s -> %s", url, r.status_code)
        except Exception as e:
            log.warning("pdf fetch %s failed: %s", url, e)
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
                ["pdftoppm", "-r", "300", "-gray", "-png", str(pdf_path), str(tmp_path / "page")],
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


def extract_text(rd: dict, *, allow_ocr: bool = True) -> Optional[str]:
    """Extract text from a recap_document, handling all the gap cases.

    Order of operations:
      1. Use ``plain_text`` from CL if non-empty.
      2. If the PDF is sealed, return None — never going to be available.
      3. If not is_available, return None — not on RECAP yet; we'll retry
         on next sync once the fingerprint changes.
      4. Download the PDF, try pypdf, then optionally tesseract OCR.
    """
    text = (rd.get("plain_text") or "").strip()
    if text:
        return text

    if rd.get("is_sealed"):
        return None
    if not rd.get("is_available"):
        return None

    pdf_bytes = fetch_pdf_bytes(rd)
    if not pdf_bytes:
        return None

    text = extract_with_pypdf(pdf_bytes)
    if len(text) >= _MIN_USEFUL_CHARS:
        return text

    if allow_ocr:
        ocr = ocr_with_tesseract(pdf_bytes)
        if len(ocr) >= _MIN_USEFUL_CHARS:
            return ocr

    return text or None
