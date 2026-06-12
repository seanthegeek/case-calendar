# Model comparison

The default is a **split**: Gemini (`gemini-3.1-flash-lite`) for extraction,
Anthropic (`claude-sonnet-4-6`) for summaries — and here are the data and tools
to check that yourself, including scoring it against your own reading of the
dockets, or re-running the whole thing on other cases and models.

The numbers and the per-provider analysis live in **[SCORECARD.md](SCORECARD.md)**.
This README is the *method*: what's measured, why it's set up this way, and how
to reproduce or extend it.

> **A note on what the score does and does not measure.** The deviation-from-
> human-truth score this comparison reports is necessary but not sufficient.
> The default has flipped repeatedly along this fault line and the lesson
> has stayed the same: aggregate deviation alone doesn't decide which provider
> a public docket-watching calendar should ship with.
>
> Through 0.11.0 the extraction default stayed **Anthropic** for a coverage
> reason, not a deviation-score reason: relying on its training priors, Gemini
> systematically classified a long tail of substantive federal-procedure
> deadline classes (PSR windows, Speedy Trial Act exclusions, surrender for
> service of sentence, civil-forfeiture claim/answer, substantive sealing motion
> practice, exhibit-filing deadlines, certified administrative record) as
> `procedural-minor` and dropped them at the render-time significance gate — off
> subscriber calendars entirely — and the deviation score never penalized those
> drops hard enough, because they're failure modes outside the common cases the
> aggregate rewards.
>
> 0.13.0 made **Gemini** the extraction default by closing that gap **in the
> prompt, for every provider**: a structured `DEADLINE_SIGNIFICANCE_RULES` block
> enumerates those classes explicitly and biases the default toward `major`, so
> Gemini classifies them as substantive from the same instructions Anthropic
> gets rather than from intrinsic priors (its training didn't change; the prompt
> now carries the priors). The honest caveat survives — the ruleset names the
> classes the project currently knows about, so an operator whose caseload
> carries substantive classes it doesn't name should verify against their own
> dockets and can pin Anthropic via `LLM_EXTRACTION_PROVIDER`. The summary track
> stays Anthropic because Sonnet pulls more case-distinguishing detail (statute
> citations, count numbers, sentence breakdowns, cancelled-schedule notes). See
> [SCORECARD.md](SCORECARD.md) for the current head-to-head numbers and the
> per-track override env vars.

Case Calendar can run on any of three providers (Gemini / OpenAI / Anthropic;
one line in `config.yaml`). To pick a default we rebuild every tracked case's
extraction from the **same** frozen court data with several model configurations
and compare cost and accuracy. Each configuration is one **model**: by default
one per provider at its out-of-the-box models, plus an extra model that varies
the *extraction* model within a provider to test whether a pricier tier earns
its keep (`gpt-5.4-mini` against the OpenAI default `gpt-5.4-nano`). This folder
holds the tooling, the model-output data, and a scoring method designed so you
don't have to take our word for any of it.

## Why this is set up the way it is

Ranking models on accuracy invites two distinct biases. This setup removes one
of them; the other is an inherent limitation that no method here can fix, and
it's only honest to say so plainly.

