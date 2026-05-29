# Provider accuracy vs human ground truth

Scored **46** of 46 CourtListener records (those with all six counts filled in). Lower deviation = closer to the human-read truth. Deviation is the sum of |model count − your count| over the six status categories.

This release (0.10.0) reverts the default LLM provider to **Anthropic for BOTH the extraction track AND the summary track**, despite Gemini's better deviation score (328 vs Anthropic's 381) on the comparison fixture. The score is real and so are Gemini's cost/latency wins (~4× cheaper, ~2× faster) — what the score doesn't capture is the failure mode that drove this reversion: Gemini systematically classifies substantive federal-procedure deadline classes as **procedural-minor**, which silently drops them from subscriber calendars at the render-time significance gate. Real-world cases discovered while running Gemini in production after 0.9.0:

- PSR interview, first PSR disclosure, PSR objection windows — all dropped as minor
- Speedy Trial Act 18 U.S.C. § 3161(h) exclusion orders — dropped as minor
- Surrender for service of sentence (the date a defendant must self-report to BOP custody) — dropped as minor
- Civil forfeiture Supp. R. G claim + answer deadlines — dropped as minor
- Substantive sealing motion practice (briefing on a motion to seal/unseal, not the routine "filed under seal" stamps) — dropped as minor
- Exhibit-filing deadlines under a final pretrial order — dropped as minor
- Certified administrative record / certified index of the administrative record (the deadline that starts the APA cross-motion briefing clock) — dropped as minor

Each miss is addressable with a targeted prompt-vocabulary addition that names the class explicitly. The problem is the *list of classes is decades deep and unbounded*: the maintainer is not a lawyer, and a calendar people rely on cannot have its silent-drops audited case-by-case after the fact. Gemini's training corpus does not include enough legal-procedure text to load these priors implicitly, so without explicit vocabulary additions it defaults to minor on whatever vocabulary it doesn't recognize. Anthropic's training corpus covers them for free — the model classifies "Order Excluding Time Under the Speedy Trial Act" as substantive without ever being told what the Speedy Trial Act is.

The score-vs-coverage gap is the recurring lesson here: aggregate deviation rewards a provider that gets the common cases right at scale, but a docket-watching calendar's value depends on NOT missing the rare substantive deadline. Anthropic is therefore the new default again, the per-track override env vars (`LLM_EXTRACTION_PROVIDER` / `LLM_SUMMARY_PROVIDER`) remain available for operators who have verified their caseload's class profile and want to pin Gemini for cost, and the rest of this SCORECARD documents what the measurements DO show so an operator can make that call from real numbers.

## Totals (lower is better)

| model | total deviation | H sched | H held | H canc | D pend | D met/pass | D canc |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| gemini/gemini-3.1-flash-lite | **328** | 25 | 76 | 31 | 17 | 167 | 12 |
| anthropic/claude-haiku-4-5 | **381** | 27 | 90 | 20 | 24 | 182 | 38 |
| prod (live, 0.8.2 anthropic) | **413** | 28 | 84 | 63 | 21 | 203 | 14 |
| openai/gpt-5.4-nano | **425** | 72 | 115 | 14 | 60 | 164 | 0 |
| openai/gpt-5.4-mini | **435** | 44 | 94 | 19 | 26 | 236 | 16 |

The dedupe-deletion change alone closed roughly 40 points on each of Anthropic and Gemini vs the prior intermediate measurement — H_canc on Anthropic dropped from `69` to `20`, on Gemini from `76` to `31` (each absorbed sibling used to count as a `cancelled` deviation point). OpenAI nano's `H_canc=14` is the cleanest in the table — also because it under-extracts on the held and scheduled axes overall (note `H_sched=72`, the highest in the table — nano's pattern is to leave many proceedings in `scheduled` rather than transitioning them).

## Wall-clock + cost per column

Full from-scratch backfill of the maintainer's caseload (28 cases / 34 logical PACER dockets / 46 CourtListener records), all four columns built in PARALLEL against the SAME cached CourtListener responses (every CourtListener fetch happens at most once total). The Gemini extraction-candidate `gemini-3.5-flash` is **dropped from the default set** due to long processing times (~+100 min wall-clock) without a payoff its absent comparison column would justify; pass `--extra-variant gemini:gemini-3.5-flash` to add it back for a one-off.

| column | wall-clock | LLM calls | mean s/call | extract | verify | summary | **TOTAL** |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| gemini/gemini-3.1-flash-lite | **38.9m** | 1230 | **1.90s** | $1.49 | $0.11 | $1.09 | **$2.69** |
| openai/gpt-5.4-mini | 47.1m | 1275 | 2.22s | $3.59 | $0.47 | $1.64 | $5.70 |
| openai/gpt-5.4-nano | 55.4m | 1254 | 2.65s | $1.00 | $0.12 | $1.62 | **$2.75** |
| anthropic/claude-haiku-4-5 | 74.6m | 1232 | 3.64s | $6.07 | $0.55 | $2.24 | $8.86 |

