# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog][kac], and this project
adheres to [Semantic Versioning][semver].

[kac]: https://keepachangelog.com/en/1.1.0/
[semver]: https://semver.org/spec/v2.0.0.html

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
  E.D. Va.) where three CL `docket_id`s carry non-overlapping slices of
  the PACER entries. The summary pipeline now pools entries across
  every CL `docket_id` in the same `(docket_number, court_id)` group
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
  `_dedupe_concurrent_hearings` therefore picks up cross-CL-sibling
  drift on SCHEDULED rows too (the Akhter-shape future-trial scenario
  with two CL docket_ids holding same-slot trials under different
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
  cross-CL-sibling drift exposed by the docket grouping work:
  `sentencing-didenko` (from prior sync of one CL sibling) and
  `sentencing-didenko-2` (from today's sync of the sibling CL docket
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
