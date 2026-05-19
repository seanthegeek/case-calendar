"""Tests for the cli emit-time helpers (title composition, deadline mapping).

Title composition lives at the cli/emit layer, not in the renderers, so the
ICS and gcal outputs receive a fully-built title and write it through.
"""

from __future__ import annotations

import argparse
import io
import urllib.error
from argparse import Namespace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from case_calendar import cli
from .conftest import must
from case_calendar.cli import (
    _cases_from_config,
    _compose_title,
    _deadline_to_hearing,
    _load_config,
    _resolve_gcal,
    _resolve_m365,
    cmd_emit,
    cmd_serve,
    cmd_setup,
    cmd_show,
    cmd_summarize,
    cmd_sync,
    emit_calendars,
    main,
)


class TestComposeTitle:
    def test_timed_hearing_no_time_status_prefix(self):
        out = _compose_title(
            raw_title="Sentencing",
            kind="HEARING",
            case_name="US v. X",
            starts_at_utc="2099-04-14T15:00:00+00:00",
            duration_minutes=90,
        )
        assert out == "[HEARING] US v. X: Sentencing"

    def test_future_date_only_hearing_gets_time_tbd(self):
        out = _compose_title(
            raw_title="Sentencing",
            kind="HEARING",
            case_name="US v. X",
            starts_at_utc="2099-04-14T04:00:00+00:00",
            duration_minutes=0,
        )
        # Category first, then time-status, then case name. Subscribers
        # scanning a shared calendar can spot the kind ([HEARING]) at a
        # glance regardless of whether a time-status flag is present.
        assert out == "[HEARING] [time TBD] US v. X: Sentencing"

    def test_past_date_only_hearing_gets_time_unknown(self):
        out = _compose_title(
            raw_title="Sentencing",
            kind="HEARING",
            case_name="US v. X",
            starts_at_utc="2020-04-14T04:00:00+00:00",
            duration_minutes=0,
        )
        assert out == "[HEARING] [time unknown] US v. X: Sentencing"

    def test_deadline_kind_prefix(self):
        out = _compose_title(
            raw_title="Reply ISO MTD",
            kind="DEADLINE",
            case_name="Anthropic v. DOW",
            starts_at_utc="2026-05-31T21:00:00+00:00",
            duration_minutes=15,
        )
        assert out == "[DEADLINE] Anthropic v. DOW: Reply ISO MTD"

    def test_null_duration_treated_as_no_time(self):
        out = _compose_title(
            raw_title="Trial",
            kind="HEARING",
            case_name="US v. Y",
            starts_at_utc="2099-04-14T04:00:00+00:00",
            duration_minutes=None,
        )
        assert "[time TBD]" in out


class TestDeadlineToHearing:
    def _row(self, **over):
        base = {
            "case_id": "anthropic-v-dow",
            "deadline_key": "reply-mtd",
            "title": "Reply ISO MTD",
            "due_at_utc": "2026-05-31T21:00:00+00:00",
            "timezone": "America/New_York",
            "notes": None,
            "status": "pending",
            "significance": "major",
            "deadline_type": "reply",
            "gcal_event_id": None,
            "docket_id": 72380208,
            "source_entry_ids": [1, 2],
        }
        base.update(over)
        return base

    def test_returns_none_without_due_timestamp(self):
        assert _deadline_to_hearing(self._row(due_at_utc=None)) is None

    def test_uid_namespace_is_prefixed(self):
        # The "deadline:" prefix on the hearing_key keeps the ICS UID and
        # gcal deterministic ID separate from any real hearing's namespace —
        # otherwise a hearing and a deadline sharing a slug would collide.
        out = must(_deadline_to_hearing(self._row()))
        assert out["hearing_key"] == "deadline:reply-mtd"

    def test_does_not_pre_prefix_title(self):
        # _compose_title is responsible for prefixing — _deadline_to_hearing
        # returns the raw title so cli.py's compose step has clean inputs.
        out = must(_deadline_to_hearing(self._row()))
        assert out["title"] == "Reply ISO MTD"

    def test_passed_status_maps_to_held(self):
        # Past-due pending deadlines flip to 'passed' in the store; for
        # rendering they map to 'held' so they stay visible in the ICS feed.
        out = must(_deadline_to_hearing(self._row(status="passed")))
        assert out["status"] == "held"

    def test_met_status_maps_to_cancelled(self):
        # 'met' = the filing was made. Renderers skip cancelled rows so
        # they fall off the calendar — exactly what we want for met
        # deadlines, which no longer need a reminder.
        out = must(_deadline_to_hearing(self._row(status="met")))
        assert out["status"] == "cancelled"


