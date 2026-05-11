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
from .courtlistener import CourtListener
from .store import Store
from .sync import CaseConfig, CaseSyncer


# Deadline status -> hearing-equivalent status used by the renderers.
# pending: still upcoming -> scheduled
# passed:  due-date past, no MARK_FILED arrived -> held (still visible, dim)
# met:     party filed, no need to surface in calendar
# cancelled: vacated/superseded
_DEADLINE_STATUS_MAP = {
    "pending": "scheduled",
    "passed": "held",
    "met": "cancelled",       # filtered out by renderers
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


def _cases_from_config(cfg: dict[str, Any]) -> list[CaseConfig]:
    return [
        CaseConfig(
            case_id=c["id"],
            name=c["name"],
            dockets=list(c["dockets"]),
            calendar=c["calendar"],
            extract_deadlines=bool(c.get("extract_deadlines", False)),
        )
        for c in cfg["cases"]
    ]


def cmd_sync(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    cases = _cases_from_config(cfg)
    if args.case:
        cases = [c for c in cases if c.case_id == args.case]
        if not cases:
            print(f"no case with id {args.case!r}", file=sys.stderr)
            return 2

    store = Store(cfg.get("store_path", "data/case-calendar.sqlite"))
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
            if stats.get("actions") or stats.get("verified") \
                    or stats.get("auto_held") or stats.get("auto_passed"):
                affected_calendars.add(case.calendar)

    if not args.no_emit and affected_calendars:
        results = emit_calendars(
            cfg, store,
            only_calendars=affected_calendars,
        )
        for cal_id, r in results.items():
            if r["ics_path"]:
                print(f"[{cal_id}] wrote {r['events']} events -> {r['ics_path']}")
            if r["gcal_pushed"]:
                gcal_id = cfg["calendars"][cal_id]["google_calendar_id"]
                print(f"[{cal_id}] pushed {r['events']} events to gcal {gcal_id}")
            if r["m365_pushed"]:
                m365_id = cfg["calendars"][cal_id].get("m365_calendar_id") or "(default)"
                print(f"[{cal_id}] pushed {r['events']} events to M365 {m365_id}")
    store.close()
    return 0


def _resolve_gcal(cfg: dict[str, Any], *, setup: bool) -> tuple[str | None, Path] | None:
    """Return (credentials_path, token_path) if gcal push is enabled, else None.

    Push is enabled when ``google_credentials_path`` is configured AND
    either the token cache exists OR ``setup=True`` (first-run OAuth
    permitted). Returning None means "skip gcal" with no error — typical
    on a fresh deploy before the operator has run the one-time setup.
    """
    credentials_path = cfg.get("google_credentials_path")
    if not credentials_path:
        return None
    token_path = Path(cfg.get(
        "google_token_path", "~/.case-calendar/google-token.json"
    )).expanduser()
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
        cfg.get("m365_client_id")
        or os.environ.get("M365_CLIENT_ID", "").strip()
    )
    if not client_id:
        return None
    token_path = Path(cfg.get(
        "m365_token_path", "~/.case-calendar/m365-token.json"
    )).expanduser()
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
        notify_emails = list(case_cfg.get("notify_emails")
                             or cal_cfg.get("notify_emails") or [])
        reminders = list(case_cfg.get("reminders")
                         or cal_cfg.get("reminders") or [])

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
            # entry in the CL UI without copy-pasting the opaque CL entry id.
            # Some entries (paperless minute orders) lack a position number;
            # those are silently dropped.
            source_ids = h.get("source_entry_ids") or []
            if source_ids:
                num_map = store.get_entry_numbers(source_ids)
                h["docket_entry_numbers"] = [
                    num_map[i] for i in source_ids if i in num_map
                ]
                # Per-entry document URLs (IA mirror or CL storage). Order
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
        m365_enabled = (
            cal_cfg.get("m365_calendar_id") is not None
            or cal_cfg.get("m365_use_default_calendar")
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
    return out


def cmd_emit(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    store = Store(cfg.get("store_path", "data/case-calendar.sqlite"))
    results = emit_calendars(cfg, store)
    for cal_id, r in results.items():
        if r["ics_path"]:
            print(f"[{cal_id}] wrote {r['events']} events -> {r['ics_path']}")
        if r["gcal_pushed"]:
            gcal_id = cfg["calendars"][cal_id]["google_calendar_id"]
            print(f"[{cal_id}] pushed {r['events']} events to gcal {gcal_id}")
        if r["m365_pushed"]:
            m365_id = cfg["calendars"][cal_id].get("m365_calendar_id") or "(default)"
            print(f"[{cal_id}] pushed {r['events']} events to M365 {m365_id}")
    store.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
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

    def emit_fn(only_calendars: set[str]) -> None:
        results = emit_calendars(
            cfg, store,
            only_calendars=only_calendars,
        )
        for cal_id, r in results.items():
            if r["ics_path"]:
                log.info(
                    "[%s] wrote %d events -> %s",
                    cal_id, r["events"], r["ics_path"],
                )
            if r["gcal_pushed"]:
                gcal_id = cfg["calendars"][cal_id]["google_calendar_id"]
                log.info(
                    "[%s] pushed %d events to gcal %s",
                    cal_id, r["events"], gcal_id,
                )
            if r["m365_pushed"]:
                m365_id = cfg["calendars"][cal_id].get(
                    "m365_calendar_id",
                ) or "(default)"
                log.info(
                    "[%s] pushed %d events to M365 %s",
                    cal_id, r["events"], m365_id,
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

        token_path = Path(cfg.get(
            "google_token_path", "~/.case-calendar/google-token.json"
        )).expanduser()
        GoogleCalendarSync(
            credentials_path=cfg["google_credentials_path"],
            token_path=token_path,
        )
        print(f"gcal token staged at {token_path}")
        return 0

    # m365
    client_id = (
        cfg.get("m365_client_id")
        or os.environ.get("M365_CLIENT_ID", "").strip()
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

    token_path = Path(cfg.get(
        "m365_token_path", "~/.case-calendar/m365-token.json"
    )).expanduser()
    M365CalendarSync(client_id=client_id, token_path=token_path)
    print(f"m365 auth record staged at {token_path}")
    return 0


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
        "--no-emit",
        action="store_true",
        help="skip auto-emitting ICS for affected calendars at end of sync",
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
    p_serve.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=8000, help="bind port (default 8000)")
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

    p_show = sub.add_parser("show", help="dump current hearings")
    p_show.add_argument("--case", help="only show this case_id")
    p_show.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