1. **Evaluation bias** — an AI asked to judge AI output tends to favor its own
   family. This *is* removed: accuracy is decided by a **human reading the public
   dockets** (blind — scoring the docket itself, never any model's output), and a
   **dumb deterministic script** (`score_models.py`) measures how far each
   model's per-entry counts deviate from the human's. No model and no opinion is
   in the scoring loop.
2. **Prompt-fit bias** — Case Calendar's extraction and summary prompts were
   written by Anthropic's Claude, and the comparison runs those *same* prompts for
   every model. A model may simply respond better to prompts written by its own
   family. **Nothing here neutralizes this**: blind scoring can't, because the
   prompts don't change between models. It's a home-field advantage baked into
   the comparison itself.

So scope the result accordingly. This measures **which model is most accurate at
running Case Calendar's actual, Claude-authored prompts** — the question that
matters for *this project*, because those are the prompts you would deploy. It is
**not** a neutral claim that Claude is the most capable model. The conclusion is
a **default** recommendation, not "the best model."

## How accuracy is measured — per entry, against complete text

The human and the model are compared **per docket entry**, on the eight action
counts the extractor itself emits:

```text
hearings:  scheduled / rescheduled / held / cancelled
deadlines: set / rescheduled / met-filed / cancelled
```

Two design choices make this honest:

- **Complete-text inputs.** The benchmark snapshot carries every entry's full
  text (description + extracted PDF), not the operational store's regex-filtered
  stubs. A date hidden in a stubbed entry would be invisible to *both* the models
  and the human — so a real date the regex pre-filter dropped shows up as a
  **provider-independent miss** (an entry the human counted but every model
  scored 0), which is exactly the recall gap a per-docket count can't see. (The
  CourtListener web UI is itself incomplete relative to the v4 API —
  [#7429](https://github.com/freelawproject/courtlistener/issues/7429) — which is
  why the human reads the API text, not the page.)
- **Blind scoring.** The human fills the counts from an offline HTML page that
  shows each entry's text + document links but **no model output**. The
  committed model output is the per-entry counts CSV (`model_actions.csv`), not
  the rendered calendars (which would be easy to peek at and stay gitignored
  under `data/provider-stores/`).

The benchmark is a **stratified 6-case sample** frozen for reproducibility:
us-v-ding (the dense one), anthropic-v-dow (3 dockets: D.C. Cir. / 9th Cir. /
N.D. Cal.), us-v-knoot, us-v-gholinejad, us-v-mcgonigal, us-v-schmitz — 992
scoreable entries, 421 human-counted actions, 10 logical dockets.

## What's here

| Path | What it is |
| --- | --- |
| `ground_truth.csv` | **The human truth** — one row per scored entry, the eight per-entry counts, `reviewed` / `bad_ocr` flags. Ships filled, so you can re-score without re-reading anything. |
| `model_actions.csv` | **Model output** — one row per (provider column, entry) with the same eight counts, captured by `build_provider_stores.py --entry-actions-csv`. The `provider` column is the label `provider/extraction-model` (e.g. `gemini/gemini-3.1-flash-lite`). |
| `score_models.py` | The deterministic scorer. Joins the two CSVs on `entry_id` and reports per-provider deviation. Pure stdlib — no API keys, no rebuild. |
| `funnel_analysis.py` | Traces one model's deviation down to its rendered calendar — over/under buckets, significance-gate and repeat-firing effects, verify/dedupe cleanup, and the duplicate candidates left in the final store. CSV math needs nothing beyond the two committed CSVs; the store/log/ICS sections need that model's `build_provider_stores.py` output on disk. Backs the SCORECARD's "what a deviation means for the calendar" section. |
| `SCORECARD.md` | The written analysis + current numbers backing the default-provider choice. |
| `build_scoring_page.py` | Generates the offline HTML scoring page (`ground_truth_scoring.html`) for filling `ground_truth.csv` blind. |
| `snapshot_benchmark.py` | Builds the frozen, full-text benchmark snapshot (`snapshots/benchmark-store.sqlite`, committed via Git LFS). |
| `build_provider_stores.py` | Rebuilds every comparison model from the same court data. Needs API keys; costs money. |
| `summarize_phase.py` | Regenerates the per-docket case **summaries** on a chosen store with a chosen summary provider/model (via `summary.refresh_stale`, copying the store first so the source is never mutated) and dumps the prose to a text file. This is the qualitative complement to the deterministic *extraction* scoring above — summary quality (accuracy / readability / provenance such as a China or DPRK nexus) is read and graded by hand, not scored by `score_models.py`. Point every model at the same top-extractor scaffold for an apples-to-apples summary comparison, or run each on its own extraction. |
| `snapshots/` | The committed benchmark snapshot + its `.manifest.json` (date, row counts, sha256). Fetch the `.sqlite` with `git lfs pull`. |

## Re-score with the committed data — no API keys, no rebuild

The model output (`model_actions.csv`) and the human truth (`ground_truth.csv`)
both ship in the repo, so anyone can reproduce the SCORECARD numbers instantly:

```bash
python3 model-comparison/score_models.py   # prints the deviation report
```

(`SCORECARD.md` is hand-curated analysis, not the scorer's raw output — pass
`--out model-comparison/score.md` if you want to save a scratch copy; it's
gitignored.)

## Score it yourself — supply your own truth

This is the part that makes the ranking credible: you read the dockets.

```bash
git lfs pull   # fetch the full-text snapshot (one-time)
# regenerate the blind scoring page from the snapshot:
uv run python model-comparison/build_scoring_page.py
# open model-comparison/scoring/ground_truth_scoring.html in a browser, score
# every entry, hit "Download CSV" -> save over model-comparison/ground_truth.csv
python3 model-comparison/score_models.py
```

The page shows each entry's complete text and document links, never any model's
answer — scoring blind to the models is the whole point. Counts autosave to your
browser's localStorage (keyed by `entry_id`), so a refresh can't lose work.

### How to count each entry

Put a number in each of the eight boxes for **what this entry does** (not the
cumulative docket state), counting **every** hearing and deadline regardless of
whether it would surface on the calendar (major) or not (minor). The page's help
block has the full conventions; the ones that trip people up:

- **A continuance is a reschedule** (`= 1`), never a cancel + a new schedule.
- **Cancel** is only an explicit cancellation/vacatur with no replacement date.
- **One slot is one hearing** — a single proceeding that disposes of several
  motions at one date+time counts once, not once per motion.
- **A minute entry that records a proceeding** is `held` (`+1`).
- **Dark trial days are non-events** (a day the trial isn't in session is
  neither a hearing nor a deadline).
- **An amended minute entry supersedes the original** — count the event(s) once
  on the amended entry, 0 on the superseded one.

`score_models.py` scores only `reviewed` rows (tick the checkbox); `bad_ocr`
entries (unreadable source — not the model's fault) are excluded entirely.

## Run your own comparison (other cases or models)

`build_provider_stores.py` rebuilds every model at once. It copies the source
store, clears only the AI-derived tables, and replays the real extraction
pipeline against identical cached court data, so the only thing that differs
between runs is the model. Point it at your own `config.yaml` to compare on
other cases, or add a model with `--extra-variant provider:extract[,summary]`.
Requires `COURTLISTENER_TOKEN` + an API key for each provider in play, and
re-spends the LLM cost (see [SCORECARD.md](SCORECARD.md#cost) /
[docs/cost.md](../docs/cost.md)).

```bash
# rebuild every model into data/provider-stores/ (gitignored), capture the
# per-entry model counts, and write a cost report:
uv run python model-comparison/build_provider_stores.py \
    --source model-comparison/snapshots/benchmark-store.sqlite --frozen \
    --skip-summaries \
    --entry-actions-csv model-comparison/model_actions.csv \
    --out model-comparison/cost.md
python3 model-comparison/score_models.py

# narrower runs:
#   one provider's models only:   --variants openai
#   one specific model:           --variants openai/gpt-5.4-mini
#   an ad-hoc model not in the set: --extra-variant gemini:gemini-3.1-pro-preview
```

`--frozen` makes any live CourtListener request or PDF download a hard error, so
a run against the snapshot provably uses only the snapshot's data — it can't
silently drift, even on another machine. (`--skip-summaries` scores just the
extraction track, which is what `score_models.py` reads; drop it to also rebuild
summaries.) The `--entry-actions-csv` tap records every entry's counts even on an
LLM-cache hit, so a warm-cache rebuild still produces a complete CSV. The large
stores, their `build.log`s (with the per-entry DECISION trace), and the rendered
calendars stay under the gitignored `data/provider-stores/`.

> **The committed `model_actions.csv` also carries the local models**
> (`ollama/gpt-oss:20b` at thinking low and `-medium`, `ollama/gemma4:e4b`
> thinking on and `-nothink`, `ollama/qwen3.5:9b`, `ollama/granite4.1:8b`,
> `ollama/llama3.2:3b` — see
> [SCORECARD.md](SCORECARD.md#local-models--gpt-oss20b-leads-thinking-helps-extraction)).
> They are measured under the shipping policy (bounded thinking on, structured
> output on) except where the variant suffix says otherwise — the on/off pairs
> exist because thinking measurably helps extraction (gemma4:e4b 1241 thinking
> vs 1945 not).
> They are opt-in — building them needs a running Ollama server, and because 11
> benchmark entries have no usable PDF text in the snapshot they must run
> **non-frozen** (a live PDF fetch + OCR, the same text the hosted models saw),
> e.g. `--extra-variant ollama:gemma4:e4b` with `OLLAMA_BASE_URL` set and **no**
> `--frozen`. A default rebuild of the CSV (hosted models only) will overwrite
> the file and **drop the local rows** — re-add the `--extra-variant` flags
> (with a live Ollama) to keep them. Re-*scoring* the committed CSV needs none of
> this.

### Compare summary quality

`build_provider_stores.py` / `score_models.py` measure the *extraction* track. The
**summary** track is compared separately with `summarize_phase.py`, which
regenerates the per-docket case summaries on a chosen store with a chosen summary
provider/model (via `summary.refresh_stale`, copying the store first so the source
is untouched) and dumps the prose to a text file for **hand-grading** — there is no
automated summary scorer, because summary quality is provenance and readability,
not countable actions.

```bash
# every model summarizes the same top-extractor scaffold (apples-to-apples):
GEM=data/provider-stores/gemini/gemini-3.1-flash-lite/case-calendar.sqlite
uv run python model-comparison/summarize_phase.py \
    --store "$GEM" --provider anthropic --model claude-sonnet-4-6 \
    --out /tmp/sum_sonnet.txt
#   --case <id>     limit to one case (e.g. a quick feasibility check)
# A local model needs a running Ollama server; OLLAMA_NUM_CTX=131072 fits the big
# dockets on a 24 GB card. Thinking is A/B-tested with flags: --no-think (force a
# boolean-thinker's reasoning off), --think-level low|medium|high (gpt-oss level),
# --think-budget N (reasoning headroom). The script also flags a ⚠️ RUNAWAY (huge
# output) or ⚠️ HUNG model (no progress in 240s) live, with SUM_ABORT_ON_HANG=1 to
# auto-curtail — local thinking models can run away or stall on the summary task.
```

Read the dumped files side by side. Run each model on the **same** scaffold to
isolate summary quality, or on its **own** extraction store to grade the full
per-model pipeline.

## Reproducible benchmarking: the frozen snapshot

By default the build reads the **live** prod store (`store_path`), which
`case-calendar sync` keeps mutating. For a comparison that's reproducible — the
whole point when tuning models or prompts — pin it to the frozen, full-text
snapshot that ships with the repo (Git LFS):

```bash
git lfs install        # one-time, if you've never used LFS
git lfs pull           # fetch model-comparison/snapshots/benchmark-store.sqlite
```

The snapshot is **input-only** (model-output tables cleared, so it can't be
opened to peek at what a model produced) and **full-text** (every entry's body,
so the regex pre-filter's own recall is measurable). Its sibling
`benchmark-store.manifest.json` (committed, not LFS) records the date, row
counts, and the snapshot's sha256.

To refresh the baseline (e.g. for a new release), re-snapshot **deliberately**
from a fresh sync and re-score the worksheet against the new dockets at the same
time, so truth and inputs stay captured from one point in time:

```bash
uv run python model-comparison/snapshot_benchmark.py --force   # full rebuild
# or pick up a new filing on one case without re-pulling the whole benchmark:
uv run python model-comparison/snapshot_benchmark.py --case us-v-ding
# -> commits a new LFS object; re-run build_scoring_page.py + re-score
```

A fully-synced source means a frozen run reports **0 CourtListener calls**; a
`FrozenSnapshotError` means the snapshot was missing an entry's text —
re-snapshot from a freshly-synced store.

## `model_actions.csv` columns

`provider`, `case_id`, `docket_number`, `court`, `docket_id`, `entry_id`,
`entry_number`, then the eight counts (`h_scheduled`, `h_rescheduled`, `h_held`,
`h_cancelled`, `d_set`, `d_rescheduled`, `d_met_filed`, `d_cancelled`). The
`provider` column is the comparison label `provider/extraction-model`;
`ground_truth.csv` carries the same identity columns plus `reviewed` / `bad_ocr`
and the same eight counts.