class TestEmitCalendars:
    """``emit_calendars`` is shared by cmd_emit, cmd_sync's auto-emit, and
    the webhook auto-emit. The scoping (only_calendars) is what lets the
    webhook path skip calendars unaffected by a given delivery."""

    @pytest.fixture
    def cfg(self, tmp_path):
        return {
            "store_path": str(tmp_path / "x.sqlite"),
            "calendars": {
                "cyber": {
                    "name": "Cybercrime",
                    "ics_path": str(tmp_path / "cyber.ics"),
                },
                "tech": {
                    "name": "Tech",
                    "ics_path": str(tmp_path / "tech.ics"),
                },
            },
            "cases": [
                {
                    "id": "us-v-x",
                    "name": "US v. X",
                    "calendar": "cyber",
                    "dockets": [100],
                },
                {
                    "id": "acme-v-widget",
                    "name": "Acme v. Widget",
                    "calendar": "tech",
                    "dockets": [200],
                },
            ],
        }

    def _seed_hearing(self, store, *, case_id, key, calendar_unused="cyber"):
        store.upsert_hearing(
            {
                "case_id": case_id,
                "hearing_key": key,
                "title": "Sentencing",
                "hearing_type": "sentencing",
                "starts_at_utc": "2099-04-14T15:00:00+00:00",
                "duration_minutes": 90,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [1],
            }
        )

    def test_writes_ics_for_each_calendar(self, store, cfg):
        self._seed_hearing(store, case_id="us-v-x", key="sentencing-x")
        self._seed_hearing(store, case_id="acme-v-widget", key="hearing-acme")
        results = emit_calendars(cfg, store)
        assert set(results) == {"cyber", "tech"}
        assert results["cyber"]["events"] == 1
        assert results["tech"]["events"] == 1
        # ICS files are real on disk.
        for cal in ("cyber", "tech"):
            text = open(results[cal]["ics_path"]).read()
            assert "BEGIN:VCALENDAR" in text and "END:VCALENDAR" in text

    def test_only_calendars_scopes_writes(self, store, cfg, tmp_path):
        # Pre-write the tech ICS with a sentinel string. Scoped emit on
        # {"cyber"} must not touch tech.ics.
        sentinel = tmp_path / "tech.ics"
        sentinel.write_text("SHOULD-NOT-BE-OVERWRITTEN")
        self._seed_hearing(store, case_id="us-v-x", key="sentencing-x")
        results = emit_calendars(cfg, store, only_calendars={"cyber"})
        assert set(results) == {"cyber"}
        assert sentinel.read_text() == "SHOULD-NOT-BE-OVERWRITTEN"

    def test_docket_entry_numbers_rendered_into_ics(self, store, cfg):
        # The hearing's source_entry_ids should be resolved against the
        # entries table to surface PACER docket positions in the description.
        store.mark_entry(
            100,
            1001,
            "2026-01-01T00:00:00Z",
            "fp",
            entry_number=65,
            description="ORDER",
        )
        store.mark_entry(
            100,
            1002,
            "2026-01-02T00:00:00Z",
            "fp",
            entry_number=82,
            description="ORDER",
        )
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "sentencing-x",
                "title": "Sentencing",
                "starts_at_utc": "2099-04-14T15:00:00+00:00",
                "duration_minutes": 90,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [1001, 1002],
            }
        )
        emit_calendars(cfg, store, only_calendars={"cyber"})
        text = open(cfg["calendars"]["cyber"]["ics_path"]).read()
        # ICS folds long lines at 75 octets, so the literal text may be
        # broken across "\r\n " continuations; un-fold before asserting.
        unfolded = text.replace("\r\n ", "")
        assert "Docket entries: 65\\, 82" in unfolded

    def test_document_urls_rendered_into_ics(self, store, cfg):
        # Each source entry's recap_documents JSON is pulled at emit time
        # and flattened into the hearing's `documents` list, then rendered
        # one-line-per-doc by the description builder.
        docs_1001 = [
            {
                "id": 5,
                "document_number": 65,
                "attachment_number": None,
                "is_available": True,
                "is_sealed": False,
                "filepath_ia": "https://archive.org/65.pdf",
            },
            {
                "id": 6,
                "document_number": 65,
                "attachment_number": 1,
                "is_available": True,
                "is_sealed": False,
                "filepath_ia": "https://archive.org/65a.pdf",
            },
        ]
        store.mark_entry(
            100,
            1001,
            "2026-01-01T00:00:00Z",
            "fp",
            entry_number=65,
            description="ORDER",
            recap_documents=docs_1001,
        )
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "sentencing-x",
                "title": "Sentencing",
                "starts_at_utc": "2099-04-14T15:00:00+00:00",
                "duration_minutes": 90,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [1001],
            }
        )
        emit_calendars(cfg, store, only_calendars={"cyber"})
        # Read bytes so the on-disk CRLF survives Python's text-mode
        # newline translation; otherwise the long second URL gets folded
        # ("\r\n ") and the fold normalizes to "\n " under text mode, so
        # the literal "\r\n " unfold misses it.
        text = Path(cfg["calendars"]["cyber"]["ics_path"]).read_bytes().decode()
        unfolded = text.replace("\r\n ", "")
        assert "Documents:" in unfolded
        assert "65: https://archive.org/65.pdf" in unfolded
        assert "65-1: https://archive.org/65a.pdf" in unfolded

    def test_gcal_skipped_when_no_token_cache(self, store, cfg, tmp_path):
        # gcal push auto-enables when a token cache is present. Without
        # one — first run, or after a token wipe — push is skipped
        # silently so the daemon never blocks on a missing OAuth.
        cfg["calendars"]["cyber"]["google_calendar_id"] = (
            "abc@group.calendar.google.com"
        )
        cfg["google_credentials_path"] = "/nonexistent.json"  # would crash if used
        cfg["google_token_path"] = str(tmp_path / "no-such-token.json")
        self._seed_hearing(store, case_id="us-v-x", key="sentencing-x")
        results = emit_calendars(cfg, store)
        assert results["cyber"]["gcal_pushed"] is False

    def test_m365_skipped_when_no_token_cache(self, store, cfg, tmp_path, monkeypatch):
        # Same auto-detect contract as gcal: configured client id but no
        # cached token => skip, don't crash.
        cfg["calendars"]["cyber"]["m365_calendar_id"] = "AAMkADExAAA"
        cfg["m365_client_id"] = "00000000-0000-0000-0000-000000000000"
        cfg["m365_token_path"] = str(tmp_path / "no-such-m365.json")
        monkeypatch.delenv("M365_CLIENT_ID", raising=False)
        self._seed_hearing(store, case_id="us-v-x", key="sentencing-x")
        results = emit_calendars(cfg, store)
        assert results["cyber"]["m365_pushed"] is False

    def test_index_html_written_when_configured(self, store, cfg, tmp_path):
        # index_path opts in to the static HTML index. It's a global file
        # listing every calendar + case, so it has to be written on every
        # emit regardless of only_calendars scoping.
        index_path = tmp_path / "site" / "index.html"
        cfg["index_path"] = str(index_path)
        cfg["public_base_url"] = "https://calendars.example.com"
        self._seed_hearing(store, case_id="us-v-x", key="sentencing-x")
        emit_calendars(cfg, store, only_calendars={"cyber"})
        text = index_path.read_text(encoding="utf-8")
        assert text.startswith("<!doctype html>")
        # Both calendars appear even though only "cyber" was in scope —
        # the index is the global view, not per-emit.
        assert "Cybercrime" in text
        assert "Tech" in text
        assert "US v. X" in text and "Acme v. Widget" in text
        # Subscribe URLs use the configured public_base_url.
        assert "https://calendars.example.com/cyber.ics" in text

    def test_index_html_write_is_logged(self, store, cfg, tmp_path, caplog):
        # The webhook auto-emit path doesn't print to stdout, so operators
        # watching journalctl rely on this log line to know the index was
        # actually refreshed (vs. emit_calendars silently skipping it).
        index_path = tmp_path / "site" / "index.html"
        cfg["index_path"] = str(index_path)
        self._seed_hearing(store, case_id="us-v-x", key="sentencing-x")
        with caplog.at_level("INFO", logger="case_calendar.cli"):
            emit_calendars(cfg, store)
        assert any(
            "wrote index" in rec.message and str(index_path) in rec.message
            for rec in caplog.records
        )

    def test_index_html_not_written_when_unconfigured(self, store, cfg, tmp_path):
        # No index_path => no index.html. Existing files in tmp_path stay.
        sentinel = tmp_path / "index.html"
        sentinel.write_text("untouched")
        self._seed_hearing(store, case_id="us-v-x", key="sentencing-x")
        emit_calendars(cfg, store)
        assert sentinel.read_text() == "untouched"

    def test_gcal_push_invokes_sync_when_token_cache_present(
        self,
        store,
        cfg,
        tmp_path,
        monkeypatch,
    ):
        # Stage a fake token cache so the resolver enables gcal push, then
        # patch the constructor and sync method so we observe the call.
        token_path = tmp_path / "google-token.json"
        token_path.write_text("{}")
        cfg["google_credentials_path"] = "/nonexistent.json"
        cfg["google_token_path"] = str(token_path)
        cfg["calendars"]["cyber"]["google_calendar_id"] = (
            "abc@group.calendar.google.com"
        )

        instances: list[MagicMock] = []

        def _factory(credentials_path, token_path):
            inst = MagicMock(name="GoogleCalendarSync")
            instances.append(inst)
            return inst

        monkeypatch.setattr(
            "case_calendar.calendars.gcal.GoogleCalendarSync",
            _factory,
        )
        self._seed_hearing(store, case_id="us-v-x", key="sentencing-x")
        results = emit_calendars(cfg, store)
        assert results["cyber"]["gcal_pushed"] is True
        assert len(instances) == 1
        instances[0].sync.assert_called_once()

    def test_m365_push_invokes_sync_when_token_cache_present(
        self,
        store,
        cfg,
        tmp_path,
        monkeypatch,
    ):
        token_path = tmp_path / "m365-token.json"
        token_path.write_text("{}")
        cfg["m365_client_id"] = "00000000-0000-0000-0000-000000000000"
        cfg["m365_token_path"] = str(token_path)
        cfg["calendars"]["cyber"]["m365_calendar_id"] = "AAMkADExAAA"

        instances: list[MagicMock] = []

        def _factory(client_id, token_path):
            inst = MagicMock(name="M365CalendarSync")
            instances.append(inst)
            return inst

        monkeypatch.setattr(
            "case_calendar.calendars.m365.M365CalendarSync",
            _factory,
        )
        monkeypatch.delenv("M365_CLIENT_ID", raising=False)
        self._seed_hearing(store, case_id="us-v-x", key="sentencing-x")
        results = emit_calendars(cfg, store)
        assert results["cyber"]["m365_pushed"] is True
        assert len(instances) == 1
        instances[0].sync.assert_called_once()

    def test_m365_use_default_calendar_opts_in(
        self,
        store,
        cfg,
        tmp_path,
        monkeypatch,
    ):
        # When m365_calendar_id is absent, m365_use_default_calendar: true
        # still routes to Microsoft's default calendar.
        token_path = tmp_path / "m365-token.json"
        token_path.write_text("{}")
        cfg["m365_client_id"] = "11111111-1111-1111-1111-111111111111"
        cfg["m365_token_path"] = str(token_path)
        cfg["calendars"]["cyber"]["m365_use_default_calendar"] = True

        instances: list[MagicMock] = []

        def _factory(client_id, token_path):
            inst = MagicMock(name="M365CalendarSync")
            instances.append(inst)
            return inst

        monkeypatch.setattr(
            "case_calendar.calendars.m365.M365CalendarSync",
            _factory,
        )
        monkeypatch.delenv("M365_CLIENT_ID", raising=False)
        self._seed_hearing(store, case_id="us-v-x", key="sentencing-x")
        results = emit_calendars(cfg, store)
        assert results["cyber"]["m365_pushed"] is True


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_loads_valid_config(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text(
            yaml.safe_dump(
                {
                    "calendars": {"a": {"name": "A"}},
                    "cases": [
                        {"id": "x", "name": "X", "calendar": "a", "dockets": [1]}
                    ],
                }
            )
        )
        cfg = _load_config(str(p))
        assert cfg["cases"][0]["id"] == "x"

    def test_rejects_missing_keys(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text(yaml.safe_dump({"cases": []}))
        with pytest.raises(SystemExit):
            _load_config(str(p))

    def test_rejects_empty_config(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text("")
        with pytest.raises(SystemExit):
            _load_config(str(p))


class TestCasesFromConfig:
    def test_parses_extract_deadlines_default(self):
        cfg = {
            "cases": [
                {"id": "x", "name": "X", "calendar": "a", "dockets": [1]},
                {
                    "id": "y",
                    "name": "Y",
                    "calendar": "a",
                    "dockets": [2],
                    "extract_deadlines": True,
                },
            ]
        }
        cases = _cases_from_config(cfg)
        assert cases[0].extract_deadlines is False
        assert cases[1].extract_deadlines is True

    def test_defaults_empty_extra_documents(self):
        cfg = {
            "cases": [
                {"id": "x", "name": "X", "calendar": "a", "dockets": [1]},
            ]
        }
        cases = _cases_from_config(cfg)
        assert cases[0].extra_documents == []

    def test_parses_extra_documents(self):
        cfg = {
            "cases": [
                {
                    "id": "us-v-zewei",
                    "name": "United States v. Zewei",
                    "calendar": "cybercrime",
                    "dockets": [70789744],
                    "extra_documents": [
                        {
                            "docket": 70789744,
                            "url": "https://www.justice.gov/opa/media/1407196/dl",
                            "note": "Sourced from DoJ press release attachment; "
                            "indictment was unsealed by court order despite "
                            "SEALED watermarks.",
                        }
                    ],
                }
            ]
        }
        cases = _cases_from_config(cfg)
        assert len(cases[0].extra_documents) == 1
        extra = cases[0].extra_documents[0]
        assert extra.docket == 70789744
        assert extra.url == "https://www.justice.gov/opa/media/1407196/dl"
        assert extra.note.startswith("Sourced from DoJ")

    def test_strips_extra_documents_note_whitespace(self):
        # YAML literal-block notes often arrive with leading / trailing
        # whitespace; the parser strips them so the LLM prompt isn't
        # dotted with blank tails that change cache fingerprints on
        # whitespace churn.
        cfg = {
            "cases": [
                {
                    "id": "x",
                    "name": "X",
                    "calendar": "a",
                    "dockets": [1],
                    "extra_documents": [
                        {
                            "docket": 1,
                            "url": "https://example.com/x.pdf",
                            "note": "\n  trailing newline note  \n",
                        }
                    ],
                }
            ]
        }
        cases = _cases_from_config(cfg)
        assert cases[0].extra_documents[0].note == "trailing newline note"

    def test_extra_documents_none_or_empty_yields_empty_list(self):
        for raw in (None, []):
            cfg = {
                "cases": [
                    {
                        "id": "x",
                        "name": "X",
                        "calendar": "a",
                        "dockets": [1],
                        "extra_documents": raw,
                    }
                ]
            }
            cases = _cases_from_config(cfg)
            assert cases[0].extra_documents == []

    def test_extra_documents_must_be_list(self):
        cfg = {
            "cases": [
                {
                    "id": "x",
                    "name": "X",
                    "calendar": "a",
                    "dockets": [1],
                    "extra_documents": {"docket": 1},
                }
            ]
        }
        with pytest.raises(SystemExit, match="must be a list"):
            _cases_from_config(cfg)

    def test_extra_documents_entry_must_be_mapping(self):
        cfg = {
            "cases": [
                {
                    "id": "x",
                    "name": "X",
                    "calendar": "a",
                    "dockets": [1],
                    "extra_documents": ["not-a-dict"],
                }
            ]
        }
        with pytest.raises(SystemExit, match="must be a mapping"):
            _cases_from_config(cfg)

    def test_extra_documents_missing_required_key_raises(self):
        # Missing `note` — operator forgot to describe the document.
        # Catch loudly; the note is the whole reason the field exists.
        cfg = {
            "cases": [
                {
                    "id": "x",
                    "name": "X",
                    "calendar": "a",
                    "dockets": [1],
                    "extra_documents": [{"docket": 1, "url": "https://x.com/y.pdf"}],
                }
            ]
        }
        with pytest.raises(SystemExit, match="missing key"):
            _cases_from_config(cfg)

    def test_extra_documents_docket_must_be_on_case(self):
        # Specified docket id isn't tracked by this case — typo / wrong
        # docket. Surface immediately rather than silently no-op'ing.
        cfg = {
            "cases": [
                {
                    "id": "x",
                    "name": "X",
                    "calendar": "a",
                    "dockets": [1],
                    "extra_documents": [
                        {
                            "docket": 999,
                            "url": "https://x.com/y.pdf",
                            "note": "test",
                        }
                    ],
                }
            ]
        }
        with pytest.raises(SystemExit, match="is not in this case's dockets"):
            _cases_from_config(cfg)

    def test_extra_documents_docket_must_be_int(self):
        cfg = {
            "cases": [
                {
                    "id": "x",
                    "name": "X",
                    "calendar": "a",
                    "dockets": [1],
                    "extra_documents": [
                        {
                            "docket": "1",
                            "url": "https://x.com/y.pdf",
                            "note": "test",
                        }
                    ],
                }
            ]
        }
        with pytest.raises(SystemExit, match="is not in this case's dockets"):
            _cases_from_config(cfg)

    def test_extra_documents_note_must_be_string(self):
        cfg = {
            "cases": [
                {
                    "id": "x",
                    "name": "X",
                    "calendar": "a",
                    "dockets": [1],
                    "extra_documents": [
                        {
                            "docket": 1,
                            "url": "https://x.com/y.pdf",
                            "note": 12345,
                        }
                    ],
                }
            ]
        }
        with pytest.raises(SystemExit, match="note must be a string"):
            _cases_from_config(cfg)

    def test_extra_documents_note_must_be_non_empty(self):
        # Empty / whitespace-only note is a misconfiguration — the
        # entire point of the field is to describe the document.
        for empty in ("", "   ", "\n\n"):
            cfg = {
                "cases": [
                    {
                        "id": "x",
                        "name": "X",
                        "calendar": "a",
                        "dockets": [1],
                        "extra_documents": [
                            {
                                "docket": 1,
                                "url": "https://x.com/y.pdf",
                                "note": empty,
                            }
                        ],
                    }
                ]
            }
            with pytest.raises(SystemExit, match="non-empty string"):
                _cases_from_config(cfg)


# ---------------------------------------------------------------------------
# _resolve_gcal / _resolve_m365
# ---------------------------------------------------------------------------


class TestResolveGcal:
    def test_returns_none_when_credentials_unset(self):
        assert _resolve_gcal({}, setup=False) is None

    def test_returns_none_when_no_token_and_no_setup(self, tmp_path):
        cfg = {
            "google_credentials_path": "/nonexistent.json",
            "google_token_path": str(tmp_path / "missing.json"),
        }
        assert _resolve_gcal(cfg, setup=False) is None

    def test_returns_paths_when_token_present(self, tmp_path):
        token = tmp_path / "t.json"
        token.write_text("{}")
        cfg = {
            "google_credentials_path": "/c.json",
            "google_token_path": str(token),
        }
        assert _resolve_gcal(cfg, setup=False) == ("/c.json", token)

    def test_returns_paths_when_setup_flag_set_even_without_token(self, tmp_path):
        cfg = {
            "google_credentials_path": "/c.json",
            "google_token_path": str(tmp_path / "missing.json"),
        }
        result = _resolve_gcal(cfg, setup=True)
        assert result is not None and result[0] == "/c.json"


class TestResolveM365:
    def test_returns_none_when_no_client_id(self, monkeypatch):
        monkeypatch.delenv("M365_CLIENT_ID", raising=False)
        assert _resolve_m365({}, setup=False) is None

    def test_falls_back_to_env_var(self, monkeypatch, tmp_path):
        token = tmp_path / "m.json"
        token.write_text("{}")
        monkeypatch.setenv("M365_CLIENT_ID", "env-client-id")
        cfg = {"m365_token_path": str(token)}
        result = _resolve_m365(cfg, setup=False)
        assert result == ("env-client-id", token)

    def test_returns_none_when_no_token_and_no_setup(self, tmp_path, monkeypatch):
        monkeypatch.delenv("M365_CLIENT_ID", raising=False)
        cfg = {
            "m365_client_id": "cfg-id",
            "m365_token_path": str(tmp_path / "missing.json"),
        }
        assert _resolve_m365(cfg, setup=False) is None


# ---------------------------------------------------------------------------
# Command handlers (cmd_sync / cmd_emit / cmd_serve / cmd_setup / cmd_summarize
# / cmd_show / main). The handlers wire together CourtListener, syncer, summary, etc; we
# monkeypatch the dependencies so no network or LLM is hit.
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg_file(tmp_path):
    """Write a minimal config to disk and return the path."""
    cfg = {
        "store_path": str(tmp_path / "x.sqlite"),
        "calendars": {
            "cyber": {"name": "Cybercrime", "ics_path": str(tmp_path / "cyber.ics")},
        },
        "cases": [
            {"id": "us-v-x", "name": "US v. X", "calendar": "cyber", "dockets": [100]},
        ],
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


@pytest.fixture
def fake_cl_ctx(monkeypatch):
    """Replace ``cli.CourtListener`` with a context manager returning a stub."""
    instance = MagicMock(name="CourtListener")

    class _Ctx:
        def __enter__(self):
            return instance

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(cli, "CourtListener", lambda *a, **kw: _Ctx())
    return instance


class TestCmdSync:
    def test_unknown_case_id_returns_2(self, cfg_file):
        args = Namespace(config=str(cfg_file), case="nope", no_emit=False)
        assert cmd_sync(args) == 2

    def test_runs_syncer_and_emits_on_actions(
        self,
        cfg_file,
        fake_cl_ctx,
        monkeypatch,
        capsys,
    ):
        # Force CaseSyncer.sync_case to report actions, which should trigger
        # the auto-emit. Patch llm.provider_info to avoid the env detection.
        monkeypatch.setattr(cli.llm, "provider_info", lambda: "fake/model")

        def _fake_sync_case(self, case):
            return {
                "dockets_skipped": 0,
                "entries_seen": 1,
                "entries_processed": 1,
                "actions": 1,
                "verified": 0,
                "auto_passed": 0,
            }

        monkeypatch.setattr(cli.CaseSyncer, "sync_case", _fake_sync_case)
        # Capture which calendars emit was called for, without actually
        # writing to disk again (emit_calendars itself is exercised in
        # TestEmitCalendars; here we just want to know it was reached).
        emit_calls: list[set[str] | None] = []
        real_emit = cli.emit_calendars

        def _spy_emit(cfg, store, *, only_calendars=None, **kw):
            emit_calls.append(only_calendars)
            return real_emit(cfg, store, only_calendars=only_calendars, **kw)

        monkeypatch.setattr(cli, "emit_calendars", _spy_emit)

        args = Namespace(config=str(cfg_file), case=None, no_emit=False)
        assert cmd_sync(args) == 0
        # Auto-emit fired, scoped to the case's calendar.
        assert emit_calls == [{"cyber"}]
        out = capsys.readouterr().out
        assert "us-v-x" in out

    def test_emits_index_with_no_actions(
        self,
        cfg_file,
        fake_cl_ctx,
        monkeypatch,
    ):
        # Even when no calendar's hearings/deadlines changed, the global
        # index is re-rendered on every sync (a sibling docket's activity
        # may have advanced, so its row position can shift). emit_calendars
        # is called with an empty `only_calendars` set, which skips
        # per-calendar ICS / gcal / M365 work but still writes the index.
        monkeypatch.setattr(cli.llm, "provider_info", lambda: "fake/model")
        monkeypatch.setattr(
            cli.CaseSyncer,
            "sync_case",
            lambda self, case: {
                "dockets_skipped": 1,
                "entries_seen": 0,
                "entries_processed": 0,
                "actions": 0,
                "verified": 0,
                "auto_passed": 0,
            },
        )
        emit_calls: list[Any] = []
        monkeypatch.setattr(
            cli,
            "emit_calendars",
            lambda *a, **kw: emit_calls.append(kw.get("only_calendars")) or {},
        )
        args = Namespace(config=str(cfg_file), case=None, no_emit=False)
        assert cmd_sync(args) == 0
        assert emit_calls == [set()]

    def test_writes_index_on_no_op_sync_when_configured(
        self,
        cfg_file,
        tmp_path,
        fake_cl_ctx,
        monkeypatch,
    ):
        # End-to-end through the real emit_calendars: a sync that touches
        # zero calendars still refreshes index.html when index_path is set.
        # This is the fix for the bug where index.html was never generated
        # after a sync that produced no actions.
        cfg = yaml.safe_load(cfg_file.read_text())
        index_path = tmp_path / "site" / "index.html"
        cfg["index_path"] = str(index_path)
        cfg_file.write_text(yaml.safe_dump(cfg))

        monkeypatch.setattr(cli.llm, "provider_info", lambda: "fake/model")
        monkeypatch.setattr(
            cli.CaseSyncer,
            "sync_case",
            lambda self, case: {
                "dockets_skipped": 1,
                "entries_seen": 0,
                "entries_processed": 0,
                "actions": 0,
                "verified": 0,
                "auto_passed": 0,
            },
        )
        args = Namespace(config=str(cfg_file), case=None, no_emit=False)
        assert cmd_sync(args) == 0
        assert index_path.exists()
        assert index_path.read_text(encoding="utf-8").startswith("<!doctype html>")

    def test_no_emit_flag_skips_emit(
        self,
        cfg_file,
        fake_cl_ctx,
        monkeypatch,
    ):
        monkeypatch.setattr(cli.llm, "provider_info", lambda: "fake/model")
        monkeypatch.setattr(
            cli.CaseSyncer,
            "sync_case",
            lambda self, case: {
                "dockets_skipped": 0,
                "entries_seen": 1,
                "entries_processed": 1,
                "actions": 1,
                "verified": 0,
                "auto_passed": 0,
            },
        )
        emit_calls: list[Any] = []
        monkeypatch.setattr(
            cli,
            "emit_calendars",
            lambda *a, **kw: emit_calls.append(1) or {},
        )
        args = Namespace(config=str(cfg_file), case=None, no_emit=True)
        assert cmd_sync(args) == 0
        assert emit_calls == []

    def test_runs_summary_refresh_when_enabled(
        self,
        cfg_file,
        tmp_path,
        fake_cl_ctx,
        monkeypatch,
    ):
        # Enable case_summaries in config and stub refresh_stale.
        cfg = yaml.safe_load(cfg_file.read_text())
        cfg["case_summaries"] = {"enabled": True, "allow_ocr": False}
        cfg_file.write_text(yaml.safe_dump(cfg))
        monkeypatch.setattr(cli.llm, "provider_info", lambda: "fake/model")
        monkeypatch.setattr(
            cli.CaseSyncer,
            "sync_case",
            lambda self, case: {
                "dockets_skipped": 0,
                "entries_seen": 0,
                "entries_processed": 0,
                "actions": 0,
                "verified": 0,
                "auto_passed": 0,
            },
        )
        from case_calendar import summary as summary_mod

        refresh_calls: list[dict[str, Any]] = []

        def _fake_refresh(**kw):
            refresh_calls.append(kw)
            return {"us-v-x": {100}}

        monkeypatch.setattr(summary_mod, "refresh_stale", _fake_refresh)
        monkeypatch.setattr(
            cli,
            "emit_calendars",
            lambda *a, **kw: {
                "cyber": {
                    "events": 0,
                    "ics_path": None,
                    "gcal_pushed": False,
                    "m365_pushed": False,
                }
            },
        )

        args = Namespace(config=str(cfg_file), case=None, no_emit=False)
        assert cmd_sync(args) == 0
        assert len(refresh_calls) == 1
        assert refresh_calls[0]["only_case_ids"] == {"us-v-x"}
        assert refresh_calls[0]["allow_ocr"] is False
        # Default: no force, just refresh stale rows.
        assert refresh_calls[0]["force"] is False

    def test_force_summaries_flag_propagates(
        self,
        cfg_file,
        tmp_path,
        fake_cl_ctx,
        monkeypatch,
    ):
        # `sync --force-summaries` bundles the equivalent of
        # `summarize --force` into the same CourtListener session — avoids a second
        # run that would hit CourtListener's docket-entries endpoint all over again.
        cfg = yaml.safe_load(cfg_file.read_text())
        cfg["case_summaries"] = {"enabled": True}
        cfg_file.write_text(yaml.safe_dump(cfg))
        monkeypatch.setattr(cli.llm, "provider_info", lambda: "fake/model")
        monkeypatch.setattr(
            cli.CaseSyncer,
            "sync_case",
            lambda self, case: {
                "dockets_skipped": 0,
                "entries_seen": 0,
                "entries_processed": 0,
                "actions": 0,
                "verified": 0,
                "auto_passed": 0,
            },
        )
        from case_calendar import summary as summary_mod

        refresh_calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            summary_mod,
            "refresh_stale",
            lambda **kw: (refresh_calls.append(kw), {})[1],
        )
        monkeypatch.setattr(cli, "emit_calendars", lambda *a, **kw: {})

        args = Namespace(
            config=str(cfg_file),
            case=None,
            no_emit=False,
            force_summaries=True,
        )
        assert cmd_sync(args) == 0
        assert refresh_calls[0]["force"] is True

    def test_only_new_filters_to_unseen_dockets(
        self,
        tmp_path,
        fake_cl_ctx,
        monkeypatch,
        capsys,
    ):
        # `sync --only-new` skips cases whose dockets are already in the
        # store (the use case: you added new cases to config.yaml and
        # don't want to remember their ids).
        cfg = {
            "store_path": str(tmp_path / "x.sqlite"),
            "calendars": {
                "cyber": {
                    "name": "Cybercrime",
                    "ics_path": str(tmp_path / "cyber.ics"),
                },
            },
            "cases": [
                {
                    "id": "us-v-old",
                    "name": "US v. Old",
                    "calendar": "cyber",
                    "dockets": [100],
                },
                {
                    "id": "us-v-new",
                    "name": "US v. New",
                    "calendar": "cyber",
                    "dockets": [200],
                },
            ],
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg))

        # Pre-populate the store so docket 100 is "known" and 200 is not.
        # Different Store instances → must commit via tx() for the cmd_sync
        # connection to see the row.
        from case_calendar.store import Store

        pre_store = Store(cfg["store_path"])
        with pre_store.tx() as _:
            pre_store.set_docket_last_modified(100, "2026-01-01T00:00:00Z")
        pre_store.close()

        monkeypatch.setattr(cli.llm, "provider_info", lambda: "fake/model")
        synced_ids: list[str] = []
        monkeypatch.setattr(
            cli.CaseSyncer,
            "sync_case",
            lambda self, case: (
                synced_ids.append(case.case_id)
                or {
                    "dockets_skipped": 0,
                    "entries_seen": 0,
                    "entries_processed": 0,
                    "actions": 0,
                    "verified": 0,
                    "auto_passed": 0,
                }
            ),
        )
        monkeypatch.setattr(cli, "emit_calendars", lambda *a, **kw: {})

        args = Namespace(
            config=str(cfg_path),
            case=None,
            no_emit=False,
            only_new=True,
        )
        assert cmd_sync(args) == 0
        # Only the new case ran — the one with a docket already in the store
        # was filtered out before sync_case was called.
        assert synced_ids == ["us-v-new"]

    def test_only_new_with_no_new_cases_short_circuits(
        self,
        tmp_path,
        fake_cl_ctx,
        monkeypatch,
        capsys,
    ):
        # When every configured case's dockets are already known, --only-new
        # prints a friendly message and returns 0 without invoking sync_case
        # or emit_calendars at all.
        cfg = {
            "store_path": str(tmp_path / "x.sqlite"),
            "calendars": {
                "cyber": {
                    "name": "Cybercrime",
                    "ics_path": str(tmp_path / "cyber.ics"),
                },
            },
            "cases": [
                {
                    "id": "us-v-x",
                    "name": "US v. X",
                    "calendar": "cyber",
                    "dockets": [100],
                },
            ],
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg))
        from case_calendar.store import Store

        pre_store = Store(cfg["store_path"])
        with pre_store.tx() as _:
            pre_store.set_docket_last_modified(100, "2026-01-01T00:00:00Z")
        pre_store.close()

        sync_calls: list[str] = []
        emit_calls: list[Any] = []
        monkeypatch.setattr(
            cli.CaseSyncer,
            "sync_case",
            lambda self, case: sync_calls.append(case.case_id),
        )
        monkeypatch.setattr(
            cli,
            "emit_calendars",
            lambda *a, **kw: emit_calls.append(1) or {},
        )

        args = Namespace(
            config=str(cfg_path),
            case=None,
            no_emit=False,
            only_new=True,
        )
        assert cmd_sync(args) == 0
        assert sync_calls == []
        assert emit_calls == []
        out = capsys.readouterr().out
        assert "no new cases" in out


class TestCmdEmit:
    def test_runs_and_prints_each_backend(
        self,
        cfg_file,
        monkeypatch,
        capsys,
    ):
        # emit_calendars is itself well-tested; here we just confirm cmd_emit
        # wires it and prints the expected lines for ICS + gcal + M365.
        cfg = yaml.safe_load(cfg_file.read_text())
        cfg["calendars"]["cyber"]["google_calendar_id"] = (
            "abc@group.calendar.google.com"
        )
        cfg["calendars"]["cyber"]["m365_calendar_id"] = "AAMkADExAAA"
        cfg_file.write_text(yaml.safe_dump(cfg))

        monkeypatch.setattr(
            cli,
            "emit_calendars",
            lambda cfg, store: {
                "cyber": {
                    "events": 3,
                    "ics_path": "/tmp/cyber.ics",
                    "gcal_pushed": True,
                    "m365_pushed": True,
                }
            },
        )
        args = Namespace(config=str(cfg_file))
        assert cmd_emit(args) == 0
        out = capsys.readouterr().out
        assert "wrote 3 events" in out
        assert "pushed 3 events to gcal" in out
        assert "pushed 3 events to M365" in out


class TestCmdServe:
    def test_rejects_short_secret(self, cfg_file, monkeypatch, capsys):
        monkeypatch.setenv("CASE_CALENDAR_WEBHOOK_SECRET", "too-short")
        args = Namespace(config=str(cfg_file), host="127.0.0.1", port=8000)
        assert cmd_serve(args) == 2
        assert "WEBHOOK_SECRET" in capsys.readouterr().err

    def test_calls_serve_with_emit_fn(
        self,
        cfg_file,
        fake_cl_ctx,
        monkeypatch,
    ):
        # Patch serve() so we don't actually bind a socket; instead capture
        # the emit_fn callback and exercise it through one call to confirm
        # it wires emit_calendars + _arm_debounce.
        monkeypatch.setenv(
            "CASE_CALENDAR_WEBHOOK_SECRET",
            "this-is-a-sufficiently-long-secret",
        )
        monkeypatch.setattr(cli.llm, "provider_info", lambda: "fake/model")
        captured: dict[str, Any] = {}

        def _fake_serve(**kw):
            captured.update(kw)

        monkeypatch.setattr("case_calendar.serve.serve", _fake_serve)
        # Cover the gcal_pushed / m365_pushed log branches by returning both
        # flags set; that exercises lines 588-589 and 594-597.
        cfg = yaml.safe_load(cfg_file.read_text())
        cfg["calendars"]["cyber"]["google_calendar_id"] = (
            "abc@group.calendar.google.com"
        )
        cfg["calendars"]["cyber"]["m365_calendar_id"] = "AAMkADExAAA"
        cfg_file.write_text(yaml.safe_dump(cfg))
        monkeypatch.setattr(
            cli,
            "emit_calendars",
            lambda *a, **kw: {
                "cyber": {
                    "events": 1,
                    "ics_path": "/tmp/cyber.ics",
                    "gcal_pushed": True,
                    "m365_pushed": True,
                }
            },
        )

        args = Namespace(config=str(cfg_file), host="127.0.0.1", port=9000)
        assert cmd_serve(args) == 0
        assert captured["port"] == 9000
        # Invoke the wired emit_fn — must accept a calendar set.
        captured["emit_fn"]({"cyber"})

    def test_debounced_summary_refresh_fires(
        self,
        cfg_file,
        fake_cl_ctx,
        monkeypatch,
        tmp_path,
    ):
        # Wire case_summaries on, mark one docket's summary stale, then
        # invoke the captured emit_fn — that arms the debounce timer.
        # Replace threading.Timer with a fake so the callback runs inline
        # and we cover _arm_debounce + _fire_debounced_summary.
        cfg = yaml.safe_load(cfg_file.read_text())
        cfg["case_summaries"] = {"enabled": True, "debounce_seconds": 0.01}
        cfg_file.write_text(yaml.safe_dump(cfg))

        # Seed an existing stale summary row in the store. Needs the dockets
        # metadata so the debounce arm can resolve docket_id → group.
        s = cli.Store(cfg["store_path"])
        s.upsert_docket_meta(
            100,
            {
                "court_id": "mad",
                "docket_number": "1:25-cr-1",
                "case_name": "X",
                "absolute_url": "/x/",
            },
        )
        s.upsert_case_summary(
            "us-v-x",
            "1:25-cr-1",
            "mad",
            summary="old",
            model="m",
            source_entry_ids=[],
        )
        s.mark_summary_stale("us-v-x", "1:25-cr-1", "mad")
        s.conn.commit()
        s.close()

        monkeypatch.setenv(
            "CASE_CALENDAR_WEBHOOK_SECRET",
            "this-is-a-sufficiently-long-secret",
        )
        monkeypatch.setattr(cli.llm, "provider_info", lambda: "fake/model")

        from case_calendar import summary as summary_mod

        refresh_calls: list[Any] = []

        def _fake_refresh(**kw):
            refresh_calls.append(kw)
            return {"us-v-x": {100}}

        monkeypatch.setattr(summary_mod, "refresh_stale", _fake_refresh)

        # Fake Timer that runs the callback in a thread (not inline) so it
        # doesn't deadlock on the debounce_lock that _arm_debounce holds while
        # starting the timer. After cmd_serve returns we'll join() to make
        # _fire_debounced_summary observably complete.
        import threading

        threads: list[threading.Thread] = []

        class _FakeTimer:
            def __init__(self, interval, callback):
                self.callback = callback
                self.daemon = False
                self._thread = threading.Thread(target=callback)
                threads.append(self._thread)

            def start(self):
                self._thread.start()

            def cancel(self):
                pass

        monkeypatch.setattr("threading.Timer", _FakeTimer)

        # Invoke emit_fn from INSIDE the fake serve so the store is still
        # open when _fire_debounced_summary runs (cmd_serve closes the store
        # after serve() returns).
        def _fake_serve(**kw):
            kw["emit_fn"]({"cyber"})

        monkeypatch.setattr("case_calendar.serve.serve", _fake_serve)
        monkeypatch.setattr(
            cli,
            "emit_calendars",
            lambda *a, **kw: {
                "cyber": {
                    "events": 0,
                    "ics_path": None,
                    "gcal_pushed": False,
                    "m365_pushed": False,
                }
            },
        )

        args = Namespace(config=str(cfg_file), host="127.0.0.1", port=9000)

        # Override _fake_serve to wait for the debounce thread to finish
        # before serve() returns, so the store is still open when
        # _fire_debounced_summary runs.
        def _serve_and_wait(**kw):
            kw["emit_fn"]({"cyber"})
            for t in threads:
                t.join(timeout=5)

        monkeypatch.setattr("case_calendar.serve.serve", _serve_and_wait)

        assert cmd_serve(args) == 0
        # The fake timer fired _fire_debounced_summary; refresh_stale was
        # invoked with the calendar's scoped case set.
        assert len(refresh_calls) == 1
        assert refresh_calls[0]["only_case_ids"] == {"us-v-x"}

    def test_debounce_skips_when_no_stale_rows(
        self,
        cfg_file,
        fake_cl_ctx,
        monkeypatch,
    ):
        # case_summaries enabled but no docket is stale -> _arm_debounce
        # short-circuits and never starts the timer.
        cfg = yaml.safe_load(cfg_file.read_text())
        cfg["case_summaries"] = {"enabled": True}
        cfg_file.write_text(yaml.safe_dump(cfg))
        monkeypatch.setenv(
            "CASE_CALENDAR_WEBHOOK_SECRET",
            "this-is-a-sufficiently-long-secret",
        )
        monkeypatch.setattr(cli.llm, "provider_info", lambda: "fake/model")

        # Pre-seed a NON-stale summary, so is_summary_stale returns False.
        s = cli.Store(yaml.safe_load(cfg_file.read_text())["store_path"])
        s.upsert_docket_meta(
            100,
            {
                "court_id": "mad",
                "docket_number": "1:25-cr-1",
                "case_name": "X",
                "absolute_url": "/x/",
            },
        )
        s.upsert_case_summary(
            "us-v-x",
            "1:25-cr-1",
            "mad",
            summary="x",
            model="m",
            source_entry_ids=[],
        )
        s.conn.commit()
        s.close()

        timers: list[Any] = []

        class _FakeTimer:
            def __init__(self, *a, **k):
                timers.append(self)

            def start(self):
                timers.append("started")

            def cancel(self):
                pass

        monkeypatch.setattr("threading.Timer", _FakeTimer)

        def _fake_serve(**kw):
            kw["emit_fn"]({"cyber"})

        monkeypatch.setattr("case_calendar.serve.serve", _fake_serve)
        monkeypatch.setattr(
            cli,
            "emit_calendars",
            lambda *a, **kw: {
                "cyber": {
                    "events": 0,
                    "ics_path": None,
                    "gcal_pushed": False,
                    "m365_pushed": False,
                }
            },
        )

        args = Namespace(config=str(cfg_file), host="127.0.0.1", port=9000)
        assert cmd_serve(args) == 0
        # No Timer was created because no stale row existed.
        assert timers == []

    def test_debounce_arm_handles_missing_meta_and_sibling_dockets(
        self,
        cfg_file,
        fake_cl_ctx,
        monkeypatch,
    ):
        # _arm_debounce iterates each docket on the affected calendars to
        # decide whether to start the summary-refresh timer. PR #3's group
        # dedup added two `continue` branches that weren't being hit:
        #
        #   1. Docket id in the case's list but with no `dockets` metadata
        #      (sync interrupted before upsert_docket_meta, or operator
        #      added a docket that hasn't been synced yet) — skipped via
        #      ``if not docket_number or not court_id: continue``.
        #   2. Sibling CL docket_ids on the same logical PACER docket
        #      (the Akhter shape) — the second sibling skips via
        #      ``if group_key in seen_groups: continue`` so we don't
        #      double-query `is_summary_stale` for the same logical
        #      docket.
        #
        # Expand the configured case to cover both: dockets=[100, 101, 999]
        # where 100 and 101 share `(1:25-cr-1, mad)` (sibling) and 999 has
        # no metadata at all. None of them are stale, so any_stale stays
        # False and the timer never arms — we're not testing the timer,
        # we're confirming the two `continue` branches don't crash and
        # the function reaches the `if not any_stale: return` path with
        # the same answer it would have given on the single-docket case.
        cfg = yaml.safe_load(cfg_file.read_text())
        cfg["case_summaries"] = {"enabled": True}
        cfg["cases"][0]["dockets"] = [100, 101, 999]
        cfg_file.write_text(yaml.safe_dump(cfg))
        monkeypatch.setenv(
            "CASE_CALENDAR_WEBHOOK_SECRET",
            "this-is-a-sufficiently-long-secret",
        )
        monkeypatch.setattr(cli.llm, "provider_info", lambda: "fake/model")

        s = cli.Store(yaml.safe_load(cfg_file.read_text())["store_path"])
        for did in (100, 101):
            s.upsert_docket_meta(
                did,
                {
                    "court_id": "mad",
                    "docket_number": "1:25-cr-1",
                    "case_name": "X",
                    "absolute_url": f"/x/{did}/",
                },
            )
        # NON-stale summary so any_stale stays False — the two `continue`
        # branches fire on the second sibling and the no-meta docket
        # before the loop ends naturally.
        s.upsert_case_summary(
            "us-v-x",
            "1:25-cr-1",
            "mad",
            summary="existing",
            model="m",
            source_entry_ids=[],
        )
        s.conn.commit()
        s.close()

        timers: list[Any] = []

        class _FakeTimer:
            def __init__(self, *a, **k):
                timers.append(self)

            def start(self):
                timers.append("started")

            def cancel(self):
                pass

        monkeypatch.setattr("threading.Timer", _FakeTimer)

        def _fake_serve(**kw):
            kw["emit_fn"]({"cyber"})

        monkeypatch.setattr("case_calendar.serve.serve", _fake_serve)
        monkeypatch.setattr(
            cli,
            "emit_calendars",
            lambda *a, **kw: {
                "cyber": {
                    "events": 0,
                    "ics_path": None,
                    "gcal_pushed": False,
                    "m365_pushed": False,
                }
            },
        )

        args = Namespace(config=str(cfg_file), host="127.0.0.1", port=9000)
        assert cmd_serve(args) == 0
        # No timer created — the loop traversed every docket including
        # the sibling-dedup `continue` and the no-meta `continue`, then
        # returned early because any_stale stayed False.
        assert timers == []


class TestCmdSetup:
    def test_gcal_without_credentials_path_errors(
        self,
        cfg_file,
        monkeypatch,
        capsys,
    ):
        args = Namespace(config=str(cfg_file), backend="gcal")
        assert cmd_setup(args) == 2
        assert "google_credentials_path" in capsys.readouterr().err

    def test_gcal_runs_constructor(self, cfg_file, tmp_path, monkeypatch, capsys):
        cfg = yaml.safe_load(cfg_file.read_text())
        cfg["google_credentials_path"] = "/c.json"
        cfg["google_token_path"] = str(tmp_path / "tok.json")
        cfg_file.write_text(yaml.safe_dump(cfg))
        invoked: list[dict[str, Any]] = []

        def _factory(*, credentials_path, token_path):
            invoked.append({"creds": credentials_path, "tok": token_path})

        monkeypatch.setattr(
            "case_calendar.calendars.gcal.GoogleCalendarSync",
            _factory,
        )
        args = Namespace(config=str(cfg_file), backend="gcal")
        assert cmd_setup(args) == 0
        assert invoked[0]["creds"] == "/c.json"
        assert "gcal token staged" in capsys.readouterr().out

    def test_m365_without_client_id_errors(self, cfg_file, monkeypatch, capsys):
        monkeypatch.delenv("M365_CLIENT_ID", raising=False)
        args = Namespace(config=str(cfg_file), backend="m365")
        assert cmd_setup(args) == 2
        assert "m365_client_id" in capsys.readouterr().err

    def test_m365_runs_constructor(self, cfg_file, tmp_path, monkeypatch, capsys):
        cfg = yaml.safe_load(cfg_file.read_text())
        cfg["m365_client_id"] = "00000000-0000-0000-0000-000000000000"
        cfg["m365_token_path"] = str(tmp_path / "m.json")
        cfg_file.write_text(yaml.safe_dump(cfg))
        invoked: list[dict[str, Any]] = []

        def _factory(*, client_id, token_path):
            invoked.append({"id": client_id, "tok": token_path})

        monkeypatch.setattr(
            "case_calendar.calendars.m365.M365CalendarSync",
            _factory,
        )
        args = Namespace(config=str(cfg_file), backend="m365")
        assert cmd_setup(args) == 0
        assert "m365 auth record staged" in capsys.readouterr().out


class TestCmdSummarize:
    def test_requires_case_summaries_enabled(
        self,
        cfg_file,
        monkeypatch,
        capsys,
    ):
        args = Namespace(
            config=str(cfg_file),
            case=None,
            force=False,
            no_emit=False,
        )
        assert cmd_summarize(args) == 2
        assert "case_summaries.enabled" in capsys.readouterr().err

    def test_unknown_case_id_returns_2(self, cfg_file, monkeypatch, capsys):
        cfg = yaml.safe_load(cfg_file.read_text())
        cfg["case_summaries"] = {"enabled": True}
        cfg_file.write_text(yaml.safe_dump(cfg))
        monkeypatch.setattr(cli, "CourtListener", lambda *a, **kw: MagicMock())
        monkeypatch.setattr(cli.llm, "provider_info", lambda: "fake/model")
        args = Namespace(
            config=str(cfg_file),
            case="nope",
            force=False,
            no_emit=False,
        )
        assert cmd_summarize(args) == 2

    def test_runs_summarize_case_and_emits(
        self,
        cfg_file,
        fake_cl_ctx,
        monkeypatch,
        capsys,
    ):
        cfg = yaml.safe_load(cfg_file.read_text())
        cfg["case_summaries"] = {"enabled": True, "provider": "anthropic"}
        cfg_file.write_text(yaml.safe_dump(cfg))
        monkeypatch.setattr(cli.llm, "provider_info", lambda: "fake/model")

        from case_calendar import summary as summary_mod

        summarize_calls: list[dict[str, Any]] = []

        def _fake(**kw):
            summarize_calls.append(kw)
            return [
                {
                    "docket_number": "1:25-cr-1",
                    "court_id": "mad",
                    "summary": "x" * 42,
                    "model": "m",
                }
            ]

        monkeypatch.setattr(summary_mod, "summarize_case", _fake)
        emit_calls: list[Any] = []
        monkeypatch.setattr(
            cli,
            "emit_calendars",
            lambda *a, **kw: emit_calls.append(kw.get("only_calendars")) or {},
        )
        args = Namespace(
            config=str(cfg_file),
            case=None,
            force=True,
            no_emit=False,
        )
        assert cmd_summarize(args) == 0
        assert summarize_calls[0]["force"] is True
        assert emit_calls == [{"cyber"}]


class TestCmdShow:
    def test_dumps_hearings_and_deadlines(self, cfg_file, capsys, store, monkeypatch):
        # Open a fresh store at the configured path and seed it before
        # cmd_show creates its own Store handle (which uses the same file).
        cfg = yaml.safe_load(cfg_file.read_text())
        s = cli.Store(cfg["store_path"])
        s.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "k1",
                "title": "Sentencing",
                "starts_at_utc": "2099-01-01T00:00:00+00:00",
                "duration_minutes": 60,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [1],
                "location": "Courtroom 1",
                "judge": "Hon. Smith",
                "dial_in": "tel:555-1234",
            }
        )
        s.upsert_deadline(
            {
                "case_id": "us-v-x",
                "deadline_key": "d1",
                "title": "Reply ISO MTD",
                "due_at_utc": "2099-01-15T22:00:00+00:00",
                "timezone": "America/New_York",
                "status": "pending",
                "significance": "major",
                "deadline_type": "reply",
                "docket_id": 100,
                "source_entry_ids": [1],
            }
        )
        # upsert_hearing / upsert_deadline don't wrap in self.tx(); the
        # caller in sync.py shares one Store handle for the lifetime of the
        # process so commit-on-close isn't needed there. cmd_show opens a
        # second handle, so commit before close to make the rows visible.
        s.conn.commit()
        s.close()

        args = Namespace(config=str(cfg_file), case=None)
        assert cmd_show(args) == 0
        out = capsys.readouterr().out
        assert "Sentencing" in out and "Reply ISO MTD" in out
        assert "Courtroom 1" in out
        assert "tel:555-1234" in out

    def test_case_filter_limits_to_one_case(self, cfg_file, capsys):
        args = Namespace(config=str(cfg_file), case="other-case")
        assert cmd_show(args) == 0
        assert capsys.readouterr().out == ""


