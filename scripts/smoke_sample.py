"""Pre-deploy smoke test against a sampled set of dockets, using the
real LLM and credentials loaded from .env.

CourtListener limits at the user's tier: 10 req/min, 75 req/hour, 300/day.
We pick a small sample, pace requests with sleeps, and rely on the client's
built-in 429 backoff for any burst that slips through.

Sample composition:
  * Always: Anthropic v. DOW (3 dockets, appellate / civil) — single shared
    court id, exercises multi-docket case aggregation.
  * Two random criminal dockets from the rest of config.yaml — exercises
    geographic / court variety + the hearing-extraction pipeline against
    different docket-clerk styles.

Override the criminal pick with --case-id <id> for repeated runs against the
same target. ``--seed`` makes the random pick reproducible.

We then re-sync to verify the per-docket short-circuit kicks in (zero
entries fetched, zero LLM calls on the second pass).
"""

from __future__ import annotations

import argparse
import random
import shutil
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # loads COURTLISTENER_TOKEN + LLM keys into env

import yaml

from case_calendar import llm
from case_calendar.calendars.ics import write_ics
from case_calendar.courtlistener import CourtListener
from case_calendar.courts import COURT_TIMEZONES, DEFAULT_TZ, tz_for
from case_calendar.extractor import is_hearing_relevant
from case_calendar.store import Store
from case_calendar.sync import CaseConfig, CaseSyncer, _is_fetchable

# Per-request inter-call sleep, to stay under the 10/min ceiling. With ~3
# CL calls per docket and 5 dockets we make ~15 calls — pacing at 7s keeps
# us under the cap with margin.
INTER_DOCKET_SLEEP_S = 7.0

ALWAYS_INCLUDE = ["anthropic-v-dow"]
CRIMINAL_POOL = [
    "us-v-ashtor", "us-v-knoot", "us-v-wang", "us-v-didenko",
    "us-v-jin", "us-v-hwa", "us-v-mcgonigal",
]


def _pick_cases(
    config_cases: list[dict],
    *,
    seed: int,
    extra_picks: int = 2,
    forced: list[str] | None = None,
) -> list[dict]:
    by_id = {c["id"]: c for c in config_cases}
    chosen_ids: list[str] = []
    for cid in ALWAYS_INCLUDE:
        if cid in by_id:
            chosen_ids.append(cid)
    if forced:
        for cid in forced:
            if cid not in chosen_ids and cid in by_id:
                chosen_ids.append(cid)
    rng = random.Random(seed)
    pool = [c for c in CRIMINAL_POOL if c in by_id and c not in chosen_ids]
    rng.shuffle(pool)
    needed = max(0, extra_picks - max(0, len(chosen_ids) - len(ALWAYS_INCLUDE)))
    chosen_ids.extend(pool[:needed])
    return [by_id[i] for i in chosen_ids]

parser = argparse.ArgumentParser()
parser.add_argument("--seed", type=int, default=42, help="random seed for criminal sample")
parser.add_argument("--case-id", action="append", default=[],
                    help="force-include this case_id (repeatable)")
parser.add_argument("--extra-picks", type=int, default=2,
                    help="how many random criminal cases to add on top of forced/always-include")
parser.add_argument("--config", default="config.yaml")
args = parser.parse_args()

CONFIG = yaml.safe_load(open(args.config))
chosen = _pick_cases(
    CONFIG["cases"], seed=args.seed,
    extra_picks=args.extra_picks, forced=args.case_id,
)
CASES = [
    CaseConfig(case_id=c["id"], name=c["name"],
               dockets=list(c["dockets"]), calendar=c["calendar"])
    for c in chosen
]

DATA = Path("data/smoke_sample")
OUT = Path("out/smoke_sample")
if DATA.exists():
    shutil.rmtree(DATA)
if OUT.exists():
    shutil.rmtree(OUT)

# Count CL API calls
real_get = CourtListener._get
api_calls: Counter[str] = Counter()
def counted_get(self, url, params=None):
    if "/docket-entries/" in url:
        api_calls["docket-entries/"] += 1
    elif "/dockets/" in url:
        api_calls["dockets/"] += 1
    elif "/recap-documents/" in url:
        api_calls["recap-documents/"] += 1
    elif "/courts/" in url:
        api_calls["courts/"] += 1
    else:
        api_calls["other"] += 1
    return real_get(self, url, params)
CourtListener._get = counted_get

problems: list[str] = []
def warn(msg: str) -> None:
    problems.append(msg)
    print(f"  ⚠ {msg}")

total_dockets = sum(len(c.dockets) for c in CASES)
print(f"=== Sampled smoke test: {total_dockets} dockets across {len(CASES)} cases ===")
print(f"  cases: {', '.join(c.case_id for c in CASES)}")
print(f"  LLM: {llm.provider_info()}")
print(f"  inter-docket sleep: {INTER_DOCKET_SLEEP_S}s (under 10/min CL cap)\n")
sys.stdout.flush()

store = Store(DATA / "db.sqlite")

