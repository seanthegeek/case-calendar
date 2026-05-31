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

| Track | Runs when | Default models (Anthropic / Gemini / OpenAI) |
| --- | --- | --- |
| Extraction + verify | Always | Claude Haiku 4.5 / Gemini 3.1 Flash Lite / GPT-5.4-nano |
| Summaries | Opt-in (`case_summaries.enabled`) | Claude Sonnet 4.6 / Gemini 2.5 Pro / GPT-5.4 |

As of 0.13.0 the default is a **split**: **Gemini 3.1 Flash Lite for extraction** and **Claude Sonnet 4.6 for summaries** (see [the SCORECARD](https://github.com/seanthegeek/case-calendar/tree/main/model-comparison/SCORECARD.md) for why). Zero-config auto-detection (you set the API keys, no `LLM_*` env vars) wires up exactly this assignment — the numbers below let you decide whether a different provider fits your caseload.

Cost scales with your caseload — the number of dockets, how many entries each
has, and how long the documents are — so a single universal figure would
mislead. Instead, here are **real measured numbers** from a full from-scratch
backfill of the maintainer's own calendar (28 cases / 34 logical dockets,
measured 2026-05-31), broken out by provider and track. "Backfill" means
processing every historical docket entry plus generating every summary — the
one-time cost of onboarding a caseload:

| Provider (extraction / summary model) | Extraction | Verify | Summary | Backfill total |
| --- | --: | --: | --: | --: |
| Anthropic (Haiku 4.5 / Sonnet 4.6) | $6.72 | $0.83 | $2.30 | **$9.84** |
| Gemini (3.1 Flash Lite / 2.5 Pro) — **extraction default** | $1.82 | $0.19 | $1.11 | **$3.12** |
| OpenAI (GPT-5.4-nano / GPT-5.4) | $1.10 | $0.13 | $1.52 | **$2.76** |
| OpenAI (GPT-5.4-mini / GPT-5.4) | $3.73 | $0.45 | $1.67 | **$5.85** |

The **Extraction** and **Verify** columns are what you pay with summaries off;
the **Summary** column is the opt-in add-on. That's roughly **$0.03–0.23 per
case for extraction** (it scales with entry count, so a busy docket costs
more) and **$0.03–0.07 per docket for summaries**. After the backfill,
existing summaries are reused unless a docket gets a new primary document or
disposition, so ongoing spend is **pennies a week**; the `verify` track (one
focused call per non-terminal hearing/deadline + the new source-entry-aware
context as of 0.11.0) is what runs on every sync, and even that stayed under
$1.00 across the whole caseload on the priciest provider.

If you run the extraction track on Anthropic, its verify pass stays
uncached on Haiku 4.5 (estimated at $0.83 across the whole caseload in this
0.13.0 build). The merged `VERIFY_SYSTEM_PROMPT` (\~2000
`count_tokens`-measured tokens) sits just under Anthropic Haiku 4.5's
documented 2048-token prompt-cache floor, so it isn't cached (`cached=0`,
`cache_write=0` on every verify call). A deliberate experiment that bumped
it to 2941 tokens — well over the documented floor — *also* didn't cache,
so the empirical conclusion is that Haiku 4.5's real floor is higher than
the documented 2048 (likely 4096); the bump was reverted since it cost more
without buying a cache discount. Bringing the verify track into cache would
require either substantially more substantive content (a stretch given the
rule space is enumerated) or routing the verify track to Sonnet (which
caches at 1024 per the docs but bills at the higher Sonnet rate). See the AGENTS.md "Anthropic prompt caching" design
note for the full empirical write-up.

These are **estimates from the price table** (below), not a bill, and they
reflect one specific caseload on one date — don't take them on faith; measure
your own with the `llm-tokens` lines.

### Why extraction defaults to Gemini and summaries to Anthropic

0.13.0 splits the default: **Gemini for extraction, Anthropic for summaries.**
The two tracks are chosen on different grounds, so the reasoning is split too.

#### Extraction → Gemini

