# case-calendar

Automatically sync court hearing dates and filing deadlines from
CourtListener / RECAP into your calendar (ICS file, Google Calendar, or
Microsoft 365 / Outlook). Built for cases where docket-watching by hand is
too much: cybercrime prosecutions, multi-docket tech litigation, etc.

## What it does

For each case in `config.yaml`:

1. Pulls new docket entries from CourtListener (one or more dockets per case).
2. Filters for entries that look hearing- or deadline-related.
3. Extracts hearing and filing-deadline details with an LLM (Anthropic,
   OpenAI, or Gemini), reading both the docket text and the linked PDFs.
   Cross-references (`granting 65 Motion ...`) and recent docket activity
   feed in as context, so the LLM can name a hearing correctly even when
   the scheduling entry itself is unspecific.
4. Stores hearings and deadlines in SQLite with stable keys, so reschedules
   and dial-in updates land on the same row. Each row is tagged `major` or
   `minor`; minor (procedural-only phone calls, routine status reports) and
   `cancelled` / `met` rows are skipped at render time so the calendar
   tracks major case moments only.
5. Writes an ICS file (subscribe from Proton, Apple, etc.) and optionally
   pushes to Google Calendar and/or Microsoft 365 / Outlook —
   automatically, after every sync or webhook delivery, so subscribers
   see updates without a manual re-emit.

Filing-deadline tracking is auto-detected from each docket's number:
civil dockets get response/reply/brief deadlines, routine criminal dockets
don't (set `extract_deadlines: true` per case to opt a serious criminal
trial in).

Court-local timezones are preserved on each event (`DTSTART;TZID=...` /
Google Calendar `timeZone` field), so a 3 PM Pacific hearing stays
"3 PM Pacific" through DST and shows in your viewer's local time. Multi-
docket cases (e.g. district + appellate) collapse into one logical case.
Reschedules update existing events in place; cancellations remove the
event from subscribers' calendars (the new event of record — a plea
hearing, a rescheduled trial — lives on its own row).

Two delivery modes (both auto-emit affected calendars when work happens):

- **Polling** (`case-calendar sync`) — run on a cron. A three-tier
  short-circuit (docket / entry / fingerprint dedup) means quiet hours
  cost roughly one cheap CourtListener request per docket and zero LLM calls.
  Affected calendars are re-rendered at the end of the sync.
- **Webhook** (`case-calendar serve`) — register a public HTTPS URL with
  CourtListener and receive `DOCKET_ALERT` events in real time. Bypasses
  the daily polling quota entirely. Each delivery that changes a row
  triggers an immediate re-render of just that calendar.

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
when CourtListener hasn't yet:

```bash
sudo apt install poppler-utils tesseract-ocr  # or brew install poppler tesseract
```

Without these, the tool will still work — it just skips PDFs CourtListener
hasn't processed and re-tries on each sync (no cache poisoning).

### Optional: Google Calendar push

One-time setup (manual steps in the Google Cloud Console):

1. **Create or pick a Google Cloud project**:
   <https://console.cloud.google.com/projectcreate>.
2. **Enable the Google Calendar API** for that project:
   <https://console.cloud.google.com/apis/library/calendar-json.googleapis.com>.
3. **Configure the OAuth consent screen**: APIs & Services → OAuth consent
   screen. Pick "External" user type unless you're on Workspace, fill in
   the app name and your email, and add yourself as a test user. No
   scopes need to be pre-declared — the desktop flow requests them
   dynamically.
4. **Create an OAuth client of type "Desktop app"**: APIs & Services →
   Credentials → Create credentials → OAuth client ID → Desktop app.
   Download the JSON to a path you'll reference from `config.yaml` — by
   convention `~/.case-calendar/google-credentials.json`.
5. **Find the calendar id you want events on**: open Google Calendar in a
   browser → click the calendar name in the left sidebar → Settings and
   sharing → "Integrate calendar" → Calendar ID. It looks like
   `xxx@group.calendar.google.com` (or just your address for your
   primary calendar).
6. **Wire it into `config.yaml`**:

   ```yaml
   google_credentials_path: ~/.case-calendar/google-credentials.json
   # google_token_path:       ~/.case-calendar/google-token.json  # default

   calendars:
     cybercrime:
       google_calendar_id: xxx@group.calendar.google.com
   ```

7. **Authorize once interactively**: `case-calendar setup gcal`. The
   command opens a browser, you grant Calendar access, and the refresh
   token is cached at `google_token_path`. Subsequent runs — including
   the headless daemon — refresh silently against that cache.