class TestCmdPrune:
    def _seed_two_dockets(self, store_path: str) -> None:
        # Seed the store with two dockets — one referenced by config, one not.
        # Config (fixture above) has dockets=[100] only, so 200 is the orphan.
        s = cli.Store(store_path)
        for did, key in ((100, "k100"), (200, "k200")):
            s.upsert_docket_meta(
                did,
                {
                    "court_id": "dcd",
                    "docket_number": f"1:24-cr-{did:05d}",
                    "case_name": f"US v. Docket {did}",
                    "absolute_url": None,
                    "date_last_filing": None,
                },
            )
            s.mark_entry(
                docket_id=did,
                entry_id=did,
                date_modified="2026-01-01T00:00:00+00:00",
                fingerprint="fp",
                entry_number=1,
                date_filed="2026-01-01",
                description="x",
                short_description="x",
                recap_documents=[],
            )
            s.upsert_hearing(
                {
                    "case_id": "us-v-x",
                    "hearing_key": key,
                    "title": "Sentencing",
                    "starts_at_utc": "2099-01-01T00:00:00+00:00",
                    "duration_minutes": 60,
                    "timezone": "America/New_York",
                    "status": "scheduled",
                    "significance": "major",
                    "docket_id": did,
                    "source_entry_ids": [did],
                }
            )
        s.conn.commit()
        s.close()

    def test_no_orphans_prints_clean_message(self, cfg_file, capsys):
        # Only docket 100, which is in config — no orphans to remove.
        cfg = yaml.safe_load(cfg_file.read_text())
        s = cli.Store(cfg["store_path"])
        s.upsert_docket_meta(
            100,
            {
                "court_id": "dcd",
                "docket_number": "1:24-cr-00100",
                "case_name": "US v. X",
                "absolute_url": None,
                "date_last_filing": None,
            },
        )
        s.conn.commit()
        s.close()
        args = Namespace(config=str(cfg_file), apply=False)
        assert cli.cmd_prune(args) == 0
        out = capsys.readouterr().out
        assert "No orphan dockets" in out
        assert "1 docket" in out

    def test_dry_run_lists_orphans_without_deleting(self, cfg_file, capsys):
        cfg = yaml.safe_load(cfg_file.read_text())
        self._seed_two_dockets(cfg["store_path"])
        args = Namespace(config=str(cfg_file), apply=False)
        assert cli.cmd_prune(args) == 0
        out = capsys.readouterr().out
        # Plan surfaces docket_id, label, and per-table counts.
        assert "Found 1 orphan docket" in out
        assert "docket_id=200" in out
        assert "1:24-cr-00200" in out
        assert "US v. Docket 200" in out
        # Per-table counts: every populated table on the orphan side.
        assert "entries=1" in out
        assert "hearings=1" in out
        assert "dockets=1" in out
        assert "Dry run" in out
        # docket 100 (in config) is NOT in the plan.
        assert "docket_id=100" not in out
        # Verify nothing was actually deleted.
        s = cli.Store(cfg["store_path"])
        try:
            assert sorted(s.list_all_docket_ids()) == [100, 200]
        finally:
            s.close()

    def test_apply_deletes_orphan_rows(self, cfg_file, capsys):
        cfg = yaml.safe_load(cfg_file.read_text())
        self._seed_two_dockets(cfg["store_path"])
        args = Namespace(config=str(cfg_file), apply=True)
        assert cli.cmd_prune(args) == 0
        out = capsys.readouterr().out
        assert "Deleting" in out
        assert "deleted 3 rows" in out  # entries + hearings + dockets
        # Re-open store and confirm orphan is gone, in-config docket survives.
        s = cli.Store(cfg["store_path"])
        try:
            assert s.list_all_docket_ids() == [100]
            assert s.count_docket_rows(200) == {
                "entries": 0,
                "hearings": 0,
                "deadlines": 0,
                "case_summaries": 0,
                "dockets": 0,
            }
        finally:
            s.close()

    def test_child_only_orphan_surfaces_in_plan_with_no_metadata_label(
        self,
        cfg_file,
        capsys,
    ):
        # An entry whose docket row was never written. Plan should list it
        # under the "<no metadata>" label rather than crashing on the
        # missing dockets row.
        cfg = yaml.safe_load(cfg_file.read_text())
        s = cli.Store(cfg["store_path"])
        s.mark_entry(
            docket_id=999,
            entry_id=999,
            date_modified="2026-01-01T00:00:00+00:00",
            fingerprint="fp",
            entry_number=1,
            date_filed="2026-01-01",
            description="x",
            short_description="x",
            recap_documents=[],
        )
        s.conn.commit()
        s.close()
        args = Namespace(config=str(cfg_file), apply=False)
        assert cli.cmd_prune(args) == 0
        out = capsys.readouterr().out
        assert "docket_id=999" in out
        assert "<no metadata>" in out


