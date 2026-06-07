---
title: Real-time webhooks
---

By default Case Calendar polls CourtListener on a cron. That works, but
CourtListener throttles the free tier (125 requests per day; 300/day on the paid Tier 1) and a polling
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

To try it locally:

```bash
uv run case-calendar serve --host 127.0.0.1 --port 8000
```

It's a stdlib `ThreadingHTTPServer` listening on plain HTTP — TLS happens
upstream in your reverse proxy, not in this process. Keep it bound to
`127.0.0.1` so nothing but the local reverse proxy can reach it.

### Run it as a systemd service

In production you want `serve` running unattended under systemd as a
dedicated unprivileged user, with the install in `/opt/case-calendar`.

**Create the service user and install the code:**

```bash
# A locked-down system account that owns the install and runs the service.
sudo useradd --system --no-create-home --home-dir /opt/case-calendar --shell /usr/sbin/nologin case-calendar

# Create the install directory
sudo mkdir /opt/case-calendar

# Set ownership of the directory to case-calendar
sudo chown case-calendar:case-calendar /opt/case-calendar

# Clone the code into the directory as the service user
sudo -u case-calendar git clone https://github.com/seanthegeek/case-calendar.git /opt/case-calendar

# Install uv for the service user and build the venv. The unit below runs
# /opt/case-calendar/.local/bin/uv, which is where the installer drops it
# when HOME points at the install dir.
sudo -u case-calendar env HOME=/opt/case-calendar sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -u case-calendar env HOME=/opt/case-calendar /opt/case-calendar/.local/bin/uv sync
```

**Stage `/opt/case-calendar/.env`** (at minimum `COURTLISTENER_TOKEN`, one
`*_API_KEY`, and the `CASE_CALENDAR_WEBHOOK_SECRET` from step 1) and your
`config.yaml`, then lock the `.env` down — it holds credentials:

```bash
sudo chown case-calendar:case-calendar /opt/case-calendar/.env /opt/case-calendar/config.yaml
sudo chmod 600 /opt/case-calendar/.env
```

**Write the unit file** to `/etc/systemd/system/case-calendar.service`:

```ini
[Unit]
Description=case-calendar webhook receiver
Documentation=https://github.com/seanthegeek/case-calendar
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=case-calendar
Group=case-calendar
WorkingDirectory=/opt/case-calendar

# Loads COURTLISTENER_TOKEN, *_API_KEY, CASE_CALENDAR_WEBHOOK_SECRET, etc.
# Keep this file mode 0600, owned by the case-calendar user.
EnvironmentFile=/opt/case-calendar/.env

# uv's cache and the OAuth token caches resolve under HOME; pointing it
# inside the install dir lets ProtectHome=true safely block /home and /root.
Environment=HOME=/opt/case-calendar

# uv has no distro package; its install script drops the binary in
# $HOME/.local/bin of the user that ran it. With HOME set above, that's the
# path below — adjust if you installed uv somewhere else.
ExecStart=/opt/case-calendar/.local/bin/uv run case-calendar serve --host 127.0.0.1 --port 8000

Restart=on-failure
RestartSec=5s

# --- Hardening: shrink the unit's blast radius. None of these affect the
# expected runtime; they just limit what a compromised dependency could do. ---
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RestrictNamespaces=true
LockPersonality=true
RestrictRealtime=true

# serve writes only to data/, out/, and tokens/; everything else stays
# read-only under ProtectSystem=full.
ReadWritePaths=/opt/case-calendar/data
ReadWritePaths=/opt/case-calendar/out
ReadWritePaths=/opt/case-calendar/tokens

[Install]
WantedBy=multi-user.target
```

**Enable and start it:**

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now case-calendar
systemctl status case-calendar
journalctl -u case-calendar -f      # follow the logs
```

If you push events to Google Calendar or Microsoft 365, run the one-time
`setup gcal` / `setup m365` interactively as the service user *before*
starting the unit — the OAuth browser flow can't run headless:

```bash
sudo -u case-calendar env HOME=/opt/case-calendar \
    /opt/case-calendar/.local/bin/uv run case-calendar setup gcal
