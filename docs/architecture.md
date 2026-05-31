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
│ LLM extractor     │  small/fast tier (Claude Haiku, Gemini Flash Lite, gpt-5.4-nano);
│ per docket entry  │  returns ADD_HEARING / RESCHEDULE_HEARING / CANCEL_HEARING / MARK_HELD / ...
└─────────┬─────────┘
          │
          ▼
┌───────────────────┐
│ SQLite store      ├────────────┐  hearings, deadlines, case_summaries.
└─────────┬─────────┘            │
          │                      ▼
          │            ┌───────────────────┐
          │            │ summary LLM       │  higher tier (Sonnet / Gemini Pro /
          │            │ (per docket)      │  GPT-5.4); runs when a primary
          │            └─────────┬─────────┘  doc or disposition lands.
          │                      │
          │                      ▼
          │            ┌───────────────────┐
          │            │ truthfulness      │  prompt rules are soft, so this
          │            │ guard (retry/warn)│  deterministic guard retries
          │            └─────────┬─────────┘  absence/custody slips and removes
          │                      │            ungrounded dates/amounts.
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
| **Extraction** | High (one call per relevant entry) | Claude Haiku / Gemini Flash Lite / gpt-5.4-nano | Structured-output classification — date, key, significance. The cheap tier handles it fine, and the per-case cost stays in the cents-per-day range. |
| **Summarization** | Low (one call per docket, rarely re-run) | Claude Sonnet / Gemini Pro / GPT-5.4 | Synthesis from 30-100k tokens of legal prose. Worth the upgrade; pennies per docket. |

Each track has its own provider / model env var, with `LLM_PROVIDER` /
`LLM_MODEL` as the global default that applies to both when no per-track
override is set:

- **Extraction**: `LLM_EXTRACTION_PROVIDER` (override) > `LLM_PROVIDER`
  (global) > API-key auto-detect, and `LLM_MODEL` for the model.
- **Summarization**: `LLM_SUMMARY_PROVIDER` (override) > `LLM_PROVIDER`
  (global) > API-key auto-detect, and `LLM_SUMMARY_MODEL` for the model.

### Why the default is a split — Gemini for extraction, Anthropic for summaries

As of 0.13.0 the default is no longer a single provider for both tracks. With
API keys set but no `LLM_*` env vars, Case Calendar auto-detects **Gemini
(`gemini-3.1-flash-lite`) for extraction** and **Anthropic
(`claude-sonnet-4-6`) for summaries**. The auto-detect key priority is
per-track: extraction prefers `gemini > anthropic > openai`, while the
summary / base resolution prefers `anthropic > gemini > openai`. `LLM_PROVIDER`
(global) still overrides both tracks, and `LLM_EXTRACTION_PROVIDER` /
`LLM_SUMMARY_PROVIDER` override their own track.

Earlier releases (0.10.0 / 0.11.0) kept Anthropic as the extraction default for
a specific reason: relying on its intrinsic training priors, Gemini silently
classified a long tail of substantive federal-procedure deadline classes —
PSR, Speedy Trial Act exclusions, surrender for service of sentence,
civil-forfeiture claim/answer, substantive sealing motion practice,
exhibit-filing deadlines, certified administrative record — as
`procedural-minor`, and those then dropped out at the render-time significance
gate, off subscriber calendars.

0.13.0 closes that gap in the prompt, not the model. The unified extraction
`SYSTEM_PROMPT` now carries a structured `DEADLINE_SIGNIFICANCE_RULES` block
(ordered `RULE 1`-`RULE 5`) that enumerates those substantive classes
explicitly and biases the default toward `major`. Because the classes are now
named in the prompt for *every* provider — apples-to-apples, rather than left
to each model's intrinsic priors — Gemini now classifies them as `major` too.
The important framing: the prompt carries the priors for every provider; this
is not a claim that Gemini's training improved.

With the bucketing gap closed, the measured comparison favors Gemini for
extraction. On the full caseload (see the
[SCORECARD](../model-comparison/SCORECARD.md)) Gemini posts the best
`D met/pass` in the table and far fewer spurious cancellations than Anthropic
(`D canc` 9 vs 28), and its aggregate deviation (305) is the best overall —
ahead of Anthropic's `claude-haiku-4-5` (349). Gemini is also roughly 3.75×
cheaper and roughly 1.9× faster per call on the extraction track. The summary
track stays Anthropic because Sonnet still adds a few categories of detail the
higher-tier Gemini summary model drops — statutory citations, count numbers,
cross-docket statutory distinctions, and cancelled-schedule notes.

