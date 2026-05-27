# Provider store build — cost + output comparison

- columns built: anthropic/claude-haiku-4-5, openai/gpt-5.4-nano, gemini/gemini-3.1-flash-lite, gemini/gemini-3.5-flash, openai/gpt-5.4-mini
- anthropic/claude-haiku-4-5: provider=anthropic, extraction=claude-haiku-4-5, summary=claude-sonnet-4-6, folder=`data/provider-stores/anthropic/claude-haiku-4-5/`
- openai/gpt-5.4-nano: provider=openai, extraction=gpt-5.4-nano, summary=gpt-5.4, folder=`data/provider-stores/openai/gpt-5.4-nano/`
- gemini/gemini-3.1-flash-lite: provider=gemini, extraction=gemini-3.1-flash-lite, summary=gemini-2.5-pro, folder=`data/provider-stores/gemini/gemini-3.1-flash-lite/`
- gemini/gemini-3.5-flash: provider=gemini, extraction=gemini-3.5-flash, summary=gemini-2.5-pro, folder=`data/provider-stores/gemini/gemini-3.5-flash/`
- openai/gpt-5.4-mini: provider=openai, extraction=gpt-5.4-mini, summary=gpt-5.4, folder=`data/provider-stores/openai/gpt-5.4-mini/`

## CourtListener API usage (total, shared across all columns)

- total API calls to build **all** stores: **43**
- peak rate: **10/min**, **43/hour**, **43/day**
- these are the one-time cost of warming the shared cache (cold dockets the summary pipeline falls back on); subsequent column builds add zero. PDF file downloads from storage are separate and also cached once.

## LLM cost by column and track

| column | track | calls | input tok | output tok | est USD |
| --- | --- | ---: | ---: | ---: | ---: |
| anthropic/claude-haiku-4-5 | extraction | 1084 | 8,612,530 | 262,210 | $4.3316 |
| anthropic/claude-haiku-4-5 | verify | 86 | 391,073 | 18,881 | $0.4855 |
| anthropic/claude-haiku-4-5 | summary | 36 | 946,898 | 8,012 | $2.1965 |
| openai/gpt-5.4-nano | extraction | 1084 | 7,537,863 | 219,572 | $0.7944 |
| openai/gpt-5.4-nano | verify | 159 | 750,879 | 16,826 | $0.1712 |
| openai/gpt-5.4-nano | summary | 34 | 803,731 | 5,332 | $1.5240 |
| gemini/gemini-3.1-flash-lite | extraction | 1084 | 8,321,307 | 208,359 | $1.2924 |
| gemini/gemini-3.1-flash-lite | verify | 92 | 400,361 | 5,857 | $0.1089 |
| gemini/gemini-3.1-flash-lite | summary | 34 | 870,579 | 4,347 | $1.0995 |
| gemini/gemini-3.5-flash | extraction | 1084 | 8,227,079 | 202,610 | $10.2124 |
| gemini/gemini-3.5-flash | verify | 97 | 409,780 | 1,996 | $0.6326 |
| gemini/gemini-3.5-flash | summary | 35 | 892,588 | 4,522 | $1.0781 |
| openai/gpt-5.4-mini | extraction | 1083 | 7,615,229 | 174,766 | $2.7422 |
| openai/gpt-5.4-mini | verify | 142 | 604,626 | 12,699 | $0.5098 |
| openai/gpt-5.4-mini | summary | 34 | 803,964 | 5,197 | $1.6213 |

| column | total build cost |
| --- | ---: |
| anthropic/claude-haiku-4-5 | $7.0136 |
| openai/gpt-5.4-nano | $2.4896 |
| gemini/gemini-3.1-flash-lite | $2.5007 |
| gemini/gemini-3.5-flash | $11.9231 |
| openai/gpt-5.4-mini | $4.8733 |

## Output row counts per store

| store | hearings | hearings_scheduled | hearings_held | deadlines | case_summaries |
| --- | ---: | ---: | ---: | ---: | ---: |
| **prod (current)** | 202 | 26 | 138 | 73 | 34 |
| anthropic/claude-haiku-4-5 | 253 | 30 | 156 | 63 | 34 |
| openai/gpt-5.4-nano | 223 | 102 | 99 | 70 | 34 |
| gemini/gemini-3.1-flash-lite | 254 | 22 | 150 | 63 | 34 |
| gemini/gemini-3.5-flash | 254 | 65 | 140 | 57 | 34 |
| openai/gpt-5.4-mini | 239 | 51 | 123 | 85 | 34 |

> Fidelity check: the **anthropic/claude-haiku-4-5** row should closely match **prod (current)** — prod was built by that column, so a faithful replay reproduces it. Large divergence means the replay isn't trustworthy yet.

## Compare

Open each column's rendered index to compare summaries + calendars:
- anthropic/claude-haiku-4-5: `data/provider-stores/anthropic/claude-haiku-4-5/out/index.html`
- openai/gpt-5.4-nano: `data/provider-stores/openai/gpt-5.4-nano/out/index.html`
- gemini/gemini-3.1-flash-lite: `data/provider-stores/gemini/gemini-3.1-flash-lite/out/index.html`
- gemini/gemini-3.5-flash: `data/provider-stores/gemini/gemini-3.5-flash/out/index.html`
- openai/gpt-5.4-mini: `data/provider-stores/openai/gpt-5.4-mini/out/index.html`

Each column's full sync log — including the per-entry extractor DECISION trace — is at `<column>/build.log`:
- anthropic/claude-haiku-4-5: `data/provider-stores/anthropic/claude-haiku-4-5/build.log`
- openai/gpt-5.4-nano: `data/provider-stores/openai/gpt-5.4-nano/build.log`
- gemini/gemini-3.1-flash-lite: `data/provider-stores/gemini/gemini-3.1-flash-lite/build.log`
- gemini/gemini-3.5-flash: `data/provider-stores/gemini/gemini-3.5-flash/build.log`
- openai/gpt-5.4-mini: `data/provider-stores/openai/gpt-5.4-mini/build.log`

