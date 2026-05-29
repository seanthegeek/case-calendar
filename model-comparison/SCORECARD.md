# Provider accuracy vs human ground truth

Scored **46** of 46 CourtListener records (those with all six counts filled in). Lower deviation = closer to the human-read truth. Deviation is the sum of |model count − your count| over the six status categories.

## Totals (lower is better)

| model | total deviation | H sched | H held | H canc | D pend | D met/pass | D canc |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| gemini/gemini-3.1-flash-lite | **392** | 25 | 75 | 73 | 16 | 191 | 12 |
| anthropic/claude-haiku-4-5 | **413** | 28 | 84 | 63 | 21 | 203 | 14 |
| openai/gpt-5.4-nano | **466** | 97 | 113 | 29 | 57 | 165 | 5 |
| prod (live) | **470** | 29 | 81 | 38 | 15 | 301 | 6 |
| openai/gpt-5.4-mini | **471** | 49 | 91 | 60 | 31 | 231 | 9 |

### Note on the omitted gemini-3.5-flash column

The Gemini extraction-candidate column (`gemini/gemini-3.5-flash`) was dropped from the comparison due to **long processing times** — its single-column rebuild rate (~14 LLM calls/min in this environment) projected to roughly another 100 minutes of wall-clock to finish, on top of an already-aborted first attempt; the throughput cost of carrying it across future re-runs wasn't justified given the existing default `gemini-3.1-flash-lite` column's strong showing on the same Gemini provider. The previous-pipeline measurement ($11.92, against the old criminal-off design) remains in `README.md` as historical context, explicitly flagged as not re-measured.

## Qualitative event-set diffs (Gemini vs. Anthropic)

The 21-point score gap between Gemini-Flash-Lite (392) and Anthropic-Haiku (413) is a net-deviation number, not a clean strength differential — both models catch real events the other drops, and both over-extract in different places. To see what the gap is made of, we bucketed each provider's events per docket by `(type, status, date)` and surfaced the buckets unique to one side. Gemini had **169 such unique buckets**, Anthropic **129**; below are the most telling examples by category.

### Events Gemini-Flash-Lite caught that Anthropic-Haiku missed

**The new transcript rules.** *US v. McGonigal* (`1:23-cr-00016`, S.D.N.Y.) — Gemini caught all four transcript events, Anthropic caught zero:

| date | type | sig | event |
| --- | --- | --- | --- |
| 2023-06-25 | deadline (passed) | **major** | Public release of 3/8/2023 Conference transcript |
| 2023-08-25 | deadline (passed) | minor | Notice of Intent to Request Redaction — 8/10 Plea Transcript |
| 2024-01-19 | deadline (passed) | minor | Redaction Request — Sentencing Transcript |
| 2024-03-28 | deadline (passed) | **major** | Public release of sentencing transcript |

This is exactly the pattern baked into `SYSTEM_PROMPT` in 0.8.0: redaction windows → minor, public-release → major. Gemini is executing the rule; Anthropic silently drops the entire class.

**Motion-in-limine briefing chains.** *US v. Knoot* (`3:24-cr-00151`, M.D. Tenn.) — Gemini tracked the full response/reply chain plus the actual hearing; Anthropic missed every step:

| date | event |
| --- | --- |
| 2025-07-17 | Knoot's response to Government's Motion in Limine (major deadline) |
| 2025-07-21 | Government's reply to Motion in Limine (major deadline) |
| **2025-07-29** | **Motion Hearing (Motion to Suppress) — held (major hearing)** |
| 2025-07-31 | Defendant's expert witness disclosure (major deadline) |
| 2025-08-08 | Government's response to expert disclosure (major deadline) |

