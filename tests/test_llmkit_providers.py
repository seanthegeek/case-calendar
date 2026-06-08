"""Tests for case_calendar.llmkit.providers — the provider-agnostic call layer.

Provider auto-detection, the per-provider SDK call wrappers (mocked at the
SDK boundary via injected fake modules), the 3-way dispatch, and
provider_info. Extracted from test_llm.py when this layer moved into the
llmkit subpackage.
"""

from __future__ import annotations

from typing import Any

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
        # The DEFAULT _detect_provider() priority is the SUMMARY-track order
        # (anthropic > gemini > openai): the case-summary track wants
        # Anthropic's case-distinguishing prose. The EXTRACTION track passes a
        # Gemini-first order instead (see TestDetectExtractionProvider) — that
        # split is the 0.13.0 flip, now that the structured
        # DEADLINE_SIGNIFICANCE_RULES closed Gemini's deadline-bucketing gap.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ant")
        monkeypatch.setenv("OPENAI_API_KEY", "oai")
        monkeypatch.setenv("GEMINI_API_KEY", "g")
        assert providers._detect_provider() == "anthropic"

    def test_gemini_wins_over_openai_when_both_set(self, monkeypatch):
        # Default (summary) order with no ANTHROPIC key: gemini is next.
        monkeypatch.setenv("OPENAI_API_KEY", "oai")
        monkeypatch.setenv("GEMINI_API_KEY", "g")
        assert providers._detect_provider() == "gemini"

    def test_explicit_key_priority_argument(self, monkeypatch):
        # The extraction track passes a Gemini-first key_priority; with all
        # three keys that selects gemini, vs the anthropic-first default.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ant")
        monkeypatch.setenv("OPENAI_API_KEY", "oai")
        monkeypatch.setenv("GEMINI_API_KEY", "g")
        assert providers._detect_provider() == "anthropic"
        assert (
            providers._detect_provider(key_priority=providers._EXTRACTION_KEY_PRIORITY)
            == "gemini"
        )
        # LLM_PROVIDER still overrides any key_priority.
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        assert (
            providers._detect_provider(key_priority=providers._EXTRACTION_KEY_PRIORITY)
            == "openai"
        )

    def test_explicit_ollama_provider(self, monkeypatch):
        # Ollama (local) is a valid explicit choice via LLM_PROVIDER even
        # though it has no API key to auto-detect from.
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        assert providers._detect_provider() == "ollama"

    def test_ollama_is_explicit_only_not_autodetected(self, monkeypatch):
        # Ollama has no API key, so it's never reached by key auto-detection —
        # an OLLAMA_BASE_URL alone must NOT select it. Local is opt-in by
        # design (set LLM_PROVIDER / a per-track override).
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        assert providers._detect_provider() is None


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
        # API-key auto-detect applies in the EXTRACTION (gemini-first) order.
        monkeypatch.setenv("GEMINI_API_KEY", "g")
        assert providers._detect_extraction_provider() == "gemini"

    def test_zero_config_extracts_with_gemini_summary_with_anthropic(self, monkeypatch):
        # The 0.13.0 flip: with all three keys and NO env overrides, the
        # extraction track auto-selects Gemini (best accuracy + cheapest +
        # fastest) while the base/summary default stays Anthropic.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ant")
        monkeypatch.setenv("OPENAI_API_KEY", "oai")
        monkeypatch.setenv("GEMINI_API_KEY", "g")
        assert providers._detect_extraction_provider() == "gemini"
        assert providers._detect_provider() == "anthropic"

    def test_extraction_falls_back_when_no_gemini_key(self, monkeypatch):
        # Graceful: Gemini-first priority, but with no Gemini key it falls to
        # the next available (anthropic) rather than failing.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ant")
        monkeypatch.setenv("OPENAI_API_KEY", "oai")
        assert providers._detect_extraction_provider() == "anthropic"

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

    def test_ollama_extraction_override(self, monkeypatch):
        # The extraction track can be pinned to local inference independently
        # of the summary track — local extraction, hosted summaries.
        monkeypatch.setenv("LLM_EXTRACTION_PROVIDER", "ollama")
        assert providers._detect_extraction_provider() == "ollama"


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

    def test_ollama_provider_default_model(self, monkeypatch):
        # provider_info reflects an explicit ollama selection + its default
        # model, so the startup `extraction LLM:` log line names it.
        monkeypatch.setenv("LLM_EXTRACTION_PROVIDER", "ollama")
        info = providers.provider_info()
        assert "provider=ollama" in info
        assert providers._DEFAULT_MODELS["ollama"] in info


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

    def test_routes_to_ollama(self, monkeypatch):
        captured = {}

        def fake(system, user, max_tokens, **kw):
            captured["provider"] = "ollama"
            captured["kw"] = kw
            return "ok"

        monkeypatch.setattr(providers, "_call_ollama", fake)
        assert providers._dispatch_llm_call("ollama", "s", "u", 100) == "ok"
        assert captured["provider"] == "ollama"
        # json_mode propagates the same as the other OpenAI-shaped providers.
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

    def test_temperature_passthrough(self, monkeypatch):
        # The one common sampling knob the domain layer uses to pin
        # determinism — must reach all three provider paths from
        # dispatch. Test all three because a future "I'll move
        # temperature into the provider helper" refactor would silently
        # drop it from whichever provider's call site wasn't updated.
        captured: dict[str, Any] = {}

        def fake(system, user, max_tokens, **kw):
            captured.setdefault("calls", []).append(kw)
            return "ok"

        monkeypatch.setattr(providers, "_call_anthropic", fake)
        monkeypatch.setattr(providers, "_call_openai", fake)
        monkeypatch.setattr(providers, "_call_gemini", fake)

        for prov in ("anthropic", "openai", "gemini"):
            providers._dispatch_llm_call(prov, "s", "u", 50, temperature=0.0)
        for call in captured["calls"]:
            assert call["temperature"] == 0.0

    def test_temperature_omitted_by_default(self, monkeypatch):
        # When the caller doesn't ask for a specific temperature, the
        # dispatch must still forward `temperature=None` so the
        # per-provider call knows to leave the SDK default alone. The
        # alternative — defaulting to 0 here — would mean every caller
        # silently pins determinism whether they asked for it or not,
        # taking away the knob.
        captured: dict[str, Any] = {}

        def fake(system, user, max_tokens, **kw):
            captured.setdefault("calls", []).append(kw)
            return "ok"

        monkeypatch.setattr(providers, "_call_anthropic", fake)
        monkeypatch.setattr(providers, "_call_openai", fake)
        monkeypatch.setattr(providers, "_call_gemini", fake)

        for prov in ("anthropic", "openai", "gemini"):
            providers._dispatch_llm_call(prov, "s", "u", 50)
        for call in captured["calls"]:
            assert call.get("temperature") is None

    def test_anthropic_does_not_receive_json_mode(self, monkeypatch):
        # Anthropic's SDK has no json_mode flag — we rely on the prompt
        # to elicit JSON. The helper must NOT pass json_mode through to
        # the anthropic call function or the SDK call would raise on
        # the unexpected kwarg.
        captured = {}

        def fake(
            system,
            user,
            max_tokens,
            *,
            model=None,
            purpose="llm",
            docket=None,
            temperature=None,
        ):
            captured["model"] = model
            # purpose/docket/temperature are expected (token telemetry + the
            # one common sampling knob plumbed through dispatch); json_mode
            # is NOT — this signature has no json_mode param, so a leak
            # would raise.
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

    def test_temperature_omitted_when_none(self, monkeypatch):
        # Default is temperature=None — the SDK call must NOT carry a
        # temperature kwarg so the provider's own default (currently
        # 1.0 on Anthropic) takes effect. Sending temperature=None
        # explicitly would tighten the interface to "always pin a
        # value" and lose that default-from-provider behavior.
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
        assert "temperature" not in fake_client.messages.create.call_args.kwargs

    def test_temperature_forwarded_when_set(self, monkeypatch):
        # When set, the value goes through unmodified — including 0.0,
        # which Python's truthiness would silently drop if the
        # implementation used `if temperature: ...` instead of the
        # required `if temperature is not None: ...`.
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

        providers._call_anthropic("s", "u", 10, temperature=0.0)
        assert fake_client.messages.create.call_args.kwargs["temperature"] == 0.0


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

    def test_temperature_omitted_when_none(self, monkeypatch):
        # Mirror of the same Anthropic check — see that test for the
        # rationale. Default is provider-default sampling, opt-in pinning.
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
        assert "temperature" not in fake_client.chat.completions.create.call_args.kwargs

    def test_temperature_forwarded_when_set(self, monkeypatch):
        # 0.0 specifically — the `is not None` check has to survive the
        # falsy zero, see the Anthropic mirror for the failure mode.
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

        providers._call_openai("s", "u", 10, temperature=0.0)
        assert (
            fake_client.chat.completions.create.call_args.kwargs["temperature"] == 0.0
        )


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

    def test_temperature_omitted_when_none(self, monkeypatch):
        # Mirror of the same Anthropic / OpenAI checks. Gemini packs
        # config kwargs into a GenerateContentConfig object, so this
        # one inspects the `_Cfg.kw` dict rather than the call kwargs.
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
        fake_client.models.generate_content.return_value.text = "ok"

        fake_google = MagicMock()
        fake_google.genai = fake_genai
        monkeypatch.setitem(sys.modules, "google", fake_google)
        monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
        monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)

        providers._call_gemini("s", "u", 10)
        cfg = fake_client.models.generate_content.call_args.kwargs["config"]
        assert "temperature" not in cfg.kw

    def test_temperature_forwarded_when_set(self, monkeypatch):
        # 0.0 specifically — same falsy-zero check as the Anthropic /
        # OpenAI mirrors.
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
        fake_client.models.generate_content.return_value.text = "ok"

        fake_google = MagicMock()
        fake_google.genai = fake_genai
        monkeypatch.setitem(sys.modules, "google", fake_google)
        monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
        monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)

        providers._call_gemini("s", "u", 10, temperature=0.0)
        cfg = fake_client.models.generate_content.call_args.kwargs["config"]
        assert cfg.kw["temperature"] == 0.0

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


