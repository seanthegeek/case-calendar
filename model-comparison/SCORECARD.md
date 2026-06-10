# Provider accuracy vs human ground truth

Scored **992** docket entries (every `reviewed`, non-`bad_ocr` entry across the
benchmark) carrying **421** human-counted actions, across **10** logical dockets
in **6** cases. Lower deviation = closer to the human-read truth.

The shipped default is a **split**: **Gemini** (`gemini-3.1-flash-lite`) for
extraction, **Anthropic** (`claude-sonnet-4-6`) for summaries. Gemini wins this
benchmark on both metrics below, and the 0.16.0 recall fix (reading order PDFs,
plus catching bare "due `<date>`" / filed-brief entries the regex pre-filter
dropped) *widened* its lead rather than narrowing it. The summary track stays
Anthropic for the case-distinguishing detail Sonnet captures (see **Summary
track**).

Optional **local** models (run via Ollama) round out the table. The recommended
local default is **`gpt-oss:20b`** (OpenAI open weights), which scores **751**
per-entry — **2nd overall, ahead of hosted Anthropic and OpenAI**, and the best
LOCAL extractor by a wide margin. **Gemma 4** (`gemma4:e4b`, Gemini's sibling) is
second among the local models at **1090** (its summaries aren't yet
public-page-ready); **Llama 3.2** (`llama3.2:3b`) is **unsuitable** (3437, 3×+
worse — it hallucinates deadlines on nearly every entry). Local scores hinge on a
**thinking-policy fix** (letting a model reason instead of suppressing it — which
cut gemma's deviation from 1471 to 1090) and a bounded reasoning budget that keeps
runaway-prone models from hanging — see **Local models** for the head-to-head.
These models are opt-in (a local Ollama server), so they're not in the default
build — `model_actions.csv` carries their rows for re-scoring, but re-*generating*
the CSV must pass `--extra-variant ollama:<model>` with a live Ollama or the local
rows drop out.

## Methodology — per-entry, blind, against complete-text inputs

This is a different (stronger) method than earlier SCORECARDs, which counted
final hearing/deadline rows per docket against counts read off the CourtListener
web UI. Two problems drove the change: the web UI is **incomplete** relative to
the v4 API ([freelawproject/courtlistener#7429](https://github.com/freelawproject/courtlistener/issues/7429)),
so it under-reported real actions and penalized a correct extractor; and a
per-docket final-row count can't see *where* a model went wrong or whether the
**regex pre-filter** (not the model) dropped an event before any LLM saw it.

The current method:

1. **Freeze a complete-text snapshot** (`snapshot_benchmark.py`) — every entry's
   full `description` + extracted PDF text, not the operational store's
   regex-filtered stubs. A date hidden in a stubbed entry would be invisible to
   *both* the models and the human; with full text it's scoreable.
2. **Human scores blind** (`build_scoring_page.py` → `ground_truth.csv`) — one
   offline HTML page, one card per entry showing the COMPLETE text the extractor
   saw + document links, beside the eight action-count boxes the extractor emits
   (hearings scheduled / rescheduled / held / cancelled; deadlines set /
   rescheduled / met-filed / cancelled). No model output is ever shown.
3. **Replay every provider** (`build_provider_stores.py --entry-actions-csv`)
   over the same frozen snapshot, capturing each provider's per-entry action
   counts to `model_actions.csv`.
4. **Score deterministically** (`score_models.py`) — join human × model on
   `entry_id`; no model and no opinion in the scoring loop.

Two biases are worth naming. **Evaluation bias** (an AI judging AI) is removed —
a human reads the dockets, a dumb script measures deviation. **Prompt-fit bias**
is NOT: the prompts were authored by Claude and run unchanged for every model, a
home-field advantage no blind scoring can neutralize. So this measures *which
model is most accurate at running Case Calendar's actual, Claude-authored
prompts* — the question that matters for this project, because those are the
prompts you'd deploy — not a neutral model-capability claim.

The benchmark is a stratified 6-case sample frozen for reproducibility (see
[README.md](README.md)): us-v-ding (421 entries), anthropic-v-dow (3 dockets:
cadc / ca9 / cand), us-v-knoot, us-v-gholinejad, us-v-mcgonigal, us-v-schmitz.

## Totals — per-entry deviation (lower is better)

Sum over the 8 action categories of |model count − human count|, summed over
all 992 entries. `over` = the model counted MORE than the human (duplicate keys
/ hallucination); `under` = FEWER (missed).

| provider | total | over | under | Hs | Hr | Hh | Hc | Ds | Dr | Df | Dc |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **gemini/gemini-3.1-flash-lite** | **653** | 456 | 197 | 94 | 60 | 62 | 15 | 207 | 77 | 130 | 8 |
| ollama/gpt-oss:20b (local) | 751 | 533 | 218 | 153 | 82 | 45 | 13 | 224 | 80 | 138 | 16 |
| anthropic/claude-haiku-4-5 | 799 | 602 | 197 | 99 | 75 | 74 | 23 | 236 | 99 | 147 | 46 |
| openai/gpt-5.4-nano | 867 | 665 | 202 | 130 | 89 | 77 | 8 | 299 | 101 | 150 | 13 |
| openai/gpt-5.4-mini | 925 | 735 | 190 | 138 | 93 | 62 | 7 | 315 | 129 | 165 | 16 |
| ollama/gemma4:e4b (local) | 1090 | 885 | 205 | 118 | 142 | 158 | 11 | 411 | 118 | 116 | 16 |
| ollama/llama3.2:3b (local) | 3437 | 3188 | 249 | 148 | 392 | 179 | 9 | 1537 | 812 | 354 | 6 |

`Hs/Hr/Hh/Hc` = hearings scheduled/rescheduled/held/cancelled; `Ds/Dr/Df/Dc` =
deadlines set/rescheduled/met-filed/cancelled. Most of the deviation is
`over` — every provider over-extracts relative to a human counting the *final*
state, with the OpenAI models the noisiest of the hosted four (the `Hs`/`Ds`
over-counts: they allocate more distinct scheduled hearings + set-deadlines than
the human folds into one). Anthropic's `Dc` 46 is spurious deadline cancellations
the human didn't count. The standout local result is **`ollama/gpt-oss:20b` at
751 — 2nd overall, ahead of hosted Anthropic (799) and both OpenAI models** — the
recommended local default and the best LOCAL extractor by a wide margin.
`ollama/gemma4:e4b` is second among the local models (1090, over-extracting harder
than any hosted model at `over` 885); `ollama/llama3.2:3b` is more than 3× worse
than gemma (`over` 3188 — it hallucinates deadlines on nearly every entry) and is
unsuitable. See the **Local models** section below. (Local models are measured
under the shipping `think:true` policy — see the thinking-policy note in that
section; the earlier `think:false` design scored gemma far worse.)

## What a deviation of 653 means for the calendar (it is not a count of calendar errors)

Read in isolation, "best model: 653" suggests a calendar full of mistakes. It
isn't, and the reason is structural: **this score counts raw per-entry extractor
*actions*, captured before any cleanup the live pipeline runs.** The calendar
renders *final* events, after three stages this score never sees — the
significance gate (drops `minor` rows), the per-row verify pass (deletes
hallucinations, confirms holds), and the same-slot dedupe sweeps (collapse
duplicate keys). The score and the calendar are measured at opposite ends of the
pipeline, so a deviation in the hundreds and a clean calendar are consistent.

Traced through the Gemini default on this benchmark, the funnel collapses fast:

| stage | count |
| --- | ---: |
| raw actions the scorer counts (the 653 / 421 human-counted) | 719 |
| logical rows those actions create or maintain (one per key) | 342 |
| rows the renderer actually writes to the `.ics` (`major`, not cancelled or filed, dated) | 195 |
| of those, duplicate rows that leaked past the sweeps (all in the past) | 8 |

### Where the 456 over-count goes

Gemini's 653 deviation is 456 `over` plus 197 `under` — the `over` and `under`
columns of the totals table above. Only an `ADD` action can put a *new* event on
the calendar, and only when it is tagged `major`. Splitting just the 456 `over`
by what each action *does* (the `over` slice of each category; the totals table
adds the `under` slice):

| over bucket | over | share | effect on the calendar |
| --- | ---: | ---: | --- |
| `ADD` (Hs 45 + Ds 190) | 235 | 52% | adds an event — only if `major` |
| lifecycle (Hr 34 + Hh 58 + Dr 59 + Df 47) | 198 | 43% | patches a row that already exists |
| cancellations (Hc 15 + Dc 8) | 23 | 5% | removes an event |
| **total** | **456** | **100%** | the `over` column of the totals table |

So 48% of the over-count cannot add calendar clutter by construction — it acts
on rows keyed by `hearing_key` / `deadline_key` that already exist, so it patches
or removes. Two more effects keep most of the remaining `ADD` over off the
calendar:

- **The significance gate.** 78 of the 339 events Gemini newly creates (23%) are
  tagged `minor` — 71 of them procedural deadlines. Most are transcript
  redaction-request windows (`minor` by the project's transcript rules); the rest
  are amicus-brief response/reply dates and procedural filings (mediation
  questionnaire, entry of appearance). The renderer drops every `minor` row, so
  roughly a quarter of what the extractor proposes is structurally invisible.
  Note this is the *procedural* tail: dispositive briefing (MTD/MSJ
  response/reply) and recurring joint status reports are classed `major` and are
  NOT in this set.
- **Repeated firing across related entries (a scoring artifact).** One
  reschedule often shows up across several entries — on us-v-ding, a stipulation
  to continue the status conference, the order granting it, and the clerk's
  `Set/Reset Hearing` notice all reference the same move of that conference to
  2024-05-08. The human ground-truth convention is *count what this entry does*,
  so the reschedule is logged once — on the notice that operatively sets the new
  date (`h_rescheduled`: human 1, Gemini 3 across the trio). The extractor instead
  fires it on each entry; those repeats all upsert onto one key — one stored row —
  but the per-entry scorer charges every extra one as an over. This benchmark carries 98 such repeat
  firings; **88 are lifecycle re-confirmations and only 2 are `ADD`s**, so they
  almost never add a visible event. Collapsing the model's output to
  one-per-`(key, date, action)` — the way the human counted it — removes 68 of
  the 456 over (deviation 653 → 607). Both the per-entry and the per-docket
  aggregate carry this inflation — collapsing the repeats drops the aggregate
  399 → 335 too; the aggregate neutralizes only pure attribution drift (the same
  action pinned to a neighboring entry), not a model firing on both copies.

### What actually reaches the rendered calendar

After the gate, verify, and dedupe: the dedupe sweeps leave **zero duplicate
hearing slots**, and the verify pass deleted 3 hallucinations (2 hearing, 1
deadline). The one place raw over-extraction does leak through is the deadline
side, which has **no dedupe sweep** (only hearings do): **8 duplicate deadline
rows survive** on this benchmark — all transcript-release key-drift on one docket
(us-v-ding), all in the past, so they sit muted at the bottom of the agenda.
Closing that gap needs a deterministic same-slot merge for deadlines, the
analogue of the hearing sweep `_dedupe_concurrent_held_hearings`.

The takeaway: 653 is the right number for **ranking models on the identical
extraction task** — which is what this page exists to do — but it is NOT a count
of calendar errors. The duplicate over-extraction that survives onto the rendered
calendar is 8 rows, all in the past. (The over buckets are
computable from the committed `model_actions.csv` × `ground_truth.csv`; the
funnel, significance split, and duplicate-firing counts come from the Gemini
benchmark build's decision trace and final store.)

## Per-docket-aggregate deviation

The same |model − human|, but counts are summed per logical docket *first* —
robust to the model and human pinning the same action to a slightly different
entry (the docket total is identical either way).

| provider | aggregate deviation |
| --- | ---: |
| **gemini/gemini-3.1-flash-lite** | **399** |
| anthropic/claude-haiku-4-5 | 519 |
| openai/gpt-5.4-nano | 579 |
| openai/gpt-5.4-mini | 619 |
| ollama/gemma4:e4b (local) | 848 |
| ollama/llama3.2:3b (local) | 3075 |

Gemini leads on both metrics, by a wider margin on the attribution-robust
aggregate (399 vs 519) than on per-entry (653 vs 799) — i.e. some of Anthropic's
per-entry penalty is attribution drift, but Gemini still leads after that's
factored out. Among local models gemma (848) is the clear pick; llama (3075) is
3.6× worse.

## The regex pre-filter recall gap — and the 0.16.0 fix

The complete-text method surfaces a class no per-docket count could: entries the
human counted but **every** provider scored 0, because `extractor.is_extractable`
(the cheap regex pre-filter) dropped them before any LLM ran. That's a
provider-independent recall gap — a hole no model choice can fix.

| | entries | actions | % of all human actions |
| --- | ---: | ---: | ---: |
| before 0.16.0 | 37 | 47 | 11.2% |
| **after 0.16.0** | 23 | 23 | **5.5%** |

0.16.0 halved it by naming two forms the regex couldn't express (new
`_DEADLINE_EXTRA_HINTS`): bare **"`<noun>` due `<date>`"** (the old regex only
matched "due by/on" — one D.C. Circuit clerk order set ELEVEN deadlines this
way, all lost), and a **filing that meets a deadline** (a brief / response /
reply at the entry head, or any appellate submission carrying the
`[Service Date: …]` stamp). Both are anchored so "due process" and an order that
merely *mentions* a response don't match. The remaining 23 are the long tail
deliberately not chased: sealing orders that set deadlines only inside the PDF
with no vocabulary in the description, and bare `NOTICE`-type filings
("`NOTICE`" is \~44% attorney-appearance noise, too weak a signal to widen the
filter for).

Those figures are the four hosted providers — the apples-to-apples 0.16.0 code
comparison, and the real provider-independent recall gap. With the two local
models now in `model_actions.csv` (`gemma4:e4b` at think:true and `llama3.2:3b`),
re-running `score_models.py` reports **16 / 16 / 3.8%** instead: the local models'
over-extraction fires on 7 of the 23 — regex-*passed* entries all four hosted
models read as IGNORE — so they no longer satisfy "every provider scored 0"
(gemma recovers 4; llama's hallucination noise covers the other 3). That is a
reclassification by over-eager models, not new regex recall: the regex gap is a
provider-independent property the table measures, and it doesn't move because you
add a model — the 23-entry hosted figure is the honest one.

Separately, 0.16.0 makes the extractor **read every order's PDF** (an order's
operative dates often live only in a schedule table the one-line description
doesn't echo) and **stop fetching transcript bodies** (a transcript is testimony
with no forward-looking scheduling; its held-date / redaction / release
deadlines are already in the description).

## Why the recall fix widened Gemini's lead

The order-PDF + recall fixes added \~90 newly-extractable entries (\~14%). The
effect split by model quality: **Gemini and Anthropic improved** (per-entry
675→653 and 825→799 vs the pre-fix build) — they turned the recovered entries
into correct extractions, lowering `under`. **The OpenAI models regressed**
(836→867, 869→925) — their `over` count rose (nano 614→665, mini 659→735): given
the extra entries they produce *more* spurious actions than the human counted.
So the extra recall is signal for accurate models and noise for over-eager ones,
which sharpens rather than blurs the ranking.

## Why deviation alone doesn't pick the provider

Deviation is one number, and it deliberately does NOT distinguish "missed a
substantive event the human counted" from "extracted a noisy procedural event
the human didn't" — both move the score equally. That's why a *good* Gemini
deviation score coexisted, in earlier releases, with Gemini silently dropping a
long tail of substantive federal-procedure deadline classes (PSR windows, Speedy
Trial Act exclusions, surrender for service of sentence, civil-forfeiture
claim/answer, substantive sealing motion practice, exhibit-filing deadlines,
certified administrative record) when it relied on its training priors — those
dropped off subscriber calendars entirely, and the score never penalized them
hard enough.

0.13.0 closed that gap **in the prompt, for every provider**: a structured
`DEADLINE_SIGNIFICANCE_RULES` block enumerates those classes explicitly and
biases the default toward `major`, so Gemini classifies them as substantive from
the same instructions Anthropic gets rather than from intrinsic priors. The
honest caveat survives: the ruleset names the classes the project currently
knows about; an operator whose caseload carries substantive classes it doesn't
name should verify against their own dockets and can pin Anthropic via
`LLM_EXTRACTION_PROVIDER`. The decision to ship Gemini extraction rests on that
coverage gap being addressed in-prompt — the deviation lead is corroborating
evidence, not the sole basis.

## Summary track — stays Anthropic

The summary track is the one place the comparison favors Anthropic, on a
different basis than extraction. Both run a higher tier (Sonnet 4.6 vs Gemini
2.5 Pro), and Gemini 2.5 Pro writes detailed, case-distinct summaries — it names
defendants, imposed sentences, dollar figures. It is not the weak link the cheap
extraction tier was. Anthropic's remaining edge is narrower and specific: longer
summaries (\~62% more characters) that carry a few categories Gemini's drop —
**statutory citations** (Anthropic cites `41 U.S.C. § 4713` etc.; Gemini cites
none, and it's exactly what distinguishes the three DOW dockets), **count-by-
number enumeration**, and **cancelled/vacated schedule** flags (material on a
docket-watching calendar). The summary track is rare (one call per docket, only
when a primary document or disposition lands), so keeping the higher-detail
provider costs little — see Cost — and the default stays Anthropic on that
margin.

## Local-model benchmarking — environment, thinking policy, and protocol

The local sweep extends the same blind, frozen-snapshot scoring above to a wider
set of [Ollama](../docs/local-llms.md) models. Local inference adds variables a
hosted API hides (which GPU, which runtime, how much reasoning the model is
allowed), so this section records them explicitly. Framed as an experiment:

- **Held constant (controls):** the frozen benchmark snapshot and its
  `ground_truth.csv`; the Claude-authored prompts; `temperature = 0.0` on every
  domain call; the 128K context window; the deterministic `score_models.py`. The
  only thing that varies between rows is the model (and its thinking config).
- **Independent variable:** the model, run on both tracks (extraction and
  summary).
- **Dependent variables:** extraction deviation (accuracy, vs human truth);
  seconds per call (speed); completion rate (reliability — did every call return
  usable output); and, for summaries, a human read against the hosted bars.
- **Named confounds (NOT controlled):** prompt-fit bias (the prompts were
  authored for this project and favor no model — see the Methodology section
  above); per-entry hardware non-determinism (below); the whole-stack difference
  in the cross-GPU speed comparison (below); and single-run measurement (one pass
  per model, no statistical replication — the aggregate scores are stable enough
  that this is acceptable for a recommendation, not a publication).

### Hardware and software environment

Two GPUs were used, recorded in full because local throughput — and, subtly, even
the tokens a model emits — depends on the entire stack, not the silicon alone:

| | A5000 box | 7900 XTX box |
| --- | --- | --- |
| GPU | NVIDIA RTX A5000, 24 GB | AMD Radeon RX 7900 XTX, 24 GB |
| Compute stack | CUDA | ROCm |
| OS | Linux, native | Windows (+ optional native-Linux, below) |
| Ollama version | 0.30.7 | 0.30.6 |
| Context window | 131072 (verified, below) | 131072 |

The 7900 XTX is measured in **multiple stack configurations**, because an early
finding is that the *software stack* — not the silicon — dominates AMD throughput
here, and lumping them into one "7900 XTX" number is misleading. The three configs,
each isolating a layer:

1. **Ollama inside WSL2** (ROCm via the `/dev/dxg` paravirtualization path) — the
   constrained route.
2. **Ollama native on Windows** (WSL2 is only the HTTP client; GPU compute is
   native Windows ROCm).
3. **Ollama native on Linux** (ROCm via `/dev/kfd`) — the cleanest, planned.

Each run records the exact Ollama base URL so its config is unambiguous. The
A5000-vs-7900 XTX timing is therefore a **whole-stack** comparison per config, not
a clean GPU-vs-GPU one (ROCm vs CUDA is itself a real software difference); it is
never reported as a bare "chip A is N× chip B." What the per-config runs settled:
on the build-pipeline metric (s/call, which folds in PDF / store overhead between
LLM calls), **the A5000 (\~9.5) and the 7900 XTX on native Windows (\~10.4) are
comparable** — the A5000 \~10% ahead. A controlled *pure-inference* batch (identical
prompts, each config run **alone** on the card) then separated the 7900 XTX's own
stack layers: native Windows vs **in-WSL2 ROCm-`/dev/dxg`** is **\~12% on extraction
latency and \~23% on generation throughput** (full detail + table in
[docs/local-llms.md](../docs/local-llms.md#windows-wsl2)). The in-WSL2 **host-CPU
overhead was not cleanly isolated** — an apparent sustained high-CPU pin was traced
to a stray harness client running concurrently, not the WSL2 path itself.
Two methodology lessons fell out, both of which nearly produced wrong numbers and
are worth stating: (1) an earlier **\~24 s/call** reading once attributed to "the
7900 XTX" was a **historical artifact** (an older/worse setup or contention), NOT
inherent to in-WSL2 GPU inference — the explicit in-WSL2 run is only \~12% off
native; and (2) a 22-call cold-start sample read \~4.5 s/call and momentarily
*looked* 2× faster than the A5000 before converging to \~9 — **cold-start samples
are not rates; take figures over the full run, and run each config alone**, since a
shared GPU or a stray client produces contention artifacts (e.g. a bogus 6 tok/s).

Per-model weights, parameters, and quantization (read from Ollama's `/api/tags`),
all Q4_K_M except gpt-oss (MXFP4):

| Model | On-disk | Params | Quant |
| --- | ---: | ---: | --- |
| llama3.2:3b | 2.0 GB | 3.2 B | Q4_K_M |
| granite4.1:8b | 5.3 GB | 8.8 B | Q4_K_M |
| qwen3.5:9b | 6.6 GB | 9.7 B | Q4_K_M |
| gemma4:e4b | 9.6 GB | 8.0 B | Q4_K_M |
| gpt-oss:20b | 13.8 GB | 20.9 B | MXFP4 |
| mistral-small3.2:24b | 15.2 GB | 24.0 B | Q4_K_M |
| granite4.1:30b | 17.5 GB | 28.9 B | Q4_K_M |
| glm-4.7-flash:q4_K_M | 19.0 GB | 29.9 B | Q4_K_M |

At the 128K window the KV cache is large: qwen3.5:9b loads at \~10.6 GB of VRAM
(room to spare on the 24 GB card) while glm-4.7-flash sits at \~23.8 GB — at the
ceiling, KV cache squeezed, which is part of why the \~30 B models are both slow
and unstable here (see runaways, below).

### The context window is verified, not assumed

A wrong context window silently truncates long summary inputs, so the 128K setting
(`OLLAMA_NUM_CTX=131072`, forwarded as `options.num_ctx` on every native request)
was confirmed two independent ways, not trusted: (1) Ollama's `/api/ps` reports
the loaded model's `context_length` as `131072`; (2) the token ledger shows real
prompts of up to **54,169** tokens *processed* in a single summary call — far above
the 32,768 default a mis-set window would have truncated to. Both agree the full
window is active.

### Thinking policy and the runaway phenomenon

Every local model that reports the `thinking` capability reasons on **every** track
— the shipping policy, because suppressing reasoning made weak models re-emit their
known events as spurious actions (see the gemma 1471→1090 finding below). The output
budget was originally **unbounded** (`num_predict = -1`) but is now **bounded** at
`max_tokens + OLLAMA_THINK_BUDGET` (default 8192) — a runaway guard added after the
unbounded budget let several boolean-thinking models hang on large inputs (the
runaway phenomenon documented just below). gpt-oss is the exception to the boolean
control: its reasoning is tuned by *level* (`low` on extraction, `high` on
summaries), not on/off — but it is bounded the same way.

Unbounded reasoning has a failure mode that this wider sweep surfaced sharply: on
large inputs some models **run away** — they never emit a stop token and generate
until the request times out, producing no usable result. It is concentrated in the
big boolean-thinking models on the summary track (the largest inputs): on the
summary benchmark, `granite4.1:30b` failed **all 10** summaries this way and
`glm-4.7-flash` failed 5 of 10, each burning the full per-call timeout per failure;
the smaller models (≤ 20 B) and the level-thinking gpt-oss completed cleanly. A
**runaway is distinct from merely slow**: `granite4.1:8b` extraction runs at
\~54 s/call (a long but *finite*, valid-output reasoning trace) — slow, recorded as
a cost, but not a runaway.

**Handling protocol (so the sweep stays honest about what failed):** every model is
attempted. A run is classified a runaway only on a hard signal in its log — a call
timeout, an empty (`No content`) response, an `OutputTruncated`, or an
out-of-memory. On a runaway the model is **stopped**, the failure is **recorded
here with its details**, and — where the model permits it — the model is **re-run
with thinking turned off** (`think:false`, reliable for the boolean-thinking models;
not possible for gpt-oss, whose reasoning can only be lowered to `low`, not
disabled) so the table can still report what that model does as a non-reasoning
extractor. A timed-out or skipped item is left as a **visible gap**, never
backfilled or silently counted as a success.

### Three-phase design (summary quality decoupled from extraction quality)

To separate *which model writes the best summary* from *which model extracts best*
— two different questions a single self-build conflates — the local runs are
structured in three phases:

- **Phase A — summaries on a fixed, best scaffold.** Each model summarizes on top
  of the **Gemini** extraction (the most accurate extractor in the table), so the
  hearings/deadlines every model is handed are identical and only the prose varies.
  This isolates summary-writing quality. Source stores are copied read-only; the
  reference summaries are never overwritten.
- **Phase B — each model's own extraction**, scored against `ground_truth.csv`
  exactly like the hosted models, with the A5000 runtime recorded.
- **Phase C — each model's summary on its own extraction**, i.e. one model for
  both tracks — what a fully-local deployment of that single model would actually
  render.

### Determinism, caches, and the cross-GPU non-determinism finding

Domain calls pin `temperature = 0.0`, so a given (model, prompt) is reproducible on
one box. It is **not** bit-reproducible *across* GPUs: re-running `llama3.2:3b`
extraction on the A5000 vs the 7900 XTX, **605 of 672** entries differ row-by-row —
greedy decoding still diverges across GPU architectures because floating-point
reduction order flips the argmax on near-tied logits. Crucially this **washes out
in aggregate**: the two boxes score **3439 vs 3437** total deviation, a 0.06 %
difference, so the recommendation is stable even though individual entries are not.
(Consequence for the harness: the persistent LLM cache keys on the request, *not*
the endpoint, so a response cached on one box would wrongly replay for the other —
every cross-GPU run therefore uses a **fresh** cache to measure real inference.)

### Summary-quality assessment (Phase A)

Summaries can't be scored by a deterministic count, so they are read by a human
against two fixed bars: **Sonnet** (`claude-sonnet-4-6`, the project's specific/gold
summary model) and **Gemini** (`gemini-2.5-pro`, accurate but vaguer). Three axes:
**accuracy/grounding** (does every stated fact trace to the documents — verdict
outcomes, sentence figures, dates), **readability** (prose quality, and leaks of
internal markup such as bare `[Dn]` tokens or the `[phrase](doc:Dn)` link
convention), and **provenance handling** — specifically whether a summary preserves
the China nexus (us-v-ding) and the North-Korea nexus (us-v-knoot), the
sensitive-origin question this caseload exists to track. Each model is graded only
from its **completed** dump (never mid-run, to avoid reading a not-yet-overwritten
scaffold row).

## Local models — gpt-oss:20b (recommended), gemma4:e4b, and the sweep

The local sweep is complete: eight models run through
[Ollama](../docs/local-llms.md) on the identical frozen benchmark, on **both**
tracks, with the window summaries need. On extraction (per-entry deviation, lower
is better) the ranking is **`gpt-oss:20b` 751 → `gemma4:e4b` 1119 → ... →
`llama3.2:3b` 3439** — so `gpt-oss:20b` is the recommended local default: the best
LOCAL extractor, and at \~13 GB it fits a mainstream 16 GB card. (For comparison,
hosted Gemini scores in the mid-600s on the same benchmark; the best local model
is \~15% behind the hosted default, the price of running free + offline.) The big
boolean-thinking models (granite4.1 30B, glm-4.7-flash, mistral 24B, qwen 9B)
were unstable on large inputs under the old unbounded reasoning budget — see the
thinking-policy and runaway findings below — which is part of why a stable
level-thinking model like gpt-oss wins the default.

### A thinking-policy fix moved gemma from 1471 to 1090

gemma reports the `thinking` capability to Ollama. An earlier design turned
thinking **off** (`think: false`) on the high-volume extraction / verify / dedupe
tracks to save time — and that suppression backfired: denied the chance to reason,
gemma **re-emitted the KNOWN hearings/deadlines it was shown** back as spurious
actions (a single dense entry could dump 20-plus phantom, mostly date-less
`ADD_DEADLINE`s). On the native `/api/chat` endpoint that scored gemma **1471 /
1267**. Letting it reason instead (`think: true`) cured the re-emission and scored
**1090 / 848**, beating even the prior `/v1`-endpoint baseline (1126 / 878). The
only cost is wall-clock (\~20 s/call thinking vs \~7 s not), which is free time on
a local GPU. This is now the shipping default for every local thinking model. The
reasoning budget was originally `num_predict = -1` (**unbounded** — local
inference has no per-token cost), but that let runaway-prone models generate until
the request timed out, so it is now **bounded** at `max_tokens + OLLAMA_THINK_BUDGET`
(default 8192) — a generous runaway guard that leaves disciplined thinkers like
gemma and gpt-oss untouched; see
[docs/local-llms.md](../docs/local-llms.md#thinking-models).

### Extraction — gpt-oss:20b leads the local models; gemma second; llama unsuitable

**`gpt-oss:20b` is the most accurate LOCAL extractor — per-entry 751** — and the
recommended local default. `gemma4:e4b` is second at per-entry **1119** (a
separate scoring run of the same think:true policy put it at 1090 / 848 — the
spread is cross-GPU non-determinism, see that finding above). Both are well behind
hosted Gemini (mid-600s); the gap is over-extraction. Taking gemma as the worked
example: its `under` (misses) ties Gemini (205 vs 197), but its `over` is **885 vs
456**, concentrated in deadlines-set (`Ds` 411 vs 207) and hearing reschedule/held
(`Hr` 142, `Hh` 158) — exactly the spurious over-emission the hard `format`
grammar (structured output, default-on) is best at trimming on local models. Both
local builds were clean (0 entry crashes, 0 truncations, 0 `No content`). The live
pipeline cleans *some* of the over-extraction downstream (the significance gate +
verify/dedupe sweeps), but the raw deviation means materially more noise reaching
those stages than Gemini
produces. The over-eagerness has one upside: gemma fires on **4** regex-*passed*
entries all four hosted models declined (see the regex-miss section) — a rare case
where over-extraction recovers a real action.

`llama3.2:3b` is **unsuitable**: per-entry **3437**, aggregate **3075** — more than
3× gemma. It hallucinates deadlines on nearly every entry (`over` 3188; `Ds`
over-count 1537, `Dr` 812), the small-weak-model failure mode in the extreme. Small
≠ good enough here: at 3B it is too weak for this structured extraction, where
gemma's 4.5B-effective multimodal weights are the practical floor.

### Summaries — not yet public-page-ready

On the summary track gemma4:e4b runs `gemma4:e4b` against hosted
`gemini-2.5-pro`. Two defects stand out beyond terseness:

- **Inline document links don't follow the convention.** The prompt asks the
  model to wrap a phrase as a `[phrase](doc:Dn)` marker the pipeline resolves to
  the real PDF. gemma instead writes bare footnote-style `[D3]` markers (which the
  prompt forbids), so the resolver leaves them untouched and they render to
  subscribers as literal `[D3]` noise. **7 of 10** gemma summaries leak such
  tokens; gemini-2.5-pro and Sonnet leak **0** and resolve **17 / 19** clean
  links respectively.
- **A fabricated verdict.** On us-v-ding gemma wrote that the jury was "acquitted
  of all charges" (verbatim, with a stray `[ ]` bracket around the verb) — an
  ungrounded per-count claim from a verdict form whose extracted text is a blank
  checkbox template. This is the exact failure the project's verdict-form rule
  guards against; gemini-2.5-pro and Sonnet correctly state only that the jury
  "returned its verdict" on the recorded date.

gemma's prose is also shorter and less case-distinguishing (avg **533** chars vs
gemini **688**, Sonnet **1014**) — fewer enumerated counts, less factual detail —
though it lands specifics when present (the Knoot sentence: 18 months, $15,100
restitution, $100 special assessment).

`gemma4:e4b` is a **thinking** model (it reports the capability to Ollama), so its
raised output budget is spent mostly on reasoning: it generates \~1,139 output
tokens per summary but stores only the \~530-char answer. Per-summary token cost
(what the GPU actually generated — the input is the indictment/judgment text plus
the structured-events scaffold):

| Case | Docket | Court | Input tok | Output tok (mostly reasoning) | Stored chars |
| --- | --- | --- | ---: | ---: | ---: |
| anthropic-v-dow | 26-1049 | cadc | 14,720 | 1,134 | 444 |
| anthropic-v-dow | 26-2011 | ca9 | 16,027 | 953 | 607 |
| anthropic-v-dow | 3:26-cv-01996 | cand | 37,406 | 991 | 638 |
| us-v-ding | 3:24-cr-00141 | cand | 52,630 | 1,191 | 439 |
| us-v-gholinejad | 25-4607 | ca4 | 24,734 | 1,369 | 451 |
| us-v-gholinejad | 4:24-cr-00016 | nced | 25,022 | 1,314 | 475 |
| us-v-knoot | 26-5455 | ca6 | 36,476 | 1,176 | 564 |
| us-v-knoot | 3:24-cr-00151 | tnmd | 38,089 | 1,156 | 497 |
| us-v-mcgonigal | 1:23-cr-00016 | nysd | 28,337 | 1,219 | 732 |
| us-v-schmitz | 1:24-cr-00234 | njd | 15,203 | 889 | 485 |
| **average** | | | **28,864** | **1,139** | **533** |

Net: with thinking ON, gemma4:e4b is a reasonable *cost-free* local extractor if
you accept more noise (the downstream sweeps clean part of it), but its summaries
need the inline-link convention fixed — and the verdict fabrication watched —
before they belong on a public calendar. `llama3.2:3b` is not a viable alternative
(3×+ the deviation, deadline hallucination on nearly every entry). That is the
docs' "benchmark before relying on a local model for a public calendar" caveat,
quantified.

## Structured output (schema-enforced JSON) — default on

The extraction call hard-constrains its output to a JSON Schema via each
provider's native mechanism (OpenAI `json_schema`, Gemini `response_schema`,
Ollama `format`, Anthropic tool-use). It was benchmarked OFF vs ON, each pair with
**both runs `--no-llm-cache`** (a warm-cache OFF carried cached extractions for
the \~24 frozen-snapshot PDF entries a fresh ON couldn't reproduce, which
manufactured a phantom recall "tradeoff" until both ran fresh):

| model | metric | OFF | ON | delta |
| --- | --- | ---: | ---: | --- |
| `gemini-3.1-flash-lite` | per-entry deviation | 645 | 636 | −1.4% (noise) |
| | input tokens | 9.64 M | 9.25 M | **−4%** |
| | output tokens | 126 k | 96 k | **−23%** |
| `gpt-oss:20b` | per-entry deviation | 731 | **694** | **−5%** |
| | over-count | 509 | 474 | −35 |

The verdict is **neutral-or-positive across the board**, which is why it ships
on by default. On Gemini it's accuracy-neutral but cuts tokens (the grammar stops
spurious / verbose generation, and fewer spurious rows feed a leaner accumulated
context into later calls) — the win is small precisely because Gemini already
emits clean JSON. On the local `gpt-oss:20b` it's a measurable accuracy WIN, by
suppressing the spurious over-emission that is the dominant local-model failure
mode (the same re-emission the thinking-policy fix addresses) — the hard grammar
is where it earns its keep. Two schema lessons, both pinned by live runs: the
schema must be CLOSED with every field DECLARED (Gemini's grammar degenerates on
an open schema — 5/8 entries degenerate + runaways) but only `type` REQUIRED
(forcing every field required halved Gemini's action-bearing entries). A
follow-on prompt-slim experiment (dropping the now-redundant JSON scaffold) was
tried and **reverted** — it came back token-negative (+2.5% input), because
without the scaffold the model emitted slightly more actions and the
context-accumulation loop outweighed the smaller prompt. See the Structured
Output design note in [AGENTS.md](../AGENTS.md).

## Cost

LLM cost scales with caseload, so the SCORECARD doesn't restate absolute dollars
for the 6-case benchmark — the canonical, full-caseload measured figures live on
the [Cost](../docs/cost.md#llm-cost) page and stay in one place. The shape that
matters for the default:

- On the **extract + verify** pair (the constant load that runs on every sync
  with summaries off), Anthropic costs **\~3.75× what Gemini costs**; OpenAI nano
  is cheapest on raw price but loses on accuracy above. Gemini is also \~1.9×
  faster per call than Anthropic.
- The **summary** upgrade from Gemini 2.5 Pro to Sonnet 4.6 is roughly **$0.04
  per ongoing summary** (rare), worth the case-distinguishing detail for a
  docket-watching audience.
- 0.16.0's order-PDF reading **modestly raises extraction cost** (more PDFs sent
  to the LLM) — on this benchmark \~+$0.06 for the Gemini default across a full
  re-sync, one-time (fingerprint dedup → \~0 steady state), all on the cheap
  small/fast tier. The transcript-exclusion change claws some of that back.

To measure your own, pass `--out model-comparison/cost.md` to
`build_provider_stores.py`; it reports per-provider, per-track cost, wall-clock,
and CourtListener usage for whatever caseload you point it at.

## Configuring the tracks

Zero-config (set the API keys, no `LLM_*` env vars) auto-detects the split:
extraction → Gemini, summaries → Anthropic.

```bash
# .env — zero-config default (Gemini extraction + Anthropic summaries)
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=...
```

To pin explicitly, or run a single provider on both tracks:

```bash
LLM_EXTRACTION_PROVIDER=gemini       # extraction track
LLM_SUMMARY_PROVIDER=anthropic       # summary track
# or force one provider for both tracks:
LLM_PROVIDER=anthropic
```

The global `LLM_PROVIDER` applies to both tracks; the per-track override vars
take precedence over it on their own track.

## Reproduce this

The benchmark snapshot + `ground_truth.csv` ship with the repo, so anyone can
reproduce these numbers or score their own model against the identical inputs —
no rebuild needed to re-score, and no API keys to re-score the committed
`model_actions.csv`. See [README.md](README.md) for the full workflow.
