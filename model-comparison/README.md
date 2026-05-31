# Model comparison

Why the default is now a **split**: Gemini (`gemini-3.1-flash-lite`) for
extraction, Anthropic (`claude-sonnet-4-6`) for summaries — and the data and
tools to check that yourself, including scoring it against your own reading of
the dockets, or re-running the whole thing on other cases and models.

> **A note on what the score does and does not measure.** The deviation-from-
> human-truth score this comparison reports is necessary but not sufficient.
> The default has flipped repeatedly along this fault line and the lesson
> has stayed the same: aggregate deviation alone doesn't decide which provider
> a public docket-watching calendar should ship with.
>
> The 0.8.1 release switched the default to Gemini on score; the 0.8.2 release
> reverted to Anthropic because Gemini was dropping substantive event classes
> (preliminary-injunction hearings on civil-litigation dockets, Speedy Trial
> Act exclusions, PSIR deadlines, CIPA submissions, jury-process deadlines,
> surrender-for-service-of-sentence) that the score didn't penalize hard
> enough. The 0.9.0 release tried again with matching prompt edits and the
> dedupe-sweep delete-rather-than-flip change closing the in-fixture gap —
> Gemini retook the deviation lead AND looked clean on every substantive
> class in the SCORECARD.
>
> 0.10.0 reverts to Anthropic again because the same failure pattern resurfaced
> on out-of-fixture classes: PSR interview / first disclosure / objection
> windows, Speedy Trial Act § 3161(h) exclusions, surrender-for-service-of-
> sentence, civil-forfeiture Supp. R. G claim/answer, substantive sealing
> motion practice, exhibit-filing deadlines under a final pretrial order, and
> certified-administrative-record APA-cycle deadlines all classified as
> `procedural-minor` by Gemini and silently dropped at the render-time
> significance gate. Each is fixable with a prompt-vocabulary addition naming
> the class — and the list of named federal procedural classes is decades deep
> and unbounded. The maintainer isn't a lawyer; a public calendar can't have
> its silent drops audited case-by-case. Anthropic's training corpus loads
> these priors for free, which is what makes it the safe default. See
> `SCORECARD.md` for the per-event analysis, the head-to-head numbers, and the
> per-track override env vars (`LLM_EXTRACTION_PROVIDER` /
> `LLM_SUMMARY_PROVIDER`) for operators who have measured their own caseload
> and want to pin Gemini for cost.
>
> 0.13.0 makes Gemini the extraction default — and this time the fault line is
> addressed in the prompt rather than left to a model's intrinsic priors. The
> deadline classes Gemini kept dropping (PSR windows, Speedy Trial Act
> exclusions, surrender for service of sentence, civil-forfeiture claim/answer,
> substantive sealing motion practice, exhibit-filing deadlines, certified
> administrative record) are now NAMED in a structured `DEADLINE_SIGNIFICANCE_RULES`
> block in the unified extraction prompt, which also biases the default toward
> `major`. Because those classes are enumerated in the prompt for **every**
> provider — not learned from any one model's training corpus — Gemini now
> classifies them as major (its training didn't change; the prompt now carries
> the priors). With the deadline-bucketing gap closed, the head-to-head numbers
> follow: Gemini posts the best `D met/pass` in the table and far fewer spurious
> cancellations than Anthropic (`D canc` 9 vs 28), and its aggregate deviation
> (305) is the best overall, while it stays the cheapest and fastest.
> The honest caveat is unchanged in kind, only reduced in degree: the ruleset
> enumerates the classes the project currently knows about, so an operator whose
> caseload includes substantive classes the ruleset does not name should still
> verify against their own docket set, and the per-track override env vars remain
> available. The summary track stays Anthropic — Sonnet pulls more
> case-distinguishing detail (statute citations, count numbers, sentence
> breakdowns, custody status, cancelled-schedule notes) — so the default is now a
> split: Gemini extraction, Anthropic summaries.

