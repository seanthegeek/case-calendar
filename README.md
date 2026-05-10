# case-calendar

Automatically sync court hearing dates from CourtListener / RECAP into your
calendar (ICS file or Google Calendar). Built for cases where docket-watching
by hand is too much: cybercrime prosecutions, multi-docket tech litigation,
etc.

## What it does

For each case in `config.yaml`:

1. Pulls new docket entries from CourtListener (one or more dockets per case).
2. Filters for entries that look hearing-related.
3. Extracts hearing details with an LLM (Anthropic, OpenAI, or Gemini),
   reading both the docket text and the linked PDFs. Cross-references
   (`granting 65 Motion ...`) and recent docket activity are resolved from
   the local store so the LLM has the context to name a hearing correctly
   when the entry that schedules it doesn't itself name the subject.
4. Stores hearings in SQLite with stable keys, so reschedules and dial-in
   updates land on the same row. Each hearing is tagged `major` or `minor`;
   minor (procedural-only phone calls about scheduling motions) and
   `cancelled` rows are skipped at render time so the calendar tracks
   major case moments only.
5. Writes an ICS file (subscribe from Proton, Apple, etc.) and optionally
   pushes to Google Calendar.

Court-local timezones are preserved on each event (`DTSTART;TZID=...` /
Google Calendar `timeZone` field), so a 3 PM Pacific hearing stays
"3 PM Pacific" through DST and shows in your viewer's local time. Multi-
docket cases (e.g. district + appellate) collapse into one logical case.
Reschedules update existing events in place; cancellations remove the
event from subscribers' calendars (the new event of record — a plea
hearing, a rescheduled trial — lives on its own row).

Two delivery modes:

- **Polling** (`case-calendar sync`) — run on a cron. A three-tier
  short-circuit (docket / entry / fingerprint dedup) means quiet hours
  cost roughly one cheap CL request per docket and zero LLM calls.
- **Webhook** (`case-calendar serve`) — register a public HTTPS URL with
  CourtListener and receive `DOCKET_ALERT` events in real time. Bypasses
  the daily polling quota entirely.

## Setup

```bash
uv sync
cp .env.example .env
# Fill in COURTLISTENER_TOKEN and one of ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY
cp config.example.yaml config.yaml
# Edit config.yaml to list your cases and dockets
```

### Optional: local OCR for un-OCR'd PDFs

CourtListener OCRs PDFs once they're contributed to RECAP, but there's a lag
and some entries' PDFs never get uploaded. To extract text from PDFs ourselves
when CL hasn't yet:

```bash
sudo apt install poppler-utils tesseract-ocr  # or brew install poppler tesseract
```

Without these, the tool will still work — it just skips PDFs CL hasn't
processed and re-tries on each sync (no cache poisoning).

### Optional: Google Calendar push

```bash
# 1. Create an OAuth client of type "Desktop app" in Google Cloud Console
# 2. Download the credentials JSON
# 3. Add this to config.yaml:
google_credentials_path: ~/.case-calendar/google-credentials.json
calendars:
  cybercrime:
    google_calendar_id: xxx@group.calendar.google.com
```

First push opens a browser for OAuth; the token is cached for next time.

### Optional: notifications

Per-calendar (with per-case override) in `config.yaml`:

```yaml
calendars:
  cybercrime:
    notify_emails:
      - me@example.com
    reminders:
      - {method: popup, minutes: 30}
      - {method: popup, minutes: 1440}    # 1 day before
```

`notify_emails` are added as Google Calendar attendees (so the address gets
the invitation + the event lands on their own calendar) and as ICS
`ATTENDEE` lines. `reminders.method=popup` becomes a Google Calendar
override for the calendar owner and a `VALARM:DISPLAY` block in the ICS,
which fires in any subscriber's local app. `method=email` only delivers to
the calendar owner in Google.

> ⚠️ **Privacy:** `notify_emails` are visible to anyone with view access
> to the event. Don't list them on a public calendar — use popup
> reminders only and let subscribers configure their own clients.

## Usage

```bash
case-calendar sync                    # pull updates (polling)
case-calendar emit                    # write ICS files
case-calendar emit --push-gcal        # also push to Google Calendar
case-calendar show                    # dump current hearings
case-calendar show --case us-v-wang   # one case
case-calendar sync --case us-v-wang   # sync just one case
case-calendar serve --port 8000       # real-time webhook receiver
```

