# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog][kac], and this project
adheres to [Semantic Versioning][semver].

[kac]: https://keepachangelog.com/en/1.1.0/
[semver]: https://semver.org/spec/v2.0.0.html

## [0.13.1] - 2026-05-31

Headline: a quiet `sync` is now a cheap `sync`. The end-of-case verify /
dedupe sweeps used to run on **every** case on **every** sync, even when
no docket in that case had changed — re-auditing unchanged rows at a real
per-call cost. Two back-to-back syncs with nothing new on the dockets
each spent \~$0.14 (107 LLM calls) re-confirming rows that could not have
changed. Those sweeps read their candidate rows and docket-entry context
entirely from the local store, and every domain LLM call pins
`temperature=0`, so when nothing landed the verdicts are byte-identical
to the prior sync. They are now skipped for any case whose dockets all
hit the date-modified short-circuit.

### Changed

- **`CaseSyncer.sync_case` skips the LLM-backed verify / dedupe sweeps
  for a case when no docket advanced past the short-circuit this sync**
  (`case_calendar/sync.py`). `_verify_scheduled_hearings`,
  `_verify_pending_deadlines`, `_dedupe_concurrent_hearings`,
  `_dedupe_concurrent_held_hearings`, and `_dedupe_nearslot_hearings` only
  run when at least one of the case's dockets landed new entries (or when
  `--reverify` is passed). The time-driven `_auto_mark_passed_stale`
  sweep still runs unconditionally — it flips a `pending` deadline to
  `passed` once its due date elapses and makes no LLM call, so gating it
  would strand an elapsed deadline at `pending` on a quiet docket. Safe
  with respect to `serve`: the webhook path never advances a docket's
  stored `date_modified`, so a webhook-touched docket always fails the
  short-circuit on the next poll and runs its sweeps then.

### Added

- **`case-calendar sync --reverify`** forces the verify / dedupe sweeps on
  every case regardless of the short-circuit. Use it after a
  verify-prompt or model change, or after an out-of-band store edit
  (`scripts/reprocess_entries.py`, `scripts/classify_significance.py`)
  that mutated rows without advancing any docket's `date_modified`.

## [0.13.0] - 2026-05-30

Headline: the extractor `SYSTEM_PROMPT` is now a **single unified
court-calendar prompt** that treats hearings and filing deadlines as
co-equal, rather than a hearing prompt with deadlines bolted on as an
"additional task". The prompt grew the way the project did — it began as
a hearing extractor and gained deadline support later — leaving two
prompts stapled together with duplicated scaffolding (the cross-docket
rule, the "no dates in keys" rule, the significance default + render
gate, the court-local date/time rules, the title rules, the JSON-safety
rules, and the ADD-requires-a-date logic were each stated twice) and two
separate JSON output schemas. The rewrite states each shared rule once in
a PART 1 ("rules shared by hearings and deadlines"), keeps the
hearing-specific actions in PART 2 and the deadline-specific actions in
PART 3, and emits a single JSON schema covering both. Every behavioral
protection that was pinned by a regression (anti-inference grounding, the
transcript trio + sealed carve-out, amicus major/minor, minute-entry
MARK_HELD triggers, multi-defendant key divergence, same-slot,
conditional deadlines, stipulation-vs-so-ordered) is preserved — the
change is structural, not a re-tuning of what the rules say. Validated by
a full four-column provider-store rebuild against the prior run: hearing
and deadline output stays within normal run-to-run variance, and
multi-day-trial handling is if anything cleaner (the model consolidates
trial phases instead of spawning a separate held row per trial day).

### Changed

- **`SYSTEM_PROMPT` restructured into PART 1 (shared) / PART 2 (hearings)
  / PART 3 (deadlines) with one JSON schema** (`case_calendar/llm.py`).
  Framing is now "court-calendar events … two equally-important kinds:
  HEARINGS and FILING DEADLINES" rather than hearing-first. No
  behavioral rule was dropped or weakened. The module docstring is
  updated to name both event families.
- **Deadline significance now has a structured ruleset**
  (`DEADLINE_SIGNIFICANCE_RULES`, `case_calendar/llm.py`), giving
  deadlines the same ordered scaffold hearings already had: RULE 1
  (classify by what is due, not who files it) → RULE 2 (type wins →
  major) → RULE 3 (procedural → minor) → RULE 4 (ambiguous → by the
  stakes of a miss) → RULE 5 (default major). It replaces the loose
  two-bullet major/minor list and folds the amicus and transcript
  significance splits into RULE 2 / RULE 3. RULE 2 enumerates the
  substantive federal classes a model must not bucket away as
  procedural-minor — dispositive-motion briefing, suppression / in-limine
  / Daubert briefing, trial-prep filings, sentencing memos and PSR
  objections, surrender-for-sentence, civil-forfeiture claim/answer,
  certified administrative record, substantive sealing / CIPA filings,
  the master amicus window, and transcript public-release — and RULE 5
  states the bias-toward-major rationale (a wrong "minor" is hidden by
  the render gate, a wrong "major" only adds a row). This is the
  structural counter to the deadline-significance bucketing documented in
  `model-comparison/SCORECARD.md`.
- **Behavior change — recurring joint status reports and case-management
  statements are now `major`** (RULE 2), where they were previously
  treated as procedural-`minor`. Their filing deadlines now appear on
  subscriber calendars. (An answer to a complaint is likewise `major`.)
- **`SIGNIFICANCE_RULES` renamed to `HEARING_SIGNIFICANCE_RULES`** now
  that significance is split into a hearing block and a deadline block;
  it stays a separate constant reused by
  `scripts/classify_significance.py`.
- **Three Gemini-extractor weakness rules integrated into the unified
  prompt** ahead of making Gemini the default extractor: (1) a same-DATE
  transcript rule — a transcript of a proceeding held on date X must
  `MARK_HELD` the known hearing on that date (matched on date, not
  time-of-day) instead of allocating a phantom `proceedings-<date>` row
  the same-slot dedupe can't catch; (2) Location must PRESERVE EVERY NAMED
  TOKEN (court formal name, state, ZIP, room labels) rather than
  abbreviating the courthouse; (3) `dial_in` must carry access labels
  ("audio observation only", etc., often citing Civ. L.R. 77-3(d)). These
  fold the substance of the closed-unmerged #45 PR into the new PART 2
  structure.
- **Default extraction provider flipped to Gemini; summaries stay on
  Anthropic** (`case_calendar/llmkit/providers.py`). Auto-detection now
  uses a per-track API-key priority: the extraction track prefers
  `gemini > anthropic > openai` (`_detect_extraction_provider`,
  `_EXTRACTION_KEY_PRIORITY`) and the summary/base track prefers
  `anthropic > gemini > openai` (`_detect_provider`,
  `_SUMMARY_KEY_PRIORITY`). A fresh operator who provisions all three
  keys without setting any `LLM_*` var now lands on the split — Gemini
  reading docket entries into hearings + deadlines, Anthropic writing the
  per-docket case summaries — instead of Anthropic for both. `LLM_PROVIDER`
  still forces one provider for both tracks, and `LLM_EXTRACTION_PROVIDER`
  / `LLM_SUMMARY_PROVIDER` still override per track. The flip is earned by
  the `DEADLINE_SIGNIFICANCE_RULES` change above: with the substantive
  deadline classes named in-prompt for every provider, Gemini no longer
  silently buckets them as procedural-`minor`, and it posts the best
  aggregate deviation (305) in the comparison while running \~3.75× cheaper
  and \~1.9× faster per call than Anthropic on the constant-load
  extract+verify pair. See `model-comparison/SCORECARD.md`.
- **Two MARK_HELD rules that address the date-mismatch warnings at the
  source** (PART 2), diagnosed from a focused 4-case build where 84/159
  warnings were multi-day trials and 42/159 were sequential conferences
  (the warnings are universal across providers; Gemini had the fewest).
  (A) **Multi-day trials become one event per day** — each trial-day minute
  entry `MARK_HELD`s a new `trial-<def>-day-N` key titled `<Trial> — Day N`
  on that day (the existing insert-as-held path creates the dated row), and
  the original row is retitled "— Day 1" once a Day 2 appears; a single-day
  trial stays unsuffixed. This turns the per-day minute entries from rejected
  actions into real per-day calendar events. (B) **MARK_HELD matches the
  date-closest row or IGNOREs** — for sequential proceedings
  (`status-conf-<def>-N`) the model targets the sibling within \~2 days of the
  held date rather than an arbitrary one, and emits IGNORE when none qualifies
  (the deterministic 2-day guard still backs this).

### Fixed

- **A conditional `ADD_DEADLINE` whose `local_date` came back as the
  literal string `"null"` (instead of JSON null) no longer crashes the
  sync** (`case_calendar/sync.py`). `_local_to_utc` now treats a
  `"null"` / `"None"` *date* string as a missing date and returns `None`
  (storing the row date-less, the same end state as a conditional
  deadline) — the date-side twin of the existing `local_time` "null"
  guard. Previously this reached `datetime.fromisoformat("nullT16:00")`
  and raised `ValueError`, dropping that entry's deadline. Surfaced by
  the 0.13.0 validation build.
- **Near-slot duplicate hearings are now collapsed** (`_dedupe_nearslot_hearings`
  in `case_calendar/sync.py` + `Store.find_nearslot_hearing_clusters`). The
  extractor — Gemini especially — allocates a fresh `hearing_key` for a
  proceeding it already has at a NEAR (not exactly-equal) slot, which then
  rendered twice on subscriber calendars: a sentencing held at its scheduled
  date AND its actual held date, a CIPA hearing as a date-only + a timed row,
  a trial start under two keys. The two prior dedup sweeps only merged the
  EXACT same `(docket_number, court_id, starts_at_utc)` slot. The new sweep
  clusters by same court-local date (within a status) and by same once-only
  proceeding + defendant (`held`, across dates), and routes each cluster to the
  LLM resolver (MERGE_INTO / KEEP_BOTH) so genuinely-distinct same-day hearings
  — a morning motion hearing and an afternoon status conference — are kept. The
  shared `DEDUPE_HEARING_SYSTEM_PROMPT` was generalized from exact-slot to
  near-slot framing. Closes the "Known gap" documented in AGENTS.md (a narrower
  cross-*status* case remains). Verified on the Gemini 4-case build: McGonigal
  collapses to one sentencing (@12-14) and one CIPA, wei to one trial start +
  Days 2–6, and ding's clean 13-day per-day trial series is untouched.

## [0.12.0] - 2026-05-30

Headline: timeless filing deadlines now anchor at **4 PM court-local**
instead of 5 PM. Most federal clerk's offices close at 4:00 PM (the
sampled districts — D.D.C., D. Del., D. Md., E.D. Pa., M.D. Fla.,
C.D. Cal. — close at 4:00 PM almost uniformly), so a 4 PM reminder lands
as close as possible to when a filing would actually hit the docket. The
watcher can check PACER right then and, if what they're after isn't on
RECAP yet, still has the evening to follow up. The 4 PM value remains a
reminder anchor, not the legal deadline — electronic filing itself runs
to midnight in the court's time zone under FRCP 6(a)(4) (and the parallel
FRAP 26(a)(4) / FRCrP 45(a)(4)); orders that state an explicit time are
still honored verbatim.

### Changed

- **`DEADLINE_DEFAULT_LOCAL_TIME` moved from `"17:00"` to `"16:00"`**
  (`case_calendar/sync.py`). Filing deadlines extracted without an
  explicit clock time now fire at 4 PM court-local rather than 5 PM.
  Stored `due_at_utc` timestamps for new timeless deadlines shift one
  hour earlier (e.g. 21:00 UTC → 20:00 UTC for an EDT court). Existing
  rows are not rewritten; the new anchor applies as deadlines are
  re-extracted. The matching guidance in the extractor `SYSTEM_PROMPT`
  and `docs/llm-prompts.md` now reads "4 PM court time".

## [0.11.0] - 2026-05-29

Headline: the verify pass is now **deterministic and correct at
temperature=0**. At 0.10.0 the pass made occasional borderline
decisions stochastically (temperature=1 default), so whether a past
trial vacated by guilty plea — without a formal vacatur entry on the
docket — stayed `scheduled` or got DELETE_HALLUCINATION'd to
`cancelled` came down to a coin flip in the sampler. The principle
that drove this release: a calendar must show the correct data, not
data whose correctness depends on chance. This release pins
temperature to 0 and fixes the verify-pass decision boundaries so the
deterministic outcome is the correct one.

Validated against the same 28-case fixture used for the 0.10.0
provider comparison: all 6 hearing rows that had flipped state
incorrectly at temperature=0 in the pre-fix design now match prod (the
McGonigal jury trial, the Knoot trial, the Moucka pretrial
conference, the Akhter Muneeb trial, the Gallyamov status conference
5, and the Ashtor Prince trial — all of them).

### Added

- **`temperature` parameter on the llmkit dispatch and per-provider
  call functions.** `case_calendar.llmkit.providers._dispatch_llm_call`
  and the three SDK wrappers (`_call_anthropic`, `_call_openai`,
  `_call_gemini`) now accept `temperature: float | None = None`. When
  `None` (the default), the SDK's own default applies (currently 1.0
  across all three providers); when set, the value is forwarded to
  whatever per-provider parameter that SDK names for it (Anthropic
  `temperature`, OpenAI `temperature`, Gemini
  `GenerateContentConfig.temperature`). The intent is one common
  knob for "how stochastic should this call be."

- **Source entries always included in the verify-pass context.** The
  hearing / deadline row's `source_entry_ids` — the docket entries
  that originally allocated the row — are now part of the
  `recent_entries` payload the verify-pass LLM receives. Adds
  `Store.get_entries_by_ids(docket_id, entry_ids)` and threads
  `source_entry_ids` through `CaseSyncer._verify_context_entries`.
  Without this enrichment, an old scheduling order (a 2023 order that
  set a 2024 trial, on a docket that kept moving past) sat outside
  both the most-recent-15 window and the around-hearing-date window
  the verify pass already had, leaving the model's
  DELETE_HALLUCINATION rule "you've seen the original source entry
  and concluded it does NOT actually schedule this hearing"
  unsatisfiable — and the model broke the rule at temperature=0
  rather than picking UNCLEAR.

