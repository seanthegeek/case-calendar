---
title: Cost
---

Case Calendar costs money in two independent ways, and **both apply whether or
not you enable [AI case summaries](case-summaries.md)**:

1. **LLM API calls** — the extractor runs a small/fast model on every relevant
   docket entry (always, even with summaries off); case summaries add a
   higher-tier model on top (opt-in).
2. **CourtListener API quota** — every sync makes REST calls against
   CourtListener, whose free tier is rate-limited and whose higher tiers come
   from a paid Free Law Project membership.

This page covers both — with real measured numbers — and how to measure your
own spend.

[← Back to docs](index.md)

## LLM cost

Two tracks run on different model tiers. The **extractor** (and its end-of-sync
**verify** pass) handle high-volume structured classification — date, key,
significance — so they default to the cheap small/fast tier and run whether or
not summaries are enabled. **Summaries** are the opt-in synthesis track on a
higher tier.

| Track | Runs when | Default models (Gemini / OpenAI / Anthropic) |
| --- | --- | --- |
| Extraction + verify | Always | Gemini 3.1 Flash Lite / GPT-5.4-nano / Claude Haiku 4.5 |
| Summaries | Opt-in (`case_summaries.enabled`) | Gemini 2.5 Pro / GPT-5.4 / Claude Sonnet 4.6 |

Cost scales with your caseload — the number of dockets, how many entries each
has, and how long the documents are — so a single universal figure would
mislead. Instead, here are **real measured numbers** from a full from-scratch
backfill of the maintainer's own calendar (28 cases / 34 logical dockets,
measured 2026-05-29), broken out by provider and track. "Backfill" means
processing every historical docket entry plus generating every summary — the
one-time cost of onboarding a caseload:

| Provider (extraction / summary model) | Extraction | Verify | Summary | Backfill total |
| --- | --: | --: | --: | --: |
| Gemini (3.1 Flash Lite / 2.5 Pro) — **default** | $1.49 | $0.11 | $1.09 | **$2.69** |
| OpenAI (GPT-5.4-nano / GPT-5.4) | $1.00 | $0.12 | $1.62 | **$2.75** |
| OpenAI (GPT-5.4-mini / GPT-5.4) | $3.59 | $0.47 | $1.64 | **$5.70** |
| Anthropic (Haiku 4.5 / Sonnet 4.6) | $6.07 | $0.55 | $2.24 | **$8.86** |

The **Extraction** and **Verify** columns are what you pay with summaries off;
the **Summary** column is the opt-in add-on. That's roughly **$0.03–0.22 per
case for extraction** (it scales with entry count, so a busy docket costs
more) and **$0.03–0.07 per docket for summaries**. After the backfill,
existing summaries are reused unless a docket gets a new primary document or
disposition, so ongoing spend is **pennies a week**; the `verify` track (one
focused call per non-terminal hearing/deadline) is what runs on every sync,
and even that stayed under $0.55 across the whole caseload on the priciest
provider.

These are **estimates from the price table** (below), not a bill, and they
reflect one specific caseload on one date — don't take them on faith; measure
your own with the `llm-tokens` lines.

### Why the recommended split is Gemini extraction + Anthropic summaries

The two tracks are independent (separate env vars — see
[Installation](installation.md)) and they reward different model strengths:

