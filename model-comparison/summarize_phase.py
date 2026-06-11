#!/usr/bin/env python3
"""Phase 1/2 summary generation for the model comparison.

Runs case summaries on a given store (the scaffold) with a chosen summary
provider/model via ``summary.refresh_stale(force=True)``, then dumps the
generated prose for the manual Phase 3 grading.

- Phase 1: ``--store`` = the top hosted (gemini) scaffold; vary ``--provider/--model``
  across the fast locals + hosted gold so every model summarizes the SAME events.
- Phase 2: ``--store`` = a model's own-extraction store; ``--provider/--model`` = that
  same model.

The source store is copied first (never mutated). The store is warm (built from
the frozen snapshot), so summaries read documents locally; a real CourtListener
client is passed only because the signature needs one (used at most for a couple
of cheap, model-independent sealing-detection page reads on cold dockets).
"""

import argparse
import os
import shutil
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

ap = argparse.ArgumentParser()
ap.add_argument("--store", required=True, help="source store (scaffold) .sqlite")
ap.add_argument("--config", default="config.benchmark.yaml")
ap.add_argument("--provider", required=True)
ap.add_argument("--model", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--case", default=None, help="limit to one case id (feasibility)")
# --- thinking controls (set the Ollama knobs the call layer reads, so the
#     caller doesn't have to export env vars to A/B a model's reasoning) ---
ap.add_argument(
    "--no-think",
    action="store_true",
    help="force reasoning OFF (OLLAMA_FORCE_NO_THINK) for a boolean-thinker "
    "(gemma / qwen / glm); a no-op for the level-thinking gpt-oss family",
)
ap.add_argument(
    "--think-level",
    choices=["low", "medium", "high"],
    help="reasoning LEVEL (OLLAMA_THINK_LEVEL) for a level-thinking model "
    "(gpt-oss); a no-op for boolean-thinkers",
)
ap.add_argument(
    "--think-budget",
    type=int,
    metavar="N",
    help="reasoning headroom in tokens (OLLAMA_THINK_BUDGET); lower it to make a "
    "runaway truncate sooner",
)
args = ap.parse_args()

os.environ.setdefault("OLLAMA_BASE_URL", "http://172.17.160.1:11434")
os.environ.setdefault("OLLAMA_NUM_CTX", "65536")

# Apply the thinking-control flags to the Ollama env knobs the call layer reads.
if args.no_think:
    os.environ["OLLAMA_FORCE_NO_THINK"] = "1"
if args.think_level:
    os.environ["OLLAMA_THINK_LEVEL"] = args.think_level
if args.think_budget is not None:
    os.environ["OLLAMA_THINK_BUDGET"] = str(args.think_budget)

# INFO logging so per-docket summary calls (the `llm-tokens call purpose=summary
# ... out=N` lines) + any truncation/no-content surface live — lets a runaway
# thinking model be caught in the first docket or two instead of after the timeout.
import logging  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,
)

# --- Runaway / hung-model detection (the manual watch, automated) ---
# Two failure modes seen on local thinking models summarizing:
#   RUNAWAY — the model emits a huge reasoning trace (out= near the budget cap)
#             for a 2-4 sentence summary; the call completes but burns minutes.
#             Flagged from each completed summary call's `out=` (qwen-ON: 8015).
#   HUNG    — a single call never returns within a normal window (the model
#             reasons until the per-call HTTP timeout). It logs NO `out=` line at
#             all, so an out=-only watch is blind to it — a watchdog thread flags
#             it when no call has completed while one is in flight (glm-ON: 9 min).
# Both print a loud ⚠️ to stderr so the failure surfaces live instead of after a
# manual check. SUM_ABORT_ON_HANG=1 also exits the run (auto-curtail) rather than
# waiting out the per-call / wrapper timeout.
import re  # noqa: E402
import threading  # noqa: E402