Gemini is **~2× faster than Anthropic per call**, ~30% faster than OpenAI nano, and ~15% faster than the OpenAI mini. The cost spread on the extract+verify pair (what runs constantly) is starker: **$1.60 (gemini) / $1.12 (openai-nano) / $4.06 (openai-mini) / $6.62 (anthropic)** — Anthropic costs ~4× what Gemini costs for the same workload. Over a 5-year steady-state run a 28-case caseload, Gemini would save **~$60-80 in extract+verify alone** vs Anthropic (and ~$40 vs the OpenAI mini). The summary track is rare (one call per docket, only when a primary document or disposition lands) so its cost delta is small. Despite this, the 0.10.0 default is Anthropic on both tracks: dollars-per-year on a hobby-scale caseload is a smaller concern than silently dropping a substantive deadline from a subscriber's calendar because Gemini classified it minor (see the intro). Operators running this at larger scale, who can verify their caseload's substantive-class profile and either accept the misses or maintain a prompt-vocabulary addendum, get the cost win by pinning `LLM_EXTRACTION_PROVIDER=gemini`.

## Two tracks, two providers

The codebase has two independent provider/model knobs:

- **Extraction track** — every relevant docket entry: `LLM_EXTRACTION_PROVIDER` (override) > `LLM_PROVIDER` (global) > API-key auto-detect. ~1230 calls per backfill. High-volume, low-context, structured-output classification.
- **Summary track** — one call per docket when a primary document or disposition lands (opt-in via `case_summaries.enabled`): `LLM_SUMMARY_PROVIDER` (override) > `LLM_PROVIDER` (global) > auto-detect. ~34 calls per backfill, ~near zero ongoing. Low-volume, long-context, synthesis-heavy.

The two tracks have fundamentally different cost shapes (the extractor's 1230 calls vs the summary's 34), and as the **Summary track** section below shows, they reward different model strengths. The 0.10.0 default is `LLM_PROVIDER=anthropic` for BOTH tracks (set once, applies to both), for the maintenance-treadmill reason laid out in the intro. The per-track overrides remain available for operators who want to split — see **Recommended provider split** below for the configurations that have been measured.

## Qualitative event-set diffs — extraction track

The deviation score doesn't distinguish "missed a substantive event the human counted" from "extracted a noisy procedural event the human didn't count." Both move the score by the same magnitude. The 0.8.2 revert from Gemini → Anthropic was driven by Gemini systematically dropping substantive event classes (preliminary-injunction hearings, Speedy Trial Act exclusions, PSIR deadlines, CIPA submissions, jury-process deadlines, surrender-for-service-of-sentence). All of those are now caught — by both providers — after the matched prompt edits in this release:

| SCORECARD-era ask | Was caught by | Now caught by |
| --- | --- | --- |
| McGonigal sentencing transcript class (4 events: 2023-06-25 conference release, 2023-08-25 plea redaction, 2024-01-19 sentencing redaction, 2024-03-28 sentencing release) | Gemini only | **both** |
| Knoot motion-in-limine briefing chain (response, reply, suppression hearing, expert disclosure, govt expert response) | Gemini only | **both** (Anthropic also adds the bonus expert-reply deadline) |
| Akhter multi-defendant divergence (Muneeb plea + Sohaib pro-se trial, with per-defendant arraignment / change-of-plea / sentencing keys) | Gemini only | **both** |
| Ding substantive cybercrime arcs (motion to suppress, evidentiary hearing, jury selection / trial held) | Gemini only | **both** (Anthropic catches 18+ distinct motion-hearing sub-keys, including the Daubert per-witness keys) |
| DOW preliminary-injunction hearing | Anthropic only | **both** |
| Speedy Trial Act stipulations on Ding | Anthropic only | **both** |
| PSIR / PSR deadlines | Anthropic only | **both** |
| Ashtor CIPA-pretrial-conference (per-defendant) | Anthropic only | **both** |
| McGonigal classified-info filings + surrender-for-service-of-sentence | Anthropic only | **both** |
| Akhter jury-process (questionnaire, instructions, final pretrial) | Anthropic only | **both** |

So the 0.8.2 SCORECARD's coverage gaps don't drive the 0.10.0 default decision either — both providers' extraction is substantively complete on every event class flagged in the fixture. What this table CANNOT show is the much longer tail of substantive procedural classes the fixture doesn't exercise (PSR, STA exclusions, surrender for service of sentence, civil-forfeiture claim/answer, sealing motion practice, exhibit-filing deadlines, certified administrative record — see the intro). Those failure modes are out-of-fixture and were only discovered after running Gemini in production. Anthropic's intrinsic legal-priors coverage carries through to them too; Gemini's vocabulary-dependent classification doesn't.

## Summary track — Gemini vs Anthropic

The summary track tells a different story. Anthropic's case summaries are **~55% longer** on average (median 964 chars vs Gemini's 622) — but the difference isn't padding, it's **case-distinguishing detail** that Gemini omits. The shorter Gemini summaries blur cases that fit the same bucket into nearly interchangeable prose, while Anthropic captures the thread that makes each case THIS case.

### The bucket-confusion pattern

Three case groupings in the maintainer's caseload have multiple cases of the same kind, and the difference shows up most sharply there:

**NK-IT-worker / laptop-farm scheme** (Chapman, Ashtor, Didenko, Hwa, Jin). All structurally similar: U.S.-based facilitator hosts company-issued computers, helps NK IT workers fraudulently obtain remote employment, NK government benefits via U.S. sanctions evasion.

