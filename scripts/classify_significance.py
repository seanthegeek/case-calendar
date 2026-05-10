"""Run an LLM classification pass on every hearing with NULL significance.

PRINTS the verdicts (does not write to the DB). Pipe through `tee` if you
want a copy. After reviewing, decide which to apply manually.

We use a focused single-question prompt rather than the full action
extractor so the LLM doesn't try to allocate new hearing keys or split
hearings — that's what caused the motion-ruling duplicates earlier. We
just want major/minor + a one-line reason per hearing.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys

from dotenv import load_dotenv

from case_calendar.llm import _call_anthropic, _detect_provider
from case_calendar.store import Store


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {"significance": "?", "reason": f"no JSON in: {raw[:120]}"}
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        return {"significance": "?", "reason": f"parse error: {e}; raw={raw[:120]}"}

CLASSIFY_SYSTEM = """\
You classify court-hearing entries as MAJOR or MINOR for a calendar-sync
tool. The user's calendar should track major case moments (substantive
proceedings + dialable events) and skip purely procedural events.

MAJOR: trial, sentencing, arraignment, initial appearance, change of plea,
oral argument, evidentiary hearing, suppression hearing, motion-in-limine,
Daubert hearing, calendar call, AND ANY pretrial conference — final,
initial, CIPA, telephonic, or otherwise. All pretrial conferences are
MAJOR regardless of how routine the underlying agenda looks. Conferences
or calls about substantive issues (suppression, dismissal, classified
info, plea discussions) are MAJOR regardless of phone vs in-person.

MINOR: phone calls / status conferences whose ONLY purpose is to rule on
a procedural motion (Motion to Continue Trial, Motion to Extend
Deadlines, scheduling-only motions) or set housekeeping dates. Classify
by the proceeding's PURPOSE, not its EFFECT — a call set just to rule on
a Motion to Continue is MINOR even if the trial gets rescheduled in it.
The trial reschedule itself lives on the trial row (which is MAJOR).

Default to MAJOR when uncertain. Only emit MINOR when the source text
clearly shows the proceeding has no substantive content.

Return ONLY a JSON object: {"significance": "major"|"minor", "reason": "..."}.
No prose, no markdown."""


def classify(case_id: str, hearing: dict, source_descriptions: list[str]) -> dict:
    user = (
        f"CASE: {case_id}\n"
        f"HEARING TITLE: {hearing['title']}\n"
        f"STATUS: {hearing['status']}\n"
        f"STARTS_AT_UTC: {hearing['starts_at_utc']}\n"
        f"\nSOURCE ENTRY TEXT (the docket entries that produced this row):\n"
        + "\n\n---\n\n".join(t[:1500] for t in source_descriptions if t)
        + "\n\nClassify."
    )
    raw = _call_anthropic(CLASSIFY_SYSTEM, user, max_tokens=200)
    return _parse_json(raw)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level="WARNING")

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/case-calendar.sqlite")
    args = parser.parse_args(argv)

    if _detect_provider() != "anthropic":
        print("This script currently uses the anthropic SDK directly.",
              file=sys.stderr)
        return 1

    store = Store(args.db)
    rows = store.conn.execute(
        "SELECT case_id, hearing_key, title, status, starts_at_utc, "
        "source_entry_ids FROM hearings WHERE significance IS NULL "
        "ORDER BY case_id, starts_at_utc"
    ).fetchall()

    print(f"\n{len(rows)} hearings with NULL significance.\n")
    print(f"{'case_id':18} {'hearing_key':35} {'verdict':6} {'title':50} reason")
    print("-" * 160)

    for r in rows:
        eids = json.loads(r["source_entry_ids"] or "[]")
        descs: list[str] = []
        for eid in eids:
            row = store.conn.execute(
                "SELECT description, short_description FROM entries WHERE entry_id=?",
                (eid,),
            ).fetchone()
            if row:
                d = row["description"] or row["short_description"] or ""
                if d:
                    descs.append(d)
        if not descs:
            print(f"  {r['case_id']:18} {r['hearing_key']:35} —      "
                  f"{r['title']:50} (no source descriptions stored)")
            continue
        verdict = classify(r["case_id"], dict(r), descs)
        sig = verdict.get("significance", "?")
        reason = verdict.get("reason", "")[:80]
        print(f"  {r['case_id']:18} {r['hearing_key']:35} {sig:6} "
              f"{r['title'][:48]:50} {reason}")

    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