**Substantive cybercrime arcs.** *US v. Ding* (`3:24-cr-00141`, N.D. Cal.): Government's response to Motion to Suppress, Evidentiary Hearing, Motion to Suppress Hearing, and the 2025-10-10 **Jury Selection/Trial — held**, all caught by Gemini. *US v. Akhter* (`1:25-cr-00307`, E.D. Va.): Initial Appearance for Muneeb, the cancelled-arraignment chain, pretrial-motions deadlines, **Change of Plea — Muneeb Akhter (2026-04-15, held)**, the 2026-05-04 **Jury Trial — held**, and the 2026-08-12 **Sentencing — Muneeb Akhter (scheduled future)** — all Gemini-only.

### Events Anthropic-Haiku caught that Gemini-Flash-Lite missed

**Speedy Trial Act stipulations.** *US v. Ding* — three speedy-trial-time exclusions Anthropic flagged that Gemini dropped:

| date | event |
| --- | --- |
| 2024-12-13 | Stipulation Excluding Speedy Trial Time |
| 2025-02-12 | Stipulation Excluding Speedy Trial Time — through 2/10/2025 |
| 2025-06-05 | Stipulation Excluding Speedy Trial Time |

These are real court orders excluding time from the Speedy Trial Act clock — substantive in federal criminal practice.

**Pre-Sentence Investigation Report deadlines.** Anthropic caught the **PSIR deadline for Muneeb Akhter** (major, met). Gemini doesn't carry a PSIR entry. PSIR is a substantive pre-sentencing event.

**The DOW preliminary-injunction hearing.** *Anthropic v. DOW* (`3:26-cv-01996`, N.D. Cal.) — Anthropic put the substantive moment of the case on the calendar; Gemini didn't:

| date | event |
| --- | --- |
| **2026-03-13** | **Hearing on Motion for TRO / Preliminary Injunction — held (major)** |
| 2026-04-22 | Joint Stipulation and Proposed Order Setting Case Schedule (major) |
| 2026-05-23 | Certified Administrative Record (major) |

**Jury-process and CIPA filings.** Akhter: Jury Questionnaire Objections, the 2026-04-20 Hearing on Disputed Jury Instructions (held), the 2026-04-29 Final Pretrial Conference (held). *US v. Ashtor* (`1:25-cr-20021`, S.D. Fla.): the CIPA-pretrial-conference response deadline. McGonigal: the classified-information letter, classified summary of sentencing submission, and the **surrender for service of sentence** deadline. These are first-class criminal-case events Gemini drops.

**Multi-defendant per-name disambiguation.** On Akhter, Anthropic distinguishes per-defendant ("Initial Appearance - Muneeb Akhter (held)" vs "Initial Appearance - Sohaib Akhter (cancelled)") where Gemini sometimes collapses them. Some of these "Anthropic-only" buckets are real distinctions; others are the same event named differently. The score aggregates over this.

### Caveats

- **Bucketing by `(docket, type, status, date)` is fuzzy.** Some "unique" buckets are the same event represented with slightly different keys or dates ±1 day; the Akhter "Initial Appearance" rows are the canonical example.
- **"Unique to a model" ≠ "correct".** Both models hallucinate events that don't exist on the actual docket, and both miss real events. The score measures net match to the human's count, not the absolute set of real events. A "unique to Gemini" event might be a hallucination; a "unique to Anthropic" event might be a real event Gemini wrongly dropped — or the inverse. The qualitative diffs above just suggest the kinds of events each model is biased toward catching.

## Per-docket detail

Truth vs each model. Format: `H scheduled/held/cancelled  D pending/met-or-passed/cancelled`.

### United States v. Ding — 3:24-cr-00141 (cand) #68317014

- truth: `H 0/29/0 D 2/59/0`
- prod (live): `H 3/27/8 D 0/0/0` — deviation 74
- anthropic/claude-haiku-4-5: `H 4/41/13 D 8/86/0` — deviation 62
- gemini/gemini-3.1-flash-lite: `H 2/37/9 D 4/79/0` — deviation 41
- openai/gpt-5.4-mini: `H 14/20/14 D 8/99/1` — deviation 84
- openai/gpt-5.4-nano: `H 32/20/5 D 19/40/0` — deviation 82

### United States v. Wei — 3:23-cr-01471 (casd) #67661286

