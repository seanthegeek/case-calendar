# Provider accuracy vs human ground truth

Scored **992** docket entries (every `reviewed`, non-`bad_ocr` entry across the
benchmark) carrying **421** human-counted actions, across **10** logical dockets
in **6** cases. Lower deviation = closer to the human-read truth. All numbers
below are measured under the shipping policy — **structured output ON** (the
schema-enforced JSON default) and, for local thinking models, the **bounded
reasoning budget** with thinking ON for extraction — unless a row's label says
otherwise. The July 2026 local sweep was run twice per model: once **greedy**
(`temperature=0`, the shipping default, matching every prior row) and once
**in-spec** (each model's lowest documented card temperature, fixed seed) — the
`-inspec` rows.

The shipped default remains a **split**: **Gemini** (`gemini-3.1-flash-lite`)
for extraction, **Anthropic** (`claude-sonnet-4-6`) for summaries. The benchmark
ran in four phases:

- **Phase 0 — extraction accuracy**: every model extracts hearings/deadlines from
  the same frozen dockets; a human-blind deviation score ranks them.
- **Phases 1 & 2 — summary generation**: each candidate summary model regenerates
  the per-docket case summaries on the top extractor's scaffold.
- **Phase 3 — summary grading**: the summaries are read and graded for accuracy,
  detail, and grammar against a fresh hosted reference generated on the same
  scaffold (there is no automated summary scorer — see `summarize_phase.py`).

The headline results of the July 2026 sweep (new 32 GB + 24 GB dual-GPU rig,
every installed Ollama model):

- **A local model now tops the aggregate metric.** `gpt-oss:20b` run in-spec
  (its vendor Modelfile `temperature=1.0`, seed 42) scores **332** aggregate —
  ahead of hosted Gemini's 376 — and 666 per-entry (3rd). Two independent
  no-cache runs at the same seed produced identical per-entry counts, so the
  result is reproducible on this rig.
- **A local model ties the hosted leader per-entry.** `gemma4:31b` (greedy)
  scores **640** per-entry vs Gemini's 636 — statistically a tie — at the cost
  of a \~10.5-hour benchmark wall-clock vs \~20 minutes of API calls.
- **Local summaries reach B for the first time.** `gemma4:31b` grades **B**,
  `mistral-small3.2` and `granite4.1:30b` **B−** — overturning the previous
  "no local summary clears a C" finding. The hosted tier is still ahead
  (Sonnet reference: A−), but the gap has narrowed from two letter grades to
  one.
- **Greedy-vs-in-spec is model-dependent**, not a blanket verdict: in-spec
  helped six models (gpt-oss most, −115 per-entry) and hurt three
  (lfm2.5 worst, +177). See the in-spec section for per-model temperatures.
- **Three models could not be benchmarked on this hardware**:
  `glm-4.7-flash` (both quants) and `qwen3.6` crash Ollama's ROCm runner on
  the Radeon AI PRO R9700 at large context sizes — a missing-kernel gap in
  the bundled hipBLASLt for gfx1201, not a model-quality finding. Details
  below.

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
hallucination); `under` = fewer (missed). Rows without a policy suffix are
greedy (`temperature=0`); `-inspec` rows are the same model at its card
temperature with seed 42; historical rows measured on the prior rig are marked.

