"""CLI entry point.

Subcommands:
  sync   — pull updates from CourtListener and refresh the hearing store.
  emit   — write ICS files (and optionally push to Google Calendar) from the
           current store state. Run after ``sync``.
  show   — dump the current state for one or all cases (sanity check).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from . import llm
from .calendars.description import no_time_title_prefix
from .calendars.ics import write_ics
from .calendars.index import build_calendar_models, write_index
from .courtlistener import CourtListener
from .store import Store
from .sync import CaseConfig, CaseSyncer, ExtraDocument


# Deadline status -> hearing-equivalent status used by the renderers.
# pending: still upcoming -> scheduled
# passed:  due-date past, no MARK_FILED arrived -> held (still visible, dim)
# met:     party filed, no need to surface in calendar
# cancelled: vacated/superseded
_DEADLINE_STATUS_MAP = {
    "pending": "scheduled",
    "passed": "held",
    "met": "cancelled",  # filtered out by renderers
    "cancelled": "cancelled",
    "unknown": "scheduled",
}

DEADLINE_DURATION_MINUTES = 15


def _deadline_to_hearing(d: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a deadline row to a hearing-shaped dict the renderers accept.

    Deadlines store their UTC due-instant directly (the syncer applied the
    17:00-court-local default at write time when the LLM didn't supply a
    specific time), so this is mostly key remapping. The category/case-name
    prefixing happens later in :func:`_compose_title`. Returns None if the
    deadline has no due timestamp.
    """
    if not d.get("due_at_utc"):
        return None
    return {
        "case_id": d["case_id"],
        # Prefix the key so it can't collide with a real hearing's key in the
        # ICS UID or the gcal deterministic ID.
        "hearing_key": f"deadline:{d['deadline_key']}",
        "title": d["title"],
        "starts_at_utc": d["due_at_utc"],
        "duration_minutes": DEADLINE_DURATION_MINUTES,
        "timezone": d["timezone"],
        "location": None,
        "judge": None,
        "notes": d.get("notes"),
        "dial_in": None,
        "status": _DEADLINE_STATUS_MAP.get(d.get("status") or "", "scheduled"),
        "significance": d.get("significance"),
        "gcal_event_id": d.get("gcal_event_id"),
        "m365_event_id": d.get("m365_event_id"),
        "docket_id": d.get("docket_id"),
        "source_entry_ids": d.get("source_entry_ids"),
    }


def _compose_title(
    *,
    raw_title: str,
    kind: str,
    case_name: str,
    starts_at_utc: str | None,
    duration_minutes: int | None,
) -> str:
    """Build the calendar event title.

    Order: ``[CATEGORY] [time-status?] {case_name}: {raw_title}``. Category
    comes first so subscribers can scan a shared calendar by event class
    ([HEARING] vs [DEADLINE]). The optional time-status flag (`[time TBD]`
    on future date-only rows, `[time unknown]` on past) sits between
    category and case name so its meaning ("we know the day, not the
    hour") is unambiguous.
    """
    parts = [f"[{kind}]"]
    no_time = not (duration_minutes and duration_minutes > 0)
    if no_time:
        parts.append(no_time_title_prefix(starts_at_utc))
    parts.append(f"{case_name}: {raw_title}")
    return " ".join(parts)


log = logging.getLogger(__name__)


def _load_config(path: str) -> dict[str, Any]:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    if not cfg or "cases" not in cfg or "calendars" not in cfg:
        raise SystemExit(f"config {path} is missing 'cases' or 'calendars'")
    return cfg


def _extra_documents_from_config(
    case_id: str,
    dockets: list[int],
    raw: Any,
) -> list[ExtraDocument]:
    """Parse the per-case ``extra_documents`` list out of the YAML.

    Validates that each entry has the required fields (``docket``, ``url``,
    ``note``), the ``docket`` id is one the case actually tracks, and the
    ``note`` is a non-empty string. We fail loud (via ``SystemExit``) on
    misconfiguration rather than silently skipping — an extra-document
    entry is hand-added by an operator for a specific case, and a typo
    should be surfaced now rather than presented later as "the LLM didn't
    see the document we told it about."

    There is no ``role`` field: a real out-of-band document doesn't always
    slot cleanly into "pleading" / "disposition", and the operator's
    natural-language ``note`` already carries the meaning a role taxonomy
    would. The note is shown verbatim to the summary LLM.
    """
    if raw in (None, []):
        return []
    if not isinstance(raw, list):
        raise SystemExit(
            f"case {case_id!r}: extra_documents must be a list, got {type(raw).__name__}"
        )
    docket_set = set(dockets)
    out: list[ExtraDocument] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SystemExit(
                f"case {case_id!r}: extra_documents[{i}] must be a mapping"
            )
        missing = [k for k in ("docket", "url", "note") if k not in item]
        if missing:
            raise SystemExit(
                f"case {case_id!r}: extra_documents[{i}] missing key(s): {missing}"
            )
        docket_id = item["docket"]
        if not isinstance(docket_id, int) or docket_id not in docket_set:
            raise SystemExit(
                f"case {case_id!r}: extra_documents[{i}].docket={docket_id!r} is not "
                f"in this case's dockets list {sorted(docket_set)}"
            )
        note = item["note"]
        if not isinstance(note, str):
            raise SystemExit(
                f"case {case_id!r}: extra_documents[{i}].note must be a string"
            )
        note = note.strip()
        if not note:
            raise SystemExit(
                f"case {case_id!r}: extra_documents[{i}].note must be a non-empty "
                f"string describing the document and why it was added"
            )
        out.append(
            ExtraDocument(
                docket=docket_id,
                url=str(item["url"]),
                note=note,
            )
        )
    return out


