"""Cheap keyword filter — decides whether an entry is worth sending to the LLM.

Intentionally over-inclusive: false positives just cost an LLM call,
false negatives lose hearings or deadlines. The LLM step decides definitively.
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

# Filing-deadline vocabulary. Same over-inclusion philosophy as the hearing
# regex — a brief extension request and a granted scheduling order both
# match, and the LLM decides whether anything actually changes.
_DEADLINE_HINTS = re.compile(
    r"\b("
    r"due\s*by|due\s*on|due\s*no\s*later\s*than|"
    r"shall\s*(?:file|respond|reply|submit|serve)|"
    r"response\s*(?:is\s*)?due|reply\s*(?:is\s*)?due|"
    r"opposition\s*(?:is\s*)?due|brief\s*(?:is\s*)?due|"
    r"briefing\s*schedule|briefing\s*order|scheduling\s*order|"
    r"deadline|"
    r"motion\s*(?:to|for)\s*extend|motion\s*(?:to|for)\s*extension|"
    r"extension\s*(?:of|granted|denied)|"
    r"stipulation|stipulated|so\s*ordered|"
    r"file\s*(?:a|its|their)\s*(?:response|reply|opposition|brief|memorandum|"
    r"answer|supplemental)"
    r")\b",
    re.IGNORECASE,
)


def _entry_text(entry: dict[str, Any]) -> str:
    blobs: list[str] = []
    for raw in (entry.get("description"), entry.get("short_description")):
        if raw:
            blobs.append(raw)
    for rd in entry.get("recap_documents", []) or []:
        d = rd.get("description")
        if d:
            blobs.append(d)
    return " | ".join(blobs)


def is_hearing_relevant(entry: dict[str, Any]) -> bool:
    text = _entry_text(entry)
    if not text.strip():
        return False
    return bool(_HEARING_HINTS.search(text))


def is_deadline_relevant(entry: dict[str, Any]) -> bool:
    text = _entry_text(entry)
    if not text.strip():
        return False
    return bool(_DEADLINE_HINTS.search(text))


def is_extractable(entry: dict[str, Any], *, want_deadlines: bool = False) -> bool:
    """True iff the entry should reach the LLM at all.

    Hearing-relevant entries always reach the LLM; deadline-relevant entries
    do too when the case opts into deadline extraction.
    """
    text = _entry_text(entry)
    if not text.strip():
        return False
    if _HEARING_HINTS.search(text):
        return True
    if want_deadlines and _DEADLINE_HINTS.search(text):
        return True
    return False
