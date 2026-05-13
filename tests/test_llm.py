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

    def test_dispatches_to_openai(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_openai",
            lambda system, user, max_tokens: '{"type": "CONFIRM"}',
        )
        out = llm.verify_hearing(
            case_name="X", court_id="x", court_tz="UTC",
            hearing=_hearing(), recent_entries=[],
        )
        assert out["type"] == "CONFIRM"

    def test_dispatches_to_gemini(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_gemini",
            lambda system, user, max_tokens: '{"type": "CONFIRM"}',
        )
        out = llm.verify_hearing(
            case_name="X", court_id="x", court_tz="UTC",
            hearing=_hearing(), recent_entries=[],
        )
        assert out["type"] == "CONFIRM"

    def test_no_provider_raises(self):
        with pytest.raises(RuntimeError, match="No LLM provider"):
            llm.verify_hearing(
                case_name="x", court_id="x", court_tz="x",
                hearing=_hearing(), recent_entries=[],
            )

    def test_empty_actions_array_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic",
            lambda *a, **kw: '{"actions": []}',
        )
        out = llm.verify_hearing(
            case_name="X", court_id="x", court_tz="UTC",
            hearing=_hearing(), recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"


# --- extract_actions provider dispatch ---


class TestExtractActionsDispatch:
    def test_dispatches_to_openai(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_openai",
            lambda system, user, max_tokens:
                '{"actions": [{"type": "IGNORE"}]}',
        )
        out = llm.extract_actions(
            case_name="x", court_id="x", court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[], known_hearings=[],
        )
        assert out == [{"type": "IGNORE"}]

    def test_dispatches_to_gemini(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_gemini",
            lambda system, user, max_tokens:
                '{"actions": [{"type": "IGNORE"}]}',
        )
        out = llm.extract_actions(
            case_name="x", court_id="x", court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[], known_hearings=[],
        )
        assert out == [{"type": "IGNORE"}]


# --- build_user_message: deadlines + referenced_entries ---


class TestBuildUserMessageOptionalBlocks:
    def test_known_deadlines_block_only_when_passed(self):
        msg_off = llm.build_user_message(
            case_name="x", court_id="x", court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[], known_hearings=[], known_deadlines=None,
        )
        assert "KNOWN DEADLINES" not in msg_off

        msg_empty = llm.build_user_message(
            case_name="x", court_id="x", court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[], known_hearings=[], known_deadlines=[],
        )
        assert "KNOWN DEADLINES" in msg_empty
        assert "(no deadlines known yet)" in msg_empty

        msg_full = llm.build_user_message(
            case_name="x", court_id="x", court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[], known_hearings=[],
            known_deadlines=[{
                "deadline_key": "reply-mtd", "status": "pending",
                "title": "Reply ISO MTD",
                "due_at_utc": "2026-05-31T21:00:00+00:00",
                "deadline_type": "reply", "docket_id": 100,
            }],
        )
        assert "reply-mtd" in msg_full
        assert "Reply ISO MTD" in msg_full

    def test_referenced_entries_block(self):
        msg = llm.build_user_message(
            case_name="x", court_id="x", court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[], known_hearings=[],
            referenced_entries=[
                {"entry_number": 65, "date_filed": "2026-01-01",
                 "description": "Motion to Continue trial"},
                {"entry_number": 66, "short_description": "",
                 "description": ""},  # empty -> skipped
            ],
        )
        assert "RELATED DOCKET ENTRIES" in msg
        assert "Motion to Continue trial" in msg

    def test_referenced_entries_all_empty_drops_block(self):
        msg = llm.build_user_message(
            case_name="x", court_id="x", court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[], known_hearings=[],
            referenced_entries=[
                {"entry_number": 65, "description": ""},
            ],
        )
        assert "RELATED DOCKET ENTRIES" not in msg


# --- Provider call functions (per-provider SDK wrappers) ---


class TestCallAnthropic:
    def test_returns_text_block(self, monkeypatch):
        from unittest.mock import MagicMock
        import sys
        # Mock the anthropic module so we don't import the real SDK.
        fake_mod = MagicMock(name="anthropic")
        fake_client = MagicMock(name="Anthropic client")
        fake_mod.Anthropic.return_value = fake_client
        # Construct a fake response with one text block.
        block = MagicMock()
        block.type = "text"
        block.text = "hello"
        fake_client.messages.create.return_value.content = [block]
        monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

        out = llm._call_anthropic("sys", "user", 100)
        assert out == "hello"
        kwargs = fake_client.messages.create.call_args.kwargs
        assert kwargs["model"] == llm._DEFAULT_MODELS["anthropic"]
        # System block carries the cache_control marker.
        assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}

    def test_respects_model_kwarg(self, monkeypatch):
        from unittest.mock import MagicMock
        import sys
        fake_mod = MagicMock(name="anthropic")
        fake_client = MagicMock()
        fake_mod.Anthropic.return_value = fake_client
        block = MagicMock(); block.type = "text"; block.text = "ok"
        fake_client.messages.create.return_value.content = [block]
        monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

        llm._call_anthropic("s", "u", 50, model="claude-opus-4-7")
        assert fake_client.messages.create.call_args.kwargs["model"] == "claude-opus-4-7"

    def test_no_text_block_raises(self, monkeypatch):
        from unittest.mock import MagicMock
        import sys
        fake_mod = MagicMock()
        fake_client = MagicMock()
        fake_mod.Anthropic.return_value = fake_client
        non_text = MagicMock(); non_text.type = "tool_use"
        fake_client.messages.create.return_value.content = [non_text]
        monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

        with pytest.raises(ValueError, match="No text block"):
            llm._call_anthropic("s", "u", 10)


