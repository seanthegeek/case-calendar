"""Shared pytest fixtures.

Tests should never hit the network or call a real LLM. Fixtures here provide
a tmp-path SQLite store, a fake CourtListener client, and stubs for the LLM
extractor.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

import pytest

from case_calendar.courtlistener import CourtListener
from case_calendar.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "case-calendar.sqlite")
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def make_entry():
    """Factory for synthesizing a docket entry dict shaped like the CL API."""

    def _make(
        *,
        entry_id: int = 1,
        docket: int = 999,
        description: str = "",
        short_description: str = "",
        date_filed: str = "2026-01-01",
        date_modified: str = "2026-01-01T08:00:00-07:00",
        recap_documents: list[dict] | None = None,
    ) -> dict[str, Any]:
        return {
            "id": entry_id,
            "docket": docket,
            "entry_number": entry_id,
            "date_filed": date_filed,
            "date_modified": date_modified,
            "description": description,
            "short_description": short_description,
            "recap_documents": list(recap_documents or []),
        }

    return _make


@pytest.fixture
def make_recap_doc():
    def _make(
        *,
        doc_id: int = 1,
        is_available: bool = True,
        is_sealed: bool | None = None,
        plain_text: str = "",
        filepath_local: str | None = None,
        filepath_ia: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        return {
            "id": doc_id,
            "is_available": is_available,
            "is_sealed": is_sealed,
            "plain_text": plain_text,
            "filepath_local": filepath_local,
            "filepath_ia": filepath_ia,
            "description": description,
        }

    return _make


class FakeCL:
    """Stand-in for CourtListener that records calls and returns canned data.

    Pass `dockets={id: {...}}`, `entries={id: [entry, ...]}`,
    `courts={id: {...}}`, or `recap_docs={id: {...}}` per test.
    """

    def __init__(
        self,
        *,
        dockets: dict[int, dict] | None = None,
        entries: dict[int, list[dict]] | None = None,
        courts: dict[str, dict] | None = None,
        recap_docs: dict[int, dict] | None = None,
    ):
        self._dockets = dockets or {}
        self._entries = entries or {}
        self._courts = courts or {}
        self._recap_docs = recap_docs or {}
        self.calls: list[tuple[str, Any]] = []

    def get_docket(self, docket_id: int) -> dict:
        self.calls.append(("docket", docket_id))
        if docket_id not in self._dockets:
            raise KeyError(f"no canned docket for id={docket_id}")
        return self._dockets[docket_id]

    def get_court(self, court_id: str) -> dict:
        self.calls.append(("court", court_id))
        return self._courts.get(court_id, {
            "citation_string": court_id.upper(),
            "short_name": court_id,
            "full_name": court_id,
        })

    def get_recap_document(self, doc_id: int) -> dict:
        self.calls.append(("recap", doc_id))
        return self._recap_docs.get(doc_id, {})

    def iter_entries(self, docket_id: int, *, modified_after=None, **_):
        self.calls.append(("entries", docket_id))
        for e in self._entries.get(docket_id, []):
            if modified_after and (e.get("date_modified") or "") < modified_after:
                return
            yield e

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


@pytest.fixture
def fake_cl():
    return FakeCL


@pytest.fixture(autouse=True)
def _no_real_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure tests don't accidentally hit the real CL API."""
    monkeypatch.setenv("COURTLISTENER_TOKEN", "test-token")
    # Strip any real LLM creds the dev shell might have.
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
              "GOOGLE_API_KEY", "LLM_PROVIDER", "LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
