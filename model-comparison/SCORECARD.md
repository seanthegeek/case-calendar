# Provider accuracy vs human ground truth

Scored **46** of 46 CourtListener records (those with all six counts filled in). Lower deviation = closer to the human-read truth. Deviation is the sum of |model count − your count| over the six status categories.

This release (0.13.0) flips the **extraction-track default to Gemini** (`gemini-3.1-flash-lite`) while keeping the **summary-track default on Anthropic** (`claude-sonnet-4-6`). This split — Gemini reading docket entries into hearings + deadlines, Anthropic writing the per-docket case summaries — is now the zero-config default: an operator who supplies API keys but sets no `LLM_*` env vars auto-detects Gemini for extraction and Anthropic for summaries. It is no longer merely a documented override.

Gemini wins extraction on this build's measurements: aggregate deviation **305** (best in the table), with the best `D met/pass` in the table (125 vs Anthropic's 155) and far fewer spurious cancellations than Anthropic (`D canc` 9 vs 28). It is also \~3.75× cheaper than Anthropic on the constant-load extract+verify pair and \~1.9× faster per call (see the cost + speed tables below).

## Why the extraction default flipped to Gemini in 0.13.0

Earlier releases (0.10.0 / 0.11.0) kept Anthropic as the extraction default for a coverage reason, not a deviation-score reason. Relying on its intrinsic training priors, Gemini systematically classified a long tail of substantive federal-procedure deadline classes as **procedural-minor**, and those then dropped out at the render-time significance gate — off subscriber calendars entirely. The classes observed missing while running Gemini in production:

- PSR interview / first PSR disclosure / PSR objection windows
- Speedy Trial Act 18 U.S.C. § 3161(h) exclusion orders
- Surrender for service of sentence (the date a defendant must self-report to BOP custody)
- Civil-forfeiture Supp. R. G claim + answer deadlines
- Substantive sealing motion practice (briefing on a motion to seal/unseal, not the routine "filed under seal" stamps)
- Exhibit-filing deadlines under a final pretrial order
- Certified administrative record / certified index of the administrative record (the deadline that starts the APA cross-motion briefing clock)

The deviation score never surfaced these — they are failure modes outside the scored set, and aggregate deviation rewards a provider that gets the common cases right at scale. But a docket-watching calendar's value depends on NOT missing the rare substantive deadline, which is exactly why the silent-drop risk kept Anthropic the default through 0.11.0.

0.13.0 closes that gap **in the prompt, for every provider**. The unified extraction `SYSTEM_PROMPT` now carries a structured `DEADLINE_SIGNIFICANCE_RULES` block (ordered `RULE 1-5`: it enumerates the substantive classes explicitly AND biases the default toward `major`), parallel to the existing `HEARING_SIGNIFICANCE_RULES`. Because the substantive classes are now NAMED IN THE PROMPT rather than left to a model's intrinsic priors, every provider sees the same apples-to-apples instructions, and Gemini classifies them as `major`. The correct framing is that **the prompt now carries the priors for every provider** — Gemini's training did not change, the instructions it receives did. The measured result is that Gemini's `D met/pass` became the best in the table (125 vs Anthropic's 155) and its spurious-cancellation rate dropped well below Anthropic's (`D canc` 9 vs 28), and the deadline-bucketing gap that kept Anthropic the default is closed.

The honest caveat survives: the ruleset enumerates the substantive classes the project currently knows about. An operator whose caseload includes substantive classes the ruleset does NOT enumerate should still verify against their own docket set, and the per-track override env vars (`LLM_EXTRACTION_PROVIDER` / `LLM_SUMMARY_PROVIDER` / the global `LLM_PROVIDER`) remain available. But the risk is materially **reduced**, not merely shifted from one provider to another: the enumeration is now in-prompt for both providers, so neither relies on intrinsic priors for the named classes.

The summary track stays Anthropic because Sonnet pulls more case-distinguishing detail (statute citations, count numbers, sentence breakdowns, custody status, cancelled-schedule notes); that rationale is unchanged and is documented in the **Summary track** section below.

### Why deviation alone doesn't pick the provider

Deviation is a useful single number, but it deliberately does not distinguish "missed a substantive event the human counted" from "extracted a noisy procedural event the human didn't count" — both move the score by the same magnitude. That is exactly why the silent-drop coverage problem could persist underneath a *good* Gemini deviation score in earlier releases. The decision to ship Gemini-extraction in 0.13.0 rests on the coverage gap being addressed in-prompt, not on the deviation number alone; the number is corroborating evidence, not the sole basis.

## Totals (lower is better)

| model | total deviation | H sched | H held | H canc | D pend | D met/pass | D canc |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| gemini/gemini-3.1-flash-lite | **305** | 26 | 78 | 32 | 35 | 125 | 9 |
| prod (live) | **342** | 31 | 85 | 28 | 29 | 144 | 25 |
| openai/gpt-5.4-mini | **343** | 59 | 83 | 18 | 27 | 151 | 5 |
| anthropic/claude-haiku-4-5 | **349** | 33 | 81 | 22 | 30 | 155 | 28 |
| openai/gpt-5.4-nano | **380** | 70 | 91 | 16 | 58 | 140 | 5 |

