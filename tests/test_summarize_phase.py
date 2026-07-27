"""Regression test for ``model-comparison/summarize_phase.py`` env-load order.

The script once set a hardcoded, machine-specific ``OLLAMA_BASE_URL`` via
``os.environ.setdefault`` *before* loading ``.env`` — and ``load_dotenv`` never
overrides an env var that is already set, so the operator's configured address
silently lost to the stale hardcoded one. The script can't be imported for a
unit test (it parses arguments and runs the summary phase at module level), so
this pins the property at the source level: ``.env`` must be loaded before any
``os.environ.setdefault`` call, and no machine-specific Ollama address may be
hardcoded as a fallback.
"""

from __future__ import annotations

from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "model-comparison" / "summarize_phase.py"
)
_SOURCE = _SCRIPT.read_text()


def test_dotenv_loads_before_any_env_setdefault():
    load_pos = _SOURCE.index("load_dotenv(")
    setdefault_pos = _SOURCE.index("os.environ.setdefault(")
    assert load_pos < setdefault_pos, (
        "summarize_phase.py must load .env before any os.environ.setdefault — "
        "load_dotenv never overrides an already-set var, so a setdefault that "
        "runs first silently beats the operator's .env configuration"
    )


def test_no_hardcoded_ollama_base_url_fallback():
    assert 'setdefault("OLLAMA_BASE_URL"' not in _SOURCE, (
        "summarize_phase.py must not hardcode an OLLAMA_BASE_URL fallback — "
        "the providers layer already defaults to localhost, and a script-level "
        "machine-specific default pointed summary runs at a dead address"
    )
