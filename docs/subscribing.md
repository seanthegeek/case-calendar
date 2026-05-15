---
title: Subscribing to a feed
---

Every case-calendar calendar publishes an `.ics` URL — the universal
iCalendar subscription format ([RFC 5545](https://www.rfc-editor.org/rfc/rfc5545)).
Once that URL is reachable on the public web (see [the public index
page](public-page.md) for one Caddy template), any modern calendar app
can subscribe to it and pull updates on its own schedule.

This page collects each major app's official subscribe-by-URL flow. The
URL you'll paste is whatever your deployment serves the `.ics` from —
e.g. `https://calendars.example.com/cybercrime.ics`.

[← Back to docs](index.md)

## A note on refresh frequency

Subscribed feeds refresh on the receiving calendar app's own schedule —
not on yours, and not when the underlying `.ics` file changes. The
numbers vary by app and most aren't configurable. Best published
intervals at the time of writing:

| App | Refresh interval |
| --- | --- |
| Apple Calendar (macOS / iOS) | User-configurable: 5 min, 15 min, hourly, daily, weekly, or manual |
| Google Calendar | Not documented; typically every several hours, no user setting |
| Outlook on the web / Outlook.com | "Approximately every 3 hours"; can take up to 24h |
| Proton Calendar | Every 4–16 hours |
| Fastmail | Hourly |
| Thunderbird | User-configurable per calendar; default 30 min |

If a hearing date moves and you can't wait for the next refresh, most
apps let you remove and re-add the subscription to force an immediate
fetch. For zero-lag updates on the same events, see the [Google
Calendar / Microsoft 365 push backends](calendars.md), which write
events directly rather than waiting for the subscriber's pull.

## Apple Calendar — macOS

Per [Apple's macOS Calendar User Guide](https://support.apple.com/guide/calendar/subscribe-to-calendars-icl1022/mac):

1. Open the **Calendar** app.
2. Choose **File → New Calendar Subscription**.
3. Paste the `.ics` URL and click **Subscribe**.
4. Pick a name and color.
5. For **Location**, choose **iCloud** to sync the subscription to
   every device on your Apple Account, or **On My Mac** to keep it
   local to this machine.
6. Set **Auto-refresh** (5 min, 15 min, hourly, daily, weekly, or
   never).
7. Click **OK**.

Subscribed calendars are read-only — events come from the publisher.

## Apple Calendar — iPhone / iPad

Per [Apple's iCloud calendar subscription guide](https://support.apple.com/en-us/102301)
(steps differ slightly between iOS 26+ and iOS 18 or earlier):

**iOS / iPadOS 26 or later:**

1. Open the **Calendar** app.
2. Tap **Calendars** at the bottom of the screen.
3. Tap **Add Calendar → Add Subscription Calendar**.
4. Paste the `.ics` URL and tap **Find**.
5. Give it a name and color.
6. Next to **Account**, choose **iCloud** (so the subscription
   syncs to all your Apple devices), then tap **Done**.

**iOS / iPadOS 18 or earlier:**

1. Open the **Calendar** app.
2. Tap **Calendars** at the bottom.
3. Tap **Add Calendar → Add Subscription Calendar**.
4. Paste the URL and tap **Subscribe**.
5. Name it and choose a color, then under **Account** pick
   **iCloud** and tap **Add**.

Older iOS releases also expose the same flow under **Settings →
Calendar → Accounts → Add Account → Other → Add Subscribed Calendar**.

## Google Calendar — web

Per [Google's "Add other calendars" help page](https://support.google.com/calendar/answer/37100):

1. Open [Google Calendar](https://calendar.google.com/) on a
   **computer web browser** (Google does not support adding a URL
   subscription from the Android or iOS Google Calendar apps).
2. On the left, under **Other calendars**, click the **+** button.
3. Choose **From URL**.
4. Paste the `.ics` URL and click **Add calendar**.

Shortcut: the **Add by URL** form is also reachable directly at
[calendar.google.com/calendar/u/0/r/settings/addbyurl](https://calendar.google.com/calendar/u/0/r/settings/addbyurl)
— skip the sidebar entirely.

The new calendar appears under **Other calendars**. It is read-only
and syncs to every device signed in to the same Google account once
it is added on the web. Google does not publish a refresh interval
and there is no manual-refresh button — pulls happen on Google's own
schedule, typically every several hours.

## Microsoft Outlook — Outlook.com, Outlook on the web, and the new Outlook for Windows

Per [Microsoft's "Import or subscribe" help page](https://support.microsoft.com/en-us/office/import-or-subscribe-to-a-calendar-in-outlook-com-or-outlook-on-the-web-cff1429c-5af6-41ec-a5b4-74f2c278e98c):

1. Open Outlook on the web (or the new Outlook for Windows) and go
   to **Calendar**.
2. In the left sidebar, click **Add calendar**.
3. Choose **Subscribe from web**.
4. Paste the `.ics` URL.
5. Click **Import**.

Microsoft notes that "whenever events change on an iCal, it can take
more than 24 hours for Outlook on the web to update your calendar,"
with typical updates "approximately every 3 hours." If the
subscription fails, paste the URL into a browser tab first to
confirm it serves an `.ics` file — most subscription failures are an
unreachable or mistyped URL.

## Proton Calendar

Per [Proton's "Subscribe to an external calendar" help page](https://proton.me/support/subscribe-to-external-calendar):

1. Sign in at [calendar.proton.me](https://calendar.proton.me/) (or
   open the desktop / mobile Proton Calendar app).
2. Open **Settings → All settings → Calendars → Other calendars**.
3. Click **Add calendar from URL**.
4. Paste the `.ics` URL and confirm.

Proton refreshes external subscriptions every 4–16 hours. A
subscribed external calendar counts toward your plan's overall
calendar quota and is read-only (no editing, sharing, or export).

## Fastmail

Per [Fastmail's calendar synchronization help](https://www.fastmail.help/hc/en-us/articles/360058752754-How-to-synchronize-a-calendar):

1. Sign in to Fastmail on the web.
2. Open **Settings → Calendars**.
3. Scroll to the **Subscriptions** section.
4. Paste the `.ics` URL into the text box and click **Subscribe to
   calendar**.
5. On the next screen, set a name and color, then click **Save**.

Fastmail polls subscribed feeds hourly.

## Mozilla Thunderbird

Per [Mozilla's "Creating new calendars" help page](https://support.mozilla.org/en-US/kb/creating-new-calendars):

1. In Thunderbird's calendar pane, open **File → New → Calendar…**
   (or click the **≡** menu → **New → Calendar…**, or right-click in
   the calendar list).
2. Choose **On the Network** and click **Next**.
3. Leave the username field empty (case-calendar feeds are public —
   no credentials required). If the dialog has a "This location
   doesn't require credentials" checkbox, tick it.
4. Paste the `.ics` URL into the **Location** field.
5. Click **Find Calendars**, pick the calendar from the result list,
   and click **Subscribe**.

Thunderbird's per-calendar properties dialog lets you set the
refresh interval (default 30 minutes).

## Verifying the URL

Across every app, the first thing to check when a subscription fails
is whether the URL actually serves `.ics` content. Paste it into a
browser address bar:

- A working URL downloads (or displays as text) a file beginning
  with `BEGIN:VCALENDAR`.
- A 404, a redirect to a login page, or HTML output means the URL
  is wrong or the host isn't serving the file correctly.

If the server returns `text/plain` instead of `text/calendar`,
desktop calendar apps may still subscribe but some mobile apps
won't — see the [public index page Caddy template](public-page.md#hosting-with-caddy)
for the correct MIME type.

## Next steps

- [Calendar backends](calendars.md) — the rest of what each backend
  does (push, attendees, reminders, OAuth).
- [Public index page](public-page.md) — generate a landing page with
  one-click subscribe buttons for every calendar.