Gemini leads on aggregate deviation (305) and on both deadline axes that drive the substantive-class question: `D met/pass` 125 (vs Anthropic 155) and `D canc` 9 (vs Anthropic 28). Anthropic's higher `D canc` deviation comes from extra cancellations the human didn't count; OpenAI nano's low `H_canc`/`D_canc` is partly an artifact of under-extraction (note its `H_sched=70` and `D_pend=58`, the highest in the table — nano's pattern is to leave many proceedings in `scheduled`/`pending` rather than transitioning them). The prod row is the current live store (Anthropic-extraction, pre-0.13.0-default); it is the fidelity reference for the anthropic column rather than a candidate.

## Wall-clock + cost per model

Full from-scratch backfill of the maintainer's caseload (28 cases / 34 logical PACER dockets / 46 CourtListener records), all four models built in PARALLEL against the SAME cached CourtListener responses (every CourtListener fetch happens at most once total — see the CourtListener API line below).

| model | wall-clock | mean s/call | extract | verify | summary | **TOTAL** |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| gemini/gemini-3.1-flash-lite | **39.3m** | **1.7s** | $1.8175 | $0.1892 | $1.1097 | **$3.1164** |
| openai/gpt-5.4-mini | 39.7m | 1.8s | $3.7291 | $0.4534 | $1.6680 | $5.8505 |
| openai/gpt-5.4-nano | 57.9m | 2.6s | $1.0983 | $0.1341 | $1.5242 | $2.7566 |
| anthropic/claude-haiku-4-5 | 70.7m | 3.2s | $6.7166 | $0.8317 | $2.2962 | $9.8444 |

Call counts this build: 1150 extraction calls per model; verify 134 (anthropic) / 141 (gemini) / 155 (openai-nano) / 156 (openai-mini); summary 35 (anthropic) / 34 (the others).

Gemini is **\~1.9× faster than Anthropic per call** (1.7s vs 3.2s), about the same as OpenAI mini, and \~1.5× faster than OpenAI nano. The cost spread on the extract+verify pair — the constant load that runs on every sync, with summaries off — is starker:

- **anthropic** $7.55
- **openai-mini** $4.18
- **gemini** $2.01
- **openai-nano** $1.23

Anthropic costs **\~3.75× what Gemini costs** for that constant-load extract+verify workload. On a hobby-scale 28-case caseload this is dollars per year, not per day — see [docs/cost.md](../docs/cost.md) for the per-case math an operator can apply to their own caseload. The summary track is rare (one call per docket, only when a primary document or disposition lands), so keeping Anthropic on summaries adds little to steady-state spend: the Sonnet-over-Gemini summary delta is about **$1.19** across the full 34-docket backfill ($2.2962 vs $1.1097) and roughly **$0.04 per ongoing summary**. The case-distinguishing detail Sonnet captures (see **Summary track** below) is worth that for a docket-watching audience.

The fidelity check on the cost build: the anthropic column should track the current prod store (both run Anthropic extraction), so a large divergence on the anthropic row's output is a signal that something in the replay drifted from the live pipeline rather than a real model difference.

CourtListener API (one-time, shared across all four columns rather than incurred per model): 38 total calls; peak 9/min, 38/hour, 38/day. The shared, thread-safe response cache guarantees each CourtListener fetch happens at most once total across the whole build, so the dollar figures above are LLM spend only.

## Output row counts

What each model's store actually contains after the backfill (`hearings / hearings_scheduled / hearings_held / deadlines / case_summaries`):

| model | hearings | scheduled | held | deadlines | summaries |
| --- | ---: | ---: | ---: | ---: | ---: |
| prod (current) | 220 | 30 | 160 | 349 | 34 |
| anthropic/claude-haiku-4-5 | 204 | 32 | 148 | 372 | 34 |
| openai/gpt-5.4-nano | 207 | 69 | 122 | 304 | 34 |
| gemini/gemini-3.1-flash-lite | 220 | 25 | 159 | 392 | 34 |
| openai/gpt-5.4-mini | 200 | 58 | 124 | 366 | 34 |

Gemini lands closest to prod on total hearings (220 vs 220) and on held hearings (159 vs 160), and produces the most deadlines (392) — consistent with the `DEADLINE_SIGNIFICANCE_RULES` block keeping substantive deadline classes as `major` rather than dropping them. OpenAI nano's high `scheduled` (69) and low `held` (122) is the same under-transitioning pattern visible in its deviation axes. All four produce the same 34 case summaries (one per docket).

## Two tracks, two providers

The codebase has two independent provider/model knobs:

- **Extraction track** — every relevant docket entry: `LLM_EXTRACTION_PROVIDER` (override) > `LLM_PROVIDER` (global) > API-key auto-detect (extraction prefers gemini > anthropic > openai). \~1150 calls per backfill. High-volume, low-context, structured-output classification.
- **Summary track** — one call per docket when a primary document or disposition lands (opt-in via `case_summaries.enabled`): `LLM_SUMMARY_PROVIDER` (override) > `LLM_PROVIDER` (global) > auto-detect (summary/base prefers anthropic > gemini > openai). \~34 calls per backfill, \~near zero ongoing. Low-volume, long-context, synthesis-heavy.

