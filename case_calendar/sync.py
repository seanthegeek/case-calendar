"""Per-case sync logic.

For each case we:
  1. Pull docket entries newer than the last seen ``date_modified``.
  2. Run the keyword pre-filter; skip irrelevant entries cheaply.
  3. For relevant entries, optionally pull the linked PDF plain-text from the
     RECAP API.
  4. Hand entry + known hearings to the LLM extractor.
  5. Apply the returned actions to the SQLite hearing store.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from . import llm, pdf, summary as summary_mod, url_validator
from .courtlistener import CourtListener
from .courts import tz_for
from .extractor import is_extractable
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class CaseConfig:
    case_id: str          # stable ID used as primary key in the store
    name: str             # human title
    dockets: list[int]
    calendar: str         # which output calendar this case belongs to
    extract_deadlines: bool = False
    """Force-on override for filing-deadline extraction. False (the default)
    means auto-detect from each docket's ``docket_number`` prefix: civil
    dockets get deadline tracking, routine criminal dockets don't. Set
    ``true`` to force deadline tracking on regardless — useful for serious
    criminal trials with real pretrial motion practice where the briefing
    cadence IS worth watching."""


# Federal docket-number type codes that indicate a routine criminal matter
# (criminal felony, criminal misdemeanor, criminal magistrate complaint,
# petty offense). Federal docket numbers look like "D:YY-XX-NNNNN-..." where
# XX is the type code; we match the type sandwiched between dashes and
# followed by a digit (the case number).
# Anything else — civil, appellate, MDL, specialty — defaults to deadlines-on.
_CRIMINAL_DOCKET_TYPES = re.compile(
    r"-(?:cr|cm|cmc|po|mj-cr)-\d", re.IGNORECASE,
)


def _docket_implies_deadlines(docket_number: str | None) -> Optional[bool]:
    """Map a federal docket number to a deadline-tracking default.

    Returns False for routine criminal dockets, True for everything else
    (civil, appellate, specialty courts), or None if the number is absent
    so the caller can fall back to a global default.
    """
    if not docket_number:
        return None
    return not bool(_CRIMINAL_DOCKET_TYPES.search(docket_number))


def _compact_recap_documents(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Compact projection of an entry's recap_documents for storage.

    Keeps the fields we need at emit time (status flags + URLs + position
    info) plus ``plain_text`` so the summary pipeline can read text without
    re-fetching the entry from CL — drops other CL bookkeeping we don't
    render. Ordered main-doc-first, then attachments by number, so calendar
    descriptions list documents in PACER's natural order.
    """
    out: list[dict[str, Any]] = []
    for rd in entry.get("recap_documents") or []:
        out.append({
            "id": rd.get("id"),
            "document_number": rd.get("document_number"),
            "attachment_number": rd.get("attachment_number"),
            "description": (rd.get("description") or "").strip() or None,
            "is_available": bool(rd.get("is_available")),
            "is_sealed": bool(rd.get("is_sealed")),
            "filepath_ia": rd.get("filepath_ia") or None,
            "filepath_local": rd.get("filepath_local") or None,
            "plain_text": (rd.get("plain_text") or "").strip() or None,
        })

    def _key(d: dict[str, Any]) -> tuple[int, int]:
        # Sort attachment_number=None / 0 (the main doc) before numbered
        # attachments. document_number normally matches across rows on the
        # same entry, but treat it as the primary sort just in case.
        try:
            dn = int(d.get("document_number") or 0)
        except (TypeError, ValueError):
            dn = 0
        try:
            an = int(d.get("attachment_number") or 0)
        except (TypeError, ValueError):
            an = 0
        return (dn, an)

    out.sort(key=_key)
    return out


def fingerprint_entry(entry: dict[str, Any]) -> str:
    """Hash that changes when meaningful entry state changes.

    We include PDF availability and presence of extracted text so that an
    entry whose PDF was missing on a prior sync is re-processed once the
    PDF (or its OCR text) shows up.
    """
    parts = [
        entry.get("description") or "",
        entry.get("short_description") or "",
        entry.get("date_filed") or "",
    ]
    for rd in entry.get("recap_documents", []) or []:
        parts.append(rd.get("description") or "")
        parts.append(str(bool(rd.get("is_available"))))
        parts.append(str(bool(rd.get("is_sealed"))))
        parts.append("1" if (rd.get("plain_text") or "").strip() else "0")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


# A pulled PDF is worth the cost when the on-docket text doesn't include the
# specifics. We only fetch when the entry looks like a hearing notice but its
# description is essentially empty.
_DETAIL_HINTS = re.compile(
    r"\b(\d{1,2}:\d{2}\s*(AM|PM)?|courtroom|judge|via\s+(zoom|teleconf|video))",
    re.IGNORECASE,
)

# CL appends a clerk-side timestamp like "[Entered: 05/06/2026 01:51 PM]" or
# "(Entered: 05/06/2026)" to most docket-entry descriptions. The HH:MM there
# is when the clerk filed it, NEVER the hearing time. Without stripping it,
# _DETAIL_HINTS matches on every such entry and we skip the PDF that does
# carry the actual hearing time.
_ENTERED_FOOTER = re.compile(r"[\(\[]Entered:[^\)\]]*[\)\]]", re.IGNORECASE)

# Orders granting a *scheduling/hearing* motion are a trap: the brief
# description references the underlying motion only by docket position, so
# the substance — what kind of hearing was being requested — lives in the
# motion entry and the order's PDF, not in the order's text. Even when the
# order inlines a date+time (which would otherwise let _DETAIL_HINTS
# short-circuit the PDF fetch), we still need the PDF body because it
# typically lists ALL the dates the order set, including ones not echoed
# in the brief description (e.g. the CIPA conference itself).
#
# Limited to scheduling/hearing motions on purpose. Orders granting
# substantive motions ("granting 50 Motion to Suppress / Dismiss / Compel")
# don't move the docket and don't justify the extra LLM tokens.
_ORDER_GRANTS_SCHEDULING_MOTION = re.compile(
    r"\bgranting\b[^.]{0,80}?\bMotion\s+"
    r"(?:for\s+Hearing|for\s+Continuance|for\s+Status\s+Conference|"
    r"to\s+(?:Continue|Reschedule|Vacate|Set|Schedule|Adjourn))",
    re.IGNORECASE,
)

# Cross-reference pattern: PACER-style "ORDER granting 65 Motion ..." or
# "DENYING 42 Motion" or just "see [12]". The verb tells us this is a
# reference to another docket entry we may have already seen; the bare
# number is the docket-position number (entries.entry_number). We only
# resolve refs we've already stored, so this is purely a context boost
# at the LLM call — no extra CL traffic.
_DOCKET_REF = re.compile(
    r"\b(?:granting|denying|grants|denies|granted|denied|ruling\s+on|"
    r"see|re|response\s+to)\s+(?:in\s+part\s+)?(?:\[)?(\d{1,4})(?:\])?\b",
    re.IGNORECASE,
)


