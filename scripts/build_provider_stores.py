#!/usr/bin/env python3
"""Build one full store + rendered outputs per LLM provider, for head-to-head comparison.

A/B/C the three providers on your real cases, eyeball each one's rendered
calendars + summaries, then push the best store to prod. Each provider
re-derives the LLM-produced tables (hearings, deadlines, case summaries) from
scratch against an identical copy of the warm store. The CourtListener-fetched
facts — the ``entries`` rows, their cached ``recap_documents`` (with
``plain_text``), and docket / court metadata — are SHARED and left untouched.

Constraints honored:

  * **CourtListener is hit at most once, total — never once per provider.** A
    shared response cache (CourtListener ``_get``/``_post`` + ``pdf.extract_text``
    by document id) is populated by the first build and reused by the rest. The
    CourtListener-derived inputs are identical across providers (only the LLM
    differs), so caching them is exact, not an approximation. The build reports
    the total CourtListener API calls AND the peak per-minute / hour / day rate,
    so you can size it against your API tier.

  * **LLM calls run in parallel across providers.** All providers build
    concurrently from the start; provider selection is thread-local so the
    concurrent builds never race on a global env var, and the shared caches
    are fetch-under-lock so the CourtListener-once guarantee holds regardless
    of concurrency (whichever thread reaches a request first makes the single
    network call; the rest get the cached response). ``--no-parallel`` forces
    strictly one-at-a-time.

  * **The result is pushable to prod.** Only the LLM-derived tables are rebuilt;
    the entries table (fingerprints, bodies) is untouched, so the store behaves
    exactly like a prod store on the next real sync (no spurious re-extraction).

  * **Each provider's run is independently readable.** The builds share one
    stderr stream, so the console interleaves every provider's lines — but each
    log record is ALSO routed to ``<provider>/build.log`` by the emitting
    thread's provider, and the extractor-track LLM calls emit a per-entry
    DECISION line (what the model decided for each entry / hearing / deadline)
    into that same file. So after a build you can read one provider's reasoning
    end-to-end without untangling it from the others. ``--no-decisions`` keeps
    the per-provider build.log but drops the per-entry DECISION trace.

Layout — one subfolder per provider:

    data/provider-stores/<provider>/
        case-calendar.sqlite        # the candidate store
        build.log                    # this provider's full sync log + DECISION trace
        out/                         # rendered ICS + index.html (NO push to gcal/M365)
            <calendar>.ics ...
            index.html

For each provider P:
  1. Copy the warm source store -> data/provider-stores/<P>/case-calendar.sqlite
  2. Clear the LLM-derived tables (hearings / deadlines / case_summaries) for the
     cases in scope. The entries table is NOT touched.
  3. Pin provider = P and replay the REAL pipeline against the cached entries:
     ``CaseSyncer._handle_entry`` per body-bearing entry, then the end-of-sync
     verify / dedupe sweeps, then ``summary.refresh_stale(force)``.
  4. Render ICS + index.html into <P>/out/ (push-ids stripped, so nothing goes
     to a real Google / M365 calendar). Keep everything.

Prove the replay is faithful with ``--validate``: build the ANTHROPIC store and
diff its row counts against your current prod store (which anthropic produced)
— they should match. Once that holds, the openai / gemini stores are trustworthy.

Push the winner yourself (stop ``serve`` first, back up the live store):

    cp data/provider-stores/<P>/case-calendar.sqlite data/case-calendar.sqlite

Usage:

    # no API calls, no spend — validate the replay plumbing on one case:
    uv run python scripts/build_provider_stores.py --fake --case <case_id>

    # build the anthropic store for one case and check it against prod:
    uv run python scripts/build_provider_stores.py --providers anthropic --case <case_id> --validate

    # full build, write a markdown report:
    uv run python scripts/build_provider_stores.py --validate --out out/provider_compare.md
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import shutil
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

# Load .env before any module reads provider keys / the CourtListener token.
load_dotenv()

from case_calendar import costs, courtlistener, llm, pdf, summary  # noqa: E402
from case_calendar.cli import (  # noqa: E402
    _cases_from_config,
    _load_config,
    emit_calendars,
)
from case_calendar.courtlistener import CourtListener  # noqa: E402
from case_calendar.courts import tz_for  # noqa: E402
from case_calendar.llmkit import providers, usage  # noqa: E402
from case_calendar.store import Store  # noqa: E402
from case_calendar.sync import CaseConfig, CaseSyncer  # noqa: E402

logger = logging.getLogger("provider_stores")

# Project-default model per provider per track — we pin these so the build
# prices the out-of-the-box configuration regardless of any LLM_MODEL override.
EXTRACT_MODELS = dict(providers._DEFAULT_MODELS)
SUMMARY_MODELS = dict(llm._DEFAULT_SUMMARY_MODELS)
ALL_PROVIDERS = ["anthropic", "openai", "gemini"]
OUT_DIR = Path("data/provider-stores")
DERIVED_TABLES = ("hearings", "deadlines", "case_summaries")

# Provider selection is thread-local so the parallel builds don't race on a
# global. ``providers._detect_provider`` (used by the extraction-track
# functions) is patched to read this; the summary track is told its provider
# explicitly via ``refresh_stale(provider=...)``.
_TL = threading.local()
_REAL_DETECT = providers._detect_provider


def _tl_detect() -> Optional[str]:
    return getattr(_TL, "provider", None) or _REAL_DETECT()


# ---------------------------------------------------------------------------
# Cost capture (one row per LLM call, bucketed by provider / docket / track)
# ---------------------------------------------------------------------------


@dataclass
class _Call:
    provider: str
    model: str
    purpose: str
    docket: str
    tokens: usage.TokenUsage
    cost: Optional[float]


@dataclass
class _Capture:
    calls: list[_Call] = field(default_factory=list)
    cl_calls: int = 0


CAP = _Capture()
_ORIG_RECORD = usage.record
_CAP_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()

# Shared response caches, populated on the FIRST provider build and reused by
# every subsequent build, so any genuine CourtListener fetch / PDF extraction
# happens at most once TOTAL across all providers — never duplicated per
# provider.
_GET_CACHE: dict[str, Any] = {}
_PDF_CACHE: dict[Any, Any] = {}
_ORIG_PDF_EXTRACT = pdf.extract_text


def _track_for(purpose: str) -> str:
    if purpose == "summary":
        return "summary"
    if purpose == "extract":
        return "extraction"
    return "verify"  # verify_hearing / verify_deadline / dedupe_hearings


def _capturing_record(
    *,
    purpose: str,
    provider: str,
    model: str,
    tokens: usage.TokenUsage,
    docket: Any = None,
) -> None:
    """Wrap ``usage.record``: capture the call for our per-provider report, then
    delegate to the real recorder so the normal ``llm-tokens`` log lines (with
    ``cost_est``) still print for live monitoring. Thread-safe."""
    call = _Call(
        provider=provider,
        model=model,
        purpose=purpose,
        docket="?" if docket is None else str(docket),
        tokens=tokens,
        cost=costs.estimate_cost(model, tokens),
    )
    with _CAP_LOCK:
        CAP.calls.append(call)
    _ORIG_RECORD(
        purpose=purpose, provider=provider, model=model, tokens=tokens, docket=docket
    )


def _fake_dispatch(
    provider: str,
    system: str,
    user: str,
    max_tokens: int,
    *,
    model: Optional[str] = None,
    json_mode: bool = True,
    purpose: str = "llm",
    docket: Any = None,
) -> str:
    """Stand-in for ``_dispatch_llm_call`` in --fake mode: synthetic token
    counts proportional to prompt length, no API call, no spend. ``provider``
    is the resolved provider the caller passed (thread-local for extraction,
    explicit for summaries), so the synthetic call is tagged correctly."""
    m = model or (SUMMARY_MODELS if purpose == "summary" else EXTRACT_MODELS).get(
        provider, "fake"
    )
    toks = usage.TokenUsage(input=(len(system) + len(user)) // 4, output=40)
    usage.record(
        purpose=purpose, provider=provider, model=m, tokens=toks, docket=docket
    )
    return (
        '{"actions": []}' if json_mode else "Fake summary text for plumbing validation."
    )


# ---------------------------------------------------------------------------
# Per-provider log + decision capture
# ---------------------------------------------------------------------------
#
# The builds run concurrently and all share one stderr stream, so the console
# is an interleaved mix of every provider's lines. To make each provider's run
# readable after the fact, ``_PerProviderLogHandler`` routes every log record
# to ``<provider>/build.log`` based on the emitting thread's thread-local
# provider (set at the top of ``build_for_provider``). On top of that, the
# extractor-track LLM entry points are wrapped to emit one DECISION line per
# call describing what that provider's model decided for the entry / hearing /
# deadline — the per-entry "reasoning" the token telemetry alone doesn't show.
# DECISION lines log through ``_DLOG`` and are kept OFF the interleaved console
# (a filter drops them from the stderr handler); they land only in each
# provider's build.log, already de-interleaved by provider.

_DLOG = logging.getLogger("provider_stores.decisions")
_LOG_FMT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class _DropDecisions(logging.Filter):
    """Drop the verbose per-entry DECISION lines from a handler (the stderr
    console) so they don't flood the interleaved stream — they still reach each
    provider's build.log via ``_PerProviderLogHandler``."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name != _DLOG.name


