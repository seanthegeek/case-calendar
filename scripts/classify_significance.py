"""Run an LLM classification pass on stored hearings.

Default: classify only rows with NULL significance, print verdicts only.
  --all   : reclassify every hearing (overwrites existing significance).
  --apply : write the verdict back to the DB.

We use a focused single-question prompt rather than the full action
extractor so the LLM doesn't try to allocate new hearing keys or split
hearings — that's what caused the motion-ruling duplicates earlier. We
just want major/minor + a one-line reason per hearing.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys

from dotenv import load_dotenv

from case_calendar.llm import _DEFAULT_MODELS, _detect_provider, SIGNIFICANCE_RULES
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

CLASSIFY_SYSTEM = (
    "You classify a single court hearing as MAJOR or MINOR for a "
    "calendar-sync tool. Output ONLY a JSON object: "
    '{"significance": "major"|"minor", "reason": "..."}. '
    "No prose, no markdown.\n\n"
) + SIGNIFICANCE_RULES


def classify(case_id: str, hearing: dict, source_descriptions: list[str]) -> dict:
    user = (
        f"CASE: {case_id}\n"
        f"HEARING TITLE: {hearing['title']}\n"
        f"STATUS: {hearing['status']} "
        f"(note: status does NOT affect significance — see rule 1)\n"
        f"STARTS_AT_UTC: {hearing['starts_at_utc']}\n"
        f"\nSOURCE ENTRY (context only — the docket entry that produced "
        f"this DB row; do NOT let this override the hearing's type):\n"
        + "\n\n---\n\n".join(t[:1500] for t in source_descriptions if t)
        + "\n\nClassify."
    )
    raw = _call_anthropic_deterministic(CLASSIFY_SYSTEM, user, max_tokens=200)
    return _parse_json(raw)


def _call_anthropic_deterministic(system: str, user: str, max_tokens: int) -> str:
    """Local anthropic call with temperature=0 so re-runs are deterministic."""
    import anthropic

    model = os.environ.get("LLM_MODEL", _DEFAULT_MODELS["anthropic"])
    client = anthropic.Anthropic(timeout=60.0)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0,
        system=[{"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    for block in resp.content:
        if block.type == "text":
            return block.text
    raise ValueError("No text block in Anthropic response")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level="WARNING")

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/case-calendar.sqlite")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Reclassify every hearing (default: only rows with NULL significance).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write verdicts back to the DB (default: print only).",
    )
    args = parser.parse_args(argv)

    if _detect_provider() != "anthropic":
        print("This script currently uses the anthropic SDK directly.",
              file=sys.stderr)
        return 1

    store = Store(args.db)
    where = "" if args.all else "WHERE significance IS NULL"
    rows = store.conn.execute(
        f"SELECT case_id, hearing_key, title, status, starts_at_utc, "
        f"significance AS prev_significance, source_entry_ids "
        f"FROM hearings {where} ORDER BY case_id, starts_at_utc"
    ).fetchall()

    scope = "every hearing" if args.all else "hearings with NULL significance"
    mode = "APPLY (writes DB)" if args.apply else "dry-run (print only)"
    print(f"\n{len(rows)} {scope}. Mode: {mode}.\n")
    print(f"{'case_id':18} {'hearing_key':35} {'prev':5} {'new':5} {'title':45} reason")
    print("-" * 160)

    changed = 0
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
        prev = r["prev_significance"] or "—"
        if not descs:
            print(f"  {r['case_id']:18} {r['hearing_key']:35} {prev:5} —     "
                  f"{r['title'][:43]:45} (no source descriptions stored)")
            continue
        verdict = classify(r["case_id"], dict(r), descs)
        sig = verdict.get("significance", "?")
        reason = verdict.get("reason", "")[:80]
        flips = sig != prev and sig in ("major", "minor")
        flag = "*" if flips else " "
        print(f" {flag}{r['case_id']:18} {r['hearing_key']:35} {prev:5} {sig:5} "
              f"{r['title'][:43]:45} {reason}")
        if flips:
            changed += 1
            if args.apply:
                store.conn.execute(
                    "UPDATE hearings SET significance=? WHERE case_id=? AND hearing_key=?",
                    (sig, r["case_id"], r["hearing_key"]),
                )

    if args.apply:
        store.conn.commit()
        print(f"\nApplied {changed} updates.")
    else:
        print(f"\nDry-run: {changed} rows would change "
              f"(re-run with --apply to write).")

    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
