#!/usr/bin/env python3
"""Trace one model's extraction deviation down to the rendered calendar.

The per-entry deviation in ``score_models.py`` counts raw extractor actions,
captured before any cleanup the live pipeline runs — the significance gate,
the per-row verify pass, and the dedupe sweeps. This script quantifies that
funnel for one model so the SCORECARD's "a deviation in the hundreds and a
clean calendar are consistent" claim stays backed by current numbers:

  * raw actions vs the human ground truth (deviation, over / under, the
    over-count split by what each action does — add / lifecycle / cancel —
    and the under-count re-checked at the docket-aggregate level, where
    attribution drift nets out and only real misses survive);
  * the logical rows those actions left in the model's provider store, and
    how many the significance gate hides (``minor``, bucketed by key pattern);
  * the events actually rendered into the provider store's ``out/*.ics``;
  * repeat firings — the same (action, key, dates) emitted on more than one
    entry (the human convention logs an action once, on the entry that
    operatively does it) — and the deviation re-scored with repeats collapsed;
  * verify-pass hallucination catches and dedupe-sweep merges, read from the
    build log and the store's audit notes;
  * same-slot / same-base-key row families left in the final store — the
    duplicate candidates a human should characterize before calling any of
    them a leak (same-slot deadlines are usually genuinely distinct: many
    real deadlines share one date — see the matching design note in
    AGENTS.md).

The CSV math needs only the committed ``model_actions.csv`` ×
``ground_truth.csv``. The store / log / ICS sections additionally need the
model's provider-store build output (``build_provider_stores.py`` writes it
under ``data/provider-stores/<provider>/<model>/``) and are skipped with a
note when it isn't on disk.

Usage:
    python3 model-comparison/funnel_analysis.py gemini/gemini-3.1-flash-lite \
        [--truth model-comparison/ground_truth.csv] \
        [--model-actions model-comparison/model_actions.csv] \
        [--store-dir data/provider-stores/<model label>]
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

CATS = [
    "h_scheduled",
    "h_rescheduled",
    "h_held",
    "h_cancelled",
    "d_set",
    "d_rescheduled",
    "d_met_filed",
    "d_cancelled",
]

ACTION_TO_CAT = {
    "ADD_HEARING": "h_scheduled",
    "RESCHEDULE_HEARING": "h_rescheduled",
    "MARK_HELD": "h_held",
    "CANCEL_HEARING": "h_cancelled",
    "ADD_DEADLINE": "d_set",
    "RESCHEDULE_DEADLINE": "d_rescheduled",
    "MARK_FILED": "d_met_filed",
    "CANCEL_DEADLINE": "d_cancelled",
}

# The three things an over-counted action can do to the calendar: add a new
# event (only add-class actions can), patch a row that already exists, or
# remove one.
BUCKET_OF_CAT = {
    "h_scheduled": "add",
    "d_set": "add",
    "h_rescheduled": "lifecycle",
    "h_held": "lifecycle",
    "d_rescheduled": "lifecycle",
    "d_met_filed": "lifecycle",
    "h_cancelled": "cancel",
    "d_cancelled": "cancel",
}

_DECISION_RE = re.compile(
    r"provider_stores\.decisions extract docket=(\d+) entry=(\d+) \".*?\" -> (.*)$"
)
_ACTION_RE = re.compile(r"([A-Z_]+)\(([^)]*)\)")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_KEY_SUFFIX_RE = re.compile(r"-\d+$")

Counts = dict[str, int]
ActionSig = tuple[str, str, tuple[str, ...]]


def _zero() -> Counts:
    return {c: 0 for c in CATS}


def read_truth(path: Path) -> tuple[dict[str, Counts], dict[str, tuple[str, ...]]]:
    """Scored entries (reviewed, not bad-OCR) -> human counts, + docket key."""
    human: dict[str, Counts] = {}
    docket_of: dict[str, tuple[str, ...]] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("reviewed") != "1" or row.get("bad_ocr") == "1":
                continue
            eid = row["entry_id"]
            human[eid] = {c: int(row.get(c) or 0) for c in CATS}
            docket_of[eid] = (row["case_id"], row["docket_number"], row["court"])
    return human, docket_of


def read_model_actions(
    path: Path, provider: str, scored: set[str]
) -> dict[str, Counts]:
    """One model's per-entry counts, restricted to the scored entries."""
    model: dict[str, Counts] = defaultdict(_zero)
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["provider"] != provider or row["entry_id"] not in scored:
                continue
            for c in CATS:
                model[row["entry_id"]][c] += int(row.get(c) or 0)
    return dict(model)