class _PerProviderLogHandler(logging.Handler):
    """Write each log record to ``<provider>/build.log`` based on the emitting
    thread's thread-local provider. Records emitted outside a provider build
    (``_TL.provider`` is None — e.g. the main thread during report assembly)
    are ignored here; they still reach the stderr handler. Thread-safe: each
    provider gets its own stream, opened once under a lock."""

    def __init__(self) -> None:
        super().__init__()
        self._streams: dict[str, Any] = {}
        self._slock = threading.Lock()

    def _stream_for(self, provider: str) -> Any:
        with self._slock:
            s = self._streams.get(provider)
            if s is None:
                path = _provider_dir(provider) / "build.log"
                path.parent.mkdir(parents=True, exist_ok=True)
                s = path.open("w", encoding="utf-8")
                self._streams[provider] = s
            return s

    def emit(self, record: logging.LogRecord) -> None:
        provider = getattr(_TL, "provider", None)
        if not provider:
            return
        try:
            stream = self._stream_for(provider)
            stream.write(self.format(record) + "\n")
            stream.flush()
        except Exception:  # noqa: BLE001 — logging must never crash a build
            self.handleError(record)

    def close(self) -> None:
        with self._slock:
            for s in self._streams.values():
                try:
                    s.close()
                except Exception:  # noqa: BLE001
                    pass
            self._streams.clear()
        super().close()


