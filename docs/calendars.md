---
title: Calendar backends
---

case-calendar always writes an ICS file. Optionally, it also pushes the same
events to Google Calendar and/or Microsoft 365 / Outlook directly. Both
push backends auto-enable after a one-time OAuth flow — there's no per-run
flag.

[← Back to docs](index.md)

## ICS — works with everything

Every calendar in `config.yaml` writes an ICS file to its `ics_path`. ICS
(also called iCalendar, RFC 5545) is the universal format every modern
calendar app understands.

Subscribing to it depends on the app:

- **Apple Calendar (macOS / iOS):** Settings → Accounts → Add Account →
  Other → CalDAV / Subscribed Calendar → paste the URL.
- **Proton Calendar:** Settings → Calendars → Add subscribed calendar.
- **Thunderbird:** File → New → Calendar → On the Network → iCalendar URL.
- **Google Calendar (read-only subscription, not push):** "Other calendars"
  → "Add by URL".
- **Outlook desktop / web:** "Add calendar" → "Subscribe from web".

The ICS file is self-contained. Once you've hosted it somewhere reachable
(see [public index page](public-page.md) for the Caddy template), any
subscriber pulls updates on their own client's refresh schedule. No account
linking needed.

Events are tagged with the court's IANA timezone (e.g. `America/Los_Angeles`).
A 3 PM Pacific hearing stays "3 PM Pacific" through Daylight Saving and
displays in each viewer's local time.

## Google Calendar push

One-time setup in the Google Cloud Console:

1. **Create or pick a Google Cloud project** at
   [console.cloud.google.com/projectcreate](https://console.cloud.google.com/projectcreate).
2. **Enable the Google Calendar API** at
   [console.cloud.google.com/apis/library/calendar-json.googleapis.com](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com).
3. **Configure the OAuth consent screen:** APIs & Services → OAuth consent
   screen. Pick "External" user type unless you're on Workspace, fill in
   the app name and your email, and add yourself as a test user. No scopes
   need to be pre-declared — the desktop flow requests them dynamically.
4. **Create an OAuth client of type "Desktop app":** APIs & Services →
   Credentials → Create credentials → OAuth client ID → Desktop app.
   Download the JSON and save it to a path you'll reference from
   `config.yaml` — by convention `tokens/google-credentials.json` inside
   the project.
5. **Find the calendar id** you want events written to. Open Google
   Calendar in a browser → click the calendar name in the left sidebar →
   Settings and sharing → "Integrate calendar" → Calendar ID. It looks
   like `xxx@group.calendar.google.com` (or just your address for your
   primary calendar).
6. **Wire it into `config.yaml`:**

   ```yaml
   google_credentials_path: tokens/google-credentials.json
   # google_token_path:       tokens/google-token.json  # default

   calendars:
     cybercrime:
       google_calendar_id: xxx@group.calendar.google.com
   ```

7. **Authorize once:**

   ```bash
   uv run case-calendar setup gcal
   ```

   This opens a browser, you grant Calendar access, and the refresh token
   is cached at `google_token_path`. Every subsequent `sync` / `serve` /
   `emit` auto-pushes silently — no browser needed.

Event IDs are deterministic (`sha1(case_id::hearing_key)`), so reschedules
and detail updates land on the same event rather than creating duplicates.
Cancelled hearings are patched to `status: cancelled` so they disappear from
subscribers' views.

## Microsoft 365 / Outlook push

Uses the official
[`msgraph-sdk`](https://github.com/microsoftgraph/msgraph-sdk-python) +
[`azure-identity`](https://learn.microsoft.com/en-us/python/api/overview/azure/identity-readme)
libraries — no third-party O365 wrappers.

One-time setup in the Microsoft Entra admin center:

1. **Register an application** at
   [entra.microsoft.com](https://entra.microsoft.com) → Identity →
   Applications → App registrations → New registration. Name it
   `case-calendar` (or whatever). Pick the right account-type tier:
   - **Personal Microsoft accounts only** — for a personal
     `outlook.com` / `hotmail.com` calendar.
   - **Accounts in this organizational directory only** — for a
     work / school mailbox in your own tenant.
   - **Multi-tenant + personal** — if you want both to work.

   Leave the Redirect URI blank for now.

2. **Add the delegated `Calendars.ReadWrite` permission:** API permissions
   → Add a permission → Microsoft Graph → Delegated permissions →
   `Calendars.ReadWrite`. If your account is a work / school account in a
   tenant that requires admin consent, click "Grant admin consent" too.

3. **Mark it as a public client:** Authentication → Advanced settings →
   "Allow public client flows" = Yes. Then under Platform configurations
   → Add a platform → Mobile and desktop applications → tick
   `http://localhost`. Public clients use no client secret — the
   interactive browser flow proves identity.

4. **Copy the Application (client) ID** from the app's Overview page
   (UUID-shaped). It's a public identifier, not a secret.

5. **Find the calendar id** (optional — omit to push to the user's
   default calendar). The easiest way is the
   [Graph Explorer](https://developer.microsoft.com/en-us/graph/graph-explorer),
   signed in as the target user, running `GET https://graph.microsoft.com/v1.0/me/calendars`
   and copying the `id` of the calendar you want. It looks like
   `AAMkADEx...`.

6. **Wire it in.** Either env or config works for the client id:

   ```bash
   # .env
   M365_CLIENT_ID=00000000-0000-0000-0000-000000000000
   ```

   ```yaml
   # config.yaml
   # m365_client_id: "00000000-0000-0000-0000-000000000000"
   # m365_token_path: tokens/m365-token.json  # default

   calendars:
     cybercrime:
       m365_calendar_id: "AAMkADExAAA..."     # specific Outlook calendar
       # m365_use_default_calendar: true      # or push to the user's primary
   ```

7. **Authorize once:**

   ```bash
   uv run case-calendar setup m365
   ```

   This opens a browser, you grant `Calendars.ReadWrite`, and the resulting
   authentication record is cached at `tokens/m365-token.json` *plus* the
   OS keyring (DPAPI on Windows, Keychain on macOS, libsecret on Linux).

Idempotency: Graph generates event ids server-side (unlike Google's
deterministic ids), so case-calendar caches the returned id on the local
row after the first create. A stable `CaseCalendarKey` extended property
lets the push recover the right event by `$filter` lookup if the local
cache is ever wiped.

## Bootstrapping OAuth on a headless server

Both `setup gcal` and `setup m365` open a browser on first run. If your
production box is headless (no graphical session), do the first-run
authorization on a workstation with a browser, then move the credentials.

- **Google.** The refresh token lives entirely in
  `tokens/google-token.json`. Run `setup gcal` on a workstation, then
  `scp tokens/google-token.json prod:/opt/case-calendar/tokens/`. The
  daemon refreshes silently from there forever.

- **Microsoft.** Trickier — `azure-identity` splits the cache.
  `tokens/m365-token.json` holds only the metadata; the refresh token
  itself lives in the OS keyring. Copying the file alone is not enough.
  Either:
  - Run `setup m365` from a graphical session on the prod box itself
    (X11-forwarded SSH, RDP, or a one-time desktop login), so the prod
    keyring is populated directly.
  - Or pick a prod host that has at least one graphical login
    available for the one-time setup.

Once the credentials are in place, the daemon runs headless forever.

## Notifications: attendees and reminders

Configurable per-calendar (with optional per-case override) in `config.yaml`:

```yaml
calendars:
  cybercrime:
    notify_emails:
      - me@example.com
    reminders:
      - {method: popup, minutes: 30}
      - {method: popup, minutes: 1440}    # 1 day before
```

- **`notify_emails`** — added as Google Calendar attendees (so the address
  gets the invitation + the event lands on their own calendar) and as ICS
  `ATTENDEE` lines. In Microsoft Graph, attendees are not part of this
  feature today.

- **`reminders.method=popup`** — fires in any subscriber's local calendar
  app from a `VALARM:DISPLAY` block (in ICS) or a Google reminder override
  (in gcal). Safe to ship on a public calendar — popup VALARMs leak nothing.

- **`reminders.method=email`** — in Google, only delivered to the calendar
  owner. In ICS, rendered as `VALARM;ACTION:EMAIL` with `ATTENDEE` lines,
  so emails *are* visible to anyone reading the public feed. Avoid on
  public calendars.

> ⚠️ **Privacy:** `notify_emails` are visible to anyone with view access
> to the event. Don't list them on a calendar you intend to make public —
> use popup-only reminders and let subscribers configure their own clients.

## Next steps

- [Real-time webhooks](webhooks.md) — replace polling with push.
- [Public index page](public-page.md) — generate a static landing page
  with subscribe buttons for every calendar.