- truth: `H 0/23/0 D 0/74/0`
- prod (live): `H 0/16/0 D 0/0/0` — deviation 81
- anthropic/claude-haiku-4-5: `H 0/16/0 D 0/15/1` — deviation 67
- gemini/gemini-3.1-flash-lite: `H 0/18/4 D 0/15/3` — deviation 71
- openai/gpt-5.4-mini: `H 1/10/0 D 0/23/2` — deviation 67
- openai/gpt-5.4-nano: `H 7/10/3 D 2/18/0` — deviation 81

### United States v. Wei — 3:23-cr-01471 (casd) #67661185

- truth: `H 0/21/0 D 0/19/0`
- prod (live): `H 3/2/4 D 0/0/0` — deviation 45
- anthropic/claude-haiku-4-5: `H 4/3/3 D 0/3/0` — deviation 41
- gemini/gemini-3.1-flash-lite: `H 1/4/17 D 0/15/0` — deviation 39
- openai/gpt-5.4-mini: `H 1/2/5 D 0/9/0` — deviation 35
- openai/gpt-5.4-nano: `H 7/1/0 D 2/22/0` — deviation 32

### United States v. McGonigal — 1:23-cr-00016 (nysd) #66749883

- truth: `H 0/4/0 D 0/26/0`
- prod (live): `H 6/5/1 D 0/0/0` — deviation 34
- anthropic/claude-haiku-4-5: `H 1/8/6 D 0/34/0` — deviation 19
- gemini/gemini-3.1-flash-lite: `H 0/6/5 D 0/43/0` — deviation 24
- openai/gpt-5.4-mini: `H 5/4/0 D 0/48/0` — deviation 27
- openai/gpt-5.4-nano: `H 5/5/1 D 2/33/0` — deviation 16

### United States v. Akhter — 1:25-cr-00307 (vaed) #73333500

- truth: `H 0/15/0 D 0/14/0`
- prod (live): `H 1/5/4 D 0/0/0` — deviation 29
- anthropic/claude-haiku-4-5: `H 2/7/7 D 3/4/3` — deviation 33
- gemini/gemini-3.1-flash-lite: `H 1/6/9 D 3/10/1` — deviation 27
- openai/gpt-5.4-mini: `H 0/7/4 D 7/8/1` — deviation 26
- openai/gpt-5.4-nano: `H 1/3/3 D 7/5/0` — deviation 32

### United States v. Knoot — 3:24-cr-00151 (tnmd) #69026861

- truth: `H 0/11/0 D 0/4/0`
- prod (live): `H 0/12/1 D 0/0/0` — deviation 6
- anthropic/claude-haiku-4-5: `H 0/11/2 D 0/14/0` — deviation 12
- gemini/gemini-3.1-flash-lite: `H 0/11/2 D 0/15/0` — deviation 13
- openai/gpt-5.4-mini: `H 1/11/6 D 2/23/1` — deviation 29
- openai/gpt-5.4-nano: `H 5/7/0 D 0/14/1` — deviation 20

### United States v. Akhter — 1:25-cr-00307 (vaed) #73320754

- truth: `H 0/7/0 D 0/7/0`
- prod (live): `H 1/1/1 D 0/0/0` — deviation 15
- anthropic/claude-haiku-4-5: `H 0/3/11 D 1/3/3` — deviation 23
- gemini/gemini-3.1-flash-lite: `H 2/3/2 D 0/0/2` — deviation 17
- openai/gpt-5.4-mini: `H 0/4/9 D 2/7/2` — deviation 16
- openai/gpt-5.4-nano: `H 3/1/2 D 2/6/1` — deviation 15

### United States v. Gallyamov — 2:25-cv-04631 (cacd) #70341311

- truth: `H 0/6/0 D 0/2/0`
- prod (live): `H 0/5/1 D 1/12/0` — deviation 13
- anthropic/claude-haiku-4-5: `H 0/6/0 D 0/10/0` — deviation 8
- gemini/gemini-3.1-flash-lite: `H 0/6/0 D 0/9/0` — deviation 7
- openai/gpt-5.4-mini: `H 3/3/0 D 0/11/0` — deviation 15
- openai/gpt-5.4-nano: `H 3/1/0 D 1/11/0` — deviation 18

