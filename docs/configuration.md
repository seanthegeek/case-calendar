---
title: Configuration
---

Case Calendar reads one YAML file (default `config.yaml`) that lists your
cases, the calendars they group into, and the optional features you want
turned on. There's an annotated `config.example.yaml` in the repo root you
can copy and edit.

[← Back to docs](index.md)

## Top-level options

```yaml
store_path: data/case-calendar.sqlite
```

| Key | Required | Purpose |
| --- | --- | --- |
| `store_path` | yes | The SQLite database that tracks already-processed entries, extracted hearings and deadlines, AI case summaries, and webhook idempotency keys. Created on first run. |
| `google_credentials_path` | only if pushing to Google Calendar | Path to the OAuth client JSON downloaded from Google Cloud Console. See [calendars](calendars.md). |
| `google_token_path` | no (default `tokens/google-token.json`) | Where to cache the refresh token after `case-calendar setup gcal`. |
| `m365_client_id` | only if pushing to Microsoft 365 | Application (client) ID from your Entra app registration. Can also be set via `M365_CLIENT_ID` in `.env`. |
| `m365_token_path` | no (default `tokens/m365-token.json`) | Where to cache the auth record after `case-calendar setup m365`. |
| `index_path` | no | Set to render a static `index.html` listing every calendar and case. See [public index page](public-page.md). |
| `public_base_url` | no | The URL where the `out/` directory is hosted (e.g. `https://calendars.example.com`). When set, the index uses absolute `https://` + `webcal://` subscribe links. |
| `site_title` | no | The `<h1>` on the index page. |
| `site_description` | no | The `<meta name="description">` content for search engines and link previews. Keep under 160 characters. |
| `case_summaries` | no | Enable AI summaries. See [case summaries](case-summaries.md). |

## Calendars

A calendar groups one or more cases under a single ICS file (and optionally a
Google or Microsoft 365 destination).

```yaml
calendars:
  cybercrime:
    name: "Cybercrime cases"
    ics_path: out/cybercrime.ics
    # google_calendar_id: abc123@group.calendar.google.com
    # m365_calendar_id: AAMkADExAAA...
    # m365_use_default_calendar: true     # alternative to m365_calendar_id
    # notify_emails:
    #   - alerts@example.com
    # reminders:
    #   - {method: popup, minutes: 30}
    #   - {method: popup, minutes: 1440}    # 1 day before
```

| Key | Required | Purpose |
| --- | --- | --- |
| `name` | yes | Human-readable label shown on the index page. |
| `ics_path` | yes | Output path for the ICS feed. Always written. |
| `google_calendar_id` | no | Push events to this Google Calendar. See [calendars](calendars.md). |
| `m365_calendar_id` | no | Push events to a specific Outlook calendar (find via Graph Explorer). |
| `m365_use_default_calendar` | no | If `true`, push to the M365 user's primary calendar (mutually exclusive with `m365_calendar_id`). |
| `notify_emails` | no | Email addresses to invite as Google Calendar attendees and list as ICS `ATTENDEE` lines. **Warning:** visible on public feeds. |
| `reminders` | no | Per-event reminders. `method: popup` is safe on public calendars; `method: email` only fires for the calendar owner in Google. |

The calendar's `id` (the top-level key, e.g. `cybercrime`) is the value cases
reference via their `calendar:` field.

### Privacy: notify_emails on public calendars

If you're publishing the ICS feed publicly, **don't** set `notify_emails` —
those addresses appear in the public feed (as `ATTENDEE` lines) and in any
Google Calendar invite. Use `reminders` with `method: popup` instead. Popup
reminders fire in each subscriber's own calendar app from a `VALARM:DISPLAY`
block and leak nothing.

## Cases

```yaml
cases:
  - id: us-v-wang
    name: "United States v. Wang"
    calendar: cybercrime
    dockets: [70678228]
```

| Key | Required | Purpose |
| --- | --- | --- |
| `id` | yes | Stable kebab-case identifier. Used as part of every event UID and as the row key in the database — don't change it casually. |
| `name` | yes | Case caption shown on the calendar and index. |
| `calendar` | yes | One of the keys under `calendars:`. |
| `dockets` | yes | One or more CourtListener docket ids (integers — the URL path component, e.g. the `70678228` in `courtlistener.com/docket/70678228/`). |
| `aggregation_note` | no | One-sentence framing for multi-docket cases, shown only to the AI summarizer. See below. |
| `extract_deadlines` | no | Force-on filing-deadline tracking for a case the auto-detector would skip (typically a serious criminal case). See below. |
| `notify_emails` | no | Per-case override of the calendar's `notify_emails`. |
| `reminders` | no | Per-case override of the calendar's `reminders`. |
| `extra_documents` | no | Operator-provided document URLs for the AI summary pipeline. See [case summaries](case-summaries.md#extra_documents). |

### Multi-docket cases

Some cases span multiple docket numbers — district + appellate, parallel
filings in different venues, or separate dockets for cooperating co-defendants
charged in the same conspiracy. List them all under one `dockets:` array and
they aggregate into a single logical case on the calendar:

```yaml
- id: anthropic-v-dow
  name: "Anthropic v. DOW"
  calendar: tech
  dockets: [72380208, 72379655, 73136734]
  aggregation_note: >-
    Parallel suits challenging separate Department of War actions taken
    under distinct statutory authorities, each filed in the proper venue
    for the action it targets.
```

The `aggregation_note` is shown only to the AI summarizer. It lets the
generated prose read like it was written by someone who understands the
litigation strategy rather than describing each docket in isolation. If
summaries are off, leave it out.

### Deadline tracking auto-detect

Case Calendar decides whether to track filing deadlines based on the docket
number prefix:

- **Civil** (e.g. `-cv-`), **appellate**, and **specialty** courts → on
  (response / reply / brief deadlines matter).
- **Routine criminal dockets** matching the substrings `-cr-`, `-cm-`,
  `-cmc-`, `-po-`, or `-mj-cr-` in the federal docket number → off
  (criminal practice is hearing-driven, not briefing-driven).

For a serious criminal case where the filing cadence *is* what you're
watching — pretrial motion practice (suppression / motions in limine /
Daubert) or the run-up to sentencing (sentencing memos, PSR objections) —
force it on:

```yaml
- id: us-v-someone
  name: "United States v. Someone"
  calendar: natsec
  dockets: [123456]
  extract_deadlines: true
```

## Validation

Case Calendar validates the config at startup. Bad values (a missing required
field, a docket id that isn't an integer, an `extra_documents` entry whose
`docket` isn't in this case's `dockets` list) fail fast with a clear error
rather than silently being skipped.

## Full example

See [`config.example.yaml`](https://github.com/seanthegeek/case-calendar/blob/main/config.example.yaml)
in the repo for a fully annotated example, including realistic
multi-docket aggregation, deadline-tracking overrides, and `extra_documents`
workarounds.

## Next steps

- [CLI reference](cli.md) — every subcommand.
- [Calendar backends](calendars.md) — Google Calendar / Microsoft 365 setup.
- [AI case summaries](case-summaries.md) — opt-in summary configuration.
