"""Tests for the provider-agnostic LLM extractor.

We monkey-patch the per-provider call functions instead of the SDK clients
so we never hit any network or import the heavy SDKs lazily-imported inside.
"""

from __future__ import annotations

from typing import Any

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
        text = '```json\n{"actions": [{"type": "IGNORE"}]}\n```'
        assert llm._parse_actions(text) == [{"type": "IGNORE"}]

    def test_extracts_json_from_chatter(self):
        text = (
            'Sure, here\'s my analysis: {"actions": [{"type": "IGNORE"}]} let me know'
        )
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

    def test_two_json_objects_uses_first(self):
        # Observed in production: the LLM returned the actions object
        # followed by a second JSON object, producing
        # `json.JSONDecodeError: Extra data: line 21 column 1`.
        # The old `text[find('{') : rfind('}') + 1]` slice greedily
        # included BOTH objects in the parse input and failed.
        # `raw_decode` parses just the first object and ignores the
        # rest.
        text = (
            '{"actions": [{"type": "RESCHEDULE", "hearing_key": "x"}]}\n'
            '{"actions": [{"type": "CANCEL", "hearing_key": "y"}]}'
        )
        assert llm._parse_actions(text) == [{"type": "RESCHEDULE", "hearing_key": "x"}]

    def test_trailing_prose_after_json_is_ignored(self):
        # Same fix covers the "valid JSON + trailing commentary" case:
        # the LLM emits the object then narrates what it did.
        text = (
            '{"actions": [{"type": "ADD", "hearing_key": "x"}]}\n\n'
            "I extracted one hearing from the entry above."
        )
        assert llm._parse_actions(text) == [{"type": "ADD", "hearing_key": "x"}]

    def test_trailing_brace_in_prose_does_not_corrupt_parse(self):
        # The old `rfind('}')` strategy would have captured a stray `}`
        # in trailing prose and tried to parse the slice through it.
        # `raw_decode` is bounded to the first valid JSON value, so
        # punctuation in chatter past the object can't poison the parse.
        text = (
            '{"actions": [{"type": "IGNORE"}]}\n'
            "Note: see also section { 3 } of the order."
        )
        assert llm._parse_actions(text) == [{"type": "IGNORE"}]


# --- build_user_message ---


class TestBuildUserMessage:
    def test_includes_case_court_and_tz(self):
        msg = llm.build_user_message(
            case_name="US v. X",
            court_id="mad",
            court_tz="America/New_York",
            entry={
                "id": 1,
                "description": "x",
                "date_filed": "2026-01-01",
                "recap_documents": [],
            },
            pdf_texts=[],
            known_hearings=[],
        )
        assert "US v. X" in msg
        assert "mad" in msg
        assert "America/New_York" in msg

    def test_no_known_hearings_message(self):
        msg = llm.build_user_message(
            case_name="x",
            court_id="x",
            court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[],
            known_hearings=[],
        )
        assert "no hearings known yet" in msg

    def test_known_hearings_serialized(self):
        msg = llm.build_user_message(
            case_name="x",
            court_id="x",
            court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[],
            known_hearings=[
                {
                    "hearing_key": "sentencing-x",
                    "status": "scheduled",
                    "title": "Sentencing",
                    "starts_at_utc": "2026-04-14T15:00:00+00:00",
                    "location": "Courtroom 4",
                }
            ],
        )
        assert "sentencing-x" in msg
        assert "Sentencing" in msg

    def test_docket_id_surfaced_for_cross_docket_rule(self):
        # Both the entry's docket_id and each known hearing's docket_id must
        # be visible to the model so it can apply the cross-docket rule.
        msg = llm.build_user_message(
            case_name="x",
            court_id="cadc",
            court_tz="America/New_York",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[],
            known_hearings=[
                {
                    "hearing_key": "oral-arg-x",
                    "status": "scheduled",
                    "title": "Oral Arg",
                    "starts_at_utc": "2026-05-19T13:30:00+00:00",
                    "location": None,
                    "docket_id": 72380208,
                }
            ],
            docket_id=72379655,
        )
        assert "docket_id   : 72379655" in msg
        assert "docket_id=72380208" in msg

    def test_pdf_texts_truncated_per_pdf(self):
        msg = llm.build_user_message(
            case_name="x",
            court_id="x",
            court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=["a" * 20_000],
            known_hearings=[],
        )
        # Per build_user_message, each PDF is truncated to 6000 chars.
        assert msg.count("a") < 8000

    def test_recap_doc_descriptions_listed(self):
        msg = llm.build_user_message(
            case_name="x",
            court_id="x",
            court_tz="x",
            entry={
                "id": 1,
                "description": "",
                "recap_documents": [{"id": 99, "description": "Notice of Hearing"}],
            },
            pdf_texts=[],
            known_hearings=[],
        )
        assert "Notice of Hearing" in msg
        assert "#99" in msg

    def test_recap_doc_with_empty_description_is_skipped(self):
        # Branch coverage for the empty-description-filter inside the
        # recap-document loop. Two docs: one with text, one without; the
        # message lists only the populated one.
        msg = llm.build_user_message(
            case_name="x",
            court_id="x",
            court_tz="x",
            entry={
                "id": 1,
                "description": "",
                "recap_documents": [
                    {"id": 1, "description": ""},
                    {"id": 2, "description": "Real document"},
                ],
            },
            pdf_texts=[],
            known_hearings=[],
        )
        assert "Real document" in msg
        # The empty-description row was dropped silently.
        assert "#1" not in msg

    def test_empty_pdf_texts_list_does_not_add_pdf_block(self):
        # Branch coverage: pdf_texts is non-empty but every element is
        # falsy, so the joined string strips to "" and the PDF block
        # is omitted entirely.
        msg = llm.build_user_message(
            case_name="x",
            court_id="x",
            court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=["", ""],
            known_hearings=[],
        )
        assert "ATTACHED PDF TEXT" not in msg


# --- extract_actions error path ---


class TestExtractActionsErrors:
    def test_no_provider_raises(self, monkeypatch):
        # Conftest already strips all provider env vars.
        with pytest.raises(RuntimeError, match="No LLM provider configured"):
            llm.extract_actions(
                case_name="x",
                court_id="x",
                court_tz="x",
                entry={"id": 1, "description": "", "recap_documents": []},
                pdf_texts=[],
                known_hearings=[],
            )

    def test_provider_call_failure_returns_ignore(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_anthropic",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        result = llm.extract_actions(
            case_name="x",
            court_id="x",
            court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[],
            known_hearings=[],
        )
        assert len(result) == 1
        assert result[0]["type"] == "IGNORE"
        assert "llm call failed" in result[0]["reason"]

    def test_output_truncated_returns_ignore_with_named_reason(
        self, monkeypatch, caplog
    ):
        # Provider hit max_tokens mid-output. The partial JSON would
        # otherwise show up as the generic "malformed JSON" warning in
        # logs and the failure mode (truncation vs malformed-from-model)
        # would be invisible to operators. We surface it explicitly.
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

        def boom(*a, **kw):
            raise llm.OutputTruncatedError("anthropic", '{"actions": [', 2048)

        monkeypatch.setattr(llm, "_call_anthropic", boom)
        with caplog.at_level("WARNING", logger="case_calendar.llm"):
            result = llm.extract_actions(
                case_name="x",
                court_id="x",
                court_tz="x",
                entry={"id": 42, "description": "", "recap_documents": []},
                pdf_texts=[],
                known_hearings=[],
            )
        assert result == [{"type": "IGNORE", "reason": "llm output truncated"}]
        # Operator-visible WARNING names the truncation; no traceback.
        msg = "\n".join(r.getMessage() for r in caplog.records)
        assert "truncated" in msg
        assert "max_tokens=2048" in msg
        assert "anthropic" in msg


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


