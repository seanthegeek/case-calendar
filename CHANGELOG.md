# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog][kac], and this project
adheres to [Semantic Versioning][semver].

[kac]: https://keepachangelog.com/en/1.1.0/
[semver]: https://semver.org/spec/v2.0.0.html

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

### Added

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