_RUNAWAY_OUT = int(os.environ.get("SUM_RUNAWAY_OUT", "4000"))
_HANG_SECONDS = int(os.environ.get("SUM_HANG_SECONDS", "240"))
_ABORT_ON_HANG = os.environ.get("SUM_ABORT_ON_HANG", "").strip().lower() not in (
    "",
    "0",
    "false",
    "no",
)
_prog = {"last": time.time(), "in_flight": None, "flagged": None}
_START_RE = re.compile(r"case-summary llm .*\bdocket=(\S+)")
_DONE_RE = re.compile(r"purpose=summary\b.*\bdocket=(\S+).*\bout=(\d+)")


class _RunawayDetector(logging.Handler):
    """Watches the summary-call log lines: marks a docket in-flight at the
    pre-call line and flags a RUNAWAY at the post-call `out=` line."""

    def emit(self, record):
        msg = record.getMessage()
        if (m := _START_RE.search(msg)) is not None:
            _prog["last"] = time.time()
            _prog["in_flight"] = m.group(1)
        elif (m := _DONE_RE.search(msg)) is not None:
            docket, out = m.group(1), int(m.group(2))
            _prog["last"] = time.time()
            _prog["in_flight"] = None
            if out >= _RUNAWAY_OUT:
                print(
                    f"  ⚠️  RUNAWAY: docket {docket} out={out} tokens "
                    f"(>= {_RUNAWAY_OUT}) — model over-generating on a summary",
                    file=sys.stderr,
                    flush=True,
                )


def _hang_watchdog():
    while True:
        time.sleep(15)
        inflight = _prog["in_flight"]
        stalled = int(time.time() - _prog["last"])
        if inflight and stalled >= _HANG_SECONDS and _prog["flagged"] != inflight:
            _prog["flagged"] = inflight
            print(
                f"  ⚠️  HUNG: no summary completed in {stalled}s (current "
                f"docket {inflight}) — model likely running away / stuck",
                file=sys.stderr,
                flush=True,
            )
            if _ABORT_ON_HANG:
                print(
                    "  ⚠️  aborting run (SUM_ABORT_ON_HANG set)",
                    file=sys.stderr,
                    flush=True,
                )
                os._exit(3)


logging.getLogger().addHandler(_RunawayDetector())
threading.Thread(target=_hang_watchdog, daemon=True).start()

# Load .env for COURTLISTENER_TOKEN (the CLI does this; this standalone script
# must too), then NEUTRALIZE the operator's LLM_* overrides so the explicit
# --provider/--model passed to refresh_stale is authoritative (same set
# build_provider_stores pops for the same reason).
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(REPO, ".env"))
for _k in (
    "LLM_MODEL",
    "LLM_SUMMARY_MODEL",
    "LLM_PROVIDER",
    "LLM_EXTRACTION_PROVIDER",
    "LLM_SUMMARY_PROVIDER",
):
    os.environ.pop(_k, None)

from case_calendar import summary  # noqa: E402
from case_calendar.cli import _cases_from_config, _load_config  # noqa: E402
from case_calendar.courtlistener import CourtListener  # noqa: E402
from case_calendar.store import Store  # noqa: E402

cfg = _load_config(args.config)
cases = _cases_from_config(cfg)
raw = {c["id"]: c for c in cfg.get("cases", [])}
only = {args.case} if args.case else None

work = f"{args.store}.sumwork"
for ext in ("", "-wal", "-shm"):
    src = args.store + ext
    if os.path.exists(src):
        shutil.copy(src, work + ext)
store = Store(work)

t0 = time.time()
with CourtListener() as cl:
    summary.refresh_stale(
        cl=cl,
        store=store,
        cases=cases,
        case_overrides=raw,
        only_case_ids=only,
        provider=args.provider,
        model=args.model,
        force=True,
    )
dt = time.time() - t0

rows = store.conn.execute(
    "select case_id, docket_number, court_id, model, summary "
    "from case_summaries order by case_id, docket_number"
).fetchall()
with open(args.out, "w") as f:
    f.write(f"# {args.provider}/{args.model}  on  {args.store}\n")
    f.write(f"# {len(rows)} summaries in {dt:.0f}s\n\n")
    for cid, dn, court, model, summ in rows:
        f.write(f"## {cid} | {dn} ({court}) | model={model}\n{summ}\n\n")
print(
    f"DONE {args.provider}/{args.model}: {len(rows)} summaries in {dt:.0f}s -> {args.out}"
)
store.close()