class TestSummaryProviderInfo:
    """Mirror of :class:`TestProviderInfo` for the summary-track helper —
    same precedence chain (config kwargs > LLM_SUMMARY_PROVIDER env >
    auto-detect) but the per-provider default model is the higher-tier
    one (Sonnet / GPT-5.4 / Gemini Pro) so the log line reflects what
    ``generate_docket_summary`` would actually pick at call time."""

    def test_no_provider(self):
        assert llm.summary_provider_info() == "no provider configured"

    def test_explicit_kwargs_win(self):
        # Config kwargs override env and auto-detect.
        info = llm.summary_provider_info(provider="anthropic", model="custom-sonnet")
        assert "anthropic" in info and "custom-sonnet" in info

    def test_unknown_provider_kwarg_reports_unknown(self):
        # Defensive: if the operator typo'd the config provider key, the
        # log should say "unknown provider=..." rather than crashing or
        # silently falling through. This is the no-default-summary-model
        # branch that the strict generate_docket_summary path would
        # raise on.
        info = llm.summary_provider_info(provider="not-a-real-provider")
        assert "unknown provider" in info
        assert "not-a-real-provider" in info

    def test_env_provider_picks_summary_default_model(self, monkeypatch):
        # No explicit kwargs — env var sets the provider, summary track
        # picks the higher-tier default for it (Sonnet for anthropic).
        monkeypatch.setenv("LLM_SUMMARY_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        info = llm.summary_provider_info()
        assert "anthropic" in info
        assert "sonnet" in info.lower()


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
        case_name="US v. Z",
        court_id="mad",
        court_tz="America/New_York",
        entry={
            "id": 1,
            "description": "Sentencing set for 4/14",
            "recap_documents": [],
        },
        pdf_texts=[],
        known_hearings=[],
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
            llm,
            "_call_anthropic",
            lambda system, user, max_tokens: (
                '{"type": "CONFIRM", "reason": "still scheduled"}'
            ),
        )
        out = llm.verify_hearing(
            case_name="US v. X",
            court_id="mad",
            court_tz="America/New_York",
            hearing=_hearing(),
            recent_entries=[],
        )
        assert out["type"] == "CONFIRM"

    def test_returns_reschedule_with_date(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_anthropic",
            lambda system, user, max_tokens: (
                '{"type": "RESCHEDULE", "local_date": "2099-02-01", '
                '"local_time": "10:00", "reason": "moved"}'
            ),
        )
        out = llm.verify_hearing(
            case_name="US v. X",
            court_id="mad",
            court_tz="America/New_York",
            hearing=_hearing(),
            recent_entries=[],
        )
        assert out["type"] == "RESCHEDULE"
        assert out["local_date"] == "2099-02-01"

    def test_strips_markdown_fences(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_anthropic",
            lambda system, user, max_tokens: (
                '```json\n{"type": "CANCEL", "reason": "vacated"}\n```'
            ),
        )
        out = llm.verify_hearing(
            case_name="US v. X",
            court_id="mad",
            court_tz="America/New_York",
            hearing=_hearing(),
            recent_entries=[],
        )
        assert out["type"] == "CANCEL"

    def test_unwraps_actions_array(self, monkeypatch):
        # Defensive: model might emit {"actions": [...]} despite the prompt.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_anthropic",
            lambda system, user, max_tokens: (
                '{"actions": [{"type": "MARK_HELD", "reason": "held"}]}'
            ),
        )
        out = llm.verify_hearing(
            case_name="US v. X",
            court_id="mad",
            court_tz="America/New_York",
            hearing=_hearing(),
            recent_entries=[],
        )
        assert out["type"] == "MARK_HELD"

    def test_non_json_response_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_anthropic",
            lambda system, user, max_tokens: "I cannot determine.",
        )
        out = llm.verify_hearing(
            case_name="US v. X",
            court_id="mad",
            court_tz="America/New_York",
            hearing=_hearing(),
            recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"

    def test_missing_type_field_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_anthropic",
            lambda system, user, max_tokens: '{"reason": "no type field"}',
        )
        out = llm.verify_hearing(
            case_name="US v. X",
            court_id="mad",
            court_tz="America/New_York",
            hearing=_hearing(),
            recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"

    def test_llm_call_failure_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

        def boom(system, user, max_tokens):
            raise RuntimeError("api down")

        monkeypatch.setattr(llm, "_call_anthropic", boom)
        out = llm.verify_hearing(
            case_name="US v. X",
            court_id="mad",
            court_tz="America/New_York",
            hearing=_hearing(),
            recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"

    def test_output_truncated_returns_unclear_with_named_reason(
        self, monkeypatch, caplog
    ):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

        def boom(system, user, max_tokens):
            raise llm.OutputTruncatedError("anthropic", '{"type":', 512)

        monkeypatch.setattr(llm, "_call_anthropic", boom)
        with caplog.at_level("WARNING", logger="case_calendar.llm"):
            out = llm.verify_hearing(
                case_name="US v. X",
                court_id="mad",
                court_tz="America/New_York",
                hearing=_hearing(),
                recent_entries=[],
            )
        assert out == {"type": "UNCLEAR", "reason": "llm output truncated"}
        msg = "\n".join(r.getMessage() for r in caplog.records)
        assert "truncated" in msg and "max_tokens=512" in msg

    def test_user_message_includes_hearing_and_entries(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        captured = {}

        def fake(system, user, max_tokens):
            captured["user"] = user
            captured["system"] = system
            return '{"type": "CONFIRM"}'

        monkeypatch.setattr(llm, "_call_anthropic", fake)
        llm.verify_hearing(
            case_name="US v. X",
            court_id="mad",
            court_tz="America/New_York",
            hearing=_hearing(),
            recent_entries=[
                {
                    "entry_number": 50,
                    "entry_id": 9999,
                    "date_filed": "2026-04-01",
                    "description": "Order vacating trial date",
                },
            ],
        )
        assert "trial-x" in captured["user"]
        assert "Order vacating trial date" in captured["user"]
        assert "audit a single court hearing" in captured["system"]

    def test_dispatches_to_openai(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_openai",
            lambda system, user, max_tokens: '{"type": "CONFIRM"}',
        )
        out = llm.verify_hearing(
            case_name="X",
            court_id="x",
            court_tz="UTC",
            hearing=_hearing(),
            recent_entries=[],
        )
        assert out["type"] == "CONFIRM"

    def test_dispatches_to_gemini(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_gemini",
            lambda system, user, max_tokens: '{"type": "CONFIRM"}',
        )
        out = llm.verify_hearing(
            case_name="X",
            court_id="x",
            court_tz="UTC",
            hearing=_hearing(),
            recent_entries=[],
        )
        assert out["type"] == "CONFIRM"

    def test_no_provider_raises(self):
        with pytest.raises(RuntimeError, match="No LLM provider"):
            llm.verify_hearing(
                case_name="x",
                court_id="x",
                court_tz="x",
                hearing=_hearing(),
                recent_entries=[],
            )

    def test_empty_actions_array_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_anthropic",
            lambda *a, **kw: '{"actions": []}',
        )
        out = llm.verify_hearing(
            case_name="X",
            court_id="x",
            court_tz="UTC",
            hearing=_hearing(),
            recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"


# --- extract_actions provider dispatch ---


class TestExtractActionsDispatch:
    def test_dispatches_to_openai(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_openai",
            lambda system, user, max_tokens: '{"actions": [{"type": "IGNORE"}]}',
        )
        out = llm.extract_actions(
            case_name="x",
            court_id="x",
            court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[],
            known_hearings=[],
        )
        assert out == [{"type": "IGNORE"}]

    def test_dispatches_to_gemini(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_gemini",
            lambda system, user, max_tokens: '{"actions": [{"type": "IGNORE"}]}',
        )
        out = llm.extract_actions(
            case_name="x",
            court_id="x",
            court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[],
            known_hearings=[],
        )
        assert out == [{"type": "IGNORE"}]


# --- build_user_message: deadlines + referenced_entries ---


class TestBuildUserMessageOptionalBlocks:
    def test_known_deadlines_block_only_when_passed(self):
        msg_off = llm.build_user_message(
            case_name="x",
            court_id="x",
            court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[],
            known_hearings=[],
            known_deadlines=None,
        )
        assert "KNOWN DEADLINES" not in msg_off

        msg_empty = llm.build_user_message(
            case_name="x",
            court_id="x",
            court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[],
            known_hearings=[],
            known_deadlines=[],
        )
        assert "KNOWN DEADLINES" in msg_empty
        assert "(no deadlines known yet)" in msg_empty

        msg_full = llm.build_user_message(
            case_name="x",
            court_id="x",
            court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[],
            known_hearings=[],
            known_deadlines=[
                {
                    "deadline_key": "reply-mtd",
                    "status": "pending",
                    "title": "Reply ISO MTD",
                    "due_at_utc": "2026-05-31T21:00:00+00:00",
                    "deadline_type": "reply",
                    "docket_id": 100,
                }
            ],
        )
        assert "reply-mtd" in msg_full
        assert "Reply ISO MTD" in msg_full

    def test_referenced_entries_block(self):
        msg = llm.build_user_message(
            case_name="x",
            court_id="x",
            court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[],
            known_hearings=[],
            referenced_entries=[
                {
                    "entry_number": 65,
                    "date_filed": "2026-01-01",
                    "description": "Motion to Continue trial",
                },
                {
                    "entry_number": 66,
                    "short_description": "",
                    "description": "",
                },  # empty -> skipped
            ],
        )
        assert "RELATED DOCKET ENTRIES" in msg
        assert "Motion to Continue trial" in msg

    def test_referenced_entries_all_empty_drops_block(self):
        msg = llm.build_user_message(
            case_name="x",
            court_id="x",
            court_tz="x",
            entry={"id": 1, "description": "", "recap_documents": []},
            pdf_texts=[],
            known_hearings=[],
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
        block = MagicMock()
        block.type = "text"
        block.text = "ok"
        fake_client.messages.create.return_value.content = [block]
        monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

        llm._call_anthropic("s", "u", 50, model="claude-opus-4-7")
        assert (
            fake_client.messages.create.call_args.kwargs["model"] == "claude-opus-4-7"
        )

    def test_no_text_block_raises(self, monkeypatch):
        from unittest.mock import MagicMock
        import sys

        fake_mod = MagicMock()
        fake_client = MagicMock()
        fake_mod.Anthropic.return_value = fake_client
        non_text = MagicMock()
        non_text.type = "tool_use"
        fake_client.messages.create.return_value.content = [non_text]
        monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

        with pytest.raises(ValueError, match="No text block"):
            llm._call_anthropic("s", "u", 10)

    def test_constructor_sets_generous_max_retries(self, monkeypatch):
        # The SDK default is 2 (cumulative backoff ~1.5s) — too short
        # to ride out an Anthropic 529 Overloaded condition, which can
        # last tens of seconds. Pin the higher value so a future bump
        # of the SDK default downward doesn't silently regress us into
        # losing entries on overload.
        from unittest.mock import MagicMock
        import sys

        fake_mod = MagicMock(name="anthropic")
        fake_client = MagicMock()
        fake_mod.Anthropic.return_value = fake_client
        block = MagicMock()
        block.type = "text"
        block.text = "ok"
        fake_client.messages.create.return_value.content = [block]
        monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

        llm._call_anthropic("s", "u", 10)
        ctor_kwargs = fake_mod.Anthropic.call_args.kwargs
        assert ctor_kwargs["max_retries"] >= 5

    def test_max_tokens_stop_reason_raises_truncated(self, monkeypatch):
        from unittest.mock import MagicMock
        import sys

        fake_mod = MagicMock(name="anthropic")
        fake_client = MagicMock()
        fake_mod.Anthropic.return_value = fake_client
        block = MagicMock()
        block.type = "text"
        block.text = '{"actions": [{"type": "RESCHEDULE_DEADLINE", "notes":'
        resp = fake_client.messages.create.return_value
        resp.content = [block]
        resp.stop_reason = "max_tokens"
        monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

        with pytest.raises(llm.OutputTruncatedError) as exc_info:
            llm._call_anthropic("s", "u", 2048)
        assert exc_info.value.provider == "anthropic"
        assert exc_info.value.max_tokens == 2048
        # Partial text is preserved on the exception for logging.
        assert exc_info.value.partial.startswith('{"actions":')


class TestCallOpenAI:
    def test_returns_message_content(self, monkeypatch):
        from unittest.mock import MagicMock
        import sys

        fake_mod = MagicMock(name="openai")
        fake_client = MagicMock()
        fake_mod.OpenAI.return_value = fake_client
        msg = MagicMock()
        msg.content = '{"actions": []}'
        choice = MagicMock()
        choice.message = msg
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
        msg = MagicMock()
        msg.content = "prose"
        choice = MagicMock()
        choice.message = msg
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
        msg = MagicMock()
        msg.content = ""
        choice = MagicMock()
        choice.message = msg
        fake_client.chat.completions.create.return_value.choices = [choice]
        monkeypatch.setitem(sys.modules, "openai", fake_mod)

        with pytest.raises(ValueError, match="No content"):
            llm._call_openai("s", "u", 10)

    def test_constructor_sets_generous_max_retries(self, monkeypatch):
        # SDK default of 2 retries is too short for transient overload;
        # see the matching pin on `_call_anthropic`.
        from unittest.mock import MagicMock
        import sys

        fake_mod = MagicMock(name="openai")
        fake_client = MagicMock()
        fake_mod.OpenAI.return_value = fake_client
        msg = MagicMock()
        msg.content = "ok"
        choice = MagicMock()
        choice.message = msg
        fake_client.chat.completions.create.return_value.choices = [choice]
        monkeypatch.setitem(sys.modules, "openai", fake_mod)

        llm._call_openai("s", "u", 10)
        ctor_kwargs = fake_mod.OpenAI.call_args.kwargs
        assert ctor_kwargs["max_retries"] >= 5

    def test_length_finish_reason_raises_truncated(self, monkeypatch):
        from unittest.mock import MagicMock
        import sys

        fake_mod = MagicMock(name="openai")
        fake_client = MagicMock()
        fake_mod.OpenAI.return_value = fake_client
        msg = MagicMock()
        msg.content = '{"actions": [{"type":'
        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = "length"
        fake_client.chat.completions.create.return_value.choices = [choice]
        monkeypatch.setitem(sys.modules, "openai", fake_mod)

        with pytest.raises(llm.OutputTruncatedError) as exc_info:
            llm._call_openai("s", "u", 2048)
        assert exc_info.value.provider == "openai"
        assert exc_info.value.max_tokens == 2048


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
        fake_google = MagicMock()
        fake_google.genai = fake_genai
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

        fake_google = MagicMock()
        fake_google.genai = fake_genai
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

        fake_google = MagicMock()
        fake_google.genai = fake_genai
        monkeypatch.setitem(sys.modules, "google", fake_google)
        monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
        monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)

        with pytest.raises(ValueError, match="No content"):
            llm._call_gemini("s", "u", 10)

    def test_max_tokens_finish_reason_raises_truncated(self, monkeypatch):
        from unittest.mock import MagicMock
        import sys

        fake_genai = MagicMock()
        fake_types = MagicMock()
        fake_types.GenerateContentConfig = lambda **kw: object()
        fake_genai.types = fake_types
        fake_client = MagicMock()
        fake_genai.Client.return_value = fake_client
        resp = fake_client.models.generate_content.return_value
        resp.text = '{"actions": [{"type":'
        # Gemini's finish_reason is an enum with `.name == "MAX_TOKENS"`.
        finish = MagicMock()
        finish.name = "MAX_TOKENS"
        candidate = MagicMock()
        candidate.finish_reason = finish
        resp.candidates = [candidate]

        fake_google = MagicMock()
        fake_google.genai = fake_genai
        monkeypatch.setitem(sys.modules, "google", fake_google)
        monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
        monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)

        with pytest.raises(llm.OutputTruncatedError) as exc_info:
            llm._call_gemini("s", "u", 2048)
        assert exc_info.value.provider == "gemini"
        assert exc_info.value.max_tokens == 2048

    def test_no_candidates_returns_text_without_truncation_check(self, monkeypatch):
        # Gemini responses without a `candidates` list (or with an empty
        # one) should fall through to the plain text return — the
        # truncation check only applies when at least one candidate is
        # present. Pin both shapes so a refactor can't drop this fast
        # path silently.
        from unittest.mock import MagicMock
        import sys

        fake_genai = MagicMock()
        fake_types = MagicMock()
        fake_types.GenerateContentConfig = lambda **kw: object()
        fake_genai.types = fake_types
        fake_client = MagicMock()
        fake_genai.Client.return_value = fake_client
        resp = fake_client.models.generate_content.return_value
        resp.text = '{"actions": []}'
        resp.candidates = []  # explicit empty — bypasses the truncation branch

        fake_google = MagicMock()
        fake_google.genai = fake_genai
        monkeypatch.setitem(sys.modules, "google", fake_google)
        monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
        monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)

        out = llm._call_gemini("s", "u", 50)
        assert out == '{"actions": []}'


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
                case_name="x",
                court_id="x",
                court_tz="x",
                deadline=_deadline(),
                recent_entries=[],
            )

    def test_returns_confirm(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_anthropic",
            lambda *a, **kw: '{"type": "CONFIRM", "reason": "still pending"}',
        )
        out = llm.verify_deadline(
            case_name="x",
            court_id="x",
            court_tz="UTC",
            deadline=_deadline(),
            recent_entries=[],
        )
        assert out["type"] == "CONFIRM"

    def test_dispatches_to_openai(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_openai",
            lambda *a, **kw: '{"type": "MARK_FILED"}',
        )
        out = llm.verify_deadline(
            case_name="x",
            court_id="x",
            court_tz="UTC",
            deadline=_deadline(),
            recent_entries=[],
        )
        assert out["type"] == "MARK_FILED"

    def test_dispatches_to_gemini(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_gemini",
            lambda *a, **kw: '{"type": "RESCHEDULE", "local_date": "2026-06-15"}',
        )
        out = llm.verify_deadline(
            case_name="x",
            court_id="x",
            court_tz="UTC",
            deadline=_deadline(),
            recent_entries=[],
        )
        assert out["type"] == "RESCHEDULE"

    def test_strips_fences(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_anthropic",
            lambda *a, **kw: '```json\n{"type": "CANCEL"}\n```',
        )
        out = llm.verify_deadline(
            case_name="x",
            court_id="x",
            court_tz="UTC",
            deadline=_deadline(),
            recent_entries=[],
        )
        assert out["type"] == "CANCEL"

    def test_unwraps_actions_array(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_anthropic",
            lambda *a, **kw: '{"actions": [{"type": "CANCEL"}]}',
        )
        out = llm.verify_deadline(
            case_name="x",
            court_id="x",
            court_tz="UTC",
            deadline=_deadline(),
            recent_entries=[],
        )
        assert out["type"] == "CANCEL"

    def test_empty_actions_array_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_anthropic",
            lambda *a, **kw: '{"actions": []}',
        )
        out = llm.verify_deadline(
            case_name="x",
            court_id="x",
            court_tz="UTC",
            deadline=_deadline(),
            recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"

    def test_non_json_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_anthropic",
            lambda *a, **kw: "can't tell",
        )
        out = llm.verify_deadline(
            case_name="x",
            court_id="x",
            court_tz="UTC",
            deadline=_deadline(),
            recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"

    def test_missing_type_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_anthropic",
            lambda *a, **kw: '{"reason": "x"}',
        )
        out = llm.verify_deadline(
            case_name="x",
            court_id="x",
            court_tz="UTC",
            deadline=_deadline(),
            recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"

    def test_call_failure_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

        def boom(*a, **kw):
            raise RuntimeError("api down")

        monkeypatch.setattr(llm, "_call_anthropic", boom)
        out = llm.verify_deadline(
            case_name="x",
            court_id="x",
            court_tz="UTC",
            deadline=_deadline(),
            recent_entries=[],
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
            case_name="X",
            court_id="mad",
            court_tz="UTC",
            deadline=_deadline(),
            recent_entries=[
                {
                    "entry_number": 50,
                    "entry_id": 99,
                    "date_filed": "2026-05-01",
                    "description": "Filed reply brief",
                },
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
            case_name="X",
            court_id="x",
            court_tz="x",
            deadline=_deadline(),
            recent_entries=[],
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
            case_name="X",
            court_id="x",
            court_tz="x",
            hearing=_hearing(),
            recent_entries=[],
        )
        assert "(none)" in captured["user"]


class TestVerifyUserMessageNeverShowsAuditNotes:
    """Load-bearing structural invariant: the verify-pass LLM is fed
    ``notes`` (docket-derived context) but NEVER ``audit_notes`` (its
    own prior conclusions). Violating this collapses the column split
    back into the McGonigal-shape circular-reasoning bug. If a future
    edit adds ``audit_notes`` to the user message, these tests fail.
    """

    def test_hearing_audit_notes_never_in_user_message(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        captured: dict[str, str] = {}

        def fake(system, user, max_tokens):
            captured["user"] = user
            return '{"type": "CONFIRM"}'

        monkeypatch.setattr(llm, "_call_anthropic", fake)
        hearing = _hearing(
            notes="Trial commences June 12, 2024.",
            audit_notes=(
                "[verify-pass] DO NOT LEAK THIS — "
                "if the verify LLM reads its own prior reason, the bug is back."
            ),
        )
        llm.verify_hearing(
            case_name="X",
            court_id="x",
            court_tz="x",
            hearing=hearing,
            recent_entries=[],
        )
        # The docket-derived notes ARE shown — verify needs them as context.
        assert "Trial commences June 12, 2024." in captured["user"]
        # The audit text is structurally invisible to the LLM.
        assert "DO NOT LEAK THIS" not in captured["user"]
        assert "[verify-pass]" not in captured["user"]

    def test_deadline_audit_notes_never_in_user_message(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        captured: dict[str, str] = {}

        def fake(system, user, max_tokens):
            captured["user"] = user
            return '{"type": "CONFIRM"}'

        monkeypatch.setattr(llm, "_call_anthropic", fake)
        deadline = _deadline(
            notes="Reply due 2/1/2026.",
            audit_notes="[verify-pass] DO NOT LEAK THIS deadline audit reason.",
        )
        llm.verify_deadline(
            case_name="X",
            court_id="x",
            court_tz="x",
            deadline=deadline,
            recent_entries=[],
        )
        assert "Reply due 2/1/2026." in captured["user"]
        assert "DO NOT LEAK THIS" not in captured["user"]
        assert "[verify-pass]" not in captured["user"]


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
            llm,
            "_call_anthropic",
            lambda system, user, max_tokens: (
                '{"type": "MERGE_INTO", '
                '"target_key": "msj-hearing-anthropic-v-usdw", '
                '"reason": "Same slot — order called the SJ hearing a Motion Hearing."}'
            ),
        )
        out = llm.resolve_duplicate_hearings(
            case_name="Anthropic v. DOW",
            court_id="cand",
            court_tz="America/Los_Angeles",
            cluster=self._cluster(),
            recent_entries=[],
        )
        assert out["type"] == "MERGE_INTO"
        assert out["target_key"] == "msj-hearing-anthropic-v-usdw"

    def test_returns_keep_both_when_truly_distinct(self, monkeypatch):
        # Stacked back-to-back proceedings — the LLM keeps both.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_anthropic",
            lambda system, user, max_tokens: (
                '{"type": "KEEP_BOTH", "reason": "Order schedules both back-to-back."}'
            ),
        )
        out = llm.resolve_duplicate_hearings(
            case_name="US v. X",
            court_id="dcd",
            court_tz="America/New_York",
            cluster=self._cluster(),
            recent_entries=[],
        )
        assert out["type"] == "KEEP_BOTH"

    def test_strips_markdown_fences(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_anthropic",
            lambda system, user, max_tokens: (
                '```json\n{"type": "MERGE_INTO", '
                '"target_key": "msj-hearing-anthropic-v-usdw", '
                '"reason": "..."}\n```'
            ),
        )
        out = llm.resolve_duplicate_hearings(
            case_name="X",
            court_id="cand",
            court_tz="America/Los_Angeles",
            cluster=self._cluster(),
            recent_entries=[],
        )
        assert out["type"] == "MERGE_INTO"

    def test_unwraps_actions_array(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_anthropic",
            lambda system, user, max_tokens: (
                '{"actions": [{"type": "KEEP_BOTH", "reason": "..."}]}'
            ),
        )
        out = llm.resolve_duplicate_hearings(
            case_name="X",
            court_id="x",
            court_tz="UTC",
            cluster=self._cluster(),
            recent_entries=[],
        )
        assert out["type"] == "KEEP_BOTH"

    def test_empty_actions_array_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_anthropic",
            lambda system, user, max_tokens: '{"actions": []}',
        )
        out = llm.resolve_duplicate_hearings(
            case_name="X",
            court_id="x",
            court_tz="UTC",
            cluster=self._cluster(),
            recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"

    def test_non_json_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_anthropic",
            lambda system, user, max_tokens: "I cannot tell.",
        )
        out = llm.resolve_duplicate_hearings(
            case_name="X",
            court_id="x",
            court_tz="UTC",
            cluster=self._cluster(),
            recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"

    def test_missing_type_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_anthropic",
            lambda system, user, max_tokens: '{"reason": "no type"}',
        )
        out = llm.resolve_duplicate_hearings(
            case_name="X",
            court_id="x",
            court_tz="UTC",
            cluster=self._cluster(),
            recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"

    def test_llm_call_failure_returns_unclear(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

        def boom(system, user, max_tokens):
            raise RuntimeError("api down")

        monkeypatch.setattr(llm, "_call_anthropic", boom)
        out = llm.resolve_duplicate_hearings(
            case_name="X",
            court_id="x",
            court_tz="UTC",
            cluster=self._cluster(),
            recent_entries=[],
        )
        assert out["type"] == "UNCLEAR"

    def test_no_provider_configured_raises(self, monkeypatch):
        # Strip every *_API_KEY and LLM_PROVIDER override so detection fails.
        for k in (
            "LLM_PROVIDER",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
        ):
            monkeypatch.delenv(k, raising=False)
        with pytest.raises(RuntimeError, match="No LLM provider"):
            llm.resolve_duplicate_hearings(
                case_name="X",
                court_id="x",
                court_tz="UTC",
                cluster=self._cluster(),
                recent_entries=[],
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
            case_name="Anthropic v. DOW",
            court_id="cand",
            court_tz="America/Los_Angeles",
            cluster=self._cluster(),
            recent_entries=[
                {
                    "entry_number": 150,
                    "entry_id": 461818939,
                    "date_filed": "2026-04-23",
                    "description": "ORDER RE 149 STIPULATION ...",
                }
            ],
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
            case_name="X",
            court_id="x",
            court_tz="UTC",
            cluster=self._cluster(),
            recent_entries=[],
        )
        assert "(none)" in captured["user"]

    def test_openai_provider_dispatch(self, monkeypatch):
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_openai",
            lambda system, user, max_tokens: (
                '{"type": "MERGE_INTO", '
                '"target_key": "msj-hearing-anthropic-v-usdw", '
                '"reason": "..."}'
            ),
        )
        out = llm.resolve_duplicate_hearings(
            case_name="X",
            court_id="x",
            court_tz="UTC",
            cluster=self._cluster(),
            recent_entries=[],
        )
        assert out["type"] == "MERGE_INTO"

    def test_gemini_provider_dispatch(self, monkeypatch):
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_gemini",
            lambda system, user, max_tokens: '{"type": "KEEP_BOTH", "reason": "..."}',
        )
        out = llm.resolve_duplicate_hearings(
            case_name="X",
            court_id="x",
            court_tz="UTC",
            cluster=self._cluster(),
            recent_entries=[],
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
            primary_documents=[
                {
                    "entry_number": 1,
                    "description": "INDICTMENT",
                    "date_filed": "2024-01-01",
                    "text": "Body of indictment...",
                }
            ],
            disposition_documents=[
                {
                    "entry_number": 99,
                    "description": "JUDGMENT",
                    "date_filed": "2025-06-15",
                    "text": "Judgment body...",
                }
            ],
            hearings=[
                {
                    "title": "Sentencing",
                    "status": "held",
                    "starts_at_utc": "2025-06-10T15:00:00+00:00",
                    "significance": "major",
                }
            ],
            deadlines=[
                {
                    "title": "Reply ISO MTD",
                    "status": "met",
                    "due_at_utc": "2024-12-15T22:00:00+00:00",
                    "deadline_type": "reply",
                }
            ],
            primary_char_budget=10_000,
            disposition_char_budget=10_000,
        )
        assert "US v. X" in msg
        assert "Parallel district + appellate" in msg
        assert "INDICTMENT" in msg
        assert "JUDGMENT" in msg
        assert "Sentencing" in msg
        assert "Reply ISO MTD" in msg

    def test_empty_hearings_and_deadlines_show_placeholders(self):
        msg = llm._build_summary_user_message(
            case_name="X",
            aggregation_note=None,
            docket={"docket_number": "x", "court_id": "x"},
            primary_documents=[],
            disposition_documents=[],
            hearings=[],
            deadlines=[],
            primary_char_budget=100,
            disposition_char_budget=100,
        )
        assert "(none recorded)" in msg
        # primary_documents empty -> "no primary document text available"
        assert "no primary document text available" in msg
        # disposition_documents empty -> "(none)"
        assert "(none)" in msg

    def test_sealing_advisory_block_emitted_when_present(self):
        msg = llm._build_summary_user_message(
            case_name="US v. Dubranova",
            aggregation_note=None,
            docket={"docket_number": "2:25-cr-578", "court_citation": "C.D. Cal."},
            primary_documents=[],
            disposition_documents=[],
            hearings=[],
            deadlines=[],
            primary_char_budget=100,
            disposition_char_budget=100,
            sealing_advisory={
                "sealing_entry_number": 44,
                "sealing_date_filed": "2025-08-21",
                "sealing_description": "ORDER granting 43 EX PARTE APPLICATION to Seal Indictment and Related Documents",
                "available_post_seal_entries": 1,
            },
        )
        assert "DOCKET VISIBILITY ADVISORY" in msg
        assert "entry #44" in msg
        assert "2025-08-21" in msg
        assert "ORDER granting 43 EX PARTE APPLICATION to Seal" in msg
        # The block must call out that it's trusted operator-supplied
        # metadata, not document text — same convention as AGGREGATION
        # NOTE so the system prompt's "untrusted text" rule doesn't
        # apply to it.
        assert "trusted" in msg.lower()
        # The observed available-post-seal count rides along so the
        # model knows how marginal the signal is (1 post-seal available
        # entry is very different from 3 — both pass the threshold but
        # one is more borderline).
        assert "post-seal entry count: 1" in msg

    def test_sealing_advisory_block_omitted_when_absent(self):
        # Default (no advisory passed) — block must not appear.
        msg = llm._build_summary_user_message(
            case_name="X",
            aggregation_note=None,
            docket={"docket_number": "x", "court_id": "x"},
            primary_documents=[],
            disposition_documents=[],
            hearings=[],
            deadlines=[],
            primary_char_budget=100,
            disposition_char_budget=100,
        )
        assert "DOCKET VISIBILITY ADVISORY" not in msg

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
            primary_documents=[],
            disposition_documents=[],
            hearings=[],
            deadlines=[
                {
                    "title": "Appellants' Motion for Appropriate Relief",
                    "status": "pending",
                    "due_at_utc": None,
                    "deadline_type": None,
                    "notes": "Appellants must file within 21 days after "
                    "resolution of related D.C. Cir. case 26-1049.",
                }
            ],
            primary_char_budget=100,
            disposition_char_budget=100,
        )
        assert "due_at_utc=None" in msg
        assert "21 days after resolution" in msg

    def test_fixed_deadline_does_not_inline_notes(self):
        # Non-conditional deadlines keep the scaffold line tight — the
        # date already says everything; notes would just add noise.
        msg = llm._build_summary_user_message(
            case_name="X",
            aggregation_note=None,
            docket={"docket_number": "x", "court_id": "x"},
            primary_documents=[],
            disposition_documents=[],
            hearings=[],
            deadlines=[
                {
                    "title": "Govt response to MTD",
                    "status": "pending",
                    "due_at_utc": "2026-05-24T21:00:00+00:00",
                    "deadline_type": "response",
                    "notes": "Some operator-added side note that shouldn't reach the LLM.",
                }
            ],
            primary_char_budget=100,
            disposition_char_budget=100,
        )
        assert "Some operator-added side note" not in msg

    def test_omits_aggregation_note_when_unset(self):
        msg = llm._build_summary_user_message(
            case_name="X",
            aggregation_note=None,
            docket={"docket_number": "x", "court_id": "x"},
            primary_documents=[],
            disposition_documents=[],
            hearings=[],
            deadlines=[],
            primary_char_budget=100,
            disposition_char_budget=100,
        )
        assert "AGGREGATION NOTE" not in msg

    def test_extra_documents_render_in_their_own_section(self):
        # extra_documents (from operator-supplied URLs) render in a
        # distinct "EXTRA DOCUMENTS PROVIDED BY OPERATOR" section after
        # the primary-document and disposition slots. Each entry is
        # labeled with its source URL and the operator's required note.
        msg = llm._build_summary_user_message(
            case_name="US v. Zewei",
            aggregation_note=None,
            docket={"docket_number": "4:23-cr-00523", "court_citation": "S.D. Tex."},
            primary_documents=[],
            disposition_documents=[],
            extra_documents=[
                {
                    "entry_id": None,
                    "entry_number": None,
                    "description": "operator-provided document",
                    "date_filed": None,
                    "text": "REDACTED INDICTMENT body...",
                    "source_url": "https://www.justice.gov/opa/media/1407196/dl",
                    "operator_note": "This is the unsealed indictment. CourtListener "
                    "entries 1-4 are missing due to bug #7345.",
                }
            ],
            hearings=[],
            deadlines=[],
            primary_char_budget=10_000,
            disposition_char_budget=10_000,
        )
        assert "EXTRA DOCUMENTS PROVIDED BY OPERATOR" in msg
        assert "OPERATOR-PROVIDED DOCUMENT" in msg
        assert "https://www.justice.gov/opa/media/1407196/dl" in msg
        assert "NOTE FROM OPERATOR:" in msg
        assert "bug #7345" in msg
        assert "REDACTED INDICTMENT body" in msg
        # No spurious "entry #None" header on the operator-provided doc.
        assert "entry #None" not in msg
        # Extras section sits AFTER the disposition section in the message.
        extras_pos = msg.index("EXTRA DOCUMENTS PROVIDED BY OPERATOR")
        disp_pos = msg.index("DISPOSITION / KEY ORDER DOCUMENTS")
        assert disp_pos < extras_pos

    def test_extras_section_omitted_when_no_extras(self):
        # When no extra_documents are present, the EXTRA DOCUMENTS section
        # header doesn't render at all — keeps the prompt tight.
        msg = llm._build_summary_user_message(
            case_name="X",
            aggregation_note=None,
            docket={"docket_number": "x", "court_id": "x"},
            primary_documents=[],
            disposition_documents=[],
            hearings=[],
            deadlines=[],
            primary_char_budget=100,
            disposition_char_budget=100,
        )
        assert "EXTRA DOCUMENTS" not in msg

    def test_extras_section_omitted_when_extras_list_empty(self):
        msg = llm._build_summary_user_message(
            case_name="X",
            aggregation_note=None,
            docket={"docket_number": "x", "court_id": "x"},
            primary_documents=[],
            disposition_documents=[],
            extra_documents=[],
            hearings=[],
            deadlines=[],
            primary_char_budget=100,
            disposition_char_budget=100,
        )
        assert "EXTRA DOCUMENTS" not in msg


