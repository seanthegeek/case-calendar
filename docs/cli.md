---
title: CLI reference
---

Every subcommand of `case-calendar`. All commands accept `-c / --config` to
point at a non-default config file:

```bash
uv run case-calendar -c production.yaml sync
```

[← Back to docs](index.md)

## `sync` — pull updates from CourtListener

Backfills new docket entries for every case in `config.yaml`, extracts
hearings and deadlines via the LLM, and re-emits affected calendars.

```bash
uv run case-calendar sync
```

| Flag | Purpose |
| --- | --- |
| `--case <case_id>` | Sync only this one case. |
| `--only-new` | Sync only cases whose dockets aren't yet in the store — useful after adding new cases to `config.yaml` without remembering their ids. |
| `--no-emit` | Skip the auto-emit at the end of the sync. Rare; mostly for tests. |
| `--force-summaries` | Regenerate every AI case summary in the same sync. Use after a model or prompt change so the CourtListener session is reused instead of running `summarize --force` separately. |
| `--reverify` | Force the per-row verify and dedupe sweeps to run even when no docket changed. By default a sync skips them for any case whose dockets all short-circuit (their verdicts can't change without new entries). Use after a verify-prompt or model change, or after an out-of-band store edit (`reprocess_entries.py` / `classify_significance.py`) that altered rows without advancing any docket. |

A three-tier short-circuit keeps quiet days cheap: the docket-level
`date_modified` cutoff, the per-entry `modified_after` filter, and the
content fingerprint dedup mean an unchanged docket costs roughly one cheap
CourtListener request and zero LLM calls. The end-of-sync verify and dedupe
sweeps are gated on the same signal — a case whose dockets all short-circuit
skips them entirely, because their inputs come from the local store and
can't have changed without new entries — so a fully-quiet sync makes no LLM
calls at all. (Webhook deliveries don't advance the stored cutoff, so a
docket touched by `serve` always re-runs its sweeps on the next poll.) Pass
`--reverify` to force the sweeps regardless.

Run on a cron once you're past the initial backfill — every five minutes is
fine; once an hour is plenty for most cases. Or skip cron entirely and use
[real-time webhooks](webhooks.md).

## `reconcile` — catch enriched placeholder entries cheaply

A docket-alert webhook delivers a new entry once, the moment it's docketed —
often as a stub whose document text isn't available yet (an empty
description plus a document that isn't on RECAP). CourtListener fills the
text in and makes the document available shortly after, but it fires only an
updated *email* alert for that change, not a second webhook
([CourtListener issue #7423](https://github.com/freelawproject/courtlistener/issues/7423)).
So `serve` never sees the enriched version, and a hearing or deadline whose
date lives only in the filled-in text can be missed until a poll re-reads
it. `reconcile` closes that gap without the per-docket cost of a full
`sync`: it re-checks only the entries that arrived as placeholders, one
CourtListener request each, so its cost scales with recent filing activity
rather than with the size of your caseload.

```bash
uv run case-calendar reconcile
```

| Flag | Purpose |
| --- | --- |
| `--case <case_id>` | Reconcile only this one case. |
| `--days <n>` | Only re-check placeholder entries filed within this many days (default 7). Bounds retries on stubs that never enrich — a placeholder older than the window drops out of scope. |
| `--no-emit` | Skip the auto-emit at the end of the reconcile. |

When a placeholder has enriched, the re-fetch re-runs the normal pipeline:
the entry's fingerprint flips, any new hearing or deadline is extracted, and
the case summary is regenerated if its posture changed — exactly as a `sync`
would, but touching only the handful of pending entries. An unchanged
placeholder is a no-op (the fingerprint matches, so no LLM call is made).

Intended to run on a frequent cheap cron alongside `serve`, with a full
`sync` kept as an infrequent catch-all. See
[real-time webhooks](webhooks.md#polling-webhooks-and-reconcile) for how the
three fit together.

## `serve` — real-time webhook receiver

Runs an HTTPS-ready webhook receiver. CourtListener pushes `DOCKET_ALERT`
events to it, and each delivery processes a single entry and re-renders
just the affected calendar in seconds. Bypasses the daily polling quota
entirely.

```bash
uv run case-calendar serve --host 127.0.0.1 --port 8000
```

| Flag | Purpose |
| --- | --- |
| `--host <addr>` | Bind address. Default `127.0.0.1`. |
| `--port <n>` | Bind port. Default `8000`. |

The receiver is HTTP, not HTTPS — put it behind Caddy / nginx / Cloudflare
Tunnel for the public-facing TLS. See [real-time webhooks](webhooks.md)
for the full registration walk-through.

## `emit` — force-render calendars

Re-renders every ICS file (and pushes to gcal / M365 if configured) without
pulling new data. Useful after editing `config.yaml` or hand-fixing a row in
the store.

```bash
uv run case-calendar emit
```

No flags. Always operates on all calendars.

## `setup gcal` / `setup m365` — one-time OAuth

Runs the interactive OAuth flow for a push backend and caches the refresh
token to disk.

```bash
uv run case-calendar setup gcal
uv run case-calendar setup m365
```

Run once per machine. Both flows open a browser, ask you to grant permission,
and write the token cache to the path configured by `google_token_path` /
`m365_token_path`. Subsequent runs of `sync` / `serve` / `emit` auto-push to
that backend with no flag.

See [calendar backends](calendars.md) for the Cloud Console / Entra app
registration steps that precede these.

## `summarize` — AI case summaries (opt-in)

Generates per-docket AI prose summaries for the index page. Gated on
`case_summaries.enabled: true` in `config.yaml`. Most users won't need to
run this manually — summaries auto-refresh during `sync` and `serve` when
a new primary document or disposition lands.

```bash
uv run case-calendar summarize
```

| Flag | Purpose |
| --- | --- |
| `--case <case_id>` | Summarize only this one case. |
| `--force` | Regenerate even when a summary row already exists. Use after a model or prompt change. |
| `--no-emit` | Skip the index.html re-emit after writing. |

See [case summaries](case-summaries.md).

## `show` — dump current state

Prints every hearing and deadline currently in the store, grouped by case.
Read-only — useful for sanity checks.

```bash
uv run case-calendar show
uv run case-calendar show --case us-v-wang
```

| Flag | Purpose |
| --- | --- |
| `--case <case_id>` | Limit to one case. |

## `prune` — clean up orphaned data

After you remove a case (or one of its dockets) from `config.yaml`, the
hearings, deadlines, entry-fingerprint cache, and AI summaries tied to the
deleted docket would otherwise live forever in the store. `prune` deletes
them. **Dry-run by default** — pass `--apply` to actually delete.

```bash
uv run case-calendar prune          # dry-run: print what would be deleted
uv run case-calendar prune --apply  # actually delete
```

Back up `data/case-calendar.sqlite` (and its `-wal` / `-shm` sidecar files,
or run `PRAGMA wal_checkpoint(TRUNCATE)` first) before applying.

## `webhook-url` — print the receiver URL

Composes the URL to paste into the CourtListener webhook dashboard. Uses
`CASE_CALENDAR_WEBHOOK_SECRET` from `.env`.

```bash
uv run case-calendar webhook-url --host webhook.example.com
# https://webhook.example.com/webhooks/case-calendar/<your-secret>
```

| Flag | Purpose |
| --- | --- |
| `--host <host>` | Public host where the receiver is reachable. `https://` is assumed unless you pass an explicit `http://` URL. |
| `--check` | After printing the URL, probe the receiver's secret-gated health endpoint to verify the host is reachable, Case Calendar is the service answering, and the secret in `.env` matches the one the running receiver expects. |

`--check` is the single command that catches the common deployment
failure modes: a Cloudflare access policy intercepting the path, a stale
Caddy config pointing at the wrong port, or a secret mismatch between the
config file and the running daemon.

## Global flag

| Flag | Purpose |
| --- | --- |
| `-c <path>`, `--config <path>` | Use a different config file. Default `config.yaml`. |

## Environment variables

The commands above read their secrets and provider settings —
`COURTLISTENER_TOKEN`, the `*_API_KEY` keys, `LLM_PROVIDER` / `LLM_MODEL` and
the per-track overrides, `CASE_CALENDAR_WEBHOOK_SECRET`, `M365_CLIENT_ID`,
`LOG_LEVEL`, and the Ollama settings — from `.env` in the project root, loaded
automatically via `python-dotenv` before any module touches the environment.
See [Configuration → Environment variables](configuration.md#environment-variables)
for the complete table.
