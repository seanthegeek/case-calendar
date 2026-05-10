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
from typing import Any

import yaml
from dotenv import load_dotenv

from . import llm
from .calendars.ics import write_ics
from .courtlistener import CourtListener
from .store import Store
from .sync import CaseConfig, CaseSyncer

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
    with CourtListener() as cl:
        syncer = CaseSyncer(cl, store)
        for case in cases:
            stats = syncer.sync_case(case)
            print(
                f"[{case.case_id}] dockets_skipped={stats['dockets_skipped']} "
                f"entries_seen={stats['entries_seen']} "
                f"processed={stats['entries_processed']} actions={stats['actions']}"
            )
    store.close()
    return 0


def cmd_emit(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    cases = _cases_from_config(cfg)
    store = Store(cfg.get("store_path", "data/case-calendar.sqlite"))

    case_overrides = {c["id"]: c for c in cfg["cases"]}  # raw dicts with extras

    by_calendar: dict[str, list[dict]] = defaultdict(list)
    for case in cases:
        cal_cfg = cfg["calendars"].get(case.calendar) or {}
        case_cfg = case_overrides.get(case.case_id) or {}
        notify_emails = list(case_cfg.get("notify_emails")
                             or cal_cfg.get("notify_emails") or [])
        reminders = list(case_cfg.get("reminders")
                         or cal_cfg.get("reminders") or [])

        for h in store.get_hearings(case.case_id):
            h = dict(h)
            h["_case_name"] = case.name
            # Prefix titles with the FULL case name (e.g.
            # "United States v. Knoot: Sentencing") so events from
            # different cases stay distinguishable on a shared calendar.
            # We used to strip to the plaintiff side only ("United States:")
            # but for criminal cases that's the same string for every case
            # and disambiguates nothing.
            h["title"] = f"{case.name}: {h['title']}"
            # Decorate with docket / court info for the description body.
            docket_id = h.get("docket_id")
            if docket_id:
                meta = store.get_docket_meta(docket_id) or {}
                h["docket_number"] = meta.get("docket_number")
                h["docket_absolute_url"] = meta.get("absolute_url")
                court_id = meta.get("court_id")
                if court_id:
                    h["court_citation"] = store.get_court_citation(court_id)
            # Notification config travels on the hearing dict so both ICS
            # and gcal renderers see it.
            if notify_emails:
                h["notify_emails"] = notify_emails
            if reminders:
                h["reminders"] = reminders
            by_calendar[case.calendar].append(h)

    for cal_id, cal_cfg in cfg["calendars"].items():
        hearings = by_calendar.get(cal_id, [])
        ics_path = cal_cfg.get("ics_path")
        if ics_path:
            write_ics(
                ics_path,
                calendar_name=cal_cfg.get("name", cal_id),
                hearings=hearings,
            )
            print(f"[{cal_id}] wrote {len(hearings)} events -> {ics_path}")

        gcal_id = cal_cfg.get("google_calendar_id")
        if gcal_id and args.push_gcal:
            from .calendars.gcal import GoogleCalendarSync

            gcs = GoogleCalendarSync(
                credentials_path=cfg["google_credentials_path"],
                token_path=cfg.get(
                    "google_token_path", "~/.case-calendar/google-token.json"
                ),
            )
            gcs.sync(calendar_id=gcal_id, hearings=hearings)
            print(f"[{cal_id}] pushed {len(hearings)} events to gcal {gcal_id}")

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
    with CourtListener() as cl:
        serve(
            host=args.host,
            port=args.port,
            secret=secret,
            cases=cases,
            store=store,
            cl=cl,
        )
    store.close()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    cases = _cases_from_config(cfg)
    store = Store(cfg.get("store_path", "data/case-calendar.sqlite"))
    for case in cases:
        if args.case and case.case_id != args.case:
            continue
        hearings = store.get_hearings(case.case_id)
        print(f"=== {case.case_id} — {case.name} ({len(hearings)} hearings) ===")
        for h in sorted(hearings, key=lambda x: x.get("starts_at_utc") or ""):
            print(
                f"  [{h['status']:<10}] {h.get('starts_at_utc') or '????'}  "
                f"{h['title']}  ({h['hearing_key']})"
            )
            if h.get("location") or h.get("judge"):
                print(f"     loc={h.get('location')!r} judge={h.get('judge')!r}")
            if h.get("dial_in"):
                print(f"     dial-in={h['dial_in']!r}")
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
    p_sync.set_defaults(func=cmd_sync)

    p_emit = sub.add_parser("emit", help="emit calendars from current store")
    p_emit.add_argument(
        "--push-gcal",
        action="store_true",
        help="also push to Google Calendar (requires creds in config)",
    )
    p_emit.set_defaults(func=cmd_emit)

    p_serve = sub.add_parser(
        "serve",
        help="run the CourtListener webhook receiver (real-time alternative to sync)",
    )
    p_serve.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=8000, help="bind port (default 8000)")
    p_serve.set_defaults(func=cmd_serve)

    p_show = sub.add_parser("show", help="dump current hearings")
    p_show.add_argument("--case", help="only show this case_id")
    p_show.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