class TestGenerateDocketSummary:
    def test_no_provider_raises(self):
        with pytest.raises(RuntimeError, match="No LLM provider"):
            llm.generate_docket_summary(
                case_name="x",
                aggregation_note=None,
                docket={"docket_number": "x"},
                primary_documents=[],
                disposition_documents=[],
                hearings=[],
                deadlines=[],
            )

    def test_unknown_provider_kwarg_raises(self):
        with pytest.raises(RuntimeError, match="unknown provider"):
            llm.generate_docket_summary(
                case_name="x",
                aggregation_note=None,
                docket={"docket_number": "x"},
                primary_documents=[],
                disposition_documents=[],
                hearings=[],
                deadlines=[],
                provider="bogus",
            )

    def test_anthropic_default_model(self, monkeypatch):
        called: dict[str, Any] = {}

        def fake(system, user, max_tokens, *, model=None):
            called["model"] = model
            return "A short summary."

        monkeypatch.setattr(llm, "_call_anthropic", fake)
        text, ident = llm.generate_docket_summary(
            case_name="x",
            aggregation_note=None,
            docket={"docket_number": "x"},
            primary_documents=[],
            disposition_documents=[],
            hearings=[],
            deadlines=[],
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
            case_name="x",
            aggregation_note=None,
            docket={"docket_number": "x"},
            primary_documents=[],
            disposition_documents=[],
            hearings=[],
            deadlines=[],
            provider="openai",
            model="custom-model",
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
            case_name="x",
            aggregation_note=None,
            docket={"docket_number": "x"},
            primary_documents=[],
            disposition_documents=[],
            hearings=[],
            deadlines=[],
            provider="gemini",
        )
        assert ident.startswith("gemini/")
        assert called["json_mode"] is False

    def test_strips_code_fences_from_response(self, monkeypatch):
        monkeypatch.setattr(
            llm,
            "_call_anthropic",
            lambda *a, **kw: "```\nThe case is summarized.\n```",
        )
        text, _ = llm.generate_docket_summary(
            case_name="x",
            aggregation_note=None,
            docket={"docket_number": "x"},
            primary_documents=[],
            disposition_documents=[],
            hearings=[],
            deadlines=[],
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
            case_name="x",
            aggregation_note=None,
            docket={"docket_number": "x"},
            primary_documents=[],
            disposition_documents=[],
            hearings=[],
            deadlines=[],
        )
        assert ident == "anthropic/claude-opus-4-7"
        assert called["model"] == "claude-opus-4-7"

    def test_falls_back_to_extractor_provider(self, monkeypatch):
        # No LLM_SUMMARY_PROVIDER set; _detect_provider should pick anthropic
        # from the regular key.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(
            llm,
            "_call_anthropic",
            lambda *a, **kw: "ok",
        )
        text, ident = llm.generate_docket_summary(
            case_name="x",
            aggregation_note=None,
            docket={"docket_number": "x"},
            primary_documents=[],
            disposition_documents=[],
            hearings=[],
            deadlines=[],
        )
        # Picks Sonnet (the summary-tier default), not Haiku.
        assert ident == "anthropic/" + llm._DEFAULT_SUMMARY_MODELS["anthropic"]