The auto-detect key priority is per-track, which is what makes the Gemini-extraction / Anthropic-summary split the zero-config default: with both an Anthropic key and a Gemini key present and no `LLM_*` env vars set, extraction resolves to Gemini and summaries resolve to Anthropic. A global `LLM_PROVIDER` overrides both tracks; the per-track override vars override their own track.

## Qualitative event-set diffs — extraction track

The deviation score doesn't distinguish "missed a substantive event the human counted" from "extracted a noisy procedural event the human didn't count." Both move the score by the same magnitude. The 0.8.2 revert from Gemini → Anthropic was driven by Gemini systematically dropping substantive event classes (preliminary-injunction hearings, Speedy Trial Act exclusions, PSR deadlines, CIPA submissions, jury-process deadlines, surrender-for-service-of-sentence). All of those are now caught — by both providers — after the matched prompt edits in this release:

| SCORECARD-era ask | Was caught by | Now caught by |
| --- | --- | --- |
| McGonigal sentencing transcript class (4 events: 2023-06-25 conference release, 2023-08-25 plea redaction, 2024-01-19 sentencing redaction, 2024-03-28 sentencing release) | Gemini only | **both** |
| Knoot motion-in-limine briefing chain (response, reply, suppression hearing, expert disclosure, govt expert response) | Gemini only | **both** (Anthropic also adds the bonus expert-reply deadline) |
| Akhter multi-defendant divergence (Muneeb plea + Sohaib pro-se trial, with per-defendant arraignment / change-of-plea / sentencing keys) | Gemini only | **both** |
| Ding substantive cybercrime arcs (motion to suppress, evidentiary hearing, jury selection / trial held) | Gemini only | **both** (Anthropic catches 18+ distinct motion-hearing sub-keys, including the Daubert per-witness keys) |
| DOW preliminary-injunction hearing | Anthropic only | **both** |
| Speedy Trial Act stipulations on Ding | Anthropic only | **both** |
| PSR deadlines | Anthropic only | **both** |
| Ashtor CIPA-pretrial-conference (per-defendant) | Anthropic only | **both** |
| McGonigal classified-info filings + surrender-for-service-of-sentence | Anthropic only | **both** |
| Akhter jury-process (questionnaire, instructions, final pretrial) | Anthropic only | **both** |

The substantive deadline classes that earlier releases relied on Anthropic's intrinsic priors to catch are now handled by `DEADLINE_SIGNIFICANCE_RULES` for every provider: RULE 2 names most of them explicitly (PSR objections, surrender for service of sentence, civil-forfeiture claim/answer, the certified administrative record, substantive sealing / CIPA filings, exhibit lists), and RULE 5 biases anything it does not name — e.g. a Speedy Trial Act exclusion — toward `major`. So both providers classify them from the same instructions rather than depending on what either model learned in training.

## Summary track — Gemini vs Anthropic

The summary track is the one place the comparison still favors Anthropic, though the margin is narrower than the extraction picture and rests on a different basis. Both providers run a higher tier here (Anthropic Sonnet 4.6 vs Gemini 2.5 Pro), and Gemini 2.5 Pro writes detailed, case-distinct summaries — it names the defendants, the imposed sentences, the dollar figures, the aliases. It is not the weak link the cheap extraction tier was. Anthropic's remaining edge is narrower and specific: its summaries run longer (median \~1084 chars vs Gemini's \~670, \~62% longer) and carry a few categories of detail Gemini's still drop.

### What Anthropic still adds