class TestCmdWebhookUrl:
    def test_missing_secret_returns_2(self, monkeypatch, capsys):
        # The conftest autouse fixture already strips any inherited secret,
        # but be explicit so this test reads cleanly in isolation.
        monkeypatch.delenv("CASE_CALENDAR_WEBHOOK_SECRET", raising=False)
        args = Namespace(host=None, check=False)
        assert cli.cmd_webhook_url(args) == 2
        err = capsys.readouterr().err
        assert "CASE_CALENDAR_WEBHOOK_SECRET" in err

    def test_no_host_prints_placeholder_url_and_stderr_hint(
        self,
        monkeypatch,
        capsys,
    ):
        monkeypatch.setenv("CASE_CALENDAR_WEBHOOK_SECRET", "abc123")
        args = Namespace(host=None, check=False)
        assert cli.cmd_webhook_url(args) == 0
        out = capsys.readouterr()
        assert out.out.strip() == (
            "https://<your-public-host>/webhooks/case-calendar/abc123"
        )
        # The stderr hint nudges the user toward --host so they know the
        # output isn't pasteable as-is.
        assert "--host" in out.err

    def test_host_without_scheme_gets_https(self, monkeypatch, capsys):
        monkeypatch.setenv("CASE_CALENDAR_WEBHOOK_SECRET", "abc123")
        args = Namespace(host="webhook.example.com", check=False)
        assert cli.cmd_webhook_url(args) == 0
        out = capsys.readouterr()
        assert out.out.strip() == (
            "https://webhook.example.com/webhooks/case-calendar/abc123"
        )
        # No `--host` hint when --host is supplied — the URL is ready
        # to paste — but the sensitive-data banner still fires so the
        # operator knows not to paste this into bug reports / chat.
        assert "--host" not in out.err
        assert "sensitive" in out.err

    def test_explicit_scheme_respected(self, monkeypatch, capsys):
        # Useful for local curl testing against the receiver on 127.0.0.1.
        monkeypatch.setenv("CASE_CALENDAR_WEBHOOK_SECRET", "abc123")
        args = Namespace(host="http://localhost:8000", check=False)
        assert cli.cmd_webhook_url(args) == 0
        assert capsys.readouterr().out.strip() == (
            "http://localhost:8000/webhooks/case-calendar/abc123"
        )

    def test_trailing_slash_on_host_normalized(self, monkeypatch, capsys):
        monkeypatch.setenv("CASE_CALENDAR_WEBHOOK_SECRET", "abc123")
        args = Namespace(host="https://webhook.example.com/", check=False)
        assert cli.cmd_webhook_url(args) == 0
        # No double slash before the /webhooks/ prefix.
        assert capsys.readouterr().out.strip() == (
            "https://webhook.example.com/webhooks/case-calendar/abc123"
        )

    def test_wired_through_main(self, monkeypatch, cfg_file, capsys):
        # main() dispatches the webhook-url subcommand. The --config flag is
        # accepted for consistency with other subcommands even though this
        # one doesn't read the config.
        monkeypatch.setenv("CASE_CALENDAR_WEBHOOK_SECRET", "abc123")
        assert (
            main(
                [
                    "-c",
                    str(cfg_file),
                    "webhook-url",
                    "--host",
                    "webhook.example.com",
                ]
            )
            == 0
        )
        assert capsys.readouterr().out.strip() == (
            "https://webhook.example.com/webhooks/case-calendar/abc123"
        )

    def test_check_without_host_returns_2(self, monkeypatch, capsys):
        # --check needs an actual host to probe; refuse rather than emit a
        # bogus placeholder URL.
        monkeypatch.setenv("CASE_CALENDAR_WEBHOOK_SECRET", "abc123")
        args = Namespace(host=None, check=True)
        assert cli.cmd_webhook_url(args) == 2
        assert "--host" in capsys.readouterr().err

    def test_check_happy_path(self, monkeypatch, capsys):
        # Receiver answers the expected JSON; --check prints OK and exits 0.
        monkeypatch.setenv("CASE_CALENDAR_WEBHOOK_SECRET", "abc123")
        seen: list[str] = []

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return (
                    b'{"status":"ok","service":"case-calendar",'
                    b'"tracking":{"dockets":3,"cases":2}}'
                )

        def _fake_urlopen(req, timeout=10):
            seen.append(req.full_url)
            return _Resp()

        monkeypatch.setattr(
            "urllib.request.urlopen",
            _fake_urlopen,
        )
        args = Namespace(host="webhook.example.com", check=True)
        assert cli.cmd_webhook_url(args) == 0
        out = capsys.readouterr().out
        assert "health check OK" in out
        assert "3 dockets" in out and "2 cases" in out
        assert seen == [
            "https://webhook.example.com/webhooks/case-calendar/abc123/health"
        ]

    def test_check_wrong_secret_403(self, monkeypatch, capsys):
        # 403 from origin = secret mismatch; surface it loudly.
        # Use a long-enough secret that incidental short-string overlap
        # with diagnostic text is unlikely; assert the secret is absent
        # from the operator-facing error (the URL is replaced with a
        # generic endpoint label that doesn't flow from the secret —
        # CodeQL's data-flow analysis required severing the chain
        # entirely rather than masking via `.replace()`).
        monkeypatch.setenv("CASE_CALENDAR_WEBHOOK_SECRET", "secret-abc123-do-not-leak")

        def _fake_urlopen(req, timeout=10):
            raise urllib.error.HTTPError(
                req.full_url,
                403,
                "Forbidden",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(b'{"error":"forbidden"}'),
            )

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        args = Namespace(host="webhook.example.com", check=True)
        assert cli.cmd_webhook_url(args) == 1
        err = capsys.readouterr().err
        assert "HTTP 403" in err
        # Diagnostic must NOT contain the secret — operators may copy
        # this into bug reports / chat.
        assert "secret-abc123-do-not-leak" not in err
        # Diagnostic uses a non-sensitive endpoint label rather than the
        # secret-bearing URL — CodeQL's data-flow analysis flags any
        # string derived from the secret, so the diagnostic was severed
        # entirely from the secret-bearing URL chain.
        assert "webhook health endpoint" in err

    def test_check_unreachable_host(self, monkeypatch, capsys):
        # DNS failure / connection refused — print the reason and exit 1.
        monkeypatch.setenv("CASE_CALENDAR_WEBHOOK_SECRET", "secret-abc123-do-not-leak")

        def _fake_urlopen(req, timeout=10):
            raise urllib.error.URLError("nodename nor servname known")

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        args = Namespace(host="bogus.invalid", check=True)
        assert cli.cmd_webhook_url(args) == 1
        err = capsys.readouterr().err
        assert "cannot reach" in err
        assert "nodename" in err
        assert "secret-abc123-do-not-leak" not in err
        # Diagnostic uses a non-sensitive endpoint label rather than the
        # secret-bearing URL — CodeQL's data-flow analysis flags any
        # string derived from the secret, so the diagnostic was severed
        # entirely from the secret-bearing URL chain.
        assert "webhook health endpoint" in err

    def test_check_non_200_no_exception_path(self, monkeypatch, capsys):
        # urlopen returns a Response object with status != 200 WITHOUT
        # raising an HTTPError — possible on a custom proxy that
        # rewrites status codes, or a misconfigured Caddy returning a
        # bare 301 from the receiver path. The status-check branch
        # after the try/except surfaces the failure too.
        monkeypatch.setenv("CASE_CALENDAR_WEBHOOK_SECRET", "secret-abc123-do-not-leak")

        class _Resp:
            status = 301

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"redirect to login"

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=10: _Resp())
        args = Namespace(host="webhook.example.com", check=True)
        assert cli.cmd_webhook_url(args) == 1
        err = capsys.readouterr().err
        assert "HTTP 301" in err
        assert "redirect to login" in err
        assert "secret-abc123-do-not-leak" not in err
        # Diagnostic uses a non-sensitive endpoint label rather than the
        # secret-bearing URL — CodeQL's data-flow analysis flags any
        # string derived from the secret, so the diagnostic was severed
        # entirely from the secret-bearing URL chain.
        assert "webhook health endpoint" in err

    def test_check_200_empty_body_is_failure(self, monkeypatch, capsys):
        # This is the Cloudflare-intercept signature we ran into in
        # production — 200 with an empty body. The check has to flag it,
        # not silently call it healthy.
        monkeypatch.setenv("CASE_CALENDAR_WEBHOOK_SECRET", "secret-abc123-do-not-leak")

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b""

        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout=10: _Resp(),
        )
        args = Namespace(host="webhook.example.com", check=True)
        assert cli.cmd_webhook_url(args) == 1
        err = capsys.readouterr().err
        assert "non-JSON" in err
        assert "secret-abc123-do-not-leak" not in err
        # Diagnostic uses a non-sensitive endpoint label rather than the
        # secret-bearing URL — CodeQL's data-flow analysis flags any
        # string derived from the secret, so the diagnostic was severed
        # entirely from the secret-bearing URL chain.
        assert "webhook health endpoint" in err

    def test_check_200_wrong_service_is_failure(self, monkeypatch, capsys):
        # A 200 with valid JSON but missing/wrong "service" marker = the
        # request reached something, but not us.
        monkeypatch.setenv("CASE_CALENDAR_WEBHOOK_SECRET", "secret-abc123-do-not-leak")

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"status":"ok"}'

        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout=10: _Resp(),
        )
        args = Namespace(host="webhook.example.com", check=True)
        assert cli.cmd_webhook_url(args) == 1
        err = capsys.readouterr().err
        assert "doesn't identify as case-calendar" in err
        assert "secret-abc123-do-not-leak" not in err
        # Diagnostic uses a non-sensitive endpoint label rather than the
        # secret-bearing URL — CodeQL's data-flow analysis flags any
        # string derived from the secret, so the diagnostic was severed
        # entirely from the secret-bearing URL chain.
        assert "webhook health endpoint" in err

    def test_check_redacts_secret_echoed_in_response_body(self, monkeypatch, capsys):
        # A misconfigured proxy or upstream may echo the request URL back
        # in its response body (e.g. a 403 page that includes the path).
        # The body is shown to the operator in the FAILED message, so we
        # must redact the secret from the body too — not just from the
        # rendered URL.
        secret = "secret-abc123-do-not-leak"
        monkeypatch.setenv("CASE_CALENDAR_WEBHOOK_SECRET", secret)

        def _fake_urlopen(req, timeout=10):
            raise urllib.error.HTTPError(
                req.full_url,
                403,
                "Forbidden",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(
                    f'{{"error":"forbidden","path":"/webhooks/case-calendar/{secret}/health"}}'.encode()
                ),
            )

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        args = Namespace(host="webhook.example.com", check=True)
        assert cli.cmd_webhook_url(args) == 1
        err = capsys.readouterr().err
        assert secret not in err
        # The body still surfaces (so the operator can debug), just with
        # the secret swapped out.
        assert "forbidden" in err