class TestSystemPromptAntiInferenceGuards:
    """Regression guards for the prompt sections added after the McGonigal
    hallucination — a CANCEL emitted by the LLM purely from co-defendant
    inference (held plea on one defendant → inferred "trial vacated" for
    another). These tests aren't behavioral (no real LLM call), they
    just assert the key instruction phrases survive future prompt edits.
    Delete a phrase here and you must replace it with an equivalent one,
    not just remove the test.
    """

    def test_cancel_requires_explicit_grounding(self):
        # The header is the essential phrase: a future edit should
        # not drop the explicit-grounding rule without replacing it.
        assert "CANCEL / MARK_HELD need EXPLICIT GROUNDING" in llm.SYSTEM_PROMPT

    def test_co_defendant_inference_explicitly_forbidden(self):
        # The McGonigal-shape: a held plea for one defendant must NOT
        # cancel a trial for another.
        assert "co-defendant cases" in llm.SYSTEM_PROMPT
        assert "Multi-defendant" in llm.SYSTEM_PROMPT
        # Calls out the actual failure mode — `known_hearings` is for
        # key reuse, not status inference.
        assert "KEY REUSE and same-slot detection" in llm.SYSTEM_PROMPT

    def test_absence_of_activity_explicitly_not_grounds(self):
        # Status-conf-mcgonigal-2 shape: a "no minute entry" inference
        # writing "[appears to have been vacated]" into notes.
        assert "Absence of docket activity" in llm.SYSTEM_PROMPT

    def test_notes_forbids_inferred_brackets(self):
        # The circular-reasoning trap the column split also addresses
        # structurally — but the prompt rule catches it at write time,
        # so audit_notes only collects audit-pass writes, not extractor
        # inferences.
        assert "NO inferred commentary" in llm.SYSTEM_PROMPT
        assert "circular-reasoning trap" in llm.SYSTEM_PROMPT


