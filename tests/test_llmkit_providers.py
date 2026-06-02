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
    """Local inference through Ollama's OpenAI-compatible endpoint. We reuse
    the ``openai`` SDK pointed at a local base URL, so these tests inject a
    fake ``openai`` module the same way ``TestCallOpenAI`` does, and assert on
    the Ollama-specific call shape (base_url, dummy key, ``max_tokens`` rather
    than ``max_completion_tokens``, optional ``num_ctx``)."""

    @staticmethod
    def _fake_openai(monkeypatch, content="hello"):
        from unittest.mock import MagicMock
        import sys

        fake_mod = MagicMock(name="openai")
        fake_client = MagicMock()
        fake_mod.OpenAI.return_value = fake_client
        msg = MagicMock()
        msg.content = content
        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = "stop"
        fake_client.chat.completions.create.return_value.choices = [choice]
        monkeypatch.setitem(sys.modules, "openai", fake_mod)
        return fake_mod, fake_client

    def test_returns_message_content(self, monkeypatch):
        _mod, client = self._fake_openai(monkeypatch, content='{"actions": []}')
        out = providers._call_ollama("s", "u", 50)
        assert out == '{"actions": []}'
        kw = client.chat.completions.create.call_args.kwargs
        # JSON mode on by default; default model is the ollama default.
        assert kw["response_format"] == {"type": "json_object"}
        assert kw["model"] == providers._DEFAULT_MODELS["ollama"]

    def test_uses_classic_max_tokens_not_completion_tokens(self, monkeypatch):
        # Ollama's OpenAI-compat endpoint expects the classic `max_tokens`,
        # unlike the gpt-5 family (which requires `max_completion_tokens`).
        _mod, client = self._fake_openai(monkeypatch)
        providers._call_ollama("s", "u", 64)
        kw = client.chat.completions.create.call_args.kwargs
        assert kw["max_tokens"] == 64
        assert "max_completion_tokens" not in kw

    def test_default_base_url_and_dummy_key(self, monkeypatch):
        # Nothing leaves the machine: a localhost base URL and a throwaway key
        # (Ollama ignores the key but the SDK requires a non-empty one).
        fake_mod, _client = self._fake_openai(monkeypatch)
        providers._call_ollama("s", "u", 10)
        ctor = fake_mod.OpenAI.call_args.kwargs
        assert ctor["base_url"] == "http://localhost:11434/v1"
        assert ctor["api_key"]  # non-empty

    def test_base_url_override(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://gpu-box:11434/v1")
        fake_mod, _client = self._fake_openai(monkeypatch)
        providers._call_ollama("s", "u", 10)
        assert fake_mod.OpenAI.call_args.kwargs["base_url"] == "http://gpu-box:11434/v1"

    def test_respects_model_kwarg_and_llm_model_env(self, monkeypatch):
        _mod, client = self._fake_openai(monkeypatch)
        providers._call_ollama("s", "u", 10, model="qwen2.5:32b")
        assert client.chat.completions.create.call_args.kwargs["model"] == "qwen2.5:32b"
        # LLM_MODEL is the env fallback when no model kwarg is passed.
        monkeypatch.setenv("LLM_MODEL", "mistral-small")
        providers._call_ollama("s", "u", 10)
        assert (
            client.chat.completions.create.call_args.kwargs["model"] == "mistral-small"
        )

    def test_json_mode_off_omits_response_format(self, monkeypatch):
        _mod, client = self._fake_openai(monkeypatch, content="prose")
        providers._call_ollama("s", "u", 50, json_mode=False)
        assert "response_format" not in client.chat.completions.create.call_args.kwargs

    def test_num_ctx_forwarded_via_extra_body_when_set(self, monkeypatch):
        # Local models truncate long prompts silently; OLLAMA_NUM_CTX widens
        # the window. It rides through the OpenAI SDK's extra_body as the
        # native `options.num_ctx`.
        monkeypatch.setenv("OLLAMA_NUM_CTX", "32768")
        _mod, client = self._fake_openai(monkeypatch)
        providers._call_ollama("s", "u", 10)
        kw = client.chat.completions.create.call_args.kwargs
        assert kw["extra_body"] == {"options": {"num_ctx": 32768}}

    def test_num_ctx_omitted_when_unset(self, monkeypatch):
        # Default path sends a vanilla request any Ollama version accepts.
        _mod, client = self._fake_openai(monkeypatch)
        providers._call_ollama("s", "u", 10)
        assert "extra_body" not in client.chat.completions.create.call_args.kwargs

    def test_empty_content_raises(self, monkeypatch):
        _mod, _client = self._fake_openai(monkeypatch, content="")
        with pytest.raises(ValueError, match="No content in Ollama"):
            providers._call_ollama("s", "u", 10)

    def test_length_finish_reason_raises_truncated(self, monkeypatch):
        _mod, client = self._fake_openai(monkeypatch, content='{"actions": [')
        client.chat.completions.create.return_value.choices[0].finish_reason = "length"
        with pytest.raises(providers.OutputTruncatedError) as exc_info:
            providers._call_ollama("s", "u", 2048)
        assert exc_info.value.provider == "ollama"
        assert exc_info.value.max_tokens == 2048

    def test_records_usage_under_ollama_provider(self, monkeypatch):
        # Telemetry must bucket the call under provider="ollama" (so cost
        # estimation can zero it) — recorded via the OpenAI-shaped usage path.
        from case_calendar.llmkit import usage

        seen = {}
        monkeypatch.setattr(
            usage,
            "record",
            lambda **kw: seen.update(kw),
        )
        _mod, _client = self._fake_openai(monkeypatch)
        providers._call_ollama("s", "u", 10)
        assert seen["provider"] == "ollama"

    def test_temperature_omitted_when_none(self, monkeypatch):
        _mod, client = self._fake_openai(monkeypatch)
        providers._call_ollama("s", "u", 10)
        assert "temperature" not in client.chat.completions.create.call_args.kwargs

    def test_temperature_forwarded_when_zero(self, monkeypatch):
        # 0.0 must survive the falsy-zero trap (the `is not None` check).
        _mod, client = self._fake_openai(monkeypatch)
        providers._call_ollama("s", "u", 10, temperature=0.0)
        assert client.chat.completions.create.call_args.kwargs["temperature"] == 0.0


# --- verify_deadline (parallel to verify_hearing) ---
