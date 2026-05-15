---
title: Real-time webhooks
---

By default case-calendar polls CourtListener on a cron. That works, but
CourtListener throttles the free tier (300 requests per day) and a polling
schedule means the ICS file is only refreshed at each cron tick —
minutes or hours after CourtListener has the entry, depending on how
often the cron runs.

Webhooks flip the model. CourtListener calls your receiver the moment a new
entry lands on a docket you've subscribed to. The receiver processes the
entry, updates the SQLite store, and re-renders just the affected calendar
in seconds — no polling, no quota burn. (How soon a subscriber's
calendar app then re-reads the ICS file is its own refresh schedule —
see [Limitations](index.md#limitations) for the end-to-end chain.)

[← Back to docs](index.md)

## What you'll set up

1. A long random shared secret in `.env` (`CASE_CALENDAR_WEBHOOK_SECRET`).
2. A small HTTPS endpoint that CourtListener can `POST` to, typically Caddy
   in front of `case-calendar serve`.
3. One webhook registration in the CourtListener dashboard.
4. One docket alert per docket in your `config.yaml`.

The whole thing is a 10-minute setup once your server has a public hostname.

## 1. Choose a secret

Generate a long random string and put it in `.env`:

```bash
# .env
CASE_CALENDAR_WEBHOOK_SECRET=PUT_A_LONG_RANDOM_STRING_HERE
```

CourtListener has no signing mechanism (no HMAC like Stripe / GitHub). The
secret embedded in the receiver URL *is* the auth model. Treat it like a
password — anyone who has it can submit forged events into your store.

The secret must be **URL-safe**: it goes straight into the receiver path
(`/webhooks/case-calendar/<secret>`), so it can't contain characters that
need percent-encoding (`+`, `/`, `=`, `&`, `?`, whitespace, etc.). Use
Python's `secrets.token_urlsafe`, which is purpose-built for this:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

That returns a 43-character string drawn from the URL-safe alphabet
(letters, digits, `-`, `_`) with 256 bits of entropy — plenty.

## 2. Run the receiver

```bash
uv run case-calendar serve --host 127.0.0.1 --port 8000
```

It's a stdlib `ThreadingHTTPServer` listening on plain HTTP — TLS happens
upstream in your reverse proxy, not in this process.

In production you'd run this as a systemd unit. There's a
[`case-calendar.service`](https://github.com/seanthegeek/case-calendar/blob/main/case-calendar.service)
template in the repo root you can adapt.

## 3. Front it with HTTPS

You need a public hostname pointing at port 8000 of the box running
`serve`. Most deployments use [Caddy](https://caddyserver.com/), which
handles the Let's Encrypt certificate automatically. The repo's
[`Caddyfile`](https://github.com/seanthegeek/case-calendar/blob/main/Caddyfile)
includes a working template:

```caddyfile
webhook.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Edit the hostname and either symlink the file into `/etc/caddy/Caddyfile`
or run `caddy run --config Caddyfile` directly. Cloudflare Tunnel, fly.io,
or a tailscale funnel work just as well — anything that gives you a stable
HTTPS URL pointing at port 8000.

## 4. Compute and verify the webhook URL

case-calendar will print the exact URL to register, with an optional probe
that verifies the secret matches:

```bash
uv run case-calendar webhook-url \
    --host webhook.example.com \
    --check
```

What `--check` does:

- Prints the full URL ready to paste:
  `https://webhook.example.com/webhooks/case-calendar/<your-secret>`
- Hits the secret-gated `/health` endpoint and confirms three things in
  one shot:
  - The host is reachable from the public internet.
  - case-calendar (and not, say, a Cloudflare access policy or a stale
    Caddy config) is actually answering on that path.
  - The secret in your `.env` matches the secret the running receiver
    expects.

If any of those is wrong, `--check` tells you which.

## 5. Register the webhook with CourtListener

Open
[courtlistener.com/profile/webhooks/](https://www.courtlistener.com/profile/webhooks/)
and create a new webhook:

- **Event type:** `DOCKET_ALERT`
- **Endpoint URL:** the URL printed by `webhook-url` (with your secret)
- **Enabled:** Yes

CourtListener fires a `Test` event you can use to confirm the connection.

## 6. Subscribe to docket alerts

The webhook fires only for dockets you have a docket alert on. For each
docket in `config.yaml`, open its CourtListener page and click "Get
alerts". (You can script this with the
[Docket Alerts API](https://www.courtlistener.com/help/api/rest/recap/#docket-alerts-endpoint),
but the UI is usually faster for a small case list.)

That's it. New entries on any of those dockets now flow into the ICS
file within seconds of CourtListener calling your receiver. When your
calendar app then re-reads the ICS feed is on its own refresh schedule
— see the [Limitations](index.md#limitations) section on the docs
landing page for the delivery chain end to end.

## How the receiver authenticates and dedupes

Two safety nets keep duplicate or retried deliveries from creating duplicate
rows:

- **URL secret check.** Every `POST` URL ends in `/<secret>`. The receiver
  compares against `CASE_CALENDAR_WEBHOOK_SECRET` with a constant-time
  comparison. Wrong secret → 404, no processing.
- **`Idempotency-Key` header.** CourtListener stamps each delivery with a
  stable key and retries failures using the same key. The receiver records
  every key it sees in the `webhook_events` table and acks duplicates
  without re-processing.

Even without the idempotency check, the per-entry fingerprint dedup in the
store means a double-delivery of the same content does no extra work.

## Polling and webhooks at the same time

You can run both safely. `case-calendar sync` (the polling path) and
`case-calendar serve` (the webhook path) share the same SQLite file and use
WAL journaling + a 5-second `busy_timeout` so concurrent writes don't
collide. There's no harm in keeping a once-an-hour cron running as a
safety net even when webhooks are healthy.

## Operational tips

- **Logs to watch:** The receiver logs each delivery with the case id, the
  docket id, and the number of entries it processed. A successful delivery
  takes well under a second; if you see latency spikes, check whether
  Caddy / Cloudflare is adding the lag.
- **CourtListener delivery retries:** CourtListener retries failed
  deliveries with the same `Idempotency-Key`. If a delivery fails because
  your server was down, CourtListener will replay it once it comes back,
  and the dedup tables make the replay a no-op if the entry already landed
  via a later polling sync.
- **The `webhook_events` table** is unbounded by default — it stores every
  idempotency key forever. On a busy installation you may want to truncate
  rows older than a few days; the dedup window CourtListener actually
  retries inside is hours, not weeks.

## Next steps

- [Public index page](public-page.md) — serve the ICS feeds and a
  landing page alongside the receiver.
- [Architecture](architecture.md) — how the receiver shares the sync
  pipeline.