```

### Run reconcile on a systemd timer

`serve` gives you the real-time first touch, but it can't see the later
enrichment of an entry it already delivered (see
[Polling, webhooks, and reconcile](#polling-webhooks-and-reconcile)). A
`reconcile` timer fills that gap cheaply — it re-checks only the entries that
arrived as placeholders, so it costs a handful of CourtListener requests per
run regardless of caseload. Run it as an unprivileged `oneshot` on a timer,
under the same service user as `serve`.

**Write the service** to `/etc/systemd/system/case-calendar-reconcile.service`:

```ini
[Unit]
Description=case-calendar placeholder reconcile
Documentation=https://github.com/seanthegeek/case-calendar
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=case-calendar
Group=case-calendar
WorkingDirectory=/opt/case-calendar
EnvironmentFile=/opt/case-calendar/.env
Environment=HOME=/opt/case-calendar
ExecStart=/opt/case-calendar/.local/bin/uv run case-calendar reconcile

# Same hardening + write paths as the serve unit above.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RestrictNamespaces=true
LockPersonality=true
RestrictRealtime=true
ReadWritePaths=/opt/case-calendar/data
ReadWritePaths=/opt/case-calendar/out
ReadWritePaths=/opt/case-calendar/tokens
```

**Write the timer** to `/etc/systemd/system/case-calendar-reconcile.timer`:

```ini
[Unit]
Description=Run case-calendar reconcile hourly

[Timer]
OnCalendar=hourly
# Run a missed timer at next boot (e.g. after a reboot spanning the hour).
Persistent=true
# Spread load off the top of the hour so every box doesn't hit CourtListener
# at :00.
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
```

**Enable the timer** (enable the `.timer`, not the `.service` — the timer
pulls the service in on schedule):

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now case-calendar-reconcile.timer
systemctl list-timers case-calendar-reconcile.timer   # next run + last run
journalctl -u case-calendar-reconcile.service -f      # follow reconcile logs
```

Keep a full `sync` as an infrequent catch-all for anything `reconcile`
doesn't cover. Copy the two units above to `case-calendar-sync.service` /
`.timer`, swap the `ExecStart` to `... case-calendar sync` and the timer to
`OnCalendar=daily`. Mind your CourtListener request budget: a full `sync`
spends roughly one request per docket every run, while `reconcile` spends
one per pending placeholder — so the full sync is the one to keep
infrequent as the caseload grows.

## 3. Put it behind HTTPS with Caddy

