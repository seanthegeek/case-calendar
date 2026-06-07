---
title: Configuration
---

Case Calendar reads one YAML file (default `config.yaml`) that lists your
cases, the calendars they group into, and the optional features you want
turned on. There's an annotated `config.example.yaml` in the repo root you
can copy and edit.

Secrets and LLM-provider selection live separately in a `.env` file — see
[Environment variables](#environment-variables) below.

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
| `case_summaries` | no | Enable and configure AI case summaries. Keys in [Case summaries](#case-summaries) below; the feature is described in [AI case summaries](case-summaries.md). |
| `ensure_docket_alerts` | no (default `true`) | Auto-subscribe each configured docket to CourtListener docket alerts on `sync` / `serve` startup, so the webhook receiver gets real-time pushes. Set `false` if you manage subscriptions another way. See [webhooks](webhooks.md). |

### Case summaries

The `case_summaries` block turns on AI-generated case summaries and selects the
model that writes them. It's off by default. The feature itself — what gets
summarized, the truthfulness guardrails, the inline document links — is
described in [AI case summaries](case-summaries.md).

```yaml
case_summaries:
  enabled: true
  # provider: anthropic
  # model: claude-sonnet-4-6
  # allow_ocr: true
  # debounce_seconds: 300
```

| Key | Required | Purpose |
| --- | --- | --- |
| `enabled` | yes | Master switch. Defaults to `false`. |
| `provider` | no | Force the summary-track provider (`anthropic` / `openai` / `gemini`). When unset, falls back to `LLM_SUMMARY_PROVIDER`, then `LLM_PROVIDER`, then auto-detection from whichever API keys are set (summary key priority: anthropic > gemini > openai). See [Architecture → why the default is a split](architecture.md#why-the-default-is-a-split--gemini-for-extraction-anthropic-for-summaries). |
| `model` | no | Override the model. Defaults to Sonnet / GPT-5.4 / Gemini Pro depending on provider. |
| `allow_ocr` | no (default `true`) | Run local OCR on PDFs that arrived without usable text (CourtListener's `plain_text` was empty or garbled — CourtListener does not OCR documents, so the project OCRs them itself). Set `false` to skip tesseract entirely. |
| `debounce_seconds` | no (default `300`) | Webhook-only. Seconds of quiet to wait after the last summary-relevant entry before re-running the LLM. Polling syncs ignore it and regenerate immediately. |

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

### Reminders

`reminders` is a list of `{method, minutes}` entries, settable on a calendar
and overridable per-case:

| Field | Required | Purpose |
| --- | --- | --- |
| `method` | yes | `popup` (fires in each subscriber's own calendar app — safe on public feeds) or `email` (Google delivers only to the calendar owner; in ICS the addresses are visible on the feed). |
| `minutes` | yes | Minutes before the event to fire the reminder (e.g. `30`, or `1440` for one day before). |

Which apps honor what, and the public-feed privacy traps, are covered in
[Calendar backends → notifications](calendars.md#notifications-attendees-and-reminders).

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
| `notify_emails` | no | Per-case override of the calendar's `notify_emails`. |
| `reminders` | no | Per-case override of the calendar's `reminders`. |
| `extra_documents` | no | Operator-provided document URLs for the AI summary pipeline. Fields in [extra_documents](#extra_documents) below; rationale in [case summaries](case-summaries.md#extra_documents). |
| `tags` | no | Topical labels (e.g. `DPRK`, `PRC`, `Russia`) rendered on calendar event descriptions and as click-to-filter chips on the HTML index. See below. |

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

#### CourtListener sibling dockets (same docket number, different docket\_ids)

A subtler case: CourtListener sometimes stores a SINGLE logical PACER
docket as MULTIPLE `docket_id` rows. The trigger is upstream: the
`pacer_case_id` for the docket changed at some point (CourtListener's
reconciler couldn't merge them — see
[CourtListener issue #7345](https://github.com/freelawproject/courtlistener/issues/7345)),
and each `docket_id` carries a partial slice of the entries. List every
sibling `docket_id` so the AI summary pools entries across the slices
into one complete view; if you list only one, you get a partial summary
based on whichever slice that `docket_id` happens to hold.

```yaml
- id: us-v-akhter
  name: "United States v. Akhter"
  calendar: cybercrime
  dockets: [71989485, 73333500, 73320754]
  # Same docket: 1:25-cr-00307 (E.D. Va.) — three CourtListener docket_id rows
  # because the upstream pacer_case_id changed mid-life. Each carries
  # a different slice of the entries; the AI summary needs all three
  # to see the indictment, motions, and judgment together.
```

Case Calendar detects sibling docket\_ids by comparing each
`docket_id`'s `(docket_number, court_id)` pair and treats matches as
one logical PACER docket: one summary, one paragraph in the rendered
index, one link to a CourtListener docket page. **The link goes to
whichever `docket_id` you listed first in `dockets:`**, so put your
preferred CourtListener page first (typically the one with the most
content visible on the CourtListener side).

To know whether to list one or multiple `docket_id`s, check each on
the CourtListener docket page (`courtlistener.com/docket/<id>/...`):
if they have the same docket number under the same court, list them
all. If the docket numbers differ — district vs appellate, parallel
suits in different courts — list them all too; Case Calendar treats
different `(docket_number, court_id)` pairs as parallel proceedings
and renders one labeled paragraph per logical docket (the Anthropic
v. DOW shape above).

### Deadline tracking

Filing deadlines are tracked on every docket uniformly — civil, criminal,
appellate, magistrate, specialty. There's no per-case opt-in or
docket-number auto-detect. Significance (`major` vs `minor`, set by the
LLM per the rules in `SYSTEM_PROMPT`) decides what reaches subscriber
calendars: dispositive briefing, sentencing memos, PSR objections, and the
amicus master filing window land as major and appear on the calendar;
procedural shuffle (motion-for-leave responses/replies, redaction-request
windows, routine status reports) lands as minor and stays in the audit
trail only. Pretrial motion practice on a serious criminal case (suppression
/ motions in limine / Daubert) flows automatically as a result.

### Tags

Each case can carry a short list of topical labels:

```yaml
- id: us-v-knoot
  name: "United States v. Knoot"
  calendar: cybercrime
  dockets: [69026861]
  tags: [DPRK, IT worker fraud, laptop farm]
```

Tags appear in two places:

- **Calendar events** — every event for the case (hearings and deadlines)
  carries a `Tags: DPRK, IT worker fraud, laptop farm` line directly
  under the event description, above the docket-keeping blocks (Judge,
  Case, Docket, etc.). Tags render verbatim, so the casing you write
  is what subscribers see — anyone scanning their calendar app can
  tell at a glance which topic each event belongs to.
- **HTML index** — tags render as clickable chips under each case row.
  Clicking a chip appends the tag to the global search bar, filtering the
  list to cases that carry it. The search bar uses an AND-substring match,
  so clicking two chips narrows further; typed words and chip-added tags
  combine the same way.

Tags are case-insensitive for filter / dedup purposes; the casing you write
here is how they render. Multi-word tags are supported — write them with
whitespace and they'll be quoted into the search box on click.

### extra_documents

Point the AI summary pipeline at a document CourtListener doesn't surface
(a sealed-then-unsealed indictment, a PDF hit by a CourtListener metadata
bug). The full rationale and failure modes are in
[AI case summaries → extra_documents](case-summaries.md#extra_documents); the
per-entry fields are:

```yaml
extra_documents:
  - docket: 70789744
    url: https://www.justice.gov/opa/media/1407196/dl
    note: >-
      The unsealed indictment in S.D. Tex. 4:23-cr-00523 (United States v. Xu Zewei).
```

| Field | Required | Purpose |
| --- | --- | --- |
| `docket` | yes | Must be one of this case's `dockets` ids. |
| `url` | yes | Absolute `https://` URL to a PDF (DoJ press release, archived storage URL, court website). |
| `note` | yes | One sentence naming the document, fed to the summary LLM as trusted metadata; the document text itself stays untrusted. Keep tooling details (bug numbers, "remove once fixed") in a YAML `#` comment, not here. |

## Validation

Case Calendar validates the config at startup. Bad values (a missing required
field, an `extra_documents` entry whose `docket` isn't an integer or isn't in
this case's `dockets` list) fail fast with a clear error
rather than silently being skipped.

## Full example

See [`config.example.yaml`](https://github.com/seanthegeek/case-calendar/blob/main/config.example.yaml)
in the repo for a fully annotated example, including realistic
multi-docket aggregation, deadline-tracking overrides, and `extra_documents`
workarounds.

## Environment variables

Secrets and LLM-provider selection live in a `.env` file in the project root,
not in `config.yaml`, so credentials stay out of the file you might publish or
share. Case Calendar loads `.env` automatically (via `python-dotenv`) before
anything reads these. There's an annotated `.env.example` to copy; the
step-by-step walkthrough is in
[Installation → configure secrets](installation.md#configure-secrets).

| Variable | Required | Purpose |
| --- | --- | --- |
| `COURTLISTENER_TOKEN` | yes | CourtListener API token (from your CourtListener user-profile page). Used by `sync`, `serve`, and `summarize`. |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | at least one | API key for each LLM provider you use. Extraction always needs one; summaries need one when enabled. `GOOGLE_API_KEY` is accepted as an alias for `GEMINI_API_KEY`. |
| `CASE_CALENDAR_WEBHOOK_SECRET` | only for `serve` | Long URL-safe random string that gates the webhook receiver path. See [webhooks](webhooks.md#1-choose-a-secret). |
| `M365_CLIENT_ID` | only if pushing to Microsoft 365 | Entra app (client) ID — an alternative to the `m365_client_id` YAML key. |
| `LLM_PROVIDER` | no | Pin ONE provider (`anthropic` / `openai` / `gemini` / `ollama`) for BOTH the extraction and summary tracks. Overridden per-track by the two variables below. |
| `LLM_EXTRACTION_PROVIDER` | no | Pin the extraction track's provider only (beats `LLM_PROVIDER` for that track). Accepts `ollama` for local extraction. |
| `LLM_SUMMARY_PROVIDER` | no | Pin the summary track's provider only. The `case_summaries.provider` YAML key takes precedence over this. Accepts `ollama` for local summaries. |
| `LLM_MODEL` | no | Override the extraction track's model (default: the per-provider small/fast tier). |
| `LLM_SUMMARY_MODEL` | no | Override the summary track's model. The `case_summaries.model` YAML key takes precedence over this. |
| `OLLAMA_BASE_URL` | no (default `http://localhost:11434/v1`) | Where the local [Ollama](local-llms.md) server listens. Only consulted when a track resolves to the `ollama` provider. |
| `OLLAMA_NUM_CTX` | no | Context window (tokens) for Ollama requests. Local models default to a small window and silently truncate longer prompts — raise it for the summary track. See [Local models](local-llms.md). |
| `LOG_LEVEL` | no (default `INFO`) | Python logging level — `DEBUG`, `INFO`, `WARNING`, etc. |

When no provider is pinned, each track auto-detects from whichever API keys are
set, in its own priority order: extraction prefers **gemini > anthropic >
openai**, summaries prefer **anthropic > gemini > openai**. With all three keys
present that lands a zero-config operator on the recommended split — Gemini for
extraction, Anthropic for summaries. The reasoning is in
[Architecture → why the default is a split](architecture.md#why-the-default-is-a-split--gemini-for-extraction-anthropic-for-summaries);
the full precedence walkthrough is in
[Installation](installation.md#configure-secrets).

## Next steps

- [CLI reference](cli.md) — every subcommand.
- [Calendar backends](calendars.md) — Google Calendar / Microsoft 365 setup.
- [AI case summaries](case-summaries.md) — opt-in summary configuration.
