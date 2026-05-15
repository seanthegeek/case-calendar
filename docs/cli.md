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

A three-tier short-circuit keeps quiet days cheap: the docket-level
`date_modified` watermark, the per-entry `modified_after` filter, and the
content fingerprint dedup mean an unchanged docket costs roughly one cheap
CourtListener request and zero LLM calls.

Run on a cron once you're past the initial backfill — every five minutes is
fine; once an hour is plenty for most cases. Or skip cron entirely and use
[real-time webhooks](webhooks.md).

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
| `--check` | After printing the URL, probe the receiver's secret-gated health endpoint to verify the host is reachable, case-calendar is the service answering, and the secret in `.env` matches the one the running receiver expects. |

`--check` is the single command that catches the common deployment
failure modes: a Cloudflare access policy intercepting the path, a stale
Caddy config pointing at the wrong port, or a secret mismatch between the
config file and the running daemon.

## Global flag

| Flag | Purpose |
| --- | --- |
| `-c <path>`, `--config <path>` | Use a different config file. Default `config.yaml`. |

## Environment variables

| Variable | Purpose |
| --- | --- |
| `COURTLISTENER_TOKEN` | Required. Your CourtListener API token. |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | Set at least one for the extractor pipeline. The tool auto-detects which provider to use. |
| `LLM_PROVIDER` | Optional. Force a specific extractor provider (`anthropic` / `openai` / `gemini`). |
| `LLM_MODEL` | Optional. Override the extractor model. |
| `LLM_SUMMARY_PROVIDER` | Optional. Force a specific summary-pipeline provider. |
| `LLM_SUMMARY_MODEL` | Optional. Override the summary model. |
| `CASE_CALENDAR_WEBHOOK_SECRET` | Required for `serve` and `webhook-url`. A long random string included in the webhook URL — CourtListener has no signing mechanism, so the URL secret is the auth model. |
| `M365_CLIENT_ID` | Alternative to `m365_client_id` in `config.yaml`. |
| `LOG_LEVEL` | Optional. `DEBUG` for verbose output; default `INFO`. |

Environment variables are read from `.env` in the project root via
`python-dotenv` before any module touches the environment.