def _short(s: Optional[str], n: int = 80) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _action_brief(a: Any) -> str:
    """Compact one-line summary of a single LLM action / decision dict: the
    TYPE plus whichever of key / significance / date the action carries."""
    if not isinstance(a, dict):
        return repr(a)
    atype = str(a.get("type") or "?").upper()
    extras: list[str] = []
    key = a.get("hearing_key") or a.get("deadline_key") or a.get("target_key")
    if key:
        extras.append(str(key))
    if a.get("significance"):
        extras.append(str(a["significance"]))
    for dk in ("local_date", "due_date", "new_local_date", "local_time"):
        if a.get(dk):
            extras.append(str(a[dk]))
    return f"{atype}({', '.join(extras)})" if extras else atype


def _format_decision(kind: str, kwargs: dict[str, Any], result: Any) -> str:
    """Render one DECISION line for a wrapped extractor-track LLM call.

    ``kind`` selects which kwarg carries the context (the entry, the candidate
    hearing / deadline, or the same-slot cluster); ``result`` is what the model
    returned — a list of actions for extract, a single decision dict for the
    verify / dedupe passes."""
    if kind == "extract":
        entry = kwargs.get("entry") or {}
        acts = result if isinstance(result, list) else [result]
        summary = ", ".join(_action_brief(a) for a in acts) if acts else "(none)"
        desc = _short(entry.get("short_description") or entry.get("description"))
        return (
            f"extract docket={kwargs.get('docket_id')} "
            f'entry={entry.get("id")} "{desc}" -> {summary}'
        )
    if kind == "verify_hearing":
        h = kwargs.get("hearing") or {}
        return (
            f"verify_hearing key={h.get('hearing_key')!r} "
            f"starts={h.get('starts_at_utc')} status={h.get('status')} "
            f"-> {_action_brief(result)}"
        )
    if kind == "verify_deadline":
        d = kwargs.get("deadline") or {}
        return (
            f"verify_deadline key={d.get('deadline_key')!r} "
            f"due={d.get('due_at_utc')} status={d.get('status')} "
            f"-> {_action_brief(result)}"
        )
    if kind == "dedupe":
        cluster = kwargs.get("cluster") or []
        keys = ", ".join(str(h.get("hearing_key")) for h in cluster)
        starts = cluster[0].get("starts_at_utc") if cluster else None
        return f"dedupe cluster=[{keys}] starts={starts} -> {_action_brief(result)}"
    return f"{kind} -> {result!r}"