### United States v. Ashtor — 1:25-cr-20021 (flsd) #69570297

- truth: `H 0/5/1 D 0/13/0`
- prod (live): `H 0/6/4 D 0/0/0` — deviation 17
- anthropic/claude-haiku-4-5: `H 0/5/7 D 0/9/1` — deviation 11
- gemini/gemini-3.1-flash-lite: `H 0/5/5 D 1/6/1` — deviation 13
- openai/gpt-5.4-mini: `H 1/5/4 D 1/18/0` — deviation 10
- openai/gpt-5.4-nano: `H 4/4/3 D 1/11/0` — deviation 10

### Anthropic v. DOW — 3:26-cv-01996 (cand) #72379655

- truth: `H 0/2/0 D 7/22/0`
- prod (live): `H 1/2/1 D 10/28/1` — deviation 12
- anthropic/claude-haiku-4-5: `H 1/3/0 D 5/29/1` — deviation 12
- gemini/gemini-3.1-flash-lite: `H 1/2/0 D 6/28/1` — deviation 9
- openai/gpt-5.4-mini: `H 1/2/3 D 8/32/0` — deviation 15
- openai/gpt-5.4-nano: `H 1/3/0 D 11/24/1` — deviation 9

### United States v. Didenko — 1:24-cr-00261 (dcd) #68810897

- truth: `H 0/6/0 D 0/5/0`
- prod (live): `H 0/0/1 D 0/0/0` — deviation 12
- anthropic/claude-haiku-4-5: `H 0/1/2 D 0/2/0` — deviation 10
- gemini/gemini-3.1-flash-lite: `H 0/0/3 D 0/0/0` — deviation 14
- openai/gpt-5.4-mini: `H 0/1/2 D 0/7/0` — deviation 9
- openai/gpt-5.4-nano: `H 2/0/0 D 0/0/0` — deviation 13

### United States v. Gholinejad — 4:24-cr-00016 (nced) #70378502

- truth: `H 0/2/1 D 0/9/0`
- prod (live): `H 0/3/1 D 0/0/0` — deviation 10
- anthropic/claude-haiku-4-5: `H 1/2/1 D 0/6/0` — deviation 4
- gemini/gemini-3.1-flash-lite: `H 0/3/1 D 0/6/0` — deviation 4
- openai/gpt-5.4-mini: `H 0/2/1 D 0/12/0` — deviation 3
- openai/gpt-5.4-nano: `H 3/2/0 D 5/5/0` — deviation 13

### United States v. Akhter — 1:25-cr-00307 (vaed) #71989485

- truth: `H 0/6/0 D 0/6/0`
- prod (live): `H 0/7/5 D 0/0/0` — deviation 12
- anthropic/claude-haiku-4-5: `H 0/6/4 D 0/6/0` — deviation 4
- gemini/gemini-3.1-flash-lite: `H 2/5/5 D 0/8/1` — deviation 11
- openai/gpt-5.4-mini: `H 4/3/2 D 1/8/0` — deviation 12
- openai/gpt-5.4-nano: `H 3/5/2 D 1/9/1` — deviation 11

### United States v. Gholinejad — 4:24-cr-00016 (nced) #70402649