- **Deterministic DELETE_HALLUCINATION guard.**
  `CaseSyncer._delete_hallucination_allowed` checks that every source
  entry ID for the candidate row was present in the recent_entries
  payload the LLM actually received. When any source entry was
  missing — because the row was deleted from the store, the
  `source_entry_ids` list is malformed, etc. — the verdict is
  downgraded to UNCLEAR with a WARN log naming the missing entry
  ids. Pair with the context enrichment above: the enrichment makes
  the model's rule satisfiable, the guard enforces it deterministically
  regardless of what the LLM emits.

- **Per-case progress monitor convention (AGENTS.md).** Long-running
  sync / build operations now emit one line per case start carrying
  case slug, queue position (X/N), and a rolling ETA computed from
  the average interval between case starts. The convention is opt-in
  for the operator who likes that signal; the implementation is
  Monitor + a small awk on `tail -F` of the script's log, parsing the
  `replaying case <slug>` line markers. See the Conventions section
  of AGENTS.md for the full shape.

- **Anthropic prompt-cache threshold documentation (AGENTS.md Key
  Design Decisions).** Anthropic's prompt cache has a per-model
  minimum-prompt-size threshold: 1024 tokens on Sonnet/Opus 4.x,
  2048 on Haiku 4.5. Below the floor, `cache_control: ephemeral` is
  a no-op (nothing written, nothing read, every call pays full
  uncached input rate). Each of our prompts is mapped against the
  floor in the design note, with the bidirectional risk for future
  prompt edits called out: slim passes can quietly hurt cost by
  dropping a prompt below the floor; merge passes can quietly help
  cost by lifting one above it.

- **Model-comparison directory section (AGENTS.md Architecture).**
  Moves `build_provider_stores.py` out of "One-shot maintenance
  scripts (scripts/)" — where it doesn't live — into a new "Model
  comparison (model-comparison/)" section, and documents the sibling
  files (`ground_truth_worksheet.py`, `export_model_events.py`,
  `score.py`, `SCORECARD.md`) that were undocumented previously.

### Changed

- **Every domain-level LLM call now pins `temperature=0.0`.** Each of
  `extract_actions`, `verify_hearing`, `verify_deadline`,
  `resolve_duplicate_hearings`, and `generate_docket_summary` in
  `case_calendar.llm` now passes `temperature=0.0` through dispatch.
  In production this means the model's decisions are deterministic
  given the inputs — same prompt + same context produces the same
  verdict, sync after sync. The 0.10.0 default of 1.0 (Anthropic's
  SDK default) made the verify pass's borderline-cancellation
  decisions a coin flip, so the McGonigal-trial-stays-scheduled
  outcome that prod accidentally got right at 0.10.0 could have
  flipped on any future sync. This release closes that gap.

- **Verify `max_tokens` bumped from 512 to 1500.** The verify pass
  cites the docket entries that justify its verdict in the `reason`
  field; on a busy past trial with multiple contradicting entries to
  weigh, the model wants to write several hundred chars of citation.
  At 512 the response was getting truncated mid-sentence and
  `_call_lm_and_parse` returned UNCLEAR (the safe fallback for
  unparseable JSON). The us-v-knoot `trial-knoot` truncation observed
  during the 0.10.0 provider-store run — model emitted 2063 partial
  chars before hitting the cap — is the canonical case. 1500 leaves
  plenty of headroom for verbose citation reasoning without pushing
  per-call cost meaningfully (output tokens are a small fraction of
  total spend; the cached 8900-token system prompt dominates input
  cost).