class TestCallOpenAI:
    def test_returns_message_content(self, monkeypatch):
        from unittest.mock import MagicMock
        import sys
        fake_mod = MagicMock(name="openai")
        fake_client = MagicMock()
        fake_mod.OpenAI.return_value = fake_client
        msg = MagicMock(); msg.content = '{"actions": []}'
        choice = MagicMock(); choice.message = msg
        fake_client.chat.completions.create.return_value.choices = [choice]
        monkeypatch.setitem(sys.modules, "openai", fake_mod)

        out = llm._call_openai("s", "u", 50)
        assert out == '{"actions": []}'
        # JSON mode is on by default and shows up as response_format.
        kw = fake_client.chat.completions.create.call_args.kwargs
        assert kw["response_format"] == {"type": "json_object"}

    def test_json_mode_off_omits_response_format(self, monkeypatch):
        from unittest.mock import MagicMock
        import sys
        fake_mod = MagicMock()
        fake_client = MagicMock()
        fake_mod.OpenAI.return_value = fake_client
        msg = MagicMock(); msg.content = "prose"
        choice = MagicMock(); choice.message = msg
        fake_client.chat.completions.create.return_value.choices = [choice]
        monkeypatch.setitem(sys.modules, "openai", fake_mod)

        llm._call_openai("s", "u", 50, json_mode=False)
        kw = fake_client.chat.completions.create.call_args.kwargs
        assert "response_format" not in kw

    def test_empty_content_raises(self, monkeypatch):
        from unittest.mock import MagicMock
        import sys
        fake_mod = MagicMock()
        fake_client = MagicMock()
        fake_mod.OpenAI.return_value = fake_client
        msg = MagicMock(); msg.content = ""
        choice = MagicMock(); choice.message = msg
        fake_client.chat.completions.create.return_value.choices = [choice]
        monkeypatch.setitem(sys.modules, "openai", fake_mod)

        with pytest.raises(ValueError, match="No content"):
            llm._call_openai("s", "u", 10)


