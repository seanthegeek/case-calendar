# Provider accuracy vs human ground truth

Scored **46** of 46 CourtListener records (those with all six counts filled in). Lower deviation = closer to the human-read truth. Deviation is the sum of |model count − your count| over the six status categories.

## Totals (lower is better)

| model | total deviation | H sched | H held | H canc | D pend | D met/pass | D canc |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| gemini/gemini-3.1-flash-lite | **314** | 24 | 78 | 28 | 35 | 143 | 6 |
| anthropic/claude-haiku-4-5 | **343** | 30 | 83 | 32 | 30 | 143 | 25 |
| openai/gpt-5.4-nano | **367** | 61 | 118 | 17 | 59 | 111 | 1 |
| prod (live) | **371** | 21 | 87 | 23 | 29 | 172 | 39 |
| openai/gpt-5.4-mini | **418** | 64 | 105 | 13 | 55 | 177 | 4 |

## Per-docket detail

Truth vs each model. Format: `H scheduled/held/cancelled  D pending/met-or-passed/cancelled`.

### United States v. Ding — 3:24-cr-00141 (cand) #68317014

- truth: `H 0/29/0 D 2/59/0`
- prod (live): `H 1/39/5 D 10/82/0` — deviation 47
- anthropic/claude-haiku-4-5: `H 2/37/6 D 6/77/0` — deviation 38
- gemini/gemini-3.1-flash-lite: `H 2/35/6 D 4/76/0` — deviation 33
- openai/gpt-5.4-mini: `H 11/17/7 D 9/93/0` — deviation 71
- openai/gpt-5.4-nano: `H 19/18/4 D 19/53/0` — deviation 57

### United States v. Wei — 3:23-cr-01471 (casd) #67661286

- truth: `H 0/23/0 D 0/74/0`
- prod (live): `H 0/16/0 D 7/38/1` — deviation 51
- anthropic/claude-haiku-4-5: `H 0/19/0 D 7/36/1` — deviation 50
- gemini/gemini-3.1-flash-lite: `H 0/23/1 D 17/56/0` — deviation 36
- openai/gpt-5.4-mini: `H 3/9/0 D 14/59/1` — deviation 47
- openai/gpt-5.4-nano: `H 3/12/1 D 14/50/0` — deviation 53

### United States v. Akhter — 1:25-cr-00307 (vaed) #73333500

- truth: `H 0/15/0 D 0/14/0`
- prod (live): `H 1/4/0 D 6/1/7` — deviation 38
- anthropic/claude-haiku-4-5: `H 3/5/2 D 6/6/5` — deviation 34
- gemini/gemini-3.1-flash-lite: `H 1/4/2 D 6/9/3` — deviation 28
- openai/gpt-5.4-mini: `H 2/3/1 D 8/11/0` — deviation 26
- openai/gpt-5.4-nano: `H 0/1/1 D 7/11/0` — deviation 25

### United States v. Wei — 3:23-cr-01471 (casd) #67661185

- truth: `H 0/21/0 D 0/19/0`
- prod (live): `H 0/7/1 D 0/17/1` — deviation 18
- anthropic/claude-haiku-4-5: `H 2/3/4 D 0/17/0` — deviation 26
- gemini/gemini-3.1-flash-lite: `H 1/1/2 D 0/17/0` — deviation 25
- openai/gpt-5.4-mini: `H 2/4/0 D 1/19/0` — deviation 20
- openai/gpt-5.4-nano: `H 0/1/0 D 3/10/0` — deviation 32

### United States v. McGonigal — 1:23-cr-00016 (nysd) #66749883

- truth: `H 0/4/0 D 0/26/0`
- prod (live): `H 4/7/1 D 0/32/0` — deviation 14
- anthropic/claude-haiku-4-5: `H 5/6/2 D 0/28/1` — deviation 12
- gemini/gemini-3.1-flash-lite: `H 3/8/2 D 0/45/0` — deviation 28
- openai/gpt-5.4-mini: `H 8/3/0 D 0/44/0` — deviation 27
- openai/gpt-5.4-nano: `H 5/4/1 D 2/26/0` — deviation 8

### United States v. Knoot — 3:24-cr-00151 (tnmd) #69026861

