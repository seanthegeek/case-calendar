#!/usr/bin/env python3
"""Keep ``docs/llm-prompts.md`` in lockstep with the prompt constants.

The page reproduces each runtime system prompt **verbatim** so readers can see
exactly what the model is told. Hand-syncing it on every release is error-prone
(a prompt edit in ``case_calendar/llm.py`` can silently drift from the page, and
the version stamp / GitHub source-line anchors go stale), so this script is the
single mechanism that keeps the two in sync.

It rewrites only the machine-derived parts of the page and leaves every line of
hand-written prose untouched:

- the four ````text fenced blocks, replaced with the live values of
  ``SYSTEM_PROMPT`` / ``VERIFY_SYSTEM_PROMPT`` / ``DEDUPE_HEARING_SYSTEM_PROMPT``
  / ``SUMMARY_SYSTEM_PROMPT`` (in document order);
- the four ``case_calendar/llm.py#L<n>`` source anchors, repointed at each
  constant's current definition line;
- the ``as of **vX.Y.Z**`` version stamp, set to the project version.

Usage::

    python scripts/sync_llm_prompts_doc.py            # rewrite the page
    python scripts/sync_llm_prompts_doc.py --check     # verify it is in sync

``--check`` writes nothing and exits non-zero if the page is stale; the test
suite runs it so CI fails on drift.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "llm-prompts.md"
LLM_SRC = ROOT / "case_calendar" / "llm.py"

# The four prompt sections, in the order they appear in the page (which is the
# order their ````text blocks and source anchors appear). Each name is the
# attribute on ``case_calendar.llm`` AND the constant assigned in ``llm.py``.
PROMPT_NAMES = [
    "SYSTEM_PROMPT",
    "VERIFY_SYSTEM_PROMPT",
    "DEDUPE_HEARING_SYSTEM_PROMPT",
    "SUMMARY_SYSTEM_PROMPT",
]

_BLOCK_RE = re.compile(r"````text\n.*?\n````", re.DOTALL)
_ANCHOR_RE = re.compile(r"case_calendar/llm\.py#L\d+")
_STAMP_RE = re.compile(r"as of \*\*v\d+\.\d+\.\d+\*\*")


def project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return data["project"]["version"]


def constant_line(name: str) -> int:
    """1-based line of ``name``'s first assignment in ``llm.py``."""
    pat = re.compile(rf"^{re.escape(name)} = ")
    for i, line in enumerate(LLM_SRC.read_text().splitlines(), 1):
        if pat.match(line):
            return i
    raise SystemExit(f"constant {name!r} not found in {LLM_SRC}")


def prompt_constants() -> dict[str, str]:
    sys.path.insert(0, str(ROOT))
    import case_calendar.llm as llm  # imported lazily so --help works without deps

    return {name: getattr(llm, name) for name in PROMPT_NAMES}


def render(current: str) -> str:
    """Return the page with all machine-derived parts refreshed."""
    consts = prompt_constants()
    lines = {name: constant_line(name) for name in PROMPT_NAMES}

    blocks = _BLOCK_RE.findall(current)
    if len(blocks) != len(PROMPT_NAMES):
        raise SystemExit(
            f"expected {len(PROMPT_NAMES)} ````text blocks in {DOC.name}, "
            f"found {len(blocks)} — the page structure changed; update "
            f"PROMPT_NAMES / this script."
        )
    anchors = _ANCHOR_RE.findall(current)
    if len(anchors) != len(PROMPT_NAMES):
        raise SystemExit(
            f"expected {len(PROMPT_NAMES)} llm.py#L anchors in {DOC.name}, "
            f"found {len(anchors)}."
        )

    block_iter = iter(PROMPT_NAMES)
    out = _BLOCK_RE.sub(
        lambda _m: f"````text\n{consts[next(block_iter)].rstrip(chr(10))}\n````",
        current,
    )
    anchor_iter = iter(PROMPT_NAMES)
    out = _ANCHOR_RE.sub(
        lambda _m: f"case_calendar/llm.py#L{lines[next(anchor_iter)]}",
        out,
    )
    out = _STAMP_RE.sub(f"as of **v{project_version()}**", out)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the page is in sync without writing; exit 1 if stale",
    )
    args = parser.parse_args(argv)

    current = DOC.read_text()
    updated = render(current)

    if current == updated:
        print(f"{DOC.relative_to(ROOT)} is in sync.")
        return 0

    if args.check:
        print(
            f"{DOC.relative_to(ROOT)} is OUT OF SYNC with case_calendar/llm.py.\n"
            f"Run: python scripts/sync_llm_prompts_doc.py",
            file=sys.stderr,
        )
        return 1

    DOC.write_text(updated)
    print(f"Rewrote {DOC.relative_to(ROOT)} from case_calendar/llm.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