- truth: `H 0/2/1 D 0/9/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 12
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 12
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 12
- openai/gpt-5.4-mini: `H 0/0/0 D 0/0/0` — deviation 12
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 12

### United States v. Moucka — 2:24-cr-00180 (wawd) #69362701

- truth: `H 0/2/0 D 0/0/0`
- prod (live): `H 1/1/2 D 0/0/0` — deviation 4
- anthropic/claude-haiku-4-5: `H 2/1/1 D 1/3/1` — deviation 9
- gemini/gemini-3.1-flash-lite: `H 2/1/2 D 1/4/1` — deviation 11
- openai/gpt-5.4-mini: `H 2/2/1 D 1/4/0` — deviation 8
- openai/gpt-5.4-nano: `H 1/1/1 D 1/3/1` — deviation 8

### Anthropic v. DOW — 26-1049 (cadc) #72380208

- truth: `H 0/1/0 D 1/12/0`
- prod (live): `H 0/1/0 D 2/6/3` — deviation 10
- anthropic/claude-haiku-4-5: `H 0/1/0 D 2/5/1` — deviation 9
- gemini/gemini-3.1-flash-lite: `H 0/1/0 D 1/9/0` — deviation 3
- openai/gpt-5.4-mini: `H 0/1/0 D 1/16/0` — deviation 4
- openai/gpt-5.4-nano: `H 0/1/0 D 1/12/0` — deviation 0

### United States v. Zolotarjovs — 1:24-cr-00076 (ohsd) #69060414

- truth: `H 0/10/0 D 0/1/0`
- prod (live): `H 0/9/2 D 0/0/0` — deviation 4
- anthropic/claude-haiku-4-5: `H 0/8/2 D 0/3/0` — deviation 6
- gemini/gemini-3.1-flash-lite: `H 0/10/2 D 0/1/0` — deviation 2
- openai/gpt-5.4-mini: `H 1/6/2 D 0/2/0` — deviation 8
- openai/gpt-5.4-nano: `H 1/4/2 D 0/2/0` — deviation 10

### United States v. Didenko — 1:24-cr-00261 (dcd) #68810724

- truth: `H 0/8/0 D 0/5/0`
- prod (live): `H 0/5/0 D 0/0/0` — deviation 8
- anthropic/claude-haiku-4-5: `H 0/5/0 D 0/5/0` — deviation 3
- gemini/gemini-3.1-flash-lite: `H 0/6/0 D 0/5/0` — deviation 2
- openai/gpt-5.4-mini: `H 0/5/0 D 1/9/1` — deviation 9
- openai/gpt-5.4-nano: `H 1/4/0 D 0/5/0` — deviation 5

### United States v. Chapman — 1:24-cr-00220 (dcd) #68534169

- truth: `H 0/5/0 D 0/3/0`
- prod (live): `H 0/4/0 D 0/0/0` — deviation 4
- anthropic/claude-haiku-4-5: `H 0/4/0 D 0/7/0` — deviation 5
- gemini/gemini-3.1-flash-lite: `H 0/5/0 D 0/6/0` — deviation 3
- openai/gpt-5.4-mini: `H 1/3/0 D 0/6/0` — deviation 6
- openai/gpt-5.4-nano: `H 2/3/0 D 1/6/0` — deviation 8

### United States v. Tymoshchuk — 1:23-cr-00324 (nyed) #70029216

- truth: `H 1/4/0 D 0/3/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 8
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 8
- gemini/gemini-3.1-flash-lite: `H 0/1/0 D 0/0/0` — deviation 7
- openai/gpt-5.4-mini: `H 1/1/0 D 0/0/0` — deviation 6
- openai/gpt-5.4-nano: `H 1/0/0 D 0/0/0` — deviation 7

### United States v. Zhenxing Wang — 1:25-cr-10273 (mad) #70678228

- truth: `H 0/5/0 D 0/0/0`
- prod (live): `H 0/4/0 D 0/0/0` — deviation 1
- anthropic/claude-haiku-4-5: `H 0/4/1 D 0/1/0` — deviation 3
- gemini/gemini-3.1-flash-lite: `H 0/3/2 D 0/0/0` — deviation 4
- openai/gpt-5.4-mini: `H 0/4/1 D 1/2/0` — deviation 5
- openai/gpt-5.4-nano: `H 2/2/1 D 0/1/0` — deviation 7

### United States v. Martino — 1:26-cr-20065 (flsd) #72389253