with CourtListener() as cl:
    syncer = CaseSyncer(cl, store)

    for case_idx, case in enumerate(CASES):
        print(f"\n--- [{case_idx + 1}/{len(CASES)}] {case.case_id}: {case.name} ---")
        sys.stdout.flush()

        # First sync — exercises everything (LLM, PDF, store). Pace between
        # cases to keep CL happy.
        if case_idx > 0:
            time.sleep(INTER_DOCKET_SLEEP_S)
        t0 = time.time()
        try:
            stats = syncer.sync_case(case)
        except Exception:
            warn(f"case {case.case_id}: sync failed:\n"
                 + traceback.format_exc(limit=8))
            continue
        print(f"  first sync: {stats}  ({time.time() - t0:.1f}s)")

        # Print docket meta + tz cached during the sync.
        for docket_id in case.dockets:
            meta = store.get_docket_meta(docket_id) or {}
            court_id = meta.get("court_id") or ""
            if court_id and court_id not in COURT_TIMEZONES:
                warn(f"docket {docket_id}: court_id={court_id!r} not in "
                     f"COURT_TIMEZONES; falling back to {DEFAULT_TZ}")
            print(f"    docket {docket_id}: court={court_id} tz={tz_for(court_id)} "
                  f"docket_no={meta.get('docket_number')!r}")
        sys.stdout.flush()

    # Re-sync: should short-circuit on docket-level date_modified.
    print("\n=== Re-sync (should skip everything) ===")
    cl_calls_before = sum(api_calls.values())
    skipped_total = 0
    expected = sum(len(c.dockets) for c in CASES)
    for case in CASES:
        try:
            stats = syncer.sync_case(case)
            skipped_total += stats["dockets_skipped"]
        except Exception:
            warn(f"case {case.case_id}: re-sync failed:\n" + traceback.format_exc(limit=5))
    cl_calls_after = sum(api_calls.values())
    second_pass_calls = cl_calls_after - cl_calls_before
    print(f"  dockets_skipped: {skipped_total}/{expected}, "
          f"CL calls during re-sync: {second_pass_calls} (1 per docket expected)")
    if skipped_total != expected:
        warn(f"re-sync skipped {skipped_total}/{expected} dockets; expected all to short-circuit")

# --- Hearings produced ---

print("\n=== Hearings extracted ===")
total_hearings = 0
for case in CASES:
    hearings = sorted(store.get_hearings(case.case_id), key=lambda h: h.get("starts_at_utc") or "")
    print(f"\n  {case.case_id}: {len(hearings)} hearing(s)")
    for h in hearings:
        flag = ""
        if not h.get("starts_at_utc"):
            flag = "  ⚠ no starts_at_utc"
        print(f"    [{h['status']:<10}] {h.get('starts_at_utc') or '??????????'}  "
              f"{h['title']}  ({h['hearing_key']}){flag}")
        if h.get("location") or h.get("judge"):
            print(f"       loc={h.get('location')!r} judge={h.get('judge')!r}")
        if h.get("dial_in"):
            print(f"       dial_in={h['dial_in']!r}")
    total_hearings += len(hearings)

# --- ICS spot-check: write outputs and confirm files are non-empty ---

print("\n=== ICS output spot-check ===")
by_calendar: dict[str, list[dict]] = {}
for case in CASES:
    for h in store.get_hearings(case.case_id):
        h = dict(h)
        short = case.name.split(" v. ")[0] if " v. " in case.name else case.name
        h["title"] = f"{short}: {h['title']}"
        docket_id = h.get("docket_id")
        if docket_id:
            meta = store.get_docket_meta(docket_id) or {}
            h["docket_number"] = meta.get("docket_number")
            h["docket_absolute_url"] = meta.get("absolute_url")
            court_id = meta.get("court_id")
            if court_id:
                h["court_citation"] = store.get_court_citation(court_id)
        ids = list(h.get("source_entry_ids") or [])
        texts = []
        for eid in sorted(ids, reverse=True):
            t = store.get_entry_text(eid)
            if t:
                t["entry_id"] = eid
                texts.append(t)
        h["source_entry_texts"] = texts
        by_calendar.setdefault(case.calendar, []).append(h)

OUT.mkdir(parents=True, exist_ok=True)
for cal, hearings in by_calendar.items():
    p = OUT / f"{cal}.ics"
    write_ics(p, calendar_name=cal, hearings=hearings)
    body = p.read_text()
    n_events = body.count("BEGIN:VEVENT")
    print(f"  {p}: {n_events} events, {len(body)} bytes")
    if n_events == 0 and any(h.get("starts_at_utc") for h in hearings):
        warn(f"{p}: no events written despite hearings having dates")

# --- Summary ---

print("\n" + "=" * 70)
print(f"PROBLEMS: {len(problems)}")
for p in problems:
    print(f"  - {p}")

print(f"\nCL API CALLS: {sum(api_calls.values())} total")
for k, v in api_calls.most_common():
    print(f"  {k}: {v}")

print(f"\nHEARINGS extracted across all cases: {total_hearings}")
if problems:
    sys.exit(1)
print("\n✓ Smoke test passed.")