def _extract_docket_refs(entry: dict[str, Any]) -> list[int]:
    """Pull docket-position numbers referenced by this entry's description.

    Returns a deduplicated list of integers. We strip the "(Entered: ...)"
    footer first so a clerk timestamp's day-of-month doesn't get parsed as
    a referenced motion.
    """
    desc = (entry.get("description") or "") + " " + (entry.get("short_description") or "")
    desc = _ENTERED_FOOTER.sub("", desc)
    seen: list[int] = []
    for m in _DOCKET_REF.finditer(desc):
        n = int(m.group(1))
        if n not in seen:
            seen.append(n)
    return seen


def _needs_pdf(entry: dict[str, Any]) -> bool:
    desc = (entry.get("description") or "") + " " + (entry.get("short_description") or "")
    desc = _ENTERED_FOOTER.sub("", desc)
    if _ORDER_GRANTS_SCHEDULING_MOTION.search(desc):
        return True
    if _DETAIL_HINTS.search(desc):
        return False
    # No specific details inline — go fetch the PDFs.
    return True


def _is_fetchable(rd: dict[str, Any]) -> bool:
    """True if this recap_document points at a real PDF we could pull text from.

    Paperless orders, minute entries, and entries whose document hasn't been
    contributed to RECAP yet all show up as recap_documents with
    ``is_available: false`` and no ``filepath_local`` / ``filepath_ia``.
    They have no body to fetch — only a description in the docket text — so
    we should never try to download them.
    """
    if rd.get("is_sealed"):
        return False
    # CL-extracted plain_text is itself a fetchable source.
    if (rd.get("plain_text") or "").strip():
        return True
    if not rd.get("is_available"):
        return False
    return bool(rd.get("filepath_local") or rd.get("filepath_ia"))


def _validate_action_dial_in(action: dict[str, Any]) -> None:
    """Verify action.dial_in resolves; on failure move it to notes.

    LLM URL extraction occasionally swallows trailing prose into the URL when
    the source text has no separator. We do a one-step parent-path repair;
    if that fails too, we keep the broken text accessible to the human reader
    by appending it to ``notes`` and clearing ``dial_in``.
    """
    original = (action.get("dial_in") or "").strip()
    if not original:
        return
    repaired = url_validator.validate_url(original)
    if repaired == original:
        return
    if repaired:
        action["dial_in"] = repaired
        return
    # Validation failed entirely — preserve the URL text in notes so a human
    # can salvage it.
    addendum = f"Dial-in (unverified): {original}"
    existing_notes = action.get("notes")
    action["notes"] = (
        f"{existing_notes}\n\n{addendum}" if existing_notes else addendum
    )
    action["dial_in"] = None


def _local_to_utc(date_str: str, time_str: Optional[str], tz: str) -> Optional[str]:
    if not date_str:
        return None
    if time_str:
        dt = datetime.fromisoformat(f"{date_str}T{time_str}")
    else:
        # date-only — treat as midnight local; the calendar layer turns this
        # into an all-day event.
        dt = datetime.fromisoformat(f"{date_str}T00:00")
    dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(timezone.utc).isoformat()


# Filing deadlines without an explicit clock time fire at end-of-business
# court time, so calendar reminders give the watcher a useful "check PACER
# tonight" anchor rather than a midnight alert nobody acts on.
DEADLINE_DEFAULT_LOCAL_TIME = "17:00"


def _deadline_local_to_utc(
    date_str: str, time_str: Optional[str], tz: str
) -> Optional[str]:
    """Same as _local_to_utc but defaults missing times to 17:00 court-local
    rather than midnight. Used by the deadline path so the stored UTC
    timestamp already reflects end-of-business semantics."""
    return _local_to_utc(date_str, time_str or DEADLINE_DEFAULT_LOCAL_TIME, tz)


def _mark_held_date_matches(
    action: dict[str, Any], existing: dict[str, Any], tolerance_days: int = 2
) -> bool:
    """True if a MARK_HELD action's date is close enough to the existing hearing.

    Returns True when:
    - the action carries no local_date (older LLM responses; trust the match)
    - the existing hearing has no starts_at_utc (we have no date to compare)
    - the dates are within ``tolerance_days`` of each other

    A 2-day window covers same-week reschedules where the minute entry might
    be filed a day or two after the hearing; anything wider is almost certainly
    the LLM stapling a held proceeding onto the wrong logical hearing.
    """
    action_date_str = action.get("local_date")
    existing_starts = existing.get("starts_at_utc")
    if not action_date_str or not existing_starts:
        return True
    try:
        from datetime import date
        existing_date = datetime.fromisoformat(existing_starts).date()
        action_date = date.fromisoformat(action_date_str)
    except (ValueError, TypeError):
        return True
    return abs((existing_date - action_date).days) <= tolerance_days


def _default_duration(hearing_type: str | None, time_set: bool) -> int:
    if not time_set:
        return 0  # all-day
    return {
        "sentencing": 90,
        "trial": 240,
        "oral_argument": 60,
        "evidentiary_hearing": 120,
        "motion_hearing": 60,
        "plea_hearing": 45,
        "change_of_plea": 45,
        "arraignment": 30,
        "initial_appearance": 30,
        "status_conference": 30,
        "telephonic_conference": 30,
    }.get(hearing_type or "", 60)