- truth: `H 0/11/0 D 0/4/0`
- prod (live): `H 0/12/0 D 0/8/6` — deviation 11
- anthropic/claude-haiku-4-5: `H 0/13/1 D 0/14/2` — deviation 15
- gemini/gemini-3.1-flash-lite: `H 0/10/3 D 0/15/0` — deviation 15
- openai/gpt-5.4-mini: `H 5/11/0 D 5/20/1` — deviation 27
- openai/gpt-5.4-nano: `H 4/5/2 D 1/13/1` — deviation 23

### United States v. Akhter — 1:25-cr-00307 (vaed) #73320754

- truth: `H 0/7/0 D 0/7/0`
- prod (live): `H 2/1/2 D 0/0/8` — deviation 25
- anthropic/claude-haiku-4-5: `H 4/2/2 D 0/5/4` — deviation 17
- gemini/gemini-3.1-flash-lite: `H 1/2/3 D 0/8/0` — deviation 10
- openai/gpt-5.4-mini: `H 3/2/1 D 0/5/0` — deviation 11
- openai/gpt-5.4-nano: `H 2/1/1 D 0/2/0` — deviation 14

### United States v. Gallyamov — 2:25-cv-04631 (cacd) #70341311

- truth: `H 0/6/0 D 0/2/0`
- prod (live): `H 0/7/2 D 0/22/1` — deviation 24
- anthropic/claude-haiku-4-5: `H 0/3/3 D 0/13/0` — deviation 17
- gemini/gemini-3.1-flash-lite: `H 0/6/1 D 0/17/0` — deviation 16
- openai/gpt-5.4-mini: `H 2/1/0 D 0/12/0` — deviation 17
- openai/gpt-5.4-nano: `H 2/2/0 D 0/5/0` — deviation 9

### Anthropic v. DOW — 3:26-cv-01996 (cand) #72379655

- truth: `H 0/2/0 D 7/22/0`
- prod (live): `H 1/3/0 D 7/27/0` — deviation 7
- anthropic/claude-haiku-4-5: `H 1/3/1 D 4/27/2` — deviation 13
- gemini/gemini-3.1-flash-lite: `H 1/2/0 D 7/31/0` — deviation 10
- openai/gpt-5.4-mini: `H 1/3/0 D 8/39/0` — deviation 20
- openai/gpt-5.4-nano: `H 1/2/0 D 5/21/0` — deviation 4

### United States v. Ashtor — 1:25-cr-20021 (flsd) #69570297

- truth: `H 0/5/1 D 0/13/0`
- prod (live): `H 0/5/6 D 0/4/3` — deviation 17
- anthropic/claude-haiku-4-5: `H 0/6/5 D 0/7/3` — deviation 14
- gemini/gemini-3.1-flash-lite: `H 0/6/4 D 0/9/0` — deviation 8
- openai/gpt-5.4-mini: `H 2/4/2 D 1/19/1` — deviation 12
- openai/gpt-5.4-nano: `H 1/5/2 D 2/7/0` — deviation 10

### United States v. Akhter — 1:25-cr-00307 (vaed) #71989485

- truth: `H 0/6/0 D 0/6/0`
- prod (live): `H 1/8/2 D 0/8/2` — deviation 9
- anthropic/claude-haiku-4-5: `H 1/8/2 D 0/8/1` — deviation 8
- gemini/gemini-3.1-flash-lite: `H 1/5/1 D 0/6/0` — deviation 3
- openai/gpt-5.4-mini: `H 4/5/0 D 1/14/0` — deviation 14
- openai/gpt-5.4-nano: `H 5/4/1 D 0/8/0` — deviation 10

### United States v. Gholinejad — 4:24-cr-00016 (nced) #70402649