class TestRedactSecret:
    """Direct coverage of `cli._redact_secret`.

    The helper is exercised end-to-end by the health-check failure
    tests above, but the empty-secret short-circuit branch isn't hit
    by those tests (every fixture sets a non-empty secret). Pin it
    here so future refactors don't silently drop the guard.
    """

    def test_empty_secret_returns_text_unchanged(self):
        # The defensive guard: when no secret is configured, redaction
        # is a no-op. Without this branch a caller passing `secret=""`
        # would try `text.replace("", "<REDACTED>")`, which str.replace
        # treats as inserting between every character.
        assert cli._redact_secret("hello world", "") == "hello world"

    def test_replaces_every_occurrence(self):
        # Idempotent multi-occurrence replace — bodies that echo the
        # URL multiple times still get cleaned.
        secret = "topsecret"
        out = cli._redact_secret(f"path/{secret}/and/{secret}/again", secret)
        assert secret not in out
        assert out.count("<REDACTED>") == 2


class TestMain:
    def test_dispatches_to_subcommand(self, cfg_file, monkeypatch):
        calls: list[argparse.Namespace] = []

        def _spy(ns):
            calls.append(ns)
            return 0

        monkeypatch.setattr(cli, "cmd_show", _spy)
        assert main(["-c", str(cfg_file), "show"]) == 0
        assert calls and calls[0].cmd == "show"

    def test_requires_subcommand(self, cfg_file):
        with pytest.raises(SystemExit):
            main(["-c", str(cfg_file)])


