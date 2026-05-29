# Provider store build — cost + output comparison

> **Why the cheapest column is NOT the project default (read this before reading the cost totals).** The 0.8.1 release promoted Gemini-Flash-Lite to default partly on the basis that it backfilled the entire caseload for $2.56 vs Anthropic's $8.56 — a 3.3× cost advantage. The 0.8.2 release reverted that and switched back to Anthropic. The reason is documented in `SCORECARD.md`: Gemini systematically drops substantive event classes that subscribers depend on (preliminary-injunction hearings on civil-litigation dockets, Speedy Trial Act exclusions, PSIR deadlines, CIPA submissions, jury-process deadlines, surrender-for-service-of-sentence). The cost differential is real but small — over a 5-year horizon for a 28-case caseload, the Anthropic premium is roughly $60 (≈$6 once at backfill, ≈$0.20/week steady-state) — orders of magnitude below the value of not silently missing a docket's PI hearing. Read these cost totals as "what the comparison measured," not as "Gemini is the right default."
>
> **Note on the gemini/gemini-3.5-flash column.** The OpenAI extraction candidate
> (gpt-5.4-mini) finished cleanly. The Gemini extraction candidate
> (gemini-3.5-flash) was attempted but its store build hit the
> Google AI Studio prepayment wall partway through the summary phase
> (`429 RESOURCE_EXHAUSTED: Your prepayment credits are depleted`), then a
> follow-up rebuild was stopped before completion. Its rows were removed from
> the comparison so neither incomplete data nor a stale earlier number
> contaminates the scoring or the cost totals; the previous-pipeline number for
> this column ($11.92, measured 2026-05-25) lives in `README.md` only as
> historical context until a clean re-measurement is taken.

- columns built: anthropic/claude-haiku-4-5, openai/gpt-5.4-nano, gemini/gemini-3.1-flash-lite, openai/gpt-5.4-mini
- anthropic/claude-haiku-4-5: provider=anthropic, extraction=claude-haiku-4-5, summary=claude-sonnet-4-6, folder=`data/provider-stores/anthropic/claude-haiku-4-5/`
- openai/gpt-5.4-nano: provider=openai, extraction=gpt-5.4-nano, summary=gpt-5.4, folder=`data/provider-stores/openai/gpt-5.4-nano/`
- gemini/gemini-3.1-flash-lite: provider=gemini, extraction=gemini-3.1-flash-lite, summary=gemini-2.5-pro, folder=`data/provider-stores/gemini/gemini-3.1-flash-lite/`
- openai/gpt-5.4-mini: provider=openai, extraction=gpt-5.4-mini, summary=gpt-5.4, folder=`data/provider-stores/openai/gpt-5.4-mini/`

## CourtListener API usage (total, shared across all columns)

- total API calls to build **all** stores: **46**
- peak rate: **11/min**, **46/hour**, **46/day**
- these are the one-time cost of warming the shared cache (cold dockets the summary pipeline falls back on); subsequent column builds add zero. PDF file downloads from storage are separate and also cached once.

## LLM cost by column and track

| column | track | calls | input tok | output tok | est USD |
| --- | --- | ---: | ---: | ---: | ---: |
| anthropic/claude-haiku-4-5 | extraction | 1092 | 12,379,298 | 334,651 | $5.7448 |
| anthropic/claude-haiku-4-5 | verify | 114 | 524,879 | 24,658 | $0.6482 |
| anthropic/claude-haiku-4-5 | summary | 35 | 932,806 | 7,604 | $2.1701 |
| openai/gpt-5.4-nano | extraction | 1092 | 10,847,611 | 277,355 | $1.0172 |
| openai/gpt-5.4-nano | verify | 179 | 758,584 | 17,852 | $0.1740 |
| openai/gpt-5.4-nano | summary | 34 | 814,826 | 5,142 | $1.4665 |
| gemini/gemini-3.1-flash-lite | extraction | 1092 | 12,020,771 | 217,398 | $1.3543 |
| gemini/gemini-3.1-flash-lite | verify | 98 | 409,954 | 6,599 | $0.1124 |
| gemini/gemini-3.1-flash-lite | summary | 34 | 883,838 | 4,343 | $1.0884 |
| openai/gpt-5.4-mini | extraction | 1092 | 11,043,735 | 216,863 | $3.5179 |
| openai/gpt-5.4-mini | verify | 152 | 597,090 | 13,219 | $0.5073 |
| openai/gpt-5.4-mini | summary | 34 | 816,834 | 5,300 | $1.6239 |

| column | total build cost |
| --- | ---: |
| anthropic/claude-haiku-4-5 | $8.5632 |
| openai/gpt-5.4-nano | $2.6577 |
| gemini/gemini-3.1-flash-lite | $2.5551 |
| openai/gpt-5.4-mini | $5.6491 |

## Build time per column

Wall-clock per column, and mean latency per LLM call. NOTE: columns build in PARALLEL (unless `--no-parallel`), so wall-clock includes contention with the other columns and the shared PDF / CourtListener cache locks — read it as a relative signal, not isolated model speed. Mean s/call (which times just the model dispatch) is the cleaner latency proxy.

| column | wall-clock | LLM calls | mean s/call |
| --- | ---: | ---: | ---: |
| anthropic/claude-haiku-4-5 | 77.7 m | 1241 | 3.7 |
| openai/gpt-5.4-nano | 58.6 m | 1305 | 2.7 |
| gemini/gemini-3.1-flash-lite | 40.5 m | 1224 | 1.9 |
| openai/gpt-5.4-mini | 45.1 m | 1278 | 2.1 |

## Output row counts per store

| store | hearings | hearings_scheduled | hearings_held | deadlines | case_summaries |
| --- | ---: | ---: | ---: | ---: | ---: |
| anthropic/claude-haiku-4-5 | 247 | 25 | 157 | 299 | 34 |
| openai/gpt-5.4-nano | 221 | 96 | 96 | 314 | 34 |
| gemini/gemini-3.1-flash-lite | 253 | 22 | 156 | 312 | 34 |
| openai/gpt-5.4-mini | 224 | 48 | 114 | 416 | 34 |

## Compare

Open each column's rendered index to compare summaries + calendars:

- anthropic/claude-haiku-4-5: `data/provider-stores/anthropic/claude-haiku-4-5/out/index.html`
- openai/gpt-5.4-nano: `data/provider-stores/openai/gpt-5.4-nano/out/index.html`
- gemini/gemini-3.1-flash-lite: `data/provider-stores/gemini/gemini-3.1-flash-lite/out/index.html`
- openai/gpt-5.4-mini: `data/provider-stores/openai/gpt-5.4-mini/out/index.html`

Each column's full sync log — including the per-entry extractor DECISION trace — is at `<column>/build.log`:

- anthropic/claude-haiku-4-5: `data/provider-stores/anthropic/claude-haiku-4-5/build.log`
- openai/gpt-5.4-nano: `data/provider-stores/openai/gpt-5.4-nano/build.log`
- gemini/gemini-3.1-flash-lite: `data/provider-stores/gemini/gemini-3.1-flash-lite/build.log`
- openai/gpt-5.4-mini: `data/provider-stores/openai/gpt-5.4-mini/build.log`
