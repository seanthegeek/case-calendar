"""Tests for case_calendar.llmkit.providers — the provider-agnostic call layer.

Provider auto-detection, the per-provider SDK call wrappers (mocked at the
SDK boundary via injected fake modules), the 3-way dispatch, and
provider_info. Extracted from test_llm.py when this layer moved into the
llmkit subpackage.
"""

from __future__ import annotations

import pytest

from case_calendar.llmkit import providers


class TestDetectProvider:
    def test_explicit_provider_env(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        assert providers._detect_provider() == "openai"

    def test_explicit_provider_is_normalized(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "  Anthropic ")
        assert providers._detect_provider() == "anthropic"

    def test_invalid_provider_falls_through_to_keys(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "bogus")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert providers._detect_provider() == "openai"

    def test_anthropic_key_only(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
        assert providers._detect_provider() == "anthropic"

    def test_openai_key_only(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai")
        assert providers._detect_provider() == "openai"

    def test_gemini_key_only(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "g-key")
        assert providers._detect_provider() == "gemini"

    def test_google_api_key_also_works_for_gemini(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
        assert providers._detect_provider() == "gemini"

    def test_no_keys_returns_none(self):
        assert providers._detect_provider() is None

    def test_anthropic_wins_when_all_three_set(self, monkeypatch):
        # Priority order is anthropic > gemini > openai per the 0.10.0
        # default reversion: Gemini systematically misclassifies
        # substantive deadline classes (PSR, STA, surrender for
        # service of sentence, civil-forfeiture claim/answer, sealing
        # motion practice, exhibit-filing) as procedural-minor, and
        # the project maintainer can't enumerate every federally-named
        # class to teach Gemini the priors. Anthropic's training corpus
        # covered them. A fresh operator who provisions every key
        # without setting LLM_PROVIDER lands on the recommended default.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ant")
        monkeypatch.setenv("OPENAI_API_KEY", "oai")
        monkeypatch.setenv("GEMINI_API_KEY", "g")
        assert providers._detect_provider() == "anthropic"

    def test_gemini_wins_over_openai_when_both_set(self, monkeypatch):
        # Without an ANTHROPIC key, gemini is next in the priority —
        # the published comparison ranks it best on deviation and it
        # remains substantially faster / cheaper than either OpenAI
        # tier, so an operator with only OpenAI + Gemini keys lands
        # on the better-performing column.
        monkeypatch.setenv("OPENAI_API_KEY", "oai")
        monkeypatch.setenv("GEMINI_API_KEY", "g")
        assert providers._detect_provider() == "gemini"


class TestDetectExtractionProvider:
    """``_detect_extraction_provider`` layers ``LLM_EXTRACTION_PROVIDER``
    on top of ``_detect_provider``. This is the function the four
    extractor entry points (extract / verify_hearing / verify_deadline /
    resolve_duplicate_hearings) call, so it controls which provider runs
    on the per-entry pipeline. ``LLM_PROVIDER`` continues to be the
    global default that applies when no per-track override is set.
    """

    def test_extraction_override_takes_precedence_over_global(self, monkeypatch):
        # LLM_EXTRACTION_PROVIDER beats LLM_PROVIDER for the extraction
        # track — letting an operator pin Gemini for extraction while
        # keeping Anthropic for summaries (or any other split).
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("LLM_EXTRACTION_PROVIDER", "gemini")
        assert providers._detect_extraction_provider() == "gemini"

    def test_global_provider_used_when_no_extraction_override(self, monkeypatch):
        # When only LLM_PROVIDER is set, both tracks share it.
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        assert providers._detect_extraction_provider() == "openai"

    def test_falls_through_to_key_autodetect(self, monkeypatch):
        # When neither LLM_EXTRACTION_PROVIDER nor LLM_PROVIDER is set,
        # API-key auto-detect applies in the usual priority.
        monkeypatch.setenv("GEMINI_API_KEY", "g")
        assert providers._detect_extraction_provider() == "gemini"

    def test_extraction_override_normalized(self, monkeypatch):
        # Same lower/strip normalization as LLM_PROVIDER.
        monkeypatch.setenv("LLM_EXTRACTION_PROVIDER", "  GEMINI ")
        assert providers._detect_extraction_provider() == "gemini"

    def test_invalid_extraction_override_falls_through_to_global(self, monkeypatch):
        # An unrecognized value is treated like unset and the global
        # LLM_PROVIDER / key auto-detect resolves.
        monkeypatch.setenv("LLM_EXTRACTION_PROVIDER", "bogus")
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        assert providers._detect_extraction_provider() == "anthropic"

    def test_no_provider_configured_returns_none(self):
        # Nothing set → None (caller raises the "No LLM provider
        # configured" error).
        assert providers._detect_extraction_provider() is None


# --- _parse_actions ---


class TestProviderInfo:
    def test_no_provider(self):
        assert providers.provider_info() == "no provider configured"

    def test_with_provider_default_model(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        info = providers.provider_info()
        assert "anthropic" in info
        assert "claude-haiku-4-5" in info  # the chosen default

    def test_with_model_override(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setenv("LLM_MODEL", "claude-opus-4-7")
        assert "claude-opus-4-7" in providers.provider_info()


class TestDispatchLLMCall:
    """The 3-way provider dispatch used by ``extract_actions``,
    ``_call_lm_and_parse``, and ``generate_docket_summary``. Three
    callers, one helper — these tests pin the routing so a future
    fourth caller can rely on the same behavior."""

    def test_routes_to_anthropic(self, monkeypatch):
        captured = {}

        def fake(system, user, max_tokens, **kw):
            captured["provider"] = "anthropic"
            captured["kw"] = kw
            return "ok"

        monkeypatch.setattr(providers, "_call_anthropic", fake)
        assert providers._dispatch_llm_call("anthropic", "s", "u", 100) == "ok"
        assert captured["provider"] == "anthropic"

    def test_routes_to_openai(self, monkeypatch):
        captured = {}

        def fake(system, user, max_tokens, **kw):
            captured["provider"] = "openai"
            captured["kw"] = kw
            return "ok"

        monkeypatch.setattr(providers, "_call_openai", fake)
        assert providers._dispatch_llm_call("openai", "s", "u", 100) == "ok"
        # Default json_mode=True propagates to the per-provider call so
        # the SDK's response_format kwarg fires as expected.
        assert captured["kw"]["json_mode"] is True

    def test_routes_to_gemini(self, monkeypatch):
        captured = {}

        def fake(system, user, max_tokens, **kw):
            captured["provider"] = "gemini"
            captured["kw"] = kw
            return "ok"

        # Any provider name that isn't "anthropic" or "openai" falls
        # through to gemini — matches the historical else-branch
        # behavior across all three callers.
        monkeypatch.setattr(providers, "_call_gemini", fake)
        assert providers._dispatch_llm_call("gemini", "s", "u", 100) == "ok"
        assert captured["kw"]["json_mode"] is True

    def test_model_and_json_mode_passthrough(self, monkeypatch):
        # Summary-track callers pin a higher-tier model and disable
        # JSON mode (the model returns prose, not a JSON object). The
        # helper threads both kwargs through to openai/gemini.
        captured = {}

        def fake(system, user, max_tokens, **kw):
            captured["kw"] = kw
            return "summary text"

        monkeypatch.setattr(providers, "_call_openai", fake)
        providers._dispatch_llm_call(
            "openai", "s", "u", 800, model="gpt-5.4", json_mode=False
        )
        # model + json_mode pass through (purpose/docket also ride along for
        # the token telemetry; assert the subset this test is about).
        assert captured["kw"]["model"] == "gpt-5.4"
        assert captured["kw"]["json_mode"] is False

    def test_anthropic_does_not_receive_json_mode(self, monkeypatch):
        # Anthropic's SDK has no json_mode flag — we rely on the prompt
        # to elicit JSON. The helper must NOT pass json_mode through to
        # the anthropic call function or the SDK call would raise on
        # the unexpected kwarg.
        captured = {}

        def fake(system, user, max_tokens, *, model=None, purpose="llm", docket=None):
            captured["model"] = model
            # purpose/docket are expected (token telemetry); json_mode is NOT
            # — this signature has no json_mode param, so a leak would raise.
            return "ok"

        monkeypatch.setattr(providers, "_call_anthropic", fake)
        providers._dispatch_llm_call(
            "anthropic", "s", "u", 100, model="claude-sonnet-4-6", json_mode=False
        )
        assert captured["model"] == "claude-sonnet-4-6"


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

        out = providers._call_anthropic("sys", "user", 100)
        assert out == "hello"
        kwargs = fake_client.messages.create.call_args.kwargs
        assert kwargs["model"] == providers._DEFAULT_MODELS["anthropic"]
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

        providers._call_anthropic("s", "u", 50, model="claude-opus-4-7")
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
            providers._call_anthropic("s", "u", 10)

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

        providers._call_anthropic("s", "u", 10)
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

        with pytest.raises(providers.OutputTruncatedError) as exc_info:
            providers._call_anthropic("s", "u", 2048)
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

        out = providers._call_openai("s", "u", 50)
        assert out == '{"actions": []}'
        # JSON mode is on by default and shows up as response_format.
        kw = fake_client.chat.completions.create.call_args.kwargs
        assert kw["response_format"] == {"type": "json_object"}
        # The gpt-5 family rejects `max_tokens` (400 unsupported_parameter) and
        # requires `max_completion_tokens`; pin that we send the newer name and
        # never the old one. (Regression: every openai call 400'd otherwise.)
        assert kw["max_completion_tokens"] == 50
        assert "max_tokens" not in kw

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

        providers._call_openai("s", "u", 50, json_mode=False)
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
            providers._call_openai("s", "u", 10)

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

        providers._call_openai("s", "u", 10)
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

        with pytest.raises(providers.OutputTruncatedError) as exc_info:
            providers._call_openai("s", "u", 2048)
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

        out = providers._call_gemini("s", "u", 50)
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

        providers._call_gemini("s", "u", 50, json_mode=False)
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
            providers._call_gemini("s", "u", 10)

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

        with pytest.raises(providers.OutputTruncatedError) as exc_info:
            providers._call_gemini("s", "u", 2048)
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

        out = providers._call_gemini("s", "u", 50)
        assert out == '{"actions": []}'


# --- verify_deadline (parallel to verify_hearing) ---