def _cases_from_config(cfg: dict[str, Any]) -> list[CaseConfig]:
    cases: list[CaseConfig] = []
    for c in cfg["cases"]:
        dockets = list(c["dockets"])
        cases.append(
            CaseConfig(
                case_id=c["id"],
                name=c["name"],
                dockets=dockets,
                calendar=c["calendar"],
                extract_deadlines=bool(c.get("extract_deadlines", False)),
                extra_documents=_extra_documents_from_config(
                    c["id"],
                    dockets,
                    c.get("extra_documents"),
                ),
            )
        )
    return cases


def _print_emit_results(
    cfg: dict[str, Any], results: dict[str, dict[str, Any]]
) -> None:
    """Print the per-backend summary for the operator running the CLI.

    The index write is logged inside :func:`emit_calendars` itself
    (via ``log.info``), so we don't re-surface it here — the log line
    is the authoritative signal whether the call came from the CLI or
    the webhook auto-emit path.
    """
    for cal_id, r in results.items():
        if r["ics_path"]:
            print(f"[{cal_id}] wrote {r['events']} events -> {r['ics_path']}")
        if r["gcal_pushed"]:
            gcal_id = cfg["calendars"][cal_id]["google_calendar_id"]
            print(f"[{cal_id}] pushed {r['events']} events to gcal {gcal_id}")
        if r["m365_pushed"]:
            m365_id = cfg["calendars"][cal_id].get("m365_calendar_id") or "(default)"
            print(f"[{cal_id}] pushed {r['events']} events to M365 {m365_id}")


