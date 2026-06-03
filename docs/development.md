---
title: Development
---

This page is for contributors: how to get a working development environment,
run the app against real dockets, run the tests, and iterate on the
LLM-driven parts of the pipeline without spending a fortune doing it. If you
only want to *use* Case Calendar, start at [Installation](installation.md)
instead.

[← Back to docs](index.md)

## Prerequisites

- **Python 3.13** or newer.
- [**uv**](https://docs.astral.sh/uv/getting-started/installation/) for
  dependency management. It creates a project-local virtual environment and
  pins exact versions, so everyone — and CI — runs the same thing.
- A **CourtListener API token** (free account at
  [courtlistener.com](https://www.courtlistener.com/)).
- One **LLM API key** — Anthropic, Google (Gemini), or OpenAI. Anthropic is
  the recommended default; see [Installation](installation.md) for why.
- Optional but recommended: **poppler** and **tesseract** for the local OCR
  fallback. Without them the pipeline still runs — it just skips PDFs whose
  text it can't extract any other way and retries them on a later sync.

  ```bash
  sudo apt install poppler-utils tesseract-ocr   # Debian / Ubuntu
  brew install poppler tesseract                 # macOS
  ```

## Get the code

```bash
git clone https://github.com/seanthegeek/case-calendar
cd case-calendar
uv sync --extra test --extra lint
```

`uv sync` reads `pyproject.toml` and installs everything into
`.venv/`. The two extras pull in the test and lint toolchains
(pytest + coverage, and the version-pinned Ruff) so you can run the full
check suite. Prefix every command with `uv run` and uv handles activation
for you.

The test suite needs nothing else. If you want to run the **model-comparison
benchmark** against the committed, frozen input snapshot, fetch it once with
`git lfs install && git lfs pull` (it's a Git LFS object — see
[model-comparison/README.md](../model-comparison/README.md)).

## Configure secrets

Copy the example env file and fill in your two required secrets:

```bash
cp .env.example .env
```

```bash
COURTLISTENER_TOKEN=...        # from your CourtListener profile page
GEMINI_API_KEY=...
ANTHROPIC_API_KEY=...
```

The CLI loads `.env` automatically before any module reads an environment
variable. Nothing else is required to run a sync — Google Calendar and
Microsoft 365 push are opt-in and need their own one-time OAuth
(`setup gcal` / `setup m365`).

## Pick what to track

You have two starting points:

- **`config.example.yaml` → `config.yaml`** — the full template, documented
  inline. Copy it and edit the `cases:` list to the dockets you want.

  ```bash
  cp config.example.yaml config.yaml
  ```

- **`config.dev.yaml`** — a checked-in dev config covering only the cases that
  have driven a documented regression in one of the LLM-driven layers
  (extractor, verify pass, dedupe sweeps, summary pipeline). Each case is
  annotated with the failure mode it exercises. This is the fast inner loop
  for prompt and model work — it touches every documented failure mode for a
  fraction of a full-caseload run. Use it with `-c config.dev.yaml` on any
  command.

`config.yaml` is gitignored (it's your personal caseload). `config.dev.yaml`
is tracked, because the dockets in it are public CourtListener records and the
dev config is useful to everyone working on the project.

## First run, from scratch

```bash
uv run case-calendar -c config.dev.yaml sync
```

The first sync pulls each configured docket from CourtListener, runs the
regex pre-filter, sends the surviving entries through the LLM extractor, and
writes the resulting hearings and deadlines into the SQLite store at
`store_path`. It then renders the ICS files (and the index page, if
`index_path` is set) for every affected calendar. You'll see one progress
line per case, then one per calendar written:

```text
[us-v-moucka] entries_seen=42 entries_processed=11 actions=8 verified=11
[cybercrime (dev)] wrote 14 events -> out/dev/cybercrime.ics
```

Useful commands once the store is warm:

```bash
uv run case-calendar -c config.dev.yaml show         # dump current hearings + deadlines
uv run case-calendar -c config.dev.yaml summarize    # generate AI case summaries (opt-in)
uv run case-calendar -c config.dev.yaml emit         # re-render ICS/index without a CourtListener pull
uv run case-calendar -c config.dev.yaml serve        # run the webhook receiver instead of polling
```

The store is the source of truth; `emit` re-renders from it for free, so you
can iterate on the renderers without re-syncing.

## Run the tests

The suite is hermetic — no real HTTP, no real LLM, no real Google /
Microsoft Graph, no real keyring. Every external dependency is stubbed or
monkey-patched, and an autouse fixture strips any real `*_API_KEY` from your
shell so a test can never hit a live provider by accident.

```bash
uv run pytest                                                  # full suite, ~600 tests in ~25s
uv run pytest tests/test_sync_integration.py                   # one file
uv run pytest -k verify                                        # by keyword
uv run pytest --cov=case_calendar --cov-branch --cov-report=term-missing
```

Every behavior change ships with the test that proves it. CI runs the full
suite with branch coverage on every push and pull request and **fails the
build under 90% project coverage** — but 90% is a floor, not a target. The
local rule is stronger: no commit should reduce coverage at the module level.
Run the coverage command above before declaring a change done and confirm the
modules you touched held or gained coverage.

Test files mirror the modules they cover (`tests/test_store.py` ↔
`case_calendar/store.py`), so a coverage gap is one file away from its fix.
Cross-module flows live in `tests/test_sync_integration.py` and
`tests/test_serve.py`.

## Lint, format, and type-check

These are exactly what CI runs, so run them before you push:

```bash
uv run ruff check .                                  # lint
uv run ruff format --check .                         # formatting (drop --check to apply)
PYRIGHT_PYTHON_FORCE_VERSION=latest uv run pyright    # static type check
```

Ruff is version-pinned in `pyproject.toml` so local and CI never disagree on
formatting. The pyright env var overrides the wrapper's pinned release so you
type-check against the latest pyright, as CI does.

## Iterating on prompts and models cheaply

The extractor and summary prompts live in
[`case_calendar/llm.py`](https://github.com/seanthegeek/case-calendar/blob/main/case_calendar/llm.py); the per-prompt rules are
reproduced in [LLM prompts](llm-prompts.md). The unit tests pin prompt
*structure*, but they can't tell you whether a wording change actually
improves what the model extracts — for that you have to run the real model
against real dockets. Doing that against your whole caseload on every tweak is
expensive, so the project gives you three levers, cheapest first.

### 1. The dev config

Run any command with `-c config.dev.yaml` to exercise only the ~18 regression
cases instead of a full caseload. A prompt change that's meant to fix one of
those failure modes can be checked against exactly the cases that surfaced it.

### 2. The provider-comparison harness

[`model-comparison/build_provider_stores.py`](https://github.com/seanthegeek/case-calendar/blob/main/model-comparison/build_provider_stores.py)
builds a complete store + rendered output per LLM provider from the *same*
cached CourtListener data, so you can compare cost and output side by side
before changing a default. Point it at the dev config to keep it cheap:

```bash
# Plumbing check with synthetic tokens — no API calls, no spend:
uv run python model-comparison/build_provider_stores.py --config config.dev.yaml --fake

# Real build of one provider column against the dev config:
uv run python model-comparison/build_provider_stores.py --config config.dev.yaml --variants anthropic
```

It copies the store and never mutates the live file, replays the real
pipeline (extractor + verify/dedupe sweeps + summaries), and prints a per-
provider, per-track cost report. See
[`model-comparison/SCORECARD.md`](https://github.com/seanthegeek/case-calendar/blob/main/model-comparison/SCORECARD.md) for the
analysis behind the current default-provider choice.

### 3. The persistent LLM-response cache

The harness keeps a content-addressed cache of every LLM response on disk
(`data/llm-cache.sqlite`), on by default. Because every domain call runs at
`temperature=0`, a response is a pure function of its request, so the cache
keys on the full request (provider, model, prompts, `max_tokens`,
`temperature`) and replays any identical call for free.

The payoff is automatic per-track scoping: after a first build warms the
cache, a **second build following a single-track prompt tweak re-bills only
that track** — a summary-prompt edit replays every extraction and verify call
from cache and pays only for the summaries. The end-of-run log prints
per-column hit/miss counts so you can see it working. Pass `--no-llm-cache`
for a guaranteed-fresh build, or delete the sidecar file to invalidate every
entry.

Reserve a full-caseload build (`-c config.yaml`, no dev config) for the final
check before you commit a prompt or model change.

## Where things live

```text
case_calendar/        the package
  cli.py              subcommands; the shared emit pipeline
  courtlistener.py    REST v4 client (retry/backoff, pagination)
  sync.py             per-case orchestration (extract → verify → dedupe)
  llm.py              domain prompts + extraction / summary entry points
  llmkit/             provider-agnostic LLM call layer + token telemetry
  summary.py          per-docket case-summary pipeline
  store.py            SQLite state
  pdf.py              PDF text extraction with OCR fallback
  calendars/          ICS, Google Calendar, Microsoft 365, index.html renderers
  serve.py            webhook receiver
tests/                mirror the modules they cover; hermetic
docs/                 these pages
model-comparison/     the provider-comparison harness + scoring
scripts/              one-shot maintenance + deployment scripts
```

[Architecture](architecture.md) walks the pipeline end to end.

## Deployment scripts (`scripts/`)

Alongside the one-shot maintenance scripts, `scripts/` holds the
deployment-management wrappers used to run the public deployment — they run
syncs on the production host, move the SQLite store between dev and prod, and
apply upgrades. They're committed (not host-specific) because none of them
hardcode a server address, login user, or install path.

Each one reads its connection settings from `.env` through
`scripts/_prod-env.sh`. Those values are deployment-identifying, so they live
in the gitignored `.env` and are deliberately **not** mirrored into
`.env.example` (which documents only what a fresh checkout needs). Set these in
your own `.env` if you adopt the scripts:

| Variable | What it is |
| --- | --- |
| `CC_PROD_HOST` | prod server hostname or IP for ssh / scp |
| `CC_PROD_SSH_USER` | ssh login user on the prod host (e.g. `root`) |
| `CC_PROD_APP_DIR` | absolute path to the install on prod (e.g. `/opt/case-calendar`) |
| `CC_PROD_SERVICE` | systemd unit name + unix service account (assumed identical) |
| `CC_PROD_STAGE_DIR` | a directory on prod writable by the ssh user, used to stage scp drops |

`scripts/_prod-env.sh` loads and validates these — failing loudly if any is
missing — and is sourced by every deployment script. Its header comment is the
authoritative reference for each variable; each script's header documents the
subset it uses.

The scripts, all run from the repo root:

- **`sync-prod`** — run `case-calendar sync` on prod over ssh, streaming
  output back to your terminal. Forwards extra args (`--case …`,
  `--force-summaries`). Doesn't stop the service; sync and serve coexist
  safely under WAL journaling.
- **`sync-via-prod`** — push your local `config.yaml` to prod, sync there (so
  prod's CourtListener token does the work, not dev's), restart the service to
  pick up the new config, then pull the resulting store back to dev. The
  dev → prod config / prod → dev DB direction is intentional. `--prune` also
  runs `prune --apply` for dockets you removed from config; all other args
  forward to `sync`.
- **`pull-prod-db`** — copy prod's `case-calendar.sqlite` down to dev (DB
  only, no prod-side sync). Uses SQLite's online-backup API so prod's serve
  process keeps handling webhooks during the copy, and backs up the local
  store first.
- **`push-db-to-prod`** — the inverse: upload the local store + `.env` +
  `config.yaml` to prod, pull code, re-emit, and restart. Prompts for
  confirmation first, because it overwrites prod state.
- **`upgrade-prod`** — apt package upgrades, push the local `.env`, pull code,
  `uv sync`, re-emit, restart, and flag if a reboot is required. Leaves the
  prod DB and `config.yaml` untouched.

## Conventions for changes

The project's rules for human and AI contributors alike live in
[`AGENTS.md`](https://github.com/seanthegeek/case-calendar/blob/main/AGENTS.md) — read it before your first pull request. The ones
that catch newcomers most often:

- **Every behavior change ships with its test.** Adding a branch adds a test;
  fixing a bug adds the test that fails on the old code; changing behavior
  updates the tests that asserted the old behavior.
- **Spell "CourtListener" in full** everywhere — code, comments, commits, docs.
  The only allowed abbreviation is the lowercase `cl` parameter name for a
  client object.
- **Plain language over jargon**, and **no unsupportable empirical claims** in
  comments, docstrings, or prompts — cite the rule, statute, or specific
  docket instead.
- **Back up the SQLite store before any schema or migration change.** The
  store holds operational history that isn't cheaply reconstructible. (Tests
  use throwaway tmp-path stores and don't need this.)
- **Commit and push are separate, explicit grants on `main`.** Finish the
  change, run the checks, summarize the diff, then wait to be asked. Feature
  branches you created yourself are exempt from the per-commit gate.

## Next steps

- [Architecture](architecture.md) — how the pipeline fits together.
- [Configuration](configuration.md) — every `config.yaml` option.
- [CLI reference](cli.md) — every subcommand and flag.
- [Cost](cost.md) — what the LLM and CourtListener APIs actually cost.
