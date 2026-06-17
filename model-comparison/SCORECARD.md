# Provider accuracy vs human ground truth

Scored **992** docket entries (every `reviewed`, non-`bad_ocr` entry across the
benchmark) carrying **421** human-counted actions, across **10** logical dockets
in **6** cases. Lower deviation = closer to the human-read truth. All numbers
below are measured under the shipping policy: **structured output ON** (the
schema-enforced JSON default) and, for local thinking models, the **bounded
reasoning budget** with thinking ON.

The shipped default is a **split**: **Gemini** (`gemini-3.1-flash-lite`) for
extraction, **Anthropic** (`claude-sonnet-4-6`) for summaries. The benchmark ran
in four phases:

- **Phase 0 — extraction accuracy**: every model extracts hearings/deadlines from
  the same frozen dockets; a human-blind deviation score ranks them.
- **Phases 1 & 2 — summary generation**: each candidate summary model regenerates
  the per-docket case summaries, on the top extractor's scaffold (Phase 1) and on
  its own extraction (Phase 2).
- **Phase 3 — summary grading**: the summaries are read by hand and graded for
  accuracy, readability, and the China / DPRK / Russia provenance these cases turn
  on (there is no automated summary scorer — see `summarize_phase.py`).

The headline results:

- **Extraction**: Gemini wins (636 per-entry). The best **local** model,
  `gpt-oss:20b`, is a close 2nd at **710 — ahead of hosted Anthropic, OpenAI-mini,
  and OpenAI-nano** — so a free local model rivals the paid hosted tier on
  extraction.
- **Summaries**: no local summary is publication-ready — each is accurate on the
  figures but too thin (gpt-oss, glm) or too clunky/defective (gemma, qwen), the
  best reaching only a **C**. **Summaries need the higher hosted tier** — the
  opposite of extraction.
- **Thinking helps extraction but harms summaries** — a clean inversion (see the
  thinking notes in each phase).

## Methodology — per-entry, blind, against complete-text inputs