def cmd_sync(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    cases = _cases_from_config(cfg)
    if args.case:
        cases = [c for c in cases if c.case_id == args.case]
        if not cases:
            print(f"no case with id {args.case!r}", file=sys.stderr)
            return 2

    store = Store(cfg.get("store_path", "data/case-calendar.sqlite"))

    if getattr(args, "only_new", False):
        known = store.known_docket_ids()
        cases = [c for c in cases if any(d not in known for d in c.dockets)]
        if not cases:
            print(
                "no new cases — every configured case's dockets are already in the store"
            )
            store.close()
            return 0

    log.info("LLM: %s", llm.provider_info())
    affected_calendars: set[str] = set()
    with CourtListener() as cl:
        syncer = CaseSyncer(cl, store)
        for case in cases:
            stats = syncer.sync_case(case)
            print(
                f"[{case.case_id}] dockets_skipped={stats['dockets_skipped']} "
                f"entries_seen={stats['entries_seen']} "
                f"processed={stats['entries_processed']} actions={stats['actions']}"
            )
            # `entries_processed` is included so we also re-emit when a
            # known entry was reprocessed for a non-scheduling reason —
            # the common case being a previously-unavailable PDF landing on
            # RECAP. process_entry rewrites that entry's recap_documents
            # JSON, and any hearing referencing it as a source_entry will
            # render new document URLs on the next emit; without this
            # condition those links never appear because the LLM returns
            # zero actions for a doc-availability flip.
            if (
                stats.get("actions")
                or stats.get("verified")
                or stats.get("auto_passed")
                or stats.get("entries_processed")
                or stats.get("deduped")
                or stats.get("deduped_held")
            ):
                affected_calendars.add(case.calendar)

        # Agentic summary refresh: process_entry flipped stale=1 on the
        # rows whose dockets received a primary document or disposition
        # this sync; refresh_stale also picks up dockets that have no
        # summary row yet (new cases / new dockets in config). Calendars
        # whose summary text changed get added to affected_calendars so
        # the emit below re-renders them.
        if (cfg.get("case_summaries") or {}).get("enabled"):
            summary_cfg = cfg["case_summaries"]
            from . import summary as summary_mod

            raw_cases = {c["id"]: c for c in cfg["cases"]}
            written = summary_mod.refresh_stale(
                cl=cl,
                store=store,
                cases=cases,
                case_overrides=raw_cases,
                only_case_ids={c.case_id for c in cases},
                provider=summary_cfg.get("provider"),
                model=summary_cfg.get("model"),
                allow_ocr=bool(summary_cfg.get("allow_ocr", True)),
                force=bool(getattr(args, "force_summaries", False)),
            )
            for case_id, docket_ids in written.items():
                case = next(c for c in cases if c.case_id == case_id)
                affected_calendars.add(case.calendar)
                for did in docket_ids:
                    print(f"[{case_id}] regenerated summary for docket {did}")

    # Always run emit_calendars (unless --no-emit). Per-calendar work is
    # scoped by `only_calendars=affected_calendars` — an empty set skips
    # every calendar — but the index is a global view that may have moved
    # even when no calendar's ICS changed, so it gets re-rendered on every
    # sync.
    if not args.no_emit:
        results = emit_calendars(
            cfg,
            store,
            only_calendars=affected_calendars,
        )
        _print_emit_results(cfg, results)
    store.close()
    return 0


def _resolve_gcal(cfg: dict[str, Any], *, setup: bool) -> tuple[str, Path] | None:
    """Return (credentials_path, token_path) if gcal push is enabled, else None.

    Push is enabled when ``google_credentials_path`` is configured AND
    either the token cache exists OR ``setup=True`` (first-run OAuth
    permitted). Returning None means "skip gcal" with no error — typical
    on a fresh deploy before the operator has run the one-time setup.
    """
    credentials_path = cfg.get("google_credentials_path")
    if not credentials_path:
        return None
    token_path = Path(
        cfg.get("google_token_path", "tokens/google-token.json")
    ).expanduser()
    if not token_path.exists() and not setup:
        log.info(
            "gcal push skipped: no token cache at %s. Run "
            "`case-calendar setup gcal` once to stage it.",
            token_path,
        )
        return None
    return credentials_path, token_path


def _resolve_m365(cfg: dict[str, Any], *, setup: bool) -> tuple[str, Path] | None:
    """Return (client_id, token_path) if m365 push is enabled, else None.

    Same auto-detect contract as :func:`_resolve_gcal`: enabled when the
    client id is resolvable AND (token cache present OR setup=True).
    """
    client_id = (
        cfg.get("m365_client_id") or os.environ.get("M365_CLIENT_ID", "").strip()
    )
    if not client_id:
        return None
    token_path = Path(cfg.get("m365_token_path", "tokens/m365-token.json")).expanduser()
    if not token_path.exists() and not setup:
        log.info(
            "m365 push skipped: no token cache at %s. Run "
            "`case-calendar setup m365` once to stage it.",
            token_path,
        )
        return None
    return client_id, token_path


def emit_calendars(
    cfg: dict[str, Any],
    store: Store,
    *,
    only_calendars: set[str] | None = None,
    setup_gcal: bool = False,
    setup_m365: bool = False,
) -> dict[str, dict[str, Any]]:
    """Render hearings + deadlines to ICS files (and gcal / M365 where configured).

    Used by both the ``emit`` CLI command and the polling/webhook paths so
    a single sync update flows all the way to subscribers without a manual
    re-emit. Pass ``only_calendars`` to scope the work to the calendars
    affected by a particular event (e.g. one webhook).

    Push to gcal / M365 happens automatically for any calendar that has
    the relevant id configured AND whose backend has a staged OAuth token
    on disk. ``setup_gcal=True`` / ``setup_m365=True`` additionally allow
    the OAuth browser flow to run if no token is cached — this is the
    first-run code path used by ``case-calendar setup``.

    Returns ``{cal_id: {"events": int, "ics_path": str|None,
    "gcal_pushed": bool, "m365_pushed": bool}}``.
    """
    cases = _cases_from_config(cfg)
    case_overrides = {c["id"]: c for c in cfg["cases"]}  # raw dicts with extras

    by_calendar: dict[str, list[dict]] = defaultdict(list)
    for case in cases:
        if only_calendars is not None and case.calendar not in only_calendars:
            continue
        cal_cfg = cfg["calendars"].get(case.calendar) or {}
        case_cfg = case_overrides.get(case.case_id) or {}
        notify_emails = list(
            case_cfg.get("notify_emails") or cal_cfg.get("notify_emails") or []
        )
        reminders = list(case_cfg.get("reminders") or cal_cfg.get("reminders") or [])

        # Hearings + deadlines flow through the same renderer. Title is
        # composed up-front via _compose_title so the renderer doesn't need
        # to know about category/time-status/case-name structure (it just
        # writes the SUMMARY line as-given).
        rows: list[tuple[str, dict]] = []
        for h in store.get_hearings(case.case_id):
            rows.append(("HEARING", dict(h)))
        for d in store.get_deadlines(case.case_id):
            mapped = _deadline_to_hearing(d)
            if mapped is not None:
                rows.append(("DEADLINE", mapped))

        for kind, h in rows:
            h["_case_name"] = case.name
            h["title"] = _compose_title(
                raw_title=h["title"],
                kind=kind,
                case_name=case.name,
                starts_at_utc=h.get("starts_at_utc"),
                duration_minutes=h.get("duration_minutes"),
            )
            # Decorate with docket / court info for the description body.
            docket_id = h.get("docket_id")
            if docket_id:
                meta = store.get_docket_meta(docket_id) or {}
                h["docket_number"] = meta.get("docket_number")
                h["docket_absolute_url"] = meta.get("absolute_url")
                court_id = meta.get("court_id")
                if court_id:
                    h["court_citation"] = store.get_court_citation(court_id)
            # PACER docket-position numbers ("[65]") for the source entries —
            # surfaced in the description so subscribers can find the cited
            # entry in the CourtListener UI without copy-pasting the opaque CourtListener entry id.
            # Some entries (paperless minute orders) lack a position number;
            # those are silently dropped.
            source_ids = h.get("source_entry_ids") or []
            if source_ids:
                num_map = store.get_entry_numbers(source_ids)
                h["docket_entry_numbers"] = [
                    num_map[i] for i in source_ids if i in num_map
                ]
                # Per-entry document URLs (IA mirror or CourtListener storage). Order
                # by source-entry chronology, with each entry's main doc
                # ahead of its attachments. The compact JSON we persist is
                # already sorted within each entry, so just flatten.
                doc_map = store.get_entry_documents(source_ids)
                docs: list[dict] = []
                for eid in source_ids:
                    docs.extend(doc_map.get(eid, []))
                if docs:
                    h["documents"] = docs
            # Notification config travels on the hearing dict so both ICS
            # and gcal renderers see it.
            if notify_emails:
                h["notify_emails"] = notify_emails
            if reminders:
                h["reminders"] = reminders
            by_calendar[case.calendar].append(h)

    # Resolve push readiness once per emit pass (auto-detect from config +
    # token cache presence). The OAuth-capable backends initialize lazily
    # on first push so an emit that touches only ICS-only calendars never
    # imports the SDKs.
    gcal_resolved = _resolve_gcal(cfg, setup=setup_gcal)
    m365_resolved = _resolve_m365(cfg, setup=setup_m365)

    out: dict[str, dict[str, Any]] = {}
    gcs = None
    m365 = None
    for cal_id, cal_cfg in cfg["calendars"].items():
        if only_calendars is not None and cal_id not in only_calendars:
            continue
        hearings = by_calendar.get(cal_id, [])
        result: dict[str, Any] = {
            "events": len(hearings),
            "ics_path": None,
            "gcal_pushed": False,
            "m365_pushed": False,
        }
        ics_path = cal_cfg.get("ics_path")
        if ics_path:
            write_ics(
                ics_path,
                calendar_name=cal_cfg.get("name", cal_id),
                hearings=hearings,
            )
            result["ics_path"] = ics_path

        gcal_id = cal_cfg.get("google_calendar_id")
        if gcal_id and gcal_resolved is not None:
            if gcs is None:
                from .calendars.gcal import GoogleCalendarSync

                credentials_path, token_path = gcal_resolved
                gcs = GoogleCalendarSync(
                    credentials_path=credentials_path,
                    token_path=token_path,
                )
            gcs.sync(calendar_id=gcal_id, hearings=hearings)
            result["gcal_pushed"] = True

        # M365: push iff this calendar opts in. Per-calendar
        # `m365_calendar_id` selects a specific Outlook calendar; omit it
        # to use the user's default. The auth client is shared across
        # calendars in one emit pass.
        m365_enabled = cal_cfg.get("m365_calendar_id") is not None or cal_cfg.get(
            "m365_use_default_calendar"
        )
        if m365_enabled and m365_resolved is not None:
            if m365 is None:
                from .calendars.m365 import M365CalendarSync

                client_id, token_path = m365_resolved
                m365 = M365CalendarSync(
                    client_id=client_id,
                    token_path=token_path,
                )
            m365.sync(
                hearings=hearings,
                store=store,
                calendar_id=cal_cfg.get("m365_calendar_id"),
            )
            result["m365_pushed"] = True
        out[cal_id] = result

    # index.html is global (lists every calendar + case), so we render it
    # on every emit regardless of `only_calendars` — a webhook touching one
    # calendar may still move another calendar's "Last filing" display
    # because docket date_last_filing watermarks may have advanced
    # elsewhere in the same sync. The write is microseconds and idempotent.
    index_path = cfg.get("index_path")
    if index_path:
        models = build_calendar_models(
            cfg,
            store,
            public_base_url=cfg.get("public_base_url"),
        )
        site_title = cfg.get("site_title", "Case Calendar")
        site_description = cfg.get("site_description")
        kwargs: dict[str, Any] = {"site_title": site_title}
        if site_description:
            kwargs["site_description"] = site_description
        write_index(index_path, calendars=models, **kwargs)
        log.info("wrote index -> %s", index_path)
    return out


def cmd_emit(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    store = Store(cfg.get("store_path", "data/case-calendar.sqlite"))
    results = emit_calendars(cfg, store)
    _print_emit_results(cfg, results)
    store.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import threading

    from .serve import serve

    cfg = _load_config(args.config)
    cases = _cases_from_config(cfg)
    secret = os.environ.get("CASE_CALENDAR_WEBHOOK_SECRET", "").strip()
    if not secret or len(secret) < 16:
        print(
            "CASE_CALENDAR_WEBHOOK_SECRET must be set in .env to a long random "
            "string (>=16 chars). Generate one with:\n"
            "  python -c 'import secrets; print(secrets.token_urlsafe(32))'",
            file=sys.stderr,
        )
        return 2

    store = Store(cfg.get("store_path", "data/case-calendar.sqlite"))
    log.info("LLM: %s", llm.provider_info())

    # Debounced summary refresh state. PACER batch uploads can fire many
    # webhook deliveries inside a few minutes; we don't want a Sonnet call
    # per delivery. Each delivery that landed a stale-flagging entry resets
    # this Timer, so the regen fires only after `debounce_seconds` of
    # quiet. The timer thread itself is a daemon so it exits with the
    # process; if the burst doesn't settle before shutdown, the next sync
    # (or the next webhook after restart) will catch the stale rows.
    summary_cfg = cfg.get("case_summaries") or {}
    summary_enabled = bool(summary_cfg.get("enabled"))
    debounce_seconds = float(summary_cfg.get("debounce_seconds", 300))
    debounce_lock = threading.Lock()
    debounce_state: dict[str, Any] = {
        "timer": None,
        "pending_cals": set(),
    }
    raw_cases = {c["id"]: c for c in cfg["cases"]}

    def _fire_debounced_summary() -> None:
        """Timer callback: regenerate stale summaries that have settled.

        Pulls the accumulated pending-calendars set, clears the timer
        handle under the lock, then runs the refresh and a second emit
        scoped to whatever calendars actually got new prose. Runs in a
        daemon thread, so any exception is logged and swallowed — the
        webhook listener is unaffected.
        """
        with debounce_lock:
            cals = set(debounce_state["pending_cals"])
            debounce_state["pending_cals"].clear()
            debounce_state["timer"] = None
        if not cals:
            return
        try:
            from . import summary as summary_mod

            scoped_ids = {c.case_id for c in cases if c.calendar in cals}
            written = summary_mod.refresh_stale(
                cl=cl,
                store=store,
                cases=cases,
                case_overrides=raw_cases,
                only_case_ids=scoped_ids,
                provider=summary_cfg.get("provider"),
                model=summary_cfg.get("model"),
                allow_ocr=bool(summary_cfg.get("allow_ocr", True)),
            )
            if not written:
                return
            written_cals = {c.calendar for c in cases if c.case_id in written}
            log.info(
                "debounced summary refresh: regenerated %d row(s); re-emitting %s",
                sum(len(v) for v in written.values()),
                written_cals,
            )
            emit_calendars(cfg, store, only_calendars=written_cals)
        except Exception:
            log.exception("debounced summary refresh failed")

    def _arm_debounce(only_calendars: set[str]) -> None:
        """(Re)start the debounce timer if any of the affected calendars
        have stale summary rows. Each call extends the wait — the regen
        only fires after ``debounce_seconds`` of webhook quiet."""
        if not summary_enabled:
            return
        # Cheap check: is any (case_id, docket_number, court_id) group on
        # these calendars stale? Avoids arming the timer on deliveries
        # that didn't touch any primary-document or disposition entry.
        # Resolving docket_id → group via Store dedupes the inner loop so
        # CourtListener splits sharing one logical PACER docket count once.
        any_stale = False
        for case in cases:
            if case.calendar not in only_calendars:
                continue
            seen_groups: set[tuple[str, str]] = set()
            for docket_id in case.dockets:
                meta = store.get_docket_meta(docket_id) or {}
                docket_number = meta.get("docket_number")
                court_id = meta.get("court_id")
                if not docket_number or not court_id:
                    continue
                group_key = (docket_number, court_id)
                if group_key in seen_groups:
                    continue
                seen_groups.add(group_key)
                if store.is_summary_stale(case.case_id, docket_number, court_id):
                    any_stale = True
                    break
            if any_stale:
                break
        if not any_stale:
            return
        with debounce_lock:
            debounce_state["pending_cals"].update(only_calendars)
            existing = debounce_state["timer"]
            if existing is not None:
                existing.cancel()
            t = threading.Timer(debounce_seconds, _fire_debounced_summary)
            t.daemon = True
            debounce_state["timer"] = t
            t.start()
        log.info(
            "debounce armed: summary refresh in %.0fs for calendars=%s",
            debounce_seconds,
            only_calendars,
        )

    def emit_fn(only_calendars: set[str]) -> None:
        # Fast path: re-render ICS / push gcal+M365 immediately so the
        # subscriber-visible calendar reflects this delivery within
        # seconds. The (potentially expensive) Sonnet summary regen is
        # debounced separately — a second emit_calendars call fires from
        # the timer once the burst settles. The index rendered here will
        # still carry the previous summary text; that's fine, the
        # timer-fired re-emit overwrites the index a few minutes later.
        results = emit_calendars(
            cfg,
            store,
            only_calendars=only_calendars,
        )
        # Debounce-arm the summary refresh. If this delivery didn't touch
        # a primary document or disposition, _arm_debounce notices
        # there are no stale rows and noops, so we don't pay for an
        # idle timer.
        _arm_debounce(only_calendars)
        for cal_id, r in results.items():
            if r["ics_path"]:
                log.info(
                    "[%s] wrote %d events -> %s",
                    cal_id,
                    r["events"],
                    r["ics_path"],
                )
            if r["gcal_pushed"]:
                gcal_id = cfg["calendars"][cal_id]["google_calendar_id"]
                log.info(
                    "[%s] pushed %d events to gcal %s",
                    cal_id,
                    r["events"],
                    gcal_id,
                )
            if r["m365_pushed"]:
                m365_id = (
                    cfg["calendars"][cal_id].get(
                        "m365_calendar_id",
                    )
                    or "(default)"
                )
                log.info(
                    "[%s] pushed %d events to M365 %s",
                    cal_id,
                    r["events"],
                    m365_id,
                )

    with CourtListener() as cl:
        serve(
            host=args.host,
            port=args.port,
            secret=secret,
            cases=cases,
            store=store,
            cl=cl,
            emit_fn=emit_fn,
        )
    store.close()
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """One-time OAuth setup for gcal or m365 push.

    Triggers the interactive browser flow and stages the resulting token
    cache, so subsequent ``sync`` / ``serve`` invocations push silently.
    Must run on a machine with a browser; see README "Bootstrapping OAuth
    on a headless server" for the cross-machine token-copy workaround.
    """
    cfg = _load_config(args.config)
    if args.backend == "gcal":
        if not cfg.get("google_credentials_path"):
            print(
                "google_credentials_path not set in config.yaml. See README "
                "section 'Optional: Google Calendar push'.",
                file=sys.stderr,
            )
            return 2
        from .calendars.gcal import GoogleCalendarSync

        token_path = Path(
            cfg.get("google_token_path", "tokens/google-token.json")
        ).expanduser()
        GoogleCalendarSync(
            credentials_path=cfg["google_credentials_path"],
            token_path=token_path,
        )
        print(f"gcal token staged at {token_path}")
        return 0

    # m365
    client_id = (
        cfg.get("m365_client_id") or os.environ.get("M365_CLIENT_ID", "").strip()
    )
    if not client_id:
        print(
            "m365_client_id not set in config.yaml and M365_CLIENT_ID env "
            "var is empty. See README section 'Optional: Microsoft 365 / "
            "Outlook push'.",
            file=sys.stderr,
        )
        return 2
    from .calendars.m365 import M365CalendarSync

    token_path = Path(cfg.get("m365_token_path", "tokens/m365-token.json")).expanduser()
    M365CalendarSync(client_id=client_id, token_path=token_path)
    print(f"m365 auth record staged at {token_path}")
    return 0


def cmd_summarize(args: argparse.Namespace) -> int:
    """Generate per-docket AI summaries for the index page.

    Opt-in feature gated on ``case_summaries.enabled`` in the config. Each
    case's dockets are scanned for the primary document (latest
    indictment / amended complaint / etc.) and any disposition documents
    (judgment, plea agreement, verdict, dismissal). The PDFs are fed to a
    higher-tier LLM (Sonnet by default) along with the structured-events
    scaffold the extractor already recorded, producing a 2-4 sentence
    prose summary persisted to the ``case_summaries`` table and rendered
    into ``index.html`` on the next emit.

    Existing summary rows are reused unless ``--force`` is passed; primary
    documents are stable, so re-running cheaply is the default.
    """
    cfg = _load_config(args.config)
    summary_cfg = cfg.get("case_summaries") or {}
    if not summary_cfg.get("enabled"):
        print(
            "case_summaries.enabled is not set in the config. "
            "Enable it under the top-level `case_summaries:` block to use this command.",
            file=sys.stderr,
        )
        return 2

    from .summary import summarize_case

    cases = _cases_from_config(cfg)
    raw_cases = {c["id"]: c for c in cfg["cases"]}
    if args.case:
        cases = [c for c in cases if c.case_id == args.case]
        if not cases:
            print(f"no case with id {args.case!r}", file=sys.stderr)
            return 2

    provider = summary_cfg.get("provider")
    model = summary_cfg.get("model")
    allow_ocr = bool(summary_cfg.get("allow_ocr", True))

    store = Store(cfg.get("store_path", "data/case-calendar.sqlite"))
    log.info("LLM: %s", llm.provider_info())
    affected_calendars: set[str] = set()
    with CourtListener() as cl:
        for case in cases:
            raw = raw_cases.get(case.case_id, {})
            aggregation_note = raw.get("aggregation_note")
            print(f"=== summarizing {case.case_id} — {case.name} ===")
            written = summarize_case(
                cl=cl,
                store=store,
                case=case,
                aggregation_note=aggregation_note,
                provider=provider,
                model=model,
                allow_ocr=allow_ocr,
                force=args.force,
            )
            for row in written:
                print(
                    f"  {row['docket_number']} ({row['court_id']}): "
                    f"{len(row['summary'])} chars (model={row['model']})"
                )
            if written:
                affected_calendars.add(case.calendar)

    # Re-emit so the new summaries land in the index.html immediately.
    if not args.no_emit and affected_calendars:
        results = emit_calendars(
            cfg,
            store,
            only_calendars=affected_calendars,
        )
        _print_emit_results(cfg, results)
    store.close()
    return 0


def cmd_webhook_url(args: argparse.Namespace) -> int:
    """Print the full CourtListener webhook URL.

    The URL has three components: the public scheme+host where this
    ``serve`` receiver is reachable, the constant path prefix
    ``/webhooks/case-calendar/``, and the secret from
    ``CASE_CALENDAR_WEBHOOK_SECRET`` in ``.env``. Reads the secret from
    the env (load_dotenv has already run by the time we get here) and
    composes the URL ready to paste into the CourtListener webhook
    dashboard.

    With ``--check``, also hits the receiver's secret-gated health
    endpoint and reports the result. This is the one-shot way to
    confirm that the public host is reachable, that whatever fronts
    the receiver (Caddy / Cloudflare / etc.) is forwarding to
    ``case-calendar serve`` rather than synthesizing a 200, and that
    the secret in your ``.env`` matches the one the running receiver
    booted with.
    """
    from .serve import WEBHOOK_PATH_PREFIX

    secret = os.environ.get("CASE_CALENDAR_WEBHOOK_SECRET", "").strip()
    if not secret:
        print(
            "CASE_CALENDAR_WEBHOOK_SECRET not set in .env. Generate one with:\n"
            "  python -c 'import secrets; print(secrets.token_urlsafe(32))'\n"
            "and paste it on the CASE_CALENDAR_WEBHOOK_SECRET= line in .env.",
            file=sys.stderr,
        )
        return 2

    host = args.host
    if host:
        # If --host omits a scheme, default to https (CourtListener won't
        # deliver to plain http in production). An explicit http://... is
        # respected as-is so a developer can curl the local receiver.
        if not host.startswith(("http://", "https://")):
            host = f"https://{host}"
        host = host.rstrip("/")
    elif args.check:
        print(
            "--check requires --host so we know which receiver to probe.",
            file=sys.stderr,
        )
        return 2
    else:
        host = "https://<your-public-host>"
        print(
            "note: pass --host <your-public-host> for a ready-to-paste URL "
            "(e.g. --host webhook.example.com). Substitute the placeholder "
            "below for whatever fronting host you've set up (Caddy / "
            "Cloudflare Tunnel / fly.io / etc).",
            file=sys.stderr,
        )

    url = f"{host}{WEBHOOK_PATH_PREFIX}{secret}"
    # Primary output of the `webhook-url` command — operator pastes this
    # into the CourtListener webhook dashboard. The URL embeds the
    # webhook secret by design, so the operator should treat the line
    # as sensitive (don't paste into bug reports / chat). Stderr banner
    # makes that explicit. CodeQL flags this as
    # `py/clear-text-logging-sensitive-data`; the alert is dismissed
    # with rationale because the command's contract IS to emit the URL.
    print(
        "# The URL below embeds your webhook secret. Treat it as sensitive — "
        "paste it into the CourtListener webhook dashboard, not into bug "
        "reports / chat / commit messages.",
        file=sys.stderr,
    )
    print(url)

    if args.check:
        return _check_webhook_health(url, secret)
    return 0


def _redact_secret(text: str, secret: str) -> str:
    """Replace every occurrence of ``secret`` in ``text`` with ``<REDACTED>``.

    Used by health-check error messages so an operator can copy/paste a
    failing diagnostic into a bug report or chat without leaking the
    receiver secret. Idempotent; safe to call with an empty secret (the
    redaction becomes a no-op).
    """
    if not secret:
        return text
    return text.replace(secret, "<REDACTED>")


def _check_webhook_health(webhook_url: str, secret: str) -> int:
    """Probe the secret-gated health endpoint and report.

    Returns 0 on a healthy 200 with the expected service identifier, 1
    on any reachability / auth / shape problem. The receiver's
    `GET <prefix>/<secret>/health` route is the contract — anything
    else (200-empty from a stale proxy, 403 from a wrong secret, a
    CF-served HTML error page) signals a real misconfiguration the
    operator needs to see.

    ``secret`` is the same value embedded in ``webhook_url``; it is used
    only to redact any response body that echoes it in operator-facing
    failure messages, never to log or transmit the secret beyond the GET
    request itself.
    """
    import json as _json
    import urllib.error
    import urllib.request

    health_url = f"{webhook_url}/health"
    # Do not log URL values derived from the secret-bearing webhook path.
    # Use a stable, non-sensitive endpoint label in diagnostics.
    safe_endpoint = "webhook health endpoint"
    req = urllib.request.Request(
        health_url,
        method="GET",
        headers={"User-Agent": "case-calendar-webhook-check/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = _redact_secret(
            e.read().decode("utf-8", errors="replace"), secret
        )
        print(
            f"\nhealth check FAILED: HTTP {e.code} from {safe_endpoint}\n{body}",
            file=sys.stderr,
        )
        return 1
    except urllib.error.URLError as e:
        print(
            f"\nhealth check FAILED: cannot reach {safe_endpoint}: {e.reason}",
            file=sys.stderr,
        )
        return 1

    body = _redact_secret(body, secret)
    if status != 200:
        print(
            f"\nhealth check FAILED: HTTP {status} from {safe_endpoint}\n{body}",
            file=sys.stderr,
        )
        return 1

    try:
        payload = _json.loads(body)
    except _json.JSONDecodeError:
        print(
            f"\nhealth check FAILED: non-JSON 200 from {safe_endpoint} — "
            f"something between you and the receiver is intercepting "
            f"requests. Body was:\n{body}",
            file=sys.stderr,
        )
        return 1

    if payload.get("service") != "case-calendar":
        print(
            f"\nhealth check FAILED: 200 from {safe_endpoint} but body "
            f"doesn't identify as case-calendar:\n{body}",
            file=sys.stderr,
        )
        return 1

    tracking = payload.get("tracking") or {}
    print(
        f"\nhealth check OK: receiver tracking "
        f"{tracking.get('dockets', '?')} dockets "
        f"across {tracking.get('cases', '?')} cases."
    )
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """Delete every store row tied to a docket_id no longer in config.

    Two-phase by default: print the plan (which dockets, how many rows per
    table) and exit. Re-run with ``--apply`` to actually delete. The destructive
    step is irreversible — the AGENTS.md backup-before-schema-change rule extends in
    spirit to bulk DML against a real store; back the DB up first.
    """
    cfg = _load_config(args.config)
    cases = _cases_from_config(cfg)
    live: set[int] = {int(d) for c in cases for d in c.dockets}
    store = Store(cfg.get("store_path", "data/case-calendar.sqlite"))
    try:
        known = set(store.list_all_docket_ids())
        orphans = sorted(known - live)
        if not orphans:
            print(
                f"No orphan dockets — store has {len(known)} docket(s), "
                f"all referenced by config."
            )
            return 0
        print(
            f"Found {len(orphans)} orphan docket(s) "
            f"(in store, not referenced by any case in config):"
        )
        plan: list[tuple[int, dict[str, int], dict[str, Any]]] = []
        for did in orphans:
            counts = store.count_docket_rows(did)
            meta_row = store.conn.execute(
                "SELECT court_id, docket_number, case_name "
                "FROM dockets WHERE docket_id=?",
                (did,),
            ).fetchone()
            meta = (
                dict(meta_row)
                if meta_row
                else {
                    "court_id": None,
                    "docket_number": None,
                    "case_name": None,
                }
            )
            plan.append((did, counts, meta))
            label = (
                f"{meta['docket_number'] or '?'} "
                f"({meta['court_id'] or '?'}) — "
                f"{meta['case_name'] or '<no metadata>'}"
            )
            total = sum(counts.values())
            per_table = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
            print(f"  docket_id={did}  {label}")
            print(f"    {total} rows: {per_table or '(empty)'}")
        if not args.apply:
            print()
            print("Dry run — re-run with --apply to delete.")
            return 0
        print()
        print("Deleting...")
        for did, _, _ in plan:
            deleted = store.delete_docket(did)
            per_table = ", ".join(f"{k}={v}" for k, v in deleted.items() if v)
            print(
                f"  docket_id={did}  deleted {sum(deleted.values())} rows: "
                f"{per_table or '(none)'}"
            )
        return 0
    finally:
        store.close()


def cmd_show(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    cases = _cases_from_config(cfg)
    store = Store(cfg.get("store_path", "data/case-calendar.sqlite"))
    for case in cases:
        if args.case and case.case_id != args.case:
            continue
        hearings = store.get_hearings(case.case_id)
        deadlines = store.get_deadlines(case.case_id)
        print(
            f"=== {case.case_id} — {case.name} "
            f"({len(hearings)} hearings, {len(deadlines)} deadlines) ==="
        )
        for h in sorted(hearings, key=lambda x: x.get("starts_at_utc") or ""):
            print(
                f"  [{h['status']:<10}] {h.get('starts_at_utc') or '????'}  "
                f"{h['title']}  ({h['hearing_key']})"
            )
            if h.get("location") or h.get("judge"):
                print(f"     loc={h.get('location')!r} judge={h.get('judge')!r}")
            if h.get("dial_in"):
                print(f"     dial-in={h['dial_in']!r}")
        for d in sorted(deadlines, key=lambda x: x.get("due_at_utc") or ""):
            print(
                f"  [{d['status']:<10}] {d.get('due_at_utc') or '????'}  "
                f"DEADLINE: {d['title']}  ({d['deadline_key']})"
            )
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(prog="case-calendar")
    parser.add_argument(
        "-c", "--config", default="config.yaml", help="path to config YAML"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync", help="pull updates from CourtListener")
    p_sync.add_argument("--case", help="only sync this case_id")
    p_sync.add_argument(
        "--only-new",
        action="store_true",
        help="only sync cases whose dockets aren't yet in the store — "
        "useful after adding new cases to config.yaml without needing "
        "to remember their ids",
    )
    p_sync.add_argument(
        "--no-emit",
        action="store_true",
        help="skip auto-emitting ICS for affected calendars at end of sync",
    )
    p_sync.add_argument(
        "--force-summaries",
        action="store_true",
        help="regenerate every case summary as part of the sync (use after "
        "a model upgrade or prompt change — avoids a separate "
        "`summarize --force` run that would hit CourtListener again)",
    )
    p_sync.set_defaults(func=cmd_sync)

    p_emit = sub.add_parser(
        "emit",
        help="emit calendars from current store (auto-pushes to any "
        "configured gcal / M365 backend with a staged token)",
    )
    p_emit.set_defaults(func=cmd_emit)

    p_serve = sub.add_parser(
        "serve",
        help="run the CourtListener webhook receiver (real-time alternative to sync)",
    )
    p_serve.add_argument(
        "--host", default="127.0.0.1", help="bind host (default 127.0.0.1)"
    )
    p_serve.add_argument(
        "--port", type=int, default=8000, help="bind port (default 8000)"
    )
    p_serve.set_defaults(func=cmd_serve)

    p_setup = sub.add_parser(
        "setup",
        help="one-time OAuth setup for Google Calendar or Microsoft 365 / "
        "Outlook push (opens a browser to stage the token cache)",
    )
    p_setup.add_argument(
        "backend",
        choices=["gcal", "m365"],
        help="which backend to authorize",
    )
    p_setup.set_defaults(func=cmd_setup)

    p_summarize = sub.add_parser(
        "summarize",
        help="generate per-docket AI case summaries for the index page "
        "(opt-in; gated on case_summaries.enabled in the config)",
    )
    p_summarize.add_argument(
        "--case",
        help="only summarize this case_id",
    )
    p_summarize.add_argument(
        "--force",
        action="store_true",
        help="regenerate summaries even when a row already exists "
        "(use after a model upgrade or prompt change)",
    )
    p_summarize.add_argument(
        "--no-emit",
        action="store_true",
        help="skip auto-emitting index.html after writing summaries",
    )
    p_summarize.set_defaults(func=cmd_summarize)

    p_show = sub.add_parser("show", help="dump current hearings")
    p_show.add_argument("--case", help="only show this case_id")
    p_show.set_defaults(func=cmd_show)

    p_prune = sub.add_parser(
        "prune",
        help="delete store rows tied to docket_ids no longer in the config "
        "(dry-run by default; pass --apply to actually delete)",
    )
    p_prune.add_argument(
        "--apply",
        action="store_true",
        help="actually delete the orphan rows. Default is dry-run, which "
        "prints the deletion plan without modifying the store. Back up "
        "the SQLite store before applying.",
    )
    p_prune.set_defaults(func=cmd_prune)

    p_webhook_url = sub.add_parser(
        "webhook-url",
        help="print the CourtListener webhook URL ready to paste into the "
        "webhook dashboard (uses CASE_CALENDAR_WEBHOOK_SECRET from .env)",
    )
    p_webhook_url.add_argument(
        "--host",
        help="public host where the serve receiver is reachable, e.g. "
        "webhook.example.com (https:// is assumed) or "
        "http://localhost:8000 for a local curl test",
    )
    p_webhook_url.add_argument(
        "--check",
        action="store_true",
        help="after printing the URL, hit the secret-gated health "
        "endpoint to verify the receiver is reachable and the "
        "secret matches (requires --host)",
    )
    p_webhook_url.set_defaults(func=cmd_webhook_url)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