class TestCallGemini:
    def test_returns_text(self, monkeypatch):
        from unittest.mock import MagicMock
        import sys
        # google.genai is a nested module; stub both.
        fake_genai = MagicMock(name="google.genai")
        fake_types = MagicMock(name="google.genai.types")

        class _Cfg:
            def __init__(self, **kw):
                self.kw = kw

        fake_types.GenerateContentConfig = _Cfg
        fake_genai.types = fake_types
        fake_client = MagicMock()
        fake_genai.Client.return_value = fake_client
        fake_client.models.generate_content.return_value.text = '{"actions": []}'

        # Stub the package structure so `from google import genai` and
        # `from google.genai import types as gtypes` both resolve.
        fake_google = MagicMock(); fake_google.genai = fake_genai
        monkeypatch.setitem(sys.modules, "google", fake_google)
        monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
        monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)

        out = llm._call_gemini("s", "u", 50)
        assert out == '{"actions": []}'
        # json_mode on -> response_mime_type set
        cfg = fake_client.models.generate_content.call_args.kwargs["config"]
        assert cfg.kw["response_mime_type"] == "application/json"

    def test_json_mode_off_omits_mime_type(self, monkeypatch):
        from unittest.mock import MagicMock
        import sys
        fake_genai = MagicMock()
        fake_types = MagicMock()

        class _Cfg:
            def __init__(self, **kw):
                self.kw = kw

        fake_types.GenerateContentConfig = _Cfg
        fake_genai.types = fake_types
        fake_client = MagicMock()
        fake_genai.Client.return_value = fake_client
        fake_client.models.generate_content.return_value.text = "prose"

        fake_google = MagicMock(); fake_google.genai = fake_genai
        monkeypatch.setitem(sys.modules, "google", fake_google)
        monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
        monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)

        llm._call_gemini("s", "u", 50, json_mode=False)
        cfg = fake_client.models.generate_content.call_args.kwargs["config"]
        assert "response_mime_type" not in cfg.kw

    def test_empty_text_raises(self, monkeypatch):
        from unittest.mock import MagicMock
        import sys
        fake_genai = MagicMock()
        fake_types = MagicMock()
        fake_types.GenerateContentConfig = lambda **kw: object()
        fake_genai.types = fake_types
        fake_client = MagicMock()
        fake_genai.Client.return_value = fake_client
        fake_client.models.generate_content.return_value.text = ""

        fake_google = MagicMock(); fake_google.genai = fake_genai
        monkeypatch.setitem(sys.modules, "google", fake_google)
        monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
        monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)

        with pytest.raises(ValueError, match="No content"):
            llm._call_gemini("s", "u", 10)


# --- verify_deadline (parallel to verify_hearing) ---


def _deadline(**overrides):
    base = {
        "case_id": "anthropic-v-dow",
        "deadline_key": "reply-mtd",
        "title": "Reply ISO MTD",
        "due_at_utc": "2026-05-31T21:00:00+00:00",
        "status": "pending",
        "significance": "major",
        "deadline_type": "reply",
        "docket_id": 100,
        "source_entry_ids": [1, 2],
        "notes": None,
    }
    base.update(overrides)
    return base