- truth: `H 0/2/1 D 0/9/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 12
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 12
- gemini/gemini-3.1-flash-lite: `H 1/0/1 D 0/0/0` — deviation 12
- openai/gpt-5.4-mini: `H 1/0/0 D 0/0/0` — deviation 13
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 12

### United States v. Didenko — 1:24-cr-00261 (dcd) #68810897

- truth: `H 0/6/0 D 0/5/0`
- prod (live): `H 0/2/0 D 0/2/3` — deviation 10
- anthropic/claude-haiku-4-5: `H 0/1/0 D 0/3/0` — deviation 7
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 11
- openai/gpt-5.4-mini: `H 0/1/0 D 1/4/0` — deviation 7
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 11

### Anthropic v. DOW — 26-1049 (cadc) #72380208

- truth: `H 0/1/0 D 1/12/0`
- prod (live): `H 0/1/0 D 2/5/2` — deviation 10
- anthropic/claude-haiku-4-5: `H 0/1/0 D 2/6/2` — deviation 9
- gemini/gemini-3.1-flash-lite: `H 0/1/0 D 1/9/0` — deviation 3
- openai/gpt-5.4-mini: `H 0/1/0 D 1/15/0` — deviation 3
- openai/gpt-5.4-nano: `H 0/1/0 D 1/9/0` — deviation 3

### United States v. Moucka — 2:24-cr-00180 (wawd) #69362701

- truth: `H 0/2/0 D 0/0/0`
- prod (live): `H 1/1/2 D 1/3/1` — deviation 9
- anthropic/claude-haiku-4-5: `H 1/1/2 D 1/3/1` — deviation 9
- gemini/gemini-3.1-flash-lite: `H 2/1/1 D 1/4/1` — deviation 10
- openai/gpt-5.4-mini: `H 2/1/1 D 2/4/0` — deviation 10
- openai/gpt-5.4-nano: `H 2/1/1 D 1/4/0` — deviation 9

### United States v. Chapman — 1:24-cr-00220 (dcd) #68534169

- truth: `H 0/5/0 D 0/3/0`
- prod (live): `H 0/5/0 D 0/8/0` — deviation 5
- anthropic/claude-haiku-4-5: `H 0/5/0 D 0/6/0` — deviation 3
- gemini/gemini-3.1-flash-lite: `H 0/6/0 D 0/6/0` — deviation 4
- openai/gpt-5.4-mini: `H 1/2/0 D 1/8/0` — deviation 10
- openai/gpt-5.4-nano: `H 1/1/1 D 0/6/0` — deviation 9

### United States v. Zolotarjovs — 1:24-cr-00076 (ohsd) #69060414

- truth: `H 0/10/0 D 0/1/0`
- prod (live): `H 0/9/1 D 0/3/0` — deviation 4
- anthropic/claude-haiku-4-5: `H 0/9/1 D 0/1/0` — deviation 2
- gemini/gemini-3.1-flash-lite: `H 0/10/1 D 0/1/0` — deviation 1
- openai/gpt-5.4-mini: `H 0/5/1 D 1/2/0` — deviation 8
- openai/gpt-5.4-nano: `H 0/5/1 D 1/1/0` — deviation 7

### United States v. Gholinejad — 25-4607 (ca4) #71906511

- truth: `H 0/0/0 D 0/8/0`
- prod (live): `H 0/0/0 D 0/6/0` — deviation 2
- anthropic/claude-haiku-4-5: `H 0/0/0 D 2/3/0` — deviation 7
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 1/4/0` — deviation 5
- openai/gpt-5.4-mini: `H 0/0/0 D 1/4/0` — deviation 5
- openai/gpt-5.4-nano: `H 0/0/0 D 1/3/0` — deviation 6

### Anthropic v. DOW — 26-2011 (ca9) #73136734