- truth: `H 0/2/0 D 1/3/0`
- prod (live): `H 1/2/0 D 0/0/0` — deviation 5
- anthropic/claude-haiku-4-5: `H 1/2/0 D 0/5/0` — deviation 4
- gemini/gemini-3.1-flash-lite: `H 1/3/0 D 1/7/0` — deviation 6
- openai/gpt-5.4-mini: `H 1/2/1 D 2/7/0` — deviation 7
- openai/gpt-5.4-nano: `H 1/2/0 D 3/6/0` — deviation 6

### United States v. Gholinejad — 25-4607 (ca4) #71906511

- truth: `H 0/0/0 D 0/8/0`
- prod (live): `H 0/0/0 D 1/3/0` — deviation 6
- anthropic/claude-haiku-4-5: `H 0/0/0 D 1/3/0` — deviation 6
- gemini/gemini-3.1-flash-lite: `H 1/0/0 D 1/3/0` — deviation 7
- openai/gpt-5.4-mini: `H 0/0/0 D 1/3/0` — deviation 6
- openai/gpt-5.4-nano: `H 0/0/0 D 1/3/0` — deviation 6

### United States v. Lytvynenko — 3:23-cr-00088 (tnmd) #71820111

- truth: `H 0/2/0 D 0/4/0`
- prod (live): `H 3/2/0 D 0/0/0` — deviation 7
- anthropic/claude-haiku-4-5: `H 3/2/1 D 1/3/0` — deviation 6
- gemini/gemini-3.1-flash-lite: `H 3/2/2 D 1/3/0` — deviation 7
- openai/gpt-5.4-mini: `H 4/2/1 D 1/3/0` — deviation 7
- openai/gpt-5.4-nano: `H 3/2/1 D 1/3/0` — deviation 6

### Anthropic v. DOW — 26-2011 (ca9) #73136734

- truth: `H 0/1/0 D 0/1/0`
- prod (live): `H 0/0/0 D 1/2/2` — deviation 5
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/1/3` — deviation 4
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 1/2/2` — deviation 5
- openai/gpt-5.4-mini: `H 0/0/0 D 0/5/1` — deviation 6
- openai/gpt-5.4-nano: `H 0/0/0 D 3/1/0` — deviation 4

### United States v. Zewei — 4:23-cr-00523 (txsd) #70789744

- truth: `H 0/2/0 D 1/2/0`
- prod (live): `H 2/3/0 D 0/0/0` — deviation 6
- anthropic/claude-haiku-4-5: `H 2/3/0 D 1/2/0` — deviation 3
- gemini/gemini-3.1-flash-lite: `H 2/3/0 D 1/2/0` — deviation 3
- openai/gpt-5.4-mini: `H 2/3/0 D 1/2/0` — deviation 3
- openai/gpt-5.4-nano: `H 2/3/0 D 1/2/0` — deviation 3

### United States v. Tymoshchuk — 1:23-cr-00324 (nyed) #71300581

- truth: `H 0/2/0 D 0/4/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 6
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 6
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 6
- openai/gpt-5.4-mini: `H 0/0/0 D 0/0/0` — deviation 6
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 6

### United States v. Knoot — 26-5455 (ca6) #73388385

- truth: `H 0/0/0 D 2/2/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 4
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 4
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 4
- openai/gpt-5.4-mini: `H 0/0/0 D 0/0/0` — deviation 4
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 4

### United States v. Stryzhak — 1:25-cr-00381 (nyed) #72011504

- truth: `H 0/1/0 D 0/0/0`
- prod (live): `H 1/1/1 D 0/0/0` — deviation 2
- anthropic/claude-haiku-4-5: `H 1/1/2 D 0/0/0` — deviation 3
- gemini/gemini-3.1-flash-lite: `H 1/1/2 D 0/0/0` — deviation 3
- openai/gpt-5.4-mini: `H 2/1/2 D 0/0/0` — deviation 4
- openai/gpt-5.4-nano: `H 2/1/1 D 0/0/0` — deviation 3

### United States v. Eileen Wang — 2:26-cr-00186 (cacd) #73323008