Run `sync` on a cron — the SQLite store dedupes already-seen entries, so
re-running is cheap. PDFs that weren't yet on RECAP, or hearings whose
PDFs hadn't been OCR'd, get re-checked on each sync until available.

### Real-time via webhooks (no polling burn)

CourtListener can push events to a URL you control instead of you polling.
This is by far the cheapest mode — zero CL API calls per quiet hour, zero
daily-quota risk, and updates land within seconds of filing.

Setup:

1. Generate a secret and put it in `.env`:

   ```bash
   CASE_CALENDAR_WEBHOOK_SECRET=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')
   ```

2. `case-calendar serve --port 8000` — runs the receiver on localhost.
3. Expose `127.0.0.1:8000` over public HTTPS. Easy options:
   - Cloudflare Tunnel (`cloudflared tunnel --url http://localhost:8000`)
   - Caddy on a small VPS (auto-TLS)
   - fly.io / Railway / Render container
4. In the CL dashboard's webhooks panel, register:

   ```text
   https://<your-host>/webhooks/case-calendar/<CASE_CALENDAR_WEBHOOK_SECRET>
   ```

   Event type: `DOCKET_ALERT`. Pick the highest webhook version.
5. In CL, subscribe to docket alerts for every docket in your `config.yaml`
   (this is what tells CL to send those events to your webhook).

The receiver makes one `/dockets/{id}/` lookup the first time it sees a new
docket (to learn its court timezone and citation), then zero CL calls per
event after that. Polling sync still works alongside webhooks for backfill
or as a safety net.

**Security note:** CL doesn't sign webhook payloads, so the secret in the
URL path is your only defense against forged events. Keep `.env` private,
and rotate the secret if it ever leaks.

## Cost notes

- CourtListener API is free with per-tier limits. Authenticated users get
  5 req/min, 50 req/hour, 125/day by default; Free Law Project members get
  higher limits. The client honors any `Retry-After` value (the daily
  bucket can return ~24h once exhausted) so a long-running sync resumes
  itself across the daily reset rather than crashing. Every 429 logs the
  URL, response body, and rate-limit headers so it's clear which bucket
  fired.
- Default LLM models are the small/fast tier (Haiku, gpt-5.4-nano,
  Gemini Flash Lite) — date extraction doesn't need flagship reasoning.
  Override with `LLM_MODEL` if you want.
- Three-tier short-circuit keeps quiet-day cost near zero:
  1. Per-docket: skip entirely if the docket's `date_modified` hasn't
     advanced since last sync (one cheap GET, no entries API, no LLM).
  2. Per-entry: server-side `modified_after` filter on the entries API.
  3. Per-fingerprint: dedup so unchanged entries never hit the LLM.
- LLM is only called on entries that pass the regex pre-filter (briefs,
  attorney appearances, paperless minute entries, etc. are dropped for
  free).
- Anthropic system prompt is cached, so most of each call's tokens are
  cache reads.
- Webhooks bypass the polling quota entirely — once registered, only new
  filings cost anything (one LLM call per hearing-relevant entry).

## Testing

```bash
uv sync --extra test
uv run pytest                                  # full suite (~8s, 190+ tests)
uv run pytest --cov=case_calendar              # with coverage
```

Tests are hermetic — no network, no real LLM, no real Google API. `FakeCL`
and stubbed LLM responses cover the integration paths; pure helpers
(timezone conversion, ICS rendering, fingerprinting, retry logic) have
direct unit tests. Webhook integration tests boot a real
`ThreadingHTTPServer` on an ephemeral port and post JSON to it.

## Project layout

```text
case_calendar/
  cli.py              # entry point: sync / emit / show / serve
  courtlistener.py    # REST v4 client (backoff, honors any Retry-After)
  courts.py           # court_id -> IANA timezone (full federal coverage)
  extractor.py        # cheap keyword filter
  llm.py              # provider-agnostic extraction (anthropic/openai/gemini)
  pdf.py              # plain_text -> pypdf -> tesseract fallback chain
  serve.py            # CourtListener webhook receiver (DOCKET_ALERT)
  store.py            # SQLite: dockets, courts, entries, hearings, webhook events
  sync.py             # per-case sync orchestration; shared with serve.py
  calendars/
    description.py    # shared event-body builder + time-prefix helper
    ics.py            # RFC 5545 output (TZID-tagged local times)
    gcal.py           # Google Calendar API sync (per-court timeZone)
scripts/
  reprocess_entries.py    # re-run LLM against stored entries (after prompt changes)
  classify_significance.py # bulk-classify NULL-significance hearings (read-only)
```