class TestSummaryPromptDatedReferenceGuards:
    """Regression guards for the SUMMARY_SYSTEM_PROMPT sections added
    after the McGonigal/Shestakov "trial date set" hedge — where the
    Sonnet model named scheduled events without stating their dates
    because the dates were past-but-unconfirmed and the model defaulted
    to vague language rather than describe the staleness honestly.
    """

    def test_must_state_dates_when_referencing_schedule(self):
        assert (
            "if you mention a hearing date, STATE THE DATE" in llm.SUMMARY_SYSTEM_PROMPT
        )
        # Concrete forbidden phrases — these are the actual hedges the
        # model produced. If a future edit drops them, the door reopens.
        assert '"a trial date set"' in llm.SUMMARY_SYSTEM_PROMPT
        assert '"a hearing is scheduled"' in llm.SUMMARY_SYSTEM_PROMPT
        # The good-form example must also stay so the model knows what
        # the rule looks like in practice.
        assert "a trial was set for June 12, 2024" in llm.SUMMARY_SYSTEM_PROMPT

    def test_past_dated_scheduled_rows_must_be_called_out(self):
        # The McGonigal trial-mcgonigal / status-conf-mcgonigal shape:
        # past `starts_at_utc` on a still-`scheduled` row means the
        # public docket has not confirmed occurrence or vacatur. Must
        # be described honestly, not as if upcoming.
        assert "past-dated 'scheduled' rows" in llm.SUMMARY_SYSTEM_PROMPT
        # And the model must NOT speculate about why — naming a
        # mechanism (sealed orders, missed minute entries, etc.) the
        # docket doesn't actually confirm is itself the speculation
        # this rule is supposed to prevent.
        assert "Do NOT speculate about the cause" in llm.SUMMARY_SYSTEM_PROMPT
        # The rule must be marked independent of trial-vs-plea — the
        # two govern different concerns and both apply.
        assert "independent of the trial-vs-plea invariant" in llm.SUMMARY_SYSTEM_PROMPT