| model | host | per-entry | over | under | Hs | Hr | Hh | Hc | Ds | Dr | Df | Dc | aggregate |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **gemini/gemini-3.1-flash-lite** | hosted | **636** | 438 | 198 | 109 | 51 | 61 | 10 | 211 | 61 | 129 | 4 | 376 |
| **ollama/gemma4:31b** | local | **640** | 393 | 247 | 152 | 57 | 60 | 7 | 225 | 33 | 105 | 1 | 438 |
| **ollama/gpt-oss:20b-inspec** | local | **666** | 430 | 236 | 136 | 68 | 56 | 9 | 182 | 65 | 137 | 13 | **332** |
| ollama/gemma4:31b-inspec | local | 687 | 421 | 266 | 160 | 57 | 54 | 6 | 268 | 37 | 105 | 0 | 477 |
| ollama/gpt-oss:20b-medium (prior rig) | local | 728 | 508 | 220 | 149 | 60 | 44 | 4 | 234 | 83 | 139 | 15 | 420 |
| ollama/gpt-oss:20b | local | 781 | 550 | 231 | 176 | 70 | 57 | 5 | 246 | 68 | 136 | 23 | 435 |
| anthropic/claude-haiku-4-5 | hosted | 784 | 590 | 194 | 105 | 66 | 84 | 16 | 232 | 95 | 143 | 43 | 476 |
| openai/gpt-5.4-mini | hosted | 879 | 676 | 203 | 113 | 92 | 62 | 4 | 322 | 107 | 173 | 6 | 551 |
| ollama/gemma4:latest-inspec | local | 917 | 704 | 213 | 120 | 136 | 149 | 13 | 276 | 99 | 109 | 15 | 665 |
| ollama/granite4.1:30b-inspec | local | 920 | 719 | 201 | 127 | 101 | 103 | 19 | 305 | 142 | 113 | 10 | 672 |
| ollama/qwen3.5:9b (prior rig) | local | 930 | 676 | 254 | 142 | 56 | 54 | 13 | 490 | 36 | 115 | 24 | 700 |
| openai/gpt-5.4-nano | hosted | 967 | 760 | 207 | 146 | 99 | 83 | 7 | 366 | 131 | 132 | 3 | 697 |
| ollama/gemma4:latest | local | 979 | 774 | 205 | 131 | 191 | 132 | 13 | 281 | 111 | 105 | 15 | 741 |
| ollama/mistral-small3.2:latest-inspec | local | 998 | 806 | 192 | 127 | 105 | 88 | 17 | 368 | 84 | 193 | 16 | 650 |
| ollama/granite4.1:30b | local | 1000 | 800 | 200 | 152 | 126 | 101 | 21 | 343 | 130 | 110 | 17 | 758 |
| ollama/mistral-small3.2:latest | local | 1105 | 913 | 192 | 145 | 142 | 100 | 15 | 383 | 90 | 204 | 26 | 771 |
| ollama/gemma4:e4b (prior rig) | local | 1241 | 1030 | 211 | 140 | 181 | 143 | 13 | 493 | 147 | 112 | 12 | 985 |
| ollama/granite3.3:8b | local | 1275 | 1039 | 236 | 209 | 95 | 67 | 18 | 670 | 89 | 114 | 13 | 981 |
| ollama/granite3.3:8b-inspec | local | 1325 | 1097 | 228 | 198 | 101 | 111 | 15 | 675 | 105 | 117 | 3 | 1043 |
| ollama/granite4.1:8b-inspec | local | 1536 | 1347 | 189 | 206 | 105 | 352 | 31 | 579 | 83 | 157 | 23 | 1230 |
| ollama/granite4.1:8b | local | 1848 | 1635 | 213 | 198 | 94 | 494 | 35 | 765 | 96 | 148 | 18 | 1548 |
| ollama/gemma4:e4b-nothink (prior rig) | local | 1945 | 1740 | 205 | 115 | 181 | 176 | 8 | 1155 | 178 | 116 | 16 | 1681 |
| ollama/lfm2.5:latest | local | 2059 | 1748 | 311 | 607 | 161 | 98 | 9 | 1040 | 35 | 104 | 5 | 1737 |
| ollama/lfm2.5:latest-inspec | local | 2236 | 1946 | 290 | 614 | 141 | 97 | 11 | 1218 | 41 | 108 | 6 | 1894 |
| ollama/llama3.2:3b-inspec | local | 2541 | 2260 | 281 | 189 | 426 | 171 | 16 | 777 | 712 | 208 | 42 | 2011 |
| ollama/llama3.2:3b | local | 2686 | 2413 | 273 | 147 | 378 | 135 | 12 | 1052 | 681 | 276 | 5 | 2274 |

`Hs/Hr/Hh/Hc` = hearings scheduled/rescheduled/held/cancelled; `Ds/Dr/Df/Dc` =
deadlines set/rescheduled/met-filed/cancelled. Most deviation is `over` — every
model over-extracts relative to a human counting the *final* state.

**Three results stand out.** Gemini still leads per-entry (636), but
`gemma4:31b` greedy is a statistical tie at 640, and `gpt-oss:20b` in-spec
takes the **aggregate lead outright** (332 vs 376) — the first time a local
model has beaten the hosted leader on either metric. The per-entry and
aggregate rankings disagree because the aggregate forgives attribution drift
(the same action pinned to a neighboring entry); gpt-oss-in-spec's errors are
mostly drift, while its event discovery per docket is the best measured.

