# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog][kac], and this project
adheres to [Semantic Versioning][semver].

[kac]: https://keepachangelog.com/en/1.1.0/
[semver]: https://semver.org/spec/v2.0.0.html

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
