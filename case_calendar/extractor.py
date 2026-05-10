"""Cheap keyword filter — decides whether an entry is worth sending to the LLM.

Intentionally over-inclusive: false positives just cost an LLM call,
false negatives lose hearings. The LLM step decides definitively.
"""

from __future__ import annotations

import re
from typing import Any

_HEARING_HINTS = re.compile(
    r"\b("
    r"hearing|sentenc\w*|arraignment|argument|conference|trial|"
    r"proceedings?|"
    r"set\s*for|reset\s*for|scheduled\s*for|set/reset|"
    r"notice\s*of\s*hearing|notice\s*of\s*rescheduling|"
    r"change\s*of\s*plea|initial\s*appearance|status|"
    r"oral\s*argument|telephonic|videoconference|video conference|zoom|"
    r"continued\s*to|continue\s*to|continued\s*until|vacated"
    r")\b",
    re.IGNORECASE,
)


def is_hearing_relevant(entry: dict[str, Any]) -> bool:
    blobs = [
        entry.get("description") or "",
        entry.get("short_description") or "",
    ]
    for rd in entry.get("recap_documents", []) or []:
        blobs.append(rd.get("description") or "")
    text = " | ".join(blobs)
    if not text.strip():
        return False
    return bool(_HEARING_HINTS.search(text))