- truth: `H 0/1/0 D 0/1/0`
- prod (live): `H 0/0/0 D 1/2/2` — deviation 5
- anthropic/claude-haiku-4-5: `H 0/0/0 D 1/2/2` — deviation 5
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 1/2/2` — deviation 5
- openai/gpt-5.4-mini: `H 0/0/0 D 1/4/1` — deviation 6
- openai/gpt-5.4-nano: `H 0/0/0 D 1/4/0` — deviation 5

### United States v. Zhenxing Wang — 1:25-cr-10273 (mad) #70678228

- truth: `H 0/5/0 D 0/0/0`
- prod (live): `H 0/3/0 D 0/1/0` — deviation 3
- anthropic/claude-haiku-4-5: `H 0/4/0 D 0/0/0` — deviation 1
- gemini/gemini-3.1-flash-lite: `H 0/3/1 D 0/0/0` — deviation 3
- openai/gpt-5.4-mini: `H 2/3/0 D 1/1/0` — deviation 6
- openai/gpt-5.4-nano: `H 1/2/1 D 0/0/0` — deviation 5

### United States v. Didenko — 1:24-cr-00261 (dcd) #68810724

- truth: `H 0/8/0 D 0/5/0`
- prod (live): `H 0/3/0 D 0/5/1` — deviation 6
- anthropic/claude-haiku-4-5: `H 0/4/0 D 0/6/0` — deviation 5
- gemini/gemini-3.1-flash-lite: `H 0/6/0 D 0/5/0` — deviation 2
- openai/gpt-5.4-mini: `H 1/4/0 D 1/5/0` — deviation 6
- openai/gpt-5.4-nano: `H 1/3/0 D 0/5/0` — deviation 6

### United States v. Martino — 1:26-cr-20065 (flsd) #72389253

- truth: `H 0/2/0 D 1/3/0`
- prod (live): `H 1/2/0 D 1/5/1` — deviation 4
- anthropic/claude-haiku-4-5: `H 1/2/0 D 1/3/1` — deviation 2
- gemini/gemini-3.1-flash-lite: `H 1/3/0 D 1/7/0` — deviation 6
- openai/gpt-5.4-mini: `H 1/2/0 D 1/7/0` — deviation 5
- openai/gpt-5.4-nano: `H 2/1/0 D 1/4/0` — deviation 4

### United States v. Tymoshchuk — 1:23-cr-00324 (nyed) #70029216

- truth: `H 1/4/0 D 0/3/0`
- prod (live): `H 1/1/0 D 1/2/0` — deviation 5
- anthropic/claude-haiku-4-5: `H 1/4/0 D 1/2/0` — deviation 2
- gemini/gemini-3.1-flash-lite: `H 1/5/0 D 1/2/0` — deviation 3
- openai/gpt-5.4-mini: `H 2/3/0 D 1/2/0` — deviation 4
- openai/gpt-5.4-nano: `H 2/1/0 D 1/2/0` — deviation 6

### United States v. Tymoshchuk — 1:23-cr-00324 (nyed) #71300581

- truth: `H 0/2/0 D 0/4/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 6
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 6
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 6
- openai/gpt-5.4-mini: `H 0/0/0 D 0/0/0` — deviation 6
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 6

### United States v. Gholinejad — 4:24-cr-00016 (nced) #70378502

- truth: `H 0/2/1 D 0/9/0`
- prod (live): `H 0/3/1 D 0/9/0` — deviation 1
- anthropic/claude-haiku-4-5: `H 0/3/1 D 0/9/0` — deviation 1
- gemini/gemini-3.1-flash-lite: `H 0/3/1 D 0/9/0` — deviation 1
- openai/gpt-5.4-mini: `H 0/3/1 D 0/13/0` — deviation 5
- openai/gpt-5.4-nano: `H 1/3/1 D 1/8/0` — deviation 4

### United States v. Lytvynenko — 3:23-cr-00088 (tnmd) #71820111

- truth: `H 0/2/0 D 0/4/0`
- prod (live): `H 3/2/0 D 1/3/0` — deviation 5
- anthropic/claude-haiku-4-5: `H 3/2/0 D 1/3/0` — deviation 5
- gemini/gemini-3.1-flash-lite: `H 3/2/0 D 1/3/0` — deviation 5
- openai/gpt-5.4-mini: `H 4/2/0 D 1/4/0` — deviation 5
- openai/gpt-5.4-nano: `H 3/2/0 D 1/3/0` — deviation 5

### United States v. Knoot — 26-5455 (ca6) #73388385