class TestEmitCalendarsCoverage:
    """Edge-case branches in emit_calendars not covered by TestEmitCalendars
    above. Each focuses on a single guard that the headline tests skip past
    because they always populate the relevant field."""

    @pytest.fixture
    def cfg(self, tmp_path):
        return {
            "store_path": str(tmp_path / "x.sqlite"),
            "calendars": {
                "cyber": {
                    "name": "Cybercrime",
                    "ics_path": str(tmp_path / "cyber.ics"),
                },
            },
            "cases": [
                {
                    "id": "us-v-x",
                    "name": "US v. X",
                    "calendar": "cyber",
                    "dockets": [100],
                },
            ],
        }

    def test_valid_deadline_renders_into_calendar(self, store, cfg):
        # The True branch of _deadline_to_hearing's not-None check: a
        # pending deadline with a real due_at_utc gets mapped and rendered.
        store.upsert_deadline(
            {
                "case_id": "us-v-x",
                "deadline_key": "reply",
                "title": "Reply ISO MTD",
                "due_at_utc": "2099-05-24T22:00:00+00:00",
                "timezone": "America/New_York",
                "status": "pending",
                "significance": "major",
                "deadline_type": "reply",
                "docket_id": 100,
                "source_entry_ids": [],
            }
        )
        results = emit_calendars(cfg, store)
        assert results["cyber"]["events"] == 1
        text = Path(results["cyber"]["ics_path"]).read_text()
        assert "Reply ISO MTD" in text

    def test_docket_with_court_id_pulls_citation(self, store, cfg):
        # The True branch of `if court_id:` — citation lookup fires when
        # the docket's cached court_id is non-empty.
        store.upsert_docket_meta(
            100,
            {
                "court_id": "mad",
                "docket_number": "1:25-cr-1",
                "case_name": "X",
                "absolute_url": "/x/",
            },
        )
        store.upsert_court(
            "mad",
            "D. Mass.",
            "mad",
            "District of Massachusetts",
        )
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "h1",
                "title": "Sentencing",
                "starts_at_utc": "2099-04-14T15:00:00+00:00",
                "duration_minutes": 90,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [],
            }
        )
        results = emit_calendars(cfg, store)
        text = Path(results["cyber"]["ics_path"]).read_text()
        # The citation makes it into the description body.
        assert "D. Mass." in text

    def test_deadline_without_due_timestamp_is_skipped(self, store, cfg):
        # A pending deadline without a due_at_utc value maps to None via
        # _deadline_to_hearing; the row is dropped at emit time so the
        # ICS doesn't get a date-less event.
        store.upsert_deadline(
            {
                "case_id": "us-v-x",
                "deadline_key": "d-no-date",
                "title": "Pending unscoped",
                "due_at_utc": None,
                "timezone": "America/New_York",
                "status": "pending",
                "significance": "major",
                "deadline_type": "response",
                "docket_id": 100,
                "source_entry_ids": [],
            }
        )
        results = emit_calendars(cfg, store)
        assert results["cyber"]["events"] == 0  # the deadline didn't render
        text = Path(results["cyber"]["ics_path"]).read_text()
        assert "d-no-date" not in text

    def test_hearing_without_docket_id_renders_without_decoration(self, store, cfg):
        # Legacy row pre-dating the docket_id column: the docket lookup is
        # skipped entirely (no docket_number / court citation in the body).
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "legacy",
                "title": "Sentencing",
                "starts_at_utc": "2099-04-14T15:00:00+00:00",
                "duration_minutes": 90,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": None,
                "source_entry_ids": [],
            }
        )
        results = emit_calendars(cfg, store)
        assert results["cyber"]["events"] == 1

    def test_docket_without_court_id_skips_citation_lookup(self, store, cfg):
        # Docket meta cached but its court_id is empty -> the renderer
        # skips get_court_citation entirely. (No assertion needed beyond
        # "doesn't crash"; the branch coverage flag is what we're after.)
        store.upsert_docket_meta(
            100,
            {
                "court_id": "",  # the field under test
                "docket_number": "1:25-cr-1",
                "case_name": "X",
                "absolute_url": "/x/",
            },
        )
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "h1",
                "title": "Sentencing",
                "starts_at_utc": "2099-04-14T15:00:00+00:00",
                "duration_minutes": 90,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [],
            }
        )
        results = emit_calendars(cfg, store)
        assert results["cyber"]["events"] == 1

    def test_notify_emails_and_reminders_attach_to_hearing(self, store, cfg):
        # Configure both notify_emails and reminders at the calendar level
        # so the True side of each guard fires.
        cfg["calendars"]["cyber"]["notify_emails"] = ["a@example.com"]
        cfg["calendars"]["cyber"]["reminders"] = [{"method": "popup", "minutes": 30}]
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "h1",
                "title": "Sentencing",
                "starts_at_utc": "2099-04-14T15:00:00+00:00",
                "duration_minutes": 90,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [],
            }
        )
        results = emit_calendars(cfg, store)
        text = Path(results["cyber"]["ics_path"]).read_text()
        # ATTENDEE for the notify_email, VALARM for the reminder.
        assert "ATTENDEE" in text and "a@example.com" in text
        assert "VALARM" in text

    def test_calendar_without_ics_path_writes_no_file(self, store, tmp_path):
        # A push-only calendar (no ics_path) skips the ICS write but the
        # rest of the emit cycle still runs.
        cfg = {
            "store_path": str(tmp_path / "x.sqlite"),
            "calendars": {
                "cyber": {"name": "Cybercrime"},  # no ics_path
            },
            "cases": [
                {
                    "id": "us-v-x",
                    "name": "US v. X",
                    "calendar": "cyber",
                    "dockets": [100],
                },
            ],
        }
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "h1",
                "title": "Sentencing",
                "starts_at_utc": "2099-04-14T15:00:00+00:00",
                "duration_minutes": 90,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [],
            }
        )
        results = emit_calendars(cfg, store)
        assert results["cyber"]["ics_path"] is None
        # No files written under tmp_path's calendar dir.
        assert not list(tmp_path.glob("*.ics"))

    def test_gcal_and_m365_clients_reuse_across_calendars(
        self, store, tmp_path, monkeypatch
    ):
        # Two calendars both opting into both push backends: each backend
        # client must be constructed at most ONCE per emit pass and reused
        # for the second calendar.
        gtoken = tmp_path / "google-token.json"
        gtoken.write_text("{}")
        mtoken = tmp_path / "m365-token.json"
        mtoken.write_text("{}")
        cfg = {
            "store_path": str(tmp_path / "x.sqlite"),
            "google_credentials_path": "/nonexistent.json",
            "google_token_path": str(gtoken),
            "m365_client_id": "00000000-0000-0000-0000-000000000000",
            "m365_token_path": str(mtoken),
            "calendars": {
                "cyber": {
                    "name": "Cyber",
                    "ics_path": str(tmp_path / "c.ics"),
                    "google_calendar_id": "g1@group.calendar.google.com",
                    "m365_calendar_id": "AAMkADExAAA",
                },
                "tech": {
                    "name": "Tech",
                    "ics_path": str(tmp_path / "t.ics"),
                    "google_calendar_id": "g2@group.calendar.google.com",
                    "m365_calendar_id": "AAMkADTechAAA",
                },
            },
            "cases": [
                {
                    "id": "us-v-x",
                    "name": "US v. X",
                    "calendar": "cyber",
                    "dockets": [100],
                },
                {
                    "id": "acme",
                    "name": "Acme",
                    "calendar": "tech",
                    "dockets": [200],
                },
            ],
        }
        gcs_instances: list[MagicMock] = []
        m365_instances: list[MagicMock] = []

        def _gcs_factory(*a, **kw):
            inst = MagicMock(name="GoogleCalendarSync")
            gcs_instances.append(inst)
            return inst

        def _m365_factory(*a, **kw):
            inst = MagicMock(name="M365CalendarSync")
            m365_instances.append(inst)
            return inst

        monkeypatch.setattr(
            "case_calendar.calendars.gcal.GoogleCalendarSync", _gcs_factory
        )
        monkeypatch.setattr(
            "case_calendar.calendars.m365.M365CalendarSync", _m365_factory
        )
        monkeypatch.delenv("M365_CLIENT_ID", raising=False)
        for case_id, docket_id in (("us-v-x", 100), ("acme", 200)):
            store.upsert_hearing(
                {
                    "case_id": case_id,
                    "hearing_key": f"h-{case_id}",
                    "title": "Sentencing",
                    "starts_at_utc": "2099-04-14T15:00:00+00:00",
                    "duration_minutes": 90,
                    "timezone": "America/New_York",
                    "status": "scheduled",
                    "significance": "major",
                    "docket_id": docket_id,
                    "source_entry_ids": [],
                }
            )
        emit_calendars(cfg, store)
        # Each backend constructed exactly once, then reused.
        assert len(gcs_instances) == 1
        assert gcs_instances[0].sync.call_count == 2
        assert len(m365_instances) == 1
        assert m365_instances[0].sync.call_count == 2

    def test_site_description_threaded_into_index(self, store, cfg, tmp_path):
        # `site_description` is optional. When set, it's passed through to
        # write_index and surfaces in the <meta name="description"> tag.
        cfg["index_path"] = str(tmp_path / "index.html")
        cfg["site_description"] = "Custom description for this deployment."
        store.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "h1",
                "title": "Sentencing",
                "starts_at_utc": "2099-04-14T15:00:00+00:00",
                "duration_minutes": 90,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [],
            }
        )
        emit_calendars(cfg, store)
        text = Path(cfg["index_path"]).read_text()
        assert "Custom description for this deployment." in text