Once the token is staged, `case-calendar sync`, `case-calendar serve`,
and `case-calendar emit` auto-push to Google Calendar for every calendar
that has `google_calendar_id` set. No flag required.

### Optional: Microsoft 365 / Outlook push

Uses the **official** [`msgraph-sdk`][msgraph] + [`azure-identity`][azid]
libraries — no third-party O365 wrappers.

One-time setup (manual steps in the Microsoft Entra admin center):

1. **Register an application** in Entra: <https://entra.microsoft.com> →
   Identity → Applications → App registrations → New registration. Pick
   a name (e.g. "case-calendar"). For "Supported account types":
   - **"Personal Microsoft accounts only"** if pushing to a personal
     `outlook.com` / `hotmail.com` calendar.
   - **"Accounts in this organizational directory only"** if pushing to
     a work / school mailbox in your own tenant.
   - **"Accounts in any organizational directory and personal Microsoft
     accounts"** (multi-tenant) if you want both to work.

   Leave the Redirect URI blank for now — we add it in step 3.
2. **Add the delegated `Calendars.ReadWrite` permission**: API
   permissions → Add a permission → Microsoft Graph → Delegated
   permissions → `Calendars.ReadWrite`. If your account is a work /
   school account in a tenant that requires admin consent, click "Grant
   admin consent" too.
3. **Mark it as a public client**: Authentication → Advanced settings →
   "Allow public client flows" = Yes. Then under "Platform
   configurations" → Add a platform → Mobile and desktop applications →
   tick `http://localhost`. Public clients use no client secret — the
   interactive browser flow proves identity instead.
4. **Copy the Application (client) ID** from the app's Overview page
   (UUID-shaped). This is a public identifier, not a secret.
5. **Find the calendar id you want events on** (optional — omit to push
   to the user's primary calendar). The easiest way is [Graph
   Explorer][gex] signed in as the target user → run `GET
   https://graph.microsoft.com/v1.0/me/calendars` and copy the `id` of
   the calendar you want. It looks like `AAMkADEx...`.
6. **Wire it in**. Either env or config works for the client id; the rest
   stays in `config.yaml`:

   ```bash
   # .env
   M365_CLIENT_ID=00000000-0000-0000-0000-000000000000
   ```

   ```yaml
   # config.yaml — or put the client id here instead and skip the env var
   # m365_client_id: "00000000-0000-0000-0000-000000000000"
   # m365_token_path: ~/.case-calendar/m365-token.json  # default

   calendars:
     cybercrime:
       m365_calendar_id: "AAMkADExAAA..."   # specific Outlook calendar
       # m365_use_default_calendar: true    # or push to the user's primary
   ```

7. **Authorize once interactively**: `case-calendar setup m365`. The
   command opens a browser, you grant `Calendars.ReadWrite`, and the
   resulting `AuthenticationRecord` is cached at
   `~/.case-calendar/m365-token.json` plus the OS keyring
   (DPAPI / Keychain / libsecret). The daemon reads the record back and
   refreshes silently thereafter — it cannot prompt a browser on its own.

Once the auth record is staged, `case-calendar sync`, `case-calendar
serve`, and `case-calendar emit` auto-push to Outlook for every calendar
that has `m365_calendar_id` or `m365_use_default_calendar` set. No flag
required. Idempotency is automatic: the Graph event id is cached on the
local row after the first create, and a stable `CaseCalendarKey` extended
property lets the push recover the right event by `$filter` lookup if the
local cache is ever wiped.

[msgraph]: https://github.com/microsoftgraph/msgraph-sdk-python
[azid]: https://learn.microsoft.com/en-us/python/api/overview/azure/identity-readme
[gex]: https://developer.microsoft.com/en-us/graph/graph-explorer

### Bootstrapping OAuth on a headless server

Both `case-calendar setup gcal` and `case-calendar setup m365` need an
interactive browser on first run: they spin up a local HTTP listener and
complete the OAuth redirect on `http://localhost:<port>/` on the same
machine. The auth URL is printed to the console, but opening it on a
different machine won't complete the flow — the post-sign-in callback
can't reach the headless server.

Practical answer: do the first-run authorization on a workstation with a
browser, then move the cached credentials to the prod box.

- **Google.** The refresh token lives in
  `~/.case-calendar/google-token.json`. Run `case-calendar setup gcal`
  on a workstation, then copy that file to the same path on prod
  (`scp ~/.case-calendar/google-token.json prod:~/.case-calendar/`). The
  daemon refreshes silently from there forever.