class TestSummaryPromptInsufficientDocumentsRefusal:
    """Regression guards for the refuse-rather-than-fabricate rule added
    after the us-v-dubranova "CSRERI / Roskomnadzor" hallucination — the
    upstream PDF text was garbled font-encoding noise and the summary
    model invented organization names plausible enough to read as real.
    The rule tells the model that for unusable input the ONLY correct
    output is the canonical refusal sentence verbatim.
    """

    def test_constant_is_present_in_prompt(self):
        # The constant `summary.py` greps for must appear in the prompt
        # exactly — otherwise the model will emit a paraphrased version
        # and the detection in `summarize_docket` will miss it.
        assert llm.SUMMARY_INSUFFICIENT_DOCUMENTS in llm.SUMMARY_SYSTEM_PROMPT

    def test_constant_is_a_complete_sentence(self):
        # Sanity check on the constant shape so a future "shorten this"
        # refactor doesn't accidentally make it ambiguous.
        s = llm.SUMMARY_INSUFFICIENT_DOCUMENTS
        assert s.endswith("."), s
        assert "insufficient" in s.lower()
        assert "summary" in s.lower()

    def test_refusal_rule_calls_out_garbled_text_as_a_trigger(self):
        # The garbled-text case is the canonical trigger; pin it so the
        # rule survives future prompt revisions.
        assert "garbled font-encoding output" in llm.SUMMARY_SYSTEM_PROMPT
        # Explicit prohibition on fabrication to fill the gap. (The
        # phrase "organization names" wraps across a line in the prompt,
        # so collapse whitespace before matching.)
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert "Do NOT invent organization names" in normalized

    def test_refusal_overrides_length_guidance(self):
        # The 2-4-sentence rule applies to normal summaries, not the
        # fallback — make sure the prompt is explicit about this so the
        # model doesn't try to "pad out" the refusal to meet the length
        # target.
        assert "overrides the" in llm.SUMMARY_SYSTEM_PROMPT
        assert "length guidance" in llm.SUMMARY_SYSTEM_PROMPT