- truth: `H 0/3/0 D 0/0/0`
- prod (live): `H 1/1/0 D 0/0/0` — deviation 3
- anthropic/claude-haiku-4-5: `H 1/1/0 D 0/0/0` — deviation 3
- gemini/gemini-3.1-flash-lite: `H 1/1/0 D 0/0/0` — deviation 3
- openai/gpt-5.4-mini: `H 1/1/1 D 0/0/0` — deviation 4
- openai/gpt-5.4-nano: `H 1/1/0 D 0/0/0` — deviation 3

### United States v. Kejia "Tony" Wang — 1:25-cr-10274 (mad) #70691920

- truth: `H 0/2/0 D 0/0/0`
- prod (live): `H 1/1/0 D 0/0/0` — deviation 2
- anthropic/claude-haiku-4-5: `H 1/1/0 D 0/1/0` — deviation 3
- gemini/gemini-3.1-flash-lite: `H 1/1/0 D 0/0/0` — deviation 2
- openai/gpt-5.4-mini: `H 1/1/0 D 0/1/0` — deviation 3
- openai/gpt-5.4-nano: `H 1/1/0 D 0/0/0` — deviation 2

### United States v. Tymoshchuk — 1:23-cr-00324 (nyed) #70701403

- truth: `H 0/2/0 D 0/0/0`
- prod (live): `H 1/2/0 D 0/0/0` — deviation 1
- anthropic/claude-haiku-4-5: `H 1/2/0 D 0/0/0` — deviation 1
- gemini/gemini-3.1-flash-lite: `H 1/2/1 D 0/0/0` — deviation 2
- openai/gpt-5.4-mini: `H 1/0/0 D 0/0/0` — deviation 3
- openai/gpt-5.4-nano: `H 1/1/0 D 0/0/0` — deviation 2

### United States v. Schmitz — 1:24-cr-00234 (njd) #73292090

- truth: `H 1/1/0 D 0/0/0`
- prod (live): `H 0/1/1 D 0/0/0` — deviation 2
- anthropic/claude-haiku-4-5: `H 0/1/1 D 0/0/0` — deviation 2
- gemini/gemini-3.1-flash-lite: `H 0/1/1 D 1/0/0` — deviation 3
- openai/gpt-5.4-mini: `H 0/1/1 D 1/0/0` — deviation 3
- openai/gpt-5.4-nano: `H 0/1/2 D 0/0/0` — deviation 3

### United States v. Zheng et al. — 1:26-mj-00315 (gand) #73103748

- truth: `H 0/1/1 D 1/0/0`
- prod (live): `H 0/2/0 D 0/0/0` — deviation 3
- anthropic/claude-haiku-4-5: `H 0/1/1 D 0/0/0` — deviation 1
- gemini/gemini-3.1-flash-lite: `H 0/1/1 D 0/0/0` — deviation 1
- openai/gpt-5.4-mini: `H 0/1/2 D 0/0/0` — deviation 2
- openai/gpt-5.4-nano: `H 0/1/2 D 0/0/0` — deviation 2

### United States v. Volkov — 1:25-cr-00211 (insd) #71842241

- truth: `H 0/3/0 D 1/1/0`
- prod (live): `H 0/3/0 D 0/0/0` — deviation 2
- anthropic/claude-haiku-4-5: `H 0/3/0 D 0/1/0` — deviation 1
- gemini/gemini-3.1-flash-lite: `H 0/3/0 D 0/1/0` — deviation 1
- openai/gpt-5.4-mini: `H 0/3/0 D 0/2/0` — deviation 2
- openai/gpt-5.4-nano: `H 0/3/0 D 0/1/0` — deviation 1

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
- anthropic/claude-haiku-4-5: `H 0/5/0 D 0/0/0` — deviation 1
- gemini/gemini-3.1-flash-lite: `H 0/5/0 D 0/0/0` — deviation 1
- openai/gpt-5.4-mini: `H 0/3/0 D 0/0/0` — deviation 1
- openai/gpt-5.4-nano: `H 1/3/0 D 0/0/0` — deviation 2

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