- **Microsoft.** Trickier, because `azure-identity` splits the cache:
  `~/.case-calendar/m365-token.json` holds only the
  `AuthenticationRecord` metadata, while the **refresh token itself lives
  in the OS keyring** (DPAPI on Windows, Keychain on macOS, libsecret on
  Linux). Copying the file alone is not enough — the prod keyring won't
  have the matching refresh token. Workarounds:
  - Run `case-calendar setup m365` from a graphical session on the prod
    box itself (X11-forwarded SSH, RDP, or a one-time desktop login), so
    the keyring on prod is populated directly.
  - Or pick a prod host that has at least one graphical login available
    for the one-time setup.

  A cleaner long-term option is to switch the M365 path to
  `DeviceCodeCredential`, which prints a short code + URL you can punch in
  from any browser. Not implemented today — open an issue if you need it.

Once the credentials are in place, `case-calendar serve` runs headless
forever and auto-pushes to whichever backends have a staged token; only
the first authorization needs a browser.

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

The intended workflow is:

1. Edit `config.yaml` to list the cases / dockets / calendars you want.
2. Optionally run `case-calendar setup gcal` and/or
   `case-calendar setup m365` once to authorize push backends.
3. `case-calendar sync` to backfill existing hearings + deadlines onto
   your calendars.
4. `case-calendar serve` to receive future updates from CourtListener
   webhooks in real time.

```bash
case-calendar sync                       # pull updates + auto-emit affected calendars
case-calendar sync --no-emit             # skip the auto-emit (rare; mostly for tests)
case-calendar sync --case us-v-wang      # sync just one case
case-calendar emit                       # re-render ICS + push (e.g. after editing config)
case-calendar show                       # dump current hearings + deadlines
case-calendar show --case us-v-wang      # one case
case-calendar serve --port 8000          # real-time webhook receiver (auto-emits)
case-calendar setup gcal                 # one-time Google Calendar OAuth
case-calendar setup m365                 # one-time Microsoft 365 / Outlook OAuth
```

`sync`, `serve`, and `emit` all auto-emit ICS for every configured
calendar and auto-push to gcal / M365 for any backend with a staged OAuth
token — no per-command flag required. The standalone `emit` command is
useful for forcing a re-render (e.g. after editing config) without
pulling new data; the `setup` commands handle the one-time OAuth flows.

Run `sync` on a cron — the SQLite store dedupes already-seen entries, so
re-running is cheap. PDFs that weren't yet on RECAP, or hearings whose
PDFs hadn't been OCR'd, get re-checked on each sync until available.

### Hosting the ICS feeds publicly (Caddy + index page)

