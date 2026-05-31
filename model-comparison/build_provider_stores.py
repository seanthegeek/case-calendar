#!/usr/bin/env python3
"""Build one full store + rendered outputs per model-variant column, for head-to-head comparison.

A comparison COLUMN is a ``(provider, extraction-model, summary-model)`` combo —
by default one per provider at its out-of-the-box models, plus extra columns that
vary the extraction model WITHIN a provider (the open question this run answers).
A/B/C the columns on your real cases, eyeball each one's rendered calendars +
summaries, then push the best store to prod. Each column re-derives the
LLM-produced tables (hearings, deadlines, case summaries) from scratch against an
identical copy of the warm store. The CourtListener-fetched facts — the
``entries`` rows, their cached ``recap_documents`` (with ``plain_text``), and
docket / court metadata — are SHARED and left untouched.

Constraints honored:

  * **CourtListener is hit at most once, total — never once per column.** A
    shared response cache (CourtListener ``_get``/``_post`` + ``pdf.extract_text``
    by document id) is populated by the first build and reused by the rest. The
    CourtListener-derived inputs are identical across columns (only the LLM
    differs), so caching them is exact, not an approximation. This holds under
    ``--no-parallel`` too: the cache is module-level and the ``CourtListener``
    client is created once and shared, so a serial run reuses both exactly as a
    parallel one does — the cache spans all columns regardless of mode. And
    because a cache HIT returns before the wrapped ``_get``/``_post`` reaches the
    client's ``_request`` (the only place request stats are recorded), the
    reported total CourtListener API calls AND the peak per-minute / hour / day
    rate count genuine network calls only — a re-fetch never inflates them. (The
    one mode difference: under ``--no-parallel`` the genuine fetches all land
    during the first column's run rather than racing at the start, so the peak
    rate reads a little lower — same total, just spread out.)

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

Layout — each comparison column is one model variant, nested under its provider
(``<provider>/<extraction-model>/``), so columns on the same provider sit side by
side:

    data/provider-stores/<provider>/<extraction-model>/   # gitignored
        case-calendar.sqlite        # the candidate store
        build.log                    # this column's full sync log + DECISION trace
        out/                         # rendered ICS + index.html (NO push to gcal/M365)
            <calendar>.ics ...
            index.html

    e.g. data/provider-stores/gemini/gemini-3.1-flash-lite/   (the gemini default)
         data/provider-stores/gemini/gemini-3.5-flash/        (an eval candidate)

The committed comparison artifact is the events CSV
``model-comparison/export_model_events.py`` produces from these, not the stores
themselves (which are large, and whose rendered calendars would make the blind
ground-truth scoring peekable).

For each column C (folder ``<provider>/<extraction-model>``):
  1. Copy the warm source store -> data/provider-stores/<C>/case-calendar.sqlite
  2. Clear the LLM-derived tables (hearings / deadlines / case_summaries) for the
     cases in scope. The entries table is NOT touched.
  3. Pin the column's provider + extraction model and replay the REAL pipeline
     against the cached entries: ``CaseSyncer._handle_entry`` per body-bearing
     entry, then the end-of-sync verify / dedupe sweeps, then
     ``summary.refresh_stale(force)`` pinned to the column's summary model.
  4. Render ICS + index.html into <C>/out/ (push-ids stripped, so nothing goes
     to a real Google / M365 calendar). Keep everything.

Prove the replay is faithful with ``--validate``: the gemini default column's
row counts should match your current prod store (which that column produced) —
once that holds, the other columns are trustworthy.

Push the winner yourself (stop ``serve`` first, back up the live store):

    cp data/provider-stores/<C>/case-calendar.sqlite data/case-calendar.sqlite

Usage:

    # no API calls, no spend — validate the replay plumbing on one case:
    uv run python model-comparison/build_provider_stores.py --fake --case <case_id>

    # build the gemini store for one case and check it against prod:
    uv run python model-comparison/build_provider_stores.py --variants gemini --case <case_id> --validate

    # full build, write the cost report:
    uv run python model-comparison/build_provider_stores.py --validate --out model-comparison/cost.md
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import threading
import time
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


@dataclass(frozen=True)
class Variant:
    """One comparison column: a ``(provider, extraction-model, summary-model)``
    combination.

    The default set is the three providers at their out-of-the-box models, one
    column each. EXTRA variants compare a different model choice WITHIN a
    provider — e.g. a second Gemini extraction model against the Gemini default —
    so the unit of comparison is the model config, not the provider. ``provider``
    is the SDK provider it dispatches to (and the one whose API key must be
    present); the two ``*_model`` fields pin the extraction and summary models so
    the run is reproducible regardless of any ``LLM_MODEL`` override.

    The column's identity is its :attr:`label` — ``provider/extraction-model`` —
    which is also the nested folder under ``data/provider-stores/`` (``Path``
    division splits on the slash, so ``gemini/gemini-3.5-flash`` lands in
    ``…/gemini/gemini-3.5-flash/``), the ``provider`` column in the events CSV,
    the cost bucket, and the build.log routing key. Keying on the extraction
    model means two columns can't collide unless they share both provider AND
    extraction model.
    """

    provider: str
    extract_model: str
    summary_model: str

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.extract_model}"


def _default_variants() -> list[Variant]:
    """The committed comparison set: one column per provider at its default
    models, plus the extraction-model evaluation candidates.

    The extra column varies ONLY the extraction model (same summary model as
    its provider's default column), because the open question is which
    extraction model each provider should default to. It is an evaluation
    candidate: if it wins it becomes that provider's default in
    ``providers._DEFAULT_MODELS`` (and its default column then IS that model,
    so the extra column is dropped); if it loses, the extra column is
    removed.

    The Gemini extraction candidate ``gemini-3.5-flash`` was dropped from the
    default set due to long processing times — its single-column rebuild rate
    (~14 LLM calls/min) projects to roughly an extra 100 minutes of wall-clock
    per run, and the existing gemini default ``gemini-3.1-flash-lite`` is the
    overall comparison leader, so the throughput cost of carrying it across
    re-runs isn't justified. Pass it explicitly via ``--extra-variant
    gemini:gemini-3.5-flash`` to add it back for a one-off run.
    """
    out = [
        Variant(p, providers._DEFAULT_MODELS[p], llm._DEFAULT_SUMMARY_MODELS[p])
        for p in ALL_PROVIDERS
    ]
    out.append(
        # OpenAI extraction candidate: gpt-5.4-mini vs the openai default
        # (gpt-5.4-nano). Summary stays on the openai default tier.
        Variant("openai", "gpt-5.4-mini", llm._DEFAULT_SUMMARY_MODELS["openai"])
    )
    return out


VARIANTS = _default_variants()


def _parse_extra_variant(spec: str) -> Variant:
    """Parse a ``--extra-variant`` spec ``provider:extract[:summary]``.

    The summary model is optional; when omitted it defaults to ``provider``'s
    summary default, matching how the built-in evaluation candidates are
    defined. Raises ``SystemExit`` on a malformed spec or unknown provider so a
    typo fails at the command line, not three minutes into a paid build.
    """
    parts = spec.split(":")
    if len(parts) not in (2, 3):
        raise SystemExit(f"--extra-variant {spec!r} must be provider:extract[:summary]")
    provider, extract = parts[0], parts[1]
    if provider not in ALL_PROVIDERS:
        raise SystemExit(
            f"--extra-variant {spec!r}: unknown provider {provider!r}; "
            f"choose from {ALL_PROVIDERS}"
        )
    summary = parts[2] if len(parts) == 3 else llm._DEFAULT_SUMMARY_MODELS[provider]
    if not (extract and summary):
        raise SystemExit(f"--extra-variant {spec!r}: empty field")
    return Variant(provider, extract, summary)


# Variant selection is thread-local so the parallel builds don't race on a
# global. ``providers._detect_provider`` (used by the extraction-track
# functions) is patched to read ``_TL.provider``; ``_TL.extract_model`` is
# injected into each extraction/verify/dedupe dispatch by ``_variant_dispatch``
# so two variants on the SAME provider but different extraction models don't
# collide; ``_TL.label`` routes logs + cost capture to the right column. The
# summary track is told its provider + model explicitly via ``refresh_stale``.
_TL = threading.local()
_REAL_DETECT = providers._detect_provider


def _tl_detect() -> Optional[str]:
    return getattr(_TL, "provider", None) or _REAL_DETECT()


# Operator env vars the comparison run must neutralize so each column's
# (provider, model) pinning is authoritative. The MODEL overrides would force
# every column onto one model; the PROVIDER overrides short-circuit the
# thread-local ``_detect_provider`` patch above — the extraction track checks
# ``LLM_EXTRACTION_PROVIDER`` first and the summary track ``LLM_SUMMARY_PROVIDER``
# first, so leaving them set routes every column to one provider while still
# sending that column's own model. An operator running the recommended split
# (``LLM_EXTRACTION_PROVIDER=gemini`` in ``.env``, which ``uv run`` loads) then
# 404s every non-Gemini column on its own model. Popped at run start, restored
# in the finally block.
_NEUTRALIZED_RUN_ENV = (
    "LLM_MODEL",
    "LLM_SUMMARY_MODEL",
    "LLM_PROVIDER",
    "LLM_EXTRACTION_PROVIDER",
    "LLM_SUMMARY_PROVIDER",
)


def _tl_label() -> Optional[str]:
    """The current thread's comparison-column label (``provider/extract-model``),
    falling back to the SDK provider when only that is set (keeps the log handler
    usable in unit tests that set just ``_TL.provider``)."""
    return getattr(_TL, "label", None) or getattr(_TL, "provider", None)


# ---------------------------------------------------------------------------
# Cost capture (one row per LLM call, bucketed by column / docket / track)
# ---------------------------------------------------------------------------


@dataclass
class _Call:
    label: str  # the comparison column (provider/extract-model) this call is in
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


@dataclass
class _Timing:
    """Per-column timing (thread-safe; columns build in parallel). ``wall`` is
    each column's wall-clock seconds; ``call_secs`` is ``[n_calls,
    total_seconds]`` of model-dispatch time, so mean s/call is a contention-light
    latency proxy (it times just the model call, not the PDF / CourtListener cache
    waits that inflate wall-clock)."""

    wall: dict[str, float] = field(default_factory=dict)
    call_secs: dict[str, list[float]] = field(default_factory=dict)


TIMING = _Timing()
_TIMING_LOCK = threading.Lock()

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
    """Wrap ``usage.record``: capture the call for our per-column report, then
    delegate to the real recorder so the normal ``llm-tokens`` log lines (with
    ``cost_est``) still print for live monitoring. Thread-safe.

    Bucketing is by the thread-local LABEL (variant name), not ``provider`` — two
    variants on the same provider (e.g. the gemini default and the
    gemini-3.5-flash candidate) record the same ``provider`` but must stay in
    separate cost columns."""
    call = _Call(
        label=_tl_label() or provider,
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
    temperature: Optional[float] = None,
) -> str:
    """Stand-in for ``_dispatch_llm_call`` in --fake mode: synthetic token
    counts proportional to prompt length, no API call, no spend. ``provider``
    is the resolved provider the caller passed (thread-local for extraction,
    explicit for summaries), so the synthetic call is tagged correctly.

    ``temperature`` is accepted and ignored — the fake doesn't sample, so
    the value is irrelevant. The signature must match the real dispatch
    so callers passing ``temperature=0.0`` don't trip on an unexpected
    kwarg under ``--fake``.
    """
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


def _make_variant_dispatch(base: Any) -> Any:
    """Wrap a dispatch function so the extraction track uses the current
    variant's extraction model.

    The extraction / verify / dedupe entry points in ``case_calendar.llm`` call
    dispatch with no ``model=`` (they let it default), so two variants on the
    same provider would otherwise both run that provider's default model. We
    inject the thread-local ``extract_model`` for every non-summary purpose so
    each column runs the model it's meant to. The summary track passes its model
    explicitly (from ``refresh_stale(model=...)``), so we leave ``purpose ==
    "summary"`` calls untouched. ``base`` is the real ``_dispatch_llm_call`` (or
    ``_fake_dispatch`` in --fake mode)."""

    def wrapped(
        provider: str,
        system: str,
        user: str,
        max_tokens: int,
        *,
        model: Optional[str] = None,
        json_mode: bool = True,
        purpose: str = "llm",
        docket: Any = None,
        temperature: Optional[float] = None,
    ) -> str:
        if model is None and purpose != "summary":
            model = getattr(_TL, "extract_model", None)
        label = _tl_label() or provider
        t0 = time.monotonic()
        try:
            return base(
                provider,
                system,
                user,
                max_tokens,
                model=model,
                json_mode=json_mode,
                purpose=purpose,
                docket=docket,
                temperature=temperature,
            )
        finally:
            dt = time.monotonic() - t0
            with _TIMING_LOCK:
                acc = TIMING.call_secs.setdefault(label, [0.0, 0.0])
                acc[0] += 1.0
                acc[1] += dt

    return wrapped


# ---------------------------------------------------------------------------
# Per-provider log + decision capture
# ---------------------------------------------------------------------------
#
# The builds run concurrently and all share one stderr stream, so the console
# is an interleaved mix of every provider's lines. To make each provider's run
# readable after the fact, ``_PerProviderLogHandler`` routes every log record
# to ``<provider>/build.log`` based on the emitting thread's thread-local
# provider (set at the top of ``build_for_variant``). On top of that, the
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
    """Write each log record to ``<label>/build.log`` based on the emitting
    thread's thread-local column label (the variant name). Records emitted
    outside a build (no label/provider set — e.g. the main thread during report
    assembly) are ignored here; they still reach the stderr handler. Thread-safe:
    each column gets its own stream, opened once under a lock."""

    def __init__(self) -> None:
        super().__init__()
        self._streams: dict[str, Any] = {}
        self._slock = threading.Lock()

    def _stream_for(self, label: str) -> Any:
        with self._slock:
            s = self._streams.get(label)
            if s is None:
                path = _provider_dir(label) / "build.log"
                path.parent.mkdir(parents=True, exist_ok=True)
                s = path.open("w", encoding="utf-8")
                self._streams[label] = s
            return s

    def emit(self, record: logging.LogRecord) -> None:
        label = _tl_label()
        if not label:
            return
        try:
            stream = self._stream_for(label)
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
# Persistent LLM-response cache (the developer-iteration lever)
# ---------------------------------------------------------------------------


class _LLMCache:
    """Persistent, content-addressed cache for LLM dispatch responses.

    The whole pipeline runs at ``temperature=0`` (every domain call in
    ``case_calendar.llm`` pins it), so a call's output is a *pure function*
    of its request. Keying on the FULL resolved request — provider, model,
    system prompt, user message, ``max_tokens``, ``json_mode``,
    ``temperature`` — means a cached response is byte-identical to what a
    fresh call would return. There's no separate version tag because the
    request IS the version: change the model and the key changes; change a
    word of any prompt and the key changes; both miss naturally and re-run
    live, while every UNCHANGED call replays for free.

    Why it exists: the CourtListener / PDF caches above already make a
    rebuild's data layer free, but the LLM calls — the entire dollar cost of
    a build — were re-paid in full on every run. That made each prompt tweak
    a full-caseload spend even when only one track's prompt changed. With
    this cache persisted to a SQLite sidecar across runs, a second build
    after a single-track prompt edit re-bills ONLY that track's calls (their
    requests changed); the other tracks' calls hit the cache. A summary-only
    tweak drops from a full rebuild to just the summary track, automatically,
    with no flag — the request hashes do the scoping.

    Determinism caveat: this inherits exactly the same near-determinism the
    project already relies on for its temperature=0 verify-pass validation.
    A cached response equals what the model would return for an identical
    request *within a model version*; a provider-side model/infra update is
    the same epsilon the rest of the harness already accepts. Pass
    ``--no-llm-cache`` for a guaranteed-fresh build.

    Thread-safety: columns build in parallel. All cache reads/writes are
    serialized under ``self._lock``; the real model call on a miss happens
    OUTSIDE the lock (it's slow, and distinct columns use distinct models →
    distinct keys, so two threads computing the same key essentially never
    happens). Errors are never cached — an exception from the wrapped call
    propagates without a store, so a transient failure can't poison the
    cache.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False is safe because every access is under _lock.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS llm_responses ("
            "key TEXT PRIMARY KEY, response TEXT NOT NULL, "
            "provider TEXT, model TEXT, purpose TEXT)"
        )
        self._conn.commit()
        self.hits: dict[str, int] = {}
        self.misses: dict[str, int] = {}

    @staticmethod
    def _effective_model(provider: str, model: Optional[str]) -> Optional[str]:
        """Resolve the model the actual request will carry, mirroring the
        per-provider call functions' ``model or os.environ['LLM_MODEL'] or
        default`` resolution EXACTLY.

        This is the soundness pivot of the whole cache: the key must capture
        what is *actually sent to the provider*, not the dispatch argument.
        When the caller passes ``model=None``, the real request's model comes
        from ``LLM_MODEL`` (or the provider default) — so two ``model=None``
        calls under different ``LLM_MODEL`` values build DIFFERENT requests and
        must NOT share a cache entry. Keying on the unresolved arg would let
        them collide. (In this harness ``model`` is always concrete at the
        cache boundary and ``LLM_MODEL`` is popped from the env, so the gap is
        closed by construction today — but the cache must not silently depend
        on that invariant.)"""
        return model or os.environ.get(
            "LLM_MODEL", providers._DEFAULT_MODELS.get(provider)
        )

    @staticmethod
    def _key(
        provider: str,
        model: Optional[str],
        system: str,
        user: str,
        max_tokens: int,
        json_mode: bool,
        temperature: Optional[float],
    ) -> str:
        payload = json.dumps(
            [provider, model, system, user, max_tokens, json_mode, temperature],
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def wrap(self, base: Any) -> Any:
        """Return a dispatch function with ``base``'s signature that serves
        identical requests from the persistent cache and falls through to
        ``base`` on a miss. ``base`` is the real ``_dispatch_llm_call`` (the
        cache is never installed in ``--fake`` mode — synthetic calls are
        already free)."""

        def wrapped(
            provider: str,
            system: str,
            user: str,
            max_tokens: int,
            *,
            model: Optional[str] = None,
            json_mode: bool = True,
            purpose: str = "llm",
            docket: Any = None,
            temperature: Optional[float] = None,
        ) -> str:
            # Key on the RESOLVED model the request will actually carry, not
            # the (possibly None) dispatch arg — see _effective_model.
            eff_model = self._effective_model(provider, model)
            key = self._key(
                provider, eff_model, system, user, max_tokens, json_mode, temperature
            )
            label = _tl_label() or provider
            with self._lock:
                row = self._conn.execute(
                    "SELECT response FROM llm_responses WHERE key=?", (key,)
                ).fetchone()
                if row is not None:
                    self.hits[label] = self.hits.get(label, 0) + 1
                    return row[0]
            # MISS — the real model call (and its usage.record cost capture)
            # happens here, OUTSIDE the lock. Only successful returns are
            # stored; an exception propagates with nothing written.
            resp = base(
                provider,
                system,
                user,
                max_tokens,
                model=model,
                json_mode=json_mode,
                purpose=purpose,
                docket=docket,
                temperature=temperature,
            )
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO llm_responses"
                    "(key, response, provider, model, purpose) VALUES(?,?,?,?,?)",
                    (key, resp, provider, eff_model, purpose),
                )
                self._conn.commit()
                self.misses[label] = self.misses.get(label, 0) + 1
            return resp

        return wrapped

    def log_summary(self) -> None:
        """Log per-column and total hit/miss counts. The cost report's TOTAL
        already reflects only the misses (a hit skips ``usage.record``), so
        these counts explain *why* a re-run was cheap."""
        with self._lock:
            labels = sorted(set(self.hits) | set(self.misses))
            for label in labels:
                h = self.hits.get(label, 0)
                m = self.misses.get(label, 0)
                logger.info(
                    "llm-cache [%s] hits=%d misses=%d "
                    "(cost reflects the %d live calls only)",
                    label,
                    h,
                    m,
                    m,
                )
            if labels:
                logger.info(
                    "llm-cache TOTAL hits=%d misses=%d",
                    sum(self.hits.values()),
                    sum(self.misses.values()),
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


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

    # End-of-case sweeps — same order as CaseSyncer.sync_case. MUST stay in
    # sync with that method's sweep sequence: a sweep added there but not here
    # silently doesn't run in the comparison build (the near-slot dedup was
    # missed exactly this way).
    syncer._verify_scheduled_hearings(case)
    syncer._dedupe_concurrent_hearings(case)
    syncer._dedupe_concurrent_held_hearings(case)
    syncer._dedupe_nearslot_hearings(case)
    syncer._verify_pending_deadlines(case)
    syncer._auto_mark_passed_stale(case.case_id)
    store.conn.commit()


def build_for_variant(
    variant: Variant,
    src_path: str,
    cfg: dict[str, Any],
    cases: list[CaseConfig],
    raw_cases: dict[str, Any],
    cl: CourtListener,
    *,
    skip_summaries: bool = False,
) -> str:
    """Build the full store + rendered outputs for one comparison column.
    Thread-safe: variant selection is thread-local (provider for detection,
    extraction model for dispatch injection, label for log/cost routing) and
    each build writes its own store + subfolder, so this runs concurrently with
    other columns — including a sibling column on the same provider."""
    _TL.provider = variant.provider  # extraction-track provider for THIS thread
    _TL.extract_model = variant.extract_model  # injected by _variant_dispatch
    _TL.label = variant.label  # routes logs + cost capture to this column
    name = variant.label  # provider/extract-model — nests under the provider dir
    started = time.monotonic()
    pdir = _provider_dir(name)
    out_dir = pdir / "out"
    dst = str(pdir / "case-calendar.sqlite")
    for suffix in ("", "-wal", "-shm"):
        Path(dst + suffix).unlink(missing_ok=True)
    _copy_store(src_path, dst)

    store = Store(dst)
    syncer = CaseSyncer(cl, store)  # shared, response-cached cl

    logger.info(
        "[%s] clearing derived tables for %d case(s) (extract=%s, summary=%s)",
        name,
        len(cases),
        variant.extract_model,
        variant.summary_model,
    )
    _clear_derived(store, cases)

    for case in cases:
        logger.info("[%s] replaying case %s (%s)", name, case.case_id, case.name)
        _replay_case(syncer, store, case)

    if skip_summaries:
        logger.info(
            "[%s] skipping case summaries (--skip-summaries set); the per-column "
            "store will have 0 case_summaries rows and the rendered out/ pages "
            "will show no per-docket summary blocks — this is intentional and "
            "score.py only reads hearings + deadlines, so the comparison is still "
            "complete on the calendar-events question",
            name,
        )
    else:
        logger.info("[%s] generating case summaries", name)
        summary.refresh_stale(
            cl=cl,
            store=store,
            cases=cases,
            case_overrides=raw_cases,
            force=True,
            provider=variant.provider,
            model=variant.summary_model,
        )

    # Fold the WAL into the main file so the kept store is a single cp-able file.
    try:
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        store.conn.commit()
    except Exception:  # noqa: BLE001
        logger.warning("[%s] wal_checkpoint failed (sidecars left in place)", name)

    # Render ICS + index into <name>/out/ — push-ids stripped, so nothing
    # is written to a real Google / M365 calendar.
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        emit_calendars(_render_cfg(cfg, out_dir), store)
        logger.info("[%s] rendered ICS + index -> %s", name, out_dir)
    except Exception:  # noqa: BLE001
        logger.exception("[%s] render failed", name)

    closer = getattr(store, "close", None)
    if closer:
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass
    with _TIMING_LOCK:
        TIMING.wall[name] = time.monotonic() - started
    logger.info("[%s] done -> %s", name, pdir)
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


def _timing_rows(
    names: list[str],
    wall: dict[str, float],
    call_secs: dict[str, list[float]],
) -> list[str]:
    """Markdown table rows for per-column timing. ``wall`` maps column label ->
    wall-clock seconds; ``call_secs`` maps label -> ``[n_calls, total_seconds]``.
    A column with no recorded timing (e.g. one that failed) shows ``—``."""
    rows = [
        "| column | wall-clock | LLM calls | mean s/call |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name in names:
        w = wall.get(name)
        wtxt = f"{w / 60:.1f} m" if w is not None else "—"
        cs = call_secs.get(name)
        if cs and cs[0]:
            calls_txt, spc = str(int(cs[0])), f"{cs[1] / cs[0]:.1f}"
        else:
            calls_txt, spc = "—", "—"
        rows.append(f"| {name} | {wtxt} | {calls_txt} | {spc} |")
    return rows


def build_report(
    variants_built: list[Variant],
    cfg: dict[str, Any],
    prod_path: str,
    cl: CourtListener,
    validate: bool,
    failed: Optional[list[str]] = None,
) -> str:
    names = [v.label for v in variants_built]
    L: list[str] = ["# Provider store build — cost + output comparison", ""]
    L.append(f"- columns built: {', '.join(names)}")
    if failed:
        L.append(f"- ⚠️ columns that FAILED (store may be partial): {', '.join(failed)}")
    for v in variants_built:
        L.append(
            f"- {v.label}: provider={v.provider}, extraction={v.extract_model}, "
            f"summary={v.summary_model}, folder=`{_provider_dir(v.label)}/`"
        )
    L.append("")

    # --- CourtListener usage (made ONCE total, shared across all columns) ---
    L.append("## CourtListener API usage (total, shared across all columns)")
    L.append("")
    total = getattr(cl, "_request_total", CAP.cl_calls)
    times = getattr(cl, "_request_times", [])
    L.append(f"- total API calls to build **all** stores: **{total}**")
    L.append(
        f"- peak rate: **{courtlistener._peak_in_window(times, 60.0)}/min**, "
        f"**{courtlistener._peak_in_window(times, 3600.0)}/hour**, "
        f"**{courtlistener._peak_in_window(times, 86400.0)}/day**"
    )
    L.append(
        "- these are the one-time cost of warming the shared cache (cold dockets "
        "the summary pipeline falls back on); subsequent column builds add zero. "
        "PDF file downloads from storage are separate and also cached once."
    )
    L.append("")

    # --- cost by column x track ---
    # Bucket by the column LABEL (variant name), not provider: two columns on
    # the same provider (e.g. gemini vs gemini-3.5-flash) must stay separate.
    agg: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {"calls": 0, "in": 0, "out": 0, "cost": 0.0, "unpriced": 0}
    )
    with _CAP_LOCK:
        snapshot = list(CAP.calls)
    for c in snapshot:
        a = agg[(c.label, _track_for(c.purpose))]
        a["calls"] += 1
        a["in"] += c.tokens.input
        a["out"] += c.tokens.output
        if c.cost is None:
            a["unpriced"] += 1
        else:
            a["cost"] += c.cost
    L.append("## LLM cost by column and track")
    L.append("")
    L.append("| column | track | calls | input tok | output tok | est USD |")
    L.append("| --- | --- | ---: | ---: | ---: | ---: |")
    tracks = ["extraction", "verify", "summary"]
    totals: dict[str, float] = defaultdict(float)
    for name in names:
        for t in tracks:
            a = agg.get((name, t))
            if not a:
                continue
            unp = f" (+{int(a['unpriced'])} unpriced)" if a["unpriced"] else ""
            L.append(
                f"| {name} | {t} | {int(a['calls'])} | {int(a['in']):,} | "
                f"{int(a['out']):,} | {_fmt_usd(a['cost'])}{unp} |"
            )
            totals[name] += a["cost"]
    L.append("")
    L.append("| column | total build cost |")
    L.append("| --- | ---: |")
    for name in names:
        L.append(f"| {name} | {_fmt_usd(totals[name])} |")
    L.append("")

    # --- build time per column ---
    L.append("## Build time per column")
    L.append("")
    L.append(
        "Wall-clock per column, and mean latency per LLM call. NOTE: columns build "
        "in PARALLEL (unless `--no-parallel`), so wall-clock includes contention "
        "with the other columns and the shared PDF / CourtListener cache locks — "
        "read it as a relative signal, not isolated model speed. Mean s/call (which "
        "times just the model dispatch) is the cleaner latency proxy."
    )
    L.append("")
    with _TIMING_LOCK:
        wall = dict(TIMING.wall)
        call_secs = {k: list(v) for k, v in TIMING.call_secs.items()}
    L += _timing_rows(names, wall, call_secs)
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
    for name in names:
        try:
            counts = _store_counts(str(_provider_dir(name) / "case-calendar.sqlite"))
        except Exception as exc:  # noqa: BLE001
            L.append(f"| {name} | (count failed: {exc}) |")
            continue
        L.append(f"| {name} | " + " | ".join(str(counts[c]) for c in cols) + " |")
    L.append("")
    # prod was built by anthropic at its default extraction model, so that column
    # is the one whose counts should reproduce prod.
    anthropic_default = f"anthropic/{providers._DEFAULT_MODELS['anthropic']}"
    if baseline is not None and anthropic_default in names:
        L.append(
            f"> Fidelity check: the **{anthropic_default}** row should closely "
            "match **prod (current)** — prod was built by that column, so a "
            "faithful replay reproduces it. Large divergence means the replay "
            "isn't trustworthy yet."
        )
        L.append("")

    L.append("## Compare")
    L.append("")
    L.append("Open each column's rendered index to compare summaries + calendars:")
    for name in names:
        L.append(f"- {name}: `{_provider_dir(name)}/out/index.html`")
    L.append("")
    L.append(
        "Each column's full sync log — including the per-entry extractor "
        "DECISION trace — is at `<column>/build.log`:"
    )
    for name in names:
        L.append(f"- {name}: `{_provider_dir(name)}/build.log`")
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
        "--variants",
        default=None,
        help="comma-separated subset to build; match a column by its full label "
        "(provider/extract-model), its extraction model alone, or a bare "
        "provider name (which selects every column on that provider). "
        f"default all: {', '.join(v.label for v in VARIANTS)}",
    )
    ap.add_argument(
        "--providers",
        dest="variants",
        help="deprecated alias for --variants (a bare provider name selects "
        "every column on that provider)",
    )
    ap.add_argument(
        "--extra-variant",
        action="append",
        default=[],
        metavar="provider:extract[:summary]",
        help="add an ad-hoc comparison column; repeatable. summary model "
        "defaults to the provider's summary default when omitted",
    )
    ap.add_argument("--case", help="limit to one case id (pilot)")
    ap.add_argument(
        "--fake", action="store_true", help="synthetic tokens, no API calls, no spend"
    )
    ap.add_argument(
        "--no-parallel",
        action="store_true",
        help="build columns strictly one at a time",
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
        "column's build.log (the per-column build.log itself is always written)",
    )
    ap.add_argument(
        "--skip-summaries",
        action="store_true",
        help="skip the summary.refresh_stale phase per column. Useful when "
        "iterating on extractor-prompt changes: the comparison's score.py "
        "only reads hearings + deadlines, so summaries aren't needed to "
        "rank columns, and skipping them saves the higher-tier model spend "
        "(typically the biggest cost line per column).",
    )
    ap.add_argument(
        "--no-llm-cache",
        action="store_true",
        help="disable the persistent content-addressed LLM-response cache. By "
        "default, identical requests (same provider/model/prompts/temperature) "
        "are served from a SQLite sidecar across runs, so a prompt tweak only "
        "re-bills the track whose prompt changed; everything unchanged replays "
        "for free. Pass this for a guaranteed-fresh build (no replay). Ignored "
        "under --fake (synthetic calls are already free).",
    )
    ap.add_argument(
        "--llm-cache-path",
        default="data/llm-cache.sqlite",
        help="path to the persistent LLM-response cache (default: "
        "data/llm-cache.sqlite, which is gitignored). Delete the file to "
        "invalidate every cached response.",
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

    # Resolve the comparison columns: the built-in set plus any --extra-variant,
    # then filter to the --variants subset (default all). Labels
    # (provider/extract-model) must be unique so two columns can't fight over one
    # folder / build.log / cost bucket.
    by_label: dict[str, Variant] = {v.label: v for v in VARIANTS}
    for spec in args.extra_variant:
        v = _parse_extra_variant(spec)
        if v.label in by_label:
            raise SystemExit(
                f"--extra-variant {v.label!r} duplicates an existing column"
            )
        by_label[v.label] = v
    all_variants = list(by_label.values())
    if args.variants:
        wanted = [n.strip() for n in args.variants.split(",") if n.strip()]
        variants_to_build = []
        for token in wanted:
            # Match a token against the full label, the extraction model alone,
            # or a bare provider (which selects every column on that provider).
            matched = [
                v
                for v in all_variants
                if token in (v.label, v.extract_model, v.provider)
            ]
            if not matched:
                raise SystemExit(
                    f"unknown column {token!r}; choose from {sorted(by_label)} "
                    "(or a bare provider / extraction-model name)"
                )
            for v in matched:
                if v not in variants_to_build:
                    variants_to_build.append(v)
    else:
        variants_to_build = all_variants

    for v in variants_to_build:
        if not args.fake and not _has_key(v.provider):
            raise SystemExit(
                f"missing API key for column {v.label!r} (provider {v.provider!r}) "
                "— set it in .env or use --fake"
            )

    cfg = _load_config(args.config)
    cases = _cases_from_config(cfg)
    raw_cases = {c["id"]: c for c in cfg["cases"]}
    if args.case:
        cases = [c for c in cases if c.case_id == args.case]
        if not cases:
            raise SystemExit(f"no case with id {args.case!r}")
    src_path = cfg.get("store_path", "data/case-calendar.sqlite")

    # Patch telemetry + provider detection. Always wrap dispatch with
    # _variant_dispatch so each column runs its own extraction model (the
    # extraction track passes no model=, so the thread-local one is injected);
    # in --fake mode the wrapped base is the synthetic dispatch, so no SDK is
    # ever called.
    usage.set_price_estimator(costs.estimate_cost)
    usage.record = _capturing_record  # type: ignore[assignment]
    providers._detect_provider = _tl_detect  # type: ignore[assignment]
    orig_dispatch = providers._dispatch_llm_call
    base_dispatch = _fake_dispatch if args.fake else orig_dispatch
    # Persistent LLM-response cache wraps the REAL dispatch (never the fake
    # one), keyed on the fully-resolved request. _make_variant_dispatch
    # injects the column's extraction model BEFORE calling base, so the cache
    # sees the concrete model and keys on it correctly.
    llm_cache: Optional[_LLMCache] = None
    if not args.fake and not args.no_llm_cache:
        llm_cache = _LLMCache(args.llm_cache_path)
        base_dispatch = llm_cache.wrap(base_dispatch)
    providers._dispatch_llm_call = _make_variant_dispatch(  # type: ignore[assignment]
        base_dispatch
    )
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
    # Pin each column to its own (provider, model): neutralize the operator's
    # model AND per-track provider env overrides for the run (see
    # ``_NEUTRALIZED_RUN_ENV`` for why the provider overrides matter). Restored
    # in the finally block below.
    saved_models = {k: os.environ.pop(k, None) for k in _NEUTRALIZED_RUN_ENV}

    # One CourtListener client, response-cached and shared across every build.
    cl = CourtListener()
    _install_cl_cache(cl)

    failed: list[str] = []

    def _build(v: Variant) -> str:
        # Set the thread-locals here too so logs route correctly from the very
        # first line (build_for_variant sets them again, harmlessly).
        _TL.provider = v.provider
        _TL.extract_model = v.extract_model
        _TL.label = v.label
        logger.info("==================== building %s ====================", v.label)
        return build_for_variant(
            v,
            src_path,
            cfg,
            cases,
            raw_cases,
            cl,
            skip_summaries=args.skip_summaries,
        )

    def _safe_build(v: Variant) -> None:
        # One column's failure must not abort the others or the final report.
        try:
            _build(v)
        except Exception:  # noqa: BLE001
            logger.exception(
                "column %s build FAILED; continuing with the rest", v.label
            )
            failed.append(v.label)

    try:
        # All columns concurrently. The shared CourtListener / PDF caches are
        # thread-safe and fetch-under-lock, so whichever thread reaches a given
        # request first makes the one network call and the others get the cached
        # response — CourtListener (and PDF) fetches happen once total no matter
        # how many columns race. LLM calls run outside the lock, in parallel.
        if len(variants_to_build) > 1 and not args.no_parallel:
            with ThreadPoolExecutor(max_workers=len(variants_to_build)) as ex:
                list(ex.map(_safe_build, variants_to_build))
        else:
            for v in variants_to_build:
                _safe_build(v)
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
        if llm_cache is not None:
            llm_cache.log_summary()
            llm_cache.close()
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
        variants_to_build, cfg, src_path, cl, args.validate, failed=failed
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
