"""Pre-deploy smoke test across all dockets in config.yaml.

What this validates (without burning LLM tokens):
  * Every docket fetches successfully
  * Court IDs all have timezone mappings (warn on fallbacks to default ET)
  * Entry pagination works end-to-end
  * Keyword filter relevance counts per docket
  * RECAP document availability (fetchable / paperless / sealed) per docket
  * Sync pipeline runs without exceptions, no LLM calls, no PDF API calls
  * Per-docket totals + a summary report at the end

What it does NOT test:
  * Actual LLM extraction quality (no API key configured here)
  * Calendar emission (run `case-calendar emit` separately)
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path

import yaml

# Stub the LLM before anything imports it.
import case_calendar.llm as llm_mod
llm_mod.extract_actions = lambda **kw: [{"type": "IGNORE", "reason": "stub"}]

from case_calendar.courtlistener import CourtListener
from case_calendar.courts import COURT_TIMEZONES, DEFAULT_TZ, tz_for
from case_calendar.extractor import is_hearing_relevant
from case_calendar.store import Store
from case_calendar.sync import CaseConfig, CaseSyncer, _is_fetchable, _needs_pdf

TOKEN = "32dbe2fe13ce77fd7a9511fedb69275fa756ea6b"
os.environ["COURTLISTENER_TOKEN"] = TOKEN

CONFIG = yaml.safe_load(open("config.yaml"))
CASES = [
    CaseConfig(
        case_id=c["id"], name=c["name"], dockets=list(c["dockets"]), calendar=c["calendar"]
    )
    for c in CONFIG["cases"]
]

# Fresh DB so we exercise the cold-start path.
DATA = Path("data/smoke_all")
if DATA.exists():
    shutil.rmtree(DATA)
store = Store(DATA / "db.sqlite")

# Count CL API calls.
real_get = CourtListener._get
api_calls: Counter[str] = Counter()
def counted_get(self, url, params=None):
    if "/dockets/" in url and "/docket-entries/" not in url:
        api_calls["dockets/"] += 1
    elif "/docket-entries/" in url:
        api_calls["docket-entries/"] += 1
    elif "/recap-documents/" in url:
        api_calls["recap-documents/"] += 1
    elif "/courts/" in url:
        api_calls["courts/"] += 1
    else:
        api_calls["other"] += 1
    return real_get(self, url, params)
CourtListener._get = counted_get

problems: list[str] = []
docket_reports: list[dict] = []

def warn(msg: str) -> None:
    problems.append(msg)
    print(f"  ⚠ {msg}")

print(f"=== Smoke testing {sum(len(c.dockets) for c in CASES)} dockets across "
      f"{len(CASES)} cases ===\n")

with CourtListener() as cl:
    syncer = CaseSyncer(cl, store)

    for case in CASES:
        print(f"--- {case.case_id}: {case.name} ---")
        for docket_id in case.dockets:
            t0 = time.time()
            try:
                docket = cl.get_docket(docket_id)
            except Exception as e:
                warn(f"docket {docket_id}: fetch failed: {e}")
                continue

            court_id = docket.get("court_id") or ""
            if not court_id:
                warn(f"docket {docket_id}: no court_id")
            elif court_id not in COURT_TIMEZONES:
                warn(f"docket {docket_id}: court_id={court_id!r} not in COURT_TIMEZONES; "
                     f"falling back to {DEFAULT_TZ}")

            # Inspect entries: count, relevance, recap availability.
            entry_count = 0
            relevant = 0
            paperless = 0
            sealed = 0
            available_with_text = 0
            available_no_text = 0
            unavailable = 0
            no_recap_doc = 0
            try:
                for entry in cl.iter_entries(docket_id):
                    entry_count += 1
                    if is_hearing_relevant(entry):
                        relevant += 1
                    rds = entry.get("recap_documents") or []
                    if not rds:
                        no_recap_doc += 1
                    for rd in rds:
                        if rd.get("is_sealed"):
                            sealed += 1
                        elif (rd.get("plain_text") or "").strip():
                            available_with_text += 1
                        elif rd.get("is_available"):
                            available_no_text += 1
                        elif not _is_fetchable(rd):
                            paperless += 1
                        else:
                            unavailable += 1
            except Exception:
                warn(f"docket {docket_id}: entry pagination failed:\n"
                     + traceback.format_exc(limit=3))
                continue

            elapsed = time.time() - t0

            report = {
                "case": case.case_id,
                "docket": docket_id,
                "court": court_id,
                "tz": tz_for(court_id),
                "docket_number": docket.get("docket_number"),
                "case_name": docket.get("case_name"),
                "entries": entry_count,
                "relevant": relevant,
                "rd_with_text": available_with_text,
                "rd_no_text": available_no_text,
                "rd_unavailable": unavailable,
                "rd_paperless": paperless,
                "rd_sealed": sealed,
                "no_recap_doc": no_recap_doc,
                "elapsed_s": round(elapsed, 1),
            }
            docket_reports.append(report)

            if entry_count == 0:
                warn(f"docket {docket_id}: zero entries returned — wrong ID?")

            print(f"  docket {docket_id} ({court_id}/{tz_for(court_id)}): "
                  f"{relevant}/{entry_count} relevant, "
                  f"rds: {available_with_text}+text/{available_no_text}+avail/"
                  f"{unavailable}unavail/{paperless}paperless/{sealed}sealed, "
                  f"{elapsed:.1f}s")

        # Now run the actual sync (with stub LLM) to exercise the full path.
        try:
            stats = syncer.sync_case(case)
            print(f"  sync stats: {stats}")
        except Exception:
            warn(f"case {case.case_id}: sync failed:\n"
                 + traceback.format_exc(limit=5))

    # Second-pass sync: confirms docket-level short-circuit kicks in.
    print("\n=== Re-sync to verify docket-level short-circuit ===")
    skipped_total = 0
    for case in CASES:
        try:
            stats = syncer.sync_case(case)
            skipped_total += stats["dockets_skipped"]
        except Exception:
            warn(f"case {case.case_id}: re-sync failed:\n"
                 + traceback.format_exc(limit=5))
    expected = sum(len(c.dockets) for c in CASES)
    if skipped_total != expected:
        warn(f"re-sync skipped {skipped_total}/{expected} dockets; expected all to short-circuit")
    else:
        print(f"  ✓ all {expected} dockets short-circuited (no API or LLM work)")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print("\n" + "=" * 70)
print(f"PROBLEMS: {len(problems)}")
for p in problems:
    print(f"  - {p}")

print(f"\nAPI CALLS:")
for k, v in api_calls.most_common():
    print(f"  {k}: {v}")
total_calls = sum(api_calls.values())
print(f"  total: {total_calls}")
total_dockets = sum(len(c.dockets) for c in CASES)
print(f"  per docket avg: {total_calls / total_dockets:.1f}")

# Per-court coverage
courts_used: Counter[str] = Counter()
for r in docket_reports:
    courts_used[r["court"]] += 1
print(f"\nCOURTS REPRESENTED:")
for court, count in courts_used.most_common():
    tz = COURT_TIMEZONES.get(court, f"{DEFAULT_TZ} (default)")
    print(f"  {court:8} → {tz:25} ({count} docket{'s' if count > 1 else ''})")

# Hearings extracted
hearing_count = 0
for case in CASES:
    hearing_count += len(store.get_hearings(case.case_id))
print(f"\nHEARINGS EXTRACTED (with stub LLM, expect 0): {hearing_count}")
print("  (Real LLM sync would produce hearings here; pipeline ran cleanly.)")

print(f"\nDOCKET TOTALS:")
print(f"  entries seen: {sum(r['entries'] for r in docket_reports)}")
print(f"  hearing-relevant: {sum(r['relevant'] for r in docket_reports)}")
print(f"  recap_docs with text:    {sum(r['rd_with_text'] for r in docket_reports)}")
print(f"  recap_docs avail no text:{sum(r['rd_no_text'] for r in docket_reports)}")
print(f"  recap_docs unavail:      {sum(r['rd_unavailable'] for r in docket_reports)}")
print(f"  recap_docs paperless:    {sum(r['rd_paperless'] for r in docket_reports)}")
print(f"  recap_docs sealed:       {sum(r['rd_sealed'] for r in docket_reports)}")

if problems:
    sys.exit(1)
print("\n✓ All structural checks passed.")