CourtListener needs to reach your receiver over public HTTPS, but `serve`
speaks plain HTTP and binds `127.0.0.1`. Put a reverse proxy in front to
terminate TLS. [Caddy](https://caddyserver.com/) is the simplest — it gets
a Let's Encrypt certificate automatically. Point a subdomain's DNS at your
server, then add this to `/etc/caddy/Caddyfile`:

```caddyfile
webhook.example.com {
    reverse_proxy 127.0.0.1:8000

    # CourtListener retries failed deliveries; the receiver's own
    # Idempotency-Key dedup makes that safe. Don't add aggressive rate
    # limits here or you'll throw away legitimate retry traffic.
}
```

Replace `webhook.example.com` with your hostname and reload Caddy:

```bash
sudo systemctl reload caddy
```

Caddy needs inbound TCP/80 + TCP/443 reachable from the public internet for
the ACME challenge; if you're behind a firewall, use the DNS-01 plugin
instead. Cloudflare Tunnel, fly.io, or a tailscale funnel work too — anything
that gives you a stable HTTPS URL forwarding to port 8000.

> If you also serve the public ICS feeds from the same box, one Caddy
> install can host both — the feeds on one hostname and this webhook
> endpoint on another. See
> [Public index page → Hosting with Caddy](public-page.md#hosting-with-caddy)
> for the combined config.

## 4. Compute and verify the webhook URL

Case Calendar will print the exact URL to register, with an optional probe
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
  - Case Calendar (and not, say, a Cloudflare access policy or a stale
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

The webhook fires only for dockets you have a docket alert on, but
Case Calendar maintains that subscription list for you: every
`case-calendar sync` and `case-calendar serve` startup lists your
account's existing alerts via CourtListener's
[Docket Alerts API](https://www.courtlistener.com/help/api/rest/recap/#docket-alerts-endpoint),
compares against the union of docket ids configured under `cases:`,
and POSTs a subscription for any docket that isn't already covered.
Adding a case to `config.yaml` automatically wires up the docket alert
on the next sync; removing a case leaves the existing subscription
in place (no automatic cleanup).

Failures are logged but don't abort sync / serve — polling still works
without webhook alerts, and a temporary CourtListener outage during
the reconcile shouldn't block the rest of the pipeline. The summary
line in the log reads
`docket alerts: <created> created, <exists> already subscribed, <failed> failed`.

To opt out — say you maintain alerts through some other surface (a
bulk CSV upload, a separate admin tool) — set
`ensure_docket_alerts: false` at the top level of `config.yaml` (see the
[configuration reference](configuration.md#top-level-options)). The
reconciler then skips the list + create calls entirely on every run.

That's it. New entries on any of those dockets now flow into the ICS
file within seconds of CourtListener calling your receiver. When your
calendar app then re-reads the ICS feed is on its own refresh schedule
— see the [Limitations](index.md#limitations) section on the docs
landing page for the delivery chain end to end.

## How the receiver authenticates and dedupes

Two safety nets keep duplicate or retried deliveries from creating duplicate
rows:

- **URL secret check.** Every `POST` URL ends in `/<secret>`. The receiver
  compares against `CASE_CALENDAR_WEBHOOK_SECRET` with a direct string
  comparison. Wrong secret → 403, no processing.
- **`Idempotency-Key` header.** CourtListener stamps each delivery with a
  stable key and retries failures using the same key. The receiver records
  every key it sees in the `webhook_events` table and acks duplicates
  without re-processing.

Even without the idempotency check, the per-entry fingerprint dedup in the
store means a double-delivery of the same content does no extra work.

## Polling, webhooks, and reconcile

You can run all three safely. `case-calendar sync` (polling),
`case-calendar serve` (webhooks), and `case-calendar reconcile` (the
enrichment re-check) share the same SQLite file and use WAL journaling + a
5-second `busy_timeout` so concurrent writes don't collide.

Webhooks alone are not a complete picture of a docket. A CourtListener
docket alert fires **once**, when an entry is first docketed, and the
payload reflects the entry's state at that instant — which for many filings
is a stub: an empty description and a document that isn't on RECAP yet.
CourtListener fills in the entry text and makes the document available
shortly after, but it does **not** fire a second webhook for that update
([CourtListener issue #7423](https://github.com/freelawproject/courtlistener/issues/7423)).
So `serve` on its own never sees the enriched entry, and a hearing or
deadline whose date lives only in the filled-in text can be missed until a
poll re-reads it.

Reconciling that enrichment is the job of polling — and `reconcile` does it
cheaply. Instead of re-checking every docket like a full `sync`, it
re-fetches only the entries that arrived as placeholders (one CourtListener
request each), so its cost scales with recent filing activity, not caseload
size. The recommended shape for a webhook deployment:

- `serve` for real-time first-touch,
- `reconcile` on a frequent cheap cron (e.g. hourly) to pick up enrichment,
- a full `sync` on an infrequent cron (e.g. daily) as the catch-all.

There's no harm in running the full `sync` cron more often if your
CourtListener request budget allows — it's the original safety net, and it
catches anything `reconcile` doesn't. `reconcile` simply lets the common
case stay cheap as the caseload grows.

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