class TestSummaryPromptAbsenceOfActivityGuard:
    """Regression guards for the SUMMARY_SYSTEM_PROMPT block added after
    the us-v-dubranova (2:25-cr-00578, C.D. Cal.) regression — where the
    summary closed with 'No hearings or deadlines have been recorded on
    this docket, and the case remains pending.' on a docket that had
    been sealed after RECAP captured its initial state. The visible
    scaffold was empty not because the case was dormant but because
    later activity was no longer publicly visible; the model converted
    'missing from RECAP' into a positive claim about case posture.
    """

    def test_block_header_present(self):
        # The header wraps across a line in the prompt, so collapse
        # whitespace before matching.
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert (
            "do NOT assert the ABSENCE of scheduling, activity, or disposition"
        ) in normalized

    def test_explicitly_forbids_the_dubranova_phrasings(self):
        # The exact closer the Dubranova summary produced — pin it so a
        # future "shorten the rule" edit can't drop the canonical
        # forbidden form.
        assert (
            '"no hearings or deadlines have been recorded on this docket"'
            in llm.SUMMARY_SYSTEM_PROMPT
        )
        assert '"no hearings have been recorded"' in llm.SUMMARY_SYSTEM_PROMPT
        assert '"no deadlines are set"' in llm.SUMMARY_SYSTEM_PROMPT
        # The "remains pending" closer is the other half of the failure
        # mode — it sounds neutral but encodes an inference from absence
        # of disposition.
        assert '"the case remains pending"' in llm.SUMMARY_SYSTEM_PROMPT
        assert '"no disposition has been entered"' in llm.SUMMARY_SYSTEM_PROMPT
        assert '"the docket shows no recent activity"' in llm.SUMMARY_SYSTEM_PROMPT

    def test_sealed_docket_rationale_is_present(self):
        # The rule must call out sealing as the failure mode it's
        # guarding against, so a future editor doesn't read the rule as
        # blanket caution and weaken it for non-sealed cases.
        assert "sealed" in llm.SUMMARY_SYSTEM_PROMPT
        # And specifically the post-RECAP-capture re-seal flavor that
        # produced the Dubranova regression — initial-sealing alone is
        # routine and would be a misleading rationale.
        assert "re-sealed after" in llm.SUMMARY_SYSTEM_PROMPT

    def test_remains_pending_removed_from_legal_terminology_list(self):
        # The old list licensed "remains pending" / "case remains
        # pending" as legal terminology when no disposition has
        # occurred — that license is what produced the failure mode.
        # The terminology block at the top of the prompt must no longer
        # recommend it, even though the new CRITICAL block lower down
        # mentions it as a forbidden phrasing.
        prompt = llm.SUMMARY_SYSTEM_PROMPT
        terminology_block = prompt.split("CRITICAL — do NOT confuse")[0]
        assert '"remains pending"' not in terminology_block
        assert '"case remains pending"' not in terminology_block

    def test_silence_is_acceptable_explicit(self):
        # The rule must explicitly tell the model that omitting a
        # closing posture sentence is fine — otherwise it will pad with
        # something close to "remains pending" out of length pressure.
        assert (
            "Silence on procedural posture is acceptable" in llm.SUMMARY_SYSTEM_PROMPT
        )


class TestSummaryPromptVisibilityAdvisoryGuard:
    """Phase 2 prompt invariant: when a DOCKET VISIBILITY ADVISORY block
    appears in the user message (programmatic sealing detection from
    summary.detect_sealing), the summary MUST surface the sealing
    constraint to subscribers. This is the inverse of the
    absence-of-activity rule — Phase 1 forbids the model from inventing
    "the case is dormant" on an empty scaffold; Phase 2 tells the model
    that when WE'VE confirmed the scaffold is empty BECAUSE of sealing,
    it should say so.
    """

    def test_advisory_handling_rule_is_present(self):
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert (
            "when a DOCKET VISIBILITY ADVISORY block appears at the top "
            "of the user message"
        ) in normalized
        # And the rule must tell the model to SURFACE the constraint —
        # not just acknowledge it internally.
        assert "the summary MUST surface the sealing constraint" in normalized

    def test_advisory_is_trusted_metadata_like_aggregation_note(self):
        # The advisory carries an entry number, date, and verbatim
        # docket description from our programmatic detector. The prompt
        # must mark it as trusted operator-supplied metadata so the
        # "untrusted text" rule above doesn't kick in and tell the model
        # to ignore it.
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert (
            "the advisory itself is trusted operator-supplied metadata"
        ) in normalized
        # Cross-reference to AGGREGATION NOTE — the existing trusted-
        # metadata exemplar — so future editors keep the two consistent.
        assert "AGGREGATION NOTE" in llm.SUMMARY_SYSTEM_PROMPT

    def test_advisory_forbids_speculation_about_what_is_sealed(self):
        # The advisory hints that activity is hidden; the model must
        # not invent what that activity might be. Same logic as the
        # documents-only rule at the AGENTS.md level.
        assert "must NOT speculate about what is happening" in llm.SUMMARY_SYSTEM_PROMPT
        assert "behind the seal" in llm.SUMMARY_SYSTEM_PROMPT

    def test_advisory_phrasing_example_is_present(self):
        # A worked example with concrete date + entry number — same
        # style as the other CRITICAL rules. Pinning the example so a
        # future editor doesn't drop it; without it, the rule reads as
        # abstract guidance. The example wraps across a line in the
        # prompt, so collapse whitespace before matching.
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert "August 21, 2025 (entry 44)" in normalized
        # And the explicit "subsequent docket activity may not be
        # publicly visible" hedge — the exact subscriber-facing language
        # we want the model to produce.
        assert "subsequent docket activity may not be publicly visible" in normalized

    def test_advisory_rule_does_not_relax_absence_rule(self):
        # The advisory is a license to mention sealing; it is NOT a
        # license to also append "remains pending" or "no further
        # activity has been recorded". The absence-of-activity rule
        # still binds.
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert (
            'do not append "case remains pending" or any other '
            "absence-of-activity claim"
        ) in normalized
        assert "the rule above still applies" in normalized


class TestSummaryPromptDocumentNarrationGuard:
    """Phase 3 prompt invariant: when the primary document text is
    sparse / low-quality (us-v-moucka shape — pypdf returned only page
    headers from the indictment), the model must work around it
    silently and produce a normal subscriber-facing summary. The
    canonical failure mode it's blocking is meta-commentary like
    'The primary document text consists only of page-header citations
    with no substantive charge allegations visible, but...' — the
    subscriber reads a finished case summary, not a report on what the
    LLM could and couldn't extract.
    """

    def test_silent_workaround_rule_present(self):
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert (
            "work around partial or low-quality source documents SILENTLY"
        ) in normalized

    def test_canonical_forbidden_meta_commentary_pinned(self):
        # The exact opening clause us-v-moucka produced — pin it so a
        # future "shorten the rule" edit can't drop the canonical
        # forbidden form. Re-flowed across two lines in the prompt.
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert (
            '"The primary document text consists only of page-header '
            "citations with no substantive charge allegations visible, "
            'but..."'
        ) in normalized
        # And a representative variant — the LLM might phrase it as
        # "based on minute entries" or "per the limited disposition
        # documents available" instead of the moucka shape.
        assert (
            '"Based on the available minute entries, [defendant] is charged with..."'
        ) in normalized

    def test_rule_explicitly_does_not_relax_refusal_rule(self):
        # The model has THREE options in principle for a low-quality
        # input: narrate the workaround (now forbidden), refuse via
        # SUMMARY_INSUFFICIENT_DOCUMENTS, or produce a clean summary
        # from whatever signals exist. The rule must explicitly tell
        # the model the third path is preferred when feasible AND that
        # narration is NOT a fallback to refusal — otherwise the model
        # might read "no meta-commentary" as "refuse if anything is
        # partial". Both sentences wrap across lines, so normalize
        # whitespace before matching.
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert (
            "This rule does NOT relax the refuse-rather-than-fabricate rule below"
            in normalized
        )
        assert "There is NO middle ground that narrates the workaround" in normalized