class TestCmdSyncCaseFilter:
    """`cmd_sync --case <id>` runs the case filter; the success branch
    (filter found a match) was uncovered."""

    def test_case_filter_with_known_id_proceeds(
        self,
        cfg_file,
        fake_cl_ctx,
        monkeypatch,
    ):
        monkeypatch.setattr(cli.llm, "provider_info", lambda: "fake/model")
        sync_calls: list[Any] = []

        class _FakeSyncer:
            def __init__(self, *a, **kw):
                pass

            def sync_case(self, case):
                sync_calls.append(case.case_id)
                return {
                    "dockets_skipped": 0,
                    "entries_seen": 0,
                    "entries_processed": 0,
                    "actions": 0,
                    "verified": 0,
                    "deduped": 0,
                    "deduped_held": 0,
                }

        monkeypatch.setattr(cli, "CaseSyncer", _FakeSyncer)
        # No actions / dedups -> no emit, but the case filter branch fires.
        args = Namespace(
            config=str(cfg_file),
            case="us-v-x",  # match the seeded case
            no_emit=False,
            only_new=False,
            force_summaries=False,
        )
        assert cmd_sync(args) == 0
        assert sync_calls == ["us-v-x"]


class TestEnsureDocketAlertsWiring:
    """Verifies the ensure-alerts wire-up in cmd_sync and cmd_serve.

    The feature defaults to on; `ensure_docket_alerts: false` in the
    top-level config opts out. cmd_sync runs the reconciliation once
    before iterating cases; cmd_serve runs it once before starting
    the webhook listener. Both paths converge on the same helper
    (`_maybe_ensure_docket_alerts`) so we test it directly with
    representative cmd_sync / cmd_serve scaffolding.
    """

    def test_helper_skips_when_flag_false(self, monkeypatch):
        from case_calendar.cli import _maybe_ensure_docket_alerts
        from .conftest import FakeCourtListener

        ensure_calls: list[Any] = []

        def _fake_ensure(cl, docket_ids):
            ensure_calls.append(list(docket_ids))
            return {}

        monkeypatch.setattr("case_calendar.alerts.ensure_docket_alerts", _fake_ensure)
        case = cli.CaseConfig(case_id="x", name="X", dockets=[100], calendar="cyber")
        _maybe_ensure_docket_alerts(
            {"ensure_docket_alerts": False}, FakeCourtListener(), [case]
        )
        assert ensure_calls == []

    def test_helper_runs_by_default(self, monkeypatch):
        from case_calendar.cli import _maybe_ensure_docket_alerts
        from .conftest import FakeCourtListener

        ensure_calls: list[Any] = []

        def _fake_ensure(cl, docket_ids):
            ensure_calls.append(list(docket_ids))
            return {100: "created", 200: "exists"}

        monkeypatch.setattr("case_calendar.alerts.ensure_docket_alerts", _fake_ensure)
        cases = [
            cli.CaseConfig(case_id="a", name="A", dockets=[100, 200], calendar="cyber"),
            cli.CaseConfig(case_id="b", name="B", dockets=[200, 300], calendar="cyber"),
        ]
        # Empty cfg → flag defaults to true → reconciliation runs.
        _maybe_ensure_docket_alerts({}, FakeCourtListener(), cases)
        # De-duplicated and sorted docket ids reach the helper.
        assert ensure_calls == [[100, 200, 300]]

    def test_helper_skips_when_no_dockets_configured(self, monkeypatch):
        from case_calendar.cli import _maybe_ensure_docket_alerts
        from .conftest import FakeCourtListener

        called = {"n": 0}

        def _fake_ensure(*_a, **_kw):
            called["n"] += 1
            return {}

        monkeypatch.setattr("case_calendar.alerts.ensure_docket_alerts", _fake_ensure)
        _maybe_ensure_docket_alerts({}, FakeCourtListener(), [])
        assert called["n"] == 0

    def test_helper_logs_summary_only_when_something_happened(
        self, monkeypatch, caplog
    ):
        from case_calendar.cli import _maybe_ensure_docket_alerts
        from .conftest import FakeCourtListener

        # all-exists -> no log line (the all-zero counters skip the
        # summary log to keep quiet syncs quiet).
        monkeypatch.setattr(
            "case_calendar.alerts.ensure_docket_alerts",
            lambda cl, dids: {d: "exists" for d in dids},
        )
        case = cli.CaseConfig(case_id="a", name="A", dockets=[100], calendar="cyber")
        with caplog.at_level("INFO", logger="case_calendar.cli"):
            _maybe_ensure_docket_alerts({}, FakeCourtListener(), [case])
        assert not any("docket alerts:" in r.message for r in caplog.records)

        # created -> log the summary so operators see what changed.
        caplog.clear()
        monkeypatch.setattr(
            "case_calendar.alerts.ensure_docket_alerts",
            lambda cl, dids: {d: "created" for d in dids},
        )
        with caplog.at_level("INFO", logger="case_calendar.cli"):
            _maybe_ensure_docket_alerts({}, FakeCourtListener(), [case])
        assert any("docket alerts: 1 created" in r.message for r in caplog.records)


class TestCmdSummarizeCoverage:
    """Branches in cmd_summarize that the main happy-path test skips."""

    def test_case_filter_with_known_id_proceeds(
        self,
        cfg_file,
        fake_cl_ctx,
        monkeypatch,
    ):
        cfg = yaml.safe_load(cfg_file.read_text())
        cfg["case_summaries"] = {"enabled": True}
        cfg_file.write_text(yaml.safe_dump(cfg))
        monkeypatch.setattr(cli.llm, "provider_info", lambda: "fake/model")

        from case_calendar import summary as summary_mod

        monkeypatch.setattr(summary_mod, "summarize_case", lambda **_: [])
        emit_calls: list[Any] = []
        monkeypatch.setattr(
            cli,
            "emit_calendars",
            lambda *a, **kw: emit_calls.append(kw) or {},
        )
        args = Namespace(
            config=str(cfg_file),
            case="us-v-x",  # match the seeded case
            force=False,
            no_emit=False,
        )
        assert cmd_summarize(args) == 0
        # summarize_case returned [] -> affected_calendars empty -> no emit.
        assert emit_calls == []

    def test_no_emit_flag_short_circuits_even_when_summaries_written(
        self,
        cfg_file,
        fake_cl_ctx,
        monkeypatch,
    ):
        cfg = yaml.safe_load(cfg_file.read_text())
        cfg["case_summaries"] = {"enabled": True}
        cfg_file.write_text(yaml.safe_dump(cfg))
        monkeypatch.setattr(cli.llm, "provider_info", lambda: "fake/model")

        from case_calendar import summary as summary_mod

        monkeypatch.setattr(
            summary_mod,
            "summarize_case",
            lambda **_: [
                {
                    "docket_number": "1:25-cr-1",
                    "court_id": "mad",
                    "summary": "x" * 12,
                    "model": "m",
                }
            ],
        )
        emit_calls: list[Any] = []
        monkeypatch.setattr(
            cli,
            "emit_calendars",
            lambda *a, **kw: emit_calls.append(kw) or {},
        )
        args = Namespace(
            config=str(cfg_file),
            case=None,
            force=False,
            no_emit=True,  # the field under test
        )
        assert cmd_summarize(args) == 0
        assert emit_calls == []


class TestCmdShowMissingFields:
    """`cmd_show` formats a few fields conditionally. The headline test
    populates every field; cover the empty branches here."""

    def test_hearing_without_location_judge_or_dial_in(
        self,
        cfg_file,
        capsys,
    ):
        cfg = yaml.safe_load(cfg_file.read_text())
        s = cli.Store(cfg["store_path"])
        s.upsert_hearing(
            {
                "case_id": "us-v-x",
                "hearing_key": "bare",
                "title": "Sentencing",
                "starts_at_utc": "2099-01-01T00:00:00+00:00",
                "duration_minutes": 60,
                "timezone": "America/New_York",
                "status": "scheduled",
                "significance": "major",
                "docket_id": 100,
                "source_entry_ids": [1],
                # location, judge, dial_in all default to None.
            }
        )
        s.conn.commit()
        s.close()

        args = Namespace(config=str(cfg_file), case=None)
        assert cmd_show(args) == 0
        out = capsys.readouterr().out
        # Title rendered, but the optional decoration lines didn't fire.
        assert "Sentencing" in out
        assert "loc=" not in out
        assert "dial-in=" not in out