> Gemini for Chapman: *"helping overseas IT workers, allegedly affiliated with North Korea, fraudulently obtain remote employment at hundreds of U.S. companies using stolen identities"*
> Gemini for Ashtor: *"running a scheme to defraud U.S. companies by using false identities to obtain remote IT work, with proceeds allegedly intended to benefit the North Korean government"*
> Gemini for Didenko: *"running a scheme to help overseas IT workers, including some from North Korea, fraudulently obtain remote jobs with U.S. companies using stolen identities"*

Read those back-to-back: nearly interchangeable. Anthropic's versions of the same three pin what makes each distinct: Chapman ran a **laptop farm from Arizona, ~hundreds of Fortune 500 companies**; Ashtor was a **5-defendant indictment including two NK IT workers in China and a Mexican citizen in Sweden**; Didenko ran **"UpworkSell" creating nearly 900 fraudulent U.S. identities**. Three operationally distinct shapes of the same scheme — and on a docket-watching calendar, that distinction is what's useful.

**Ransomware-operator scheme** (Berezhnoy, Gallyamov, Gholinejad). Anthropic distinguishes them by target/MO: Berezhnoy/8Base **extorted >$16M in Bitcoin from schools, hospitals, government contractors**; Gholinejad/Robbinhood specifically attacked **the cities of Greenville and Baltimore, healthcare orgs, from Jan 2019 through Mar 2024**; Gallyamov/Qakbot was a **civil-forfeiture-of-$2,061,517.68-and-crypto** posture, distinct from the others' criminal indictments. Gemini's three read as one generic "ransomware conspiracy" story.

**DOW litigation group** (`cadc 26-1049`, `ca9 26-2011`, `cand 3:26-cv-01996`). All three are parts of the SAME fight, but they're distinguishable along a critical axis: which statutory authority each one targets. Anthropic spells it out — cadc challenges the **§ 4713 supply-chain-risk notice**, cand challenges the **§ 3252 supply-chain-risk designation + the Hegseth Directive**, ca9 is the **stayed appeal of the cand case**. Gemini collapses all three to "supply-chain risk" and loses the statutory distinction that drives why they're separate proceedings.

### The categories of detail Anthropic captures and Gemini drops

Patterns observed across the full 34-summary comparison:

1. **Statutory citations**: `41 U.S.C. § 4713`, `10 U.S.C. § 3252`, `18 U.S.C. § 371`, `18 U.S.C. § 951`. Gemini drops these almost universally.
2. **Count numbers + count-specific outcomes**: Anthropic enumerates by count number (`Count 1`, `Count 7`, `Count 8`, `Count 13`), Gemini gives just the offense type.
3. **Sentence + forfeiture breakdowns**: Anthropic on Chapman: *concurrent 78-month terms on wire fraud and money laundering counts plus a consecutive 24-month mandatory term on aggravated identity theft*, plus *forfeiture of $284,666.92 in identified funds and a forfeiture money judgment of $176,850*. Gemini: *102 months in prison and ordered to pay restitution and forfeiture*.
4. **Precise factual figures**: Anthropic includes `approximately 96 federal agency databases`, `more than 1,000 confidential files`, `TPU and GPU chip designs and cluster management software`, `tens of millions of dollars in losses`, `restitution to eighteen victims`. Gemini rounds or omits.
5. **Aliases / named instruments**: Anthropic includes `Sina Ghaaf`, `Hegseth Directive`, `Supply Chain Designation`. Gemini drops them.
6. **Background descriptors**: Anthropic includes `Arcadia City Council member`, `U.S. national from Arizona`, `Mexican citizen residing in Sweden`, `former Google software engineer affiliated with two PRC-based AI startups`. Gemini attenuates or drops.
7. **Procedural posture changes — especially CANCELLED schedules**: Anthropic on Ashtor: *Emanuel Ashtor's jury trial, which had been set for June 15, 2026, was cancelled*. Gemini drops these — material to subscribers since a cancelled trial date is exactly what a calendar exists to surface.
8. **Cross-docket framing** (multi-docket cases): Anthropic on the cadc DOW: *This docket addresses the § 4713 notice specifically, while Anthropic's separate challenge to the Secretary's concurrent invocation of 41 U.S.C. § 3252 proceeds in the Northern District of California*. Gemini collapses or omits.
9. **Custody status**: Anthropic includes `in federal custody`. Gemini omits.
10. **Briefing schedules**: Anthropic gives the full schedule (admin record + plaintiff motion + defendants' opposition + reply). Gemini gives the headline event.

### Cost / latency delta for the summary track

| Provider | Summary track cost (full backfill, 34 dockets) | Per-docket cost |
| --- | ---: | ---: |
| gemini-2.5-pro | $1.09 | ~$0.03 |
| openai gpt-5.4 | $1.62 | ~$0.05 |
| openai gpt-5.4 (mini's column) | $1.64 | ~$0.05 |
| anthropic claude-sonnet-4-6 | $2.24 | ~$0.07 |

So the Sonnet-over-Gemini summary upgrade is about **$1.15 across a 28-case backfill** and roughly **$0.04 per ongoing summary** (rare — only when a primary document or disposition lands on a docket). For the docket-watching audience this calendar is built for, the case-distinguishing detail Sonnet captures is worth the dollar.

## Recommended provider split

The 0.10.0 default is **Anthropic on both tracks**. That's the simple configuration: set `LLM_PROVIDER=anthropic` once and both extraction + summaries use it. The per-track override env vars are still wired up — operators can pin different providers per track when they have measured their own caseload — but the project no longer documents a split as the recommended starting point.

### The 0.10.0 default — Anthropic on both tracks

| Track | Provider / model | Why |
| --- | --- | --- |
| **Extraction** | `claude-haiku-4-5` (via `LLM_PROVIDER=anthropic`) | Loads federal-procedure priors implicitly — substantive deadline classes (PSR, STA exclusions, surrender for service of sentence, civil-forfeiture claim/answer, sealing motion practice, exhibit-filing deadlines, certified administrative record) classify as `major` without a prompt-vocabulary enumeration. The aggregate deviation score is slightly higher than Gemini's (381 vs 328) on the fixture, but the out-of-fixture failure modes (silent drops of substantive classes) are absent. |
| **Summaries** | `claude-sonnet-4-6` (via the same `LLM_PROVIDER=anthropic`) | Captures case-distinguishing detail (statute citations, count numbers, sentence breakdowns, cancelled-schedule notes, custody status, full briefing schedules) that Gemini's terser version glosses over. See the **Summary track** section above for the full pattern catalog. |

```bash
# .env
LLM_PROVIDER=anthropic               # global default — applies to extraction + summaries
ANTHROPIC_API_KEY=sk-ant-...
```

This is the configuration the maintainer runs in production. The downside is cost: a full backfill of the maintainer's 28-case caseload is about $8.86 on Anthropic vs $2.69 on the Gemini-default; ongoing steady-state cost runs an order of magnitude higher than Gemini for the same workload. For a hobby-scale calendar this is dollars per year, not dollars per day — see [docs/cost.md](../docs/cost.md) for the per-case math an operator can apply to their own caseload.

### Splitting the tracks is still supported

The per-track override env vars from 0.9.0 are intact — splitting is just no longer the default. If an operator has confirmed Gemini handles their caseload's substantive-deadline class profile and wants the cost win on extraction while keeping Sonnet for the case-distinguishing summary prose, the configuration is:

```bash
# .env
LLM_PROVIDER=anthropic               # default for both tracks
LLM_EXTRACTION_PROVIDER=gemini       # override extraction only — keep Anthropic for summaries
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=...
```

The mirror split (Gemini extraction + Anthropic summaries, as the 0.9.0 release recommended) is equivalent and still works — the only behavioral difference between 0.9.0 and 0.10.0 is which provider `LLM_PROVIDER` defaults to. Operators who measured Gemini favorably on their own caseload and want to keep that posture should pin both tracks explicitly via the override vars rather than relying on the default.

## Per-docket detail

Truth vs each model. Format: `H scheduled/held/cancelled  D pending/met-or-passed/cancelled`.

### United States v. Wei — 3:23-cr-01471 (casd) #67661286

- truth: `H 0/23/0 D 0/74/0`
- prod (live): `H 0/16/0 D 0/15/1` — deviation 67
- anthropic/claude-haiku-4-5: `H 0/14/0 D 0/18/1` — deviation 66
- gemini/gemini-3.1-flash-lite: `H 0/18/2 D 0/15/0` — deviation 66
- openai/gpt-5.4-mini: `H 0/11/0 D 0/24/1` — deviation 63
- openai/gpt-5.4-nano: `H 2/12/1 D 4/19/0` — deviation 73

### United States v. Ding — 3:24-cr-00141 (cand) #68317014

- truth: `H 0/29/0 D 2/59/0`
- prod (live): `H 4/41/13 D 8/86/0` — deviation 62
- anthropic/claude-haiku-4-5: `H 4/38/3 D 10/78/0` — deviation 43
- gemini/gemini-3.1-flash-lite: `H 2/26/5 D 4/75/1` — deviation 29
- openai/gpt-5.4-mini: `H 12/24/3 D 6/97/1` — deviation 63
- openai/gpt-5.4-nano: `H 23/13/5 D 19/49/0` — deviation 71

### United States v. Wei — 3:23-cr-01471 (casd) #67661185

- truth: `H 0/21/0 D 0/19/0`
- prod (live): `H 4/3/3 D 0/3/0` — deviation 41
- anthropic/claude-haiku-4-5: `H 0/7/1 D 0/17/1` — deviation 18
- gemini/gemini-3.1-flash-lite: `H 1/2/3 D 0/19/0` — deviation 23
- openai/gpt-5.4-mini: `H 0/3/0 D 0/5/0` — deviation 32
- openai/gpt-5.4-nano: `H 5/4/2 D 1/15/0` — deviation 29

### United States v. Akhter — 1:25-cr-00307 (vaed) #73333500

- truth: `H 0/15/0 D 0/14/0`
- prod (live): `H 2/7/7 D 3/4/3` — deviation 33
- anthropic/claude-haiku-4-5: `H 1/4/0 D 6/1/7` — deviation 38
- gemini/gemini-3.1-flash-lite: `H 1/7/4 D 6/10/1` — deviation 24
- openai/gpt-5.4-mini: `H 0/6/2 D 7/16/2` — deviation 22
- openai/gpt-5.4-nano: `H 1/1/0 D 6/7/0` — deviation 28

### United States v. McGonigal — 1:23-cr-00016 (nysd) #66749883

- truth: `H 0/4/0 D 0/26/0`
- prod (live): `H 1/8/6 D 0/34/0` — deviation 19
- anthropic/claude-haiku-4-5: `H 4/7/2 D 0/32/0` — deviation 15
- gemini/gemini-3.1-flash-lite: `H 1/8/5 D 0/42/0` — deviation 26
- openai/gpt-5.4-mini: `H 4/5/0 D 0/59/0` — deviation 38
- openai/gpt-5.4-nano: `H 4/5/0 D 2/27/0` — deviation 8

### United States v. Knoot — 3:24-cr-00151 (tnmd) #69026861

- truth: `H 0/11/0 D 0/4/0`
- prod (live): `H 0/11/2 D 0/14/0` — deviation 12
- anthropic/claude-haiku-4-5: `H 0/12/0 D 0/8/6` — deviation 11
- gemini/gemini-3.1-flash-lite: `H 0/10/2 D 0/13/2` — deviation 14
- openai/gpt-5.4-mini: `H 4/10/1 D 1/20/6` — deviation 29
- openai/gpt-5.4-nano: `H 5/7/0 D 3/13/0` — deviation 21

### United States v. Akhter — 1:25-cr-00307 (vaed) #73320754

- truth: `H 0/7/0 D 0/7/0`
- prod (live): `H 0/3/11 D 1/3/3` — deviation 23
- anthropic/claude-haiku-4-5: `H 2/1/2 D 0/0/8` — deviation 25
- gemini/gemini-3.1-flash-lite: `H 0/3/3 D 0/9/2` — deviation 11
- openai/gpt-5.4-mini: `H 1/0/1 D 1/4/0` — deviation 13
- openai/gpt-5.4-nano: `H 2/1/0 D 1/4/0` — deviation 12

### United States v. Gallyamov — 2:25-cv-04631 (cacd) #70341311

- truth: `H 0/6/0 D 0/2/0`
- prod (live): `H 0/6/0 D 0/10/0` — deviation 8
- anthropic/claude-haiku-4-5: `H 0/7/0 D 0/13/0` — deviation 12
- gemini/gemini-3.1-flash-lite: `H 2/4/0 D 0/11/0` — deviation 13
- openai/gpt-5.4-mini: `H 2/2/0 D 0/14/0` — deviation 18
- openai/gpt-5.4-nano: `H 3/2/0 D 2/14/0` — deviation 21

### Anthropic v. DOW — 3:26-cv-01996 (cand) #72379655

- truth: `H 0/2/0 D 7/22/0`
- prod (live): `H 1/3/0 D 5/29/1` — deviation 12
- anthropic/claude-haiku-4-5: `H 1/3/0 D 7/27/0` — deviation 7
- gemini/gemini-3.1-flash-lite: `H 1/3/0 D 7/30/1` — deviation 11
- openai/gpt-5.4-mini: `H 1/4/0 D 8/35/1` — deviation 18
- openai/gpt-5.4-nano: `H 1/3/0 D 10/29/0` — deviation 12

### United States v. Ashtor — 1:25-cr-20021 (flsd) #69570297

- truth: `H 0/5/1 D 0/13/0`
- prod (live): `H 0/5/7 D 0/9/1` — deviation 11
- anthropic/claude-haiku-4-5: `H 0/5/6 D 0/4/3` — deviation 17
- gemini/gemini-3.1-flash-lite: `H 0/6/5 D 0/8/1` — deviation 11
- openai/gpt-5.4-mini: `H 1/3/5 D 1/15/1` — deviation 11
- openai/gpt-5.4-nano: `H 3/4/2 D 3/6/0` — deviation 15

### United States v. Didenko — 1:24-cr-00261 (dcd) #68810897

- truth: `H 0/6/0 D 0/5/0`
- prod (live): `H 0/1/2 D 0/2/0` — deviation 10
- anthropic/claude-haiku-4-5: `H 0/2/0 D 0/2/3` — deviation 10
- gemini/gemini-3.1-flash-lite: `H 0/1/0 D 0/5/0` — deviation 5
- openai/gpt-5.4-mini: `H 0/2/1 D 0/5/0` — deviation 5
- openai/gpt-5.4-nano: `H 1/0/0 D 0/0/0` — deviation 12

### United States v. Gholinejad — 4:24-cr-00016 (nced) #70402649

- truth: `H 0/2/1 D 0/9/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 12
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 12
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 12
- openai/gpt-5.4-mini: `H 0/0/0 D 0/0/0` — deviation 12
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 12

### United States v. Moucka — 2:24-cr-00180 (wawd) #69362701

- truth: `H 0/2/0 D 0/0/0`
- prod (live): `H 2/1/1 D 1/3/1` — deviation 9
- anthropic/claude-haiku-4-5: `H 1/1/2 D 1/3/1` — deviation 9
- gemini/gemini-3.1-flash-lite: `H 2/1/1 D 1/4/1` — deviation 10
- openai/gpt-5.4-mini: `H 2/1/1 D 1/4/0` — deviation 9
- openai/gpt-5.4-nano: `H 2/1/1 D 2/5/0` — deviation 11

### United States v. Akhter — 1:25-cr-00307 (vaed) #71989485

- truth: `H 0/6/0 D 0/6/0`
- prod (live): `H 0/6/4 D 0/6/0` — deviation 4
- anthropic/claude-haiku-4-5: `H 1/8/2 D 0/8/2` — deviation 9
- gemini/gemini-3.1-flash-lite: `H 1/6/0 D 0/5/1` — deviation 3
- openai/gpt-5.4-mini: `H 2/7/1 D 1/11/1` — deviation 11
- openai/gpt-5.4-nano: `H 0/7/1 D 1/5/0` — deviation 4

### Anthropic v. DOW — 26-1049 (cadc) #72380208

- truth: `H 0/1/0 D 1/12/0`
- prod (live): `H 0/1/0 D 2/5/1` — deviation 9
- anthropic/claude-haiku-4-5: `H 0/1/0 D 2/5/2` — deviation 10
- gemini/gemini-3.1-flash-lite: `H 0/1/0 D 1/9/0` — deviation 3
- openai/gpt-5.4-mini: `H 0/1/0 D 1/12/0` — deviation 0
- openai/gpt-5.4-nano: `H 0/1/0 D 4/9/0` — deviation 6

### United States v. Didenko — 1:24-cr-00261 (dcd) #68810724

- truth: `H 0/8/0 D 0/5/0`
- prod (live): `H 0/5/0 D 0/5/0` — deviation 3
- anthropic/claude-haiku-4-5: `H 0/3/0 D 0/5/1` — deviation 6
- gemini/gemini-3.1-flash-lite: `H 0/4/0 D 0/5/0` — deviation 4
- openai/gpt-5.4-mini: `H 1/5/0 D 0/7/3` — deviation 9
- openai/gpt-5.4-nano: `H 0/4/0 D 0/5/0` — deviation 4

### United States v. Tymoshchuk — 1:23-cr-00324 (nyed) #70029216

- truth: `H 1/4/0 D 0/3/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 8
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 8
- gemini/gemini-3.1-flash-lite: `H 0/1/0 D 0/0/0` — deviation 7
- openai/gpt-5.4-mini: `H 0/0/1 D 0/0/0` — deviation 9
- openai/gpt-5.4-nano: `H 3/0/0 D 0/0/0` — deviation 9

### United States v. Zolotarjovs — 1:24-cr-00076 (ohsd) #69060414

- truth: `H 0/10/0 D 0/1/0`
- prod (live): `H 0/8/2 D 0/3/0` — deviation 6
- anthropic/claude-haiku-4-5: `H 0/9/1 D 0/3/0` — deviation 4
- gemini/gemini-3.1-flash-lite: `H 0/10/1 D 0/1/0` — deviation 1
- openai/gpt-5.4-mini: `H 0/4/1 D 0/2/0` — deviation 8
- openai/gpt-5.4-nano: `H 1/5/1 D 0/2/0` — deviation 8

### United States v. Martino — 1:26-cr-20065 (flsd) #72389253

- truth: `H 0/2/0 D 1/3/0`
- prod (live): `H 1/2/0 D 0/5/0` — deviation 4
- anthropic/claude-haiku-4-5: `H 1/2/0 D 1/5/1` — deviation 4
- gemini/gemini-3.1-flash-lite: `H 1/3/0 D 1/7/0` — deviation 6
- openai/gpt-5.4-mini: `H 1/2/1 D 2/7/0` — deviation 7
- openai/gpt-5.4-nano: `H 1/2/0 D 4/6/0` — deviation 7

### Anthropic v. DOW — 26-2011 (ca9) #73136734

- truth: `H 0/1/0 D 0/1/0`
- prod (live): `H 0/0/0 D 0/1/3` — deviation 4
- anthropic/claude-haiku-4-5: `H 0/0/0 D 1/2/2` — deviation 5
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 1/2/2` — deviation 5
- openai/gpt-5.4-mini: `H 0/0/0 D 1/5/0` — deviation 6
- openai/gpt-5.4-nano: `H 0/0/0 D 2/3/0` — deviation 5

### United States v. Chapman — 1:24-cr-00220 (dcd) #68534169

- truth: `H 0/5/0 D 0/3/0`
- prod (live): `H 0/4/0 D 0/7/0` — deviation 5
- anthropic/claude-haiku-4-5: `H 0/5/0 D 0/8/0` — deviation 5
- gemini/gemini-3.1-flash-lite: `H 0/5/0 D 0/5/0` — deviation 2
- openai/gpt-5.4-mini: `H 0/3/0 D 0/7/0` — deviation 6
- openai/gpt-5.4-nano: `H 1/3/0 D 0/6/0` — deviation 6

### United States v. Gholinejad — 25-4607 (ca4) #71906511

- truth: `H 0/0/0 D 0/8/0`
- prod (live): `H 0/0/0 D 1/3/0` — deviation 6
- anthropic/claude-haiku-4-5: `H 0/0/0 D 1/3/0` — deviation 6
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 1/3/0` — deviation 6
- openai/gpt-5.4-mini: `H 0/0/0 D 1/3/0` — deviation 6
- openai/gpt-5.4-nano: `H 0/0/0 D 1/3/0` — deviation 6

### United States v. Tymoshchuk — 1:23-cr-00324 (nyed) #71300581

- truth: `H 0/2/0 D 0/4/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 6
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 6
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 6
- openai/gpt-5.4-mini: `H 0/0/0 D 0/0/0` — deviation 6
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 6

### United States v. Lytvynenko — 3:23-cr-00088 (tnmd) #71820111

- truth: `H 0/2/0 D 0/4/0`
- prod (live): `H 3/2/1 D 1/3/0` — deviation 6
- anthropic/claude-haiku-4-5: `H 3/2/0 D 1/3/0` — deviation 5
- gemini/gemini-3.1-flash-lite: `H 4/2/0 D 1/3/0` — deviation 6
- openai/gpt-5.4-mini: `H 3/2/0 D 2/3/0` — deviation 6
- openai/gpt-5.4-nano: `H 3/2/0 D 1/3/0` — deviation 5

### United States v. Knoot — 26-5455 (ca6) #73388385

- truth: `H 0/0/0 D 2/2/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 4
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 4
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 4
- openai/gpt-5.4-mini: `H 0/0/0 D 0/0/0` — deviation 4
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 4

### United States v. Zhenxing Wang — 1:25-cr-10273 (mad) #70678228

- truth: `H 0/5/0 D 0/0/0`
- prod (live): `H 0/4/1 D 0/1/0` — deviation 3
- anthropic/claude-haiku-4-5: `H 0/3/0 D 0/1/0` — deviation 3
- gemini/gemini-3.1-flash-lite: `H 0/5/0 D 0/0/0` — deviation 0
- openai/gpt-5.4-mini: `H 0/4/1 D 0/2/0` — deviation 4
- openai/gpt-5.4-nano: `H 1/3/0 D 0/1/0` — deviation 4

### United States v. Gholinejad — 4:24-cr-00016 (nced) #70378502

- truth: `H 0/2/1 D 0/9/0`
- prod (live): `H 1/2/1 D 0/6/0` — deviation 4
- anthropic/claude-haiku-4-5: `H 0/3/1 D 0/9/0` — deviation 1
- gemini/gemini-3.1-flash-lite: `H 0/3/1 D 0/9/0` — deviation 1
- openai/gpt-5.4-mini: `H 1/2/1 D 0/11/0` — deviation 3
- openai/gpt-5.4-nano: `H 2/2/1 D 0/8/0` — deviation 3

### United States v. Kejia "Tony" Wang — 1:25-cr-10274 (mad) #70691920

- truth: `H 0/2/0 D 0/0/0`
- prod (live): `H 1/1/0 D 0/1/0` — deviation 3
- anthropic/claude-haiku-4-5: `H 1/1/0 D 1/0/0` — deviation 3
- gemini/gemini-3.1-flash-lite: `H 1/1/0 D 0/0/0` — deviation 2
- openai/gpt-5.4-mini: `H 1/1/0 D 0/1/0` — deviation 3
- openai/gpt-5.4-nano: `H 1/1/0 D 0/0/0` — deviation 2

### United States v. Zewei — 4:23-cr-00523 (txsd) #70789744

- truth: `H 0/2/0 D 1/2/0`
- prod (live): `H 2/3/0 D 1/2/0` — deviation 3
- anthropic/claude-haiku-4-5: `H 2/3/0 D 1/2/0` — deviation 3
- gemini/gemini-3.1-flash-lite: `H 2/3/0 D 1/2/0` — deviation 3
- openai/gpt-5.4-mini: `H 2/2/0 D 1/2/0` — deviation 2
- openai/gpt-5.4-nano: `H 2/2/0 D 1/2/0` — deviation 2

### United States v. Tymoshchuk — 1:23-cr-00324 (nyed) #70701403

- truth: `H 0/2/0 D 0/0/0`
- prod (live): `H 1/2/0 D 0/0/0` — deviation 1
- anthropic/claude-haiku-4-5: `H 1/2/0 D 0/0/0` — deviation 1
- gemini/gemini-3.1-flash-lite: `H 1/2/0 D 0/0/0` — deviation 1
- openai/gpt-5.4-mini: `H 1/2/0 D 0/0/0` — deviation 1
- openai/gpt-5.4-nano: `H 1/0/0 D 0/0/0` — deviation 3

### United States v. Stryzhak — 1:25-cr-00381 (nyed) #72011504

- truth: `H 0/1/0 D 0/0/0`
- prod (live): `H 1/1/2 D 0/0/0` — deviation 3
- anthropic/claude-haiku-4-5: `H 1/1/0 D 0/0/0` — deviation 1
- gemini/gemini-3.1-flash-lite: `H 1/1/0 D 0/0/0` — deviation 1
- openai/gpt-5.4-mini: `H 1/1/0 D 0/0/0` — deviation 1
- openai/gpt-5.4-nano: `H 2/1/0 D 0/0/0` — deviation 2

### United States v. Eileen Wang — 2:26-cr-00186 (cacd) #73323008

- truth: `H 0/3/0 D 0/0/0`
- prod (live): `H 1/1/0 D 0/0/0` — deviation 3
- anthropic/claude-haiku-4-5: `H 1/1/0 D 0/0/0` — deviation 3
- gemini/gemini-3.1-flash-lite: `H 1/1/0 D 0/0/0` — deviation 3
- openai/gpt-5.4-mini: `H 1/1/0 D 0/0/0` — deviation 3
- openai/gpt-5.4-nano: `H 1/1/0 D 0/0/0` — deviation 3

### United States v. Zheng et al. — 1:26-mj-00315 (gand) #73103748

- truth: `H 0/1/1 D 1/0/0`
- prod (live): `H 0/1/1 D 0/0/0` — deviation 1
- anthropic/claude-haiku-4-5: `H 0/2/0 D 0/0/0` — deviation 3
- gemini/gemini-3.1-flash-lite: `H 0/1/1 D 0/0/0` — deviation 1
- openai/gpt-5.4-mini: `H 0/1/1 D 0/0/0` — deviation 1
- openai/gpt-5.4-nano: `H 0/0/2 D 0/0/0` — deviation 3

### United States v. Schmitz — 1:24-cr-00234 (njd) #73292090

- truth: `H 1/1/0 D 0/0/0`
- prod (live): `H 0/1/1 D 0/0/0` — deviation 2
- anthropic/claude-haiku-4-5: `H 0/1/0 D 0/0/0` — deviation 1
- gemini/gemini-3.1-flash-lite: `H 0/1/0 D 1/0/0` — deviation 2
- openai/gpt-5.4-mini: `H 0/1/0 D 1/0/0` — deviation 2
- openai/gpt-5.4-nano: `H 1/1/0 D 1/0/0` — deviation 1

### United States v. Schmitz — 1:24-cr-00234 (njd) #73353898

- truth: `H 1/1/0 D 0/0/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 2
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 2
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 2
- openai/gpt-5.4-mini: `H 0/0/0 D 0/0/0` — deviation 2
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 2

### United States v. Eileen Wang — 2:26-cr-00186 (cacd) #73326420

- truth: `H 0/2/0 D 0/0/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 2
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 2
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 2
- openai/gpt-5.4-mini: `H 0/0/0 D 0/0/0` — deviation 2
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 2

### United States v. Zheng et al. — 3:26-mj-70297 (cand) #72532372

- truth: `H 0/4/0 D 0/0/0`
- prod (live): `H 0/5/0 D 0/0/0` — deviation 1
- anthropic/claude-haiku-4-5: `H 0/6/0 D 0/0/0` — deviation 2
- gemini/gemini-3.1-flash-lite: `H 0/5/0 D 0/0/0` — deviation 1
- openai/gpt-5.4-mini: `H 0/4/0 D 0/0/0` — deviation 0
- openai/gpt-5.4-nano: `H 1/3/0 D 0/0/0` — deviation 2

### United States v. Volkov — 1:25-cr-00211 (insd) #71842241

- truth: `H 0/3/0 D 1/1/0`
- prod (live): `H 0/3/0 D 0/1/0` — deviation 1
- anthropic/claude-haiku-4-5: `H 0/3/0 D 0/1/0` — deviation 1
- gemini/gemini-3.1-flash-lite: `H 0/3/0 D 0/1/0` — deviation 1
- openai/gpt-5.4-mini: `H 0/3/0 D 1/1/0` — deviation 0
- openai/gpt-5.4-nano: `H 0/3/0 D 0/1/0` — deviation 1

### United States v. Kim Kwang Jin et al. — 1:25-cr-00291 (gand) #70673091

- truth: `H 0/0/0 D 0/0/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 0
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 0
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 0
- openai/gpt-5.4-mini: `H 0/0/0 D 0/0/0` — deviation 0
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 0

### United States v. Jong Song Hwa et al. — 4:24-cr-00648 (moed) #69459808

- truth: `H 0/0/0 D 0/0/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 0
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 0
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 0
- openai/gpt-5.4-mini: `H 0/0/0 D 0/0/0` — deviation 0
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 0

### United States v. Dubranova — 2:25-cr-00578 (cacd) #72013021

- truth: `H 0/0/0 D 0/0/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 0
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 0
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 0
- openai/gpt-5.4-mini: `H 0/0/0 D 0/0/0` — deviation 0
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 0

### United States v. Stryzhak — 1:25-cr-00381 (nyed) #72012131

- truth: `H 0/0/0 D 0/0/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 0
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 0
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 0
- openai/gpt-5.4-mini: `H 0/0/0 D 0/0/0` — deviation 0
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 0

### United States v. Berezhnoy et al. — 8:23-cr-00459 (mdd) #69629801

- truth: `H 0/0/0 D 0/0/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 0
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 0
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 0
- openai/gpt-5.4-mini: `H 0/0/0 D 0/0/0` — deviation 0
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 0

### United States v. Berezhnoy et al. — 8:23-cr-00459 (mdd) #70711269

- truth: `H 0/0/0 D 0/0/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 0
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 0
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 0
- openai/gpt-5.4-mini: `H 0/0/0 D 0/0/0` — deviation 0
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 0

### United States v. Berezhnoy et al. — 8:23-cr-00459 (mdd) #70821399

- truth: `H 0/0/0 D 0/0/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 0
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 0
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 0
- openai/gpt-5.4-mini: `H 0/0/0 D 0/0/0` — deviation 0
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 0

### United States v. Zheng et al. — 1:26-mj-00316 (gand) #72798154

- truth: `H 0/0/0 D 1/0/0`
- prod (live): `H 0/0/0 D 1/0/0` — deviation 0
- anthropic/claude-haiku-4-5: `H 0/0/0 D 1/0/0` — deviation 0
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 1/0/0` — deviation 0
- openai/gpt-5.4-mini: `H 0/0/0 D 1/0/0` — deviation 0
- openai/gpt-5.4-nano: `H 0/0/0 D 1/0/0` — deviation 0