class TestSummaryPromptRestitutionForfeitureSameAmountGuard:
    """Regression guards for the SUMMARY_SYSTEM_PROMPT rule added after
    the us-v-knoot (3:24-cr-00151, M.D. Tenn.) regression — the summary
    closed with "$15,100 in restitution ... with a forfeiture money
    judgment of $15,100 also entered against him." That phrasing is
    technically a faithful reading of the docket (entry 136's
    sentencing minute entry sets $15,100 restitution; entry 139's
    Order of Forfeiture (Money Judgment) sets a $15,100 forfeiture),
    but a lay subscriber reads it as two separate $15,100 obligations
    summing to $30,200 — when the forfeiture and restitution actually
    cover the SAME $15,100 of proceeds, with the forfeiture going to
    the government and the restitution to the victim. The rule tells
    the model to OMIT the forfeiture money judgment when its amount
    matches restitution: it adds noise without adding information for
    lay subscribers when the dollar figure is already stated as
    restitution.
    """

    def test_omission_rule_header_present(self):
        # The directive must be active ("OMIT") — a defensive rule
        # alone would still let the model word the relationship
        # explicitly, which the user prefers to avoid.
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert (
            "when a forfeiture money judgment against the same "
            "defendant equals the restitution amount, OMIT the "
            "forfeiture money judgment from the summary"
        ) in normalized

    def test_omission_is_deliberate_not_silent(self):
        # The model must understand this is a prompted omission, not
        # a license to silently drop financial obligations elsewhere.
        # Without this clarification it could over-generalize the rule
        # into the multi-payee territory the next rule explicitly
        # forbids.
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert (
            "This is a DELIBERATE, prompted omission, NOT a silent drop"
        ) in normalized

    def test_canonical_forbidden_phrasing_pinned(self):
        # The exact us-v-knoot closer — pin it so a future
        # "shorten the rule" edit can't drop the canonical forbidden
        # form. It wraps across multiple lines in the prompt.
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert (
            '"$15,100 in restitution ... with a forfeiture money '
            'judgment of $15,100 also entered against him"'
        ) in normalized
        # And the $30,200 sum-trap is what makes the phrasing wrong;
        # call it out by name so a future editor doesn't lose the
        # reason the rule exists.
        assert "$30,200" in llm.SUMMARY_SYSTEM_PROMPT

    def test_explicit_mention_forms_also_forbidden(self):
        # The previous iteration of the rule made the relationship
        # explicit ("in the same amount" / "for the same $15,100").
        # The new rule forbids those forms too — the user judged them
        # technically accurate but still redundant noise for lay
        # subscribers. Pin both forms as NOT acceptable.
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert (
            '"$15,100 in restitution and a forfeiture money judgment '
            'in the same amount"'
        ) in normalized
        assert (
            "the court entered a forfeiture money judgment for the same $15,100"
        ) in normalized
        # And both must appear in the NOT-acceptable list, not the
        # acceptable one. The simplest pin: the "still redundant" or
        # "same problem" framing follows each one.
        assert "still redundant noise" in normalized
        assert "same problem" in normalized

    def test_acceptable_shape_pinned(self):
        # The single acceptable shape — restitution stated, forfeiture
        # money judgment omitted entirely, summary continues to
        # non-financial details or stops. The "full stop, no mention"
        # framing is the operative signal for the model.
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert (
            '"$15,100 in restitution, and a $100 special assessment" '
            "— full stop, no mention of the forfeiture money judgment"
        ) in normalized

    def test_same_defendant_guardrail_present(self):
        # Co-defendants in a multi-defendant case can independently
        # receive matching financial orders (e.g. each ordered $15,100
        # in restitution). Those are TWO independent obligations from
        # two different defendants — dropping one would erase that
        # defendant's debt entirely. The rule must explicitly require
        # the orders run against the SAME defendant.
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert (
            "The forfeiture money judgment and the restitution are "
            "entered against the SAME defendant"
        ) in normalized
        # And a worked counter-example so the model has a concrete
        # picture of what NOT to collapse.
        assert (
            "If two co-defendants in the same case each receive "
            "matching financial orders, those are TWO independent "
            "obligations"
        ) in normalized

    def test_money_judgment_vs_identified_property_guardrail_present(self):
        # The omission rule applies only to forfeiture MONEY JUDGMENTS
        # (in personam orders to disgorge a dollar amount), not to
        # forfeiture of identified property (specific named assets).
        # Identified-property forfeiture takes things, not money, and
        # stays in the summary on its own merits.
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert (
            "The forfeiture is a MONEY JUDGMENT (an in personam order "
            "to disgorge a proceeds amount in dollars), NOT forfeiture "
            "of identified property"
        ) in normalized
        # And worked examples of identified property so the model
        # recognizes the carve-out.
        assert "houses, cars, bank accounts, cryptocurrency wallets" in normalized
        # And the mixed-judgment case (money judgment + identified
        # property in one order) must be explicit — only the money
        # judgment portion drops.
        assert ("drops only the money-judgment portion under this rule") in normalized

    def test_total_restitution_match_guardrail_present(self):
        # Guardrail #3 — the forfeiture money judgment must equal the
        # TOTAL restitution across all payees, not match a per-victim
        # figure. A $15,100 forfeiture against $15,100×2 restitution
        # (totaling $30,200) does NOT match — the forfeiture stays.
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert (
            "The forfeiture money judgment equals the TOTAL "
            "restitution amount across all victims/payees"
        ) in normalized
        # And the worked counter-example.
        assert ("$15,100 each to two victims summing to $30,200") in normalized
        assert ("the forfeiture stays in the summary") in normalized

    def test_outside_conditions_default_is_independent_line_items(self):
        # When ANY guardrail fails, the default behavior is to treat
        # each financial order as its own line item — the rule must
        # state this default explicitly so the model doesn't fall back
        # to its own heuristic.
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert (
            "Outside of all three conditions, restitution and "
            "forfeiture are their own line items"
        ) in normalized

    def test_equal_amount_multiple_payees_rule_present(self):
        # Guardrail #2 is defensive — it tells the model NOT to
        # collapse two restitution orders against the same defendant
        # at matching amounts. A defensive rule alone leaves the
        # failure-mode door open: the model can obey #2 by silently
        # dropping one of the payees rather than reporting both. The
        # positive rule forces the model to state the TOTAL and
        # itemize, so a "$15,100 in restitution" reading is unavailable
        # when the court actually ordered "$15,100 each to two
        # victims, totaling $30,200."
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert (
            "EQUAL-AMOUNT MULTIPLE PAYEES — every payee is its own order"
        ) in normalized
        # The "halve or quarter" framing — the core trap the rule is
        # closing.
        assert (
            "would silently halve (or quarter, etc.) the defendant's stated liability"
        ) in normalized

    def test_equal_amount_multiple_payees_acceptable_shapes_present(self):
        # The model needs at least one explicit good-form example to
        # anchor on. Pin the canonical "$30,200 in restitution, $15,100
        # each to Acme Corp. and Beta Inc." example so a future
        # "shorten the rule" pass doesn't drop the worked shape.
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert (
            '"$30,200 in restitution, $15,100 each to Acme Corp. and Beta Inc."'
        ) in normalized

    def test_equal_amount_multiple_payees_forbidden_shape_pinned(self):
        # The canonical wrong form — single-payee summary when the
        # judgment ordered multiple. Pin it so a future editor doesn't
        # drop the warning.
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert (
            '"$15,100 in restitution" stated once when the judgment '
            "names multiple same-amount payees"
        ) in normalized

    def test_equal_amount_multiple_payees_extends_to_forfeiture_and_schedules(self):
        # The rule must apply equally to forfeiture orders and to
        # judgments that itemize via an attached schedule — the
        # vocabulary varies but the substance is the same.
        import re

        normalized = re.sub(r"\s+", " ", llm.SUMMARY_SYSTEM_PROMPT)
        assert (
            "same rule applies to multiple equal-amount forfeiture orders"
        ) in normalized
        # "as set forth in the attached schedule" — the canonical
        # phrasing courts use to fold per-victim itemizations into a
        # schedule rather than the judgment body proper. The model
        # must recognize this form as N orders.
        assert (
            '"restitution to victims as set forth in the attached schedule"'
        ) in normalized