class TestDebounceLifecycleBranches:
    """Tests targeting the three remaining branches in the webhook
    debounce path: empty-pending early return, refresh-returned-empty
    early return, exception swallow, and the timer-cancel-then-rearm
    branch when a second delivery lands inside the debounce window."""

    def test_fire_debounced_summary_returns_early_when_pending_empty(
        self,
        cfg_file,
        fake_cl_ctx,
        monkeypatch,
    ):
        # Arm the debounce, then race the fire so pending_cals is cleared
        # by the first fire BEFORE refresh_stale runs. Easier to model:
        # patch the fire fn directly with a captured no-op and assert it
        # returns without calling refresh_stale.
        cfg = yaml.safe_load(cfg_file.read_text())
        cfg["case_summaries"] = {"enabled": True, "debounce_seconds": 0.01}
        cfg_file.write_text(yaml.safe_dump(cfg))
        monkeypatch.setenv(
            "CASE_CALENDAR_WEBHOOK_SECRET",
            "this-is-a-sufficiently-long-secret",
        )
        monkeypatch.setattr(cli.llm, "provider_info", lambda: "fake/model")

        from case_calendar import summary as summary_mod

        refresh_calls: list[Any] = []
        monkeypatch.setattr(
            summary_mod,
            "refresh_stale",
            lambda **kw: refresh_calls.append(kw) or {},
        )

        # Capture the fire callback so we can call it AFTER pending_cals
        # has been cleared.
        captured: dict = {}
        import threading

        class _FakeTimer:
            def __init__(self, interval, callback):
                captured["fire"] = callback

            def start(self):
                pass

            def cancel(self):
                pass

        monkeypatch.setattr(threading, "Timer", _FakeTimer)

        s = cli.Store(cfg["store_path"])
        s.upsert_docket_meta(
            100,
            {
                "court_id": "mad",
                "docket_number": "1:25-cr-1",
                "case_name": "X",
                "absolute_url": "/x/",
            },
        )
        s.upsert_case_summary(
            "us-v-x",
            "1:25-cr-1",
            "mad",
            summary="old",
            model="m",
            source_entry_ids=[],
        )
        s.mark_summary_stale("us-v-x", "1:25-cr-1", "mad")
        s.conn.commit()
        s.close()

        def _fake_serve(**kw):
            kw["emit_fn"]({"cyber"})  # arms the timer
            # Now drain pending_cals by firing once...
            captured["fire"]()
            # ...and fire again — the second fire sees empty pending.
            refresh_calls.clear()
            captured["fire"]()

        monkeypatch.setattr("case_calendar.serve.serve", _fake_serve)
        monkeypatch.setattr(
            cli,
            "emit_calendars",
            lambda *a, **kw: {
                "cyber": {
                    "events": 0,
                    "ics_path": None,
                    "gcal_pushed": False,
                    "m365_pushed": False,
                }
            },
        )

        args = Namespace(config=str(cfg_file), host="127.0.0.1", port=9000)
        assert cmd_serve(args) == 0
        # The second fire returned early; refresh_stale wasn't called.
        assert refresh_calls == []

    def test_fire_debounced_summary_returns_early_when_no_rows_written(
        self,
        cfg_file,
        fake_cl_ctx,
        monkeypatch,
    ):
        # refresh_stale returns {} (no row regenerated) -> skip the re-emit.
        cfg = yaml.safe_load(cfg_file.read_text())
        cfg["case_summaries"] = {"enabled": True, "debounce_seconds": 0.01}
        cfg_file.write_text(yaml.safe_dump(cfg))
        monkeypatch.setenv(
            "CASE_CALENDAR_WEBHOOK_SECRET",
            "this-is-a-sufficiently-long-secret",
        )
        monkeypatch.setattr(cli.llm, "provider_info", lambda: "fake/model")

        from case_calendar import summary as summary_mod

        monkeypatch.setattr(summary_mod, "refresh_stale", lambda **kw: {})

        captured: dict = {}
        import threading

        class _FakeTimer:
            def __init__(self, interval, callback):
                captured["fire"] = callback

            def start(self):
                pass

            def cancel(self):
                pass

        monkeypatch.setattr(threading, "Timer", _FakeTimer)

        s = cli.Store(cfg["store_path"])
        s.upsert_docket_meta(
            100,
            {
                "court_id": "mad",
                "docket_number": "1:25-cr-1",
                "case_name": "X",
                "absolute_url": "/x/",
            },
        )
        s.upsert_case_summary(
            "us-v-x",
            "1:25-cr-1",
            "mad",
            summary="old",
            model="m",
            source_entry_ids=[],
        )
        s.mark_summary_stale("us-v-x", "1:25-cr-1", "mad")
        s.conn.commit()
        s.close()

        emit_calls: list[Any] = []

        def _emit(*a, **kw):
            emit_calls.append(kw.get("only_calendars"))
            return {}

        def _fake_serve(**kw):
            kw["emit_fn"]({"cyber"})
            emit_calls.clear()  # ignore the initial emit
            captured["fire"]()

        monkeypatch.setattr("case_calendar.serve.serve", _fake_serve)
        monkeypatch.setattr(cli, "emit_calendars", _emit)

        args = Namespace(config=str(cfg_file), host="127.0.0.1", port=9000)
        assert cmd_serve(args) == 0
        # No re-emit fired from the debounced refresh — refresh_stale
        # returned {} so the early-return branch ran.
        assert emit_calls == []

    def test_fire_debounced_summary_swallows_refresh_exceptions(
        self,
        cfg_file,
        fake_cl_ctx,
        monkeypatch,
        caplog,
    ):
        cfg = yaml.safe_load(cfg_file.read_text())
        cfg["case_summaries"] = {"enabled": True, "debounce_seconds": 0.01}
        cfg_file.write_text(yaml.safe_dump(cfg))
        monkeypatch.setenv(
            "CASE_CALENDAR_WEBHOOK_SECRET",
            "this-is-a-sufficiently-long-secret",
        )
        monkeypatch.setattr(cli.llm, "provider_info", lambda: "fake/model")

        from case_calendar import summary as summary_mod

        def _boom(**kw):
            raise RuntimeError("refresh exploded")

        monkeypatch.setattr(summary_mod, "refresh_stale", _boom)

        captured: dict = {}
        import threading

        class _FakeTimer:
            def __init__(self, interval, callback):
                captured["fire"] = callback

            def start(self):
                pass

            def cancel(self):
                pass

        monkeypatch.setattr(threading, "Timer", _FakeTimer)

        s = cli.Store(cfg["store_path"])
        s.upsert_docket_meta(
            100,
            {
                "court_id": "mad",
                "docket_number": "1:25-cr-1",
                "case_name": "X",
                "absolute_url": "/x/",
            },
        )
        s.upsert_case_summary(
            "us-v-x",
            "1:25-cr-1",
            "mad",
            summary="old",
            model="m",
            source_entry_ids=[],
        )
        s.mark_summary_stale("us-v-x", "1:25-cr-1", "mad")
        s.conn.commit()
        s.close()

        def _fake_serve(**kw):
            kw["emit_fn"]({"cyber"})
            captured["fire"]()  # raises inside the callback, must be swallowed

        monkeypatch.setattr("case_calendar.serve.serve", _fake_serve)
        monkeypatch.setattr(
            cli,
            "emit_calendars",
            lambda *a, **kw: {
                "cyber": {
                    "events": 0,
                    "ics_path": None,
                    "gcal_pushed": False,
                    "m365_pushed": False,
                }
            },
        )

        args = Namespace(config=str(cfg_file), host="127.0.0.1", port=9000)
        with caplog.at_level("ERROR", logger="case_calendar.cli"):
            # No exception escapes — the swallow branch must catch it.
            assert cmd_serve(args) == 0
        assert any(
            "debounced summary refresh failed" in r.message for r in caplog.records
        )

    def test_arm_debounce_skips_cases_outside_only_calendars(
        self,
        cfg_file,
        fake_cl_ctx,
        monkeypatch,
    ):
        # Two cases on two calendars; only the first is in scope. The
        # second case's calendar-filter `continue` is the branch under
        # test.
        cfg = yaml.safe_load(cfg_file.read_text())
        cfg["case_summaries"] = {"enabled": True}
        cfg["calendars"]["tech"] = {
            "name": "Tech",
            "ics_path": str(Path(cfg["store_path"]).parent / "tech.ics"),
        }
        cfg["cases"].append(
            {"id": "acme", "name": "Acme", "calendar": "tech", "dockets": [200]}
        )
        cfg_file.write_text(yaml.safe_dump(cfg))
        monkeypatch.setenv(
            "CASE_CALENDAR_WEBHOOK_SECRET",
            "this-is-a-sufficiently-long-secret",
        )
        monkeypatch.setattr(cli.llm, "provider_info", lambda: "fake/model")

        s = cli.Store(cfg["store_path"])
        # Non-stale summary so any_stale stays False; the loop must traverse
        # the second case AND skip it via the calendar-filter branch.
        for did, dnum in ((100, "1:25-cr-1"), (200, "1:25-cv-2")):
            s.upsert_docket_meta(
                did,
                {
                    "court_id": "mad",
                    "docket_number": dnum,
                    "case_name": "X",
                    "absolute_url": f"/x/{did}/",
                },
            )
        s.upsert_case_summary(
            "us-v-x",
            "1:25-cr-1",
            "mad",
            summary="x",
            model="m",
            source_entry_ids=[],
        )
        s.upsert_case_summary(
            "acme",
            "1:25-cv-2",
            "mad",
            summary="y",
            model="m",
            source_entry_ids=[],
        )
        s.conn.commit()
        s.close()

        timers: list[Any] = []

        class _FakeTimer:
            def __init__(self, *a, **k):
                timers.append(self)

            def start(self):
                pass

            def cancel(self):
                pass

        monkeypatch.setattr("threading.Timer", _FakeTimer)

        def _fake_serve(**kw):
            kw["emit_fn"]({"cyber"})  # tech is OUT of scope

        monkeypatch.setattr("case_calendar.serve.serve", _fake_serve)
        monkeypatch.setattr(
            cli,
            "emit_calendars",
            lambda *a, **kw: {
                "cyber": {
                    "events": 0,
                    "ics_path": None,
                    "gcal_pushed": False,
                    "m365_pushed": False,
                }
            },
        )

        args = Namespace(config=str(cfg_file), host="127.0.0.1", port=9000)
        assert cmd_serve(args) == 0
        # No timer armed (no stale rows), and crucially the loop didn't
        # crash on the out-of-scope case.
        assert timers == []

    def test_arm_debounce_cancels_existing_timer_on_rearm(
        self,
        cfg_file,
        fake_cl_ctx,
        monkeypatch,
    ):
        # Two deliveries fire emit_fn in rapid succession; the second
        # rearm must cancel the first timer.
        cfg = yaml.safe_load(cfg_file.read_text())
        cfg["case_summaries"] = {"enabled": True, "debounce_seconds": 60}
        cfg_file.write_text(yaml.safe_dump(cfg))
        monkeypatch.setenv(
            "CASE_CALENDAR_WEBHOOK_SECRET",
            "this-is-a-sufficiently-long-secret",
        )
        monkeypatch.setattr(cli.llm, "provider_info", lambda: "fake/model")

        s = cli.Store(cfg["store_path"])
        s.upsert_docket_meta(
            100,
            {
                "court_id": "mad",
                "docket_number": "1:25-cr-1",
                "case_name": "X",
                "absolute_url": "/x/",
            },
        )
        s.upsert_case_summary(
            "us-v-x",
            "1:25-cr-1",
            "mad",
            summary="old",
            model="m",
            source_entry_ids=[],
        )
        s.mark_summary_stale("us-v-x", "1:25-cr-1", "mad")
        s.conn.commit()
        s.close()

        cancel_calls: list[int] = []
        start_calls: list[int] = []

        class _FakeTimer:
            _seq = 0

            def __init__(self, *a, **k):
                _FakeTimer._seq += 1
                self.id = _FakeTimer._seq

            def start(self):
                start_calls.append(self.id)

            def cancel(self):
                cancel_calls.append(self.id)

        monkeypatch.setattr("threading.Timer", _FakeTimer)

        def _fake_serve(**kw):
            kw["emit_fn"]({"cyber"})  # arms timer 1
            kw["emit_fn"]({"cyber"})  # rearms -> cancels timer 1, arms timer 2

        monkeypatch.setattr("case_calendar.serve.serve", _fake_serve)
        monkeypatch.setattr(
            cli,
            "emit_calendars",
            lambda *a, **kw: {
                "cyber": {
                    "events": 0,
                    "ics_path": None,
                    "gcal_pushed": False,
                    "m365_pushed": False,
                }
            },
        )

        args = Namespace(config=str(cfg_file), host="127.0.0.1", port=9000)
        assert cmd_serve(args) == 0
        # Timer 1 got cancelled when the second arm came in.
        assert 1 in cancel_calls
        # Two timers were started across both arms.
        assert start_calls == [1, 2]