ICS files are useful only if subscribers can reach them. The simplest
pattern is to point [Caddy](https://caddyserver.com/) at the `out/`
directory and let it serve everything under that path over HTTPS, with
automatic Let's Encrypt certs:

```caddyfile
calendars.example.com {
    root * /home/<you>/case-calendar/out
    file_server { index index.html }
    @ics path *.ics
    header @ics Content-Type "text/calendar; charset=utf-8"
}
```

A ready-to-edit template lives at `Caddyfile.example` in the repo root — copy it to `Caddyfile` (gitignored) and fill in your domain. With this in place,
subscribers point their calendar app at `https://calendars.example.com/<calendar>.ics`.
The same file also has a commented-out `webhook.example.com` block that
reverse-proxies the `case-calendar serve` receiver (see the webhook
section below), so one Caddy install can front both the public feed
site and the private webhook endpoint.

To go one step further and surface a public landing page that lists every
calendar (with one-click subscribe links) and every tracked case (with
docket links, date filed, and last activity, sortable client-side), add
three lines to `config.yaml`:

```yaml
index_path:      out/index.html
public_base_url: https://calendars.example.com   # where Caddy serves out/
site_title:      "My court calendar"
```

The page is regenerated on every `sync` / `serve` / `emit`. It is
self-contained HTML (no CDN, no third-party JS) with a manual dark-mode
toggle that respects `prefers-color-scheme`; the
[Darkreader](https://darkreader.org/) extension recognizes the page's
built-in dark theme via `<meta name="color-scheme">` and skips applying
its own filter on top.

### Real-time via webhooks (no polling burn)

CourtListener can push events to a URL you control instead of you polling.
This is by far the cheapest mode — zero CourtListener API calls per quiet hour, zero
daily-quota risk, and updates land within seconds of filing.

Setup:

1. Generate a secret and put it in `.env`:

   ```bash
   CASE_CALENDAR_WEBHOOK_SECRET=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')
   ```

2. `case-calendar serve --port 8000` — runs the receiver on localhost.
   On a PaaS (fly / Railway / Render) you'll want `--host 0.0.0.0 --port $PORT`
   so the platform's router can reach it.
3. Expose `127.0.0.1:8000` over public HTTPS. Pick the option that matches
   where you want the receiver to live:

   - **[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)** — easiest if you already use Cloudflare. No
     VPS, no static IP, no inbound port; the tunnel daemon dials out from
     your machine. Free for personal use.

     ```bash
     # quick / ephemeral (random *.trycloudflare.com URL each run):
     cloudflared tunnel --url http://localhost:8000

     # stable (named tunnel bound to a domain you own):
     cloudflared tunnel login
     cloudflared tunnel create case-calendar
     cloudflared tunnel route dns case-calendar webhook.example.com
     cloudflared tunnel run --url http://localhost:8000 case-calendar
     ```

   - **[Caddy](https://caddyserver.com/) on a small VPS** — best when you already have a box running
     and want a long-lived service that doesn't depend on a third party's
     tunnel. Caddy provisions and renews a Let's Encrypt cert
     automatically; point a subdomain at the VPS first.

     ```caddyfile
     webhook.example.com {
         reverse_proxy 127.0.0.1:8000
     }
     ```

     `Caddyfile.example` in the repo root ships this block ready to
     uncomment, alongside the static-site config for the ICS feeds — one
     Caddy process can serve both. Run `case-calendar serve` under
     systemd / docker / tmux on the same box; Caddy fronts it with TLS.

   - **[fly.io](https://fly.io/)** — container PaaS with a generous free tier and a
     persistent-volume option for the SQLite store. Suits the receiver
     well: low traffic, mostly idle, must keep one DB file around.

     ```bash
     fly launch --no-deploy           # generates fly.toml + Dockerfile
     fly volumes create cc_data --size 1
     # mount at /data, point store_path to /data/case-calendar.sqlite,
     # set CASE_CALENDAR_WEBHOOK_SECRET / COURTLISTENER_TOKEN /
     # <provider>_API_KEY via `fly secrets set`, then:
     fly deploy
     ```

     Set the internal port in `fly.toml` to 8000 and the CMD to
     `case-calendar serve --host 0.0.0.0 --port 8000`.

   - **[Railway](https://railway.com/)** — git-push deploys with a managed Postgres-ish UX.
     Easiest if you want a hosted dashboard for env vars + logs. Mount a
     volume for SQLite persistence (Railway's ephemeral filesystem
     otherwise wipes the store on redeploy).

     ```text
     Build:  uv sync
     Start:  uv run case-calendar serve --host 0.0.0.0 --port $PORT
     Vars:   CASE_CALENDAR_WEBHOOK_SECRET, COURTLISTENER_TOKEN,
             ANTHROPIC_API_KEY (or whichever provider you use)
     Volume: mount at /data, set store_path: /data/case-calendar.sqlite
     ```

   - **[Render](https://render.com/)** — similar to Railway; free tier exists but spins the
     service down on idle (a cold start adds ~30s to the first webhook
     after quiet, and CourtListener will retry, so it still works).
     Choose the paid "Background Worker" or "Web Service" tier with a
     persistent disk if you don't want cold starts.

     ```text
     Build:  uv sync
     Start:  uv run case-calendar serve --host 0.0.0.0 --port $PORT
     Disk:   mount at /data, set store_path: /data/case-calendar.sqlite
     ```

4. In the CourtListener dashboard's webhooks panel, register:

   ```text
   https://<your-host>/webhooks/case-calendar/<CASE_CALENDAR_WEBHOOK_SECRET>
   ```

   Event type: `DOCKET_ALERT`. Pick the highest webhook version.
5. In CourtListener, subscribe to docket alerts for every docket in your `config.yaml`
   (this is what tells CourtListener to send those events to your webhook).

The receiver makes one `/dockets/{id}/` lookup the first time it sees a new
docket (to learn its court timezone and citation), then zero CourtListener calls per
event after that. Polling sync still works alongside webhooks for backfill
or as a safety net.

**Security note:** CourtListener doesn't sign webhook payloads, so the secret in the
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
uv run pytest                                  # full suite (~12s, ~315 tests)
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
    m365.py           # Microsoft Graph (msgraph-sdk + azure-identity)
scripts/
  reprocess_entries.py    # re-run LLM against stored entries (after prompt changes)
  classify_significance.py # classify hearing significance (--all reclassifies, --apply writes)
```