class TestVerifyDeadline:
    def test_no_provider_raises(self):
        with pytest.raises(RuntimeError, match="No LLM provider"):
            llm.verify_deadline(
                case_name="x", court_id="x", court_tz="x",
                deadline=_deadline(), recent_entries=[],
            )

    def test_returns_confirm(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic",
            lambda *a, **kw: '{"type": "CONFIRM", "reason": "still pending"}',
        )
        out = llm.verify_deadline(
            case_name="x", court_id="x", court_tz="UTC",
            deadline=_deadline(), recent_entries=[],
        )
        assert out["type"] == "CONFIRM"

    def test_dispatches_to_openai(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_openai",
            lambda *a, **kw: '{"type": "MARK_FILED"}',
        )
        out = llm.verify_deadline(
            case_name="x", court_id="x", court_tz="UTC",
            deadline=_deadline(), recent_entries=[],
        )
        assert out["type"] == "MARK_FILED"

    def test_dispatches_to_gemini(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_gemini",
            lambda *a, **kw: '{"type": "RESCHEDULE", "local_date": "2026-06-15"}',
        )
        out = llm.verify_deadline(
            case_name="x", court_id="x", court_tz="UTC",
            deadline=_deadline(), recent_entries=[],
        )
        assert out["type"] == "RESCHEDULE"

    def test_strips_fences(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic",
            lambda *a, **kw: '```json\n{"type": "CANCEL"}\n```',
        )
        out = llm.verify_deadline(
            case_name="x", court_id="x", court_tz="UTC",
            deadline=_deadline(), recent_entries=[],
        )
        assert out["type"] == "CANCEL"

    def test_unwraps_actions_array(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic",
            lambda *a, **kw: '{"actions": [{"type": "CANCEL"}]}',
        )
        out = llm.verify_deadline(
            case_name="x", court_id="x", court_tz="UTC",
            deadline=_deadline(), recent_entries=[],
        )
        assert out["type"] == "CANCEL"

    def test_empty_actions_array_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic", lambda *a, **kw: '{"actions": []}',
        )
        out = llm.verify_deadline(
            case_name="x", court_id="x", court_tz="UTC",
            deadline=_deadline(), recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"

    def test_non_json_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic", lambda *a, **kw: "can't tell",
        )
        out = llm.verify_deadline(
            case_name="x", court_id="x", court_tz="UTC",
            deadline=_deadline(), recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"

    def test_missing_type_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic", lambda *a, **kw: '{"reason": "x"}',
        )
        out = llm.verify_deadline(
            case_name="x", court_id="x", court_tz="UTC",
            deadline=_deadline(), recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"

    def test_call_failure_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

        def boom(*a, **kw):
            raise RuntimeError("api down")

        monkeypatch.setattr(llm, "_call_anthropic", boom)
        out = llm.verify_deadline(
            case_name="x", court_id="x", court_tz="UTC",
            deadline=_deadline(), recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"

    def test_user_message_includes_deadline_and_entries(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        captured: dict[str, str] = {}

        def fake(system, user, max_tokens):
            captured["user"] = user
            return '{"type": "CONFIRM"}'

        monkeypatch.setattr(llm, "_call_anthropic", fake)
        llm.verify_deadline(
            case_name="X", court_id="mad", court_tz="UTC",
            deadline=_deadline(),
            recent_entries=[
                {"entry_number": 50, "entry_id": 99,
                 "date_filed": "2026-05-01",
                 "description": "Filed reply brief"},
            ],
        )
        assert "reply-mtd" in captured["user"]
        assert "Filed reply brief" in captured["user"]

    def test_empty_recent_entries_renders_none_marker(self, monkeypatch):
        # Confirms the no-recent-entries branch in _build_verify_deadline_user_message.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        captured: dict[str, str] = {}

        def fake(system, user, max_tokens):
            captured["user"] = user
            return '{"type": "CONFIRM"}'

        monkeypatch.setattr(llm, "_call_anthropic", fake)
        llm.verify_deadline(
            case_name="X", court_id="x", court_tz="x",
            deadline=_deadline(), recent_entries=[],
        )
        assert "(none)" in captured["user"]


class TestVerifyHearingNoRecentEntries:
    def test_empty_recent_entries_renders_none_marker(self, monkeypatch):
        # Symmetry with verify_deadline; this hits the no-entries branch
        # of _build_verify_user_message.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        captured: dict[str, str] = {}

        def fake(system, user, max_tokens):
            captured["user"] = user
            return '{"type": "CONFIRM"}'

        monkeypatch.setattr(llm, "_call_anthropic", fake)
        llm.verify_hearing(
            case_name="X", court_id="x", court_tz="x",
            hearing=_hearing(), recent_entries=[],
        )
        assert "(none)" in captured["user"]


class TestResolveDuplicateHearings:
    """End-of-sync dedupe sweep — same-docket same-slot LLM resolver."""

    def _cluster(self):
        return [
            _hearing(
                hearing_key="msj-hearing-anthropic-v-usdw",
                title="Hearing on Motion for Summary Judgment and Cross-Motion",
                starts_at_utc="2099-07-30T17:00:00+00:00",
                source_entry_ids=[149, 150],
                docket_id=72379655,
            ),
            _hearing(
                hearing_key="motion-hearing-anthropic-v-usdw-2",
                title="Motion Hearing",
                starts_at_utc="2099-07-30T17:00:00+00:00",
                source_entry_ids=[150],
                docket_id=72379655,
            ),
        ]

    def test_returns_merge_into_action(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic",
            lambda system, user, max_tokens:
                '{"type": "MERGE_INTO", '
                '"target_key": "msj-hearing-anthropic-v-usdw", '
                '"reason": "Same slot — order called the SJ hearing a Motion Hearing."}',
        )
        out = llm.resolve_duplicate_hearings(
            case_name="Anthropic v. DOW", court_id="cand",
            court_tz="America/Los_Angeles",
            cluster=self._cluster(), recent_entries=[],
        )
        assert out["type"] == "MERGE_INTO"
        assert out["target_key"] == "msj-hearing-anthropic-v-usdw"

    def test_returns_keep_both_when_truly_distinct(self, monkeypatch):
        # Stacked back-to-back proceedings — the LLM keeps both.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic",
            lambda system, user, max_tokens:
                '{"type": "KEEP_BOTH", "reason": "Order schedules both back-to-back."}',
        )
        out = llm.resolve_duplicate_hearings(
            case_name="US v. X", court_id="dcd", court_tz="America/New_York",
            cluster=self._cluster(), recent_entries=[],
        )
        assert out["type"] == "KEEP_BOTH"

    def test_strips_markdown_fences(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic",
            lambda system, user, max_tokens:
                '```json\n{"type": "MERGE_INTO", '
                '"target_key": "msj-hearing-anthropic-v-usdw", '
                '"reason": "..."}\n```',
        )
        out = llm.resolve_duplicate_hearings(
            case_name="X", court_id="cand", court_tz="America/Los_Angeles",
            cluster=self._cluster(), recent_entries=[],
        )
        assert out["type"] == "MERGE_INTO"

    def test_unwraps_actions_array(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic",
            lambda system, user, max_tokens:
                '{"actions": [{"type": "KEEP_BOTH", "reason": "..."}]}',
        )
        out = llm.resolve_duplicate_hearings(
            case_name="X", court_id="x", court_tz="UTC",
            cluster=self._cluster(), recent_entries=[],
        )
        assert out["type"] == "KEEP_BOTH"

    def test_empty_actions_array_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic",
            lambda system, user, max_tokens: '{"actions": []}',
        )
        out = llm.resolve_duplicate_hearings(
            case_name="X", court_id="x", court_tz="UTC",
            cluster=self._cluster(), recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"

    def test_non_json_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic",
            lambda system, user, max_tokens: "I cannot tell.",
        )
        out = llm.resolve_duplicate_hearings(
            case_name="X", court_id="x", court_tz="UTC",
            cluster=self._cluster(), recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"

    def test_missing_type_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic",
            lambda system, user, max_tokens: '{"reason": "no type"}',
        )
        out = llm.resolve_duplicate_hearings(
            case_name="X", court_id="x", court_tz="UTC",
            cluster=self._cluster(), recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"

    def test_llm_call_failure_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

        def boom(system, user, max_tokens):
            raise RuntimeError("api down")

        monkeypatch.setattr(llm, "_call_anthropic", boom)
        out = llm.resolve_duplicate_hearings(
            case_name="X", court_id="x", court_tz="UTC",
            cluster=self._cluster(), recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"

    def test_no_provider_configured_raises(self, monkeypatch):
        # Strip every *_API_KEY and LLM_PROVIDER override so detection fails.
        for k in ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                  "GEMINI_API_KEY", "GOOGLE_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        with pytest.raises(RuntimeError, match="No LLM provider"):
            llm.resolve_duplicate_hearings(
                case_name="X", court_id="x", court_tz="UTC",
                cluster=self._cluster(), recent_entries=[],
            )

    def test_user_message_lists_all_candidates_and_recent(self, monkeypatch):
        # Captures the user message to assert all cluster keys + a recent
        # entry line appear in it — the LLM needs both to pick a target.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        captured: dict[str, str] = {}

        def fake(system, user, max_tokens):
            captured["user"] = user
            return '{"type": "UNCLEAR"}'

        monkeypatch.setattr(llm, "_call_anthropic", fake)
        llm.resolve_duplicate_hearings(
            case_name="Anthropic v. DOW", court_id="cand",
            court_tz="America/Los_Angeles",
            cluster=self._cluster(),
            recent_entries=[{
                "entry_number": 150, "entry_id": 461818939,
                "date_filed": "2026-04-23",
                "description": "ORDER RE 149 STIPULATION ...",
            }],
        )
        msg = captured["user"]
        assert "msj-hearing-anthropic-v-usdw" in msg
        assert "motion-hearing-anthropic-v-usdw-2" in msg
        assert "ORDER RE 149 STIPULATION" in msg

    def test_empty_recent_entries_renders_none_marker(self, monkeypatch):
        # The user message must still be valid when the docket window is
        # empty — exercises the `if not recent_entries` branch.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        captured: dict[str, str] = {}

        def fake(system, user, max_tokens):
            captured["user"] = user
            return '{"type": "UNCLEAR"}'

        monkeypatch.setattr(llm, "_call_anthropic", fake)
        llm.resolve_duplicate_hearings(
            case_name="X", court_id="x", court_tz="UTC",
            cluster=self._cluster(), recent_entries=[],
        )
        assert "(none)" in captured["user"]

    def test_openai_provider_dispatch(self, monkeypatch):
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_openai",
            lambda system, user, max_tokens:
                '{"type": "MERGE_INTO", '
                '"target_key": "msj-hearing-anthropic-v-usdw", '
                '"reason": "..."}',
        )
        out = llm.resolve_duplicate_hearings(
            case_name="X", court_id="x", court_tz="UTC",
            cluster=self._cluster(), recent_entries=[],
        )
        assert out["type"] == "MERGE_INTO"

    def test_gemini_provider_dispatch(self, monkeypatch):
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_gemini",
            lambda system, user, max_tokens:
                '{"type": "KEEP_BOTH", "reason": "..."}',
        )
        out = llm.resolve_duplicate_hearings(
            case_name="X", court_id="x", court_tz="UTC",
            cluster=self._cluster(), recent_entries=[],
        )
        assert out["type"] == "KEEP_BOTH"


# --- summary pipeline ---


class TestTruncate:
    def test_empty_returns_empty(self):
        assert llm._truncate("", 10) == ""
        assert llm._truncate(None, 10) == ""

    def test_short_text_unchanged(self):
        assert llm._truncate("hello", 100) == "hello"

    def test_long_text_appends_marker(self):
        out = llm._truncate("x" * 200, 50)
        assert out.startswith("x" * 50)
        assert "[...truncated...]" in out


class TestBuildSummaryUserMessage:
    def test_full_layout(self):
        msg = llm._build_summary_user_message(
            case_name="US v. X",
            aggregation_note="Parallel district + appellate dockets.",
            docket={
                "docket_number": "1:24-cr-100",
                "court_citation": "S.D.N.Y.",
            },
            operative_docs=[{
                "entry_number": 1, "description": "INDICTMENT",
                "date_filed": "2024-01-01", "text": "Body of indictment...",
            }],
            disposition_docs=[{
                "entry_number": 99, "description": "JUDGMENT",
                "date_filed": "2025-06-15", "text": "Judgment body...",
            }],
            hearings=[{
                "title": "Sentencing", "status": "held",
                "starts_at_utc": "2025-06-10T15:00:00+00:00",
                "significance": "major",
            }],
            deadlines=[{
                "title": "Reply ISO MTD", "status": "met",
                "due_at_utc": "2024-12-15T22:00:00+00:00",
                "deadline_type": "reply",
            }],
            operative_char_budget=10_000, disposition_char_budget=10_000,
        )
        assert "US v. X" in msg
        assert "Parallel district + appellate" in msg
        assert "INDICTMENT" in msg
        assert "JUDGMENT" in msg
        assert "Sentencing" in msg
        assert "Reply ISO MTD" in msg

    def test_empty_hearings_and_deadlines_show_placeholders(self):
        msg = llm._build_summary_user_message(
            case_name="X", aggregation_note=None,
            docket={"docket_number": "x", "court_id": "x"},
            operative_docs=[], disposition_docs=[],
            hearings=[], deadlines=[],
            operative_char_budget=100, disposition_char_budget=100,
        )
        assert "(none recorded)" in msg
        # operative_docs empty -> "no operative pleading text available"
        assert "no operative pleading text available" in msg
        # disposition_docs empty -> "(none)"
        assert "(none)" in msg

    def test_conditional_deadline_surfaces_notes_verbatim(self):
        # Conditional deadlines (no fixed date — court order triggered by
        # an unknown future event) ride into the summary scaffold with
        # ``due_at_utc=None`` and the court's verbatim trigger language in
        # ``notes``. The scaffold MUST surface notes for these rows so
        # the summary LLM can describe the deadline in the court's own
        # words instead of inventing a date.
        msg = llm._build_summary_user_message(
            case_name="Anthropic v. DOW",
            aggregation_note=None,
            docket={"docket_number": "26-2011", "court_id": "ca9"},
            operative_docs=[], disposition_docs=[],
            hearings=[],
            deadlines=[{
                "title": "Appellants' Motion for Appropriate Relief",
                "status": "pending",
                "due_at_utc": None,
                "deadline_type": None,
                "notes": "Appellants must file within 21 days after "
                         "resolution of related D.C. Cir. case 26-1049.",
            }],
            operative_char_budget=100, disposition_char_budget=100,
        )
        assert "due_at_utc=None" in msg
        assert "21 days after resolution" in msg

    def test_fixed_deadline_does_not_inline_notes(self):
        # Non-conditional deadlines keep the scaffold line tight — the
        # date already says everything; notes would just add noise.
        msg = llm._build_summary_user_message(
            case_name="X", aggregation_note=None,
            docket={"docket_number": "x", "court_id": "x"},
            operative_docs=[], disposition_docs=[],
            hearings=[],
            deadlines=[{
                "title": "Govt response to MTD",
                "status": "pending",
                "due_at_utc": "2026-05-24T21:00:00+00:00",
                "deadline_type": "response",
                "notes": "Some operator-added side note that shouldn't reach the LLM.",
            }],
            operative_char_budget=100, disposition_char_budget=100,
        )
        assert "Some operator-added side note" not in msg

    def test_omits_aggregation_note_when_unset(self):
        msg = llm._build_summary_user_message(
            case_name="X", aggregation_note=None,
            docket={"docket_number": "x", "court_id": "x"},
            operative_docs=[], disposition_docs=[],
            hearings=[], deadlines=[],
            operative_char_budget=100, disposition_char_budget=100,
        )
        assert "AGGREGATION NOTE" not in msg


class TestGenerateDocketSummary:
    def test_no_provider_raises(self):
        with pytest.raises(RuntimeError, match="No LLM provider"):
            llm.generate_docket_summary(
                case_name="x", aggregation_note=None,
                docket={"docket_number": "x"},
                operative_docs=[], disposition_docs=[],
                hearings=[], deadlines=[],
            )

    def test_unknown_provider_kwarg_raises(self):
        with pytest.raises(RuntimeError, match="unknown provider"):
            llm.generate_docket_summary(
                case_name="x", aggregation_note=None,
                docket={"docket_number": "x"},
                operative_docs=[], disposition_docs=[],
                hearings=[], deadlines=[],
                provider="bogus",
            )

    def test_anthropic_default_model(self, monkeypatch):
        called: dict[str, Any] = {}

        def fake(system, user, max_tokens, *, model=None):
            called["model"] = model
            return "A short summary."

        monkeypatch.setattr(llm, "_call_anthropic", fake)
        text, ident = llm.generate_docket_summary(
            case_name="x", aggregation_note=None,
            docket={"docket_number": "x"},
            operative_docs=[], disposition_docs=[],
            hearings=[], deadlines=[],
            provider="anthropic",
        )
        assert text == "A short summary."
        assert ident == "anthropic/" + llm._DEFAULT_SUMMARY_MODELS["anthropic"]
        assert called["model"] == llm._DEFAULT_SUMMARY_MODELS["anthropic"]

    def test_openai_dispatch_json_mode_off(self, monkeypatch):
        called: dict[str, Any] = {}

        def fake(system, user, max_tokens, *, model=None, json_mode=True):
            called["model"] = model
            called["json_mode"] = json_mode
            return "Summary."

        monkeypatch.setattr(llm, "_call_openai", fake)
        text, ident = llm.generate_docket_summary(
            case_name="x", aggregation_note=None,
            docket={"docket_number": "x"},
            operative_docs=[], disposition_docs=[],
            hearings=[], deadlines=[],
            provider="openai", model="custom-model",
        )
        assert text == "Summary."
        assert ident == "openai/custom-model"
        assert called["json_mode"] is False  # summaries are prose, not JSON

    def test_gemini_dispatch(self, monkeypatch):
        called: dict[str, Any] = {}

        def fake(system, user, max_tokens, *, model=None, json_mode=True):
            called["json_mode"] = json_mode
            return "Some summary."

        monkeypatch.setattr(llm, "_call_gemini", fake)
        text, ident = llm.generate_docket_summary(
            case_name="x", aggregation_note=None,
            docket={"docket_number": "x"},
            operative_docs=[], disposition_docs=[],
            hearings=[], deadlines=[],
            provider="gemini",
        )
        assert ident.startswith("gemini/")
        assert called["json_mode"] is False

    def test_strips_code_fences_from_response(self, monkeypatch):
        monkeypatch.setattr(
            llm, "_call_anthropic",
            lambda *a, **kw: "```\nThe case is summarized.\n```",
        )
        text, _ = llm.generate_docket_summary(
            case_name="x", aggregation_note=None,
            docket={"docket_number": "x"},
            operative_docs=[], disposition_docs=[],
            hearings=[], deadlines=[],
            provider="anthropic",
        )
        assert text == "The case is summarized."

    def test_falls_back_to_env_provider(self, monkeypatch):
        monkeypatch.setenv("LLM_SUMMARY_PROVIDER", "anthropic")
        monkeypatch.setenv("LLM_SUMMARY_MODEL", "claude-opus-4-7")
        called: dict[str, Any] = {}

        def fake(system, user, max_tokens, *, model=None):
            called["model"] = model
            return "x"

        monkeypatch.setattr(llm, "_call_anthropic", fake)
        _, ident = llm.generate_docket_summary(
            case_name="x", aggregation_note=None,
            docket={"docket_number": "x"},
            operative_docs=[], disposition_docs=[],
            hearings=[], deadlines=[],
        )
        assert ident == "anthropic/claude-opus-4-7"
        assert called["model"] == "claude-opus-4-7"

    def test_falls_back_to_extractor_provider(self, monkeypatch):
        # No LLM_SUMMARY_PROVIDER set; _detect_provider should pick anthropic
        # from the regular key.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm, "_call_anthropic", lambda *a, **kw: "ok",
        )
        text, ident = llm.generate_docket_summary(
            case_name="x", aggregation_note=None,
            docket={"docket_number": "x"},
            operative_docs=[], disposition_docs=[],
            hearings=[], deadlines=[],
        )
        # Picks Sonnet (the summary-tier default), not Haiku.
        assert ident == "anthropic/" + llm._DEFAULT_SUMMARY_MODELS["anthropic"]
