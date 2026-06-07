"""Cheap keyword filter — decides whether an entry is worth sending to the LLM.

Intentionally over-inclusive: false positives just cost an LLM call,
false negatives lose hearings or deadlines. The LLM step decides definitively.
"""

from __future__ import annotations

import re
from typing import Any

_HEARING_HINTS = re.compile(
    r"\b("
    r"hearing|sentenc\w*|arraignment|argu\w*|conference|trial|"
    r"proceedings?|"
    r"set\s*for|reset\s*for|scheduled\s*for|set/reset|"
    r"notice\s*of\s*hearing|notice\s*of\s*rescheduling|"
    r"change\s*of\s*plea|initial\s*appearance|status|"
    r"telephonic|videoconference|video conference|zoom|"
    r"mandates?|calendared|"
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
    r"appendix\s*(?:is\s*)?due|answer\s*(?:is\s*)?due|"
    r"notice\s*(?:is\s*)?due|report\s*(?:is\s*)?due|"
    r"memo(?:randum)?\s*(?:is\s*)?due|material\s*(?:is\s*)?due|"
    r"briefing\s*schedules?|briefing\s*orders?|scheduling\s*orders?|"
    r"deadlines?|"
    r"motions?\b[^.\n]{0,80}\b(?:to|for)\s*(?:extend|extension)|"
    r"extensions?\s*(?:of|granted|denied)|"
    r"rehearing|mandates?|"
    r"presentenc\w*|psr|cipa|jencks|"
    r"notice\s+of\s+appeal|"
    r"disclosures?|"
    r"discovery\s+(?:cutoff|cut-off|close|closes)|"
    r"motions?\s+in\s+limine|"
    r"class\s*cert(?:ification)?|"
    r"(?:joint\s+)?status\s+reports?|"
    r"mediations?|"
    r"pretrial\s+orders?|"
    r"claim\s+construction|markman|"
    r"stipulations?|stipulated|so\s*ordered|"
    r"file\s*(?:a|its|their)\s*(?:response|reply|opposition|brief|memorandum|"
    r"answer|supplemental)"
    r")\b",
    re.IGNORECASE,
)

# Deadline forms the `\b(...)\b`-wrapped _DEADLINE_HINTS above can't express,
# both surfaced by the ground-truth scoring as entries that set or met a real
# deadline yet were dropped before any LLM saw them:
#   - Bare "due <date>" — _DEADLINE_HINTS only catches "due by/on". Appellate
#     and clerk orders write "docketing statement due 04/08/2026"; one D.C.
#     Circuit clerk order set ELEVEN such deadlines in a single entry, all lost.
#     Anchored to an actual date (or "within N days") so "due process" /
#     "due diligence" don't match.
#   - A party FILING that MEETS a deadline: a brief / response / opposition /
#     reply at the head of the entry ("BRIEF by ...", "RESPONSE in Opposition
#     ...", "PETITIONER REPLY BRIEF ..."), or any appellate submission carrying
#     the "[Service Date: ...]" e-filing stamp. The existing regex only catches
#     the forward-looking "shall file a response", never the past-tense filing,
#     so the matching deadline never flipped to filed.
# Kept as its own regex (not folded into _DEADLINE_HINTS) because the date forms
# end in digits, which that pattern's trailing `\b` cannot anchor, and because
# the filing forms must anchor to the entry head (`^`, MULTILINE).
_MONTHS = r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_DEADLINE_EXTRA_HINTS = re.compile(
    r"\bdue\s+(?:by\s+|on\s+|within\s+|no\s+later\s+than\s+)?"
    rf"(?:\d{{1,2}}[/-]\d|\d{{4}}\b|\d{{1,2}}\s+(?:business\s+)?days\b|(?:{_MONTHS}))"
    r"|\[service\s+date:"
    r"|^\s*(?:\d+\s+)?"
    r"(?:(?:petitioner|respondent|appellant|appellee|cross-?appell\w+|"
    r"opening|answering|amended|corrected|supplemental|first|second|third|"
    r"final|joint)\s+)*"
    r"(?:response|opposition|reply|sur-?reply|brief)\b",
    re.IGNORECASE | re.MULTILINE,
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
    return bool(_DEADLINE_HINTS.search(text) or _DEADLINE_EXTRA_HINTS.search(text))


def is_extractable(entry: dict[str, Any]) -> bool:
    """True iff the entry should reach the LLM at all.

    Hearing-relevant and deadline-relevant entries both reach the LLM; the
    LLM then decides whether anything actually changes. Deadline extraction
    is now uniform across all dockets — there's no per-case opt-in.
    """
    text = _entry_text(entry)
    if not text.strip():
        return False
    return bool(
        _HEARING_HINTS.search(text)
        or _DEADLINE_HINTS.search(text)
        or _DEADLINE_EXTRA_HINTS.search(text)
    )
