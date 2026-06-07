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
| anthropic/claude-haiku-4-5 | 799 | 602 | 197 | 99 | 75 | 74 | 23 | 236 | 99 | 147 | 46 |
| openai/gpt-5.4-nano | 867 | 665 | 202 | 130 | 89 | 77 | 8 | 299 | 101 | 150 | 13 |
| openai/gpt-5.4-mini | 925 | 735 | 190 | 138 | 93 | 62 | 7 | 315 | 129 | 165 | 16 |

`Hs/Hr/Hh/Hc` = hearings scheduled/rescheduled/held/cancelled; `Ds/Dr/Df/Dc` =
deadlines set/rescheduled/met-filed/cancelled. Most of the deviation is
`over` — every provider over-extracts relative to a human counting the *final*
state, with the OpenAI columns the noisiest (the `Hs`/`Ds` over-counts: they
allocate more distinct scheduled hearings + set-deadlines than the human folds
into one). Anthropic's `Dc` 46 is spurious deadline cancellations the human
didn't count.

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

Gemini leads on both metrics, by a wider margin on the attribution-robust
aggregate (399 vs 519) than on per-entry (653 vs 799) — i.e. some of Anthropic's
per-entry penalty is attribution drift, but Gemini still leads after that's
factored out.

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

Separately, 0.16.0 makes the extractor **read every order's PDF** (an order's
operative dates often live only in a schedule table the one-line description
doesn't echo) and **stop fetching transcript bodies** (a transcript is testimony
with no forward-looking scheduling; its held-date / redaction / release
deadlines are already in the description).

## Why the recall fix widened Gemini's lead

The order-PDF + recall fixes added \~90 newly-extractable entries (\~14%). The
effect split by model quality: **Gemini and Anthropic improved** (per-entry
675→653 and 825→799 vs the pre-fix build) — they turned the recovered entries
into correct extractions, lowering `under`. **The OpenAI columns regressed**
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