class CaseSyncer:
    def __init__(self, cl: CourtListener, store: Store):
        self.cl = cl
        self.store = store

    # --- shared helpers (used by polling sync_case AND the webhook server) ---

    def resolve_extract_deadlines(
        self, case: CaseConfig, docket_id: int | None = None,
    ) -> bool:
        """Decide whether to extract filing deadlines for this case/docket.

        ``case.extract_deadlines=True`` is a force-on override that always
        wins. Otherwise we look at the docket number(s): routine criminal
        dockets default OFF, everything else defaults ON. With ``docket_id``
        set, the decision is per-docket (used per-entry); without, we
        aggregate across the case's dockets (used for the end-of-case
        verify pass — any one civil docket flips the case to ON).

        Falls back to True when no docket metadata is cached yet, since
        civil-leaning is the safer default for the unknown case.
        """
        if case.extract_deadlines:
            return True
        docket_ids = [docket_id] if docket_id is not None else case.dockets
        saw_classifiable_off = False
        for did in docket_ids:
            meta = self.store.get_docket_meta(did) or {}
            decision = _docket_implies_deadlines(meta.get("docket_number"))
            if decision is True:
                return True
            if decision is False:
                saw_classifiable_off = True
        return not saw_classifiable_off

    def ensure_docket_cached(self, docket_id: int) -> dict[str, Any]:
        """Return cached docket meta, fetching from CL exactly once if missing.

        Webhook payloads don't include parent-docket metadata, so the first
        time we see a docket via webhook we have to do one /dockets/ GET to
        learn its court_id (and therefore its timezone). After that everything
        is cached and incoming webhooks make zero CL calls.
        """
        meta = self.store.get_docket_meta(docket_id)
        if meta and meta.get("court_id"):
            return meta
        docket = self.cl.get_docket(docket_id)
        self.store.upsert_docket_meta(
            docket_id,
            {
                "court_id": docket.get("court_id"),
                "docket_number": docket.get("docket_number"),
                "case_name": docket.get("case_name"),
                "absolute_url": docket.get("absolute_url"),
                "date_last_filing": docket.get("date_last_filing"),
            },
        )
        self._ensure_court(docket.get("court_id") or "")
        return self.store.get_docket_meta(docket_id) or {}

    def process_entry(
        self,
        case: CaseConfig,
        docket_id: int,
        entry: dict[str, Any],
        *,
        stats: Optional[dict[str, int]] = None,
    ) -> bool:
        """End-to-end processing for one entry: filter, LLM extract, store.

        Used by both polling ``sync_case`` and the webhook receiver, so the
        two paths produce identical hearing rows.
        """
        if stats is None:
            stats = {"entries_seen": 0, "entries_processed": 0, "actions": 0}

        eid = entry["id"]
        fp = fingerprint_entry(entry)
        if self.store.entry_seen(docket_id, eid, fp):
            return False

        meta = self.ensure_docket_cached(docket_id)
        court_id = meta.get("court_id") or ""
        tz = tz_for(court_id)

        processed = self._handle_entry(case, docket_id, court_id, tz, entry, stats)
        # Persist the full description body when the entry is either:
        #   (a) hearing/deadline-relevant — already LLM-processed, body is
        #       needed for `get_recent_relevant_entries` cross-entry context
        #       and emit-time description rendering; or
        #   (b) operative-pleading or disposition — needed so the summary
        #       pipeline can find these entries locally instead of
        #       re-fetching the same docket-entries pages from CL right
        #       after sync wrote them down.
        # Everything else (notices, briefs, attorney appearances, etc.) gets
        # a fingerprint-only stub: dedup keeps working, but no dead-weight
        # body text.
        summary_relevant = (
            summary_mod.is_operative_pleading(entry)
            or summary_mod.is_disposition(entry)
        )
        store_full = processed or summary_relevant
        self.store.mark_entry(
            docket_id,
            eid,
            entry.get("date_modified") or "",
            fp,
            date_filed=entry.get("date_filed"),
            entry_number=entry.get("entry_number"),
            description=entry.get("description") if store_full else None,
            short_description=entry.get("short_description") if store_full else None,
            recap_documents=_compact_recap_documents(entry) if store_full else None,
        )
        # Advance the docket's date_modified to this entry's value if newer.
        # date_modified is the docket-level short-circuit watermark — the
        # polling path sets it from the parent docket at end-of-loop, but
        # the webhook path never sees the parent docket per delivery, so
        # without this conditional bump webhook-only deployments would
        # never short-circuit unchanged dockets on subsequent polls.
        entry_dm = entry.get("date_modified") or ""
        if entry_dm:
            self.store.bump_docket_last_modified(docket_id, entry_dm)
        # Same idea for the index page's "Last filing" date: webhook
        # deliveries don't refetch the parent docket, so CL's
        # ``date_last_filing`` would lag the entry we just processed.
        # Use the entry's own ``date_filed`` as a forward-only stand-in;
        # the next docket fetch overwrites it with CL's authoritative
        # value via ``upsert_docket_meta``.
        entry_df = entry.get("date_filed") or ""
        if entry_df:
            self.store.bump_docket_last_filing(docket_id, entry_df)
        # Flag the case_summaries row stale when this entry looks like an
        # operative pleading (superseding indictment, amended complaint,
        # etc.) or a disposition (judgment, plea agreement, verdict,
        # dismissal, dispositive memo). These are exactly the entries that
        # change the substantive answer to "what is this case about, and
        # where does it stand?" — so the next sync/webhook auto-emit will
        # regenerate the summary before re-rendering the index. We check
        # this independently of `processed` because operative pleadings
        # and judgments rarely match the hearing-relevance regex but are
        # the most important signals for the summary.
        if summary_relevant:
            self.store.mark_summary_stale(case.case_id, docket_id)
        return processed

    # --- polling entry point ---

    def sync_case(self, case: CaseConfig) -> dict[str, int]:
        stats = {
            "dockets_skipped": 0,
            "entries_seen": 0,
            "entries_processed": 0,
            "actions": 0,
            "verified": 0,
        }
        for docket_id in case.dockets:
            log.info("Syncing docket %s for case %s", docket_id, case.case_id)
            docket = self.cl.get_docket(docket_id)
            docket_mod = docket.get("date_modified") or ""
            last_mod = self.store.docket_last_modified(docket_id)
            if last_mod and docket_mod and docket_mod <= last_mod:
                log.info(
                    "docket %s unchanged since %s; skipping (no API/LLM calls)",
                    docket_id, last_mod,
                )
                # We've already paid the get_docket call above; capturing
                # date_last_filing here costs nothing extra and is the only
                # path that populates the column for quiet dockets after
                # the column was added.
                if docket.get("date_last_filing"):
                    self.store.bump_docket_last_filing(
                        docket_id, docket["date_last_filing"],
                    )
                stats["dockets_skipped"] += 1
                continue

            # Persist meta + court so process_entry has what it needs.
            self.store.upsert_docket_meta(
                docket_id,
                {
                    "court_id": docket.get("court_id"),
                    "docket_number": docket.get("docket_number"),
                    "case_name": docket.get("case_name"),
                    "absolute_url": docket.get("absolute_url"),
                    "date_last_filing": docket.get("date_last_filing"),
                },
            )
            self._ensure_court(docket.get("court_id") or "")
            cutoff = self.store.latest_entry_modified(docket_id)

            iterated_ok = True
            try:
                for entry in self.cl.iter_entries(docket_id, modified_after=cutoff):
                    stats["entries_seen"] += 1
                    self.process_entry(case, docket_id, entry, stats=stats)
                    with self.store.tx() as _:
                        pass  # commit per entry so partial progress sticks
            except Exception:
                iterated_ok = False
                raise
            finally:
                if iterated_ok and docket_mod:
                    self.store.set_docket_last_modified(docket_id, docket_mod)
                    with self.store.tx() as _:
                        pass

        # End-of-case sweeps:
        #   1. Confidence pass — for each future scheduled hearing, ask the
        #      LLM whether recent docket entries support it staying on the
        #      calendar. Catches missed reschedules/cancellations and the
        #      hallucination class (rows extracted from tangentially-related
        #      entries with no actual scheduling order behind them).
        #   2. Auto-held — any 'scheduled' row whose start time is in the
        #      past flips to 'held'. starts_at_utc is already UTC, so the
        #      comparison is timezone-free.
        # Verify first so a "rescheduled to past date" outcome can't get
        # double-flipped to held by the second sweep.
        # _verify_scheduled_hearings audits BOTH future and past 'scheduled'
        # rows. There is no separate auto-held sweep: a past hearing is only
        # marked 'held' when the LLM cites a minute entry / verdict /
        # transcript / judgment-after as evidence of occurrence. Past-dated
        # rows without that evidence stay 'scheduled' — accurately
        # reflecting "the docket has not confirmed this happened" rather
        # than guessing 'held' because the calendar date passed. (Trials
        # get continued or vacated by plea without an explicit cancellation
        # entry; the auto-held heuristic was wrong by default for them.)
        stats["verified"] = self._verify_scheduled_hearings(case)
        # Run dedupe AFTER verify so any RESCHEDULE / CANCEL from verify
        # gets a chance to clear concurrency before we ask the LLM to
        # resolve it.
        stats["deduped"] = self._dedupe_concurrent_hearings(case)
        if self.resolve_extract_deadlines(case):
            stats["deadlines_verified"] = self._verify_pending_deadlines(case)
            stats["auto_passed"] = self._auto_mark_passed_stale(case.case_id)
        return stats

    def _verify_scheduled_hearings(self, case: CaseConfig) -> int:
        """Audit non-terminal hearings against recent docket entries.

        Returns the number of hearings whose row was modified by the audit.

        Scope: every ``scheduled`` row (past and future) plus every PAST
        ``cancelled`` row. The action grid:

        - For ``scheduled`` rows the LLM returns CONFIRM (no-op),
          RESCHEDULE, CANCEL, MARK_HELD, DELETE_HALLUCINATION, or
          UNCLEAR. Past-dated rows require explicit evidence
          (minute entry / verdict / transcript / judgment-after) for
          MARK_HELD — date-passed alone is not enough. Trials and other
          hearings can pass their scheduled date without an explicit
          cancellation entry, so UNCLEAR (no change) is the correct
          default for past rows the docket doesn't confirm.

        - For PAST ``cancelled`` rows the LLM additionally returns
          UNCANCEL when the cancellation isn't supported by docket
          evidence (no vacatur entry, no plea agreement, no dismissal —
          just an absence of activity that a prior LLM misread as
          cancellation). The caller flips the row back to ``scheduled``
          so the next sync can MARK_HELD it on real evidence or leave it
          UNCLEAR. This catches the inverse-Moucka failure mode where a
          live trial got falsely marked cancelled.

          Future cancelled rows are NOT verified — a deliberately
          cancelled future hearing should stay cancelled until something
          actively un-cancels it.
        """
        from . import llm as llm_mod

        now_iso = datetime.now(timezone.utc).isoformat()
        rows = self.store.conn.execute(
            """
            SELECT * FROM hearings
            WHERE case_id=?
              AND starts_at_utc IS NOT NULL
              AND (
                status='scheduled'
                OR (status='cancelled' AND starts_at_utc < ?)
              )
            """,
            (case.case_id, now_iso),
        ).fetchall()
        if not rows:
            return 0

        n_changed = 0
        for r in rows:
            hearing = dict(r)
            try:
                hearing["source_entry_ids"] = json.loads(
                    hearing.get("source_entry_ids") or "[]"
                )
            except (json.JSONDecodeError, TypeError):
                hearing["source_entry_ids"] = []

            docket_id = hearing.get("docket_id")
            if not docket_id:
                continue
            meta = self.ensure_docket_cached(docket_id)
            court_id = meta.get("court_id") or ""
            tz = tz_for(court_id)

            recent = self.store.get_recent_relevant_entries(
                docket_id, "9999-12-31T00:00:00", limit=15,
            )
            action = llm_mod.verify_hearing(
                case_name=case.name,
                court_id=court_id,
                court_tz=tz,
                hearing=hearing,
                recent_entries=recent,
            )
            if self._apply_verify_action(case, docket_id, tz, hearing, action):
                n_changed += 1
        if n_changed:
            self.store.conn.commit()
        return n_changed

    def _apply_verify_action(
        self,
        case: CaseConfig,
        docket_id: int,
        tz: str,
        hearing: dict[str, Any],
        action: dict[str, Any],
    ) -> bool:
        """Apply a single verify-pass action to the hearing row.

        Returns True if the row changed. Uses the same upsert path as the
        regular extraction pipeline so source_entry_ids and audit fields
        stay consistent.
        """
        atype = (action.get("type") or "UNCLEAR").upper()
        if atype in ("CONFIRM", "UNCLEAR"):
            return False

        merged = dict(hearing)
        sources = list(hearing.get("source_entry_ids") or [])

        if atype == "CANCEL":
            merged.update(status="cancelled")
            note = action.get("reason") or "Cancelled per recent docket entries"
            merged["notes"] = (
                (hearing.get("notes") or "") + f"\n\n[verify-pass] {note}"
            ).strip()
        elif atype == "DELETE_HALLUCINATION":
            # Don't actually delete — preserve the audit trail by marking
            # cancelled with an explanatory note. Renderers skip cancelled
            # rows so the calendar shows the right thing.
            merged.update(status="cancelled")
            note = action.get("reason") or "No docket entry supports this hearing"
            merged["notes"] = (
                (hearing.get("notes") or "") + f"\n\n[verify-pass] {note}"
            ).strip()
        elif atype == "MARK_HELD":
            merged.update(status="held")
        elif atype == "UNCANCEL":
            # Issued for a 'cancelled' row whose cancellation is not
            # supported by an explicit docket entry. Revert to
            # 'scheduled' so the next verify pass can MARK_HELD it on
            # real evidence (or leave it UNCLEAR if the outcome still
            # isn't documented). The McGonigal-shape regression — a
            # past trial row marked cancelled even though the case
            # continued to be actively briefed after the trial date —
            # is the canonical case.
            merged.update(status="scheduled")
            note = action.get("reason") or (
                "Cancellation not supported by docket; reverted to scheduled"
            )
            merged["notes"] = (
                (hearing.get("notes") or "") + f"\n\n[verify-pass] {note}"
            ).strip()
        elif atype == "RESCHEDULE":
            local_date = action.get("local_date")
            local_time = action.get("local_time")
            if not local_date:
                log.warning(
                    "verify RESCHEDULE without local_date: case=%s key=%r",
                    case.case_id, hearing.get("hearing_key"),
                )
                return False
            convert_tz = hearing.get("timezone") or tz
            merged["starts_at_utc"] = _local_to_utc(
                local_date, local_time, convert_tz
            )
            note = action.get("reason") or "Rescheduled per recent docket entries"
            merged["notes"] = (
                (hearing.get("notes") or "") + f"\n\n[verify-pass] {note}"
            ).strip()
        else:
            log.warning(
                "verify-pass unknown action type %s for case=%s key=%r",
                atype, case.case_id, hearing.get("hearing_key"),
            )
            return False

        log.info(
            "verify-pass applying %s case=%s key=%r reason=%s",
            atype, case.case_id, hearing.get("hearing_key"),
            (action.get("reason") or "")[:120],
        )
        merged["source_entry_ids"] = sources
        self.store.upsert_hearing(merged)
        return True

    def _dedupe_concurrent_hearings(self, case: CaseConfig) -> int:
        """Resolve future scheduled hearings sharing (docket_id, starts_at_utc).

        A single court cannot hold two hearings on one docket at the same
        date and time, so equal ``(docket_id, starts_at_utc)`` across
        ``status='scheduled'`` rows is a signal the per-entry extractor
        split one logical event across keys (e.g. a stipulation said
        "Hearing on Motion for Summary Judgment" and the signed order
        called it "Motion Hearing" — same slot, two ``hearing_key``s).
        The verify pass operates on one row in isolation and has no view
        of sibling future hearings; this sweep closes that gap.

        For each cluster the LLM returns MERGE_INTO (cancel duplicates,
        merge their source_entry_ids into the target row) or KEEP_BOTH /
        UNCLEAR (no-op — used for the rare case where two distinct
        proceedings really are scheduled back-to-back at the same time).
        Returns the count of rows cancelled by merge.
        """
        from . import llm as llm_mod

        clusters = self.store.find_concurrent_hearing_clusters(case.case_id)
        if not clusters:
            return 0

        n_merged = 0
        for cluster in clusters:
            # find_concurrent_hearing_clusters guarantees docket_id NOT
            # NULL via its SQL filter — no defensive check needed here.
            docket_id = cluster[0]["docket_id"]
            meta = self.ensure_docket_cached(docket_id)
            court_id = meta.get("court_id") or ""
            tz = tz_for(court_id)
            recent = self.store.get_recent_relevant_entries(
                docket_id, "9999-12-31T00:00:00", limit=15,
            )
            action = llm_mod.resolve_duplicate_hearings(
                case_name=case.name,
                court_id=court_id,
                court_tz=tz,
                cluster=cluster,
                recent_entries=recent,
            )
            n_merged += self._apply_dedupe_action(case, cluster, action)

        if n_merged:
            self.store.conn.commit()
        return n_merged

    def _apply_dedupe_action(
        self,
        case: CaseConfig,
        cluster: list[dict[str, Any]],
        action: dict[str, Any],
    ) -> int:
        """Apply one MERGE_INTO / KEEP_BOTH / UNCLEAR action to a cluster.

        Returns the number of rows that were cancelled (i.e. merged into
        the target).
        """
        atype = (action.get("type") or "UNCLEAR").upper()
        if atype != "MERGE_INTO":
            log.info(
                "dedupe: keys=%s -> %s reason=%r",
                [h.get("hearing_key") for h in cluster], atype,
                (action.get("reason") or "")[:120],
            )
            return 0

        target_key = action.get("target_key")
        target = next(
            (h for h in cluster if h.get("hearing_key") == target_key), None,
        )
        if not target:
            log.warning(
                "dedupe MERGE_INTO target_key %r not in cluster %s: leaving cluster alone",
                target_key, [h.get("hearing_key") for h in cluster],
            )
            return 0

        # Merge source_entry_ids from all duplicates into the target.
        merged_sources: list[Any] = list(target.get("source_entry_ids") or [])
        seen: set[Any] = set(merged_sources)
        for dup in cluster:
            if dup.get("hearing_key") == target_key:
                continue
            for sid in dup.get("source_entry_ids") or []:
                if sid not in seen:
                    seen.add(sid)
                    merged_sources.append(sid)
        target["source_entry_ids"] = merged_sources
        self.store.upsert_hearing(target)

        # Cancel each duplicate with an explanatory note pointing back to
        # the target. Renderers skip cancelled rows so the calendar shows
        # the right thing; the row is preserved for the audit trail.
        n_cancelled = 0
        reason = action.get("reason") or f"Duplicate of {target_key}"
        for dup in cluster:
            if dup.get("hearing_key") == target_key:
                continue
            dup_row = dict(dup)
            dup_row["status"] = "cancelled"
            dup_row["notes"] = (
                (dup_row.get("notes") or "")
                + f"\n\n[dedupe] Merged into {target_key}: {reason}"
            ).strip()
            self.store.upsert_hearing(dup_row)
            n_cancelled += 1

        log.info(
            "dedupe: merged %d hearing(s) into %r on docket %s (case=%s)",
            n_cancelled, target_key, target.get("docket_id"), case.case_id,
        )
        return n_cancelled

    def _verify_pending_deadlines(self, case: CaseConfig) -> int:
        """Audit every future pending deadline against recent docket entries.

        Mirrors :meth:`_verify_scheduled_hearings` for filing deadlines:
        catches missed extensions, vacaturs, and the hallucination class.
        Returns the count of rows modified.
        """
        from . import llm as llm_mod

        now_iso = datetime.now(timezone.utc).isoformat()
        rows = self.store.conn.execute(
            "SELECT * FROM deadlines "
            "WHERE case_id=? AND status='pending' "
            "AND due_at_utc IS NOT NULL AND due_at_utc >= ?",
            (case.case_id, now_iso),
        ).fetchall()
        if not rows:
            return 0

        n_changed = 0
        for r in rows:
            d = dict(r)
            try:
                d["source_entry_ids"] = json.loads(d.get("source_entry_ids") or "[]")
            except (json.JSONDecodeError, TypeError):
                d["source_entry_ids"] = []

            docket_id = d.get("docket_id")
            if not docket_id:
                continue
            meta = self.ensure_docket_cached(docket_id)
            court_id = meta.get("court_id") or ""
            tz = tz_for(court_id)

            recent = self.store.get_recent_relevant_entries(
                docket_id, "9999-12-31T00:00:00", limit=15,
            )
            action = llm_mod.verify_deadline(
                case_name=case.name,
                court_id=court_id,
                court_tz=tz,
                deadline=d,
                recent_entries=recent,
            )
            if self._apply_verify_deadline_action(case, docket_id, tz, d, action):
                n_changed += 1
        if n_changed:
            self.store.conn.commit()
        return n_changed

    def _apply_verify_deadline_action(
        self,
        case: CaseConfig,
        docket_id: int,
        tz: str,
        deadline: dict[str, Any],
        action: dict[str, Any],
    ) -> bool:
        atype = (action.get("type") or "UNCLEAR").upper()
        if atype in ("CONFIRM", "UNCLEAR"):
            return False

        merged = dict(deadline)
        sources = list(deadline.get("source_entry_ids") or [])

        if atype == "CANCEL":
            merged["status"] = "cancelled"
            note = action.get("reason") or "Vacated per recent docket entries"
            merged["notes"] = (
                (deadline.get("notes") or "") + f"\n\n[verify-pass] {note}"
            ).strip()
        elif atype == "DELETE_HALLUCINATION":
            merged["status"] = "cancelled"
            note = action.get("reason") or "No docket entry supports this deadline"
            merged["notes"] = (
                (deadline.get("notes") or "") + f"\n\n[verify-pass] {note}"
            ).strip()
        elif atype == "MARK_FILED":
            merged["status"] = "met"
        elif atype == "RESCHEDULE":
            local_date = action.get("local_date")
            if not local_date:
                log.warning(
                    "verify_deadline RESCHEDULE without local_date: case=%s key=%r",
                    case.case_id, deadline.get("deadline_key"),
                )
                return False
            convert_tz = deadline.get("timezone") or tz
            merged["due_at_utc"] = _deadline_local_to_utc(
                local_date, action.get("local_time"), convert_tz
            )
            note = action.get("reason") or "Extended per recent docket entries"
            merged["notes"] = (
                (deadline.get("notes") or "") + f"\n\n[verify-pass] {note}"
            ).strip()
        else:
            log.warning(
                "verify_deadline unknown action type %s: case=%s key=%r",
                atype, case.case_id, deadline.get("deadline_key"),
            )
            return False

        log.info(
            "verify_deadline applying %s case=%s key=%r reason=%s",
            atype, case.case_id, deadline.get("deadline_key"),
            (action.get("reason") or "")[:120],
        )
        merged["source_entry_ids"] = sources
        self.store.upsert_deadline(merged)
        return True

    def _auto_mark_passed_stale(self, case_id: str) -> int:
        """Flip past-dated 'pending' deadlines to 'passed'. Returns count flipped.

        Mirrors :meth:`_auto_mark_held_stale` for hearings: ``due_at_utc`` is
        UTC and the server clock is UTC, so the comparison is timezone-free.
        If a later entry shows the filing was actually made, MARK_FILED on a
        subsequent sync flips the row to 'met'; otherwise it stays 'passed'
        and the operator knows to go check PACER.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        rows = self.store.conn.execute(
            "SELECT case_id, deadline_key, due_at_utc FROM deadlines "
            "WHERE case_id=? AND status='pending' "
            "AND due_at_utc IS NOT NULL AND due_at_utc < ?",
            (case_id, now_iso),
        ).fetchall()
        n = 0
        for r in rows:
            log.info(
                "auto-marking deadline passed: case=%s key=%r due=%s",
                r["case_id"], r["deadline_key"], r["due_at_utc"],
            )
            self.store.conn.execute(
                "UPDATE deadlines SET status='passed', last_updated=? "
                "WHERE case_id=? AND deadline_key=?",
                (now_iso, r["case_id"], r["deadline_key"]),
            )
            n += 1
        if n:
            self.store.conn.commit()
        return n

    def _insert_terminal_hearing(
        self,
        case: CaseConfig,
        docket_id: int,
        tz: str,
        entry: dict[str, Any],
        action: dict[str, Any],
        *,
        status: str,
        prev_sources: list[int],
    ) -> None:
        """Insert a brand-new hearing directly into a terminal status.

        Used when CANCEL or MARK_HELD targets a hearing_key that isn't in
        the store — typically because its original scheduling entry was
        filtered out by the prefilter, but a later memo/minute entry
        explicitly tells us a hearing existed and is now adjourned/held.
        Preserves the audit trail without depending on the LLM emitting
        an ADD-then-CANCEL pair.
        """
        local_date = action.get("local_date")
        local_time = action.get("local_time")
        starts_utc = _local_to_utc(local_date, local_time, tz)
        duration = action.get("duration_minutes") or _default_duration(
            action.get("hearing_type"), bool(local_time)
        )
        self.store.upsert_hearing({
            "case_id": case.case_id,
            "hearing_key": action["hearing_key"],
            "title": action.get("title")
                or action["hearing_key"].replace("-", " ").title(),
            "starts_at_utc": starts_utc,
            "duration_minutes": duration,
            "timezone": tz,
            "location": action.get("location"),
            "judge": action.get("judge"),
            "notes": action.get("notes"),
            "dial_in": action.get("dial_in"),
            "status": status,
            "significance": action.get("significance") or "major",
            "gcal_event_id": None,
            "docket_id": docket_id,
            "source_entry_ids": prev_sources,
        })

    # --- per-entry logic ---

    def _ensure_court(self, court_id: str) -> None:
        """Cache court metadata (citation_string) on first sight."""
        if not court_id:
            return
        if self.store.get_court_citation(court_id) is not None:
            return
        try:
            c = self.cl.get_court(court_id)
        except Exception as e:
            log.warning("court fetch failed id=%s: %s", court_id, e)
            return
        self.store.upsert_court(
            court_id,
            c.get("citation_string"),
            c.get("short_name"),
            c.get("full_name"),
        )

    def _handle_entry(
        self,
        case: CaseConfig,
        docket_id: int,
        court_id: str,
        tz: str,
        entry: dict[str, Any],
        stats: dict[str, int],
    ) -> bool:
        """True iff the entry made it through the regex filter and reached the LLM."""
        want_deadlines = self.resolve_extract_deadlines(case, docket_id)
        if not is_extractable(entry, want_deadlines=want_deadlines):
            log.debug("entry %s skipped by regex pre-filter", entry.get("id"))
            return False
        stats["entries_processed"] += 1

        pdf_texts = self._maybe_fetch_pdfs(entry)
        # Restrict known-events context to siblings in the same court.
        # Parallel proceedings in different venues must not feed each other's
        # context — a "stay appellate proceedings" order in court B would
        # otherwise contaminate court A's events with bogus CANCEL actions.
        # Co-defendant dockets in the same court (multi-defendant criminal
        # cases) still aggregate correctly.
        known = self.store.get_hearings_in_court(case.case_id, court_id)
        referenced = self._resolve_docket_refs(docket_id, entry)
        known_deadlines = (
            self.store.get_deadlines_in_court(case.case_id, court_id)
            if want_deadlines else None
        )

        actions = llm.extract_actions(
            case_name=case.name,
            court_id=court_id,
            court_tz=tz,
            entry=entry,
            pdf_texts=pdf_texts,
            known_hearings=known,
            docket_id=docket_id,
            referenced_entries=referenced,
            known_deadlines=known_deadlines,
            extract_deadlines=want_deadlines,
        )

        for action in actions:
            atype = (action.get("type") or "").upper()
            stats["actions"] += 1
            if atype.endswith("_DEADLINE") or atype == "MARK_FILED":
                self._apply_deadline_action(case, docket_id, tz, entry, action)
            else:
                _validate_action_dial_in(action)
                self._apply_action(case, docket_id, tz, entry, action)

        return True

    def _resolve_docket_refs(
        self, docket_id: int, entry: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Look up entries that give the LLM context for the current entry.

        Combines two channels:

        1. *Explicit references* — when the entry text says "granting 65
           Motion" or "see [42]", we pull entry 65 / 42 by docket position.
        2. *Temporal proximity* — the most recent few hearing-relevant
           entries on the same docket. Many orders that schedule a hearing
           don't cite the underlying motion by docket number ("PAPERLESS
           Order Setting Telephonic Pretrial Conference..." with no "65"
           anywhere), so without this the LLM would title the hearing only
           from what the order says — losing details like "CIPA" that live
           in the originating motion just a few entries earlier.

        Misses (entries with no stored description, e.g. filter-failed
        notices) are silently skipped. Entries that appear in both channels
        are included once.
        """
        out: list[dict[str, Any]] = []
        seen_ids: set[int] = set()

        for n in _extract_docket_refs(entry):
            row = self.store.get_entry_by_number(docket_id, n)
            if row and (row.get("description") or row.get("short_description")):
                eid = row.get("entry_id")
                if eid not in seen_ids:
                    seen_ids.add(eid)
                    out.append({"entry_number": n, **row})

        recent = self.store.get_recent_relevant_entries(
            docket_id, entry.get("date_modified") or "", limit=5
        )
        for row in recent:
            eid = row.get("entry_id")
            if eid not in seen_ids:
                seen_ids.add(eid)
                out.append({"entry_number": row.get("entry_number"), **row})

        return out

    def _maybe_fetch_pdfs(self, entry: dict[str, Any]) -> list[str]:
        """Pull PDF text for the LLM. Called only when an entry's fingerprint
        flips, i.e. on first sight or when CL has changed something. PDFs are
        immutable once attached and we don't cache the extracted text — the
        rare fingerprint flip pays one extra round-trip."""
        if not _needs_pdf(entry):
            return []

        rds = entry.get("recap_documents") or []
        # Paperless / minute-entry placeholders: the entry has recap_document
        # rows but none of them point to anything fetchable.
        if rds and not any(_is_fetchable(rd) for rd in rds):
            log.debug(
                "entry %s has %d recap_document(s) but none are fetchable "
                "(paperless / not-yet-uploaded); skipping PDF stage",
                entry.get("id"), len(rds),
            )
            return []

        out: list[str] = []
        for rd in rds:
            doc_id = rd.get("id")
            if not doc_id:
                continue
            if not _is_fetchable(rd):
                # Single paperless doc inside an otherwise-fetchable entry —
                # skip it but don't bail on the entry as a whole.
                continue

            text = pdf.extract_text(rd)
            if text:
                out.append(text)
            else:
                if rd.get("is_sealed"):
                    log.info("recap_doc %s sealed; skipping", doc_id)
                elif not rd.get("is_available"):
                    log.info(
                        "recap_doc %s not yet on PACER (entry %s); "
                        "will retry next sync",
                        doc_id, entry.get("id"),
                    )
                else:
                    log.info(
                        "recap_doc %s available but text extraction yielded "
                        "nothing; install pdftoppm + tesseract for OCR fallback",
                        doc_id,
                    )
        return out

    def _apply_action(
        self,
        case: CaseConfig,
        docket_id: int,
        tz: str,
        entry: dict[str, Any],
        action: dict[str, Any],
    ) -> None:
        atype = (action.get("type") or "IGNORE").upper()
        if atype == "IGNORE":
            return

        key = action.get("hearing_key")
        if not key:
            log.warning("action without hearing_key: %s", action)
            return

        # ADD requires a date. Date-less ADDs come from entries that anticipate
        # a hearing without scheduling it (motion-for-hearing, plea agreement,
        # etc.) — they create ghost rows that never get a starts_at_utc and
        # never appear on the calendar. Drop them; the actual scheduling order
        # will come through later as its own entry.
        if atype == "ADD" and not action.get("local_date"):
            log.warning(
                "skipping date-less ADD: case=%s key=%r entry=%s "
                "(LLM should have IGNOREd; treating as such)",
                case.case_id, key, entry["id"],
            )
            return

        log.info(
            "applying %s case=%s key=%r entry=%s date=%s time=%s",
            atype, case.case_id, key, entry["id"],
            action.get("local_date"), action.get("local_time"),
        )

        existing = self.store.get_hearing(case.case_id, key)
        eid = entry["id"]
        prev_sources = list(existing.get("source_entry_ids", [])) if existing else []
        if eid not in prev_sources:
            prev_sources.append(eid)

        # docket_id is sticky after first ADD — sibling-docket entries can
        # touch a hearing (CANCEL, MARK_HELD, etc.) but the hearing's
        # canonical home docket (which feeds the description's case citation
        # and CL link) shouldn't drift to whichever docket touched it last.
        sticky_docket_id = existing.get("docket_id") if existing else docket_id

        if atype == "CANCEL":
            if existing:
                merged = dict(existing)
                merged.update(
                    status="cancelled",
                    notes=action.get("notes") or existing.get("notes"),
                    source_entry_ids=prev_sources,
                    docket_id=sticky_docket_id,
                )
                self.store.upsert_hearing(merged)
            elif action.get("local_date"):
                # Hearing was never in our store (its scheduling entry was
                # filtered out by the prefilter) but a memo endorsement is
                # adjourning it. Insert a fresh row directly into
                # 'cancelled' so the audit trail captures the event.
                self._insert_terminal_hearing(
                    case, docket_id, tz, entry, action,
                    status="cancelled", prev_sources=prev_sources,
                )
            else:
                log.warning(
                    "CANCEL on unknown key with no local_date: "
                    "case=%s key=%r entry=%s — dropping",
                    case.case_id, key, entry["id"],
                )
            return

        if atype == "MARK_HELD":
            if existing:
                # Date-proximity validation: if the action specifies a date
                # and it's > 2 days off from the existing hearing's date,
                # the LLM probably matched the wrong key. Reject so the
                # auto-held sweep can mark the real hearing later, and so
                # we don't poison the wrong row's source list.
                if not _mark_held_date_matches(action, existing):
                    log.warning(
                        "MARK_HELD date mismatch: case=%s key=%r "
                        "existing_starts=%s action_local_date=%s — rejecting",
                        case.case_id, key, existing.get("starts_at_utc"),
                        action.get("local_date"),
                    )
                    return
                merged = dict(existing)
                merged.update(
                    status="held",
                    source_entry_ids=prev_sources,
                    docket_id=sticky_docket_id,
                )
                self.store.upsert_hearing(merged)
            elif action.get("local_date"):
                # Same as CANCEL-on-unknown: a minute entry for a hearing
                # we never saw scheduled. Insert directly into 'held'.
                self._insert_terminal_hearing(
                    case, docket_id, tz, entry, action,
                    status="held", prev_sources=prev_sources,
                )
            else:
                log.warning(
                    "MARK_HELD on unknown key with no local_date: "
                    "case=%s key=%r entry=%s — dropping",
                    case.case_id, key, entry["id"],
                )
            return

        # ADD / RESCHEDULE / UPDATE_DETAILS — figure out the new field set.
        local_date = action.get("local_date")
        local_time = action.get("local_time")

        starts_utc = existing.get("starts_at_utc") if existing else None
        if local_date:
            # Convert using the existing tz when one is set — a reschedule
            # via a sibling docket shouldn't move the wall-clock time.
            convert_tz = (existing.get("timezone") if existing else None) or tz
            starts_utc = _local_to_utc(local_date, local_time, convert_tz)

        # 0 from the LLM means "not specified" — same as null. Falling through
        # to the default keeps zero-length blips out of subscribers' calendars.
        # We also treat an EXISTING duration of 0 as "unknown" so a follow-up
        # UPDATE_DETAILS can repair a hearing that was first inserted by an
        # entry that didn't know the duration (e.g. an ADD whose entry didn't
        # specify length, then a later UPDATE_DETAILS that also doesn't —
        # without this, the row stays pinned at 0 forever). For date-only
        # all-day events, _default_duration with time_set=False returns 0,
        # so we still land on 0 in that case — no regression.
        duration = action.get("duration_minutes") or None
        if duration is None and existing and atype != "RESCHEDULE":
            duration = existing.get("duration_minutes") or None
        if duration is None:
            duration = _default_duration(
                action.get("hearing_type"), bool(local_time)
            )

        # Timezone is sticky after first insertion. A hearing happens in one
        # courthouse; later entries from sibling dockets in different
        # timezones (e.g. an N.D. Cal entry referencing a D.C. Cir oral
        # argument) shouldn't shift the displayed tz, especially since the
        # stored UTC was computed from the original docket's tz.
        sticky_tz = existing.get("timezone") if existing else tz

        # Significance: stickier than other fields because the LLM rarely sets
        # it on UPDATE_DETAILS / RESCHEDULE. If the new action has it, prefer
        # that; otherwise keep what we already had.
        significance = action.get("significance") or (
            existing.get("significance") if existing else None
        )

        merged: dict[str, Any] = {
            "case_id": case.case_id,
            "hearing_key": key,
            "title": action.get("title")
                or (existing.get("title") if existing else key.replace("-", " ").title()),
            "starts_at_utc": starts_utc,
            "duration_minutes": duration,
            "timezone": sticky_tz,
            "location": action.get("location")
                or (existing.get("location") if existing else None),
            "judge": action.get("judge")
                or (existing.get("judge") if existing else None),
            "notes": action.get("notes")
                or (existing.get("notes") if existing else None),
            "dial_in": action.get("dial_in")
                or (existing.get("dial_in") if existing else None),
            "status": "scheduled",
            "significance": significance,
            "gcal_event_id": existing.get("gcal_event_id") if existing else None,
            "docket_id": sticky_docket_id,
            "source_entry_ids": prev_sources,
        }
        self.store.upsert_hearing(merged)

    # --- deadlines ---

    def _apply_deadline_action(
        self,
        case: CaseConfig,
        docket_id: int,
        tz: str,
        entry: dict[str, Any],
        action: dict[str, Any],
    ) -> None:
        atype = (action.get("type") or "").upper()
        key = action.get("deadline_key")
        if not key:
            log.warning("deadline action without deadline_key: %s", action)
            return

        # ADD_DEADLINE with no date is allowed ONLY when the LLM explicitly
        # marks it conditional (deadline runs from an unknown future event,
        # e.g. "21 days after resolution of [related case]"). The row is
        # persisted with due_at_utc=NULL so the renderers skip it (no fake
        # calendar entry) while the summary scaffold still surfaces the
        # verbatim court text from `notes`. A date-less ADD_DEADLINE without
        # the conditional flag is a motion-anticipating-a-deadline that the
        # LLM should have IGNOREd — drop it.
        if atype == "ADD_DEADLINE" and not action.get("local_date"):
            if not action.get("conditional"):
                log.warning(
                    "skipping date-less ADD_DEADLINE: case=%s key=%r entry=%s "
                    "(LLM should have IGNOREd or marked conditional)",
                    case.case_id, key, entry["id"],
                )
                return
            log.info(
                "applying conditional ADD_DEADLINE: case=%s key=%r entry=%s "
                "notes=%r",
                case.case_id, key, entry["id"],
                (action.get("notes") or "")[:120],
            )

        log.info(
            "applying %s case=%s key=%r entry=%s date=%s",
            atype, case.case_id, key, entry["id"], action.get("local_date"),
        )

        existing = self.store.get_deadline(case.case_id, key)
        eid = entry["id"]
        prev_sources = list(existing.get("source_entry_ids", [])) if existing else []
        if eid not in prev_sources:
            prev_sources.append(eid)
        sticky_docket_id = existing.get("docket_id") if existing else docket_id
        sticky_tz = existing.get("timezone") if existing else tz

        if atype == "CANCEL_DEADLINE":
            if existing:
                merged = dict(existing)
                merged.update(
                    status="cancelled",
                    notes=action.get("notes") or existing.get("notes"),
                    source_entry_ids=prev_sources,
                    docket_id=sticky_docket_id,
                )
                self.store.upsert_deadline(merged)
            elif action.get("local_date"):
                # Same fallback as hearings: insert a fresh row directly into
                # 'cancelled' so the audit trail captures the vacatur.
                due_at_utc = _deadline_local_to_utc(
                    action["local_date"], action.get("local_time"), tz
                )
                self.store.upsert_deadline({
                    "case_id": case.case_id,
                    "deadline_key": key,
                    "title": action.get("title")
                        or key.replace("-", " ").title(),
                    "due_at_utc": due_at_utc,
                    "timezone": tz,
                    "notes": action.get("notes"),
                    "status": "cancelled",
                    "significance": action.get("significance") or "major",
                    "deadline_type": action.get("deadline_type"),
                    "docket_id": docket_id,
                    "source_entry_ids": prev_sources,
                })
            else:
                log.warning(
                    "CANCEL_DEADLINE on unknown key with no local_date: "
                    "case=%s key=%r entry=%s — dropping",
                    case.case_id, key, entry["id"],
                )
            return

        if atype == "MARK_FILED":
            if existing:
                merged = dict(existing)
                merged.update(
                    status="met",
                    source_entry_ids=prev_sources,
                    docket_id=sticky_docket_id,
                )
                self.store.upsert_deadline(merged)
            else:
                log.info(
                    "MARK_FILED on unknown key (no row to update): "
                    "case=%s key=%r entry=%s",
                    case.case_id, key, entry["id"],
                )
            return

        # ADD_DEADLINE / RESCHEDULE_DEADLINE
        due_at_utc = existing.get("due_at_utc") if existing else None
        if action.get("local_date"):
            # Convert via the existing row's tz (sticky) so a sibling-docket
            # extension doesn't shift the deadline's wall-clock by retz.
            convert_tz = (existing.get("timezone") if existing else None) or tz
            due_at_utc = _deadline_local_to_utc(
                action["local_date"], action.get("local_time"), convert_tz
            )
        elif atype == "ADD_DEADLINE" and action.get("conditional"):
            # Explicitly conditional — clear any prior date so the renderer
            # skips this row and the summary scaffold surfaces the verbatim
            # court text in `notes` instead of an estimated date.
            due_at_utc = None
        significance = action.get("significance") or (
            existing.get("significance") if existing else None
        )
        merged: dict[str, Any] = {
            "case_id": case.case_id,
            "deadline_key": key,
            "title": action.get("title")
                or (existing.get("title") if existing else key.replace("-", " ").title()),
            "due_at_utc": due_at_utc,
            "timezone": sticky_tz,
            "notes": action.get("notes")
                or (existing.get("notes") if existing else None),
            "status": "pending",
            "significance": significance,
            "deadline_type": action.get("deadline_type")
                or (existing.get("deadline_type") if existing else None),
            "gcal_event_id": existing.get("gcal_event_id") if existing else None,
            "docket_id": sticky_docket_id,
            "source_entry_ids": prev_sources,
        }
        self.store.upsert_deadline(merged)