This counts each entry's action counts against a human's, not final per-docket
rows against the CourtListener web UI (which is incomplete relative to the v4 API,
[freelawproject/courtlistener#7429](https://github.com/freelawproject/courtlistener/issues/7429),
and can't see *where* a model erred or whether the **regex pre-filter** dropped an
event before any LLM saw it).

1. **Freeze a complete-text snapshot** (`snapshot_benchmark.py`) — every entry's
   full `description` + extracted PDF text, not the operational store's
   regex-filtered stubs. A date hidden in a stubbed entry would be invisible to
   *both* the models and the human; with full text it becomes a
   provider-independent miss the scorer can count.
2. **Human scores blind** (`build_scoring_page.py` → `ground_truth.csv`) — one
   offline HTML page, one card per entry showing the complete text the extractor
   saw, beside the eight action-count boxes the extractor emits. No model output
   is shown.
3. **Replay every model** (`build_provider_stores.py --entry-actions-csv`) over the
   same frozen snapshot, capturing per-entry action counts to `model_actions.csv`.
4. **Score deterministically** (`score_models.py`) — join human × model on
   `entry_id`; no model and no opinion in the scoring loop.

### How the human counted — the ground-truth conventions

The deviation numbers only mean something relative to the counting rules the
human applied (the scoring page's help block carries the same text). The human
counts **what this entry does**, not the cumulative docket state, and counts
**every** hearing and deadline regardless of significance — redaction-request,
response, status-report, and housekeeping deadlines all count even though the
calendar's significance gate would hide them. One entry often has several
non-zero counts: a minute entry can record a hearing held, schedule the next
one, and set deadlines.

| count | rule |
| --- | --- |
| `Hs` (hearing scheduled) | a new hearing this entry sets |
| `Hr` (rescheduled) | an existing hearing moved to a new date/time — a continuance counts here |
| `Hh` (held) | a minute entry recording / discussing a proceeding or held hearing |
| `Hc` (cancelled) | an explicit cancellation / vacatur with no replacement date |
| `Ds` (deadline set) | a new filing deadline this entry sets |
| `Dr` (rescheduled) | an existing deadline moved to a new date |
| `Df` (met / filed) | the filing the deadline required was made / deadline satisfied |
| `Dc` (cancelled) | a deadline cancelled / withdrawn / mooted, with no new date |

The edge rules that decide most close calls:

- **A continuance is a reschedule** (`Hr` 1), never a cancel plus a new
  schedule. **Cancel is only an explicit cancellation / vacatur** with no
  replacement date.
- **One slot is one hearing** — a single proceeding that disposes of several
  motions at one date+time counts once, never once per motion; only genuinely
  distinct proceedings at *different times* on the same day count separately.
- **Dark trial days are non-events** — a day the trial is not in session is
  neither a hearing nor a deadline.
- **An amended minute entry supersedes the original** — count the event(s)
  once on the amended entry, 0 on the superseded one.
- **Repeated across entries — count once**: when more than one entry states
  the same action (a stipulation and the order granting it; a notice
  re-issued; the same logical PACER entry mirrored on two CourtListener
  records), the action is counted once, on the entry that operatively does it,
  and 0 on the restatements. (This convention is why the funnel section below
  charges a model's repeat firings as over-counts.)
- **`bad_ocr` entries are set aside** — unreadable source text means neither
  model nor human could fairly extract, so those entries are excluded from
  scoring rather than counted against any model.

Two biases are worth naming. **Evaluation bias** (an AI judging AI) is removed — a
human reads the dockets, a dumb script measures deviation. **Prompt-fit bias** is
**not**: the prompts were authored by Claude and run unchanged for every model, a
home-field advantage no blind scoring can neutralize. So this measures *which
model is most accurate at running Case Calendar's actual, Claude-authored
prompts* — not a neutral model-capability claim.

The benchmark is a stratified 6-case sample frozen for reproducibility (see
[README.md](README.md)): us-v-ding, anthropic-v-dow (3 dockets: cadc / ca9 /
cand), us-v-knoot, us-v-gholinejad, us-v-mcgonigal, us-v-schmitz.

## Phase 0 — extraction accuracy

### Totals — per-entry deviation (lower is better)

Sum over the 8 action categories of |model count − human count|, over all 992
entries. `over` = model counted more than the human (duplicate keys /
hallucination); `under` = fewer (missed).

| model | host | per-entry | over | under | Hs | Hr | Hh | Hc | Ds | Dr | Df | Dc | aggregate |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **gemini/gemini-3.1-flash-lite** | hosted | **636** | 438 | 198 | 109 | 51 | 61 | 10 | 211 | 61 | 129 | 4 | **376** |
| **ollama/gpt-oss:20b** (thinking LOW) | local | **710** | 476 | 234 | 115 | 65 | 58 | 7 | 228 | 81 | 135 | 21 | **396** |
| ollama/gpt-oss:20b (thinking MEDIUM) | local | 728 | 508 | 220 | 149 | 60 | 44 | 4 | 234 | 83 | 139 | 15 | 420 |
| anthropic/claude-haiku-4-5 | hosted | 784 | 590 | 194 | 105 | 66 | 84 | 16 | 232 | 95 | 143 | 43 | 476 |
| openai/gpt-5.4-mini | hosted | 879 | 676 | 203 | 113 | 92 | 62 | 4 | 322 | 107 | 173 | 6 | 551 |
| ollama/qwen3.5:9b (thinking OFF) | local | 930 | 676 | 254 | 142 | 56 | 54 | 13 | 490 | 36 | 115 | 24 | 700 |
| openai/gpt-5.4-nano | hosted | 967 | 760 | 207 | 146 | 99 | 83 | 7 | 366 | 131 | 132 | 3 | 697 |
| ollama/gemma4:e4b (thinking ON) | local | 1241 | 1030 | 211 | 140 | 181 | 143 | 13 | 493 | 147 | 112 | 12 | 985 |
| ollama/granite3.3:8b | local | 1461 | 1232 | 229 | 203 | 101 | 56 | 15 | 818 | 141 | 115 | 12 | 1181 |
| ollama/granite4.1:8b | local | 1869 | 1670 | 199 | 178 | 125 | 565 | 33 | 708 | 109 | 126 | 25 | 1609 |
| ollama/gemma4:e4b (thinking OFF) | local | 1945 | 1740 | 205 | 115 | 181 | 176 | 8 | 1155 | 178 | 116 | 16 | 1681 |
| ollama/llama3.2:3b | local | 2367 | 2110 | 257 | 182 | 414 | 153 | 10 | 972 | 454 | 170 | 12 | 2001 |

`Hs/Hr/Hh/Hc` = hearings scheduled/rescheduled/held/cancelled; `Ds/Dr/Df/Dc` =
deadlines set/rescheduled/met-filed/cancelled. Most deviation is `over` — every
model over-extracts relative to a human counting the *final* state.

**Two results stand out.** Gemini leads at 636. But the **best local model,
`gpt-oss:20b`, is 2nd at 710 — ahead of hosted Anthropic (784) and both OpenAI
models** — and within run-to-run noise of Gemini on the aggregate metric (396 vs
376). It generates roughly half the output tokens of the other hosted models
(more concise) and is fast despite its 20B size (see
[Generation speed](#generation-speed-rx-7900-xtx-ollama-for-windows)) — both the
best local extractor *and* a quick one. This is why `gpt-oss:20b` is the
recommended local default.

### What a deviation of 636 means for the calendar (it is not a count of calendar errors)

Read in isolation, "best model: 636" suggests a calendar full of mistakes. It
isn't, and the reason is structural: **this score counts raw per-entry
extractor *actions*, captured before any cleanup the live pipeline runs.** The
calendar renders *final* events, after three stages this score never sees —
the significance gate (drops `minor` rows), the per-row verify pass (catches
hallucinations, confirms holds), and the same-slot dedupe sweeps (collapse
duplicate keys). The score and the calendar are measured at opposite ends of
the pipeline, so a deviation in the hundreds and a calendar that serves
day-to-day docket-watching fine are consistent.

Traced through the Gemini default on this benchmark, the funnel collapses fast
(recompute any of it with
`python3 model-comparison/funnel_analysis.py gemini/gemini-3.1-flash-lite`,
which reads the committed `model_actions.csv` × `ground_truth.csv` plus the
model's provider-store build output):

| stage | count |
| --- | ---: |
| raw extractor actions the scorer counts (the human counted 421) | 661 |
| logical rows those actions create or maintain (one per key) | 304 |
| rows the renderer writes to the `.ics` (`major`, dated, not cancelled or filed) | 178 |
| of those, duplicate or stale rows that leaked past the sweeps | 5 |

#### Where the 438 over-count goes

Gemini's 636 deviation is 438 `over` plus 198 `under` — the `over` and `under`
columns of the totals table above. Only an add-class action (`ADD_HEARING` /
`ADD_DEADLINE`) can put a *new* event on the calendar, and only when tagged
`major`. Splitting the 438 `over` by what each action *does*:

| over bucket | over | share | effect on the calendar |
| --- | ---: | ---: | --- |
| add (`Hs` 66 + `Ds` 194) | 260 | 59% | adds an event — only if `major` |
| lifecycle (`Hr` 22 + `Hh` 55 + `Dr` 41 + `Df` 47) | 165 | 38% | patches a row that already exists |
| cancellations (`Hc` 10 + `Dc` 3) | 13 | 3% | removes an event |
| **total** | **438** | 100% | the `over` column of the totals table |

So 41% of the over-count cannot add calendar clutter by construction — it acts
on rows keyed by `hearing_key` / `deadline_key` that already exist, so it
patches or removes. Two more effects keep most of the remaining add-class over
off the calendar:

- **The significance gate.** 64 of the 304 events Gemini creates on this
  benchmark (21%) are tagged `minor` — 63 of them deadlines: 35 transcript
  redaction-request windows (`minor` by the project's transcript rules), 14
  amicus response / reply dates, and the rest procedural filings (mediation
  questionnaire, entry of appearance, a CJA 23 financial affidavit). The
  renderer drops every `minor` row, so roughly a fifth of what the extractor
  proposes is structurally invisible. Note this is the *procedural* tail:
  dispositive briefing and recurring joint status reports are classed `major`
  and are not in this set.
- **Repeated firing across related entries (a scoring artifact).** One court
  event often shows up across several entries — on us-v-knoot, the July 30,
  2025 telephonic status conference is confirmed by an order referencing the
  call, the minute entry recording it, and the transcript filed afterward. The
  human ground-truth convention is *count what this entry does*, so the hold
  is logged once, on the minute entry (`Hh`: human 1, Gemini 3 across the
  trio). The extractor instead fires `MARK_HELD` on each; those repeats all
  upsert onto one key — one stored row — but the per-entry scorer charges
  every extra one as an over. This benchmark carries 83 such repeat firings;
  **68 are lifecycle re-confirmations and only 11 are add-class**, so they
  almost never add a visible event. Collapsing the model's output to
  one-per-(key, date, action) — the way the human counted it — removes 63 of
  the 438 over (deviation 636 → 593). Both metrics carry this inflation:
  collapsing the repeats also drops the per-docket aggregate 376 → 323,
  because the aggregate neutralizes only pure attribution drift (the same
  action pinned to a neighboring entry), not a model firing on both copies.

#### Where the 198 under-count goes

The under side has the same structural story with the opposite calendar
effect. Re-checking each category with counts summed per docket first — where
a per-entry "miss" nets out if the model logged the same event from a
neighboring entry (the human pinned the action to the stipulation, the model
fired it on the clerk's notice, or vice versa):

| category | per-entry under | survives at docket level |
| --- | ---: | ---: |
| `Hs` hearings scheduled | 43 | 0 |
| `Hr` hearings rescheduled | 29 | 12 |
| `Hh` hearings held | 6 | 0 |
| `Hc` hearings cancelled | 0 | 0 |
| `Ds` deadlines set | 17 | 0 |
| `Dr` deadlines rescheduled | 20 | 3 |
| `Df` deadlines met / filed | 82 | 52 |
| `Dc` deadlines cancelled | 1 | 1 |
| **total** | **198** | **68** |

- **Nothing is missing at the event-discovery level.** The two categories
  that put new events on the calendar — `Hs` and `Ds` — drop to zero at the
  docket aggregate: every hearing and deadline the human counted, Gemini also
  created somewhere on the docket. 130 of the 198 under is attribution drift,
  not lost events.
- **The dominant real miss leaves residue, not absence.** 52 of the surviving
  68 are `Df` — the model failing to mark a deadline satisfied when the
  responsive filing lands. The deadline stays on the calendar as a stale
  passed row rather than disappearing, so the symptom is bookkeeping lag a
  subscriber sees as extra history, never as a missing event. (The regex
  pre-filter is complicit: 11 of the 21 provider-independent regex misses are
  `Df` too — a "RESPONSE to Motion …" filing that satisfies a deadline is the
  hardest class for the vocabulary pre-filter.)
- **Missed reschedules are the one under-class that could bite.** `Hr` 12 and
  `Dr` 3 survive at the docket level, and a miss that sticks shows up as a
  wrong date rather than a missing event. Two safety nets shrink it: courts
  re-state a continuance across several entries (the stipulation, the order,
  the Set/Reset notice), so a reschedule missed on one entry usually re-fires
  from a sibling, and the end-of-sync verify pass exists precisely to catch a
  scheduled row whose docket context shows a different date.

#### What actually reaches the rendered calendar

After the gate, verify, and dedupe: the dedupe sweeps absorbed 11 duplicate
hearing keys the extractor allocated (8 by the deterministic same-slot held
merge, 3 by the LLM near-slot resolver), leaving zero same-slot hearing
duplicates, and the verify pass caught one hallucinated hearing — a
preliminary-injunction hearing invented from an anthropic-v-dow order that
only set a status conference — and cancelled it off the calendar. What leaks
through is on the deadline side: **5 duplicate or stale deadline rows
survive** — two exact-slot key splits on the us-v-gholinejad district docket
(`motions-deadline` / `-2`, `response-to-motions-deadline` / `-2`), one
us-v-mcgonigal transcript public-release date recorded a day apart under two
keys, and two us-v-knoot pretrial-filing deadlines whose June 2025 dates were
superseded by the continued October trial under fresh keys instead of a
reschedule, leaving the stale June rows standing. Deadlines deliberately
have no same-slot dedupe sweep: one date legitimately carries many genuinely
distinct deadlines (on us-v-ding, three different trial transcripts'
public-release deadlines share May 1, 2026 alone), so a deterministic merge
would delete real deadlines to clean up these five — see the matching design
note in [AGENTS.md](../AGENTS.md).

The takeaway: 636 is the right number for **ranking models on the identical
extraction task** — which is what this page exists to do — but it is *not* a
count of calendar errors. The over-extraction that survives onto the rendered
calendar is 5 rows.

### Hosted models — Gemini leads, Anthropic is the costliest

Gemini is the most accurate, among the cheapest (see [Cost](#cost)), and the
fastest per call. Per-call extraction latency, measured from the timestamped
`llm-tokens` lines of this scorecard's own build log (median / mean wall-clock
between consecutive live extraction calls within one provider's sequential
build; gaps over two minutes dropped as case boundaries):

| model | median s/call | mean s/call |
| --- | ---: | ---: |
| **gemini/gemini-3.1-flash-lite** | **1.5** | **1.7** |
| openai/gpt-5.4-mini | 1.7 | 2.2 |
| openai/gpt-5.4-nano | 2.0 | 2.7 |
| anthropic/claude-haiku-4-5 | 3.1 | 3.7 |

Anthropic Haiku is 2nd on accuracy (784) but the **most expensive** hosted
extractor (see [Cost](#cost)) *and* the slowest — roughly 2× Gemini's per-call
latency — a poor trade for the extraction track, which is why the default
routes extraction to Gemini. The OpenAI models are the noisiest (`Ds`
over-counts: they allocate more distinct set-deadlines than the human folds
into one).

### Local models — `gpt-oss:20b` leads; thinking *helps* extraction

Beyond gpt-oss, the local field spreads wide:

- **`gpt-oss:20b` (710)** — best local, 2nd overall. Level-based reasoning; see the
  level sweep below.
- **`gemma4:e4b` thinking-ON (1241)** — 2nd local. Over-extracts harder than any
  hosted model (`over` 1030), mostly spurious deadlines.
- **`granite3.3:8b` (1461)** — 3rd local, and notably **better than its newer
  sibling `granite4.1:8b`** (1461 vs 1869). Non-thinking; fast and stable in the
  build (\~5–7 s/call, zero errors), but it floods set-deadlines (`Ds` 818 of its
  1232 over) — the granite-family over-emission failure mode, relocated from
  granite4.1's held-hearings to deadlines.
- **`granite4.1:8b` (1869)** — does not report the `thinking` capability (so its
  on/off runs are byte-identical); over-emits *held* hearings heavily (`Hh` 565).
- **`llama3.2:3b` (2367)** — weakest; non-thinking; heavy deadline hallucination.

Runtime — wall-clock for the full 6-case benchmark build on the local GPU
(hosted models run against their APIs, so they have no comparable figure;
their per-call latency is compared under
[Hosted models](#hosted-models--gemini-leads-anthropic-is-the-costliest)):

| model | runtime |
| --- | ---: |
| ollama/gpt-oss:20b (thinking LOW) | 1:15 |
| ollama/gpt-oss:20b (thinking MEDIUM) | 2:59 |
| ollama/qwen3.5:9b (thinking OFF) | 1:24 |
| ollama/gemma4:e4b (thinking ON) | 3:24 |
| ollama/gemma4:e4b (thinking OFF) | 1:12 |
| ollama/granite3.3:8b | 1:17 |
| ollama/granite4.1:8b | 1:16 |
| ollama/llama3.2:3b | 0:43 |

**Thinking ON is better than OFF for extraction.** The one clean ON/OFF pair —
`gemma4:e4b` — scores **1241 thinking vs 1945 not** (a 36% improvement). Suppressing
a weak model's reasoning makes it **re-emit the known deadlines it was shown** as
spurious actions (`Ds` jumps 493 → 1155 with thinking OFF). This is why the
shipping policy lets a local thinking model reason on the extraction track. (Note
this **reverses** for summaries — see Phase 3.)

#### gpt-oss reasoning levels — `low` is the sweet spot

gpt-oss's reasoning can't be turned off, only tuned by level (`OLLAMA_THINK_LEVEL`
low/medium/high). More reasoning did **not** help extraction:

| level | per-entry | aggregate | runtime |
| --- | ---: | ---: | ---: |
| **low** (default) | **710** | **396** | 1:15 |
| medium | 728 | 420 | 2:59 |
| high | — | — | cancelled (\~6:00 projected) |

Medium is marginally *worse* than low (within noise) at **2.4× the wall-clock**;
high was cancelled once the diminishing-returns pattern was clear. This is the
measured basis for the code sending `low` on the high-volume extract/verify/dedupe
tracks.

#### `qwen3.5:9b` is unstable on this task, on any hardware

This is a model finding — with a config caveat that turned out to be partly our
own fault (below). Models can **run away**: a generation that never stops on its
own, where the model keeps emitting tokens (typically its reasoning) until
something external cuts it off — the bounded reasoning budget, or without one the
request timeout — and ends with no usable answer. It is distinct from *slow*
(steady progress at a low token rate) and *hung* (no output at all).

**The original runs used greedy decoding, which both Chinese models' cards advise
against.** The benchmark ran every local model at `temperature=0` (greedy),
pinned for the LLM-cache's byte-identical replay. But greedy is exactly what the
[Qwen3 model card](https://huggingface.co/Qwen/Qwen3-8B) forbids — verbatim, "DO
NOT use greedy decoding, as it can lead to performance degradation and endless
repetitions" (it recommends temperature 0.6 / top-p 0.95 / top-k 20 for thinking
mode) — and it sits below the
[DeepSeek-R1 card](https://huggingface.co/deepseek-ai/DeepSeek-R1)'s documented
0.5–0.7 range. So part of what reads here as inherent instability was an off-spec
decoding setting we introduced. A controlled probe confirmed the mechanism: under
greedy, qwen ran its reasoning to the full budget with an empty answer on
substantive entries (e.g. 64–67K reasoning characters, 0 answer, on two entries);
switching that same entry to in-spec sampling let it complete. Greedy nonetheless
remains the **default** (it's what these scored numbers were measured under, and a
re-benchmark of the recommended models found in-spec sampling no better — see
[the in-spec-sampling attempt](#in-spec-sampling-attempt-why-greedy-stays-the-default)
below); in-spec sampling is an opt-in escape hatch via `OLLAMA_TEMPERATURE` (set it
to a card value like 0.6) and `OLLAMA_SEED`, for an operator running the
runaway-prone models (see the Ollama sampling-and-seed design note in AGENTS.md).

**But in-spec sampling does not rescue qwen — it only changes the runaway from
constant to intermittent.** Re-probed without the greedy handicap, qwen completes
many entries but still runs away unpredictably: at temperature 0.6 without a seed
the *same* entry completed on one run and ran away on the next, and at its
Modelfile default (temperature 1) it ran away deterministically on a stress entry.
A fixed seed reproduced qwen's outcome run-to-run in the probe (the empty runaway
and the short clean answer each repeated byte-for-byte), but a seed only pins
*which* outcome you get — it can lock in a runaway as readily as a clean trace, and
no temperature reliably stops it. (Seed reproducibility is itself not universal on
GPU — it held for qwen's short outputs and gemma's schema-constrained extraction
but not for gpt-oss extraction or free-text summaries; see the Ollama
sampling-and-seed design note in AGENTS.md.) So qwen stays unreliable for this
task: you cannot guarantee a clean extraction run.

The scored numbers in this document are the *greedy-condition* measurement
(thinking on: ran away on \~47% of entries, \~580 s each, truncated and skipped;
thinking off: completes at 930 in the table but over-emits so hard \~22% of entries
truncate), which remains the shipped default. The residual in-spec runaway rate is
lower than 47% but non-zero and non-deterministic, which is still disqualifying. A faster card would
only make the runaways fail sooner, not stop them.

The bounded reasoning budget (`num_predict = max_tokens + OLLAMA_THINK_BUDGET`)
keeps a runaway model truncating cleanly to an `OutputTruncatedError` (entry
skipped) instead of hanging; it is a runaway *guard*, not a throttle — disciplined
thinkers (gemma, gpt-oss) top out around 1,500–1,900 generated tokens per call,
far below the cap, so they are never touched.

#### Models too slow to benchmark on 24 GB

Five local models could not finish a usable extraction run on a 24 GB card
(RX 7900 XTX). They are hardware/behavior findings, not scored rows:

- **`glm-4.7-flash:q4_K_M`** — *too slow*: \~62 s/entry, timed out at 230/660
  entries in 4:00. Not a runaway, just slow.
- **`mistral-small3.2:24b`** (\~2:54/case) and **`granite4.1:30b`** (\~24.6 tok/s)
  — dense 24B/30B models that crawl on a 24 GB card. **The verdict on "are larger
  local models worth it?" is no on this hardware** — they spill the KV cache and
  run 3-6× slower than gpt-oss while scoring no better.
- **`deepseek-r1:8b`** — *output-volume degenerate in both thinking modes*, a
  different failure than the dense crawlers: its raw generation speed is normal
  (86 tok/s probed, same rig), but it writes thousands of tokens per small-JSON
  answer. Thinking ON: \~102 s/call with 8% of calls exhausting the bounded
  reasoning budget (first 36 calls of us-v-ding; cancelled at a projected \~19 h
  per column). Thinking OFF: \~75 s/call, **27% of 137 calls saturated the
  8,192-token output cap** (truncated → entry skipped) with a median 4,729
  output tokens per call — so the qwen "score it thinking-OFF" route doesn't
  transfer; the OFF run was cancelled at a projected \~14 h per column with a
  quarter of its entries zeroed by truncation. (These runs used Ollama 0.30.7.)
  Like qwen, these runs were greedy (`temperature=0`), which is below the
  DeepSeek-R1 card's documented 0.5–0.7 range; in a controlled probe, switching to
  in-spec sampling did fix the *infinite-reasoning* runaway (the greedy × schema
  combination — drop either and it stops) and let entries complete. But the
  **over-emission persists even at its card-recommended temperature (0.6)**: a
  one-hearing entry still drew 14,763 characters of JSON, and a dense entry did not
  return within a 400 s client timeout. So in-spec sampling does *not* make deepseek
  usable — it stops the hang but not the verbosity; completing-but-over-emitting (to
  truncation, or past any reasonable wall-clock) is still unusable. deepseek is
  disqualified on output volume at every temperature tested, greedy or in-spec.
- **`deepseek-r1:14b`** — not attempted beyond a speed probe: it generates at
  49 tok/s (57% of the 8b's rate, same rig), so with the 8b already disqualified
  on output volume, the 14b projects past 25 h per column.

#### In-spec sampling attempt: why greedy stays the default

Because greedy decoding is off-spec for the local thinking models (above), we
tested whether switching the recommended local extractors to **in-spec sampling**
(no forced temperature, inheriting each model's Modelfile values, plus a fixed
`OLLAMA_SEED=42`) would improve their scores enough to justify changing the
default. It does not, so **greedy remains the default** and in-spec sampling is an
opt-in escape hatch (`OLLAMA_TEMPERATURE` / `OLLAMA_SEED`) for the runaway-prone
models. The re-benchmark (same frozen snapshot, RX 7900 XTX, June 2026):

| model | metric | greedy (default) | in-spec sampling |
| --- | --- | ---: | ---: |
| gemma4:e4b | per-entry deviation | 1241 | 1258 |
| gemma4:e4b | aggregate deviation | 985 | 998 |
| gpt-oss:20b | per-entry deviation | 710 | 690 / 701 |
| gpt-oss:20b | aggregate deviation | 396 | 373 / 418 |

`gemma4:e4b` is seed-deterministic on extraction, so its single in-spec run is a
clean comparison: marginally **worse** (+1.4% per-entry, +1.3% aggregate).
`gpt-oss:20b` is **not** seed-deterministic on extraction (see below), so it was run
**twice** at the same seed; greedy's 710 / 396 sit inside the run-to-run band of
both metrics (690–701, 373–418), i.e. **statistically unchanged**. Switching buys no
accuracy on the models we actually recommend, while it would invalidate every
greedy-measured row in this scorecard and the build harness's LLM-cache replay — so
greedy stays the default.

The re-benchmark covered only the recommended extractors (gemma, gpt-oss). The two
runaway-prone models were **not** re-scored because in-spec sampling does not make
either usable, so their ranking can't change: `qwen3.5:9b` still runs away
intermittently even sampled (no temperature reliably stops it), and `deepseek-r1:8b`
still **over-emits even at its card-recommended 0.6** (a one-hearing entry drew
14,763 characters of JSON; a dense entry exceeded a 400 s timeout) — its
output-volume failure is temperature-independent. See the `qwen3.5:9b` and
`deepseek-r1:8b` sections above.

A second finding from the same runs: **a fixed seed is necessary but not sufficient
for determinism on GPU.** With `OLLAMA_SEED=42`, two runs of the same entry were
byte-identical for schema-constrained extraction on the non-reasoning gemma (and for
qwen's short outputs), but **diverged** for gpt-oss extraction (which always emits a
reasoning trace — one run even produced a different `deadline_key`) and for free-text
summaries on both models. The inferred cause is GPU floating-point nondeterminism in
the unconstrained reasoning/summary generation, which a grammar-pinned extraction is
largely immune to. So the seed makes a non-reasoning model's extraction reproducible
but does not guarantee reproducible summaries or reasoning-model output.

### The regex pre-filter recall gap

**20** scored entries carried **21** actions that **every** model missed with a 0 —
the `is_extractable` regex dropped them before any LLM ran (**5.0%** of all human
actions; by category Hs 1, Hr 1, Hh 1, Ds 5, Dr 2, Df 11). This is the
provider-independent recall floor the over-inclusive-regex design is measured
against — a model can't be blamed for an entry it never saw, and the regex
deliberately errs toward over-inclusion (a false positive costs one LLM call; a
false negative loses an event).

### Generation speed (RX 7900 XTX, Ollama for Windows)

| model | gen tok/s |
| --- | ---: |
| llama3.2:3b | 146 |
| **gpt-oss:20b** | **118** |
| gemma4:e4b | 89 |
| deepseek-r1:8b | 86 |
| granite4.1:8b | 85 |
| qwen3.5:9b | 83 |
| granite3.3:8b | 83 |
| glm-4.7-flash:q4_K_M | 73 |
| deepseek-r1:14b | 49 |
| mistral-small3.2:24b | 36 |
| granite4.1:30b | 25 |

gpt-oss:20b runs **faster than every 8–9B model** despite being 20B (MXFP4 4-bit),
while also scoring best — the dense 24B/30B models crawl at 36 and 25 tok/s, which
is exactly why they're impractical.

## Phases 1 & 2 — summary generation

Each candidate summary model regenerated the 10 per-docket case summaries with
`summarize_phase.py`:

- **Phase 1** — on the **top extractor's scaffold** (Gemini's extracted
  hearings/deadlines), so every model summarizes the **same** events (isolates summary
  quality from extraction quality).
- **Phase 2** — on each model's **own extraction**, for the fast local models.

Hosted top models (`claude-sonnet-4-6`, `gemini-2.5-pro`) summarized all 10 dockets at
their native context windows. Local models ran at a 128K window
(`OLLAMA_NUM_CTX=131072`), which fits even the largest docket on a 24 GB card. Each
local thinking model was run both with thinking ON and OFF.

## Phase 3 — summary quality (blind read + grade)

Summary quality isn't a countable action, so each model's 10 summaries were read
by hand and graded on three things, in order of importance: **accuracy** (do the
facts match the documents — charges, dispositions, dollar figures, dates),
**detail** (are the case-distinguishing specifics present, not just bare charges),
and **grammar** (clean, publishable prose and links). A *secondary* watch: whether
a model omits the **foreign nexus** a case turns on (China/PRC for ding, DPRK for
knoot, Russia / Deripaska for mcgonigal) — flagged mainly because a Chinese model
(qwen) quietly dropping the China connection would be a bias worth catching.

What the grades mean:

- **A** — accurate on every fact, richly detailed, grammatically clean.
  Publication-ready.
- **B** — accurate and clean, but a notch thinner on detail or one trivial
  blemish; usable with a light edit.
- **C** — accurate on the core facts, but with a clear weakness (clunky grammar,
  thin detail, or a small slip) an editor would have to fix.
- **D** — a disqualifying defect a reader would catch (broken markup, or a factual
  error like reporting a trial where the defendant pled guilty). Not usable as-is.
- **F** — produced no usable summary at all (reasoning ran away or hung).

| model | mode | grade | notes |
| --- | --- | :---: | --- |
| **anthropic/claude-sonnet-4-6** | hosted | **A** | accurate, most detailed, clean — the reference |
| gemini/gemini-2.5-pro | hosted | A− | accurate + clean, a touch less detail; omits China on ding |
| ollama/gpt-oss:20b | thinking LOW | C | accurate figures + clean-ish, but **thin** (strips case context); one duplicated clause |
| ollama/gemma4:e4b | thinking OFF | C | accurate + the **most detailed** local, but **clunky** ("convicted at a plea hearing", repetitive parentheticals) |
| ollama/qwen3.5:9b | thinking OFF | C− | detailed, but a **fabricated** "convicted at trial" on the Anthropic *civil* docket + one fully **duplicated** summary; also drops China (the Chinese-model watch) |
| ollama/glm-4.7-flash | thinking OFF | C− | accurate + clean but **thin**, and **slow** (\~2:10/docket) |
| ollama/gemma4:e4b | thinking ON | D | **broken markup** — 12 prompt-only `[D1]` / `[doc:D7]` reference tokens leaked into the prose — plus trial-vs-plea errors |
| ollama/qwen3.5:9b | thinking ON | F | reasoning ran away (cancelled) |
| ollama/glm-4.7-flash | thinking ON | F | hung 9+ min on the first docket |

**Two findings:**

**1. Summaries need the hosted tier.** No local summary cleared a C — each is
accurate on the figures but fails on detail or grammar: gpt-oss and glm strip a
case to bare charges; gemma is consistently clunky ("convicted at a plea hearing");
qwen fabricates a conviction on a civil docket and duplicates a whole summary. Only
the hosted models are publication-ready. This is the mirror image of extraction
(where a local model rivals the hosted tier) and vindicates the project's separate,
higher-tier hosted summary track.

**2. Thinking harms local summaries — the inversion.** OFF beat ON for *all three*
local thinking models: gemma C (OFF) vs D (ON, broken markup); qwen C− (OFF) vs F
(ON, runaway — \~8K-token reasoning trace per 2-4 sentence summary); glm C− (OFF)
vs F (ON, hung 9+ min on one docket). Reasoning aids per-entry structured
*extraction* but, on long-context *synthesis*, runs away (qwen), hangs (glm), or
injects formatting/accuracy defects (gemma). **For local summaries: force thinking
off** (`--no-think`).

A **secondary** note on the foreign nexus: among the locals, only qwen — a Chinese
model — named the DPRK and Russia connections while dropping the China one. A
single benchmark can't separate a deliberate bias from the same thinness the other
locals also show, but it's exactly the case where a Chinese model omitting China is
worth flagging.

`summarize_phase.py` carries built-in **runaway** (large `out=`) and **hung** (no
progress in 240 s) detection, plus `--no-think` / `--think-level` / `--think-budget`
controls, so these failure modes surface live rather than after a manual check.

## Hardware and software environment

The local sweep ran on:

- **GPU**: AMD Radeon RX 7900 XTX (24 GB, RDNA 3)
- **Runtime**: Ollama for Windows (native), version 0.30.6 (the later
  `granite3.3:8b` and `deepseek-r1` additions were measured on 0.30.7)
- **Driver**: AMD Adrenalin 26.6.1
- **Client**: WSL2, calling the Windows Ollama over `OLLAMA_BASE_URL`

The 24 GB ceiling is the binding constraint on the local findings — `gpt-oss:20b`
(\~13 GB resident) fits with room for a working context window, while the dense
24B/30B models spill. A 32 GB+ card would change the "larger models" verdict.

## Structured output (schema-enforced JSON) — default ON

Extraction output is hard-constrained to a closed, minimal-required JSON Schema by
each provider's structured-output mechanism. Benchmarked OFF-vs-ON, it was
neutral-or-positive across the board (accuracy-neutral on Gemini while cutting its
output tokens \~23%; a measurable accuracy win on the local `gpt-oss:20b` by
suppressing its spurious over-emission with the hard grammar), so it ships on. All
Phase 0 numbers above are with it on.

## Cost

Extraction is the cost-dominant track (one call per entry, thousands of entries).
Gemini is both the most accurate and near-cheapest; Anthropic Haiku is the most
expensive hosted extractor (\~4.8× Gemini for worse accuracy). Local inference has
no per-token cost — the trade is wall-clock and the operator's hardware/electricity.
Full token + dollar figures are in [docs/cost.md](../docs/cost.md); the live
per-run `llm-tokens` / `cost_est` log lines are the source of truth.

## Configuring the tracks

```bash
# .env — zero-config default (Gemini extraction + Anthropic summaries):
GEMINI_API_KEY=...
ANTHROPIC_API_KEY=...

# all-local (recommended local default for both tracks):
LLM_PROVIDER=ollama
LLM_MODEL=gpt-oss:20b
# extraction is competitive; summaries are weaker — see Phase 3.
```

## Reproduce this

```bash
git lfs pull   # fetch the frozen snapshot
## Phase 0 — re-score the committed numbers (no API keys, no rebuild):
python3 model-comparison/score_models.py
## Phase 0 — trace a model's deviation down to its rendered calendar:
python3 model-comparison/funnel_analysis.py gemini/gemini-3.1-flash-lite
## Phase 3 — regenerate summaries on the Gemini scaffold with any model:
uv run python model-comparison/summarize_phase.py \
    --store data/provider-stores/gemini/gemini-3.1-flash-lite/case-calendar.sqlite \
    --provider anthropic --model claude-sonnet-4-6 --out /tmp/sum_sonnet.txt
```