Measured across the full 34-summary comparison (both providers' stores from this build):

1. **Statutory citations** — Anthropic's summaries carry 7 `U.S.C.` citations (`41 U.S.C. § 4713`, `41 U.S.C. § 3252`, `18 U.S.C. § 371`, `18 U.S.C. § 951`, …); Gemini's carry none at all. The DOW litigation group (`cadc 26-1049`, `ca9 26-2011`, `cand 3:26-cv-01996`) is where this matters most: the three dockets are parts of the same fight, distinguishable by which statutory authority each targets. Anthropic names them — the **§ 4713** supply-chain-risk determination challenged in the D.C. Circuit, the **§ 3252** designation challenged in the Northern District of California — while Gemini collapses all three to "supply-chain risk … under different statutes," losing the axis that explains why they are separate proceedings.
2. **Count-by-number enumeration** — Anthropic enumerates specific counts (`Count 1`, `Count 7`, …) 11 times across the caseload; Gemini gives the offense type with no count numbers (zero `Count N` references).
3. **Cancelled / vacated schedules** — Anthropic flags cancelled or vacated proceedings (4 mentions across the caseload); Gemini surfaces none. On a docket-watching calendar this is material — a cancelled trial date is exactly what a watcher needs to see.
4. **Granular charge lists and procedural history** — on the ransomware group, Anthropic enumerates all six charged offenses and the original-vs-superseding-indictment timeline for Berezhnoy/8Base, and names the related sealed criminal case plus the four victim-claimants for Gallyamov/Qakbot; Gemini's versions are accurate but coarser.

What Gemini 2.5 Pro does NOT drop — and what earlier, cheaper summary models did — is the case-distinguishing core: distinct defendants, imposed sentences (Chapman's 102-month term, Didenko's 60 months), dollar figures (Didenko's \~$1.4M forfeiture money judgment, Ashtor's $89,000), even custody status. So the summary-track choice is no longer "detail vs blur"; it is the narrower margin above plus length. The summary track is rare (one call per docket, only when a primary document or disposition lands), so keeping the higher-detail provider costs little — see below — and the default stays Anthropic on that margin.

### Cost / latency delta for the summary track

| Provider | Summary track cost (full backfill, 34 dockets) | Per-docket cost |
| --- | ---: | ---: |
| gemini gemini-2.5-pro | $1.1097 | \~$0.03 |
| openai gpt-5.4-nano | $1.5242 | \~$0.04 |
| openai gpt-5.4-mini | $1.6680 | \~$0.05 |
| anthropic claude-sonnet-4-6 | $2.2962 | \~$0.07 |

So the Sonnet-over-Gemini summary upgrade is about **$1.19 across the 34-docket backfill** and roughly **$0.04 per ongoing summary** (rare — only when a primary document or disposition lands on a docket). For the docket-watching audience this calendar is built for, the statutory / count / cancelled-schedule detail Sonnet still adds is worth the dollar — which is why the summary default stays Anthropic even as the extraction default moves to Gemini.

## Configuring the tracks

The 0.13.0 zero-config default needs no env vars beyond the API keys: with both an Anthropic key and a Gemini key present, extraction auto-detects Gemini and summaries auto-detect Anthropic.

```bash
# .env — zero-config 0.13.0 default (Gemini extraction + Anthropic summaries)
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=...
```

To pin the split explicitly (or to run on a single provider's keys), use the per-track overrides:

```bash
# .env — explicit split, equivalent to the zero-config default above
LLM_EXTRACTION_PROVIDER=gemini       # extraction track
LLM_SUMMARY_PROVIDER=anthropic       # summary track
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=...
```

An operator who has measured their own caseload and wants both tracks on Anthropic — e.g. because their docket set includes substantive deadline classes the `DEADLINE_SIGNIFICANCE_RULES` block does not yet enumerate — can set the global default and skip the per-track vars:

```bash
# .env — both tracks on Anthropic (the pre-0.13.0 default)
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

The global `LLM_PROVIDER` applies to both tracks; the per-track override vars override their own track and take precedence over it.

## Per-docket detail

Truth vs each model. Format: `H scheduled/held/cancelled  D pending/met-or-passed/cancelled`.

### United States v. Ding — 3:24-cr-00141 (cand) #68317014

- truth: `H 0/29/0 D 2/59/0`
- prod (live): `H 2/37/6 D 5/78/0` — deviation 38
- anthropic/claude-haiku-4-5: `H 2/39/4 D 10/84/0` — deviation 49
- gemini/gemini-3.1-flash-lite: `H 2/37/8 D 5/72/0` — deviation 34
- openai/gpt-5.4-mini: `H 11/26/8 D 4/74/0` — deviation 39
- openai/gpt-5.4-nano: `H 24/31/7 D 28/46/0` — deviation 72

### United States v. Wei — 3:23-cr-01471 (casd) #67661286

- truth: `H 0/23/0 D 0/74/0`
- prod (live): `H 0/19/0 D 7/36/1` — deviation 50
- anthropic/claude-haiku-4-5: `H 0/21/0 D 5/43/0` — deviation 38
- gemini/gemini-3.1-flash-lite: `H 0/22/2 D 11/60/0` — deviation 28
- openai/gpt-5.4-mini: `H 0/16/0 D 10/49/0` — deviation 42
- openai/gpt-5.4-nano: `H 2/14/1 D 10/45/0` — deviation 51

### United States v. Akhter — 1:25-cr-00307 (vaed) #73333500

- truth: `H 0/15/0 D 0/14/0`
- prod (live): `H 3/5/2 D 6/6/5` — deviation 34
- anthropic/claude-haiku-4-5: `H 3/5/0 D 4/5/5` — deviation 31
- gemini/gemini-3.1-flash-lite: `H 1/8/4 D 5/9/2` — deviation 24
- openai/gpt-5.4-mini: `H 2/7/0 D 2/7/0` — deviation 19
- openai/gpt-5.4-nano: `H 1/4/0 D 6/4/0` — deviation 28

### United States v. Wei — 3:23-cr-01471 (casd) #67661185

- truth: `H 0/21/0 D 0/19/0`
- prod (live): `H 2/3/3 D 0/17/0` — deviation 25
- anthropic/claude-haiku-4-5: `H 2/4/0 D 0/15/0` — deviation 23
- gemini/gemini-3.1-flash-lite: `H 1/2/1 D 0/14/0` — deviation 26
- openai/gpt-5.4-mini: `H 1/6/1 D 1/8/0` — deviation 29
- openai/gpt-5.4-nano: `H 1/7/0 D 1/9/0` — deviation 26

### United States v. Knoot — 3:24-cr-00151 (tnmd) #69026861

- truth: `H 0/11/0 D 0/4/0`
- prod (live): `H 0/14/0 D 0/14/2` — deviation 15
- anthropic/claude-haiku-4-5: `H 0/10/1 D 0/13/2` — deviation 13
- gemini/gemini-3.1-flash-lite: `H 0/10/2 D 1/12/2` — deviation 14
- openai/gpt-5.4-mini: `H 5/10/0 D 0/16/0` — deviation 18
- openai/gpt-5.4-nano: `H 7/8/1 D 2/12/3` — deviation 24

### United States v. Akhter — 1:25-cr-00307 (vaed) #73320754

- truth: `H 0/7/0 D 0/7/0`
- prod (live): `H 4/2/2 D 0/5/4` — deviation 17
- anthropic/claude-haiku-4-5: `H 2/2/2 D 0/4/5` — deviation 17
- gemini/gemini-3.1-flash-lite: `H 1/0/4 D 1/10/1` — deviation 17
- openai/gpt-5.4-mini: `H 1/1/0 D 0/5/0` — deviation 9
- openai/gpt-5.4-nano: `H 5/0/0 D 1/0/1` — deviation 21

### United States v. McGonigal — 1:23-cr-00016 (nysd) #66749883

- truth: `H 0/4/0 D 0/26/0`
- prod (live): `H 5/6/2 D 0/28/1` — deviation 12
- anthropic/claude-haiku-4-5: `H 5/6/1 D 0/32/0` — deviation 14
- gemini/gemini-3.1-flash-lite: `H 4/7/2 D 0/38/0` — deviation 21
- openai/gpt-5.4-mini: `H 9/2/1 D 0/33/1` — deviation 20
- openai/gpt-5.4-nano: `H 3/6/1 D 0/29/0` — deviation 9

### United States v. Gallyamov — 2:25-cv-04631 (cacd) #70341311

- truth: `H 0/6/0 D 0/2/0`
- prod (live): `H 0/3/3 D 0/13/0` — deviation 17
- anthropic/claude-haiku-4-5: `H 1/2/2 D 0/10/0` — deviation 15
- gemini/gemini-3.1-flash-lite: `H 1/6/0 D 0/17/0` — deviation 16
- openai/gpt-5.4-mini: `H 2/3/0 D 0/17/0` — deviation 20
- openai/gpt-5.4-nano: `H 3/3/0 D 0/5/0` — deviation 9

### United States v. Ashtor — 1:25-cr-20021 (flsd) #69570297

- truth: `H 0/5/1 D 0/13/0`
- prod (live): `H 0/6/5 D 0/7/3` — deviation 14
- anthropic/claude-haiku-4-5: `H 0/5/5 D 0/7/6` — deviation 16
- gemini/gemini-3.1-flash-lite: `H 0/6/5 D 0/10/0` — deviation 8
- openai/gpt-5.4-mini: `H 1/5/4 D 0/15/0` — deviation 6
- openai/gpt-5.4-nano: `H 1/4/3 D 0/10/0` — deviation 7

### Anthropic v. DOW — 3:26-cv-01996 (cand) #72379655

- truth: `H 0/2/0 D 7/22/0`
- prod (live): `H 1/4/0 D 4/27/2` — deviation 13
- anthropic/claude-haiku-4-5: `H 1/2/1 D 7/29/0` — deviation 9
- gemini/gemini-3.1-flash-lite: `H 1/2/0 D 8/29/0` — deviation 9
- openai/gpt-5.4-mini: `H 1/3/0 D 6/25/0` — deviation 6
- openai/gpt-5.4-nano: `H 1/2/0 D 7/14/0` — deviation 9

### United States v. Gholinejad — 4:24-cr-00016 (nced) #70402649

- truth: `H 0/2/1 D 0/9/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 12
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 12
- gemini/gemini-3.1-flash-lite: `H 1/0/1 D 0/0/0` — deviation 12
- openai/gpt-5.4-mini: `H 1/0/0 D 0/0/0` — deviation 13
- openai/gpt-5.4-nano: `H 0/0/0 D 0/1/0` — deviation 11

### United States v. Didenko — 1:24-cr-00261 (dcd) #68810897

- truth: `H 0/6/0 D 0/5/0`
- prod (live): `H 0/1/0 D 0/3/0` — deviation 7
- anthropic/claude-haiku-4-5: `H 0/1/0 D 0/3/2` — deviation 9
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/4/0` — deviation 7
- openai/gpt-5.4-mini: `H 1/0/0 D 0/0/0` — deviation 12
- openai/gpt-5.4-nano: `H 0/2/0 D 0/0/0` — deviation 9

### United States v. Moucka — 2:24-cr-00180 (wawd) #69362701

- truth: `H 0/2/0 D 0/0/0`
- prod (live): `H 1/1/2 D 1/3/1` — deviation 9
- anthropic/claude-haiku-4-5: `H 1/1/2 D 1/4/0` — deviation 9
- gemini/gemini-3.1-flash-lite: `H 2/1/1 D 1/4/1` — deviation 10
- openai/gpt-5.4-mini: `H 3/0/1 D 1/4/0` — deviation 11
- openai/gpt-5.4-nano: `H 3/1/0 D 1/3/0` — deviation 8

### United States v. Zolotarjovs — 1:24-cr-00076 (ohsd) #69060414

- truth: `H 0/10/0 D 0/1/0`
- prod (live): `H 0/9/1 D 0/1/0` — deviation 2
- anthropic/claude-haiku-4-5: `H 0/8/1 D 0/4/0` — deviation 6
- gemini/gemini-3.1-flash-lite: `H 0/10/1 D 0/1/0` — deviation 1
- openai/gpt-5.4-mini: `H 0/4/1 D 0/2/0` — deviation 8
- openai/gpt-5.4-nano: `H 1/3/1 D 0/2/0` — deviation 10

### Anthropic v. DOW — 26-1049 (cadc) #72380208

- truth: `H 0/1/0 D 1/12/0`
- prod (live): `H 0/1/0 D 2/6/2` — deviation 9
- anthropic/claude-haiku-4-5: `H 0/1/0 D 2/7/1` — deviation 7
- gemini/gemini-3.1-flash-lite: `H 0/1/0 D 1/9/0` — deviation 3
- openai/gpt-5.4-mini: `H 0/1/0 D 1/16/0` — deviation 4
- openai/gpt-5.4-nano: `H 0/1/0 D 1/7/0` — deviation 5

### United States v. Didenko — 1:24-cr-00261 (dcd) #68810724

- truth: `H 0/8/0 D 0/5/0`
- prod (live): `H 0/4/0 D 0/6/0` — deviation 5
- anthropic/claude-haiku-4-5: `H 0/3/0 D 0/5/1` — deviation 6
- gemini/gemini-3.1-flash-lite: `H 0/5/0 D 0/4/0` — deviation 4
- openai/gpt-5.4-mini: `H 1/5/0 D 0/4/4` — deviation 9
- openai/gpt-5.4-nano: `H 0/3/0 D 0/5/0` — deviation 5

### United States v. Akhter — 1:25-cr-00307 (vaed) #71989485

- truth: `H 0/6/0 D 0/6/0`
- prod (live): `H 2/8/1 D 0/8/1` — deviation 8
- anthropic/claude-haiku-4-5: `H 4/5/2 D 0/6/1` — deviation 8
- gemini/gemini-3.1-flash-lite: `H 1/4/1 D 1/8/1` — deviation 8
- openai/gpt-5.4-mini: `H 4/4/1 D 1/5/0` — deviation 9
- openai/gpt-5.4-nano: `H 0/5/1 D 0/5/1` — deviation 4

### United States v. Chapman — 1:24-cr-00220 (dcd) #68534169

- truth: `H 0/5/0 D 0/3/0`
- prod (live): `H 0/5/0 D 0/6/0` — deviation 3
- anthropic/claude-haiku-4-5: `H 0/3/1 D 0/7/0` — deviation 7
- gemini/gemini-3.1-flash-lite: `H 0/4/0 D 0/5/0` — deviation 3
- openai/gpt-5.4-mini: `H 0/3/0 D 0/7/0` — deviation 6
- openai/gpt-5.4-nano: `H 0/4/0 D 1/6/0` — deviation 5

### United States v. Gholinejad — 25-4607 (ca4) #71906511

- truth: `H 0/0/0 D 0/8/0`
- prod (live): `H 0/0/0 D 2/3/0` — deviation 7
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/3/0` — deviation 5
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 1/4/0` — deviation 5
- openai/gpt-5.4-mini: `H 0/0/0 D 1/5/0` — deviation 4
- openai/gpt-5.4-nano: `H 0/0/0 D 2/3/0` — deviation 7

### United States v. Tymoshchuk — 1:23-cr-00324 (nyed) #70029216

- truth: `H 1/4/0 D 0/3/0`
- prod (live): `H 1/4/0 D 1/2/0` — deviation 2
- anthropic/claude-haiku-4-5: `H 1/4/0 D 2/2/0` — deviation 3
- gemini/gemini-3.1-flash-lite: `H 1/5/0 D 1/2/0` — deviation 3
- openai/gpt-5.4-mini: `H 2/4/0 D 1/2/0` — deviation 3
- openai/gpt-5.4-nano: `H 2/1/0 D 1/1/0` — deviation 7

### United States v. Zhenxing Wang — 1:25-cr-10273 (mad) #70678228

- truth: `H 0/5/0 D 0/0/0`
- prod (live): `H 0/4/0 D 0/0/0` — deviation 1
- anthropic/claude-haiku-4-5: `H 0/4/0 D 1/1/0` — deviation 3
- gemini/gemini-3.1-flash-lite: `H 0/3/2 D 2/0/0` — deviation 6
- openai/gpt-5.4-mini: `H 1/3/0 D 1/2/0` — deviation 6
- openai/gpt-5.4-nano: `H 2/3/0 D 0/2/0` — deviation 6

### United States v. Tymoshchuk — 1:23-cr-00324 (nyed) #71300581

- truth: `H 0/2/0 D 0/4/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 6
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 6
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 6
- openai/gpt-5.4-mini: `H 0/0/0 D 0/0/0` — deviation 6
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 6

### Anthropic v. DOW — 26-2011 (ca9) #73136734

- truth: `H 0/1/0 D 0/1/0`
- prod (live): `H 0/0/0 D 1/2/2` — deviation 5
- anthropic/claude-haiku-4-5: `H 0/0/0 D 1/2/2` — deviation 5
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 1/2/2` — deviation 5
- openai/gpt-5.4-mini: `H 0/0/0 D 1/4/0` — deviation 5
- openai/gpt-5.4-nano: `H 0/0/0 D 2/2/0` — deviation 4

### United States v. Martino — 1:26-cr-20065 (flsd) #72389253

- truth: `H 0/2/0 D 1/3/0`
- prod (live): `H 1/2/0 D 1/3/1` — deviation 2
- anthropic/claude-haiku-4-5: `H 1/2/0 D 1/5/1` — deviation 4
- gemini/gemini-3.1-flash-lite: `H 1/2/0 D 1/7/0` — deviation 5
- openai/gpt-5.4-mini: `H 1/2/0 D 1/7/0` — deviation 5
- openai/gpt-5.4-nano: `H 3/1/0 D 1/3/0` — deviation 4

### United States v. Gholinejad — 4:24-cr-00016 (nced) #70378502

- truth: `H 0/2/1 D 0/9/0`
- prod (live): `H 0/3/1 D 0/9/0` — deviation 1
- anthropic/claude-haiku-4-5: `H 1/2/1 D 0/7/2` — deviation 5
- gemini/gemini-3.1-flash-lite: `H 0/3/1 D 0/9/0` — deviation 1
- openai/gpt-5.4-mini: `H 1/3/0 D 0/9/0` — deviation 3
- openai/gpt-5.4-nano: `H 2/2/0 D 1/8/0` — deviation 5

### United States v. Lytvynenko — 3:23-cr-00088 (tnmd) #71820111

- truth: `H 0/2/0 D 0/4/0`
- prod (live): `H 3/2/0 D 1/3/0` — deviation 5
- anthropic/claude-haiku-4-5: `H 3/2/0 D 1/3/0` — deviation 5
- gemini/gemini-3.1-flash-lite: `H 3/2/0 D 1/3/0` — deviation 5
- openai/gpt-5.4-mini: `H 3/2/0 D 1/3/0` — deviation 5
- openai/gpt-5.4-nano: `H 3/2/0 D 1/3/0` — deviation 5

### United States v. Knoot — 26-5455 (ca6) #73388385

- truth: `H 0/0/0 D 2/2/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 4
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 4
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 4
- openai/gpt-5.4-mini: `H 0/0/0 D 0/0/0` — deviation 4
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 4

### United States v. Eileen Wang — 2:26-cr-00186 (cacd) #73323008

- truth: `H 0/3/0 D 0/0/0`
- prod (live): `H 1/1/0 D 0/0/0` — deviation 3
- anthropic/claude-haiku-4-5: `H 1/1/0 D 0/0/0` — deviation 3
- gemini/gemini-3.1-flash-lite: `H 1/1/0 D 0/0/0` — deviation 3
- openai/gpt-5.4-mini: `H 1/0/0 D 0/0/0` — deviation 4
- openai/gpt-5.4-nano: `H 1/0/0 D 0/0/0` — deviation 4

### United States v. Zewei — 4:23-cr-00523 (txsd) #70789744

- truth: `H 0/2/0 D 1/2/0`
- prod (live): `H 2/3/0 D 1/2/0` — deviation 3
- anthropic/claude-haiku-4-5: `H 2/2/0 D 1/2/0` — deviation 2
- gemini/gemini-3.1-flash-lite: `H 2/3/0 D 1/2/0` — deviation 3
- openai/gpt-5.4-mini: `H 3/2/0 D 1/2/0` — deviation 3
- openai/gpt-5.4-nano: `H 2/3/0 D 1/2/0` — deviation 3

### United States v. Tymoshchuk — 1:23-cr-00324 (nyed) #70701403

- truth: `H 0/2/0 D 0/0/0`
- prod (live): `H 1/0/0 D 0/0/0` — deviation 3
- anthropic/claude-haiku-4-5: `H 1/0/0 D 0/0/0` — deviation 3
- gemini/gemini-3.1-flash-lite: `H 1/0/0 D 0/0/0` — deviation 3
- openai/gpt-5.4-mini: `H 1/0/0 D 0/0/0` — deviation 3
- openai/gpt-5.4-nano: `H 0/1/0 D 0/0/0` — deviation 1

### United States v. Schmitz — 1:24-cr-00234 (njd) #73353898

- truth: `H 1/1/0 D 0/0/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 2
- anthropic/claude-haiku-4-5: `H 0/0/0 D 1/0/0` — deviation 3
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 2
- openai/gpt-5.4-mini: `H 0/0/0 D 0/0/0` — deviation 2
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 2

### United States v. Eileen Wang — 2:26-cr-00186 (cacd) #73326420

- truth: `H 0/2/0 D 0/0/0`
- prod (live): `H 0/0/1 D 0/0/0` — deviation 3
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 2
- gemini/gemini-3.1-flash-lite: `H 0/1/0 D 0/0/0` — deviation 1
- openai/gpt-5.4-mini: `H 0/1/0 D 0/0/0` — deviation 1
- openai/gpt-5.4-nano: `H 0/1/0 D 0/0/0` — deviation 1

### United States v. Zheng et al. — 1:26-mj-00315 (gand) #73103748

- truth: `H 0/1/1 D 1/0/0`
- prod (live): `H 0/1/1 D 0/0/0` — deviation 1
- anthropic/claude-haiku-4-5: `H 0/1/1 D 0/0/0` — deviation 1
- gemini/gemini-3.1-flash-lite: `H 0/1/1 D 0/0/0` — deviation 1
- openai/gpt-5.4-mini: `H 1/1/1 D 0/0/0` — deviation 2
- openai/gpt-5.4-nano: `H 1/0/1 D 0/0/0` — deviation 3

### United States v. Kejia "Tony" Wang — 1:25-cr-10274 (mad) #70691920

- truth: `H 0/2/0 D 0/0/0`
- prod (live): `H 0/2/0 D 0/0/0` — deviation 0
- anthropic/claude-haiku-4-5: `H 0/2/0 D 1/0/0` — deviation 1
- gemini/gemini-3.1-flash-lite: `H 0/2/0 D 1/0/0` — deviation 1
- openai/gpt-5.4-mini: `H 0/2/0 D 0/2/0` — deviation 2
- openai/gpt-5.4-nano: `H 0/2/0 D 0/1/0` — deviation 1

### United States v. Schmitz — 1:24-cr-00234 (njd) #73292090

- truth: `H 1/1/0 D 0/0/0`
- prod (live): `H 0/1/0 D 0/0/0` — deviation 1
- anthropic/claude-haiku-4-5: `H 0/1/0 D 0/0/0` — deviation 1
- gemini/gemini-3.1-flash-lite: `H 0/1/0 D 1/0/0` — deviation 2
- openai/gpt-5.4-mini: `H 0/1/0 D 1/0/0` — deviation 2
- openai/gpt-5.4-nano: `H 0/1/0 D 0/0/0` — deviation 1

### United States v. Zheng et al. — 3:26-mj-70297 (cand) #72532372

- truth: `H 0/4/0 D 0/0/0`
- prod (live): `H 0/5/0 D 0/0/0` — deviation 1
- anthropic/claude-haiku-4-5: `H 0/5/0 D 0/0/0` — deviation 1
- gemini/gemini-3.1-flash-lite: `H 0/6/0 D 0/0/0` — deviation 2
- openai/gpt-5.4-mini: `H 0/3/0 D 0/0/0` — deviation 1
- openai/gpt-5.4-nano: `H 0/3/0 D 0/0/0` — deviation 1

### United States v. Volkov — 1:25-cr-00211 (insd) #71842241

- truth: `H 0/3/0 D 1/1/0`
- prod (live): `H 0/3/0 D 1/2/0` — deviation 1
- anthropic/claude-haiku-4-5: `H 0/3/0 D 1/2/0` — deviation 1
- gemini/gemini-3.1-flash-lite: `H 0/3/0 D 1/2/0` — deviation 1
- openai/gpt-5.4-mini: `H 0/3/0 D 1/2/0` — deviation 1
- openai/gpt-5.4-nano: `H 0/3/0 D 1/2/0` — deviation 1

### United States v. Stryzhak — 1:25-cr-00381 (nyed) #72011504

- truth: `H 0/1/0 D 0/0/0`
- prod (live): `H 1/1/0 D 0/0/0` — deviation 1
- anthropic/claude-haiku-4-5: `H 1/1/0 D 0/0/0` — deviation 1
- gemini/gemini-3.1-flash-lite: `H 1/1/0 D 0/0/0` — deviation 1
- openai/gpt-5.4-mini: `H 1/1/0 D 0/0/0` — deviation 1
- openai/gpt-5.4-nano: `H 1/1/0 D 0/0/0` — deviation 1

### United States v. Zheng et al. — 1:26-mj-00316 (gand) #72798154

- truth: `H 0/0/0 D 1/0/0`
- prod (live): `H 0/0/0 D 1/0/0` — deviation 0
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 1
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 1/0/0` — deviation 0
- openai/gpt-5.4-mini: `H 0/0/0 D 1/0/0` — deviation 0
- openai/gpt-5.4-nano: `H 0/0/0 D 1/0/0` — deviation 0

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