# extractor-track LLM entry points -> the context kind each call carries.
_DECISION_WRAPS = {
    "extract_actions": "extract",
    "verify_hearing": "verify_hearing",
    "verify_deadline": "verify_deadline",
    "resolve_duplicate_hearings": "dedupe",
}


def _wrap_llm(name: str, kind: str) -> Any:
    """Return a wrapper around ``llm.<name>`` that, after delegating to the
    real function, logs one DECISION line tagged to the calling thread's
    provider (via ``_PerProviderLogHandler``). The real result is returned
    unchanged; a logging failure never sinks a build."""
    orig = getattr(llm, name)

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        result = orig(*args, **kwargs)
        if getattr(_TL, "provider", None):
            try:
                _DLOG.info("%s", _format_decision(kind, kwargs, result))
            except Exception:  # noqa: BLE001
                logger.debug("decision log failed for %s", name, exc_info=True)
        return result

    return wrapped


# ---------------------------------------------------------------------------
# Shared caches
# ---------------------------------------------------------------------------


def _install_cl_cache(cl: CourtListener) -> None:
    """Wrap the CourtListener API verbs with a shared, thread-safe response
    cache. A cache MISS is a genuine network call (counted); a HIT is served
    from an earlier build's response, so the same request is never sent twice
    across providers."""
    for name in ("_get", "_post"):
        orig = getattr(cl, name)

        def make(orig: Any, verb: str):
            def wrapped(*a: Any, **k: Any) -> Any:
                key = f"{verb}:{a!r}:{sorted(k.items())!r}"
                with _CACHE_LOCK:
                    if key in _GET_CACHE:
                        return _GET_CACHE[key]
                    CAP.cl_calls += 1  # genuine network call
                    resp = orig(*a, **k)
                    _GET_CACHE[key] = resp
                    return resp

            return wrapped

        setattr(cl, name, make(orig, name))