class TestCallOllama:
    """Local inference through Ollama's NATIVE ``/api/chat`` endpoint (chosen
    over the OpenAI-compat ``/v1`` path because only the native endpoint exposes
    per-request thinking control). These tests monkeypatch the
    :func:`providers._ollama_chat_request` seam — capturing the request body and
    returning a canned native response dict — and stub
    :func:`providers.ollama_capabilities` to drive the per-track thinking
    decision, then assert on the native body shape (``format``,
    ``options.num_predict`` / ``num_ctx``, ``think``)."""

    @pytest.fixture(autouse=True)
    def _no_context_lookup(self, monkeypatch):
        # Keep these call-shape tests hermetic: the pre-flight context check
        # would otherwise reach for a real /api/show. Its own behavior (limit
        # resolution + pre/post-flight) is covered by TestOllamaContextWindow
        # and TestOllamaInputTruncation below.
        monkeypatch.setattr(providers, "_ollama_context_limit", lambda model: None)
        providers._OLLAMA_SHOW_CACHE.clear()
        yield
        providers._OLLAMA_SHOW_CACHE.clear()

    @staticmethod
    def _fake_ollama(
        monkeypatch,
        content="hello",
        *,
        caps=frozenset({"completion"}),
        done_reason="stop",
        prompt_eval_count=5,
        eval_count=3,
    ):
        """Patch the native request seam + capability lookup. Returns a dict
        the test reads ``captured["body"]`` from. ``caps`` defaults to a
        NON-thinking model (so no ``think`` field is sent unless a test opts in
        with ``caps=frozenset({"thinking"})``)."""
        captured: dict = {}

        def fake_request(body, *, timeout=600.0):
            captured["body"] = body
            captured["timeout"] = timeout
            return {
                "message": {"content": content},
                "done_reason": done_reason,
                "prompt_eval_count": prompt_eval_count,
                "eval_count": eval_count,
            }

        monkeypatch.setattr(providers, "_ollama_chat_request", fake_request)
        monkeypatch.setattr(providers, "ollama_capabilities", lambda model: caps)
        # A truthy /api/show routes the dispatcher to the native backend (real
        # Ollama). The OpenAI-compat fallback is covered separately.
        monkeypatch.setattr(
            providers, "_ollama_show", lambda model: {"capabilities": list(caps)}
        )
        return captured

    def test_returns_message_content(self, monkeypatch):
        cap = self._fake_ollama(monkeypatch, content='{"actions": []}')
        out = providers._call_ollama("s", "u", 50)
        assert out == '{"actions": []}'
        body = cap["body"]
        # JSON mode on by default; default model is the ollama default; posts
        # the system + user messages, non-streaming.
        assert body["format"] == "json"
        assert body["model"] == providers._DEFAULT_MODELS["ollama"]
        assert body["stream"] is False
        assert [m["role"] for m in body["messages"]] == ["system", "user"]

    def test_num_predict_carries_max_tokens_for_nonthinking(self, monkeypatch):
        # Native uses options.num_predict (not the OpenAI `max_tokens`); for a
        # non-thinking model it's just the requested ceiling.
        cap = self._fake_ollama(monkeypatch)
        providers._call_ollama("s", "u", 64)
        assert cap["body"]["options"]["num_predict"] == 64

    def test_native_base_strips_v1(self, monkeypatch):
        # OLLAMA_BASE_URL points at the OpenAI-compat /v1 path; the native
        # /api/chat lives at the host root, so the /v1 segment is stripped.
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        assert providers._ollama_native_base() == "http://localhost:11434"
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://gpu-box:11434/v1")
        assert providers._ollama_native_base() == "http://gpu-box:11434"
        # Already-rootless base is left alone.
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://gpu-box:11434")
        assert providers._ollama_native_base() == "http://gpu-box:11434"

    def test_respects_model_kwarg_and_llm_model_env(self, monkeypatch):
        cap = self._fake_ollama(monkeypatch)
        providers._call_ollama("s", "u", 10, model="qwen2.5:32b")
        assert cap["body"]["model"] == "qwen2.5:32b"
        # LLM_MODEL is the env fallback when no model kwarg is passed.
        monkeypatch.setenv("LLM_MODEL", "mistral-small")
        providers._call_ollama("s", "u", 10)
        assert cap["body"]["model"] == "mistral-small"

    def test_json_mode_off_omits_format(self, monkeypatch):
        cap = self._fake_ollama(monkeypatch, content="prose")
        providers._call_ollama("s", "u", 50, json_mode=False)
        assert "format" not in cap["body"]

    def test_num_ctx_forwarded_when_set(self, monkeypatch):
        # Local models truncate long prompts silently; OLLAMA_NUM_CTX widens
        # the window, carried as the native options.num_ctx.
        monkeypatch.setenv("OLLAMA_NUM_CTX", "32768")
        cap = self._fake_ollama(monkeypatch)
        providers._call_ollama("s", "u", 10)
        assert cap["body"]["options"]["num_ctx"] == 32768

    def test_num_ctx_omitted_when_unset(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_NUM_CTX", raising=False)
        cap = self._fake_ollama(monkeypatch)
        providers._call_ollama("s", "u", 10)
        assert "num_ctx" not in cap["body"]["options"]

    def test_empty_content_raises(self, monkeypatch):
        self._fake_ollama(monkeypatch, content="")
        with pytest.raises(ValueError, match="No content in Ollama"):
            providers._call_ollama("s", "u", 10)

    def test_done_reason_length_raises_truncated(self, monkeypatch):
        self._fake_ollama(monkeypatch, content='{"actions": [', done_reason="length")
        with pytest.raises(providers.OutputTruncatedError) as exc_info:
            providers._call_ollama("s", "u", 2048)
        assert exc_info.value.provider == "ollama"
        assert exc_info.value.max_tokens == 2048

    def test_records_usage_under_ollama_provider(self, monkeypatch):
        # Telemetry must bucket the call under provider="ollama" (so cost
        # estimation can zero it) — via the native usage path (from_ollama).
        from case_calendar.llmkit import usage

        seen = {}
        monkeypatch.setattr(usage, "record", lambda **kw: seen.update(kw))
        self._fake_ollama(monkeypatch, prompt_eval_count=11, eval_count=7)
        providers._call_ollama("s", "u", 10)
        assert seen["provider"] == "ollama"
        assert seen["tokens"].input == 11
        assert seen["tokens"].output == 7

    def test_temperature_omitted_when_none(self, monkeypatch):
        cap = self._fake_ollama(monkeypatch)
        providers._call_ollama("s", "u", 10)
        assert "temperature" not in cap["body"]["options"]

    def test_temperature_forwarded_when_zero(self, monkeypatch):
        # 0.0 must survive the falsy-zero trap (the `is not None` check).
        cap = self._fake_ollama(monkeypatch)
        providers._call_ollama("s", "u", 10, temperature=0.0)
        assert cap["body"]["options"]["temperature"] == 0.0

    # --- per-track thinking control ---

    def test_thinking_model_disables_thinking_off_summary(self, monkeypatch):
        # High-volume structured tracks: thinking OFF so reasoning can't overrun
        # the output budget (the qwen3 "No content" failure) or take minutes.
        for purpose in ("extract", "verify_hearing", "dedupe_hearings", "llm"):
            cap = self._fake_ollama(monkeypatch, caps=frozenset({"thinking"}))
            providers._call_ollama("s", "u", 8192, purpose=purpose)
            assert cap["body"]["think"] is False, purpose
            assert cap["body"]["options"]["num_predict"] == 8192, purpose

    def test_thinking_model_summary_keeps_thinking_bounded_budget(self, monkeypatch):
        # Summary track: thinking ON, output cap lifted to the generous BOUNDED
        # budget (not unlimited — a runaway can hang the local GPU).
        cap = self._fake_ollama(monkeypatch, caps=frozenset({"thinking"}))
        providers._call_ollama("s", "u", 8192, purpose="summary")
        assert cap["body"]["think"] is True
        assert (
            cap["body"]["options"]["num_predict"]
            == providers._OLLAMA_SUMMARY_THINKING_BUDGET
        )

    def test_summary_budget_respects_larger_max_tokens(self, monkeypatch):
        # max() — a caller asking for more than the bounded budget still wins.
        cap = self._fake_ollama(monkeypatch, caps=frozenset({"thinking"}))
        big = providers._OLLAMA_SUMMARY_THINKING_BUDGET + 5000
        providers._call_ollama("s", "u", big, purpose="summary")
        assert cap["body"]["options"]["num_predict"] == big

    def test_nonthinking_model_omits_think_field(self, monkeypatch):
        # A model without the thinking capability gets a plain request.
        cap = self._fake_ollama(monkeypatch, caps=frozenset({"completion"}))
        providers._call_ollama("s", "u", 10, purpose="summary")
        assert "think" not in cap["body"]
        assert cap["body"]["options"]["num_predict"] == 10

    def test_unknown_caps_treated_as_thinking(self, monkeypatch):
        # An unconfirmable capability lookup (empty set) is treated AS thinking
        # — the safe default — so a structured call still disables thinking.
        cap = self._fake_ollama(monkeypatch, caps=frozenset())
        providers._call_ollama("s", "u", 10, purpose="extract")
        assert cap["body"]["think"] is False

    # --- gpt-oss level-based thinking (think=false is ignored; can't disable) ---

    def test_level_thinking_extract_uses_low_level_and_budget(self, monkeypatch):
        # gpt-oss can't turn reasoning OFF — a boolean is ignored. So the
        # high-volume tracks get the SHORTEST level ("low") plus output room for
        # the trace + the answer, not think=false on a too-small budget.
        for purpose in ("extract", "verify_hearing", "dedupe_hearings", "llm"):
            cap = self._fake_ollama(monkeypatch, caps=frozenset({"thinking"}))
            providers._call_ollama("s", "u", 4096, model="gpt-oss:20b", purpose=purpose)
            assert cap["body"]["think"] == "low", purpose
            assert (
                cap["body"]["options"]["num_predict"]
                == providers._OLLAMA_SUMMARY_THINKING_BUDGET
            ), purpose

    def test_level_thinking_summary_uses_high_level_and_budget(self, monkeypatch):
        cap = self._fake_ollama(monkeypatch, caps=frozenset({"thinking"}))
        providers._call_ollama("s", "u", 4096, model="gpt-oss:20b", purpose="summary")
        assert cap["body"]["think"] == "high"
        assert (
            cap["body"]["options"]["num_predict"]
            == providers._OLLAMA_SUMMARY_THINKING_BUDGET
        )

    def test_level_thinking_summary_respects_larger_max_tokens(self, monkeypatch):
        cap = self._fake_ollama(monkeypatch, caps=frozenset({"thinking"}))
        big = providers._OLLAMA_SUMMARY_THINKING_BUDGET + 5000
        providers._call_ollama("s", "u", big, model="gpt-oss:120b", purpose="summary")
        assert cap["body"]["options"]["num_predict"] == big

    def test_requires_thinking_level_matches_gpt_oss_variants(self):
        assert providers._ollama_requires_thinking_level("gpt-oss:20b")
        assert providers._ollama_requires_thinking_level("gpt-oss:120b")
        assert providers._ollama_requires_thinking_level("GPT-OSS:20b")
        # Boolean-thinking models (qwen3, gemma, glm, granite) are NOT level-based.
        assert not providers._ollama_requires_thinking_level("qwen3.5:9b")
        assert not providers._ollama_requires_thinking_level("gemma4:e4b")

    def test_think_value_is_string_level_for_gpt_oss_but_bool_for_others(
        self, monkeypatch
    ):
        # Same track ("extract"), divergent contract: gpt-oss must receive a
        # STRING level (Ollama ignores a boolean for it), every other thinking
        # model receives a BOOLEAN. Pins the type divergence that's easy to
        # regress if the two branches are ever collapsed.
        cap_oss = self._fake_ollama(monkeypatch, caps=frozenset({"thinking"}))
        providers._call_ollama("s", "u", 4096, model="gpt-oss:20b", purpose="extract")
        assert cap_oss["body"]["think"] == "low"
        assert isinstance(cap_oss["body"]["think"], str)

        cap_qwen = self._fake_ollama(monkeypatch, caps=frozenset({"thinking"}))
        providers._call_ollama("s", "u", 4096, model="qwen3.5:9b", purpose="extract")
        assert cap_qwen["body"]["think"] is False
        assert isinstance(cap_qwen["body"]["think"], bool)


class TestOllamaCapabilities:
    """providers.ollama_capabilities reads the model's /api/show capabilities,
    cached per (base_url, model), with an empty set on any lookup failure."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        providers._OLLAMA_SHOW_CACHE.clear()
        yield
        providers._OLLAMA_SHOW_CACHE.clear()

    @staticmethod
    def _fake_urlopen(monkeypatch, payload, calls=None):
        import json
        import urllib.request

        class _Resp:
            def read(self):
                return json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake(req, timeout=None):
            if calls is not None:
                calls.append(req.full_url)
            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", fake)

    def test_parses_capabilities(self, monkeypatch):
        self._fake_urlopen(monkeypatch, {"capabilities": ["completion", "thinking"]})
        assert providers.ollama_capabilities("gemma4:e4b") == frozenset(
            {"completion", "thinking"}
        )

    def test_strips_v1_and_calls_api_show(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://gpu-box:11434/v1")
        calls: list[str] = []
        self._fake_urlopen(monkeypatch, {"capabilities": ["completion"]}, calls)
        providers.ollama_capabilities("m")
        assert calls == ["http://gpu-box:11434/api/show"]

    def test_base_url_without_v1_used_as_is(self, monkeypatch):
        # A base_url with no trailing /v1 (bare host, or a custom server) is used
        # as-is — only a /v1 suffix is stripped before appending /api/show.
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
        calls: list[str] = []
        self._fake_urlopen(monkeypatch, {"capabilities": ["completion"]}, calls)
        providers.ollama_capabilities("m")
        assert calls == ["http://localhost:11434/api/show"]

    def test_caches_per_model(self, monkeypatch):
        calls: list[str] = []
        self._fake_urlopen(monkeypatch, {"capabilities": ["thinking"]}, calls)
        providers.ollama_capabilities("m")
        providers.ollama_capabilities("m")  # served from cache, no second call
        assert len(calls) == 1

    def test_missing_capabilities_field_is_empty(self, monkeypatch):
        self._fake_urlopen(monkeypatch, {"model_info": {}})
        assert providers.ollama_capabilities("m") == frozenset()

    def test_lookup_failure_returns_empty(self, monkeypatch):
        import urllib.request

        def boom(req, timeout=None):
            raise OSError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        assert providers.ollama_capabilities("m") == frozenset()


class TestEnsureThinkingBudget:
    """providers.ensure_thinking_budget raises a too-small output budget only for
    a thinking model (whose reasoning is drawn from the answer budget)."""

    def test_gemini_always_bumps(self):
        assert providers.ensure_thinking_budget("gemini", "any", 800) == 8192

    def test_gemini_keeps_larger_request(self):
        assert providers.ensure_thinking_budget("gemini", "any", 20000) == 20000

    def test_anthropic_unchanged(self):
        assert providers.ensure_thinking_budget("anthropic", "claude", 800) == 800

    def test_openai_unchanged(self):
        assert providers.ensure_thinking_budget("openai", "gpt-5.4", 800) == 800

    def test_ollama_thinking_model_bumps(self, monkeypatch):
        monkeypatch.setattr(
            providers,
            "ollama_capabilities",
            lambda m: frozenset({"completion", "thinking"}),
        )
        assert providers.ensure_thinking_budget("ollama", "gemma4:e4b", 800) == 8192

    def test_ollama_plain_model_unchanged(self, monkeypatch):
        monkeypatch.setattr(
            providers,
            "ollama_capabilities",
            lambda m: frozenset({"completion", "tools"}),
        )
        assert providers.ensure_thinking_budget("ollama", "phi4", 800) == 800

    def test_ollama_unknown_caps_bumps(self, monkeypatch):
        # /api/show couldn't confirm -> treat as thinking (safe default: an
        # under-budgeted thinking model fails hard, an over-budgeted plain one is
        # only a soft quality issue).
        monkeypatch.setattr(providers, "ollama_capabilities", lambda m: frozenset())
        assert providers.ensure_thinking_budget("ollama", "mystery", 800) == 8192

    def test_ollama_without_model_bumps_without_lookup(self, monkeypatch):
        # No resolved model -> can't check -> bump, and skip the lookup entirely.
        seen: list = []
        monkeypatch.setattr(
            providers,
            "ollama_capabilities",
            lambda m: seen.append(m) or frozenset({"completion"}),
        )
        assert providers.ensure_thinking_budget("ollama", None, 800) == 8192
        assert seen == []

    def test_custom_floor(self):
        assert providers.ensure_thinking_budget("gemini", "x", 100, floor=4096) == 4096


class TestOllamaModelDefaults:
    """The local-inference default is `gemma4:e4b` for BOTH tracks — chosen for
    hardware fit (it runs on common consumer GPUs), with `gemma4:31b` as the
    opt-in quality upgrade. Pins the 0.16 flip; see AGENTS.md's Ollama
    default-model design decision and docs/local-llms.md. Don't flip it back up."""

    def test_extraction_default_is_e4b(self):
        assert providers._DEFAULT_MODELS["ollama"] == "gemma4:e4b"

    def test_summary_default_is_e4b(self):
        from case_calendar import llm

        assert llm._DEFAULT_SUMMARY_MODELS["ollama"] == "gemma4:e4b"

    def test_both_tracks_share_one_local_model(self):
        # Zero-config local install pulls and runs ONE model for both tracks.
        from case_calendar import llm

        assert (
            providers._DEFAULT_MODELS["ollama"] == llm._DEFAULT_SUMMARY_MODELS["ollama"]
        )


class TestContextWindowExceededError:
    """The error carries best-effort token figures and a readable message; any
    figure may be None (a hosted provider's error gives only a message)."""

    def test_message_with_all_fields(self):
        e = providers.ContextWindowExceededError(
            "ollama", sent=300000, processed=255000, limit=256000
        )
        s = str(e)
        assert "ollama" in s and "300000" in s and "255000" in s and "256000" in s
        assert e.provider == "ollama"
        assert (e.sent, e.processed, e.limit) == (300000, 255000, 256000)

    def test_message_with_detail_only(self):
        e = providers.ContextWindowExceededError("openai", detail="prompt is too long")
        assert "openai" in str(e) and "prompt is too long" in str(e)
        assert e.sent is None and e.processed is None and e.limit is None

    def test_message_bare(self):
        e = providers.ContextWindowExceededError("gemini")
        assert str(e) == "gemini prompt exceeds the context window"


class TestContextAndMemoryErrorMatchers:
    """`_is_context_length_error` / `_is_memory_error` classify SDK exceptions by
    message text (no SDK error-class imports), so they stay provider-agnostic."""

    @pytest.mark.parametrize(
        "msg",
        [
            "This model's maximum context length is 8192 tokens, however you...",
            "Error code: 400 - context_length_exceeded",
            "prompt is too long: 250000 tokens > 200000 maximum",
            "The input token count (300000) exceeds the maximum number of tokens",
            "Please reduce the length of the messages",
        ],
    )
    def test_context_length_errors_match(self, msg):
        assert providers._is_context_length_error(Exception(msg)) is True

    @pytest.mark.parametrize(
        "msg",
        ["rate limit exceeded", "connection refused", "500 internal server error"],
    )
    def test_non_context_errors_dont_match(self, msg):
        assert providers._is_context_length_error(Exception(msg)) is False

    @pytest.mark.parametrize(
        "msg",
        [
            "CUDA error: out of memory",
            "cudaMalloc failed: out of memory",
            "failed to allocate 12.3 GiB",
            "model requires more system memory (40.0 GiB) than is available",
            "not enough memory",
        ],
    )
    def test_memory_errors_match(self, msg):
        assert providers._is_memory_error(Exception(msg)) is True

    def test_context_and_memory_are_distinct(self):
        # A memory error must NOT read as a context-length error (opposite
        # remedy), and vice versa.
        mem = Exception("CUDA error: out of memory")
        ctx = Exception("prompt is too long: 250000 tokens > 200000 maximum")
        assert providers._is_memory_error(
            mem
        ) and not providers._is_context_length_error(mem)
        assert providers._is_context_length_error(
            ctx
        ) and not providers._is_memory_error(ctx)


class TestDispatchContextError:
    """`_dispatch_llm_call` normalizes a hosted provider's context-length 400 to
    ContextWindowExceededError, and passes through everything else unchanged."""

    def test_hosted_context_error_converted(self, monkeypatch):
        def boom(system, user, max_tokens, **kw):
            raise RuntimeError("This model's maximum context length is 200000 tokens")

        monkeypatch.setattr(providers, "_call_anthropic", boom)
        with pytest.raises(providers.ContextWindowExceededError) as exc:
            providers._dispatch_llm_call("anthropic", "s", "u", 100)
        assert exc.value.provider == "anthropic"
        assert "maximum context length" in exc.value.detail

    def test_context_window_exceeded_passes_through(self, monkeypatch):
        # Ollama raises ContextWindowExceededError from its own pre/post-flight;
        # the wrapper must not re-wrap or swallow it.
        orig = providers.ContextWindowExceededError("ollama", limit=4096)

        def boom(system, user, max_tokens, **kw):
            raise orig

        monkeypatch.setattr(providers, "_call_ollama", boom)
        with pytest.raises(providers.ContextWindowExceededError) as exc:
            providers._dispatch_llm_call("ollama", "s", "u", 100)
        assert exc.value is orig

    def test_output_truncated_passes_through(self, monkeypatch):
        def boom(system, user, max_tokens, **kw):
            raise providers.OutputTruncatedError("openai", "partial", 100)

        monkeypatch.setattr(providers, "_call_openai", boom)
        with pytest.raises(providers.OutputTruncatedError):
            providers._dispatch_llm_call("openai", "s", "u", 100)

    def test_unrelated_error_propagates_unchanged(self, monkeypatch):
        def boom(system, user, max_tokens, **kw):
            raise ValueError("rate limit exceeded")

        monkeypatch.setattr(providers, "_call_gemini", boom)
        with pytest.raises(ValueError, match="rate limit"):
            providers._dispatch_llm_call("gemini", "s", "u", 100)


class TestOllamaContextWindowResolution:
    """`ollama_context_window` reads the model's architecture context_length from
    /api/show's model_info; `_ollama_context_limit` layers OLLAMA_NUM_CTX on top.
    Both monkeypatch `_ollama_show` so no network is touched."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        providers._OLLAMA_SHOW_CACHE.clear()
        yield
        providers._OLLAMA_SHOW_CACHE.clear()

    def test_reads_context_length_from_model_info(self, monkeypatch):
        monkeypatch.setattr(
            providers,
            "_ollama_show",
            lambda m: {"model_info": {"gemma4.context_length": 262144}},
        )
        assert providers.ollama_context_window("gemma4:31b") == 262144

    def test_none_when_show_unavailable(self, monkeypatch):
        monkeypatch.setattr(providers, "_ollama_show", lambda m: None)
        assert providers.ollama_context_window("m") is None

    def test_none_when_no_context_length_key(self, monkeypatch):
        monkeypatch.setattr(
            providers, "_ollama_show", lambda m: {"model_info": {"general.name": "x"}}
        )
        assert providers.ollama_context_window("m") is None

    def test_none_when_model_info_not_a_dict(self, monkeypatch):
        # Defensive against an unexpected payload shape (model_info as a list).
        monkeypatch.setattr(
            providers, "_ollama_show", lambda m: {"model_info": ["unexpected"]}
        )
        assert providers.ollama_context_window("m") is None

    def test_ignores_bool_context_length(self, monkeypatch):
        # A bool is an int subclass — must not be returned as a window size.
        monkeypatch.setattr(
            providers,
            "_ollama_show",
            lambda m: {"model_info": {"x.context_length": True}},
        )
        assert providers.ollama_context_window("m") is None

    def test_limit_prefers_env_num_ctx(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_NUM_CTX", "32768")
        # Even if the model max is larger, the explicit per-request window wins.
        monkeypatch.setattr(providers, "ollama_context_window", lambda m: 262144)
        assert providers._ollama_context_limit("m") == 32768

    def test_limit_falls_back_to_model_max(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_NUM_CTX", raising=False)
        monkeypatch.setattr(providers, "ollama_context_window", lambda m: 262144)
        assert providers._ollama_context_limit("m") == 262144

    def test_limit_malformed_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_NUM_CTX", "lots")
        monkeypatch.setattr(providers, "ollama_context_window", lambda m: 8192)
        assert providers._ollama_context_limit("m") == 8192

    def test_limit_none_when_nothing_known(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_NUM_CTX", raising=False)
        monkeypatch.setattr(providers, "ollama_context_window", lambda m: None)
        assert providers._ollama_context_limit("m") is None

    def test_capabilities_and_context_share_one_show_call(self, monkeypatch):
        # The refactor's payoff: both fields come from ONE cached /api/show.
        # Drive the real shared-cache path: patch the urllib layer, not _ollama_show.
        import json
        import urllib.request

        class _Resp:
            def read(self):
                return json.dumps(
                    {
                        "capabilities": ["completion", "thinking"],
                        "model_info": {"gemma4.context_length": 262144},
                    }
                ).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        urls: list[str] = []

        def fake_urlopen(req, timeout=None):
            urls.append(req.full_url)
            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert providers.ollama_capabilities("gemma4:31b") == frozenset(
            {"completion", "thinking"}
        )
        assert providers.ollama_context_window("gemma4:31b") == 262144
        assert len(urls) == 1  # second read served from the shared cache


class TestDetectOllamaInputTruncation:
    """The post-flight backstop: the server's real prompt-token count reveals a
    silently truncated prompt. Known-limit uses budget saturation (tokenizer-
    independent); unknown-limit uses a gross sent-vs-processed shortfall."""

    def test_zero_processed_is_noop(self):
        # A test double / missing usage coerces to 0 — no signal, no raise.
        providers._detect_ollama_input_truncation(
            processed=0, prompt_chars=10_000_000, limit=4096, max_tokens=800
        )

    def test_known_limit_saturated_raises(self):
        with pytest.raises(providers.ContextWindowExceededError) as exc:
            providers._detect_ollama_input_truncation(
                processed=3300, prompt_chars=400_000, limit=4096, max_tokens=800
            )
        assert exc.value.limit == 4096 and exc.value.processed == 3300

    def test_known_limit_with_headroom_no_raise(self):
        # Prompt fit comfortably (processed well under limit - max_tokens).
        providers._detect_ollama_input_truncation(
            processed=1000, prompt_chars=4000, limit=256000, max_tokens=800
        )

    def test_unknown_limit_gross_shortfall_raises(self):
        # We estimate we sent ~26k tokens (100k chars / 3.83) but the server only
        # processed 4k — a clear truncation even with no known limit.
        with pytest.raises(providers.ContextWindowExceededError):
            providers._detect_ollama_input_truncation(
                processed=4000, prompt_chars=100_000, limit=None, max_tokens=800
            )

    def test_unknown_limit_mild_difference_no_raise(self):
        # est_sent ~2610 vs processed 2500 — within tokenizer variance, no raise.
        providers._detect_ollama_input_truncation(
            processed=2500, prompt_chars=10_000, limit=None, max_tokens=800
        )


class TestCallOllamaContextChecks:
    """Pre-flight + post-flight + memory-error handling inside `_call_ollama`,
    with the native request seam, capability lookup, and limit hook mocked."""

    @staticmethod
    def _fake_ollama(
        monkeypatch,
        *,
        content="hello",
        processed=0,
        raise_exc=None,
        caps=frozenset({"completion"}),
    ):
        captured: dict = {}

        def fake_request(body, *, timeout=600.0):
            captured["body"] = body
            if raise_exc is not None:
                raise raise_exc
            return {
                "message": {"content": content},
                "done_reason": "stop",
                "prompt_eval_count": processed,
                "eval_count": 3,
            }

        monkeypatch.setattr(providers, "_ollama_chat_request", fake_request)
        monkeypatch.setattr(providers, "ollama_capabilities", lambda model: caps)
        # A truthy /api/show routes the dispatcher to the native backend (real
        # Ollama). The OpenAI-compat fallback is covered separately.
        monkeypatch.setattr(
            providers, "_ollama_show", lambda model: {"capabilities": list(caps)}
        )
        return captured

    def test_preflight_refuses_before_calling(self, monkeypatch):
        monkeypatch.setattr(providers, "_ollama_context_limit", lambda m: 100)
        cap = self._fake_ollama(monkeypatch)
        # ~115-token estimate (400 chars / 3.5) + max_tokens 10 > limit 100.
        with pytest.raises(providers.ContextWindowExceededError) as exc:
            providers._call_ollama("x" * 400, "", 10)
        assert exc.value.limit == 100
        assert "body" not in cap  # the native request was never made

    def test_within_limit_proceeds(self, monkeypatch):
        monkeypatch.setattr(providers, "_ollama_context_limit", lambda m: 100000)
        cap = self._fake_ollama(monkeypatch, content="ok", processed=50)
        assert providers._call_ollama("s", "u", 10) == "ok"
        assert "body" in cap

    def test_postflight_saturation_raises(self, monkeypatch):
        # Tiny prompt passes pre-flight, but the server reports it evaluated up
        # to the prompt budget (limit - max_tokens) — a silent truncation.
        monkeypatch.setattr(providers, "_ollama_context_limit", lambda m: 1000)
        self._fake_ollama(monkeypatch, processed=995)
        with pytest.raises(providers.ContextWindowExceededError) as exc:
            providers._call_ollama("s", "u", 10)
        assert exc.value.processed == 995 and exc.value.limit == 1000

    def test_memory_error_logs_hint_and_reraises(self, monkeypatch, caplog):
        import logging

        monkeypatch.setattr(providers, "_ollama_context_limit", lambda m: 262144)
        self._fake_ollama(
            monkeypatch, raise_exc=RuntimeError("CUDA error: out of memory")
        )
        with caplog.at_level(logging.WARNING):
            with pytest.raises(RuntimeError, match="out of memory"):
                providers._call_ollama("s", "u", 10)
        # Operator-actionable hint, NOT a context-exceeded refusal.
        assert "LOWER the context window" in caplog.text

    def test_non_memory_error_propagates_without_hint(self, monkeypatch, caplog):
        # A non-memory failure propagates unchanged and does NOT emit the
        # lower-the-window hint (that guidance is memory-error only).
        import logging

        monkeypatch.setattr(providers, "_ollama_context_limit", lambda m: 262144)
        self._fake_ollama(
            monkeypatch, raise_exc=RuntimeError("503 service unavailable")
        )
        with caplog.at_level(logging.WARNING):
            with pytest.raises(RuntimeError, match="service unavailable"):
                providers._call_ollama("s", "u", 10)
        assert "LOWER the context window" not in caplog.text

    def test_memory_error_not_converted_to_context_exceeded(self, monkeypatch):
        # End-to-end through dispatch: an OOM must stay a plain error (opposite
        # remedy), never ContextWindowExceededError.
        monkeypatch.setattr(providers, "_ollama_context_limit", lambda m: 262144)
        self._fake_ollama(
            monkeypatch, raise_exc=RuntimeError("failed to allocate 12.3 GiB")
        )
        with pytest.raises(RuntimeError, match="allocate"):
            providers._dispatch_llm_call("ollama", "s", "u", 10)

    def test_http_error_body_detected_as_memory_error(self, monkeypatch, caplog):
        # A native HTTPError carries the OOM detail in its BODY, not str(exc);
        # _http_error_detail must read .read() so the hint still fires.
        import logging

        class _FakeHTTPError(Exception):
            def read(self):
                return b"model requires more system memory than is available"

        monkeypatch.setattr(providers, "_ollama_context_limit", lambda m: 262144)
        self._fake_ollama(monkeypatch, raise_exc=_FakeHTTPError("HTTP Error 500"))
        with caplog.at_level(logging.WARNING):
            with pytest.raises(_FakeHTTPError):
                providers._call_ollama("s", "u", 10)
        assert "LOWER the context window" in caplog.text


class TestCallOllamaOpenAICompatFallback:
    """When /api/show fails, the server is NOT Ollama (a generic
    OpenAI-compatible local server — LM Studio / vLLM / llama.cpp), so
    _call_ollama falls back to /v1/chat/completions with NO thinking control."""

    @pytest.fixture(autouse=True)
    def _route_to_compat(self, monkeypatch):
        # /api/show returns None -> not Ollama -> OpenAI-compat backend. Limit
        # None keeps the pre-flight hermetic.
        monkeypatch.setattr(providers, "_ollama_show", lambda model: None)
        monkeypatch.setattr(providers, "_ollama_context_limit", lambda model: None)
        yield

    @staticmethod
    def _fake_openai(monkeypatch, content="ok", finish_reason="stop"):
        from unittest.mock import MagicMock
        import sys

        fake_mod = MagicMock(name="openai")
        fake_client = MagicMock()
        fake_mod.OpenAI.return_value = fake_client
        msg = MagicMock()
        msg.content = content
        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = finish_reason
        fake_client.chat.completions.create.return_value.choices = [choice]
        monkeypatch.setitem(sys.modules, "openai", fake_mod)
        return fake_client

    def test_falls_back_to_openai_endpoint_no_thinking(self, monkeypatch):
        client = self._fake_openai(monkeypatch, content='{"actions": []}')
        # Even on the summary track (where Ollama would think), the compat path
        # sends a plain OpenAI request — no `think`, classic `max_tokens`.
        out = providers._call_ollama("s", "u", 64, purpose="summary")
        assert out == '{"actions": []}'
        kw = client.chat.completions.create.call_args.kwargs
        assert kw["response_format"] == {"type": "json_object"}
        assert kw["max_tokens"] == 64
        assert "think" not in kw
        assert "think" not in kw.get("extra_body", {})

    def test_compat_records_usage_as_ollama(self, monkeypatch):
        from case_calendar.llmkit import usage

        seen: dict = {}
        monkeypatch.setattr(usage, "record", lambda **kw: seen.update(kw))
        self._fake_openai(monkeypatch)
        providers._call_ollama("s", "u", 10)
        assert seen["provider"] == "ollama"

    def test_compat_num_ctx_via_extra_body(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_NUM_CTX", "16384")
        client = self._fake_openai(monkeypatch)
        providers._call_ollama("s", "u", 10)
        kw = client.chat.completions.create.call_args.kwargs
        assert kw["extra_body"] == {"options": {"num_ctx": 16384}}

    def test_compat_length_finish_raises_truncated(self, monkeypatch):
        self._fake_openai(monkeypatch, content='{"a":', finish_reason="length")
        with pytest.raises(providers.OutputTruncatedError):
            providers._call_ollama("s", "u", 128)

    def test_compat_empty_content_raises(self, monkeypatch):
        self._fake_openai(monkeypatch, content="")
        with pytest.raises(ValueError, match="No content in Ollama"):
            providers._call_ollama("s", "u", 10)

    def test_compat_memory_error_logs_hint(self, monkeypatch, caplog):
        import logging

        client = self._fake_openai(monkeypatch)
        client.chat.completions.create.side_effect = RuntimeError(
            "CUDA error: out of memory"
        )
        with caplog.at_level(logging.WARNING):
            with pytest.raises(RuntimeError, match="out of memory"):
                providers._call_ollama("s", "u", 10)
        assert "LOWER the context window" in caplog.text

    def test_force_compat_env_overrides_native_on_real_ollama(self, monkeypatch):
        # OLLAMA_USE_OPENAI_COMPAT routes to /v1 even when /api/show SUCCEEDS
        # (real Ollama) — the parity/diagnostic override. If it didn't win, the
        # native backend would be chosen and there'd be no OpenAI client call.
        monkeypatch.setattr(providers, "_ollama_show", lambda model: {"x": 1})
        monkeypatch.setenv("OLLAMA_USE_OPENAI_COMPAT", "1")
        client = self._fake_openai(monkeypatch, content='{"actions": []}')
        out = providers._call_ollama("s", "u", 10, purpose="extract")
        assert out == '{"actions": []}'
        # the /v1 (OpenAI SDK) backend ran, not the native /api/chat one
        assert client.chat.completions.create.called

    def test_force_compat_env_empty_keeps_native(self, monkeypatch):
        # An empty value is NOT an override — native still wins on real Ollama.
        monkeypatch.setattr(providers, "_ollama_show", lambda model: {"x": 1})
        monkeypatch.setenv("OLLAMA_USE_OPENAI_COMPAT", "")
        called = {}
        monkeypatch.setattr(
            providers,
            "_call_ollama_native",
            lambda *a, **k: called.setdefault("native", True) or "ok",
        )
        providers._call_ollama("s", "u", 10, purpose="extract")
        assert called.get("native")


# --- verify_deadline (parallel to verify_hearing) ---
