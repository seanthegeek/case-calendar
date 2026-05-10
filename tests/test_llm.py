"""Tests for the provider-agnostic LLM extractor.

We monkey-patch the per-provider call functions instead of the SDK clients
so we never hit any network or import the heavy SDKs lazily-imported inside.
"""

from __future__ import annotations

import json

import pytest

from case_calendar import llm


# --- _detect_provider ---


class TestDetectProvider:
    def test_explicit_provider_env(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        assert llm._detect_provider() == "openai"

    def test_explicit_provider_is_normalized(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "  Anthropic ")
        assert llm._detect_provider() == "anthropic"

    def test_invalid_provider_falls_through_to_keys(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "bogus")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert llm._detect_provider() == "openai"

    def test_anthropic_key_only(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
        assert llm._detect_provider() == "anthropic"

    def test_openai_key_only(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai")
        assert llm._detect_provider() == "openai"

    def test_gemini_key_only(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "g-key")
        assert llm._detect_provider() == "gemini"

    def test_google_api_key_also_works_for_gemini(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
        assert llm._detect_provider() == "gemini"

    def test_no_keys_returns_none(self):
        assert llm._detect_provider() is None

    def test_anthropic_wins_when_both_set(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ant")
        monkeypatch.setenv("OPENAI_API_KEY", "oai")
        assert llm._detect_provider() == "anthropic"


# --- _parse_actions ---


class TestParseActions:
    def test_clean_json(self):
        text = '{"actions": [{"type": "ADD", "hearing_key": "x"}]}'
        assert llm._parse_actions(text) == [{"type": "ADD", "hearing_key": "x"}]

    def test_strips_markdown_fences(self):
        text = "```json\n{\"actions\": [{\"type\": \"IGNORE\"}]}\n```"
        assert llm._parse_actions(text) == [{"type": "IGNORE"}]

    def test_extracts_json_from_chatter(self):
        text = "Sure, here's my analysis: {\"actions\": [{\"type\": \"IGNORE\"}]} let me know"
        assert llm._parse_actions(text) == [{"type": "IGNORE"}]

    def test_no_json_returns_ignore(self):
        result = llm._parse_actions("I cannot help with that.")
        assert len(result) == 1
        assert result[0]["type"] == "IGNORE"

    def test_malformed_json_returns_ignore(self):
        result = llm._parse_actions('{"actions": [{...broken}]}')
        assert len(result) == 1
        assert result[0]["type"] == "IGNORE"

    def test_actions_not_list_returns_empty(self):
        assert llm._parse_actions('{"actions": "not a list"}') == []

    def test_missing_actions_key_returns_empty(self):
        assert llm._parse_actions('{"other": "stuff"}') == []


# --- build_user_message ---


class TestBuildUserMessage:
    def test_includes_case_court_and_tz(self):
        msg = llm.build_user_message(
            case_name="US v. X", court_id="mad", court_tz="America/New_York",
            entry={"id": 1, "description": "x", "date_filed": "2026-01-01",
                   "recap_documents": []},
            pdf_texts=[], known_hearings=[],
        )
        assert "US v. X" in msg
        assert "mad" in msg
        assert "America/New_York" in msg

    def test_no_known_hearings_message(self):
        msg = llm.build_user_message(
            case_name="x", court_id="x", court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[], known_hearings=[],
        )
        assert "no hearings known yet" in msg

    def test_known_hearings_serialized(self):
        msg = llm.build_user_message(
            case_name="x", court_id="x", court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[],
            known_hearings=[{"hearing_key": "sentencing-x", "status": "scheduled",
                              "title": "Sentencing", "starts_at_utc": "2026-04-14T15:00:00+00:00",
                              "location": "Courtroom 4"}],
        )
        assert "sentencing-x" in msg
        assert "Sentencing" in msg

    def test_docket_id_surfaced_for_cross_docket_rule(self):
        # Both the entry's docket_id and each known hearing's docket_id must
        # be visible to the model so it can apply the cross-docket rule.
        msg = llm.build_user_message(
            case_name="x", court_id="cadc", court_tz="America/New_York",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[],
            known_hearings=[{
                "hearing_key": "oral-arg-x", "status": "scheduled",
                "title": "Oral Arg", "starts_at_utc": "2026-05-19T13:30:00+00:00",
                "location": None, "docket_id": 72380208,
            }],
            docket_id=72379655,
        )
        assert "docket_id   : 72379655" in msg
        assert "docket_id=72380208" in msg

    def test_pdf_texts_truncated_per_pdf(self):
        msg = llm.build_user_message(
            case_name="x", court_id="x", court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=["a" * 20_000],
            known_hearings=[],
        )
        # Per build_user_message, each PDF is truncated to 6000 chars.
        assert msg.count("a") < 8000

    def test_recap_doc_descriptions_listed(self):
        msg = llm.build_user_message(
            case_name="x", court_id="x", court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": [
                {"id": 99, "description": "Notice of Hearing"}]},
            pdf_texts=[], known_hearings=[],
        )
        assert "Notice of Hearing" in msg
        assert "#99" in msg


# --- extract_actions error path ---


class TestExtractActionsErrors:
    def test_no_provider_raises(self, monkeypatch):
        # Conftest already strips all provider env vars.
        with pytest.raises(RuntimeError, match="No LLM provider configured"):
            llm.extract_actions(
                case_name="x", court_id="x", court_tz="x",
                entry={"id": 1, "description": "", "recap_documents": []},
                pdf_texts=[], known_hearings=[],
            )

    def test_provider_call_failure_returns_ignore(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        result = llm.extract_actions(
            case_name="x", court_id="x", court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[], known_hearings=[],
        )
        assert len(result) == 1
        assert result[0]["type"] == "IGNORE"
        assert "llm call failed" in result[0]["reason"]


# --- provider_info ---


class TestProviderInfo:
    def test_no_provider(self):
        assert llm.provider_info() == "no provider configured"

    def test_with_provider_default_model(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        info = llm.provider_info()
        assert "anthropic" in info
        assert "claude-haiku-4-5" in info  # the chosen default

    def test_with_model_override(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setenv("LLM_MODEL", "claude-opus-4-7")
        assert "claude-opus-4-7" in llm.provider_info()


# --- end-to-end: stub provider call, verify wiring ---


def test_extract_actions_dispatches_to_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    captured = {}
    def fake_call(system, user, max_tokens):
        captured["system"] = system
        captured["user"] = user
        return '{"actions": [{"type": "ADD", "hearing_key": "x", "title": "T"}]}'
    monkeypatch.setattr(llm, "_call_anthropic", fake_call)

    out = llm.extract_actions(
        case_name="US v. Z", court_id="mad", court_tz="America/New_York",
        entry={"id": 1, "description": "Sentencing set for 4/14", "recap_documents": []},
        pdf_texts=[], known_hearings=[],
    )
    assert out == [{"type": "ADD", "hearing_key": "x", "title": "T"}]
    assert "US v. Z" in captured["user"]
    assert "Hearing types you care about" in captured["system"]


# --- verify_hearing ---


def _hearing(**overrides):
    base = {
        "case_id": "us-v-x",
        "hearing_key": "trial-x",
        "title": "Trial",
        "starts_at_utc": "2099-01-15T14:00:00+00:00",
        "duration_minutes": 240,
        "status": "scheduled",
        "significance": "major",
        "docket_id": 100,
        "source_entry_ids": [1, 2],
        "notes": None,
    }
    base.update(overrides)
    return base


class TestVerifyHearing:
    def test_returns_confirm_action(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic",
            lambda system, user, max_tokens:
                '{"type": "CONFIRM", "reason": "still scheduled"}',
        )
        out = llm.verify_hearing(
            case_name="US v. X", court_id="mad", court_tz="America/New_York",
            hearing=_hearing(), recent_entries=[],
        )
        assert out["type"] == "CONFIRM"

    def test_returns_reschedule_with_date(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic",
            lambda system, user, max_tokens:
                '{"type": "RESCHEDULE", "local_date": "2099-02-01", '
                '"local_time": "10:00", "reason": "moved"}',
        )
        out = llm.verify_hearing(
            case_name="US v. X", court_id="mad", court_tz="America/New_York",
            hearing=_hearing(), recent_entries=[],
        )
        assert out["type"] == "RESCHEDULE"
        assert out["local_date"] == "2099-02-01"

    def test_strips_markdown_fences(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic",
            lambda system, user, max_tokens:
                '```json\n{"type": "CANCEL", "reason": "vacated"}\n```',
        )
        out = llm.verify_hearing(
            case_name="US v. X", court_id="mad", court_tz="America/New_York",
            hearing=_hearing(), recent_entries=[],
        )
        assert out["type"] == "CANCEL"

    def test_unwraps_actions_array(self, monkeypatch):
        # Defensive: model might emit {"actions": [...]} despite the prompt.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic",
            lambda system, user, max_tokens:
                '{"actions": [{"type": "MARK_HELD", "reason": "held"}]}',
        )
        out = llm.verify_hearing(
            case_name="US v. X", court_id="mad", court_tz="America/New_York",
            hearing=_hearing(), recent_entries=[],
        )
        assert out["type"] == "MARK_HELD"

    def test_non_json_response_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic",
            lambda system, user, max_tokens: "I cannot determine.",
        )
        out = llm.verify_hearing(
            case_name="US v. X", court_id="mad", court_tz="America/New_York",
            hearing=_hearing(), recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"

    def test_missing_type_field_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic",
            lambda system, user, max_tokens: '{"reason": "no type field"}',
        )
        out = llm.verify_hearing(
            case_name="US v. X", court_id="mad", court_tz="America/New_York",
            hearing=_hearing(), recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"

    def test_llm_call_failure_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        def boom(system, user, max_tokens):
            raise RuntimeError("api down")
        monkeypatch.setattr(llm, "_call_anthropic", boom)
        out = llm.verify_hearing(
            case_name="US v. X", court_id="mad", court_tz="America/New_York",
            hearing=_hearing(), recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"

    def test_user_message_includes_hearing_and_entries(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        captured = {}
        def fake(system, user, max_tokens):
            captured["user"] = user
            captured["system"] = system
            return '{"type": "CONFIRM"}'
        monkeypatch.setattr(llm, "_call_anthropic", fake)
        llm.verify_hearing(
            case_name="US v. X", court_id="mad", court_tz="America/New_York",
            hearing=_hearing(),
            recent_entries=[
                {"entry_number": 50, "entry_id": 9999,
                 "date_filed": "2026-04-01",
                 "description": "Order vacating trial date"},
            ],
        )
        assert "trial-x" in captured["user"]
        assert "Order vacating trial date" in captured["user"]
        assert "audit a single scheduled court hearing" in captured["system"]
