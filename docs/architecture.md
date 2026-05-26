---
title: Architecture
---

This page is for the curious — what Case Calendar actually does between
"new docket entry" and "calendar event". You don't need it to run the
tool, but if you're going to modify it (or just want to understand the
trade-offs), this is the map.

The exhaustive design-decisions reference lives in
[`AGENTS.md`](https://github.com/seanthegeek/case-calendar/blob/main/AGENTS.md)
in the repo. This page is the concise version.

[← Back to docs](index.md)

## The pipeline at a glance

```text
CourtListener docket
          │
          ▼
┌───────────────────┐
│ regex pre-filter  │  drops non-hearing/non-deadline entries before the LLM.
└─────────┬─────────┘
          │
          ▼
┌───────────────────┐
│ LLM extractor     │  small/fast tier (Claude Haiku, gpt-5.4-nano, Gemini Flash Lite);
│ per docket entry  │  returns ADD / RESCHEDULE / CANCEL / MARK_HELD / ...
└─────────┬─────────┘
          │
          ▼
┌───────────────────┐
│ SQLite store      ├────────────┐  hearings, deadlines, case_summaries.
└─────────┬─────────┘            │
          │                      ▼
          │            ┌───────────────────┐
          │            │ summary LLM       │  higher tier (Sonnet / GPT-5.4 /
          │            │ (per docket)      │  Gemini Pro); runs when a primary
          │            └─────────┬─────────┘  doc or disposition lands.
          │                      │
          │                      ▼
          │            ┌───────────────────┐
          │            │ truthfulness      │  prompt rules are soft, so this
          │            │ guard (retry/warn)│  deterministic guard retries
          │            └─────────┬─────────┘  absence/custody slips and warns
          │                      │            on ungrounded dates/amounts.
          ▼                      │
┌───────────────────┐            │
│ end-of-sync       │            │  verify-pass LLM re-checks each live
│ confidence checks │            │  hearing / deadline against the docket.
└─────────┬─────────┘            │
          │                      │
          ▼                      ▼
┌──────────────────────────────────────────┐
│ renderers                                │  ICS, Google Calendar, M365
└──────────────────────────────────────────┘  Outlook, index.html.
```

Two delivery modes feed the pipeline:

- **Polling.** `case-calendar sync` walks every docket in `config.yaml`,
  pulls anything newer than the store's last-modified cutoff, runs it through
  the pipeline, and re-emits affected calendars. Designed to run on a
  cron.
- **Webhooks.** `case-calendar serve` listens for CourtListener
  `DOCKET_ALERT` events and runs the *same* `process_entry` function on
  each delivery. One entry per HTTP request, one calendar re-emit per
  delivery, in seconds.

Both paths share the same code beneath the entry processor. A hearing
extracted via webhook is byte-identical to one extracted via polling.

## Two LLM tracks

Case Calendar uses large-language-model calls for two distinct jobs.
Throughout the codebase and these docs:

- **Extraction** is the act of reading a single docket entry and pulling
  out the structured facts that turn into calendar events: the *what*
  (a sentencing hearing, a response-brief deadline), the *when* (date
  and time in the court's local zone), the *which* (is this a brand-new
  hearing, a reschedule of an existing one, a cancellation, or
  evidence that an earlier one already happened), and the *significance*
  (does this rise to the level of a public-calendar event, or is it
  procedural noise). Extraction runs on every relevant docket entry —
  high volume, narrow output, classification-shaped. Cheap models do it
  well.
- **Summarization** is the act of reading a docket's *primary document*
  (indictment, complaint, etc.) plus any disposition documents
  (judgments, plea agreements, dismissals) and producing 2-4 sentences
  of prose that tell a subscriber what the case is about and where it
  stands. Summarization runs at most once per docket, only when a new
  primary document or disposition lands. Low volume, long context,
  synthesis-heavy. Higher-tier models earn their keep.

The two jobs have different cost / quality trade-offs, so they're wired
to independent provider and model knobs:

| Track | Volume | Default model | Why |
| --- | --- | --- | --- |
| **Extraction** | High (one call per relevant entry) | Claude Haiku / gpt-5.4-nano / Gemini Flash Lite | Structured-output classification — date, key, significance. The cheap tier handles it fine, and the per-case cost stays in the cents-per-day range. |
| **Summarization** | Low (one call per docket, rarely re-run) | Sonnet / GPT-5.4 / Gemini Pro | Synthesis from 30-100k tokens of legal prose. Worth the upgrade; pennies per docket. |

The two tracks have independent provider / model knobs
(`LLM_PROVIDER` / `LLM_MODEL` for the extractor; `LLM_SUMMARY_PROVIDER` /
`LLM_SUMMARY_MODEL` for summaries) so changing one doesn't affect the
other.

## Why LLM-driven extraction, not regex?

Courts describe hearings inconsistently. The same event can show up as:

- `Set/Reset Hearings` (a clerk's minute entry)
- `ELECTRONIC NOTICE OF RESCHEDULING`
- `Order on Stipulation for Continuance`
- A scheduling order with the date embedded in the PDF text
- A paperless minute entry with no document attached

Maintaining regexes per court is a treadmill — and a new clerk's habits
break them silently. Instead, the LLM sees the entry plus the case's
known-hearings list, and decides `ADD` vs `RESCHEDULE` vs `UPDATE` vs
`CANCEL` in one call. A cheap regex pre-filter still runs before the LLM
to drop the obvious non-hearings (briefs, attorney appearances, sealed
placeholders) for free.

## Stable hearing keys

Each logical hearing — say, "sentencing for Smith" — gets a stable
`hearing_key` (kebab-case, e.g. `smith-sentencing`) assigned on first
observation. Reschedules and detail updates land on the same row. The
Google Calendar event id is derived deterministically from
`sha1(case_id::hearing_key)`, so the same logical hearing is the same
calendar event across syncs, reschedules, and database restores.

Filing deadlines work the same way, in a parallel `deadlines` table with
a separate `deadline_key`. Renderers don't care which is which — both are
projected into the same shape before the ICS / gcal layer ever sees them.

## Three-tier short-circuit

Quiet days cost almost nothing because the syncer short-circuits at three
levels:

1. **Per-docket** — if the docket's `date_modified` hasn't advanced since
   the last sync, skip everything. No entries API call, no LLM.
2. **Per-entry** — `iter_entries(modified_after=cutoff)` filters
   server-side to entries newer than the local last-modified cutoff.
3. **Per-fingerprint** — even if an entry comes back, dedup against
   `(docket_id, entry_id, content_fingerprint)` skips re-LLM-ing entries
   whose substantive content didn't change.

On a busy docket with a real update, this still pays for one LLM call.
On a quiet day across 30 dockets, it pays for one cheap CourtListener request per
docket and zero LLM calls.

### What's in the fingerprint

The third short-circuit is the interesting one. Case Calendar can't trust
"has this `entry_id` been seen before?" alone, because RECAP entries
evolve after they first appear — a sealed PDF gets unsealed, or a
previously-missing PDF finally gets uploaded to RECAP. Those entries
need *re-processing*, but cosmetic churn that didn't change
anything meaningful.

The fingerprint is a SHA-1 of just the entry state that matters:

- The entry's `description` and `short_description` (the docket text).
- Its `date_filed`.
- For each attached document: the document's description, whether it's
  available on RECAP, whether it's sealed, and whether any plain text
  has been extracted from it yet.

Those second-group flags are what makes "PDF finally appeared on RECAP"
or "sealed PDF was unsealed" re-trigger processing automatically: the
flag flips → the fingerprint changes → the entry no longer matches its
cached row → the syncer re-runs the LLM on it. Everything else —
re-sorted metadata fields, unrelated audit columns — leaves the
fingerprint stable, so the re-sync is a no-op.

## End-of-sync confidence pass

After per-entry extraction, every scheduled or recently-changed hearing
gets a separate focused LLM call (`verify_hearing`). The model sees just
the candidate hearing plus the last 15 hearing-relevant entries on its
docket, and returns one of:

- `CONFIRM` — no-op.
- `RESCHEDULE` — the docket says the hearing moved; update the row.
- `CANCEL` — the docket cancelled it.
- `MARK_HELD` — there's evidence the hearing happened (minute entry,
  verdict, transcript, judgment).
- `REINSTATE` — the row is marked cancelled but the docket doesn't
  actually support that cancellation.
- `DELETE_HALLUCINATION` — the row was never a real hearing.
- `UNCLEAR` — leave it alone, re-check next sync.

This catches the classes of bug that per-entry extraction can't see:
reschedules across multiple entries, trials that got mooted by a plea
but never explicitly vacated, and (rare) hallucinated rows.

There's a parallel verify pass for filing deadlines when those are
enabled on the case.

## The data model

The SQLite store has five operational tables:

- **`dockets`** — id, last `date_modified` (the short-circuit cutoff),
  last filing date, cached court metadata.
- **`entries`** — dedup of already-processed entries, keyed by
  `(docket_id, entry_id)` with a content fingerprint. Description and
  document body are persisted only for entries that matter to either
  the extractor or the summary pipeline; everything else gets a
  fingerprint-only stub.
- **`hearings`** — per-case logical hearings keyed by
  `(case_id, hearing_key)`. Includes significance, status, calendar
  event ids (for idempotency across pushes), and the source-entry list
  for audit trails.
- **`deadlines`** — parallel structure to hearings, with statuses
  `pending` / `met` / `passed` / `cancelled`.
- **`case_summaries`** — per-docket prose summary plus a `stale` flag the
  syncer flips whenever a new primary document or disposition lands — or
  whenever a hearing or deadline changes posture (marked held / cancelled /
  rescheduled), so a verify-pass outcome with no accompanying document entry
  still refreshes the prose. The flip happens in `Store.upsert_hearing` /
  `upsert_deadline`, the one chokepoint every posture-changing mutation
  passes through.
- **`webhook_events`** — idempotency-key dedup for the webhook receiver.

WAL journaling + a 5-second `busy_timeout` let the polling `sync`
process and the long-running `serve` process safely share the same
SQLite file. The webhook server also serializes its own worker threads
with a server-wide lock.

## Why "primary document"?

The summary pipeline talks about each docket's *primary document* — the
indictment, superseding indictment, information, complaint, amended
complaint, or petition that establishes what the case is about. Earlier
in the project this was called "operative pleading", which is a real
civil-practice term but reads oddly when applied to criminal indictments.
"Primary document" connects to the established "primary source" concept
and works across criminal and civil practice. See
[case summaries](case-summaries.md) for what gets matched and how
it's used.

## Data quality guardrails

Several of the codebase's stricter behaviors exist to prevent specific
failure modes seen on real dockets — hallucinations, false-positive
"held" verdicts, calendar drift across timezones, cross-docket
contamination. They look conservative on first read, and that's the
point: a wrong event on a public calendar erodes subscriber trust far
more than a missing one.

### Past-date alone is not evidence a hearing happened

Trials get continued or vacated by plea agreement without an explicit
cancellation entry; the calendar date passes; the verify pass refuses to
mark the row "held" without affirmative evidence (a minute entry, verdict,
transcript, or judgment-after-trial). A past-dated `scheduled` row
accurately communicates "outcome not confirmed".

### The summary LLM is told to refuse, not fabricate

When the inputs don't support a confident summary, the model emits a fixed
sentence ("Documents available for this docket are insufficient to generate
a reliable summary.") which the renderer surfaces verbatim. The
alternative — letting the model invent plausible-sounding facts to fill the
gap — produced exactly that kind of hallucination during early development.

### Court-local timezones are preserved on each event

Rather than normalizing to UTC, each event keeps the courthouse's local
time. A 3 PM Pacific hearing displayed in a New York viewer's calendar
still says "3 PM Pacific / 6 PM Eastern", and the semantic "this is when
the courthouse is open" survives DST transitions and travel.

### Cross-court siblings are isolated

A case can span multiple dockets across different courts (district +
circuit appeal, parallel filings under different statutes). The per-entry
LLM context only shows its siblings *in the same court* — a "stay
appellate proceedings" order on the circuit docket must not trigger
cancellations on the district docket's hearings.

### Summaries state only what the documents support

The summary prompt forbids whole classes of claim the documents don't
establish — inferring a custody status no document supports, asserting the
absence of hearings / deadlines / a disposition, stating speculative or
conditional outcomes, printing dollar figures that aren't legibly in the
text. The [LLM prompts](llm-prompts.md) page reproduces the prompt that
encodes these rules, verbatim.

But a prompt rule is *soft protection*. The model can ignore it, and for a
brand-new case there's no earlier good summary to diff the output against,
so a slip would reach subscribers unaided. Three layers cover the gap:

1. **Prompt rules — prevention.** The forbidden-claim rules above, written
   into the prompt as instructions. Catches most cases and costs nothing
   extra, but is soft.
2. **A deterministic post-generation guard — the hard backstop.** It scans
   the generated prose for absence-of-record, unsupported-custody, and
   speculative / conditional-outcome claims, matched by *construction* (a
   negation plus a procedural-record noun; a custody keyword plus a
   "we-don't-know" qualifier) rather than by literal string, so rewording
   around a phrase doesn't slip past. A hit triggers exactly one
   regeneration with the violation fed back to the model; whichever attempt
   is cleaner is kept, and a still-failing summary is logged rather than
   blocked. The summary is never withheld — *retry, then keep and warn*.
3. **A grounding check — warn-only.** Any date or dollar amount in the prose
   that can't be traced to the hearings / deadlines scaffold, the source
   documents, or the operator-supplied notes (the aggregation note and any
   `extra_documents` notes) is logged for operator review. This layer never
   retries: dates appear in nearly every summary and harmless formatting
   variance gives it a real false-positive rate, so a retry would risk
   systematic double-cost.

The split between the last two layers is deliberate — high-confidence claim
classes (absence, custody) earn a retry; the false-positive-prone
fact-grounding class only warns. Either way the wrong fact self-corrects or
surfaces in the logs, which is what lets the project run summaries
unattended.

## AGENTS.md and the runtime prompts

The full set of those guardrails — plus the reasoning behind each one,
the architectural conventions every module follows, and the testing
philosophy — lives in
[AGENTS.md](https://github.com/seanthegeek/case-calendar/blob/main/AGENTS.md)
at the repo root. That file is the project's contract with any
**AI coding assistant** working in the codebase: Claude Code, GitHub
Copilot, Cursor, Codex, Aider, or any other tool that has a
"follow this project's conventions" surface. The reason rules live in
AGENTS.md (rather than in each agent's private memory) is portability —
every collaborator, human or otherwise, picks them up the same way, and
the rules survive when one agent's session ends or a different agent
joins the project. The same file is `@`-included from `CLAUDE.md` so
Claude Code reads it on every invocation; other agents read it the same
way under their own conventions.

The data-quality guardrails described above were the *source material*
for the LLM prompts the project uses at runtime. Same rules, encoded in
two places: once as English for the human and agent contributors who
write the code, and once as English for the model that's about to
classify a real docket entry or read a real indictment. When a rule
gets sharpened (e.g., the no-fabrication refusal, or the
"trial-date-is-not-evidence-of-a-trial" invariant), it gets sharpened
in both places.

The runtime prompts all live in
[`case_calendar/llm.py`](https://github.com/seanthegeek/case-calendar/blob/main/case_calendar/llm.py),
and each one is reproduced **verbatim** on the
[LLM prompts](llm-prompts.md) page so you can read exactly what the model
is told without opening the source:

- [`SIGNIFICANCE_RULES`](https://github.com/seanthegeek/case-calendar/blob/main/case_calendar/llm.py#L42) — the major-vs-minor classification rubric, interpolated into the main extractor prompt.
- [`SYSTEM_PROMPT`](https://github.com/seanthegeek/case-calendar/blob/main/case_calendar/llm.py#L102) — per-entry hearing extraction (and, with the addendum below, deadlines).
- [`DEADLINE_PROMPT_ADDENDUM`](https://github.com/seanthegeek/case-calendar/blob/main/case_calendar/llm.py#L407) — appended to `SYSTEM_PROMPT` for cases that opt into filing-deadline tracking.
- [`VERIFY_SYSTEM_PROMPT`](https://github.com/seanthegeek/case-calendar/blob/main/case_calendar/llm.py#L1024) — the end-of-sync hearing verify pass.
- [`VERIFY_DEADLINE_SYSTEM_PROMPT`](https://github.com/seanthegeek/case-calendar/blob/main/case_calendar/llm.py#L1313) — the parallel verify pass for filing deadlines.
- [`DEDUPE_HEARING_SYSTEM_PROMPT`](https://github.com/seanthegeek/case-calendar/blob/main/case_calendar/llm.py#L1417) — same-docket same-slot duplicate resolver.
- [`SUMMARY_SYSTEM_PROMPT`](https://github.com/seanthegeek/case-calendar/blob/main/case_calendar/llm.py#L1640) — the higher-tier case-summary prompt.

Reading any of those alongside the corresponding entry in
[`AGENTS.md`](https://github.com/seanthegeek/case-calendar/blob/main/AGENTS.md)
is the fastest way to see how a particular guardrail moves from "rule
for the human / agent writing this code" to "rule the model follows
when processing a docket".

## See also

- [Configuration](configuration.md) — the surface area visible to
  operators.
- [CLI reference](cli.md) — every subcommand.
- [Real-time webhooks](webhooks.md) — the push-mode delivery path.
- [Case summaries](case-summaries.md) — the second LLM track.
