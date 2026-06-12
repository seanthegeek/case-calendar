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
   *both* the models and the human; with full text it's update agent.
2. **Human scores blind** (`build_scoring_page.py` → `ground_truth.csv`) — one
   offline HTML page, one card per entry showing the complete text the extractor
   saw, beside the eight action-count boxes the extractor emits. No model output
   is shown.
3. **Replay every model** (`build_provider_stores.py --entry-actions-csv`) over the
   same frozen snapshot, capturing per-entry action counts to `model_actions.csv`.
4. **Score deterministically** (`score_models.py`) — join human × model on
   `entry_id`; no model and no opinion in the scoring loop.

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
hallucination); `under` = fewer (missed). Runtime is wall-clock for all 6 cases
on the local GPU (hosted models run against their APIs, so no comparable figure —
their per-call API latency is compared under
[Hosted models](#hosted-models--gemini-leads-anthropic-is-the-costliest) instead).

| model | host | per-entry | aggregate | runtime |
| --- | --- | ---: | ---: | ---: |
| **gemini/gemini-3.1-flash-lite** | hosted | **636** | **376** | — |
| **ollama/gpt-oss:20b** (thinking LOW) | local | **710** | **396** | 1:15 |
| ollama/gpt-oss:20b (thinking MEDIUM) | local | 728 | 420 | 2:59 |
| anthropic/claude-haiku-4-5 | hosted | 784 | 476 | — |
| openai/gpt-5.4-mini | hosted | 879 | 551 | — |
| ollama/qwen3.5:9b (thinking OFF) | local | 930 | 700 | 1:24 |
| openai/gpt-5.4-nano | hosted | 967 | 697 | — |
| ollama/gemma4:e4b (thinking ON) | local | 1241 | 985 | 3:24 |
| ollama/granite4.1:8b | local | 1869 | 1609 | 1:16 |
| ollama/gemma4:e4b (thinking OFF) | local | 1945 | 1681 | 1:12 |
| ollama/llama3.2:3b | local | 2367 | 2001 | 0:43 |

`Hs/Hr/Hh/Hc` = hearings scheduled/rescheduled/held/cancelled; `Ds/Dr/Df/Dc` =
deadlines set/rescheduled/met-filed/cancelled (per-category columns are in
`score_models.py`'s output). Most deviation is `over` — every model over-extracts
relative to a human counting the *final* state.

**Two results stand out.** Gemini leads at 636. But the **best local model,
`gpt-oss:20b`, is 2nd at 710 — ahead of hosted Anthropic (784) and both OpenAI
models** — and within run-to-run noise of Gemini on the aggregate metric (396 vs
376). It generates roughly half the output tokens of the other hosted models
(more concise) and runs at \~118 tok/s locally despite being a 20B model (its
MXFP4 4-bit quant), so it is both the best local extractor *and* fast. This is why
`gpt-oss:20b` is the recommended local default.

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
extractor *and* the slowest — roughly 2× Gemini's per-call latency on top of
\~4.8× its cost — a poor trade for the extraction track, which is why the
default routes extraction to Gemini. The OpenAI models are the noisiest (`Ds`
over-counts: they allocate more distinct set-deadlines than the human folds
into one).

### Local models — `gpt-oss:20b` leads; thinking *helps* extraction

Beyond gpt-oss, the local field spreads wide:

- **`gpt-oss:20b` (710)** — best local, 2nd overall. Level-based reasoning; see the
  level sweep below.
- **`gemma4:e4b` thinking-ON (1241)** — 2nd local. Over-extracts harder than any
  hosted model (`over` 1030), mostly spurious deadlines.
- **`granite4.1:8b` (1869)** — does not report the `thinking` capability (so its
  on/off runs are byte-identical); over-emits *held* hearings heavily (`Hh` 565).
- **`llama3.2:3b` (2367)** — weakest; non-thinking; heavy deadline hallucination.

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

#### Models too slow or unstable to benchmark on 24 GB

Four local models could not produce a usable extraction on a 24 GB card (RX 7900
XTX). They are documented findings, not scored rows:

- **`qwen3.5:9b` thinking-ON** — *runaway*: \~47% of entries exhausted the entire
  reasoning budget producing no answer (\~580 s each). Its thinking-OFF run completes
  (930, in the table) but over-emits so hard that \~22% of entries truncate. A poor
  extractor either way.
- **`glm-4.7-flash:q4_K_M`** — *too slow*: \~62 s/entry, timed out at 230/660
  entries in 4:00. Not a runaway, just slow.
- **`mistral-small3.2:24b`** (\~2:54/case) and **`granite4.1:30b`** (\~24.6 tok/s)
  — dense 24B/30B models that crawl on a 24 GB card. **The verdict on "are larger
  local models worth it?" is no on this hardware** — they spill the KV cache and
  run 3-6× slower than gpt-oss while scoring no better.

The bounded reasoning budget (`num_predict = max_tokens + OLLAMA_THINK_BUDGET`)
keeps a runaway model truncating cleanly to an `OutputTruncatedError` (entry
skipped) instead of hanging; it is a runaway *guard*, not a throttle — disciplined
thinkers (gemma, gpt-oss) top out around 1,500–1,900 generated tokens per call,
far below the cap, so they are never touched.

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
| granite4.1:8b | 85 |
| qwen3.5:9b | 83 |
| glm-4.7-flash:q4_K_M | 73 |
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
- **Runtime**: Ollama for Windows (native), version 0.30.6
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
## Phase 3 — regenerate summaries on the Gemini scaffold with any model:
uv run python model-comparison/summarize_phase.py \
    --store data/provider-stores/gemini/gemini-3.1-flash-lite/case-calendar.sqlite \
    --provider anthropic --model claude-sonnet-4-6 --out /tmp/sum_sonnet.txt
```