def deviation_split(
    human: dict[str, Counts], model: dict[str, Counts]
) -> tuple[Counts, Counts]:
    """Per-category over (model > human) and under (model < human) sums."""
    over, under = _zero(), _zero()
    for eid, h in human.items():
        m = model.get(eid) or _zero()
        for c in CATS:
            d = m[c] - h[c]
            if d > 0:
                over[c] += d
            else:
                under[c] += -d
    return over, under


def bucket_split(over: Counts) -> Counts:
    """Fold per-category over-counts into add / lifecycle / cancel buckets."""
    out: Counts = {"add": 0, "lifecycle": 0, "cancel": 0}
    for c, n in over.items():
        out[BUCKET_OF_CAT[c]] += n
    return out


def aggregate_split(
    human: dict[str, Counts],
    model: dict[str, Counts],
    docket_of: dict[str, tuple[str, ...]],
) -> tuple[Counts, Counts]:
    """Per-category over / under with counts summed per logical docket first.

    A per-entry over+under pair nets out here when the model logged the same
    event from a neighboring entry (attribution drift); what survives is a
    docket-level surplus or miss."""
    agg_h: dict[tuple[str, ...], Counts] = defaultdict(_zero)
    agg_m: dict[tuple[str, ...], Counts] = defaultdict(_zero)
    for eid, h in human.items():
        m = model.get(eid) or _zero()
        for c in CATS:
            agg_h[docket_of[eid]][c] += h[c]
            agg_m[docket_of[eid]][c] += m[c]
    over, under = _zero(), _zero()
    for dk in agg_h:
        for c in CATS:
            d = agg_m[dk][c] - agg_h[dk][c]
            if d > 0:
                over[c] += d
            else:
                under[c] += -d
    return over, under


def aggregate_deviation(
    human: dict[str, Counts],
    model: dict[str, Counts],
    docket_of: dict[str, tuple[str, ...]],
) -> int:
    """Counts summed per logical docket before the |model − human| compare."""
    over, under = aggregate_split(human, model, docket_of)
    return sum(over.values()) + sum(under.values())


def parse_decision_log(log_text: str, scored: set[str]) -> dict[str, list[ActionSig]]:
    """Per scored entry, the extractor actions from the build log's decision
    lines, each reduced to (action type, key, dates) — the identity of what
    the action does, used to spot the same action fired on sibling entries."""
    actions: dict[str, list[ActionSig]] = defaultdict(list)
    for line in log_text.splitlines():
        m = _DECISION_RE.search(line)
        if not m or m.group(2) not in scored:
            continue
        for am in _ACTION_RE.finditer(m.group(3)):
            atype, inner = am.group(1), am.group(2)
            if atype not in ACTION_TO_CAT:
                continue
            parts = [p.strip() for p in inner.split(",")]
            key = ""
            if (
                parts
                and parts[0] not in ("major", "minor")
                and not _DATE_RE.match(parts[0])
            ):
                key = parts[0]
            dates = tuple(sorted(p for p in parts if _DATE_RE.match(p)))
            actions[m.group(2)].append((atype, key, dates))
    return dict(actions)


def collapse_repeats(
    actions: dict[str, list[ActionSig]],
) -> tuple[dict[str, Counts], list[tuple[ActionSig, str, str]]]:
    """Keep each (action, key, dates) only on the first entry that fired it.

    Returns the collapsed per-entry counts plus the dropped repeats as
    (signature, first entry, repeating entry). Entries iterate in build-log
    order, which is the order the pipeline processed them.
    """
    seen: dict[ActionSig, str] = {}
    collapsed: dict[str, Counts] = defaultdict(_zero)
    repeats: list[tuple[ActionSig, str, str]] = []
    for eid, acts in actions.items():
        for sig in acts:
            first = seen.get(sig)
            if first is not None and first != eid:
                repeats.append((sig, first, eid))
                continue
            seen[sig] = eid
            collapsed[eid][ACTION_TO_CAT[sig[0]]] += 1
    return dict(collapsed), repeats


def minor_key_buckets(keys: list[str]) -> Counter:
    """Rough characterization of minor-deadline keys by vocabulary."""
    buckets: Counter = Counter()
    for k in keys:
        if "redaction" in k:
            buckets["transcript redaction-request"] += 1
        elif "amicus" in k:
            buckets["amicus response / reply"] += 1
        elif "transcript" in k:
            buckets["other transcript"] += 1
        else:
            buckets["other procedural"] += 1
    return buckets


def base_key(key: str) -> str:
    return _KEY_SUFFIX_RE.sub("", key)


