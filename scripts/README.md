# `scripts/`

Operator and developer helpers that sit outside the `case_calendar` package:
one-shot maintenance tools, a webhook tester, and the deployment wrappers for
the production host. None of them are imported by the application — they're run
by hand.

Run everything **from the repo root** (the scripts use repo-relative paths like
`data/case-calendar.sqlite`). The Python tools go through `uv run` so they pick
up the project venv; the deployment scripts are plain bash.

See also: [AGENTS.md](../AGENTS.md#one-shot-maintenance-scripts-scripts) for the
maintainer-facing rationale and [docs/development.md](../docs/development.md) for
how the deployment scripts fit the dev → prod workflow.

## Maintenance (Python)

### `reprocess_entries.py`

Re-run the LLM extraction pipeline against specific stored docket entries,
identified by CourtListener `entry_id` (not docket position). It reads the
entry text from the local store, clears the entry's content fingerprint so the
dedup check doesn't short-circuit, and calls `CaseSyncer.process_entry` — the
same path a normal sync uses — so the entry is re-extracted under the *current*
prompt and related-entry context.

Why: after a prompt or model change you often want to see the new behavior on
entries already in the store without waiting for them to change upstream.

```bash
uv run python scripts/reprocess_entries.py 445337354 448991171
```

Caution: the LLM can allocate fresh `hearing_key`s if it now reads a
previously-vague entry as several specific events, so reprocessing can leave
duplicate rows that need manual cleanup. For a significance-only reclassify
(which can't create duplicates), use `classify_significance.py` instead.

### `classify_significance.py`

Classify the `major` / `minor` significance of stored hearings with a focused
single-question LLM prompt. It deliberately does **not** run the full action
extractor, so there's no risk of it allocating new hearing keys or splitting a
hearing — it only emits a significance verdict plus a one-line reason per row.

Why: significance controls calendar visibility (minor rows are filtered out at
render). This backfills rows whose significance is `NULL` (or re-grades every
row) without the duplicate-key hazard of full reprocessing.

```bash
# Dry run: classify only NULL-significance rows, print verdicts, change nothing.
uv run python scripts/classify_significance.py

# Re-grade every row (still dry-run unless --apply is also passed).
uv run python scripts/classify_significance.py --all

# Write the verdicts back to the store.
uv run python scripts/classify_significance.py --apply
```

Flags: `--db <path>` (default `data/case-calendar.sqlite`), `--all` (every row,
not just NULL), `--apply` (persist; omit for a dry run).

### `heal_proceeding_notes.py`

Backfill hearings whose calendar description (`notes`) regressed to a
pre-hearing administrative notice — a clerk's notice of Zoom access / courtroom
change / scheduling — instead of the record of what actually happened. The
sweep (`sync.heal_proceeding_notes`) is **deterministic**: no LLM, no
CourtListener. For every hearing whose notes are empty or an administrative
notice but whose source entries include the *record* of the proceeding (a
minute entry / transcript / clerk's notes of proceedings held), it rebuilds the
notes from that record's own docket text. Hearings that already describe the
proceeding, and hearings carrying a curated non-administrative note, are left
untouched. Because a row's source list legitimately pools related proceedings
(the status conference that *scheduled* a hearing is one of its sources), the
chosen record must both restate the row's own date AND name the same kind of
proceeding the row is keyed for — so a `Sentencing` row won't adopt a same-date
`Status Conference` minute entry, and a row nothing in its sources clearly
matches is left alone.

Why: the sync-time fix (the MARK_HELD supersede + dedupe-aware notes selection
in `sync.py`) only prevents *new* regressions. A row already collapsed in the
store stays collapsed — the sibling that held the good notes was deleted by the
dedupe merge, so re-running `sync` can't recover it. This sweep is how you fix
the rows already in the store, in one pass.

```bash
# Dry run: list the hearings it would heal (old -> new), change nothing.
uv run python scripts/heal_proceeding_notes.py

# Write the healed notes back to the store.
uv run python scripts/heal_proceeding_notes.py --apply
```

Flags: `--db <path>` (default `data/case-calendar.sqlite`), `--apply` (persist;
omit for a dry run). It only rewrites the `notes` column — never schema,
status, dates, or source lists — but back up the store before `--apply` if you
care about the DB (see [AGENTS.md](../AGENTS.md)). Re-emit (or let the next
sync's auto-emit) afterward so the ICS feeds and index page pick up the new
text.

### `heal_drifted_keys.py`

Canonicalize hearings left with a drifted `base-N` key — the "Sentencing
Lytvynenko 2" class, where a stray trailing number leaks into the
subscriber-facing title. This happens when CourtListener stores one logical
PACER docket as several records (its reconciler does this when a docket's
upstream `pacer_case_id` changes mid-life): the extractor used to treat the
records as different dockets and mint a fresh `-2` key for an event a sibling
record already had, and the end-of-sync deduplication then kept the `-2` row as
the survivor. The sweep (`sync.heal_drifted_keys`) is **deterministic**: no LLM,
no CourtListener.

It only ever touches rows it can *prove* are drift, by two signals: (1) the
survivor's audit trail records that it absorbed its own suffix-free base and
that base no longer exists → rename `base-N` → `base` (and recompute the
key-derived title); (2) the suffix-free `base` still exists at the same time in
the same logical docket group → delete the `-N` row after folding its source
entries into `base`. A *meaningful* trailing number — a second status
conference, trial day 2, a date — has neither signal and is left untouched.

Why: the code fix (`sync.py` / `llm.py`) stops *new* drift, but a row already
collapsed in the store stays collapsed — its suffix-free sibling was already
deleted by the dedupe merge, so re-running `sync` can't re-collapse it. This
sweep repairs the rows already in the store, in one pass.

```bash
# Dry run: list the keys it would rename / merge (old -> new), change nothing.
uv run python scripts/heal_drifted_keys.py

# Write the changes back to the store.
uv run python scripts/heal_drifted_keys.py --apply
```

Flags: `--db <path>` (default `data/case-calendar.sqlite`), `--apply` (persist;
omit for a dry run). Renaming a key changes its ICS UID and Google / Microsoft
365 event id, so subscribers' clients re-create those events once — back up the
store before `--apply` (see [AGENTS.md](../AGENTS.md)) and re-emit afterward (or
let the next sync's auto-emit) so the feeds and index page pick up the clean
keys and titles.

## Webhook testing

### `test_webhook.py`

POST a CourtListener-shaped `DOCKET_ALERT` payload at a running `case-calendar
serve` receiver. A fresh `Idempotency-Key` is generated per run so repeated
invocations exercise the full pipeline rather than short-circuiting on the
idempotency dedup table.

Why: confirm a deployed receiver (and the Caddy / TLS path in front of it)
actually accepts and processes a delivery, end to end, without waiting for
CourtListener to fire a real alert. See [docs/webhooks.md](../docs/webhooks.md)
for the receiver setup.

```bash
# From a JSON file:
uv run python scripts/test_webhook.py \
    https://webhook.example.com/webhooks/case-calendar/<SECRET> \
    payload.json

# From stdin:
some-command | uv run python scripts/test_webhook.py <url> -

# Re-send with a fixed key to exercise the duplicate-ack path:
uv run python scripts/test_webhook.py <url> payload.json --idempotency-key abc123
```

You supply your own payload JSON — a captured real delivery works well.
(`scripts/*.json` is gitignored, so any payload you drop here stays local.)

## Deployment (bash, over ssh)

Wrappers that operate the production deployment over ssh. They hardcode no
host-specific values: every server address, login user, and path is read from
`.env` through `scripts/_prod-env.sh`. Set these in your `.env` to use them
(they are deployment-identifying and deliberately not in `.env.example`):

| Variable | What it is |
| --- | --- |
| `CC_PROD_HOST` | prod server hostname or IP for ssh / scp |
| `CC_PROD_SSH_USER` | ssh login user on the prod host (e.g. `root`) |
| `CC_PROD_APP_DIR` | absolute path to the install on prod (e.g. `/opt/case-calendar`) |
| `CC_PROD_SERVICE` | systemd unit name + unix service account (assumed identical) |
| `CC_PROD_STAGE_DIR` | a dir on prod writable by the ssh user, used to stage scp drops |

`_prod-env.sh` (sourced, not run) loads and validates those five and exits with
a clear error if any is missing. Its header is the authoritative reference.

### `sync-prod`

Run `case-calendar sync` on prod over ssh, streaming output back to your
terminal. Forwards extra args (`--case …`, `--force-summaries`). Doesn't stop
the service — sync and serve coexist safely under WAL journaling.

```bash
./scripts/sync-prod
./scripts/sync-prod --case us-v-knoot
./scripts/sync-prod --force-summaries
```

### `reconcile-prod`

Run `case-calendar reconcile` on prod over ssh, streaming output back to your
terminal. Same shape as `sync-prod`, but runs the cheap placeholder re-check
instead of a full sync — it re-fetches only the entries that arrived as
placeholders (one CourtListener request each) to pick up the upstream
enrichment a webhook delivery can't see (see CourtListener issue #7423).
Forwards extra args (`--case …`, `--days …`). Doesn't stop the service —
reconcile and serve coexist safely under WAL journaling.

```bash
./scripts/reconcile-prod
./scripts/reconcile-prod --case us-v-knoot
./scripts/reconcile-prod --days 14
```

### `sync-via-prod`

Push your local `config.yaml` to prod, sync there (so prod's CourtListener
token does the work, not dev's), restart the service to pick up the new config,
then pull the resulting store back to dev. Config flows dev → prod, DB flows
prod → dev — intentionally one-way each. Backs up the local store first.

Why: the normal way to add or edit a case — propagate the config and let prod
do the backfill, then realign dev with prod's view in one step.

```bash
./scripts/sync-via-prod                       # propagate config + sync + pull DB
./scripts/sync-via-prod --case us-v-dubranova # forwarded to `sync`
./scripts/sync-via-prod --prune               # also `prune --apply` removed dockets
```

`--prune` is consumed by the script (runs `case-calendar prune --apply` before
the sync, for dockets you removed from config); every other arg forwards to
`sync`. Pruning is destructive and prod has no in-script backup — opt in only
when `git diff config.yaml` confirms the orphaning was intentional.

### `pull-prod-db`

Copy prod's `case-calendar.sqlite` down to dev — DB only, no prod-side sync, no
restart. Uses SQLite's online-backup API so prod's serve process keeps handling
webhooks during the copy, and backs up the local store before overwriting.

Why: mirror prod's exact current state on dev — to compare outputs under new
code, debug a prod issue locally, or capture a known-good pre-state before
something destructive.

```bash
./scripts/pull-prod-db
```

### `push-db-to-prod`

The inverse of `pull-prod-db`: upload the local store **plus `.env` and
`config.yaml`** to prod, pull latest code, re-emit, and restart. Prompts for
confirmation first, because it overwrites prod state.

Why: promote a locally-built store (e.g. a full re-summarize under new prompts)
to production in one step.

```bash
./scripts/push-db-to-prod      # answers a y/N confirmation prompt
```

### `upgrade-prod`

Apt package upgrades, push the local `.env`, pull latest code, `uv sync`,
re-emit, restart, and flag if the box wants a reboot. Leaves the prod DB and
`config.yaml` untouched (use `push-db-to-prod` for those).

Why: routine host + app maintenance in one command.

```bash
./scripts/upgrade-prod
```

### `_prod-env.sh`

Not run directly — sourced by every deployment script above to load the
`CC_PROD_*` settings from `.env`. Documented here for completeness; edit it only
to change how the prod connection settings are loaded.
