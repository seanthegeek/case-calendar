"""Tests for ``model-comparison/build_scoring_page.py`` pure helpers — the
content filter and chronological sort (the entries that show on the blind
scoring page), the dedup key, and the per-document link/text builders. Loaded by
path."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "model-comparison"
    / "build_scoring_page.py"
)
_spec = importlib.util.spec_from_file_location("build_scoring_page", _SCRIPT)
assert _spec and _spec.loader
bsp = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bsp
_spec.loader.exec_module(bsp)


def _doc(**kw):
    d = {
        "document_number": kw.get("num", 1),
        "attachment_number": kw.get("att"),
        "description": kw.get("desc", ""),
        "plain_text": kw.get("plain_text", ""),
        "is_sealed": kw.get("is_sealed", False),
        "filepath_local": kw.get("filepath_local", ""),
        "filepath_ia": kw.get("filepath_ia", ""),
    }
    return d


def _entry(**kw):
    e = {
        "entry_id": kw.get("entry_id", 1),
        "entry_number": kw.get("entry_number"),
        "date_filed": kw.get("date_filed", ""),
        "description": kw.get("description", ""),
        "short_description": kw.get("short_description", ""),
        "recap_documents": json.dumps(kw.get("docs", [])),
    }
    return e


# --------------------------------------------------------------------------- #
# _has_content — the content-less-entry filter
# --------------------------------------------------------------------------- #


def test_has_content_keeps_entries_with_a_description():
    assert bsp._has_content(_entry(description="ORDER granting motion"))
    assert bsp._has_content(_entry(short_description="Set/Reset Hearing"))


def test_has_content_keeps_doc_with_text_or_fetchable_url():
    assert bsp._has_content(_entry(docs=[_doc(plain_text="real body text")]))
    assert bsp._has_content(_entry(docs=[_doc(filepath_local="recap/x.pdf")]))
    assert bsp._has_content(_entry(docs=[_doc(filepath_ia="ia/x.pdf")]))


def test_has_content_drops_content_less_clerk_notice():
    # empty description + a not-on-RECAP doc with no text = nothing to score.
    assert not bsp._has_content(_entry(docs=[_doc(desc="Clerk's Notice")]))
    # a sealed doc isn't fetchable either
    assert not bsp._has_content(_entry(docs=[_doc(is_sealed=True, desc="Sealed")]))
    # nothing at all
    assert not bsp._has_content(_entry())


# --------------------------------------------------------------------------- #
# _sort_entries — chronological, paperless interleaved (not dumped at the end)
# --------------------------------------------------------------------------- #


def test_sort_is_chronological_with_paperless_interleaved():
    rows = [
        _entry(entry_id=1, entry_number=55, date_filed="2025-02-12"),
        _entry(entry_id=2, entry_number=None, date_filed="2025-02-12"),  # paperless
        _entry(entry_id=3, entry_number=44, date_filed="2024-12-18"),  # earlier
        _entry(entry_id=4, entry_number=53, date_filed="2025-02-18"),
    ]
    order = [e["entry_id"] for e in bsp._sort_entries(rows)]
    # earliest date first; the paperless 2025-02-12 entry sits next to #55 (same
    # day), NOT dumped after the last numbered filing.
    assert order == [3, 1, 2, 4]


def test_sort_same_day_numbered_before_paperless():
    rows = [
        _entry(entry_id=10, entry_number=None, date_filed="2025-01-01"),
        _entry(entry_id=11, entry_number=5, date_filed="2025-01-01"),
    ]
    # numbered (5) sorts before paperless (1<<30) on the same day
    assert [e["entry_id"] for e in bsp._sort_entries(rows)] == [11, 10]


# --------------------------------------------------------------------------- #
# _dedup_key
# --------------------------------------------------------------------------- #


def test_dedup_key():
    assert bsp._dedup_key(_entry(entry_number=65)) == ("num", 65)
    assert bsp._dedup_key(
        _entry(entry_number=None, date_filed="2025-01-01", description="Set/Reset")
    ) == ("desc", "2025-01-01", "Set/Reset")
    # paperless with no description keys on entry_id (never merged)
    assert bsp._dedup_key(_entry(entry_id=99, entry_number=None)) == ("uid", 99)


# --------------------------------------------------------------------------- #
# _docs_for / _doc_text
# --------------------------------------------------------------------------- #


def test_docs_for_status_and_url():
    docs = bsp._docs_for(
        json.dumps(
            [
                _doc(num=65, filepath_local="recap/a.pdf"),
                _doc(num=66, is_sealed=True),
                _doc(num=67),  # no url, not sealed
            ]
        )
    )
    by_label = {d["label"]: d for d in docs}
    assert by_label["65"]["url"].startswith("https://storage.courtlistener.com/")
    assert by_label["65"]["status"] == ""
    assert by_label["66"]["status"] == "sealed"
    assert by_label["67"]["status"] == "not yet on RECAP"


def test_doc_text_concatenates_labeled_bodies():
    txt = bsp._doc_text(
        json.dumps(
            [_doc(num=1, plain_text="alpha"), _doc(num=2, att=1, plain_text="beta")]
        )
    )
    assert "[doc 1]\nalpha" in txt
    assert "[doc 2-1]\nbeta" in txt


def test_full_url_promotes_relative():
    assert bsp._full_url("/docket/1/x/") == "https://www.courtlistener.com/docket/1/x/"
    assert bsp._full_url("https://x/y") == "https://x/y"
    assert bsp._full_url("") == ""