One honest caveat survives the change: `DEADLINE_SIGNIFICANCE_RULES` enumerates
the substantive classes the project currently knows about. An operator whose
caseload includes substantive classes the ruleset does *not* enumerate should
still verify Gemini's output against their own docket set, and the per-track
override env vars remain available for that. But the risk is materially
*reduced* — not merely shifted — because the enumeration is now in the prompt
for both providers rather than left to a single model's training corpus.

| | Extraction track | Summary track |
| --- | --- | --- |
| Default provider (0.13.0) | **Gemini** (`gemini-3.1-flash-lite`) | **Anthropic** (`claude-sonnet-4-6`) |
| Dominant constraint | Substantive-class coverage; per-call latency × volume | Synthesis quality; capturing case-distinguishing detail |
| Anthropic cost (full backfill) | \~$6.72 | \~$2.30 |
| Gemini cost (full backfill) | \~$1.82 | \~$1.11 |
| Aggregate deviation (lower is better) | Gemini **305** (best in table) vs Anthropic 349 | — |
| Why this provider | Now classifies the enumerated substantive classes as `major` (the prompt carries the priors); best `D met/pass` in the table and far fewer spurious cancellations than Anthropic, \~3.75× cheaper, \~1.9× faster per call | richer case-distinguishing prose (statutory citations, count numbers, cross-docket statutory distinctions, cancelled-schedule notes — all of which Gemini's terser summaries still drop) |

The full deviation breakdown — measured against a human-scored ground-truth
worksheet over the whole caseload — is in the
[SCORECARD](../model-comparison/SCORECARD.md). The split exists because the two
tracks optimize for different things: extraction is high-volume classification
where cost and latency multiply over thousands of calls, and the substantive
deadline classes are now enumerated in the prompt so Gemini handles them as
well as Anthropic; summaries are low-volume synthesis where Sonnet's
case-distinguishing detail earns the higher tier. An operator who prefers a
single provider can still set `LLM_PROVIDER`, or pin either track with
`LLM_EXTRACTION_PROVIDER` / `LLM_SUMMARY_PROVIDER`.

The [SCORECARD's Summary track section](../model-comparison/SCORECARD.md)
documents the bucket-confusion problem with side-by-side examples — three
NK-IT-worker cases that Gemini summarizes into nearly interchangeable prose
vs Anthropic's case-distinguishing versions of the same three; three
ransomware cases ditto; the DOW litigation group where Gemini collapses
the statutory distinction that drives why the three proceedings are
separate.

For measured per-provider backfill costs across a real caseload, see
[Case summaries → Cost](case-summaries.md#cost) and the
[Cost page](cost.md).

## Why LLM-driven extraction, not regex?

Courts describe hearings inconsistently. The same event can show up as:

- `Set/Reset Hearings` (a clerk's minute entry)
- `ELECTRONIC NOTICE OF RESCHEDULING`
- `Order on Stipulation for Continuance`
- A scheduling order with the date embedded in the PDF text
- A paperless minute entry with no document attached

Maintaining regexes per court is a treadmill — and a new clerk's habits
break them silently. Instead, the LLM sees the entry plus the case's
known-hearings list, and decides `ADD_HEARING` vs `RESCHEDULE_HEARING` vs `UPDATE` vs
`CANCEL_HEARING` in one call. A cheap regex pre-filter still runs before the LLM
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
mapped into the same shape before the ICS / gcal layer ever sees them.

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
need *re-processing* — but cosmetic churn that didn't change anything
meaningful must not trigger it.

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

After per-entry extraction, every scheduled-or-cancelled hearing and
pending deadline gets a separate focused LLM call (`verify_hearing` /
`verify_deadline`, both backed by the merged `VERIFY_SYSTEM_PROMPT` as
of 0.11.0). The model sees the candidate row plus a window of
docket-relevant entries — the most recent on the docket, the entries
filed around the row's own date, *and* the row's source entries (the
docket entries that originally allocated it) — and returns one of:

- `CONFIRM` — no-op.
- `RESCHEDULE_HEARING` — the docket says the row moved; update.
- `CANCEL_HEARING` — the docket vacated it.
- `MARK_HELD` *(hearings only)* — there's evidence the hearing happened
  (minute entry, verdict, transcript, judgment).
- `MARK_FILED` *(deadlines only)* — the required filing was made.
- `REINSTATE` *(hearings only)* — the row is marked cancelled but the
  docket doesn't actually support that cancellation.
- `DELETE_HALLUCINATION` — the row was never a real event. Only valid
  when the LLM has seen the original source entry and concluded it
  does not actually set the event; a deterministic guard
  (`CaseSyncer._delete_hallucination_allowed`) downgrades this to
  `UNCLEAR` if any source entry id wasn't in the recent_entries the
  LLM actually received.
- `UNCLEAR` — leave it alone, re-check next sync. The safe default in
  ambiguous cases.

All domain-level LLM calls pin `temperature=0.0` (since 0.11.0) so the
verify pass's decisions are deterministic given the inputs — same
prompt + same context produces the same verdict every sync, no
sampling coin flips on borderline rows.

This catches the classes of bug that per-entry extraction can't see:
reschedules across multiple entries, trials that got mooted by a plea
but never explicitly vacated, and (rare) hallucinated rows.

The around-the-row-date entries matter for past hearings on busy
dockets: the record that proves a hearing happened (a minute entry or
judgment) is filed near the hearing date and can fall outside the
most-recent window once the docket moves on. Without it the pass would
leave a concluded hearing stuck at `scheduled` — it never saw the
evidence. It widens what the model can see without lowering the bar for
`MARK_HELD`, which still requires a cited record.

There's a parallel verify pass for filing deadlines, run on every case —
deadline tracking is uniform, with no per-case opt-in.

## The data model

The SQLite store has six operational tables:

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

But a prompt rule is *soft protection*: the model can ignore it, and for a
brand-new case there's no earlier good summary to diff the output against,
so a slip would reach subscribers unaided. The summary pipeline therefore
defends in depth — one preventive layer and two deterministic backstops:

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
3. **A grounding check — retry, then strip.** Any date or dollar amount in the
   prose that can't be traced to the hearings / deadlines scaffold, the source
   documents, or the operator-supplied notes (the aggregation note and any
   `extra_documents` notes) is treated as a possible fabrication. Because
   summaries auto-generate as a service, a warning alone could sail unread into
   a subscriber's calendar, so a hit *acts*: it retries generation once asking
   the model to drop the unsupported figure, and if the figure still survives
   it deterministically removes whichever whole sentence carries it (falling
   back to the refusal sentence if that empties the summary). A warning is
   always logged so an operator can review the removal.

The retry comes before the strip on purpose. Dates appear in nearly every
summary and harmless formatting variance ("5/6/26" vs "May 6, 2026") gives this
class a real false-positive rate; re-shown the documents, the model can keep
and reformat a figure it can actually support — recovering the false positive —
so only a figure it genuinely can't support reaches the deterministic strip.
The cost of a surviving false positive is now a removed sentence rather than an
unread log line, which is why the matching deliberately biases toward silence.
Either way the ungrounded figure never reaches subscribers, which is what lets
the project run summaries unattended.

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

- [`HEARING_SIGNIFICANCE_RULES`](https://github.com/seanthegeek/case-calendar/blob/main/case_calendar/llm.py#L27) and [`DEADLINE_SIGNIFICANCE_RULES`](https://github.com/seanthegeek/case-calendar/blob/main/case_calendar/llm.py#L87) — the two major-vs-minor classification rubrics (one per event family), each interpolated into the main extractor prompt.
- [`SYSTEM_PROMPT`](https://github.com/seanthegeek/case-calendar/blob/main/case_calendar/llm.py#L86) — per-entry hearing AND filing-deadline extraction; a single merged prompt that runs on every docket (no per-case opt-in).
- [`VERIFY_SYSTEM_PROMPT`](https://github.com/seanthegeek/case-calendar/blob/main/case_calendar/llm.py#L910) — the end-of-sync verify pass; one merged prompt handles BOTH hearings AND filing deadlines (since 0.11.0).
- [`DEDUPE_HEARING_SYSTEM_PROMPT`](https://github.com/seanthegeek/case-calendar/blob/main/case_calendar/llm.py#L1234) — same-docket same-slot duplicate resolver.
- [`SUMMARY_SYSTEM_PROMPT`](https://github.com/seanthegeek/case-calendar/blob/main/case_calendar/llm.py#L1451) — the higher-tier case-summary prompt.

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