**Prior-rig rows are retained, not re-measured.** `gemma4:e4b`, `qwen3.5:9b`,
and the gpt-oss thinking-MEDIUM level sweep were measured on the previous
24 GB / Ollama-for-Windows rig and those models (or that sweep) were not part
of the July 2026 run; their rows stay for context and are labeled. Rig-to-rig
drift for re-measured models is real and worth naming honestly: on the current
rig gpt-oss greedy scores 781 where the prior-rig blob scored 710 (the
`gpt-oss` blob was also re-pulled between sweeps, so blob and environment both
changed), granite3.3:8b improved 1461 → 1275, and llama3.2:3b worsened
2367 → 2686 — same models, same greedy policy. This matches the documented
GPU-nondeterminism caveat and is why cross-rig comparisons of single rows
should be read loosely; within-sweep comparisons are the reliable ones.

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
  pre-filter is complicit: 10 of the 16 provider-independent regex misses are
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

The hosted rows were not re-run in the July 2026 sweep (their committed rows
are unchanged). Gemini is the most accurate hosted extractor, among the
cheapest (see [Cost](#cost)), and the fastest per call. Per-call extraction
latency, measured from the timestamped `llm-tokens` lines of the prior
sweep's build log (median / mean wall-clock between consecutive live
extraction calls within one provider's sequential build; gaps over two
minutes dropped as case boundaries):

| model | median s/call | mean s/call |
| --- | ---: | ---: |
| **gemini/gemini-3.1-flash-lite** | **1.5** | **1.7** |
| openai/gpt-5.4-mini | 1.7 | 2.2 |
| openai/gpt-5.4-nano | 2.0 | 2.7 |
| anthropic/claude-haiku-4-5 | 3.1 | 3.7 |

Anthropic Haiku is the **most expensive** hosted extractor (see
[Cost](#cost)) *and* the slowest — roughly 2× Gemini's per-call latency — a
poor trade for the extraction track, which is why the default routes
extraction to Gemini. The OpenAI models are the noisiest (`Ds` over-counts:
they allocate more distinct set-deadlines than the human folds into one).

### Local models — the July 2026 sweep (dual-GPU rig)

Every Ollama model installed on the new rig was benchmarked twice (greedy and
in-spec), except the three blocked by the gfx1201 kernel gap (below). Run
policy: one model at a time, the previous model explicitly unloaded first so
every model loads onto empty GPUs (Ollama places a model on the GPU with the
most free VRAM at load time — without the unload step, a still-resident
previous model pushes the next onto the other card, which contaminated one
early timing before the policy was adopted).

Greedy-run wall-clock for the full 6-case benchmark build, with the GPU that
hosted the model:

| model | params | runtime | GPU |
| --- | --- | ---: | --- |
| ollama/llama3.2:3b | 3.2B dense | 1:05:40 | R9700 |
| ollama/granite3.3:8b | 8.2B dense | 1:00:28 | R9700 |
| ollama/gpt-oss:20b | 20.9B MoE (MXFP4) | 1:28:36 | R9700 |
| ollama/granite4.1:8b | 8.8B dense | 1:35:33 | R9700 |
| ollama/mistral-small3.2:latest | 24.0B dense | 1:41:42 | R9700 |
| ollama/granite4.1:30b | 28.9B dense | 2:07:24 | R9700 |
| ollama/lfm2.5:latest | 8.5B MoE | 2:33:14 | R9700 |
| ollama/gemma4:latest | 8.0B dense | 3:38:53 | R9700 |
| ollama/gemma4:31b | 31.3B dense | 10:29:26 | both (split) |

Notes on the two standouts:

- **`gemma4:31b` is the accuracy champion and the wall-clock outlier.** Its
  256K-context KV cache does not fit beside its weights on one card, so it
  runs split across both GPUs at \~56 s per extraction call (a dense 31B
  thinker on court-length prompts), for a \~10.5-hour build. Its run also
  skipped 3 of 992 entries per sweep on the densest us-v-ding filings — a
  single-call timeout on legitimately slow generation, not a decoding
  pathology; the in-spec run skipped 3 entries in the same neighborhood.
  About 18% of the greedy run's calls replayed from the LLM cache after a
  restart, so a fully cold run would be modestly longer than the figure
  above.
- **`lfm2.5` pays a reasoning tax on every call.** Its card documents an
  always-on chain of thought, and it cannot be disabled (see Phase 3) — the
  extra reasoning tokens make an 8.5B MoE slower end-to-end than the 24B
  mistral, and its accuracy (2059) is second-worst anyway. Its first run
  landed on the RX 7900 XTX by placement accident and finished in 2:05:37 vs
  2:33:14 on the R9700 — the same model, same policy, \~18% faster on the
  higher-bandwidth card (see the hardware section).

Accuracy observations within the sweep:

- **`granite3.3:8b` still beats its newer 8B sibling** (1275 vs 1848) — the
  granite4.1:8b held-hearing flood (`Hh` 494) persists on the new rig.
- **The size ladder is real but expensive.** Within the gemma family:
  8B 979 → 31B 640. Within granite: 8B 1848 → 30B 1000. Bigger dense models
  extract better — but only gemma4:31b's jump lands it in hosted territory,
  and at a 3× to 7× wall-clock premium over the 8B models.
- **`llama3.2:3b` remains the floor** (2686), with heavy deadline
  hallucination (`Ds` 1052, `Dr` 681).

### The in-spec sweep — per-model card temperatures, seed 42

Greedy decoding (`temperature=0`) is the shipping default, pinned for
byte-identical LLM-cache replay. Because several model cards recommend
sampling, the entire roster was re-benchmarked **in-spec**: each model at the
lowest temperature its own documentation supports, other sampling knobs
inherited from the vendor's shipped Modelfile, `OLLAMA_SEED=42`. The
temperatures actually used, with their basis:

| model | in-spec temp | basis |
| --- | ---: | --- |
| mistral-small3.2 | 0.15 | model card recommendation |
| lfm2.5 | 0.2 | model card recommendation |
| glm-4.7-flash (not run — blocked) | 0.7 | card's code/terminal preset; default preset is 1.0 |
| qwen3.6 (not run — blocked) | 0.6 | card's lowest thinking-mode preset (coding); general is 1.0 |
| llama3.2:3b | 0.8 | no vendor sampling spec — Ollama's shipped default |
| granite3.3:8b / granite4.1:8b / granite4.1:30b | 0.8 | no vendor sampling spec — Ollama's shipped default |
| gpt-oss:20b | 1.0 | vendor Modelfile default; the card states no range |
| gemma4:latest / gemma4:31b | 1.0 | card's standardized value across all use cases |

The result is **model-dependent**, replacing the prior sweep's blanket
"in-spec sampling buys no accuracy" finding (measured then on gemma4:e4b and
gpt-oss only):

| model | greedy per-entry | in-spec per-entry | delta |
| --- | ---: | ---: | ---: |
| gpt-oss:20b | 781 | 666 | −115 |
| granite4.1:8b | 1848 | 1536 | −312 |
| llama3.2:3b | 2686 | 2541 | −145 |
| mistral-small3.2 | 1105 | 998 | −107 |
| granite4.1:30b | 1000 | 920 | −80 |
| gemma4:latest | 979 | 917 | −62 |
| gemma4:31b | 640 | 687 | +47 |
| granite3.3:8b | 1275 | 1325 | +50 |
| lfm2.5 | 2059 | 2236 | +177 |

Three findings:

- **`gpt-oss:20b` in-spec is the best local configuration measured** — 666
  per-entry and the overall aggregate lead at 332. Two independent runs with
  the cache disabled produced identical per-entry counts at seed 42, so the
  result is reproducible on this rig. (This differs from the prior rig, where
  two same-seed gpt-oss runs diverged — 690 / 701; the runtime stack changed
  from Ollama for Windows 0.30.x to native Linux 0.32.3, and same-seed
  extraction is now stable for this model.) Note the caveat that the greedy
  baseline on the current blob (781) is worse than the prior blob's 710, so
  part of the in-spec delta may be blob drift rather than temperature.
- **Sampling helps the over-emitters most.** The models whose greedy failure
  mode is repetitive over-emission (granite4.1:8b's `Hh` flood, llama's `Ds`
  flood) improve the most; llama3.2's in-spec build also ran a third faster
  (41:19 vs 1:05:40) because sampling breaks its repetition loops.
- **Greedy stays the shipped default.** The two models at the top of the
  local table split on the question (gemma4:31b prefers greedy, gpt-oss
  prefers in-spec), in-spec runs sacrifice the LLM-cache's byte-identical
  replay, and the win is configuration-specific. An operator who wants the
  measured gpt-oss optimum can opt in with `OLLAMA_TEMPERATURE=1.0` and
  `OLLAMA_SEED=42` — the same escape-hatch knobs as before, now with a
  measured reason to use them on one model.

### Three models blocked on this hardware — the gfx1201 kernel gap

`glm-4.7-flash:latest`, `glm-4.7-flash:q8_0`, and `qwen3.6:latest` could not
be benchmarked: each crashes Ollama's ROCm runner when loaded on the Radeon
AI PRO R9700. The journal signature is identical for all three —
`rocblaslt error: Cannot read "TensileLibrary_lazy_gfx1201.dat"` followed by
`ROCm error: no kernel image is available for execution on the device` and a
llama-server abort — i.e. the hipBLASLt library bundled with Ollama 0.32.3's
ROCm build lacks the gfx1201 (RDNA 4) kernel library that the
glm4moelite/deepseek2 and qwen35moe matmul paths request. This is a
**hardware/runtime packaging finding, not a model-quality claim** — the same
class of caveat as the prior scorecard's "too slow to benchmark on 24 GB"
section, with the cause visible in the server journal rather than inferred.

Probing narrowed the trigger: all three models run normally on the same card
with small contexts (the generation-speed table below includes all three),
and glm survives a 13K-token prompt at `num_ctx` 16384 — but crashes at 32768
and above. The benchmark needs large contexts (28% of its extraction prompts
exceed 14K tokens; p95 is 20.7K), so no client-side setting can carry the
full run. The paths to benchmarking them, in order of preference: an Ollama
release whose ROCm bundle ships gfx1201 hipBLASLt kernels; or a service-level
`ROCBLAS_USE_HIPBLASLT=0` environment override (falls back to classic rocBLAS
kernels); or pinning the Q4 glm to the RX 7900 XTX (gfx1100, fully supported)
via `HIP_VISIBLE_DEVICES` — the 31 GB q8_0 cannot fit that card. Their
planned run configuration (greedy + in-spec at 0.7 / 0.6) is staged in the
comparison tooling and runs unchanged once one of those lands.

Dense architectures (llama, granite, gemma, mistral) and gpt-oss (MoE,
MXFP4) are unaffected on gfx1201; lfm2.5 (`lfm2moe`) was explicitly re-run on
the R9700 and is also unaffected.

### Prior-generation findings retained

- **The two Chinese reasoning models of the prior sweep** — `qwen3.5:9b` and
  `deepseek-r1:8b` — were benchmarked on the previous rig under greedy
  decoding, which both their cards advise against (the
  [Qwen3 card](https://huggingface.co/Qwen/Qwen3-8B): "DO NOT use greedy
  decoding, as it can lead to performance degradation and endless
  repetitions"; the [DeepSeek-R1 card](https://huggingface.co/deepseek-ai/DeepSeek-R1)
  recommends 0.5–0.7 "to prevent endless repetitions"). Bringing them in-spec
  did not make either usable — qwen's runaways became intermittent rather
  than gone; deepseek's over-emission persisted at every temperature tested —
  so both remain disqualified with their prior-rig rows retained. Neither
  model is installed on the current rig. Their successor `qwen3.6` removes
  the explicit greedy prohibition from its card (it recommends sampling
  presets and documents `presence_penalty` 0–2 against "endless
  repetitions") — whether its behavior actually improved is untestable here
  until the gfx1201 gap is fixed.
- **The gpt-oss reasoning-level sweep** (prior rig): low 710 / 396, medium
  728 / 420 at 2.4× the wall-clock, high cancelled at \~6:00 projected. The
  code still sends `low` on the high-volume extract/verify/dedupe tracks; the
  level sweep has not been repeated on the current rig or blob.
- **Thinking ON vs OFF for extraction** (prior rig, gemma4:e4b): 1241
  thinking vs 1945 not — the measured basis for the shipping policy of
  letting local thinking models reason on the extraction track. (The
  inversion for summaries also persists — see Phase 3.)

### The regex pre-filter recall gap

**16** scored entries carried **16** actions that **every** model missed with a
0 — the `is_extractable` regex dropped them before any LLM ran (**3.8%** of all
human actions; by category Ds 5, Dr 1, Df 10). The count shrank from the prior
sweep's 20 entries / 5.0% because the wider model roster now catches four of
the old all-zero entries — with 26 model configurations, an entry only lands
here when the regex truly never let any model see it. This is the
provider-independent recall floor the over-inclusive-regex design is measured
against — a model can't be blamed for an entry it never saw, and the regex
deliberately errs toward over-inclusion (a false positive costs one LLM call;
a false negative loses an event). Ten of the sixteen are `Df` — a
"RESPONSE to Motion …" filing that satisfies a deadline remains the hardest
class for the vocabulary pre-filter.

### Generation speed (dual-GPU, Ollama 0.32.3, native Linux)

Measured with a fixed prompt, greedy, 512 generated tokens, `num_ctx` 8192,
one model loaded at a time with placement verified in the server journal.
Without the context cap, models load at their native maximum context and
split across both GPUs, which understates single-card speed — an earlier
uncontrolled pass measured granite3.3 at 29 tok/s split vs 86 single-card.

| model | R9700 gen tok/s | RX 7900 XTX gen tok/s |
| --- | ---: | ---: |
| lfm2.5:latest (8.5B MoE) | 207.9 | — |
| llama3.2:3b | 164.6 | 188.9 |
| gpt-oss:20b (MoE, MXFP4) | 105.8 | 138.5 |
| granite3.3:8b | 86.3 | 99.9 |
| gemma4:latest (8B) | 85.3 | — |
| granite4.1:8b | 83.3 | — |
| qwen3.6:latest (36B-A3B MoE) | 81.4 | — |
| glm-4.7-flash:latest (30B-A3B MoE, Q4) | 77.3 | — |
| glm-4.7-flash:q8_0 | 70.1 | — |
| mistral-small3.2 (24B) | 35.9 | 46.1 |
| granite4.1:30b (29B) | 29.3 | — |
| gemma4:31b (31B) | 25.4 | — |

Two hardware findings:

- **The RX 7900 XTX generates 15–31% faster than the R9700 on every model
  measured on both.** Token generation is memory-bandwidth-bound, and the
  XTX's published 960 GB/s outruns the R9700's 640 GB/s
  ([Sapphire's R9700 spec](https://www.sapphiretech.com/en/commercial/radeon-ai-pro-r9700));
  the full-benchmark cross-check agrees (lfm2.5: 2:05:37 on the XTX vs
  2:33:14 on the R9700). The R9700's contribution is **capacity**: its 32 GB
  is what makes the 24–31B dense models runnable at all, and the pair
  together host gemma4:31b's split-GPU KV cache.
- **MoE beats dense at equal size for speed.** The 36B-A3B qwen3.6 generates
  at 8B-class speed (81 tok/s) while the dense 29–31B models crawl at 25–29;
  gpt-oss remains faster than every dense 8B. The prior scorecard's verdict
  that "larger local models aren't worth it" was a 24 GB-VRAM artifact: with
  32 GB, dense-30B models are benchmarkable — but only `gemma4:31b` converts
  the size into hosted-tier accuracy, and MoE models are the ones that
  convert size without the speed penalty.

## Phases 1 & 2 — summary generation

Each candidate summary model regenerated the 10 per-docket case summaries
with `summarize_phase.py` on the **top hosted extractor's scaffold** (a fresh
Gemini extraction store), so every model summarizes the same events. Local
models ran under the shipping local-summary policy: greedy, thinking OFF
(`--no-think`) for boolean thinkers, level low for gpt-oss, 128K context
window. A fresh `claude-sonnet-4-6` reference was generated on the **same
scaffold** so the grades below compare like against like. All nine local
models completed all 10 dockets with zero runaways and zero hangs — the
prior generation's runaway/hung failure modes (qwen3.5 thinking-ON F,
glm F) did not recur on this roster; the built-in RUNAWAY / HUNG detectors
stayed quiet.

## Phase 3 — summary quality (read + grade)

Summary quality isn't a countable action, so each model's 10 summaries were
read and graded on three things, in order of importance: **accuracy** (do the
facts match the documents — charges, dispositions, dollar figures, dates),
**detail** (are the case-distinguishing specifics present), and **grammar**
(clean, publishable prose and links). A *secondary* watch: whether a model
omits the **foreign nexus** a case turns on (China/PRC for ding, DPRK for
knoot, Russia / Deripaska for mcgonigal). Every disputed fact was verified
against the scaffold store itself — the decisive check this round was the
us-v-ding jury verdict, whose docket entry (id 452067644, January 29, 2026,
"JURY VERDICT … Guilty on Count 1-4,…8-14", with the full verdict-form text
in the store) settled several models' claims in both directions.

What the grades mean:

- **A** — accurate on every fact, richly detailed, grammatically clean.
  Publication-ready.
- **B** — accurate and clean, but a notch thinner on detail or one trivial
  blemish; usable with a light edit.
- **C** — accurate on the core facts, but with a clear weakness (clunky
  grammar, thin detail, or a small slip) an editor would have to fix.
- **D** — a disqualifying defect a reader would catch (broken markup, or a
  factual error like reporting a trial where the defendant pled guilty). Not
  usable as-is.
- **F** — produced no usable summary at all (reasoning ran away or hung).

| model | mode | grade | notes |
| --- | --- | :---: | --- |
| anthropic/claude-sonnet-4-6 | hosted, same scaffold | A− | richest context (McGonigal's FBI counterintelligence role, the Robbinhood ransomware victims, the DPRK laptop-farm mechanism); one slip — dates the ding jury verdict to the court's blank form (Jan 27) instead of the verdict entry (Jan 29) and omits the outcome |
| **ollama/gemma4:31b** | thinking OFF | **B** | best local set measured: correct plea/appeal posture on every docket, accurate figures with per-fact links, clean prose; one defect — claims "no document in the record establishes" the ding verdict outcome when the store's verdict entry states it |
| ollama/mistral-small3.2 | non-thinking | B− | zero fabrications, correct posture everywhere ("appealing his conviction … to which he pled guilty"), names the China nexus; one docket returned an "insufficient documents" sentinel instead of a summary, and the ding verdict date slips to Jan 27 |
| ollama/granite4.1:30b | non-thinking | B− | the only model fully right on the ding verdict (date, outcome, cites the form); Judge Rita F. Lin, Robbinhood, four named victims; but calls the Ninth Circuit docket "the District of Columbia Circuit", leaks a `[D4]` token, and bolds keywords through plain prose |
| ollama/granite3.3:8b | non-thinking | C | most detailed 8B (TPU/GPU/SmartNIC secrets, the PRC companies, the $40,000 fine, the surrender date); same wrong-circuit slip on ca9, and misattributes the "Silicon Valley ideology" framing to Anthropic's own argument |
| ollama/gemma4:latest | thinking OFF | C | accurate figures; recurring "convicted at a plea of guilty" clunk, a duplicated clause, one raw scaffold-token leak, and appeal language on a district docket |
| ollama/granite4.1:8b | non-thinking | C− | detailed but calls the civil D.C. Circuit petition "appealing its conviction", writes meta-language about "the provided documents", predicts a sentence in future tense, and numbers two different counts as Count 8 |
| ollama/gpt-oss:20b | level low | D | regression from the prior C: "convicted at trial" twice for a defendant who pled guilty (the scale's canonical disqualifier), "no verdict has yet been entered" on ding (the verdict is in the store), reference-token leaks in four summaries, doubled-word grammar defects |
| ollama/lfm2.5 | `--no-think` (ineffective) | D | its full chain of thought leaks into every stored summary as literal `<think>` blocks — the card documents the reasoning as inline in the content, so Ollama's thinking control cannot strip it; the answers underneath are accurate |
| ollama/llama3.2:3b | non-thinking | D | one docket's "summary" is the literal token `[D7]`; a civil docket gains a fabricated conviction; an identical invented sentence ("60 months … $2 million restitution") recycles across three unrelated cases |

**Three findings:**

**1. Local summaries reached B.** The prior sweep's conclusion — no local
summary clears a C, summaries need the hosted tier — is overturned at the top
of the size ladder: `gemma4:31b` (B), `mistral-small3.2` (B−), and
`granite4.1:30b` (B−) are all usable with a light edit. The hosted tier still
leads (A−), and the recommended split (hosted Sonnet for summaries) still
holds for a public page, but an all-local deployment now has genuinely
publishable-with-edit options — at the summary track's low call volume, the
wall-clock cost is minutes.

**2. Model size moved summary quality more than anything else.** The three
B-range locals are the three largest models that ran (31B, 24B, 29B); every
8B-and-under model graded C or worse. This is the same size ladder as
extraction, but steeper — and unlike extraction, the small models fail on
*integrity* (fabricated posture, token leaks, template reuse), not just
detail.

**3. The truthfulness guard cuts both ways.** The summary prompt's
no-unsupported-claims machinery produced honest refusals where weaker models
fabricated (mistral's "insufficient documents" sentinel beats llama's
`[D7]`), but also over-hedging: gemma4:31b and the Sonnet reference both
declined to state a jury verdict that is plainly in the record. The one model
that threaded the needle — granite4.1:30b — read the verdict form and cited
it. Thinking stays OFF for local summaries per the prior sweep's measured
inversion; nothing in this round contradicts it, and the roster's one
always-on reasoner (lfm2.5) is precisely the one with the disqualifying
markup leak. A candidate code hardening from that finding: strip inline
`<think>` blocks in the Ollama response path for models that emit reasoning
in content.

The foreign-nexus watch was quiet this round: every model that produced usable
summaries named the PRC, DPRK, and Deripaska connections where the scaffold
carried them; the prior sweep's one flagged case (Chinese-model qwen dropping
the China nexus) had no Chinese model in this roster to re-test — qwen3.6 is
blocked on the gfx1201 gap.

`summarize_phase.py` carries built-in **runaway** (large `out=`) and **hung**
(no progress in 240 s) detection, plus `--no-think` / `--think-level` /
`--think-budget` controls, so these failure modes surface live rather than
after a manual check.

## Hardware and software environment

The July 2026 local sweep ran on:

- **GPUs**: AMD Radeon AI PRO R9700 (32 GB, RDNA 4, gfx1201) + AMD Radeon
  RX 7900 XTX (24 GB, RDNA 3, gfx1100)
- **Runtime**: Ollama 0.32.3, native Linux (kernel 7.0.0-28), ROCm bundle
  `rocm_v7_2`
- **CPU / RAM**: AMD Ryzen 9 9950X3D, 94 GB visible to Ollama

Placement policy for all measured runs: one model at a time, previous model
unloaded first, placement read back from the server journal. The prior sweep
(the retained rows) ran on the RX 7900 XTX alone under Ollama for Windows
0.30.x from WSL2. The 32 GB card removes the prior rig's binding constraint —
dense 24–31B models now complete the benchmark — and shifts the interesting
constraints to per-token speed (the XTX is the faster card; the R9700 is the
bigger one) and, for two model families, the gfx1201 kernel gap above.

## Structured output (schema-enforced JSON) — default ON

Extraction output is hard-constrained to a closed, minimal-required JSON Schema by
each provider's structured-output mechanism. Benchmarked OFF-vs-ON, it was
neutral-or-positive across the board (accuracy-neutral on Gemini while cutting its
output tokens \~23%; a measurable accuracy win on the local `gpt-oss:20b` by
suppressing its spurious over-emission with the hard grammar), so it ships on. All
Phase 0 numbers above are with it on.

## Cost

Extraction is the cost-dominant track (one call per entry, thousands of entries).
Gemini is both the most accurate hosted extractor and near-cheapest; Anthropic
Haiku is the most expensive hosted extractor (\~4.8× Gemini for worse accuracy).
Local inference has no per-token cost — the trade is wall-clock and the
operator's hardware/electricity, and the runtime table above is that trade made
concrete (a full local benchmark build spans 1:00 to 10:30 per model). For
scale: rebuilding the Gemini extraction scaffold for this sweep's summary phase
cost $1.24 (672 extraction + 37 verify calls). Full token + dollar figures are
in [docs/cost.md](../docs/cost.md); the live per-run `llm-tokens` / `cost_est`
log lines are the source of truth.

## Configuring the tracks

```bash
# .env — zero-config default (Gemini extraction + Anthropic summaries):
GEMINI_API_KEY=...
ANTHROPIC_API_KEY=...

# all-local (recommended local default for both tracks):
LLM_PROVIDER=ollama
LLM_MODEL=gpt-oss:20b
# extraction is competitive at greedy and best-in-class with the measured
# in-spec opt-in (OLLAMA_TEMPERATURE=1.0 plus OLLAMA_SEED for repeatability);
# summaries on this model are not publication-ready — see Phase 3.

# all-local, accuracy-first (slow — dense 31B; see the runtime table):
# LLM_MODEL=gemma4:31b
# extraction ties hosted Gemini; summaries grade B, the best local measured.
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