Gemini wins on every axis that matters for the extraction track. On cost,
Anthropic runs **\~3.75× more** than Gemini on the constant-load extract +
verify pair (summaries off): **$7.55 vs $2.01** across this caseload. On speed,
Gemini averages **1.7 s/call vs Anthropic's 3.2 s/call** — roughly **\~1.9×
faster per call**. And on accuracy, Gemini now posts the **best aggregate
deviation in the comparison (305 vs Anthropic 349)**, including the best
deadline axes (D met/pass deviation 125 vs Anthropic 155; D cancelled 9 vs
Anthropic 28). The full per-axis breakdown is in
[the SCORECARD](https://github.com/seanthegeek/case-calendar/tree/main/model-comparison/SCORECARD.md).

That's a change from 0.10.0 / 0.11.0, which kept Anthropic as the extraction
default precisely because Gemini, relying on its intrinsic training priors,
silently classified a long tail of substantive federal-procedure deadline
classes as `procedural-minor` — which then dropped out at the render-time
significance gate, off subscriber calendars. The classes that were getting
dropped:

- **PSR** (Presentence Investigation Report) interview, first disclosure,
  objection windows
- **Speedy Trial Act** § 3161(h) exclusion orders
- **Surrender for service of sentence** (the date a defendant must
  self-report to BOP custody)
- **Civil forfeiture** Supp. R. G claim + answer deadlines
- **Substantive sealing motion practice** (briefing on a motion to
  seal/unseal, not the routine "filed under seal" stamps)
- **Exhibit-filing deadlines** under a final pretrial order
- **Certified administrative record** / certified index of the
  administrative record (the deadline that starts the APA cross-motion
  briefing clock)

What changed is the **prompt**, not the model. 0.13.0 adds a structured
`DEADLINE_SIGNIFICANCE_RULES` block (ordered RULE 1-5) to the unified
extraction `SYSTEM_PROMPT` that enumerates those substantive classes
explicitly and biases the default toward `major`. Because the classes are now
**named in the prompt for every provider** — apples-to-apples — Gemini
classifies them as `major` instead of leaning on intrinsic priors it didn't
have. The prompt now carries the priors; Gemini's training didn't change. That
closes the deadline-bucketing gap that had kept Anthropic the default, and the
measured deviation numbers above are the result.

The honest caveat: the ruleset enumerates the substantive classes the project
currently knows about. An operator whose caseload includes substantive classes
the ruleset does **not** name should still verify Gemini's output against their
own docket set — the per-track override env vars remain available for exactly
that. But the risk is materially **reduced**, not merely shifted onto a
different provider, because the enumeration is in-prompt for both providers now
rather than left to whichever model happens to have the prior.

#### Summaries → Anthropic

The summary track stays Anthropic because Sonnet pulls more
case-distinguishing detail than Gemini: statute citations, count numbers,
sentence breakdowns, forfeiture amounts split out by money judgment vs
identified-property, custody status, cancelled-schedule notes, full briefing
schedules, cross-docket framing. The bucket-confusion problem — multiple cases
of the same kind blurring into nearly interchangeable Gemini summaries — is
documented with side-by-side examples in the SCORECARD's **Summary track**
section. Summaries are low-volume (one call per docket, rarely re-run), so the
higher Sonnet rate buys distinguishing prose cheaply: summaries added **$2.30**
on Anthropic across the whole caseload in this build.

#### Configuration

Zero-config gets you the split automatically: set both API keys, leave the
`LLM_*` env vars unset, and auto-detection wires extraction → Gemini,
summaries → Anthropic.

```bash
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=...
```

The per-track override env vars are still wired up. `LLM_PROVIDER` (global)
overrides both tracks; `LLM_EXTRACTION_PROVIDER` and `LLM_SUMMARY_PROVIDER`
override their track. To pin a single provider on both tracks — e.g. if you
only hold an Anthropic key, or you've measured a different provider against
your own caseload:

```bash
LLM_PROVIDER=anthropic               # both tracks on Anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

Or to keep the default split but pin specific providers explicitly:

```bash
LLM_EXTRACTION_PROVIDER=gemini       # extraction track
LLM_SUMMARY_PROVIDER=anthropic       # summary track
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=...
```

Want a head-to-head cost **and accuracy** comparison across providers and model tiers — including the per-docket deviation breakdown, the
wall-clock and mean-call-latency numbers, and the bucket-confusion
examples behind the summary-track recommendation? See
[`model-comparison/`](https://github.com/seanthegeek/case-calendar/tree/main/model-comparison)
in the repository.

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

Against the backfill above: it made **38 API calls total**, so its **hourly and
daily** totals (38 / 38) sit inside even the free tier (50 / 125). The catch is
the **per-minute burst** — the backfill peaked at **9 requests in one minute**
(cold-docket lookups firing back-to-back), which exceeds the free (5/min) cap
but stays inside Tier 1 (10/min). That isn't data loss: the client honors
`Retry-After` and backs off automatically (just slower). But a from-scratch
backfill wants at least **Tier 1 (10/min)** to avoid per-minute throttling.
Steady-state polling is a handful of requests per sync, far below every tier. The per-run `courtlistener-requests`
log line reports your actual peak min / hour / day so you can size your
membership.

## Measuring real token usage and estimated cost

Every LLM call — extraction and summaries alike — logs its token counts at
`INFO`, with a rough dollar **estimate** alongside. The lines are prefixed
`llm-tokens`:

```text
llm-tokens call purpose=summary provider=anthropic model=claude-sonnet-4-6 docket=12345 in=48210 out=312 cached=0 cache_write=0 cost_est=$0.1483
llm-tokens call purpose=extract provider=gemini model=gemini-3.1-flash-lite docket=12345 in=1820 out=64 cached=0 cache_write=0 cost_est=$0.0007
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
llm-tokens model=gemini-3.1-flash-lite calls=32 in=58400 out=1700 cached=0 cache_write=0 cost_est=$0.0061
llm-tokens model=claude-sonnet-4-6 calls=5 in=152480 out=310 cached=138400 cache_write=2100 cost_est=$0.0492
llm-tokens sync TOTAL calls=37 dockets=4 models=2 in=210880 out=2010 cached=138400 cache_write=2100 cost_est=$0.0553
```

The per-model lines are what let you read the cheap extractor track's spend
(here `gemini-3.1-flash-lite`) apart from the higher-tier summary track's
(`claude-sonnet-4-6`) — the two run on different models, so the by-model split
is the by-track split. The `extraction LLM:` / `summary LLM:` lines logged at
the start of the run name which model is which.

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