def _install_pdf_cache() -> None:
    """Memoize ``pdf.extract_text`` by recap_document id so a document's text
    (and any file download / OCR behind it) is produced once and reused by
    every provider build — extraction and summaries alike."""

    def cached_extract_text(rd: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        key = (rd.get("id"), args, tuple(sorted(kwargs.items())))
        # Fetch INSIDE the lock so concurrent provider threads can't both miss
        # and double-fetch the same document — a document's text (and any file
        # download / OCR behind it) is produced exactly once. The common case
        # is cached plain_text, which returns instantly, so the lock is held
        # only briefly; a rare OCR holds it for its duration.
        with _CACHE_LOCK:
            if key in _PDF_CACHE:
                return _PDF_CACHE[key]
            text = _ORIG_PDF_EXTRACT(rd, *args, **kwargs)
            _PDF_CACHE[key] = text
            return text

    pdf.extract_text = cached_extract_text  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Store handling
# ---------------------------------------------------------------------------


def _provider_dir(provider: str) -> Path:
    return OUT_DIR / provider


def _copy_store(src_path: str, dst_path: str) -> None:
    src = Path(src_path)
    if not src.exists():
        raise SystemExit(f"source store not found: {src_path}")
    Path(dst_path).parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        s = Path(str(src) + suffix)
        if s.exists():
            shutil.copy2(s, dst_path + suffix)
    logger.info("copied %s -> %s (with WAL sidecars)", src, dst_path)


def _clear_derived(store: Store, cases: list[CaseConfig]) -> None:
    for case in cases:
        for table in DERIVED_TABLES:
            store.conn.execute(f"DELETE FROM {table} WHERE case_id=?", (case.case_id,))
    store.conn.commit()


def _entries_for_replay(store: Store, docket_id: int) -> list[dict[str, Any]]:
    """Body-bearing entries on the docket, oldest-first, reconstructed in the
    CourtListener entry shape ``_handle_entry`` expects. Filter-failed stubs
    (description IS NULL) are excluded — they never reach the LLM anyway."""
    rows = store.conn.execute(
        """
        SELECT entry_id, entry_number, date_filed, date_modified,
               description, short_description, recap_documents
        FROM entries
        WHERE docket_id=? AND description IS NOT NULL
        ORDER BY date_modified
        """,
        (docket_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["recap_documents"] = json.loads(d.get("recap_documents") or "[]")
        d["id"] = d.pop("entry_id")
        out.append(d)
    return out


def _render_cfg(cfg: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """A cfg copy whose output paths point into ``out_dir`` and whose
    push-ids are stripped, so ``emit_calendars`` writes ICS + index there and
    pushes to NO real Google / M365 calendar."""
    rc = copy.deepcopy(cfg)
    rc.pop("google_credentials_path", None)
    rc.pop("m365_client_id", None)
    rc["index_path"] = str(out_dir / "index.html")
    for cal_id, cal_cfg in (rc.get("calendars") or {}).items():
        cal_cfg.pop("google_calendar_id", None)
        cal_cfg.pop("m365_calendar_id", None)
        cal_cfg.pop("m365_use_default_calendar", None)
        ics = cal_cfg.get("ics_path")
        cal_cfg["ics_path"] = (
            str(out_dir / Path(ics).name) if ics else str(out_dir / f"{cal_id}.ics")
        )
    return rc


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def _replay_case(syncer: CaseSyncer, store: Store, case: CaseConfig) -> None:
    stats: dict[str, int] = defaultdict(int)
    for docket_id in case.dockets:
        meta = store.get_docket_meta(docket_id) or {}
        court_id = meta.get("court_id")
        if not court_id:
            logger.warning("  docket %s has no court metadata — skipping", docket_id)
            continue
        tz = tz_for(court_id)
        entries = _entries_for_replay(store, docket_id)
        logger.info(
            "  docket %s (%s): %d body-bearing entries",
            docket_id,
            court_id,
            len(entries),
        )
        for entry in entries:
            try:
                syncer._handle_entry(case, docket_id, court_id, tz, entry, stats)
            except Exception:  # noqa: BLE001 — one bad entry shouldn't sink the case
                logger.exception(
                    "  _handle_entry failed docket=%s entry=%s",
                    docket_id,
                    entry.get("id"),
                )
        store.conn.commit()

    # End-of-case sweeps — same order as CaseSyncer.sync_case.
    syncer._verify_scheduled_hearings(case)
    syncer._dedupe_concurrent_hearings(case)
    syncer._dedupe_concurrent_held_hearings(case)
    if syncer.resolve_extract_deadlines(case):
        syncer._verify_pending_deadlines(case)
        syncer._auto_mark_passed_stale(case.case_id)
    store.conn.commit()


def build_for_provider(
    provider: str,
    src_path: str,
    cfg: dict[str, Any],
    cases: list[CaseConfig],
    raw_cases: dict[str, Any],
    cl: CourtListener,
) -> str:
    """Build the full store + rendered outputs for one provider. Thread-safe:
    provider selection is thread-local and each build writes its own store +
    subfolder, so this can run concurrently with other providers."""
    _TL.provider = provider  # extraction-track provider for THIS thread
    pdir = _provider_dir(provider)
    out_dir = pdir / "out"
    dst = str(pdir / "case-calendar.sqlite")
    for suffix in ("", "-wal", "-shm"):
        Path(dst + suffix).unlink(missing_ok=True)
    _copy_store(src_path, dst)

    store = Store(dst)
    syncer = CaseSyncer(cl, store)  # shared, response-cached cl

    logger.info("[%s] clearing derived tables for %d case(s)", provider, len(cases))
    _clear_derived(store, cases)

    for case in cases:
        logger.info("[%s] replaying case %s (%s)", provider, case.case_id, case.name)
        _replay_case(syncer, store, case)

    logger.info("[%s] generating case summaries", provider)
    summary.refresh_stale(
        cl=cl,
        store=store,
        cases=cases,
        case_overrides=raw_cases,
        force=True,
        provider=provider,
    )

    # Fold the WAL into the main file so the kept store is a single cp-able file.
    try:
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        store.conn.commit()
    except Exception:  # noqa: BLE001
        logger.warning("[%s] wal_checkpoint failed (sidecars left in place)", provider)

    # Render ICS + index into <provider>/out/ — push-ids stripped, so nothing
    # is written to a real Google / M365 calendar.
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        emit_calendars(_render_cfg(cfg, out_dir), store)
        logger.info("[%s] rendered ICS + index -> %s", provider, out_dir)
    except Exception:  # noqa: BLE001
        logger.exception("[%s] render failed", provider)

    closer = getattr(store, "close", None)
    if closer:
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass
    logger.info("[%s] done -> %s", provider, pdir)
    return dst


# ---------------------------------------------------------------------------
# Validation + report
# ---------------------------------------------------------------------------


def _store_counts(path: str) -> dict[str, int]:
    s = Store(path)
    out: dict[str, int] = {}
    try:
        for t in DERIVED_TABLES:
            out[t] = s.conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
        out["hearings_scheduled"] = s.conn.execute(
            "SELECT COUNT(*) AS c FROM hearings WHERE status='scheduled'"
        ).fetchone()["c"]
        out["hearings_held"] = s.conn.execute(
            "SELECT COUNT(*) AS c FROM hearings WHERE status='held'"
        ).fetchone()["c"]
    finally:
        closer = getattr(s, "close", None)
        if closer:
            closer()
    return out


def _fmt_usd(v: float) -> str:
    return f"${v:.4f}"


def build_report(
    providers_built: list[str],
    cfg: dict[str, Any],
    prod_path: str,
    cl: CourtListener,
    validate: bool,
    failed: Optional[list[str]] = None,
) -> str:
    L: list[str] = ["# Provider store build — cost + output comparison", ""]
    L.append(f"- providers built: {', '.join(providers_built)}")
    if failed:
        L.append(
            f"- ⚠️ providers that FAILED (store may be partial): {', '.join(failed)}"
        )
    for p in providers_built:
        L.append(
            f"- {p}: extraction={EXTRACT_MODELS[p]}, summary={SUMMARY_MODELS[p]}, "
            f"folder=`{_provider_dir(p)}/`"
        )
    L.append("")

    # --- CourtListener usage (made ONCE total, shared across all providers) ---
    L.append("## CourtListener API usage (total, shared across all providers)")
    L.append("")
    total = getattr(cl, "_request_total", CAP.cl_calls)
    times = getattr(cl, "_request_times", [])
    L.append(f"- total API calls to build **all** provider stores: **{total}**")
    L.append(
        f"- peak rate: **{courtlistener._peak_in_window(times, 60.0)}/min**, "
        f"**{courtlistener._peak_in_window(times, 3600.0)}/hour**, "
        f"**{courtlistener._peak_in_window(times, 86400.0)}/day**"
    )
    L.append(
        "- these are the one-time cost of warming the shared cache (cold dockets "
        "the summary pipeline falls back on); subsequent provider builds add zero. "
        "PDF file downloads from storage are separate and also cached once."
    )
    L.append("")

    # --- cost by provider x track ---
    agg: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {"calls": 0, "in": 0, "out": 0, "cost": 0.0, "unpriced": 0}
    )
    with _CAP_LOCK:
        snapshot = list(CAP.calls)
    for c in snapshot:
        a = agg[(c.provider, _track_for(c.purpose))]
        a["calls"] += 1
        a["in"] += c.tokens.input
        a["out"] += c.tokens.output
        if c.cost is None:
            a["unpriced"] += 1
        else:
            a["cost"] += c.cost
    L.append("## LLM cost by provider and track")
    L.append("")
    L.append("| provider | track | calls | input tok | output tok | est USD |")
    L.append("| --- | --- | ---: | ---: | ---: | ---: |")
    tracks = ["extraction", "verify", "summary"]
    totals: dict[str, float] = defaultdict(float)
    for p in providers_built:
        for t in tracks:
            a = agg.get((p, t))
            if not a:
                continue
            unp = f" (+{int(a['unpriced'])} unpriced)" if a["unpriced"] else ""
            L.append(
                f"| {p} | {t} | {int(a['calls'])} | {int(a['in']):,} | "
                f"{int(a['out']):,} | {_fmt_usd(a['cost'])}{unp} |"
            )
            totals[p] += a["cost"]
    L.append("")
    L.append("| provider | total build cost |")
    L.append("| --- | ---: |")
    for p in providers_built:
        L.append(f"| {p} | {_fmt_usd(totals[p])} |")
    L.append("")

    # --- output counts per store (+ prod baseline) ---
    L.append("## Output row counts per store")
    L.append("")
    cols = [
        "hearings",
        "hearings_scheduled",
        "hearings_held",
        "deadlines",
        "case_summaries",
    ]
    L.append("| store | " + " | ".join(cols) + " |")
    L.append("| --- |" + " ---: |" * len(cols))
    baseline = None
    if validate and Path(prod_path).exists():
        baseline = _store_counts(prod_path)
        L.append(
            "| **prod (current)** | "
            + " | ".join(str(baseline[c]) for c in cols)
            + " |"
        )
    for p in providers_built:
        try:
            counts = _store_counts(str(_provider_dir(p) / "case-calendar.sqlite"))
        except Exception as exc:  # noqa: BLE001
            L.append(f"| {p} | (count failed: {exc}) |")
            continue
        L.append(f"| {p} | " + " | ".join(str(counts[c]) for c in cols) + " |")
    L.append("")
    if baseline is not None and "anthropic" in providers_built:
        L.append(
            "> Fidelity check: the **anthropic** row should closely match **prod "
            "(current)** — prod was built by anthropic, so a faithful replay "
            "reproduces it. Large divergence means the replay isn't trustworthy yet."
        )
        L.append("")

    L.append("## Compare")
    L.append("")
    L.append("Open each provider's rendered index to compare summaries + calendars:")
    for p in providers_built:
        L.append(f"- {p}: `{_provider_dir(p)}/out/index.html`")
    L.append("")
    L.append(
        "Each provider's full sync log — including the per-entry extractor "
        "DECISION trace — is at `<provider>/build.log`:"
    )
    for p in providers_built:
        L.append(f"- {p}: `{_provider_dir(p)}/build.log`")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _has_key(provider: str) -> bool:
    if provider == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    if provider == "openai":
        return bool(os.environ.get("OPENAI_API_KEY"))
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument(
        "--providers",
        default=",".join(ALL_PROVIDERS),
        help="comma-separated subset; the FIRST builds serially (cache-warm + baseline), the rest in parallel",
    )
    ap.add_argument("--case", help="limit to one case id (pilot)")
    ap.add_argument(
        "--fake", action="store_true", help="synthetic tokens, no API calls, no spend"
    )
    ap.add_argument(
        "--no-parallel",
        action="store_true",
        help="build providers strictly one at a time",
    )
    ap.add_argument(
        "--validate",
        action="store_true",
        help="diff each store's row counts against current prod",
    )
    ap.add_argument(
        "--no-decisions",
        action="store_true",
        help="don't capture the per-entry extractor DECISION trace into each "
        "provider's build.log (the per-provider build.log itself is always written)",
    )
    ap.add_argument("--out", help="also write the markdown report to this path")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format=_LOG_FMT, stream=sys.stderr)
    # Keep the verbose per-entry DECISION lines off the interleaved console;
    # they go only to each provider's build.log (de-interleaved by provider).
    for _h in logging.getLogger().handlers:
        _h.addFilter(_DropDecisions())
    pp_log = _PerProviderLogHandler()
    pp_log.setFormatter(logging.Formatter(_LOG_FMT))
    logging.getLogger().addHandler(pp_log)

    providers_to_build = [p.strip() for p in args.providers.split(",") if p.strip()]
    for p in providers_to_build:
        if p not in ALL_PROVIDERS:
            raise SystemExit(f"unknown provider {p!r}; choose from {ALL_PROVIDERS}")
        if not args.fake and not _has_key(p):
            raise SystemExit(
                f"missing API key for {p!r} (set it in .env or use --fake)"
            )

    cfg = _load_config(args.config)
    cases = _cases_from_config(cfg)
    raw_cases = {c["id"]: c for c in cfg["cases"]}
    if args.case:
        cases = [c for c in cases if c.case_id == args.case]
        if not cases:
            raise SystemExit(f"no case with id {args.case!r}")
    src_path = cfg.get("store_path", "data/case-calendar.sqlite")

    # Patch telemetry + provider detection. In --fake mode also short-circuit
    # the dispatch so no provider SDK is ever called.
    usage.set_price_estimator(costs.estimate_cost)
    usage.record = _capturing_record  # type: ignore[assignment]
    providers._detect_provider = _tl_detect  # type: ignore[assignment]
    orig_dispatch = providers._dispatch_llm_call
    if args.fake:
        providers._dispatch_llm_call = _fake_dispatch  # type: ignore[assignment]
    # Wrap the extractor-track LLM entry points to log a per-entry DECISION
    # trace into each provider's build.log. Patching the module attribute is
    # caught by every call site (sync's ``llm_mod.verify_hearing`` etc. bind the
    # same module object and resolve the attribute at call time).
    saved_llm: dict[str, Any] = {}
    if not args.no_decisions:
        for _name, _kind in _DECISION_WRAPS.items():
            saved_llm[_name] = getattr(llm, _name)
            setattr(llm, _name, _wrap_llm(_name, _kind))
    _install_pdf_cache()
    # Pin to provider defaults: clear any model overrides for the run.
    saved_models = {
        k: os.environ.pop(k, None) for k in ("LLM_MODEL", "LLM_SUMMARY_MODEL")
    }

    # One CourtListener client, response-cached and shared across every build.
    cl = CourtListener()
    _install_cl_cache(cl)

    failed: list[str] = []

    def _build(p: str) -> str:
        _TL.provider = p  # so this thread's logs route to <p>/build.log from here on
        logger.info("==================== building %s ====================", p)
        return build_for_provider(p, src_path, cfg, cases, raw_cases, cl)

    def _safe_build(p: str) -> None:
        # One provider's failure must not abort the others or the final report.
        try:
            _build(p)
        except Exception:  # noqa: BLE001
            logger.exception("provider %s build FAILED; continuing with the rest", p)
            failed.append(p)

    try:
        # All providers concurrently. The shared CourtListener / PDF caches are
        # thread-safe and fetch-under-lock, so whichever thread reaches a given
        # request first makes the one network call and the others get the cached
        # response — CourtListener (and PDF) fetches happen once total no matter
        # how many providers race. LLM calls run outside the lock, in parallel.
        if len(providers_to_build) > 1 and not args.no_parallel:
            with ThreadPoolExecutor(max_workers=len(providers_to_build)) as ex:
                list(ex.map(_safe_build, providers_to_build))
        else:
            for p in providers_to_build:
                _safe_build(p)
    finally:
        providers._dispatch_llm_call = orig_dispatch  # type: ignore[assignment]
        providers._detect_provider = _REAL_DETECT  # type: ignore[assignment]
        usage.record = _ORIG_RECORD  # type: ignore[assignment]
        pdf.extract_text = _ORIG_PDF_EXTRACT  # type: ignore[assignment]
        for _name, _fn in saved_llm.items():
            setattr(llm, _name, _fn)
        for k, v in saved_models.items():
            if v is not None:
                os.environ[k] = v
        try:
            cl.log_request_stats()
        except Exception:  # noqa: BLE001
            pass
        cl_close = getattr(cl, "close", None)
        if cl_close:
            try:
                cl_close()
            except Exception:  # noqa: BLE001
                pass

    report = build_report(
        providers_to_build, cfg, src_path, cl, args.validate, failed=failed
    )
    print(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report + "\n", encoding="utf-8")
        logger.info("wrote %s", args.out)
    pp_log.close()  # flush + close each provider's build.log
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