- **`VERIFY_SYSTEM_PROMPT` consolidates BOTH hearing verify AND
  deadline verify into one prompt.** Pre-0.11.0 had two separate
  prompts; both fell under Haiku 4.5's 2048-token cache floor (the
  hearing one at ~1770 tok, the deadline one at ~420), so the verify
  track paid full input-token rate on every call. The merged prompt
  handles either row type — the user message's "CANDIDATE HEARING" /
  "CANDIDATE DEADLINE" header plus the hearing_key vs deadline_key
  field tell the model which kind it's auditing — and adds a new
  rule that explicitly tells the model to return UNCLEAR (not
  DELETE_HALLUCINATION) when the source entry isn't visible in the
  recent_entries. (Note: the cache-eligibility goal was MISSED in
  practice — the merged prompt landed ~50 tokens short of the
  Anthropic-measured threshold; see [Known Limitations](#known-limitations-0110)
  below.)

- **Phase 1 prompt slim (mechanical removals).** Trims roughly 1500
  chars of regression-citation prose and verbose example clusters
  from the prompts without touching any rule. Specifically:
  case-name regression citations (us-v-knoot, McGonigal/Shestakov,
  Wei 3:23-cr-01471, Anthropic v. DOW conditional-deadline example,
  etc.) stripped from prompt TEXT but kept as PR-description and
  AGENTS.md context; Federal Rules of Criminal Procedure citations
  removed from `SIGNIFICANCE_RULES` proceeding-type list; BAD/GOOD
  example clusters in `SUMMARY_SYSTEM_PROMPT` pruned from 3-4 to 1-2.
  The original test pins that referenced removed citations were
  updated to track the new wording. Validated against the 28-case
  fixture before and after — no behavior change observed.

### Removed

- **`VERIFY_DEADLINE_SYSTEM_PROMPT` constant.** Merged into
  `VERIFY_SYSTEM_PROMPT` (see above). `case_calendar.llm.verify_deadline`
  now calls `_call_lm_and_parse` with the merged prompt.

### Fixed

- **Verify pass's McGonigal-class DELETE_HALLUCINATION false positive
  at temperature=0.** The context enrichment + deterministic guard
  together make the verdict only reachable when the model can cite
  the source entry it read. Validated: `trial-mcgonigal` stays
  `scheduled` in the 0.11.0 validation build with the verify pass
  explicitly citing source entry 331170599 (the 2023 scheduling
  order) in its UNCLEAR verdict; in the pre-fix build at
  temperature=0 the same row was DELETE_HALLUCINATION'd to
  `cancelled` because that exact 2023 entry wasn't in the model's
  window.

- **Verify pass truncation on verbose verdicts.** The `max_tokens`
  bump from 512 to 1500 stops the model getting cut off mid-citation
  on busy past trials and silently falling through to UNCLEAR.

- **AGENTS.md incorrectly claimed all three extractor-track prompts
  cache on Haiku.** Only `SYSTEM_PROMPT` (the per-entry extract
  prompt) clears the 2048-token floor; the verify and dedupe
  prompts fall under and don't cache. The Architecture line for
  `llm.py` is corrected and a pointer added to the new Key Design
  Decisions note on the cache threshold.

### Known limitations (0.11.0)

- **Verify-track cache eligibility was the stated goal of the prompt
  consolidation but DID NOT trigger in practice, and the documented
  Haiku 4.5 cache threshold turns out to be inaccurate.** Initial
  estimate of the merged prompt at ~2104 tokens (4-chars-per-token
  rule of thumb) was followed by a post-validation bump (commit
  fe62851) that added ~2800 chars of substantive content (a
  step-by-step audit process, MARK_FILED subject-matching guidance,
  RESCHEDULE-vs-CANCEL ambiguity rule, notice-vs-status-report
  caveat) targeting the 2048 floor. Measured against Anthropic's
  `count_tokens` API, the post-bump prompt was **2941 tokens** —
  well over the documented 2048-token Haiku 4.5 floor. Despite that,
  `cached=0` and `cache_write=0` on every one of the 130 verify
  calls in the validation build. The empirical conclusion is that
  **Haiku 4.5's real cache floor is higher than the documented
  2048**, likely 4096 (since `SYSTEM_PROMPT` at 9302 measured tokens
  does cache and `VERIFY_SYSTEM_PROMPT` at 2941 doesn't). The bump
  was subsequently reverted (commit c5f0bd0): with no measurable
  behavior win and no cache offset, the extra content was just
  cost-without-payoff. The verify track stays uncached at ~2000
  tokens, costing ~$0.55/sync vs ~$0.30/sync if it were cached.
  Bringing the verify track into cache would require pushing the
  merged prompt past ~4500 measured tokens with substantive content,
  which is a stretch given the verify rule space is already fully
  enumerated; a follow-up could alternatively route the verify track
  to Sonnet (which actually caches at 1024 tokens per the docs) and
  accept the higher Sonnet per-token rate in exchange for the cache
  discount. AGENTS.md's prompt-cache threshold design note has been
  corrected with the empirical 2941-token measurement + the likely
  4096-token real Haiku floor + the lesson that the bump was
  cost-without-payoff, for the next prompt-edit pass.

## [0.10.0] - 2026-05-29

### Changed

- **Default LLM provider reverted to Anthropic for BOTH the extraction
  track AND the summary track.** The 0.9.0 release switched the default
  to Gemini for extraction on the strength of its lower aggregate
  deviation score (328 vs Anthropic's 381 on the SCORECARD fixture) and
  recommended a split: `LLM_PROVIDER=gemini` for the cheap+fast
  extraction track + `LLM_SUMMARY_PROVIDER=anthropic` for the
  case-distinguishing summary prose. Real-world use after that flip
  surfaced a maintenance treadmill the score didn't capture: Gemini
  systematically classifies substantive federal-procedure deadline
  classes as `procedural-minor` and silently drops them from subscriber
  calendars at the render-time significance gate.

  Out-of-fixture classes Gemini missed in production (each addressable
  by a targeted prompt-vocabulary addition, but the list is decades
  deep and unbounded):

  - PSR (Presentence Investigation Report) interview, first disclosure,
    objection windows
  - Speedy Trial Act § 3161(h) exclusion orders
  - Surrender for service of sentence (the date a defendant must
    self-report to BOP custody)
  - Civil-forfeiture Supp. R. G claim + answer deadlines
  - Substantive sealing motion practice (briefing on a motion to
    seal/unseal — not the routine "filed under seal" stamps)
  - Exhibit-filing deadlines under a final pretrial order
  - Certified administrative record / certified index of the
    administrative record (the deadline that starts the APA
    cross-motion briefing clock)

  Each miss is fixable with a prompt-vocabulary addition naming the
  class. The problem is the list of named federal procedural classes
  is decades deep, the maintainer is not a lawyer, and a calendar
  people rely on cannot have its silent drops audited case-by-case
  after the fact. Anthropic's training corpus loads these priors for
  free — the model classifies "Order Excluding Time Under the Speedy
  Trial Act" as substantive without ever being told what the Speedy
  Trial Act is. The aggregate deviation lead Gemini still has on the
  in-fixture set is real and so are the cost wins (~4× cheaper, ~2×
  faster per call), but for a docket-watching calendar shipped as a
  default, structural class coverage matters more than the in-fixture
  score.

  `case_calendar.llmkit.providers._detect_provider` API-key priority
  changed back to **`anthropic > gemini > openai`** — a fresh operator
  who provisions multiple keys without setting `LLM_PROVIDER` now
  lands on the project's recommended default. `.env.example`,
  `config.example.yaml`, `docs/installation.md`, `docs/architecture.md`,
  `docs/cost.md`, `model-comparison/README.md`,
  `model-comparison/SCORECARD.md`, and `AGENTS.md` all reordered
  Anthropic-first.

- **Per-track override env vars from 0.9.0 are unchanged and remain
  fully supported.** `LLM_EXTRACTION_PROVIDER` still overrides
  `LLM_PROVIDER` for the extraction + verify + dedupe calls;
  `LLM_SUMMARY_PROVIDER` still overrides for case summaries. Splitting
  the tracks just isn't the documented default anymore — operators who
  have measured Gemini against their own caseload and confirmed it
  doesn't silently drop substantive classes they care about can pin
  `LLM_EXTRACTION_PROVIDER=gemini` for the cost win while keeping
  Anthropic for the case-distinguishing summary prose (statute
  citations, count numbers, sentence breakdowns, cancelled-schedule
  notes, custody status, full briefing schedules):

  ```bash
  LLM_PROVIDER=anthropic               # default for both tracks
  LLM_EXTRACTION_PROVIDER=gemini       # override extraction only
  ANTHROPIC_API_KEY=sk-ant-...
  GEMINI_API_KEY=...
  ```

  This is the inverse of the 0.9.0 recommendation; behaviorally it
  produces the same provider assignment per track.

### Documentation

- SCORECARD.md intro rewritten to explain the maintenance-treadmill
  reasoning behind the 0.10.0 reversion, with the substantive-class
  list and the score-vs-coverage gap that drives it.
- "Recommended provider split" section rewritten as
  "Anthropic on both tracks" with the per-track split positioned as
  the optional capability for measured operators.
- `docs/cost.md` "Why the recommended split is …" section renamed to
  "Why Anthropic is the 0.10.0 default — and why splitting the tracks
  is still supported", with the cost trade-off framed alongside the
  class-coverage trade-off.
- `docs/architecture.md` "Why the two-track split matters" section
  renamed to "Why the two-track split exists — and why Anthropic is
  the 0.10.0 default for both", with the substantive-class failure
  mode documented as the structural reason for the default.
- `.env.example` and `config.example.yaml` defaults flipped back to
  Anthropic; the `LLM_SUMMARY_PROVIDER=anthropic` example line is
  removed (Anthropic is now the default for both tracks, so the
  split-by-default example was misleading), and the
  `LLM_EXTRACTION_PROVIDER=anthropic` example line documents how to
  pin a different provider for one track without changing the global
  default.

## [0.9.0] - 2026-05-29

### Changed

- **Default LLM provider switched back to Gemini.** The 0.8.2 release
  reverted from Gemini to Anthropic on the basis that Gemini was
  systematically dropping substantive event classes (preliminary-injunction
  hearings on civil-litigation dockets, Speedy Trial Act exclusions, PSIR
  deadlines, CIPA submissions, jury-process deadlines, surrender for service
  of sentence) that the comparison's deviation-from-human-truth score
  didn't penalize hard enough. This release closes those gaps via
  matched prompt edits — explicit MARK_HELD trigger phrases (Electronic
  Clerk's Notes / Minute Entry / "Proceeding held as to Defendant" / verdict
  form / judgment-after-trial), the sealed-transcript carve-out (IGNORE
  for `***SEALED***` / `***RESTRICTED***` / "Sealed Transcript" entries
  since the public version on the same docket carries the real deadlines),
  the per-proceeding transcript-deadline suffix rule (so a docket with
  arraignment + sentencing transcripts doesn't see one's deadlines
  silently overwrite the other's), TRANSCRIPT ORDER strict-IGNORE
  including MARK_HELD (the actual transcript entry filed shortly after
  carries the specific proceeding identifier), and a pretrial-transcript-
  isn't-trial-cancel anti-inference guard. Both providers now catch
  every class the 0.8.2 SCORECARD flagged on either side; the deviation
  gap reopens in Gemini's favor without the qualitative-coverage gap
  that justified the 0.8.2 revert.

  `case_calendar.llmkit.providers._detect_provider` priority changed
  to `gemini > anthropic > openai` — a fresh operator who provisions
  multiple keys without setting `LLM_PROVIDER` lands on the default
  ranked highest by the published comparison. `.env.example`,
  `config.example.yaml`, `docs/installation.md`, `docs/architecture.md`,
  `model-comparison/README.md`, `docs/cost.md`, and `AGENTS.md` all
  reordered Gemini-first.

- **Dedupe sweeps now DELETE absorbed same-slot siblings instead of
  flipping them to `cancelled`.** Both `_dedupe_concurrent_hearings`
  (LLM-driven, future scheduled clusters) and
  `_dedupe_concurrent_held_hearings` (deterministic, held clusters)
  previously folded siblings' `source_entry_ids` onto the canonical row
  then upserted each sibling back with `status='cancelled'` and an audit
  note pointing at the target. The cancelled rows were noise from a
  subscriber-calendar perspective (renderers skip them) but inflated
  H_canc deviation in the provider scorer — each absorbed key-drift
  artifact counted as a spurious cancellation in the totals even though
  it was never a real court-ordered cancellation. The Akhter case
  (1:25-cr-00307, vaed) was the canonical example: its logical PACER
  docket is split across three CourtListener `docket_id`s and the
  cross-docket rule forbids RESCHEDULE across docket_ids, so both
  providers correctly emit ADD with new `-N` suffixes on each sibling's
  pass and rely on dedupe to clean up — producing ~8 extra "cancelled"
  arraignment rows per build solely as merge byproducts. Both sweeps
  now DELETE siblings outright via a new
  `Store.delete_hearing(case_id, hearing_key)` primitive; the audit
  trail of WHICH sibling keys were absorbed (and, for the LLM-driven
  sweep, the LLM's reason) moves to the canonical row's `audit_notes`
  so it survives the deletions. `source_entry_ids` were already being
  merged onto the canonical and continue to be.

- **Operator's prod SQLite store rebuilt with the Gemini default.**
  The Gemini-built store from the new 0.9.0 backfill was copied over
  `data/case-calendar.sqlite`. The previous (Anthropic-built) 0.8.2
  store is preserved as
  `data/case-calendar.sqlite.bak-2026-05-29-pre-switch-to-gemini`.
  Fresh installs initialize their own store on first sync.

- **`model-comparison/SCORECARD.md` regenerated from a fresh
  all-provider build against the current prompts + dedupe-deletion
  code path.** Per-docket deviations, totals, and qualitative
  event-set diffs all reflect the new state.

## [0.8.2] - 2026-05-28

### Changed

- **Default LLM provider reverted to Anthropic.** The 0.8.1 release switched
  the default to Google Gemini on the basis that it scored highest on the
  comparison's deviation-from-human-truth metric. The 0.8.2 release reverses
  that decision after closer analysis of *what* each model was missing. The
  scorer measures the absolute difference between the model's count and the
  human's count in six status categories per CourtListener record; it cannot
  tell "the model missed a substantive event the human counted" from "the
  model extracted a noisy procedural event the human didn't count" — both
  move the score by the same magnitude. The qualitative event-set diffs in
  `model-comparison/SCORECARD.md` showed that Gemini's misses are
  systematically *substantive* (preliminary-injunction hearings on civil-
  litigation dockets, Speedy Trial Act exclusions, PSIR deadlines, CIPA
  Section 4/5/6/8 submissions, jury-process deadlines, surrender for service
  of sentence, certified administrative records on civil dockets) and
  Anthropic's misses are systematically *procedural* (transcript redaction
  windows, motion-in-limine briefing chains, the leave-to-file-amicus
  shuffle). Subscribers act on the substantive class; the procedural class
  is calendar noise that's typically tagged `minor` anyway. The score
  treated those as equivalent and ranked Gemini ahead; the calendar's actual
  fit-for-purpose ranks Anthropic ahead. Over a five-year horizon the cost
  premium is roughly $60 for a 28-case caseload (~$6 once for backfill,
  ~$0.20/week steady-state) — orders of magnitude below the value of not
  silently missing a docket's preliminary-injunction hearing.

  `case_calendar.llmkit.providers._detect_provider` priority restored to
  `anthropic > openai > gemini`. `.env.example`, `config.example.yaml`, and
  every provider listing in the docs reordered Anthropic-first.
  `tests/test_llmkit_providers.py` priority test restored to
  `test_anthropic_wins_when_both_set`. `model-comparison/README.md`,
  `docs/cost.md`, `docs/installation.md`, `docs/architecture.md`,
  `docs/llm-prompts.md`, and `AGENTS.md` all reverted to Anthropic-first
  ordering with the explanation of why the comparison's highest-scoring
  column is not the chosen default carried in `model-comparison/SCORECARD.md`
  as a top-note (so a reader doesn't have to reconcile the two on their own).

- **Operator's prod SQLite store reverted to the Anthropic-built one.** The
  Anthropic-built store from the 0.8.1 backfill (preserved as
  `data/v0.8.1-snapshot/provider-stores/anthropic-claude-haiku-4-5-v0.8.1/`)
  was copied back over `data/case-calendar.sqlite`. The Gemini-built store
  that was briefly promoted is preserved as
  `data/case-calendar.sqlite.bak-2026-05-28-pre-revert-to-anthropic`. Fresh
  installs initialize their own store on first sync.

### Added

- **`--skip-summaries` flag on `model-comparison/build_provider_stores.py`.**
  Skips the `summary.refresh_stale` phase per column, so the (typically
  expensive) higher-tier summary track doesn't run when iterating on
  extractor-prompt changes. The comparison's `score.py` only reads hearings
  and deadlines, so summaries aren't needed to rank columns. Cuts ~$1.10 per
  column per iteration. Logged as a no-op skip per column so the build.log
  records the intentional gap.

### Internal — what we tried and reverted on the prompt side

After 0.8.1 surfaced the score-vs-coverage tension, we attempted to close
Gemini's coverage gaps via three coordinated extractor-prompt edits, all
designed to be docket-agnostic and generic. The full diff is in the commit
history on `feat/event-vocab-prompt`. All three are gone from the prompt
again in 0.8.2 — the lessons stand even though the changes did not:

- **META-RULE — "when in doubt about extraction, ADD".** Told the model to
  lean toward extracting an event when uncertain after ruling out the
  standard IGNORE patterns. Worked exactly as designed: Gemini extracted
  more events under it, including the previously-missed 5/23 Certified
  Administrative Record deadline on the Anthropic v. DOW (`3:26-cv-01996`,
  N.D. Cal.) docket. But the measurement was Gemini at **+63 deviation
  points** against the v0.8.1 scorecard, because the extra rows landed
  *past* the human's specific count of "what counts as an event." The
  scorer methodology rewards matching, not coverage. Reverted.

- **NAMED FEDERAL-PROCEDURE EVENT CLASSES section** in the deadline portion
  of `SYSTEM_PROMPT`. An illustrative-not-exhaustive list of high-frequency
  federal-court event categories Gemini was silently dropping: Speedy Trial
  Act exclusions, PSIR deadlines, CIPA Section 4/5/6/8 submissions, jury-
  process deadlines, forfeiture orders and money judgments, discovery
  cutoffs / Rule 26(a)(2) expert disclosures, surrender for service of
  sentence, motion-in-limine briefing chains. Without the META-RULE this
  section was reference-only — the model could ignore it and did. On DOW
  cand (`72379655`), the row counts were *identical* between with-vocabulary
  and without-vocabulary Gemini builds. Reverted to keep `SYSTEM_PROMPT`
  lean.

- **CRITICAL — multi-defendant naming and rowing.** Strengthened the
  existing "Title-naming rule (defendants)" with a per-defendant-row
  mandate (held Initial Appearance for Defendant A does NOT mark held an
  Initial Appearance for Defendant B; per-defendant hearing_key suffixes).
  Targeted Gemini's tendency to collapse same-type proceedings across
  co-defendants on the Akhter docket. Did not get a clean measurement
  before the iteration was reverted (Anthropic's column on the same
  rebuild crashed at docket 30/46 with an API credit-balance error, so
  Anthropic-side deviation was confounded by 16 missing dockets). Reverted
  to the original phrasing pending a cleaner measurement opportunity.

The structural insight outlives the specific edits: a calendar-extraction
prompt has a "more extraction" lever (META-RULE-style) and a "more
specific framing" lever (named-vocabulary). Pulling the former regresses
the score in measurable units; pulling the latter alone doesn't measurably
change behavior. The DOW PI hearing on 3/13 — a case-specific gap where
Gemini collapses adjacent procedural events into one row — appears
unreachable from either lever in a way that generalizes; a docket-pattern-
specific edit would close it but defeats the project's "generic prompt
rules only" design principle. The current direction is to live with
documented coverage limits per provider rather than push the prompt
toward a model whose coverage matches Anthropic's.

[0.8.2]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.8.2

## [0.8.1] - 2026-05-28

### Changed

- **Default LLM provider switched from Anthropic to Google Gemini.** Per
  the published provider comparison
  ([`model-comparison/SCORECARD.md`](model-comparison/SCORECARD.md)),
  `gemini-3.1-flash-lite` (extraction) + `gemini-2.5-pro` (summary) is
  both the most accurate column (392 total deviation against a
  human-blind ground truth, vs `anthropic/claude-haiku-4-5` at 413) and
  the cheapest from-scratch backfill ($2.56 vs Anthropic's $8.56 — Gemini
  is roughly **3.3× cheaper** to backfill an entire caseload). The
  fallback priority in
  `case_calendar.llmkit.providers._detect_provider` is now
  **gemini > openai > anthropic**, so an operator who provisions multiple
  `*_API_KEY` env vars without an explicit `LLM_PROVIDER` lands on the
  recommended default; operators with `LLM_PROVIDER` set are unchanged.
  `.env.example`, `config.example.yaml`, and every provider-listing in
  the docs reorder Gemini-first (with the cost table flagging
  `gemini-3.1-flash-lite` as the **default** row).
- **Operator's prod SQLite store was promoted to the Gemini-built one.**
  The full from-scratch rebuild this release was measured against became
  `data/case-calendar.sqlite`; the previous Anthropic-built store was
  preserved as `data/case-calendar.sqlite.bak-2026-05-28-pre-gemini-promote`
  for rollback. This is an operator-facing note rather than a code
  change — fresh installs initialize their own store on first sync
  regardless.

### Added

- **`model-comparison/` — a blind human-ground-truth scoring framework.**
  An honest answer to "which LLM provider should this project default
  to?", structured so a reader doesn't have to take any claim on faith.
  `build_provider_stores.py` rebuilds every tracked case's calendar
  from the same cached court data across every provider/model column in
  parallel, with a shared CourtListener / PDF response cache so each
  upstream fetch happens at most once total (zero CourtListener cost
  beyond the first column). `ground_truth_worksheet.py` emits a blind
  worksheet (one row per CourtListener record, model output never seen)
  for a human to fill in by reading the dockets; `score.py` then
  computes each column's total deviation against the filled truth —
  deterministic, no LLM in the scoring loop, no judgment beyond
  `|model − truth|` summed across six per-record count categories.
  Everything is committed: the worksheet template, the scoring script,
  the full event-data export (`model_events.csv`), the cost report
  (`cost.md`), and this release's filled `ground_truth.csv` for
  reproducibility. The README explains the methodology AND its
  limitations — the prompt-fit bias (prompts were authored by Claude
  and run identically across columns) is acknowledged rather than
  papered over.
- **`SCORECARD.md` — the canonical scoring result.** New artifact
  carrying the totals, the qualitative event-set diffs between the top
  two columns (showing what classes of events each catches that the
  other drops — Gemini wins on the new 0.8.0 transcript rules and
  motion-in-limine briefing chains; Anthropic wins on Speedy Trial Act
  stipulations, PSIR deadlines, the DOW preliminary-injunction
  hearing, jury-process and CIPA filings), and per-docket detail.
  Hand-curated qualitative sections are noted in the score.py call
  pattern — re-running the scorer overwrites them.

### Fixed

- **The `--validate` baseline column in `build_provider_stores.py`
  matches reality.** The fidelity-check assertion ("the X column should
  reproduce prod") now points at `gemini/gemini-3.1-flash-lite` instead
  of `anthropic/claude-haiku-4-5`, since prod was rebuilt by the former
  as part of this release. Previously the check would have spuriously
  flagged the Anthropic column as "diverged" against a Gemini-built
  prod store.

[0.8.1]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.8.1

## [0.8.0] - 2026-05-28

### Changed

- **Filing deadlines are now tracked uniformly on every docket.** The
  docket-aware auto-detect (criminal `-cr-` / `-cm-` / `-po-` / `-mj-cr-`
  → off, civil / appellate / specialty → on) and the per-case
  `extract_deadlines` override are gone. Every case — criminal, civil,
  appellate, magistrate, specialty — runs the same per-entry pipeline,
  and the existing significance gate (`major` vs `minor`) decides what
  reaches subscriber calendars. The previous split was a cost
  optimization that produced inconsistent treatment of serious criminal
  cases (sentencing-memo deadlines, PSR objections, in-limine briefing)
  and an unprincipled difference between siblings of a multi-docket case
  (district criminal + appellate). Surfaced by the
  Anthropic-vs-Gemini-vs-OpenAI provider comparison, where the
  uniformly-zero criminal-deadline column contributed the same constant
  penalty to every model and shaped the ranking around an artifact of
  the auto-detect rather than model quality. Token usage rises modestly
  on criminal cases, which the `llm-tokens` telemetry from 0.7.0 makes
  visible; the per-call prompt-cache hit on the system block keeps the
  delta small after the first call in a sync.
- **`DEADLINE_PROMPT_ADDENDUM` is folded into `SYSTEM_PROMPT`.** The
  deadline-extraction instructions used to be a separate constant
  conditionally appended to the system prompt; they're now part of the
  single merged extractor prompt. The content the LLM sees is preserved
  verbatim, so behavior on dockets that previously had deadlines on is
  unchanged.
- **Three new transcript-handling rules in the deadline portion of the
  extractor prompt.** "ORDER for Transcript" / "Transcript Order" /
  "Order Form" entries are PRIVATE REQUESTS to purchase a transcript
  copy — they are not court orders or deadlines, and the model now
  emits IGNORE for them. Transcript-redaction-request deadlines (e.g.
  "Notice of Intent to Request Redaction due ...") are deadlines but
  procedural — extracted with `significance="minor"` so the audit trail
  keeps them while subscriber calendars don't. Transcript public-release
  deadlines (the date a filed transcript becomes publicly viewable on
  the docket) are substantive — extracted with `significance="major"`.
  Pinned by `tests/test_llm.py::TestSystemPromptTranscriptRules`.

### Removed

- **`extract_deadlines` config field on cases.** The auto-detect it
  overrode is gone, so the field is a no-op. The YAML loader doesn't
  validate unknown keys, so existing configs that still have it
  continue to load — but the comment block describing the auto-detect
  was removed from `config.example.yaml` and the
  `# extract_deadlines: true` template line on the ashtor case is gone.

[0.8.0]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.8.0

## [0.7.3] - 2026-05-27

### Fixed

- **The verify pass left concluded hearings stuck at `scheduled` when the
  proof of occurrence was an older docket entry.** The end-of-sync hearing
  verify pass only saw the 15 most-recent hearing-relevant entries on a
  docket. The record that proves a past hearing happened — a minute entry,
  verdict, transcript, or judgment — is filed on or shortly after the hearing
  date, so on a docket that kept moving afterward it fell outside that window
  and the LLM never saw the evidence it needs to mark the row held. A
  CourtListener cross-check of the provider-comparison stores found
  us-v-mcgonigal's December 2023 sentencing (judgment entered four days later)
  stuck at `scheduled` across all three providers for exactly this reason. The
  verify pass now also feeds the LLM the hearing-relevant entries filed around
  the hearing's own date (anchored on `date_filed`), widening what it can see
  without loosening the bar for marking a hearing held — the prompt still
  requires a cited record.

[0.7.3]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.7.3

## [0.7.2] - 2026-05-26

### Fixed

- **OpenAI calls failed with HTTP 400 on the gpt-5 model family.**
  `case_calendar.llmkit.providers._call_openai` sent `max_tokens`, but the
  gpt-5 family — the default OpenAI tier (`gpt-5.4-nano` for extraction,
  `gpt-5.4` for summaries) — rejects it with
  `unsupported_parameter: 'max_tokens' is not supported with this model. Use
  'max_completion_tokens' instead`. Every OpenAI call returned 400 and fell
  through to the per-caller fallback (extraction → IGNORE, verify → UNCLEAR,
  summary → error), so an OpenAI-configured deployment produced no hearings,
  no deadlines, and no summaries. Because a 400 is never billed, the failure
  was invisible on the OpenAI usage dashboard. Now sends
  `max_completion_tokens`, which the current chat-completions models accept.

- **Gemini case summaries returned "No content" and aborted the summary
  refresh.** Gemini 2.5 "thinking" models draw reasoning tokens from the same
  output budget as the visible answer, and the reasoning scales with prompt
  complexity. On a large summary prompt (tens of thousands of tokens of legal
  documents) the reasoning consumed the entire 800-token summary budget,
  leaving zero answer text — surfaced as `ValueError: No content in Gemini
  response`. `generate_docket_summary` now gives Gemini at least 8192 output
  tokens of headroom so the reasoning and the 2-4 sentence answer both fit.
  Anthropic and OpenAI are unaffected: they stop at the natural end of the
  short summary regardless of the ceiling, so the extra budget costs nothing
  there.

- **A single docket's summary failure aborted the entire `refresh_stale`
  batch.** A transient provider error on one docket (a 503 that exhausts
  retries, or an empty response) propagated out of the per-docket loop and
  skipped every remaining docket's summary for that run.
  `summary.refresh_stale` now isolates each docket: the failure is logged,
  that row is left stale so the next refresh retries just it, and the
  remaining dockets are still summarized.

[0.7.2]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.7.2

## [0.7.1] - 2026-05-26

### Added

- **Per-model subtotals in the end-of-run token / cost summary.** The
  run total used to lump every LLM call into one `TOTAL`, so you
  couldn't tell the cheap extractor track's spend from the higher-tier
  summary track's. `log_summary` now logs a per-model subtotal line
  (calls, tokens, `cost_est`) for every model seen — between the
  per-docket lines and the `TOTAL` — and the `TOTAL` carries a
  `models=N` count:

  ```text
  llm-tokens model=claude-haiku-4-5 calls=32 in=58400 out=1700 cached=52000 cache_write=0 cost_est=$0.0120
  llm-tokens model=claude-sonnet-4-6 calls=5 in=152480 out=310 cached=138400 cache_write=2100 cost_est=$0.0492
  llm-tokens sync TOTAL calls=37 dockets=4 models=2 in=210880 out=2010 cached=190400 cache_write=2100 cost_est=$0.0612
  ```

  Because the extractor and summarizer run on different models, the
  by-model split is the by-track split; the `extraction LLM:` /
  `summary LLM:` lines logged at run start name which model is which. A
  model the price table doesn't cover shows `cost_est=?` on its line.
  Model is the grouping axis (not a domain `extractor` / `summary`
  label) so `case_calendar.llmkit` stays domain-free — it buckets the
  opaque model string the same way it already buckets by docket.

[0.7.1]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.7.1

## [0.7.0] - 2026-05-26

### Added

- **Per-call LLM token telemetry.** Every LLM call now logs its real token
  counts at `INFO` so you can compute actual cost from your provider's
  current prices instead of trusting an estimate. One `llm-tokens call …`
  line per call carries the `purpose` (`extract` / `verify_hearing` /
  `verify_deadline` / `dedupe_hearings` / `summary`), provider, model,
  docket, and `in` / `out` / `cached` / `cache_write` counts; per-docket
  subtotals and a run `TOTAL` are logged at the end of a `sync` or
  `summarize` run. Counts are normalized across providers — `in` always
  means total prompt tokens with the cached portion included (Anthropic
  reports cache reads / writes separately from its input count; OpenAI and
  Gemini fold them in). Log-only: nothing is persisted, so to track spend
  over time you sum the `TOTAL` lines. The long-running `serve` daemon emits
  the per-call lines only (its worker threads and the debounce timer would
  race on a run-total reset).
- **CourtListener request-rate telemetry.** The API client now logs how many
  requests it made when it closes (end of `sync` / `summarize`, or `serve`
  shutdown): `courtlistener-requests total=N peak/min=… peak/hour=…
  peak/day=…`. The peaks are the busiest rolling windows observed, which is
  the number that matters for picking an API tier — tiers are hard ceilings,
  so you need the one that covers your busiest minute / hour / day, not your
  average. Every request that reached the server is counted, including ones
  that came back 429 or 5xx and were retried, since those still spend quota.
  Compare the numbers against your Free Law Project / CourtListener tier's
  limits to see whether you need to upgrade.
- **Optional USD cost estimate on the token-telemetry lines.** Layered on
  top of the exact token counts above, each `llm-tokens call …` line can
  now also carry a `cost_est=` field and the run `TOTAL` accumulates it. The
  estimate prices each token slice (uncached input / cache read / cache write
  / output) at its own published per-million-token rate — the cache split
  matters because the system prompt is cached on nearly every call. It is
  explicitly an estimate, not a bill: the rate table is hand-kept from each
  provider's pricing page and dated (`PRICES_VERIFIED`), and a model that
  isn't in the table (a legacy model, or an `LLM_MODEL` override the table
  doesn't cover) logs `cost_est=?` and flags the run total as partial rather
  than emitting a wrong number. Anthropic, Gemini, and the current OpenAI
  5.4 / 5.5 families are priced at their standard tier; batch discounts,
  long-context (>200k) tiers, and data-residency multipliers are not modeled.

### Changed

- **The case-summary grounding guard now removes ungrounded dates / amounts
  instead of only warning.** When a date or dollar figure in a generated
  summary can't be traced to any source document, the structured-events
  scaffold, or the operator-supplied notes, the pipeline retries generation
  once asking the model to drop it; if the figure survives, the sentence
  carrying it is deterministically removed (falling back to the refusal
  sentence if that empties the summary). A warning is still logged for
  review. Previously this check was warn-only — acceptable for an attended
  run, but a possible fabrication could reach subscribers unread now that
  summaries auto-generate as a service.
- **Documentation now lives at `docs.casecalendar.net`** — the README
  documentation link was updated from the GitHub Pages address. The README
  features list also gained the inline-document-links summary feature and a
  broader description of what triggers a summary auto-refresh (a new charging
  document or a hearing / deadline status change, not only dispositions), and
  the case-summaries cost section documents the new token logging.

### Fixed

- **Corrected a self-contradiction in the architecture docs.** The
  summary-guard section said a prompt rule was too soft to stop a slip, then
  listed "three layers [that] cover the gap" with the prompt rule as the
  first of the three — reframed as one preventive layer plus two
  deterministic backstops, which is what actually covers the gap.
- **README "How it works" diagram** was missing the connector between the
  verify-pass step and the render / push step.

### Internal

- **Provider-agnostic LLM call layer extracted into a `case_calendar.llmkit`
  subpackage.** The cross-provider dispatch (anthropic / openai / gemini),
  provider auto-detection, small/fast model defaults, lazy SDK imports, the
  Anthropic `cache_control: ephemeral` marker, output-truncation detection,
  and the token telemetry now live in `llmkit` (`providers.py` + `usage.py`),
  with no Case Calendar domain knowledge (no court prompts, no hearing /
  deadline shapes) so it can be lifted out as a standalone library later.
  `case_calendar.llm` keeps the domain prompts and entry points and calls
  into `llmkit` through its stable re-exported API. The cost estimator stays
  in the domain layer (`case_calendar/costs.py`) and plugs into the
  price-free ledger via `llmkit.usage.set_price_estimator`, so `llmkit` ships
  no prices. Behavior is unchanged; the llm.py source-link anchors in the
  architecture docs were shifted by the extraction and were corrected.

[0.7.0]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.7.0

## [0.6.0] - 2026-05-25

### Added

- **Inline document links in case summaries.** The per-docket summaries on
  the index page now hyperlink the words themselves, the way a news article
  does — the defendants "**were charged**" links to the indictment, "**pled
  guilty**" links to the plea agreement, "**was sentenced**" links to the
  judgment, and so on. Only the short action phrase is linked — the leading
  verb is kept inside the link ("was charged", not just "charged") and the
  trailing detail (the connecting preposition, the charges, the sentence
  terms, the dollar amounts, the dates) stays as plain text. The
  links land on the supporting document's PDF (CourtListener storage, with
  the Internet Archive mirror as a fallback — the same URL the calendar event
  bodies use). The summary LLM decides which phrase each document supports
  (it is the one that read them), so the feature works for any document the
  pipeline feeds it — primary documents, dispositions, and operator-supplied
  `extra_documents` — not a fixed vocabulary. Each document is shown to the
  model with a prompt-only reference token; the model links a phrase to a
  token, and the pipeline resolves the token to a real URL before storing.
  A token the model invents, or one whose document has no reachable URL
  (paperless minute orders, not-yet-uploaded or sealed PDFs), drops back to
  unlinked prose — so a summary can never link to a document that wasn't in
  the set the model was given. The post-generation truthfulness guards run
  on the prose before links are resolved, so the links don't perturb them,
  and the index page's search box matches the words a reader sees rather than
  the embedded URLs.
- **Index links every CourtListener record of a split docket.** When
  CourtListener stores one logical PACER docket as several `docket_id`
  records (an upstream case-id change — the Akhter `1:25-cr-00307` case is
  three records), the index now shows the docket number once as the primary
  link and lists every record beneath it as a muted, individually-clickable
  "CourtListener records (same docket): 1 · 2 · 3" line. That gives full
  transparency and one-click access to each record (each carries a different
  slice of the docket's entries) without misleading a lay reader into thinking
  the separate CourtListener records are separate dockets or cases — the
  docket number appears once, and the "(same docket)" label plus a tooltip
  make the relationship explicit. Genuinely distinct proceedings (a district
  case and its appeal) still render as separate dockets, as before.

### Changed

- **Document links and downloads now prefer CourtListener over the Internet
  Archive.** Both the URLs surfaced to subscribers (calendar event-body
  `Documents:` links and the new inline summary links) and the bytes the
  pipeline downloads for text extraction now use the CourtListener storage URL
  (`storage.courtlistener.com/<filepath_local>`) first, falling back to the
  Internet Archive mirror (`filepath_ia`) only when CourtListener has no copy.
  This reverses the previous Internet-Archive-first preference: the Internet
  Archive is a downstream mirror of CourtListener today (it once was upstream),
  so it can lag or omit documents, and CourtListener is the authoritative,
  current source. Shared in one helper, `pdf.recap_document_url`.

[0.6.0]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.6.0

## [0.5.1] - 2026-05-25

### Fixed

- **The case-summary grounding guard no longer false-positives on
  operator-supplied facts.** A date or dollar amount an operator provides
  via `aggregation_note` or an `extra_documents` note — which the summary
  model is given and may legitimately cite — was flagged as a "possible
  fabricated fact," because the guard's grounding corpus only included
  document text. The corpus now also includes the aggregation note and the
  `extra_documents` operator notes. Surfaced on us-v-gholinejad, where the
  Fourth Circuit appeal docket (whose own record holds no judgment) cited a
  sentencing date conveyed from the sibling district docket via the
  aggregation note. The guard is not weakened — a date or amount absent
  from the documents, the structured-events scaffold, AND the operator
  metadata is still flagged.
- **Case summaries no longer report a misleading partial financial
  picture.** When a granted restitution order is on the docket but its
  amount isn't legibly extractable — hand-filled / garbled OCR, or the
  order's document not yet uploaded to RECAP (it falls back to the docket
  description, which carries no amount) — the pipeline now detects that:
  the entry's description marks it a restitution order, yet no clean dollar
  figure extracts. It then tells the summary LLM
  (via a `DOCKET FINANCIAL ADVISORY`) to omit specific dollar amounts for
  *all* monetary penalties and say the defendant "was ordered to pay
  restitution." Previously the summary could state the legible figures from
  a separate printed forfeiture order while the (larger, unknown)
  restitution was invisible, which a subscriber would read as the total
  liability. The fixed special assessment is the one exception. The detector
  uses the strict disposition classifier, so it keys only off *granted*
  orders — never a typed *proposed* order attached to a motion. (us-v-chapman.)

### Changed

- **Case summaries omit pointless "we don't know" and speculative
  content.** Two classes of low-value text are now suppressed, in the
  prompt and by the deterministic guard:
  - *Undocumented custody status* is omitted entirely rather than
    announced. When no document establishes whether a defendant has been
    arrested or appeared, the summary now says nothing about custody —
    previously it could emit "X's custody status cannot be determined from
    the available record," which restates what the record doesn't show
    without informing the subscriber. (us-v-jin / us-v-gholinejad.)
  - *Speculative or conditional future outcomes* and routine sentencing
    boilerplate are dropped. A scheduled event keeps its date, but the
    hypothetical consequence clause is removed: "sentencing is scheduled
    for June 3, 2026, at which time X will be remanded to the custody of
    the Bureau of Prisons if a term of imprisonment is imposed" becomes
    "sentencing is scheduled for June 3, 2026." Phrasings like "if
    convicted," "if a term of imprisonment is imposed," and "should the
    court impose" are forbidden. (us-v-martino.)

[0.5.1]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.5.1

## [0.5.0] - 2026-05-24

### Added

- **Post-generation truthfulness guard on case summaries.** A
  deterministic backstop runs on every generated summary, independent of
  the prompt rules (which a model can ignore, and on a brand-new case
  there is no earlier good summary to fall back to). Absence-of-record
  and unsupported custody/flight claims trigger a single regeneration
  with the specific violation fed back to the model; the cleaner attempt
  is kept and a WARNING is logged if any persists (the summary is never
  blocked). Ungrounded dates and dollar amounts — figures traceable to
  neither the hearings/deadlines scaffold nor the source documents — are
  logged for operator review (WARN-only, since formatting variance makes
  that class false-positive-prone). On first real use it caught a
  hallucinated restitution figure that differed from one run to the next.

### Changed

- **Case summaries are now strictly documents-only about custody status.**
  A defendant is described as a fugitive / at large / in custody ONLY
  when a source document establishes it; when the record doesn't, the
  status is stated as unknown rather than inferred from the absence of an
  arrest entry. The summary prompt previously *licensed* that inference,
  which produced false "remains at large" claims on charged-but-not-yet-
  arrested defendants (and would have been flatly wrong for one defendant
  since arrested and extradited).
- **Summaries stay silent on absent scheduling / disposition.** The rule
  against "no hearings have been recorded" / "no disposition has been
  entered" now covers reworded and hedged variants ("...have been filed",
  "the docket does not reflect...", "...in the available record") — a
  docket can be sealed or only partly mirrored, so asserting absence can
  be quietly wrong.
- **Summaries no longer print a dollar figure that isn't legible in the
  documents.** Hand-filled restitution schedules OCR into noise; rather
  than reconstruct a number from garble, the summary states the
  obligation exists without an amount — and omits it *silently*, without
  narrating the OCR limitation ("not clearly legible" misdescribes a
  document that's perfectly legible to a human; the gap is ours, and
  isn't subscriber-facing).

### Fixed

- **Case summaries regenerate when a hearing or deadline changes posture,
  not only when a new document lands.** The end-of-sync verify and dedupe
  sweeps mark hearings/deadlines held / cancelled / rescheduled without a
  new document entry, so the document-only stale trigger missed them and
  a summary could freeze — an oral argument flipped to "held" while the
  prose still read "oral argument is scheduled." `Store.upsert_hearing` /
  `upsert_deadline` now flag the docket's summary stale on a new event or
  a status / date change, the one chokepoint every posture-changing
  mutation passes through. Metadata-only re-saves don't, so there's no
  churn.

### Internal

- Architecture docs (`docs/architecture.md`, `docs/case-summaries.md`)
  and the AGENTS.md design-decision reference updated for the
  posture-change refresh and the truthfulness guard; refreshed the stale
  GitHub line anchors for the runtime prompt constants.

[0.5.0]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.5.0

## [0.4.0] - 2026-05-19

### Added

- **Per-case `tags` list in `config.yaml`.** Each case can now carry
  a list of topical labels (e.g. `[DPRK, IT worker fraud, laptop farm]`,
  `[PRC, China]`, `[Russia, espionage]`, `[defense, AI, LLM]`) that
  surface in two places:
  - **Calendar event descriptions.** Every hearing and deadline emits a
    `Tags: foo, bar` line directly under the event description (above
    the docket-keeping metadata blocks — Judge, Dial-in, Case, Docket,
    Documents, entry IDs). Subscribers scanning a shared cybercrime
    calendar can see at a glance whether each event belongs to the
    DPRK IT-worker conspiracy, a PRC actor case, a NatSec espionage
    matter, etc., with a simple search.
  - **HTML index page.** Tags render as clickable pill `<button>`
    chips under each case row. Clicking a chip appends the tag to the
    global search bar (wrapping multi-word tags in `"quoted strings"`
    so they stay one AND-clause) and re-runs the filter immediately;
    repeat clicks of the same tag are idempotent. Tags also join the
    lowercased `data-search` haystack so typed queries hit them the
    same way they hit case names, docket numbers, and summary prose.
  Tags are deduped case-insensitively at config load (first-seen
  casing wins); whitespace around each label is stripped. Validation
  is loud — non-list, non-string, or empty-string entries fail config
  load with a clear `SystemExit` rather than silently dropping a tag
  the operator expected to see.
- **`docs/configuration.md` Tags subsection** describing the field, the
  two render surfaces, and the case-insensitive dedup + multi-word
  quoting behavior, plus a worked example.
- **`config.example.yaml` worked examples**, one per category:
  - Anthropic v. DOW — `tags: [defense, AI, LLM]`
  - DPRK IT-worker prosecutions (Ashtor, Knoot, Wang ×2, Didenko,
    Jin, Hwa, Chapman) — `tags: [DPRK, IT worker fraud, ...]` with
    case-specific extras like `laptop farm` and `marketplace`
  - Xu Zewei
    — `tags: [PRC, China]`
  - McGonigal — `tags: [Russia, espionage]`

  An inline doc-comment on the first tagged case (Anthropic v. DOW)
  explains the field's behavior to operators copying the example —
  including the point that chip-clicks compose with typed search the
  same way, so searching `DPRK` then `sentencing` narrows the list
  the same way clicking the `DPRK` chip and typing `sentencing` does.

### Internal

- **Shared search tokenizer in the index runtime JS.** The chip-click
  handler and the global search input now share one
  `/"([^"]*)"|(\S+)/g` parser plus a `renderQueryTokens` round-tripper,
  so a multi-word tag added by chip click writes a quoted token to the
  search box that the AND-substring matcher then treats as one
  haystack lookup. Replaces the previous whitespace-only split, which
  would have broken multi-word tags into two stray words on click.
- **`_normalize_tags` boundary helper in
  `case_calendar/calendars/index.py`** mirrors the CLI parser's
  strip+dedupe so `build_calendar_models` reads tags off the raw cfg
  dict in the same shape `_cases_from_config` produces. Avoids a
  cli.py → calendars/index.py dependency while keeping the two render
  paths consistent.
- **100% line + branch coverage maintained.** 23 new tests across
  `test_description.py` (tag placement under notes, empty/blank-tag
  filtering, row-dict surfacing), `test_cli.py` (parser positive +
  validation cases, emit-threading into ICS), and `test_index.py`
  (chip rendering, data-tag escaping, normalize-tags edges, search-
  haystack inclusion, build_calendar_models propagation).

[0.4.0]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.4.0

## [0.3.4] - 2026-05-19

### Fixed

- **Hearing-typed actions carrying a `deadline_key` (and vice versa)
  are now coerced to the correct category instead of being silently
  dropped.** Production failure shape (us-v-ding, 2025-07-11 government
  status-report reiteration): the LLM emitted `{"type":
  "UPDATE_DETAILS", "deadline_key": "govt-status-report", ...}` — a
  hearings-only action type carrying a deadline-shaped payload. The
  dispatch in `CaseSyncer.process_entry` routed by `type` to
  `_apply_action`, which then logged `action without hearing_key` and
  dropped the action from the audit trail. New `_normalize_action_category`
  trusts the key (the more specific signal — the model had to know
  about that exact row to use its key) and rewrites the type to its
  other-category equivalent: `UPDATE_DETAILS` → `RESCHEDULE_DEADLINE`
  (no `UPDATE_DETAILS_DEADLINE` exists; deadlines have a simpler shape
  with no judge / courtroom / dial-in to update), `ADD` ↔ `ADD_DEADLINE`,
  `RESCHEDULE` ↔ `RESCHEDULE_DEADLINE`, `CANCEL` ↔ `CANCEL_DEADLINE`,
  `MARK_HELD` ↔ `MARK_FILED`. Logs at INFO so the prompt-violation rate
  stays visible. Actions with both keys present, no keys, or unknown
  action types pass through unchanged so future failure modes remain
  visible. Pairs with the prompt-side rule in `DEADLINE_PROMPT_ADDENDUM`.

### Changed

- **`DEADLINE_PROMPT_ADDENDUM` now states "no `UPDATE_DETAILS` for
  deadlines" explicitly, with two examples for the model.** When an
  order merely reiterates an existing deadline with the same date and
  time, emit `IGNORE` (the deadline is already in `known_deadlines`;
  restating it doesn't change anything we render or persist). When the
  date OR time changes — including the common case of a date-only
  deadline gaining an explicit time — emit `RESCHEDULE_DEADLINE` on the
  existing key. The hearings-side `UPDATE_DETAILS` exists because
  hearings have judge / courtroom / dial-in fields that can change
  without the date moving; deadlines don't have those fields, so the
  same action type isn't needed.

### Internal

- **Three-way provider dispatch consolidated into one
  `_dispatch_llm_call(provider, system, user, max_tokens, *, model,
  json_mode)` helper.** Previously inlined identically in
  `extract_actions`, `_call_lm_and_parse`, and `generate_docket_summary`
  — same `if provider == "anthropic" ... elif "openai" ... else
  "gemini"` if/elif/else, repeated three times. Now one helper; the
  per-provider call functions still own their SDK quirks (truncation
  signal detection, json-mode kwargs, model-default selection).
  `OutputTruncatedError` and other exceptions still propagate so
  callers convert them into their own caller-specific fallback shape
  (IGNORE list vs UNCLEAR dict vs raise). The `llm.py` module shrunk
  by net statements; test fakes for the per-provider call functions
  picked up a `**kw` (or explicit `model=None` / `json_mode=True`)
  parameter to match the real signatures the helper threads through.

## [0.3.3] - 2026-05-19

### Fixed

- **LLM responses with malformed JSON now recover via `json_repair`
  instead of dropping the entry to IGNORE.** Haiku occasionally emits
  unescaped `"` characters or stray newlines inside a long `notes`
  string, terminating the JSON value early and surfacing as
  `json.JSONDecodeError: Expecting ',' delimiter` in the warning log —
  the action that triggered it (a real MARK_HELD on the us-v-ding
  Daubert hearing) was silently dropped despite carrying perfectly
  recoverable identity fields (`type`, `hearing_key`, `local_date`).
  `_parse_actions` now runs `json_repair.repair_json` on parse failure
  and uses the repaired dict when it carries an `actions` key. The
  recovered action goes through the rest of the pipeline normally; the
  WARNING line names "recovered via json_repair" so the failure rate is
  still visible in logs. Pairs with the 0.3.2 `OutputTruncatedError`
  path, which catches the orthogonal truncation case at the provider
  level — between the two, the only failures that still IGNORE are
  responses too broken even for json_repair.

### Changed

- **`notes` formatting rules tightened in the system prompt.** The
  hearings-prompt notes guidance now spells out the JSON-safety
  invariants the model has been violating in production: at most ~200
  chars, no unescaped `"` inside the string (with an explicit
  paraphrase example), no literal newlines / tabs / control characters.
  The matching deadline-prompt rule (verbatim trigger language on
  conditional deadlines) is relaxed from "VERBATIM" to "as close to
  verbatim as the JSON-safety rules allow", with paraphrase encouraged
  when the original would carry a `"` or run long. The action-schema
  comment on the `notes` field is updated inline so the rule is
  visible where the model declares the value. These prompt changes are
  the upstream fix for the malformed-JSON class of failures; the
  json_repair fallback above is the belt-and-suspenders downstream.

### Internal

- New runtime dependency: `json-repair>=0.30` (MIT, pure-Python, no
  transitive deps). Lazy-imported inside `_try_json_repair` so it
  only loads on parse failure.

## [0.3.2] - 2026-05-19

### Fixed

- **LLM output truncation at `max_tokens` is now detected explicitly
  and reported with a clear failure reason.** A complex docket whose
  briefing schedule touches dozens of known deadlines could push the
  per-entry extractor past its 2048-token output cap, leaving the
  JSON response cut off mid-string. The user-visible symptom was a
  confusing `json.JSONDecodeError: Unterminated string` WARNING in
  logs (observed on entry 461544129 with `known_deadlines=32` on a
  joint stipulation touching multiple briefing rows) — the
  RESCHEDULE_DEADLINE action that triggered it fell through to the
  IGNORE-on-failure path, and the operator had no way to tell
  truncation from genuinely malformed model output. New
  `OutputTruncatedError` carries the partial text and the cap; each
  per-provider call function (`_call_anthropic`, `_call_openai`,
  `_call_gemini`) checks the provider-native truncation signal —
  Anthropic's `stop_reason="max_tokens"`, OpenAI's
  `finish_reason="length"`, Gemini's candidate
  `finish_reason.name == "MAX_TOKENS"` — and raises it before
  returning. Both `extract_actions` and the shared `_call_lm_and_parse`
  used by verify / dedupe catch the new exception, log a single named
  WARNING (no traceback, no confusing JSON-parse error), and return
  an IGNORE / UNCLEAR with `reason="llm output truncated"` so log
  greps can distinguish truncation from "llm call failed" and
  "json parse error".

### Changed

- **`extract_actions` default `max_tokens` raised from 2048 to 8192.**
  The previous cap was tight enough that one complex docket with a
  briefing schedule touching 30+ known deadlines could legitimately
  exceed it. 8192 leaves headroom for any extraction-shaped response
  without affecting verify / dedupe calls (still 512 tokens each) or
  the summary track (independent default).

## [0.3.1] - 2026-05-19

### Fixed

- **Substance documents filed as attachments on procedural parent
  entries are now picked up — primaries AND dispositions.** When the
  substantive document (indictment, complaint, plea agreement,
  memorandum opinion, judgment, etc.) is filed as an attachment to a
  procedural parent entry whose description doesn't head-match the
  matcher regex, the entry-level classifier used to return False and
  the summary pipeline emitted the "no primary document identified"
  refusal. The us-v-stryzhak (`1:25-cr-00381`, E.D.N.Y.) docket was
  the trigger case — entry 1 was a "CONSENT TO TRANSFER JURISDICTION
  (Rule 20)" filing with the indictment as `attachment_number=1`. The
  same shape occurs whenever ANY substance document is filed as an
  exhibit on a procedural parent: Rule 20 transfers, motions to
  seal/unseal an attached charging document, "Notice of Filing of
  Plea Agreement" parents with the plea agreement as an attachment,
  parent orders with memorandum opinions filed as separate
  attachments. `is_primary_document`, `is_disposition`, and
  `_is_disposition_document` now ALL return True when the entry's head
  OR any of its recap_documents' descriptions matches the relevant
  predicate, and `_entry_doc_text` prioritizes substance-marked
  attachments over the parent's main doc so the summary LLM gets the
  actual document body rather than the procedural wrapper. The
  detection / extraction logic is factored as one pair of generic
  helpers (`_entry_matches(entry, predicate)` and
  `_recap_documents_matching(entry, predicate)`) plus per-type
  predicate functions and a `_SUBSTANCE_PREDICATES` tuple — adding a
  new document type is one predicate definition, no parallel
  per-type entry classifier or extractor branch needed.
- **PDF text extraction no longer bails on a stale `is_available=False`
  flag.** The cached recap_document flag can drift behind upstream
  state (the PDF lands on RECAP, CourtListener flips `is_available`
  to True and populates `filepath_local`, but our sync hasn't refetched
  the entry yet). The previous gate at the top of `pdf.extract_text`
  returned None when `is_available=False` without ever attempting
  `fetch_pdf_bytes`, so pypdf and OCR never ran on documents we could
  perfectly well have read. `is_sealed` remains the only hard "don't
  bother trying" gate; otherwise the pipeline always attempts the
  fetch and `fetch_pdf_bytes` itself returns None cleanly (no HTTP
  round-trip) when neither `filepath_ia` nor `filepath_local` is
  populated. The us-v-lytvynenko (`3:23-cr-00088`, M.D. Tenn.)
  indictment was the canonical case — sync had cached the recap_doc
  pre-upload, the polling cutoff hadn't refetched the entry, and the
  pipeline bailed on the stale flag despite the PDF being live on
  CourtListener's storage URL the whole time.
- **Once we download a PDF, the result comes from our pipeline, not
  from CourtListener's `plain_text`.** The previous final return in
  `pdf.extract_text` was `return text or plain or None` — falling
  back to CourtListener's plain_text after our local pypdf produced
  unusable output. Both extractions ran the same upstream pypdf pass,
  so the fallback was re-injecting exactly the garbage we'd rejected
  at the top of the function and feeding the summary LLM a worse-
  than-OCR version of the same document. After a successful fetch
  the return is now `text or None`. The fallback to plain_text is
  preserved only when the fetch itself failed (we never got to run
  our pipeline).
- **Distinct subscriber-facing messages for the four "no extractable
  text" failure modes.** Replaces the previous catch-all
  "Documents available for this docket are insufficient to generate a
  reliable summary" with four state-specific strings, picked by
  inspecting each identified primary entry's main recap_document:
  `SUMMARY_PRIMARY_DOCUMENT_SEALED` ("currently sealed") when all
  primaries have `is_sealed=True`;
  `SUMMARY_PRIMARY_DOCUMENT_NOT_AVAILABLE` ("not yet available on
  RECAP") when all primaries have no fetchable source;
  `SUMMARY_PRIMARY_DOCUMENT_UNREADABLE` ("could not be read") when the
  pipeline had something to work with but couldn't produce usable
  text, and as the catch-all for mixed states;
  `SUMMARY_INSUFFICIENT_DOCUMENTS` when no entry matched
  `is_primary_document` at all. Each is operator-actionable in a
  different direction (wait for unseal / wait for upload /
  investigate OCR tools / check sealing posture or add
  `extra_documents`). Subscribers no longer see "could not be read"
  on docks where the actual state is "currently sealed."
- **Audit pass on log messages across the project.** Every catch-all
  failure log that conflated distinct subcauses the code already had
  visibility into now logs specifically: `pdf.extract_text` emits
  separate INFO / WARNING lines for sealed / not-fetchable /
  fetched-but-pipeline-failed; `pdf.fetch_pdf_bytes` and
  `fetch_url_bytes` non-200 logs carry an HTTP category label ("not
  found" / "access denied" / "rate limited" / "client error — won't
  retry" / "server error — retry next sync"); exception logs across
  the project include the exception type so DNS / TLS / read-timeout
  / connection-error are distinguishable; `summary._fetch_extra_docu‐
  ments` drop log points operators at the per-URL log line for the
  actual cause; `summary.find_primary_documents` adds an outcome log
  after the CourtListener fallthrough so the "falling through to
  refresh" trail isn't left hanging; `sync._ensure_court`,
  `courtlistener._request`, and `alerts.ensure_docket_alerts` all
  carry richer per-failure-mode classification.

### Added

- **Both extraction LLM and summary LLM are logged at command
  startup.** The previous single `LLM: provider=... model=...` line
  named the extraction-track config only — fine when both tracks ran
  on the same model, but the project now uses distinct providers and
  models per track (Haiku for per-entry extraction, Sonnet for case
  summaries) and the single line silently misled operators about
  which model produced summary text on a given docket. A new shared
  helper `cli._log_llm_setup` is called by `cmd_sync`, `cmd_serve`,
  and `cmd_summarize` and emits both lines, plus an explicit
  "case_summaries.enabled=false — case summaries will not regenerate
  this run" note when summaries are off so operators don't have to
  cross-reference the config. New `llm.summary_provider_info` mirrors
  the existing `provider_info` for the summary track and applies the
  same precedence chain `generate_docket_summary` uses at call time,
  so the log can't drift from the runtime resolution.

### Changed

- **`pdf.looks_garbled` renamed to `pdf.is_usable_text` and inverted
  to a positive predicate.** The function checks more than just
  garbled-ness now (length floor + font-encoding gibberish +
  PACER-stamps-only), so the old name was underselling what it does.
  Inverting also lets callers consolidate the recurring
  `len(text) >= _MIN_USEFUL_CHARS and not looks_garbled(text)` pairing
  into a single `is_usable_text(text)` call that reads more naturally.
  Minor behavior change: short non-empty `plain_text` in
  `extract_text` now falls through to the PDF fetch instead of being
  returned directly (a 50-char stub like "INDICTMENT" isn't useful as
  a primary document body, and if the fetch succeeds we get something
  better; if it fails we still return the short plain_text via the
  fetch-failed fallback).
- **CLI argparse errors now print the full help text of the relevant
  subcommand alongside the error message.** Stock argparse prints only
  a one-line usage summary plus the error ("unrecognized arguments:
  --foo"), which for a project with this many subcommand flags didn't
  show the real options — operators who typo'd a flag had to re-run
  with `--help` to discover what they meant. A custom
  `_HelpfulArgumentParser` writes the relevant subparser's help first
  (auto-located when the typo is on a subcommand flag like
  `case-calendar sync --sumarize`), then the error, then exits with
  code 2 — same exit semantics as before, much more useful UX.

### Internal

- **Verify / dedupe LLM call+parse and recent-entries format
  centralized.** The single-action LLM call + JSON parse + actions-
  unwrap + type-validate sequence in `verify_hearing`,
  `verify_deadline`, and `resolve_duplicate_hearings` was ~30 lines
  of copy-paste per caller. Now in one `_call_lm_and_parse(provider,
  system_prompt, user_message, max_tokens, label)` helper that
  guarantees the returned dict carries a `type` field (UNCLEAR
  fallback on any failure). The "RECENT DOCKET ENTRIES (newest
  last)" block in the three corresponding message builders was
  also identical line-for-line; extracted to
  `_format_recent_entries(recent_entries) -> list[str]`. The
  llm.py module shrunk by ~130 lines net; future LLM-call changes
  (token tracking, retry shapes, new model quirks) land in one
  place.
- **Line-number anchors in `docs/architecture.md` refreshed.** The
  internal refactors shifted line numbers inside `llm.py`; the four
  affected `#L<n>` deep-links into the prompt-constants section
  (`VERIFY_SYSTEM_PROMPT`, `VERIFY_DEADLINE_SYSTEM_PROMPT`,
  `DEDUPE_HEARING_SYSTEM_PROMPT`, `SUMMARY_SYSTEM_PROMPT`) updated
  to their new lines.
- **100% line + branch coverage restored.** The abstractions and
  new helpers left 10 lines and 5 branches uncovered; 20 new tests
  across the touched modules close every gap (full suite at 1169
  tests, all green).

## [0.3.0] - 2026-05-18

### Added

- New top-level config flag `ensure_docket_alerts` (default true) and
  new module `case_calendar.alerts`. On every `case-calendar sync` and
  `case-calendar serve` startup, the project now lists the
  authenticated CourtListener account's existing docket-alert
  subscriptions and POSTs new ones for any docket configured under
  `cases:` that isn't already covered. Adding a case to `config.yaml`
  automatically wires up its docket alert on the next sync, so
  webhook deliveries start flowing without the manual "click Get
  alerts on each docket page" step the README used to require.
  Reconcile is one-way (it adds missing subscriptions but never
  deletes stale ones); per-docket failures log at WARNING and don't
  abort sync/serve; a full list-call failure marks every docket
  `'failed'` and skips creates to avoid spamming duplicates against
  an unknown baseline. Set `ensure_docket_alerts: false` to opt out
  if you maintain subscriptions through some other surface.
- `CourtListener.iter_docket_alerts()` and
  `CourtListener.create_docket_alert(docket_id)` expose the new
  endpoints. Both share the same retry / rate-limit machinery as the
  GET methods via a new private `_request(method, url, ...)` that
  `_get` and `_post` delegate to.

### Fixed

- `find_primary_documents_for_group` no longer drops the populated copy
  of a logical PACER entry when one CourtListener sibling carries it
  with empty `plain_text` while another sibling in the same group has
  the extracted body. The dedup was "first-seen wins, freshest CourtListener
  docket_id first," which silently discarded the good copy whenever
  the freshest sibling happened to have the empty one. The us-v-schmitz
  indictment (`1:24-cr-00234`, D.N.J.) was the canonical instance —
  freshest CourtListener sibling 73292090's recap_document had `plain_text=""`,
  while older sibling 73353898 carried 20 KB of text; the summary LLM
  received metadata only and emitted the "insufficient documents"
  refusal. The dedup now upgrades the first-seen entry when a later
  sibling's copy has populated `plain_text` on its main recap_document
  and the prior copy doesn't, for both primary documents and
  dispositions. No extra PDF reads — the choice is between copies
  already in hand.
- The single-docket cache-staleness check inside
  `find_primary_documents` now detects stale disposition entries the
  same way it detects stale primaries. A stored disposition whose
  available main recap_document has empty `plain_text` triggers the
  CourtListener fallthrough and `Store.refresh_entry_recap_documents`
  rebuild — the previous code only caught the primary case (the
  us-v-moucka shape) and would silently return a stub disposition.
  Renames the staleness helper to `_cached_entries_look_stale`
  (generic) and splits the per-entry signature into
  `_entry_looks_stale`. Only the entries that look stale are dropped
  from the cache view; fresh ones stay so the CourtListener fallthrough doesn't
  re-fetch their text unnecessarily.
- `pdf.looks_garbled` now also flags PACER-page-header-only output,
  not just font-encoding gibberish. Image-only scans with a thin OCR
  overlay on the page-header band let pypdf read several KB of clean
  ASCII off every page, but the document body never reaches the
  caller — the alpha-ratio gate passed trivially (page stamps are
  mostly letters and digits) and the OCR fallback never ran. The
  us-v-schmitz indictment was the canonical case: pypdf returned 1538
  chars of pure header stamps from an 18-page scan that OCRs cleanly
  to 20 KB of real body text. The detector now strips the standard
  PACER stamp pattern (`Case <docket> Document <n> [Filed <date>]
  [Page <i> of <n>] [PageID:/Page ID #: <id>]`) and treats the result
  as useless if less than 100 chars of body survive — same caller
  contract as the gibberish check, fall through to the next stage.
  The two failure modes are now documented side-by-side in the
  expanded AGENTS.md "Garbled `plain_text`" design note.

### Changed

- AGENTS.md gains a new "Docket alerts are reconciled automatically"
  key design decision and matching architecture entries for the new
  `case_calendar/alerts.py` module and the extended CourtListener
  client. The existing "Entry dedup across a docket group" and
  "Automatically rebuild stale cached recap_documents" rules now
  document the upgrade-on-better-text dedup and the
  disposition-staleness sweep respectively, with us-v-schmitz
  documented as the canonical case alongside the existing us-v-moucka
  reference.
- `docs/webhooks.md` step 6 ("Subscribe to docket alerts") rewritten:
  the manual "click Get alerts on each docket page" instructions are
  replaced with a description of the automatic reconciler, the
  opt-out flag, and the per-run log line.

[0.3.4]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.3.4

[0.3.3]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.3.3

[0.3.2]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.3.2

[0.3.1]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.3.1

[0.3.0]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.3.0

## [0.2.7] - 2026-05-18

### Changed

- Reverted the 0.2.6 stdlib-HTTP migration. `case_calendar.courtlistener`,
  `case_calendar.pdf`, and `case_calendar.url_validator` are back on
  `httpx`, and `httpx>=0.28.1` is restored as a direct dependency. The
  ergonomic-API + connection-pooling benefits of `httpx` won out over
  the "one fewer direct dep" simplification the 0.2.6 entry traded
  them for; the project's HTTP layer is small enough that the
  trade-off can go either way, and `httpx` was the original choice.
- The 0.2.6 `HTTPStatusError` shim in `case_calendar.courtlistener` is
  gone; callers catch `httpx.HTTPStatusError` again, as they did
  before 0.2.6.
- The MockTransport-based test infrastructure for the three migrated
  modules is restored, replacing the temporary
  `urllib.request.urlopen` monkey-patch shape that shipped in 0.2.6.

### Removed

- The `urllib`-based HTTP code paths introduced in 0.2.6 (the
  `_Response` / `_FetchResult` / `_ValidateResponse` duck-typed
  wrappers, the `_RETRYABLE_TRANSPORT_EXCEPTIONS` tuples, and the
  hand-rolled retry loops built around `urllib.request.urlopen`).

[0.2.7]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.2.7

## [0.2.6] - 2026-05-18

### Changed

- Dropped the direct `httpx` dependency from the three modules that
  made HTTP calls — `case_calendar.courtlistener`,
  `case_calendar.pdf`, `case_calendar.url_validator` — and replaced
  it with `urllib.request` / `urllib.error` / `http.client` from the
  stdlib. The previous 0.2.5 entry had already dropped
  `httpx-retries`; this is the rest of the same simplification, on
  the rationale that for THIS workload (one CourtListener host
  dominating HTTP traffic, per-entry LLM round-trips dominating
  end-to-end latency) the keep-alive / ergonomic-API benefits of
  `httpx` don't outweigh keeping one less library in the project's
  direct-dep surface. The transitive `httpx` install is unchanged —
  `anthropic`, `openai`, `google-genai`, and `msgraph-sdk` all use
  it internally for their own HTTP needs.
- Behavior is preserved across all three call sites:
  - `CourtListener._get` keeps its 6-attempt retry loop for
    429/5xx, the `_RETRY_AFTER_BUFFER_SECONDS` buffer, the
    `_no_request_before` cross-request cooldown, and the separate
    `_TRANSPORT_RETRY_BUDGET` for transient transport failures.
    The retryable-exception set now uses the stdlib equivalents
    (`urllib.error.URLError`, `socket.timeout`,
    `http.client.HTTPException`, `ConnectionError`) rather than
    the old `httpx.{TimeoutException, NetworkError, RemoteProtocolError}`.
    Redirects are followed by the default `HTTPRedirectHandler`,
    matching the previous `follow_redirects=True` setting.
  - `pdf._get_with_retry` keeps the same status-code-retry set
    (429/502/503/504) and the same `_PDF_RETRY_TOTAL` budget.
  - `url_validator._request_with_retry` keeps the same narrow
    retryable-exception set and the same fail-open semantics on
    non-retryable errors and malformed URLs.
- A new `case_calendar.courtlistener.HTTPStatusError` replaces the
  previous `httpx.HTTPStatusError` as the surface exception raised
  by `_get` on exhausted retries or non-429 4xx responses. Carries
  the same fields callers used (`status_code`, `body`, `url`).
- `validate_url` no longer accepts a `client=` parameter — there's
  no shared HTTP client to inject when using stdlib `urlopen`. The
  one caller (`case_calendar.sync`) already invoked it without a
  client, so no breaking call-site impact.

### Removed

- Direct dependency on `httpx`.

### Tests

- The MockTransport-based test infrastructure for the three migrated
  modules is replaced with a `urllib.request.urlopen` monkey-patch.
  Each module's test file defines a tiny `_FakeResp` (status, body,
  context-manager protocol) and a `_http_error` helper for
  simulating 4xx/5xx responses. The production retry loops run
  end-to-end against these stubs; coverage on all three files is
  100%.

[0.2.6]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.2.6

## [0.2.5] - 2026-05-18

### Security

- The `webhook-url --check` health probe no longer interpolates the
  secret-bearing URL into operator-facing failure messages. Previous
  diagnostics included the full URL (and any echoed body) on stderr
  for HTTPError / URLError / non-200 / non-JSON / wrong-service
  paths, so an operator who pasted a failing run into a bug report
  or chat would expose the receiver secret. The 5 health-check
  prints now use a generic `webhook health endpoint` label that
  doesn't flow from the secret at all (CodeQL's `py/clear-text-
  logging-sensitive-data` data-flow analysis flags any string
  derived from the secret-named local, so masking via `.replace()`
  doesn't sanitize — severing the chain via a literal placeholder
  does). Response bodies are still passed through a new
  `_redact_secret` helper so an upstream proxy that echoes the
  request path can't leak the secret through that channel either.
  The `webhook-url` command's primary stdout output of the full URL
  is unchanged — that's the command's contract (operator pastes it
  into the CourtListener webhook dashboard) — but a stderr banner
  now flags the line as sensitive so it doesn't end up in
  screenshots or bug reports by accident. Resolves the five
  `py/clear-text-logging-sensitive-data` CodeQL alerts on the
  health-check paths (alerts #2-#6); the remaining alert on the
  primary `print(url)` (alert #1) is intended functionality and was
  dismissed with rationale "false positive — primary stdout output
  of the webhook-url command; the URL embeds the secret by design
  so it can be pasted into the CourtListener webhook dashboard."

### Fixed

- `CourtListener._get` now sees and logs 429 responses instead of
  silently sleeping at the transport layer. The 0.2.3 wiring of
  `httpx-retries` was configured with `Retry(status_forcelist=[])`
  intending to disable status-code retries — but the library treats
  an empty list as falsy and falls back to its default
  `{429, 502, 503, 504}`, so `RetryTransport` was intercepting 429
  responses (including the daily-bucket Retry-After ~24h case) and
  running its own `time.sleep` before the response ever reached
  `_get`. That bypassed `_get`'s 429 warning log (URL / body /
  rate-limit headers), the cross-request `_no_request_before`
  cooldown barrier, and the `_RETRY_AFTER_BUFFER_SECONDS`
  clock-drift buffer — operators saw "hang" instead of "rate
  limited" and could not see which bucket fired. The hang reproduced
  on a fresh-DB backfill that exhausted the daily quota mid-sync;
  symptom was the sync producing no log output for hours, and the
  index page rendering with no case summaries and naked docket-id
  numbers (without links) for cases whose dockets were never
  fetched.

### Changed

- Dropped the `httpx-retries` dependency entirely. The decision in
  0.2.3 was to use the library for transport-error retries; the
  silent-429 bug above made it clear the library's API edge
  (the `or RETRYABLE_STATUS_CODES` fallback) was a sharp tool for
  the CourtListener client specifically, where `_get` already has
  its own response-status retry loop, and using both layers risked
  cascade misconfiguration. Replaced with inline retries handled by
  the same code paths that already retry 429 / 5xx:
  - `courtlistener._get` now catches
    `(httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)`
    inside its existing for-loop, with a separate
    `_TRANSPORT_RETRY_BUDGET` of 5 attempts.
  - `pdf.fetch_pdf_bytes` and `pdf.fetch_url_bytes` go through a
    new `_get_with_retry` helper that retries transport exceptions
    AND the same gateway-style status set (`429, 502, 503, 504`)
    that the library's default covered.
  - `url_validator._check` calls a new `_request_with_retry` that
    retries the narrow transport-exception set; non-retryable
    `httpx.RequestError` subclasses fail through to "flake" on
    the first hit as before.
  Behavior on transient transport blips is preserved across all
  three call sites; the visible change is the missing 429
  silent-sleep regression and one fewer dependency.

### Added

- `tests/test_courtlistener.py::TestTransportErrorRetry::test_429_response_reaches_get_logging_and_cooldown`
  pins the 429-visibility regression: the test exercises the
  production transport stack and asserts the warning log fires, the
  `_no_request_before` cooldown advances, and the first sleep equals
  `Retry-After + _RETRY_AFTER_BUFFER_SECONDS`. Confirmed to fail on
  the 0.2.4 codebase before the fix.
- Five tests in `tests/test_cli.py` pin the secret-redaction
  contract on every `webhook-url --check` failure path (HTTPError,
  URLError, non-200, non-JSON 200, wrong-service 200, and a
  body-echoes-URL path that proves the secret is redacted from the
  response body even when an upstream proxy echoes the request URL
  back). Each test uses a distinctive non-trivial secret
  (`secret-abc123-do-not-leak`) and asserts the exact string is
  absent from stderr while `<REDACTED>` is present.

### Removed

- Dependency on `httpx-retries`.

### Refactor

- `pdf._get_with_retry` and `url_validator._request_with_retry`
  switched from `for attempt in range(N+1):` to `while True` with an
  explicit attempt counter, so every exit is a `return` and there's
  no loop-fall-off branch for coverage to flag as unreachable.
  Behavior unchanged; patch coverage on both files rises from
  partial-branch to 100%.

[0.2.5]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.2.5

## [0.2.4] - 2026-05-18

### Fixed

- `_parse_actions` no longer fails with
  `json.JSONDecodeError: Extra data: line 21 column 1` when the LLM
  returns a valid actions object followed by trailing content
  (a second JSON object, narrative commentary, or stray braces in
  prose). The previous implementation sliced the response from the
  first `{` to the LAST `}` and fed that to `json.loads`, which
  swept any trailing JSON or punctuation into the parse input and
  blew it up. Switched to `json.JSONDecoder().raw_decode()` so we
  parse exactly one JSON object starting at the first `{` and
  ignore anything past its closing brace. Observed in production
  logs on a Ding motion-hearing extraction; the parse failure
  caused the entry to fall through to the IGNORE-on-failure path
  and the reschedule was silently dropped. (#8)

[0.2.4]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.2.4

## [0.2.3] - 2026-05-18

### Fixed

- HTTP clients now retry transient transport-level failures
  (`httpx.ReadTimeout`, `ConnectError`, `RemoteProtocolError`,
  etc.) at the transport layer via the `httpx-retries` package's
  `RetryTransport`. Before this, a single read timeout on a
  CourtListener API call mid-`sync` would propagate up through
  `iter_entries` and kill the whole run (the production traceback:
  `httpx.ReadTimeout: The read operation timed out` aborting
  `sync_case`). The same class of failure could also blow away PDF
  fetches and `extra_documents` URL fetches without retry. Now
  applied to all four httpx clients in the project — `CourtListener`,
  `pdf.fetch_pdf_bytes`, `pdf.fetch_url_bytes`, and
  `url_validator` — with per-client retry budgets sized to the
  call site (5 attempts with 0.5s base backoff on the CourtListener
  and PDF fetches; 3 attempts with 0.25s base on the
  hot-path URL validator). The library handles jitter automatically.
  CourtListener's existing 429 / 5xx retry loop in `_get` is
  preserved unchanged — the library is configured with
  `status_forcelist=[]` on that client so its retry covers
  transport errors only, leaving the cross-request cooldown and
  multi-hour `Retry-After` honoring intact. LLM SDKs (anthropic,
  openai, google-genai) already retry transient network errors
  via their own `max_retries` settings, so no change there.

### Added

- New dependency: `httpx-retries>=0.5.0` — narrow-scope library
  wrapping `httpx.BaseTransport` with configurable retry policy
  for transient errors.

[0.2.3]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.2.3

## [0.2.2] - 2026-05-18

### Fixed

- The CourtListener HTTP client now follows redirects. httpx
  defaults to `follow_redirects=False` (unlike `requests`), and the
  CourtListener client was the only one of the project's four
  httpx clients that hadn't overridden the default — so a 301/302
  from CourtListener would surface as an
  `httpx.HTTPStatusError: Redirect response '302 Found'` instead of
  transparently landing on the redirected URL. The CourtListener
  endpoints we currently use don't redirect, but a future hostname
  migration, HTTPS upgrade, trailing-slash normalization, or path
  reshape would otherwise silently break the whole sync flow. The
  PDF fetch chain in `pdf.py` and the URL validator already set
  `follow_redirects=True`; the CourtListener client now matches. (#6)

[0.2.2]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.2.2

## [0.2.1] - 2026-05-18

### Fixed

- `case-calendar sync` no longer silently drops entries when the
  process is interrupted by Ctrl+C or `SystemExit` mid-iteration on
  a docket. The per-docket entry loop was wrapped in
  `try / except Exception / finally` where the finally advanced
  `dockets.date_last_modified` to the docket's CourtListener-side
  `date_modified` whenever `iterated_ok` stayed True.
  `KeyboardInterrupt` and `SystemExit` are `BaseException`
  subclasses, not `Exception` subclasses, so the `except Exception`
  clause never caught them — the finally fired with the flag still
  True and advanced the cutoff even though only some of the new
  entries had been processed. The next sync's docket-level
  short-circuit then saw `stored_last_modified == cl_date_modified`,
  skipped the docket, and the unprocessed entries past the
  interrupt point became permanently invisible until CourtListener
  bumped the docket again (a future filing or metadata change).
  AGENTS.md documented the invariant (`the docket last-modified
  cutoff is only advanced on a clean run`) — the implementation now
  matches it. The try/except/finally is gone; the cutoff bump sits
  in linear control flow after the loop so any exception, including
  BaseException subclasses, propagates past it without advancing
  the cutoff. **Operator recovery for a previously-interrupted
  sync:** the in-progress docket's cutoff may have been bumped under
  the old code; if you suspect entries were dropped, identify the
  in-progress docket from the previous run's logs (last
  `Syncing docket N for case Y` line) and roll its cutoff back with
  `UPDATE dockets SET date_last_modified = NULL WHERE docket_id = N;`
  — the next sync will re-walk it. Fingerprint dedup ensures
  already-processed entries cost nothing on re-walk; only the
  genuinely-unprocessed new entries pay LLM tokens. (#4)

### Changed

- Codecov `patch` target tightened from `90%` to `auto` (matches
  the base commit's project coverage). The 90% threshold was loose
  enough that PR #3 merged with patch coverage of 93.83% and left
  14 uncovered branches in newly-added code; `auto` catches that
  class of gap at PR time instead of in follow-up work. Trade-off:
  very small PRs are forced toward 100% diff coverage — the
  AGENTS.md "unreachable defensive code is a test smell"
  convention is the documented escape hatch. (#5)

### Internal

- Coverage cleanup pinning the lines Codecov flagged on PR #3 that
  weren't addressed before merge: `_group_dockets_on_case`
  sibling-dedup branch (`summary.py` line 1246); group-aware
  `case_summaries` handling in `count_docket_rows` and
  `delete_docket` (`store.py` 1447→1453 and 1487→1494); sibling
  merge in `build_calendar_models` (`index.py` 848-850);
  `_arm_debounce` no-metadata and sibling-dedup `continue`
  branches (`cli.py` 657 and 660). Nine new tests across
  `tests/test_summary.py`, `tests/test_store.py`,
  `tests/test_index.py`, and `tests/test_cli.py`. (#5)

[0.2.1]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.2.1

## [0.2.0] - 2026-05-17

### Changed

- `case_summaries` is now keyed by the logical PACER docket
  `(case_id, docket_number, court_id)` rather than by the CourtListener
  `docket_id`. CourtListener's reconciler can split one logical PACER
  docket across multiple `docket_id` rows when the upstream
  `pacer_case_id` changed mid-life (see
  [CourtListener issue #7345](https://github.com/freelawproject/courtlistener/issues/7345));
  the canonical example is the Akhter twins case (`1:25-cr-00307`,
  E.D. Va.) where three CourtListener `docket_id`s carry non-overlapping slices of
  the PACER entries. The summary pipeline now pools entries across
  every CourtListener `docket_id` in the same `(docket_number, court_id)` group
  via `summary.find_primary_documents_for_group` (deduplicated by PACER
  `entry_number`, falling back to `(date_filed, description)` for
  paperless minute orders), so each logical docket gets one pooled
  summary and one paragraph in the index instead of N near-duplicates.
  Sync's stale-flagging is rerouted through the group key as well.
  The index renderer collapses same-group docket metadata to a single
  entry per logical PACER docket (siblings stay accessible via
  `sibling_docket_ids`). **Operators should back up `data/case-calendar.sqlite`
  (plus the `-wal` / `-shm` sidecars) before upgrading.** A
  non-destructive migration runs automatically on Store init: the
  existing `case_summaries` table is renamed to
  `case_summaries_pre_group_migration` (kept around for one release as
  a rollback escape hatch) and the new table is backfilled from it,
  with the latest `generated_at` winning on group collisions. Rolling
  back to 0.1.x is `DROP TABLE case_summaries; ALTER TABLE
  case_summaries_pre_group_migration RENAME TO case_summaries;` plus a
  downgrade.

- `Store.find_concurrent_hearing_clusters` now clusters scheduled rows
  by `(docket_number, court_id, starts_at_utc)` instead of
  `(docket_id, starts_at_utc)`. The existing LLM-driven
  `_dedupe_concurrent_hearings` therefore picks up cross-CourtListener-sibling
  drift on SCHEDULED rows too (the Akhter-shape future-trial scenario
  with two CourtListener docket_ids holding same-slot trials under different
  keys). Orphan dockets that lack `dockets` metadata fall back to the
  pre-grouping `docket_id` key so this is non-breaking for the rare
  edge case where a hearing row exists without its parent metadata.
- `cmd_sync` adds the case's calendar to `affected_calendars` when
  either dedup sweep flips a row, so a sweep-only sync (no entries
  processed) still re-renders the ICS — otherwise a same-slot
  duplicate flipped to cancelled would linger in the cached feed
  until the next sync touched an entry.
- Anthropic and OpenAI SDK clients are now constructed with
  `max_retries=8` instead of the SDK default of 2. The default's
  ~1.5s cumulative backoff was not enough to ride out an Anthropic
  529 Overloaded condition, which routinely lasts tens of seconds
  and was leaking through as `IGNORE` actions that silently dropped
  entries. The new ceiling is ~127s before any server-supplied
  `Retry-After` is honored. Failures past that remain possible but
  are now rare enough to surface in the logs for manual rerun via
  `scripts/reprocess_entries.py`.

### Added

- `_dedupe_concurrent_held_hearings` sweep that merges `status='held'`
  hearings sharing the same logical PACER slot
  `(docket_number, court_id, starts_at_utc)`. Resolution is
  deterministic (no LLM call) — a court physically can't have held two
  hearings simultaneously, so same-slot held clusters are
  unambiguously key-drift duplicates. The motivating case is
  cross-CourtListener-sibling drift exposed by the docket grouping work:
  `sentencing-didenko` (from prior sync of one CourtListener sibling) and
  `sentencing-didenko-2` (from today's sync of the sibling CourtListener docket
  with a different `pacer_case_id`) at the exact same UTC slot. The
  canonical row keeps its key and gets the siblings' `source_entry_ids`
  merged in; siblings are cancelled with a `[dedupe-held]` audit note
  pointing at the canonical. Renderers skip cancelled rows so the
  calendar surfaces one event, not N. Accompanying store helper:
  `Store.find_concurrent_held_hearing_clusters`.
- Extractor pattern coverage for appellate deadlines: petitions for
  rehearing (FRAP 40), mandate issuance (FRAP 41), joint appendix due
  dates, the "MOTION by [Party] to extend" appellate filing convention,
  and `argued` / `calendared` post-argument and scheduling vocabulary.
- Extractor pattern coverage for federal civil deadlines: answer due
  (FRCP 12(a)), initial / expert / pretrial disclosures (FRCP 26(a)),
  discovery cutoffs (FRCP 16(b)(3)(A)), motions in limine, class
  certification (FRCP 23(c)(1)(A)), joint status reports, mediation
  (28 U.S.C. § 651 et seq.), pretrial orders (FRCP 16(d)), and
  Markman / claim-construction briefing milestones.
- Extractor pattern coverage for federal criminal deadlines: presentence
  reports (FRCrP 32), CIPA filings (18 U.S.C. App. III), Jencks material
  (18 U.S.C. § 3500), notice of appeal (FRAP 4(b)), plus generic
  `notice` / `report` / `memo` / `material` "is due" patterns that
  catch Rule 404(b) notices, Brady / Giglio material, and sentencing
  memoranda.
- Disposition-detection coverage for guilty-plea events: factual proffer
  statements, magistrate's report-and-recommendation on plea of guilty
  or change of plea, paperless minute orders documenting a defendant's
  plea ("pled guilty" / "pleads guilty" / "plea of guilty"), and the
  trial court's adoption order. Negative coverage ensures arraignment
  "NOT GUILTY PLEA" entries and non-plea R&Rs (suppression, § 2255,
  IFP, discovery) do not falsely register as dispositions.
- Disposition-detection coverage for civil-judgment variants:
  `CONSENT JUDGMENT`, `DEFAULT JUDGMENT`, `CONSENT DECREE`, and
  `decrees?` as a body keyword.
- `AGENTS.md` convention forbidding unsupportable empirical claims
  ("most-missed", "foundational", "most common") in comments,
  docstrings, and PR descriptions, with an exception that allows
  priority directives ("the imposed sentence is the most important
  fact about the case") inside LLM prompt templates.
- Text-hash dedup for `extra_documents` against CourtListener-surfaced
  docs on the same docket. When an operator-added `extra_documents`
  URL becomes naturally findable via CourtListener — typically because
  someone re-uploads the PDF to PACER under the docket's current
  `pacer_case_id`, working around the
  [CourtListener bug #7345](https://github.com/freelawproject/courtlistener/issues/7345)
  reconciler shape FLP closed without committing to a fix —
  `summary._filter_extras_already_in_cl` compares a normalized-text
  sha256 of each extra against every CourtListener-surfaced primary /
  disposition doc on the same docket group and drops the duplicate
  before it reaches the summary LLM, logging a WARN naming the URL
  so the operator can remove the now-redundant entry from
  `config.yaml`. Previously the same body would reach the LLM twice
  and exert outsized influence on the summary.

### Changed

- LLM prompts: removed frequency-claim rationale text ("typically",
  "often", "in most cases", "reflexively", "in nearly every case")
  from classification and summary prompts, while preserving priority
  directives. No behavior change — these were rationale phrasings,
  not classification rules.

[0.2.0]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.2.0

## [0.1.1] - 2026-05-17

### Changed

- Renamed the project from "case-calendar" to "Case Calendar" in all
  user-facing prose, documentation, the index-page footer and default
  `site_title`, and the webhook-server log line. CLI commands, file
  paths, URL paths, env var names, package / module / script entry
  point identifiers, the webhook JSON `service` identifier, the ICS
  `PRODID` / `UID` suffixes, and the M365 token-cache name are
  unchanged — they remain `case-calendar` because they are wire
  identifiers and renaming them would break existing subscribers,
  webhook deployments, and token recovery.

[0.1.1]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.1.1

## [0.1.0] - 2026-05-16

Initial release.

### Added

- `case-calendar sync` polling against the CourtListener REST API, with
  per-docket / per-entry / per-fingerprint short-circuits to keep
  quiet-day cost near zero.
- `case-calendar serve` webhook receiver for CourtListener
  `DOCKET_ALERT` events, with `Idempotency-Key` dedup and a secret-gated
  health endpoint.
- `case-calendar emit` for forced re-renders.
- `case-calendar summarize` for opt-in AI per-docket case summaries.
- `case-calendar show`, `prune`, `setup gcal`, `setup m365`,
  `webhook-url` helper commands.
- ICS feed output (RFC 5545) with stable per-hearing UIDs and
  court-local timezone tagging, subscribable from Proton, Apple,
  Thunderbird, Outlook 2019+, and Google Calendar.
- Google Calendar push (opt-in) with deterministic event ids,
  idempotent updates, attendee notifications, and reminder overrides.
- Microsoft 365 / Outlook push (opt-in) via the official Microsoft Graph
  SDK, with server-assigned id caching plus `$filter` recovery against a
  stable extended-property correlation key.
- Filing-deadline tracking alongside hearings, with docket-aware
  auto-detect (civil/appellate on, routine criminal off, explicit
  force-on override per case).
- LLM-driven extraction across Anthropic, OpenAI, and Gemini, with a
  cheap small-model tier for per-entry extraction and a higher tier for
  case summaries. Per-track defaults and env / config overrides.
- End-of-case verify pass for both scheduled and cancelled hearings
  (catches missed reschedules, cancellations, and hallucinations);
  same-docket same-slot dedup sweep.
- Per-docket AI case summaries with automatic stale-flag refresh on
  primary documents and dispositions, with the case-summary LLM
  instructed to refuse rather than fabricate when source documents are
  insufficient.
- Garbled-text detector that catches upstream PDF-encoding noise and
  triggers our own poppler + tesseract OCR fallback.
- Sealed-docket visibility advisory that surfaces a "subsequent docket
  activity may not be publicly visible" hedge to the summary LLM when a
  sealing order is in effect.
- `extra_documents` per-case YAML list that lets operators feed
  out-of-band documents (e.g. a DoJ press release attachment) into the
  summary LLM as a distinct supplementary block, used to work around
  upstream CourtListener data gaps without forging RECAP entries.
- Static `index.html` renderer for a public calendar directory, with
  inline CSS/JS, system + manual dark mode, client-side sort, and
  static legal disclaimers held outside the LLM synthesis step.
- SQLite store with WAL journaling and a 5-second busy timeout so the
  polling sync and the webhook serve process can safely share one DB
  file.
- Hand-maintained court-id → IANA timezone table covering all 13
  federal circuits, all 94 district courts, the Supreme Court,
  specialty courts, and territories.
- Caddyfile template plus a systemd unit template for the webhook
  receiver, both checked in as working starting points.

[0.1.0]: https://github.com/seanthegeek/case-calendar/releases/tag/v0.1.0