def key_families(
    rows: list[dict[str, Any]], key_field: str
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group rows by (docket number, key stripped of a trailing ``-N``).

    Families with more than one row are the key-drift duplicate CANDIDATES —
    a human still has to read them (an ``amicus-response-deadline-2`` can be a
    genuinely distinct response due the same day)."""
    fams: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        fams[(r["docket_number"] or "?", base_key(r[key_field]))].append(r)
    return {k: v for k, v in fams.items() if len(v) > 1}


def count_vevents(out_dir: Path) -> dict[str, int]:
    return {
        p.name: p.read_text(encoding="utf-8").count("BEGIN:VEVENT")
        for p in sorted(out_dir.glob("*.ics"))
    }


def _fetch(db: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    cur = db.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _pct(n: int, total: int) -> str:
    return f"{n / total:.0%}" if total else "—"


def report(args: argparse.Namespace) -> None:
    human, docket_of = read_truth(args.truth)
    scored = set(human)
    model = read_model_actions(args.model_actions, args.model, scored)
    if not model:
        raise SystemExit(
            f"no rows for {args.model!r} in {args.model_actions} — the label must "
            "match the provider column exactly"
        )
    human_total = sum(sum(v.values()) for v in human.values())
    raw_total = sum(sum(v.values()) for v in model.values())
    over, under = deviation_split(human, model)
    n_over, n_under = sum(over.values()), sum(under.values())
    dev = n_over + n_under
    agg = aggregate_deviation(human, model, docket_of)

    print(f"# Deviation-to-calendar funnel — {args.model}\n")
    print(
        f"Scored entries: {len(human)}; human actions: {human_total}; "
        f"model raw actions: {raw_total}."
    )
    print(
        f"Per-entry deviation: **{dev}** (over {n_over} / under {n_under}); "
        f"per-docket aggregate: **{agg}**.\n"
    )
    print("## Where the over-count goes\n")
    print("| over bucket | over | share |")
    print("| --- | ---: | ---: |")
    buckets = bucket_split(over)
    labels = {
        "add": "add (Hs + Ds) — can add an event, if `major`",
        "lifecycle": "lifecycle (Hr + Hh + Dr + Df) — patches an existing row",
        "cancel": "cancellations (Hc + Dc) — removes an event",
    }
    for b in ("add", "lifecycle", "cancel"):
        print(f"| {labels[b]} | {buckets[b]} | {_pct(buckets[b], n_over)} |")
    print(f"| **total** | **{n_over}** | 100% |\n")
    print("over by category:", "  ".join(f"{c}={over[c]}" for c in CATS))
    print("under by category:", "  ".join(f"{c}={under[c]}" for c in CATS))

    print("\n## Where the under-count goes\n")
    print(
        "Per-entry under vs what survives with counts summed per docket first —"
        " a per-entry miss nets out there when the model logged the same event"
        " from a neighboring entry (attribution drift).\n"
    )
    agg_over, agg_under = aggregate_split(human, model, docket_of)
    print("| category | per-entry under | survives at docket level |")
    print("| --- | ---: | ---: |")
    for c in CATS:
        print(f"| {c} | {under[c]} | {agg_under[c]} |")
    print(f"| **total** | **{n_under}** | **{sum(agg_under.values())}** |")
    print(
        "\naggregate over by category:",
        "  ".join(f"{c}={agg_over[c]}" for c in CATS),
    )

    store_dir = args.store_dir or Path("data/provider-stores") / args.model
    db_path = store_dir / "case-calendar.sqlite"
    if not db_path.exists():
        print(
            f"\n(no provider store at {db_path} — store / log / ICS sections "
            "skipped; build one with build_provider_stores.py)"
        )
        return

    db = sqlite3.connect(db_path)
    hearings = _fetch(
        db,
        """SELECT h.hearing_key, h.title, h.starts_at_utc, h.status,
                  h.significance, h.audit_notes, d.docket_number
           FROM hearings h LEFT JOIN dockets d ON h.docket_id = d.docket_id""",
    )
    deadlines = _fetch(
        db,
        """SELECT dl.deadline_key, dl.title, dl.due_at_utc, dl.status,
                  dl.significance, d.docket_number
           FROM deadlines dl LEFT JOIN dockets d ON dl.docket_id = d.docket_id""",
    )
    n_rows = len(hearings) + len(deadlines)
    minor_h = [h for h in hearings if h["significance"] == "minor"]
    minor_d = [d for d in deadlines if d["significance"] == "minor"]
    print("\n## Funnel\n")
    print(f"- raw extractor actions the scorer counts: {raw_total}")
    print(
        f"- logical rows in the final store: {n_rows} "
        f"({len(hearings)} hearings + {len(deadlines)} deadlines)"
    )
    print(
        f"- of those, `minor` (hidden by the significance gate): "
        f"{len(minor_h) + len(minor_d)} ({_pct(len(minor_h) + len(minor_d), n_rows)}) "
        f"— deadlines {len(minor_d)}, hearings {len(minor_h)}"
    )
    for bucket, n in minor_key_buckets(
        [d["deadline_key"] for d in minor_d]
    ).most_common():
        print(f"    - minor deadlines, {bucket}: {n}")
    out_dir = store_dir / "out"
    if out_dir.exists():
        vevents = count_vevents(out_dir)
        per_file = ", ".join(f"{name} {n}" for name, n in vevents.items())
        print(f"- events rendered to ICS: {sum(vevents.values())} ({per_file})")
    else:
        print(f"- (no {out_dir} — rendered-event count skipped)")

    print("\n## Cleanup the score never sees\n")
    log_path = store_dir / "build.log"
    if log_path.exists():
        log_text = log_path.read_text(encoding="utf-8")
        catches = re.findall(
            r"provider_stores\.decisions (verify_\w+) key='([^']*)'.*"
            r"-> DELETE_HALLUCINATION",
            log_text,
        )
        print(
            f"- verify-pass hallucination catches: {len(catches)}"
            + ("".join(f"\n    - {kind} {key!r}" for kind, key in catches))
        )
        actions = parse_decision_log(log_text, scored)
        collapsed, repeats = collapse_repeats(actions)
        rep_buckets: Counter = Counter(
            BUCKET_OF_CAT[ACTION_TO_CAT[sig[0]]] for sig, _, _ in repeats
        )
        c_over, c_under = deviation_split(human, collapsed)
        c_agg = aggregate_deviation(human, collapsed, docket_of)
        print(
            f"- repeat firings (same action + key + dates on sibling entries): "
            f"{len(repeats)} — add {rep_buckets['add']}, "
            f"lifecycle {rep_buckets['lifecycle']}, cancel {rep_buckets['cancel']}"
        )
        print(
            f"- deviation with repeats collapsed to first firing: "
            f"{dev} -> {sum(c_over.values()) + sum(c_under.values())} per-entry "
            f"(over {sum(c_over.values())}, under {sum(c_under.values())}); "
            f"{agg} -> {c_agg} aggregate"
        )
    else:
        print(f"- (no {log_path} — verify / repeat-firing sections skipped)")
    absorbed = 0
    for h in hearings:
        for m in re.finditer(
            r"Absorbed sibling key\(s\) ([a-z0-9-]+(?:, [a-z0-9-]+)*)",
            h["audit_notes"] or "",
        ):
            absorbed += len(m.group(1).split(", "))
    print(f"- duplicate hearing keys absorbed by the dedupe sweeps: {absorbed}")

    print("\n## Duplicate candidates left in the final store\n")
    print(
        "Read these before calling any a leak — same-slot deadlines are"
        " usually genuinely distinct (see the deadline-dedupe design note in"
        " AGENTS.md), and a `-2` key can be a real second event.\n"
    )
    visible_h = [
        h for h in hearings if h["status"] != "cancelled" and h["starts_at_utc"]
    ]
    slot_h: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for h in visible_h:
        slot_h[(h["docket_number"] or "?", h["starts_at_utc"])].append(h)
    dup_slots = {k: v for k, v in slot_h.items() if len(v) > 1}
    print(f"- same-slot non-cancelled hearing clusters: {len(dup_slots)}")
    for (dn, slot), rows in sorted(dup_slots.items()):
        keys = ", ".join(r["hearing_key"] for r in rows)
        print(f"    - {dn} {slot}: {keys}")
    fams = key_families(
        [d for d in deadlines if d["status"] != "cancelled"], "deadline_key"
    )
    print(f"- same-base-key deadline families (key drift candidates): {len(fams)}")
    for (dn, base), rows in sorted(fams.items()):
        detail = "; ".join(
            f"{r['deadline_key']} {(r['due_at_utc'] or '?')[:10]} "
            f"{r['status']}/{r['significance']}"
            for r in sorted(rows, key=lambda r: r["deadline_key"])
        )
        print(f"    - {dn} {base}: {detail}")
    db.close()


def main(argv: Optional[list[str]] = None) -> None:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "model",
        help=(
            "model label exactly as it appears in model_actions.csv's provider "
            "column and under data/provider-stores/, e.g. "
            "gemini/gemini-3.1-flash-lite or ollama/gpt-oss:20b"
        ),
    )
    parser.add_argument("--truth", type=Path, default=here / "ground_truth.csv")
    parser.add_argument(
        "--model-actions", type=Path, default=here / "model_actions.csv"
    )
    parser.add_argument(
        "--store-dir",
        type=Path,
        default=None,
        help="provider-store directory (default: data/provider-stores/<model>)",
    )
    report(parser.parse_args(argv))


if __name__ == "__main__":
    main()