Case Calendar can run on any of three providers (Gemini / OpenAI / Anthropic;
one line in `config.yaml`). To pick a default we rebuilt every tracked
case's calendar from the **same** court data with several model configurations
and compared cost and accuracy. Each configuration is one **column**: by default
one per provider at its out-of-the-box models, plus an extra column that varies
the *extraction* model within a provider to test whether a pricier tier earns its
keep (`gpt-5.4-mini` against the OpenAI default `gpt-5.4-nano`). This folder holds
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
| `model_events.csv` | **Source data**: one row per hearing/deadline each column (plus the live `prod` baseline) produced — CourtListener record (`docket_id`), logical docket, status, significance, date. The `provider` column is the comparison label `provider/extraction-model` (e.g. `gemini/gemini-3.1-flash-lite`). Raw and unaggregated on purpose (see above). |
| `ground_truth.template.csv` | The blind worksheet — one row per CourtListener record with the docket link to read and empty count columns. Copy it and fill it in. |
| `score.py` | Scores a filled worksheet against `model_events.csv`. Pure stdlib. |
| `cost.md` (optional, gitignored) | The build's cost report: cost per column and track, CourtListener usage, output row counts. Written when you pass `--out model-comparison/cost.md` to `build_provider_stores.py`; the committed cost numbers live in [SCORECARD.md](SCORECARD.md#wall-clock--cost-per-model) instead so they stay in lockstep with the per-docket deviation table. |
| `ground_truth_worksheet.py` | (Re)generates the blank worksheet from `config.yaml` + the store. |
| `export_model_events.py` | Dumps the built stores to `model_events.csv`. |
| `build_provider_stores.py` | Rebuilds every comparison column at once from the same court data — point it at other cases or models (`--extra-variant`, `--variants`) to run your own comparison. Needs API keys; costs money. |

## Score it yourself — no API keys, no rebuild

This is the part that makes the ranking credible: you supply the truth.

```bash
cp model-comparison/ground_truth.template.csv model-comparison/ground_truth.csv
# open each record (the courtlistener_url column), read it, and fill the six
# count columns; then:
python3 model-comparison/score.py --out model-comparison/SCORECARD.md
```

`score.py` reads your filled worksheet plus `model_events.csv`, counts the same
six numbers per model, and reports each model's total deviation from your
numbers, with a per-docket breakdown so every number is auditable.

**Fill the worksheet from the dockets, not from `model_events.csv`** — scoring
blind to the models' answers is the whole point.

**How to fill each row.** Open the linked CourtListener page and put a number in
each of the six count columns — how many events on that page are in each state:

| column | the number of … |
| --- | --- |
| `hearings_scheduled` | hearings whose date is still ahead |
| `hearings_held` | hearings that have occurred |
| `hearings_cancelled` | hearings that were vacated or struck |
| `deadlines_pending` | deadlines whose due date is still ahead |
| `deadlines_met_or_passed` | deadlines whose date has passed **or** that were filed (one bucket — don't split these two) |
| `deadlines_cancelled` | deadlines no longer in force (e.g. a superseded briefing schedule) |

Count **every** hearing and deadline, not just the calendar-worthy ones. Count
each one **once, in its current state** — a hearing reset from 1/10 to 2/14 is a
single `scheduled` hearing, not two. But genuinely distinct events stay distinct:
a briefing schedule that sets an opening brief, a response, and a reply is
**three** deadlines. Leave a row blank to skip it; `score.py` scores only filled
rows.

## Cost (one-time backfill of every case)

The measured one-time backfill cost for the three single-provider model sets
lives in the main documentation, so there's a single place to keep it current —
see the [Cost](../docs/cost.md#llm-cost) page. In short, building one full column
end-to-end (that provider for both extraction and summaries) costs ≈$3.12 for
Gemini, ≈$2.76 for OpenAI (nano), ≈$9.84 for Anthropic, for the whole caseload,
once. The shipped default routes extraction to Gemini and summaries to Anthropic,
so its real cost sits between those: the cheap Gemini extraction track plus the
Anthropic summary track. Day-to-day cost is a tiny fraction — normal operation
only processes new entries, not the whole history.

This comparison adds one **candidate** column on top of the defaults — the
OpenAI extraction-model evaluation candidate, varying only the *extraction*
model (keeping its provider's default summary model) to test whether the
pricier OpenAI extraction tier earns its keep:

| candidate column | extraction model | vs. its default | one-time backfill |
| --- | --- | --- | ---: |
| OpenAI | gpt-5.4-mini | gpt-5.4-nano | $5.85 |

The bump is steep on the extraction track alone: `gpt-5.4-mini` ≈3.4× the
`gpt-5.4-nano` cost. On the SCORECARD's blind scoring below, mini (343
deviation) does edge out nano (380), but neither OpenAI column leads the
table — the Gemini default does. The mini stays as an evaluation candidate
rather than the OpenAI default. Full per-track breakdown is in
[SCORECARD.md](SCORECARD.md#wall-clock--cost-per-model).

The build also made 38 CourtListener API calls total, once, shared across all
columns (the court data is identical per column, so it's fetched once and
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
#   one provider's columns only:   --variants openai
#   one specific column:           --variants openai/gpt-5.4-mini
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