- truth: `H 0/0/0 D 2/2/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 4
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 4
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 4
- openai/gpt-5.4-mini: `H 0/0/0 D 0/0/0` — deviation 4
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 4

### United States v. Tymoshchuk — 1:23-cr-00324 (nyed) #70701403

- truth: `H 0/2/0 D 0/0/0`
- prod (live): `H 0/3/0 D 0/0/0` — deviation 1
- anthropic/claude-haiku-4-5: `H 1/0/0 D 0/0/0` — deviation 3
- gemini/gemini-3.1-flash-lite: `H 1/0/0 D 0/0/0` — deviation 3
- openai/gpt-5.4-mini: `H 2/0/0 D 0/0/0` — deviation 4
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 2

### United States v. Eileen Wang — 2:26-cr-00186 (cacd) #73323008

- truth: `H 0/3/0 D 0/0/0`
- prod (live): `H 1/1/0 D 0/0/0` — deviation 3
- anthropic/claude-haiku-4-5: `H 1/1/0 D 0/0/0` — deviation 3
- gemini/gemini-3.1-flash-lite: `H 1/0/0 D 0/0/0` — deviation 4
- openai/gpt-5.4-mini: `H 1/1/0 D 0/0/0` — deviation 3
- openai/gpt-5.4-nano: `H 1/0/0 D 0/0/0` — deviation 4

### United States v. Zewei — 4:23-cr-00523 (txsd) #70789744

- truth: `H 0/2/0 D 1/2/0`
- prod (live): `H 2/3/0 D 1/2/0` — deviation 3
- anthropic/claude-haiku-4-5: `H 2/3/0 D 1/2/0` — deviation 3
- gemini/gemini-3.1-flash-lite: `H 2/3/0 D 1/2/0` — deviation 3
- openai/gpt-5.4-mini: `H 2/3/0 D 1/2/0` — deviation 3
- openai/gpt-5.4-nano: `H 2/2/0 D 1/2/0` — deviation 2

### United States v. Schmitz — 1:24-cr-00234 (njd) #73353898

- truth: `H 1/1/0 D 0/0/0`
- prod (live): `H 0/0/0 D 0/0/0` — deviation 2
- anthropic/claude-haiku-4-5: `H 0/0/0 D 0/0/0` — deviation 2
- gemini/gemini-3.1-flash-lite: `H 0/0/0 D 0/0/0` — deviation 2
- openai/gpt-5.4-mini: `H 0/0/0 D 1/0/0` — deviation 3
- openai/gpt-5.4-nano: `H 0/0/0 D 0/0/0` — deviation 2

### United States v. Eileen Wang — 2:26-cr-00186 (cacd) #73326420

- truth: `H 0/2/0 D 0/0/0`
- prod (live): `H 0/1/0 D 0/0/0` — deviation 1
- anthropic/claude-haiku-4-5: `H 0/0/1 D 0/0/0` — deviation 3
- gemini/gemini-3.1-flash-lite: `H 0/1/0 D 0/0/0` — deviation 1
- openai/gpt-5.4-mini: `H 0/0/0 D 0/0/0` — deviation 2
- openai/gpt-5.4-nano: `H 0/1/0 D 0/0/0` — deviation 1

### United States v. Zheng et al. — 1:26-mj-00315 (gand) #73103748

- truth: `H 0/1/1 D 1/0/0`
- prod (live): `H 0/2/0 D 0/0/0` — deviation 3
- anthropic/claude-haiku-4-5: `H 0/1/1 D 0/0/0` — deviation 1
- gemini/gemini-3.1-flash-lite: `H 0/2/0 D 0/0/0` — deviation 3
- openai/gpt-5.4-mini: `H 0/1/1 D 0/0/0` — deviation 1
- openai/gpt-5.4-nano: `H 1/0/1 D 0/0/0` — deviation 3

### United States v. Zheng et al. — 3:26-mj-70297 (cand) #72532372

- truth: `H 0/4/0 D 0/0/0`
- prod (live): `H 0/6/0 D 0/0/0` — deviation 2
- anthropic/claude-haiku-4-5: `H 0/5/0 D 0/0/0` — deviation 1
- gemini/gemini-3.1-flash-lite: `H 0/4/0 D 0/0/0` — deviation 0
- openai/gpt-5.4-mini: `H 0/3/0 D 0/0/0` — deviation 1
- openai/gpt-5.4-nano: `H 1/2/0 D 0/0/0` — deviation 3

### United States v. Kejia "Tony" Wang — 1:25-cr-10274 (mad) #70691920

- truth: `H 0/2/0 D 0/0/0`
- prod (live): `H 0/2/0 D 0/1/0` — deviation 1
- anthropic/claude-haiku-4-5: `H 0/2/0 D 0/0/0` — deviation 0
- gemini/gemini-3.1-flash-lite: `H 0/2/0 D 1/0/0` — deviation 1
- openai/gpt-5.4-mini: `H 0/2/0 D 1/1/0` — deviation 2
- openai/gpt-5.4-nano: `H 0/2/0 D 0/0/0` — deviation 0

### United States v. Schmitz — 1:24-cr-00234 (njd) #73292090

- truth: `H 1/1/0 D 0/0/0`
- prod (live): `H 0/1/0 D 0/0/0` — deviation 1
- anthropic/claude-haiku-4-5: `H 0/1/0 D 0/0/0` — deviation 1
- gemini/gemini-3.1-flash-lite: `H 0/1/0 D 1/0/0` — deviation 2
- openai/gpt-5.4-mini: `H 0/1/0 D 1/0/0` — deviation 2
- openai/gpt-5.4-nano: `H 1/1/0 D 1/0/0` — deviation 1

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

