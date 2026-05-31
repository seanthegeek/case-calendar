"""Guards that ``docs/llm-prompts.md`` stays verbatim-faithful to the source.

The page reproduces each runtime system prompt verbatim. ``scripts/
sync_llm_prompts_doc.py`` regenerates the machine-derived parts (the four
````text blocks, the ``llm.py#L<n>`` source anchors, and the version stamp)
from ``case_calendar/llm.py`` + ``pyproject.toml``. This test fails if the page
has drifted from the source — the same check CI runs, so a prompt edit that
forgets to re-run the script can't merge.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import case_calendar.llm as llm

_SPEC = importlib.util.spec_from_file_location(
    "sync_llm_prompts_doc",
    Path(__file__).resolve().parent.parent / "scripts" / "sync_llm_prompts_doc.py",
)
assert _SPEC and _SPEC.loader
sync_llm_prompts_doc = importlib.util.module_from_spec(_SPEC)
sys.modules["sync_llm_prompts_doc"] = sync_llm_prompts_doc
_SPEC.loader.exec_module(sync_llm_prompts_doc)


def test_llm_prompts_doc_in_sync():
    """The committed page already equals what the generator would write."""
    assert sync_llm_prompts_doc.main(["--check"]) == 0, (
        "docs/llm-prompts.md is out of sync with case_calendar/llm.py — "
        "run: python scripts/sync_llm_prompts_doc.py"
    )


def test_every_prompt_constant_appears_verbatim():
    """Each system prompt is present in the page exactly as defined in llm.py."""
    doc = sync_llm_prompts_doc.DOC.read_text()
    for name in sync_llm_prompts_doc.PROMPT_NAMES:
        constant = getattr(llm, name)
        assert constant.strip() in doc, f"{name} is not reproduced verbatim in the page"


def test_source_anchors_point_at_current_definition_lines():
    """The llm.py#L<n> anchors match where each constant is actually defined."""
    doc = sync_llm_prompts_doc.DOC.read_text()
    for name in sync_llm_prompts_doc.PROMPT_NAMES:
        line = sync_llm_prompts_doc.constant_line(name)
        assert f"case_calendar/llm.py#L{line}" in doc, (
            f"{name}'s source anchor does not point at line {line}"
        )


def test_render_is_idempotent():
    """Running the generator over an in-sync page changes nothing."""
    current = sync_llm_prompts_doc.DOC.read_text()
    assert sync_llm_prompts_doc.render(current) == current
