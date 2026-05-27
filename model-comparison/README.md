# Model comparison

Why Anthropic is the default LLM provider — and the data and tools to check that
yourself, including scoring it against your own reading of the dockets, or
re-running the whole thing on other cases and models.

Case Calendar can run on any of three providers (Anthropic / OpenAI / Google
Gemini; one line in `config.yaml`). To pick a default we rebuilt every tracked
case's calendar from the **same** court data with several model configurations
and compared cost and accuracy. Each configuration is one **column**: by default
one per provider at its out-of-the-box models, plus extra columns that vary the
*extraction* model within a provider to test whether a pricier tier earns its
keep (currently `gemini-3.5-flash` against the Gemini default `gemini-3.1-flash-lite`,
and `gpt-5.4-mini` against the OpenAI default `gpt-5.4-nano`). This folder holds
the tooling, the model-output data, and a scoring method designed so you don't
have to take our word for any of it.

## Why this is set up the way it is

Ranking models on accuracy invites two distinct biases. This setup removes one
of them; the other is an inherent limitation that no method here can fix, and
it's only honest to say so plainly.

1. **Evaluation bias** — an AI asked to judge AI output tends to favor its own
   family. This *is* removed: accuracy is decided by a **human reading the public
   dockets** (blind — scoring the docket itself, never any model's output), and a
   **dumb deterministic script** measures how far each model's counts deviate from
   the human's. No model and no opinion is in the scoring loop.
2. **Prompt-fit bias** — Case Calendar's extraction and summary prompts were
   written by Anthropic's Claude, and the comparison runs those *same* prompts for
   every model. A model may simply respond better to prompts written by its own
   family. **Nothing here neutralizes this**: blind scoring can't, because the
   prompts don't change between columns. It's a home-field advantage baked into
   the comparison itself.

So scope the result accordingly. This measures **which model is most accurate at
running Case Calendar's actual, Claude-authored prompts** — which is the question
that matters for *this project*, because those are the prompts you would deploy.
It is **not** a neutral claim that Claude is the most capable model; a fair
model-capability benchmark would tune the prompts separately for each model, which
this project does not do (it ships one prompt set). The conclusion is a **default**
recommendation, not "the best model."

Keeping the scoring blind is why the committed model output is a **raw events
CSV** (`model_events.csv`), one row per extracted event, rather than the SQLite
stores or their rendered calendars: a flat list of hundreds of rows isn't
something you can eyeball into "model X says N hearings on this docket" while
filling the worksheet — you'd have to run the scorer, which you do *after*
scoring. The full stores and rendered calendars (which would be easy to peek at)
stay as gitignored local intermediates under `data/provider-stores/`.

## What's here

| Path | What it is |
| --- | --- |
| `model_events.csv` | **Source data**: one row per hearing/deadline each column (plus the live `prod` baseline) produced — logical docket, status, significance, date. The `provider` column is the comparison label `provider/extraction-model` (e.g. `gemini/gemini-3.5-flash`). Raw and unaggregated on purpose (see above). |
| `ground_truth.template.csv` | The blind worksheet — one row per logical docket with the CourtListener link(s) to read and empty count columns. Copy it and fill it in. |
| `score.py` | Scores a filled worksheet against `model_events.csv`. Pure stdlib. |
| `cost.md` | The build's cost report: cost per column and track, CourtListener usage, output row counts. |
| `ground_truth_worksheet.py` | (Re)generates the blank worksheet from `config.yaml` + the store. |
| `export_model_events.py` | Dumps the built stores to `model_events.csv`. |
| `build_provider_stores.py` | Rebuilds every comparison column at once from the same court data — point it at other cases or models (`--extra-variant`, `--variants`) to run your own comparison. Needs API keys; costs money. |

## Score it yourself — no API keys, no rebuild

This is the part that makes the ranking credible: you supply the truth.

```bash
cp model-comparison/ground_truth.template.csv model-comparison/ground_truth.csv
# open each docket (the courtlistener_urls column), read it, and fill the six
# count columns; then:
python3 model-comparison/score.py --out model-comparison/SCORECARD.md
```

`score.py` reads your filled worksheet plus `model_events.csv`, counts the same
six numbers per model, and reports each model's total deviation from your
numbers, with a per-docket breakdown so every number is auditable.

**Fill the worksheet from the dockets, not from `model_events.csv`** — scoring
blind to the models' answers is the whole point.

**How to fill each row** (per logical docket, reading the linked docket(s)):

- Count **every** hearing and **every** deadline on the docket — all of them, not
  just the calendar-worthy ones — by current status:
  - hearings → `scheduled` (date still ahead), `held` (occurred), `cancelled`
    (vacated or struck)
  - deadlines → `pending` (current due date still ahead), `met_or_passed` (due date
    has passed **or** the filing was made — one bucket, don't try to tell the two
    apart), `cancelled` (no longer in force — e.g. the briefing schedule was
    superseded, or the motion it set a response for was withdrawn)
- **One event, counted once, in its latest state.** The store updates an event in
  place when it moves, so a hearing that was reset 1/10 → 2/14, or a deadline that
  was extended, is still **one** row at its current date and status — not one per
  reset. Count it the same way: once, by where it stands now.
- **Distinct events stay distinct, though.** A briefing schedule that sets an
  opening brief, a response, and a reply is **three** separate deadlines, not one —
  count each. The test is whether they're genuinely different events, not whether
  they were set by the same order.
- The 9 rows with more than one CourtListener link are a single PACER docket split
  across CourtListener records — **read them together and count each real event
  once** (a model that double-counts across records will deviate upward, which is
  the point).
- Leave a row blank to skip it; `score.py` only scores filled rows, so you can
  work in passes and re-run.

## Cost (one-time backfill of every case)

The measured one-time backfill cost for the three **default** model sets lives in
the main documentation, so there's a single place to keep it current — see the
[Cost](../docs/cost.md#llm-cost) page. In short: ≈$7 for Anthropic, ≈$2.50 each
for OpenAI and Gemini, for the whole caseload, once. Day-to-day cost is a tiny
fraction — normal operation only processes new entries, not the whole history.

This comparison adds two **candidate** columns on top of the defaults. Each varies
only the *extraction* model (keeping its provider's default summary model) to test
whether a pricier extraction tier earns its keep:

| candidate column | extraction model | vs. its default | one-time backfill |
| --- | --- | --- | ---: |
| OpenAI | gpt-5.4-mini | gpt-5.4-nano | $4.87 |
| Google Gemini | gemini-3.5-flash | gemini-3.1-flash-lite | $11.92 |

The bump is steep on the extraction track alone: `gemini-3.5-flash` extraction
runs ≈8× the `gemini-3.1-flash-lite` extraction cost, and `gpt-5.4-mini` ≈3.5× the
`gpt-5.4-nano` cost — so the accuracy gain (the blind scoring above) has to justify
it before either becomes a default. Full per-track breakdown for all five columns
is in `cost.md`.

The build also made 43 CourtListener API calls total, once, shared across all
five columns (the court data is identical per column, so it's fetched once and
cached). Figures are estimates from published per-token prices applied to the
recorded token counts, not a bill.

## Run your own comparison (other cases or models)

`build_provider_stores.py` is the script that generates every comparison column
at once. It copies the live store, clears only the AI-derived tables, and replays
the real extraction pipeline against identical cached court data, so the only
thing that differs between columns is the model. CourtListener is fetched once
*total* and cached across every column — including under `--no-parallel` (the
cache is process-wide, not tied to the parallel path), and a cache hit never
reaches the request-stat recorder, so the CourtListener total and peak rate in
`cost.md` count genuine network calls only, in either mode. Each column is stored under
`data/provider-stores/<provider>/<extraction-model>/`, so sibling models on one
provider sit side by side. Point it at your own `config.yaml` to compare on other
cases, or add a column with `--extra-variant provider:extract[:summary]`. Requires
`COURTLISTENER_TOKEN` + an API key for each provider in play, and re-spends the
cost above.

```bash
# rebuild every column into data/provider-stores/ (gitignored) + write cost.md
uv run python model-comparison/build_provider_stores.py --validate \
    --out model-comparison/cost.md
# dump the model outputs to the committed source-data CSV
python3 model-comparison/export_model_events.py
# (re)generate the blind worksheet for the cases in your config (--force to
# overwrite an existing blank template)
uv run python model-comparison/ground_truth_worksheet.py --force

# narrower runs:
#   one provider's columns only:   --variants gemini
#   one specific column:           --variants gemini/gemini-3.5-flash
#   an ad-hoc model not in the set: --extra-variant gemini:gemini-3.1-pro-preview
```

The large stores, their `build.log`s (with the per-entry extraction DECISION
trace), and the rendered calendars live under the gitignored
`data/provider-stores/`; the committed artifact is `model_events.csv`.

## `model_events.csv` columns

`provider`, `type` (hearing/deadline), `case_id`, `docket_number`, `court`,
`docket_id`, `title`, `status`, `significance`, `date`, `source_entry_ids`. The
logical docket is `(docket_number, court)`; `docket_id` is the individual
CourtListener record (several can share one logical docket).