- **Extraction** is high-volume (~1,230 calls per backfill on the
  maintainer's caseload), structured-output classification. The cheap
  small/fast tier handles it fine, and the dominant cost line is the
  extractor's per-call rate × call volume. **Gemini wins this track**
  outright — best accuracy on the
  [SCORECARD](https://github.com/seanthegeek/case-calendar/tree/main/model-comparison/SCORECARD.md),
  ~2× faster per call than Anthropic, ~4× cheaper for the same workload.
- **Summaries** are low-volume (~34 calls per backfill, near-zero ongoing),
  long-context, synthesis-heavy. The dollar delta is small — Gemini's
  summary track is $1.09, Anthropic's is $2.24 — but the **quality
  difference is large**. Anthropic captures case-distinguishing detail
  (statute citations, count numbers, sentence breakdowns, forfeiture
  amounts split out by money judgment vs identified-property, custody
  status, cancelled-schedule notes, full briefing schedules, cross-docket
  framing) that Gemini's terser version glosses over. The bucket-confusion
  problem — multiple cases of the same kind blurring into nearly
  interchangeable Gemini summaries — is documented with side-by-side
  examples in the SCORECARD's **Summary track** section.

Pinning the recommended split in `.env`:

```bash
LLM_PROVIDER=gemini                  # global default — applies to extraction + summaries
LLM_SUMMARY_PROVIDER=anthropic       # but pin Anthropic for the rare, synthesis-heavy summary calls
GEMINI_API_KEY=...
ANTHROPIC_API_KEY=...
```

That costs you the ~$1.15 summary-track delta (extraction stays on Gemini, so
the dominant ~$1.60 extract+verify line is unchanged). The total backfill
under this split is **~$3.85** — Gemini extraction + Anthropic summaries.

> Want a head-to-head cost **and accuracy** comparison across providers and
> model tiers — including the per-docket deviation breakdown, the
> wall-clock and mean-call-latency numbers, and the bucket-confusion
> examples behind the summary-track recommendation? See
> [`model-comparison/`](https://github.com/seanthegeek/case-calendar/tree/main/model-comparison)
> in the repository.

## CourtListener API limits

The LLM dollars above are only half the picture — CourtListener's REST API is
rate-limited, and higher limits come from a Free Law Project membership (which
also funds the project). The tiers, with the per-minute / hour / day request
ceilings ([free.law/membership](https://free.law/membership/), verified
2026-05-26):

| Tier | Price | Requests (min / hour / day) |
| --- | --- | --- |
| Free (non-member) | — | 5 / 50 / 125 |
| Tier 1 | $10/mo · $100/yr | 10 / 75 / 300 |
| Tier 2 | $25/mo · $250/yr | 15 / 150 / 600 |
| Tier 3 | $50/mo · $500/yr | 20 / 250 / 1,000 |
| Tier 4 | $100/mo · $1,000/yr | 25 / 300 / 1,400 |

Membership API access is intended for small firms, small government / media
outlets, academics, and pre-revenue / pre-funding organizations — not large or
funded ones.

Against the backfill above: it made **46 API calls total**, so its **hourly and
daily** totals (46 / 46) sit inside even the free tier (50 / 125). The catch is
the **per-minute burst** — the backfill peaked at **11 requests in one minute**
(cold-docket lookups firing back-to-back), which exceeds the free (5/min) and
Tier 1 (10/min) per-minute caps. That isn't data loss: the client honors
`Retry-After` and backs off automatically (just slower). But a from-scratch
backfill wants **Tier 2 (15/min)** to avoid per-minute throttling.
Steady-state polling is a handful of requests per sync, far below every tier. The per-run `courtlistener-requests`
log line reports your actual peak min / hour / day so you can size your
membership.

## Measuring real token usage and estimated cost

Every LLM call — extraction and summaries alike — logs its token counts at
`INFO`, with a rough dollar **estimate** alongside. The lines are prefixed
`llm-tokens`:

```text
llm-tokens call purpose=summary provider=gemini model=gemini-2.5-pro docket=12345 in=48210 out=312 cached=0 cache_write=0 cost_est=$0.0301
llm-tokens call purpose=extract provider=gemini model=gemini-3.1-flash-lite docket=12345 in=1820 out=64 cached=0 cache_write=0 cost_est=$0.0002
```

- `in` — total prompt tokens (the cached portion **included**).
- `out` — completion tokens.
- `cached` — prompt tokens served from the cache (billed at the cheaper
  cache-read rate).
- `cache_write` — prompt tokens written to the cache (Anthropic only; billed
  at the higher cache-write rate). Other providers report `0`.
- `cost_est` — estimated USD for the call (see the caveat below).

`purpose` distinguishes the cheap extractor calls (`extract`,
`verify_hearing`, `verify_deadline`, `dedupe_hearings`) from the higher-tier
`summary` calls, and `docket` is the CourtListener docket id.

At the end of a `sync` or `summarize` run, a per-docket subtotal, a per-model
subtotal, and a grand total are logged so you don't have to add them up
yourself:

```text
llm-tokens docket=12345 calls=9 in=63140 out=540 cached=58000 cache_write=2100 cost_est=$0.0241
llm-tokens model=claude-haiku-4-5 calls=32 in=58400 out=1700 cached=52000 cache_write=0 cost_est=$0.0120
llm-tokens model=claude-sonnet-4-6 calls=5 in=152480 out=310 cached=138400 cache_write=2100 cost_est=$0.0492
llm-tokens sync TOTAL calls=37 dockets=4 models=2 in=210880 out=2010 cached=190400 cache_write=2100 cost_est=$0.0612
```

The per-model lines are what let you read the cheap extractor track's spend
(here `gemini-3.1-flash-lite`) apart from the higher-tier summary track's
(`gemini-2.5-pro`) — the two run on different models, so the by-model split is
the by-track split. The `extraction LLM:` / `summary LLM:` lines logged at the
start of the run name which model is which.

The token counts are normalized so `in` always means the same thing across
providers (Anthropic reports cache reads/writes separately from its input
count; we fold them in), and the cost estimate prices each slice — uncached
input, cache reads, cache writes, output — at its own rate, which matters
because the system prompt is cached on almost every call.

**`cost_est` is an estimate, not a bill.** It comes from a small static price
table (`case_calendar/costs.py`) hand-copied from each provider's pricing page
and dated with `PRICES_VERIFIED` — the default Anthropic, Gemini, and OpenAI
(5.4 / 5.5 family) models are priced at their standard tier. It does not model
batch discounts, long-context (>200k) tiers, or data-residency multipliers.
When a model isn't in the table — a legacy model, or anything you set via
`LLM_MODEL` that hasn't been added — the call logs `cost_est=?` and the run
`TOTAL` notes how many calls had no price entry, so a partial estimate is
obvious rather than silently low. Add a model's rates to that file to price it.
Nothing is persisted — to track spend over time, sum the `TOTAL` lines from
your run logs.

To force a regeneration of every summary after a model upgrade or prompt change
(useful when you want to re-measure the summary track's cost):

```bash
uv run case-calendar summarize --force
# or, bundled into a polling sync to share the CourtListener session:
uv run case-calendar sync --force-summaries
```

## Next steps

- [Configuration](configuration.md) — the complete `config.yaml` reference,
  including the `case_summaries` block.
- [AI case summaries](case-summaries.md) — the opt-in summary track that adds
  the higher-tier model cost.
- [CLI reference](cli.md) — every `case-calendar` subcommand and flag.
