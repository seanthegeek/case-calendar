# Agents

## Project Overview

Case Calendar — a CLI / webhook service that pulls federal court hearing dates AND filing deadlines from CourtListener / RECAP and emits them to ICS feeds (subscribable from Proton, Apple, etc.) and optionally pushes them to Google Calendar and/or Microsoft 365 / Outlook. Built for tracking cases where docket-watching by hand is too much: cybercrime prosecutions (e.g. DPRK IT-worker fraud), multi-docket tech litigation (e.g. Anthropic v. DOW), and similar. Designed to run unattended — `case-calendar serve` accepts CourtListener webhooks, processes each delivery, and re-renders the affected ICS file on the spot. The end-to-end "when does a subscriber actually see this?" latency is bounded by signals outside our control on either side — see the [Limitations](docs/index.md#limitations) page; the project's own internal stage (webhook arrival → ICS file written) is the only step that runs in seconds.

## Conventions

**Reading [agent-docs/CONVENTIONS.md](agent-docs/CONVENTIONS.md) in full is MANDATORY before you make any code change** (and before you commit, push, write docs, or run a sync / store build). This is a hard requirement, not advice — and reading this AGENTS.md does NOT substitute for it. If you have not read the conventions page in the current session, read it now, before you touch anything.

That page holds the rules every contributor — agent or human — must follow when changing this repo: the commit / push permission gates, the security-alert + Dependabot-PR pre-release check, the store-backup rule, the plain-language and no-unsupportable-claims rules, the CourtListener-spelling and no-OCR-claim rules, the fix-the-bug-not-the-data rule, the per-case progress-monitor convention, and the read-the-model-card rule. It is canonical project guidance — the same status as this file — split out only to keep AGENTS.md within agentic tools' context-loading limits. The bullet list above is a table of contents, not a summary you may act from; the binding wording lives on the page.

## Architecture

The module-by-module architecture (CLI, courtlistener, llm, llmkit, store, sync, summary, calendars, costs, pdf, serve, alerts, courts, extractor) lives in [agent-docs/ARCHITECTURE.md](agent-docs/ARCHITECTURE.md) — split out to keep this file within agentic tools' context-loading limits. Read it before changing how the modules fit together.

## Key Design Decisions

The rationale behind the project's non-obvious choices — LLM-driven extraction, the three-tier short-circuit, the summary truthfulness guards, the dedupe sweeps, the prompt-cache thresholds, multi-record docket grouping, and the rest — lives in [agent-docs/DESIGN-DECISIONS.md](agent-docs/DESIGN-DECISIONS.md). It is the most important reference when touching the LLM-driven layers: read the relevant note before changing extraction, verification, dedupe, or summary behavior.

## Tech Stack

- Python 3.13, uv for dependency management
- httpx (CourtListener), pypdf (embedded text), poppler + tesseract (optional system deps for OCR fallback)
- tzdata (Python package — keeps `zoneinfo` lookups consistent across hosts where the system tz database is incomplete)
- anthropic / openai / google-genai SDKs (LLM providers, lazy-imported)
- google-api-python-client + google-auth-oauthlib (Google Calendar)
- msgraph-sdk + azure-identity (Microsoft 365 / Outlook calendar — official Microsoft libs only, lazy-imported, async client wrapped in `asyncio.run`)
- python-dotenv, pyyaml

## Python Code Style

- Formatter/linter: **Ruff**
  - All code must be linted and formatted
  - Ruff is version-pinned in `pyproject.toml` under the `lint` extra so CI and contributors share one format opinion. `uv sync --extra lint` installs the pinned version; `uv run ruff check` / `uv run ruff format --check` are what CI runs. When you want to roll forward, bump the pin in `pyproject.toml`, run `uv sync --extra lint`, run `uv run ruff format`, and commit the version bump + reformat in the same PR — never let CI and local drift
  - **ALWAYS run `uv sync --extra lint` before you format or lint, so you're on the project's PINNED ruff — not whatever version `uv run ruff` happens to resolve from your environment.** Ruff's formatter changes its line-wrapping between releases, so an older (or newer) local ruff will report "all formatted" while CI's pinned version reformats the very same file — a green local check that fails CI. Confirm `uv run ruff --version` matches the `lint = ["ruff==X.Y.Z"]` pin in `pyproject.toml` before trusting a local `ruff format`/`ruff check` result or committing formatted code. (This exact drift — local 0.14.8 vs pinned 0.15.13 — slipped an unformatted file past a local check and failed CI in 0.13.0.)
- Type annotations use `TypedDict` for structured results
- Supports all currently supported Python versions
- Modern type annotations across the entire project
  - Always use the latest version of pyright for static type checking. Run with `PYRIGHT_PYTHON_FORCE_VERSION=latest` to override the pyright-python wrapper's pinned release; CI sets this env var on the pyright step
- Testing framework: **pytest**
- Every bit of code should have a test
- Build backend: **hatchling**
- Module-level loggers: `logger = logging.getLogger(__name__)` — one logger per module, named for the module
- Project-defined errors subclass `RuntimeError`, not bare `Exception`, so callers can catch project failures specifically without sweeping in unrelated bugs

## Markdown Style

- All markdown must pass VSCode's default markdownlint config
  - VSCode projects must be configured with `"markdownlint.config": {"MD024": false, "MD004": {"style": "dash"}}` — `MD024` is disabled to allow for proper changelog headings, and `MD004` is pinned to `dash` because the project prefers `- list items` over `* list items` and the default `consistent` rule otherwise picks whichever style appears first in any given file, producing churn when a file's lists get reshuffled. Use `-` followed by a space for every unordered list across the codebase
- **Keep every inline code span on a single physical line, and never leave a `<placeholder>`-style angle-bracket token in body text outside backticks.** When a `` ` ``-delimited span opens on one line and its closing backtick lands on the next, not every Markdown renderer reassembles it — and when the span fails to form, the angle-bracket placeholders that were meant to be literal (`<created>`, `<failed>`, and the like) get parsed as raw HTML tags instead. An unclosed tag such as `<failed>` then mis-nests the DOM that follows it, so every paragraph after that point renders clipped and unstyled — the formatting silently disappears, and markdownlint does NOT catch it (the source is structurally valid). Wrap the surrounding prose rather than the code span, and reserve literal `<...>` for genuinely intended HTML (e.g. a real `<br>`). This bit [docs/webhooks.md](docs/webhooks.md): a line-wrapped `docket alerts: <created> created, <exists> already subscribed, <failed> failed` span broke the rendering of the entire rest of the page. The detector is a quick sweep for lines whose backtick count is odd (a code span that crosses a line boundary) plus a grep for `<word>` tokens sitting outside backticks.
- **Keep every Markdown link's `[text](target)` on a single physical line — never let a line-wrap fall *inside* the `[...]` text.** The docs are published by Jekyll on GitHub Pages, whose default `jekyll-relative-links` plugin is what rewrites in-repo `.md` links to `.html` on the live site. Its link-matching regex works within one line, so a link whose text straddles a newline — `[AI case` on one line, `summaries](case-summaries.md)` on the next — is never matched, and the plugin leaves the raw `.md` target in place, producing a dead link on the published page (the local file renders fine, and markdownlint does NOT flag it because the source is valid Markdown). When prose needs to wrap, put the break BEFORE the `[` or AFTER the `)`, not between the brackets. This bit four pages at once ([docs/local-llms.md](docs/local-llms.md) → case-summaries, [docs/index.md](docs/index.md) → calendars, [docs/subscribing.md](docs/subscribing.md) → public-page and calendars), each a wrapped sentence that split the link text across two lines. The detector is `grep -rPzo '\[[^\]]*\n[^\]]*\]\(' docs/` — any hit is a link whose text crosses a line boundary.
- **Escape a literal `~` (used for "approximately") as `\~` in prose — GitHub-Flavored Markdown treats `~` as a strikethrough delimiter.** GFM renders both `~~text~~` AND single `~text~` as strikethrough, so a line with two or more bare tildes (`~4× ... ~$60`) gets the text between a tilde pair silently struck through on GitHub.com — the rest of the line reads fine, only the spanned run is crossed out, and (like the inline-code case above) markdownlint does NOT flag it because the source is valid. Write `\~4×`, `\~$60`, `\~2000 tokens` etc.; the backslash renders as a literal `~` and can never pair. This bit [model-comparison/SCORECARD.md](model-comparison/SCORECARD.md), where a line of `~`-prefixed approximations struck through "4× what Gemini costs ... Gemini would save". Tildes INSIDE backtick code spans are safe and must stay unescaped. The detector is a grep for `~` outside code spans; any line with two or more is a live strikethrough.
- **No all-caps shouting in human-facing documentation (`docs/`, `model-comparison/`, the top-level README).** Reserve emphasis for `**bold**` or `*italic*`, never a capitalized word — write "the most detailed local extractor", not "the MOST detailed local extractor". Documentation is read by people, and all-caps emphasis reads as shouting. Two things legitimately stay capitalized and are not shouting: (1) genuine acronyms / identifiers (`JSON`, `GPU`, `VRAM`, `CourtListener`, a Modelfile `FROM` / `PARAMETER` directive); and (2) literal tokens — a toggle *value* (`ON` / `OFF`, `think-ON`) or a label the code actually prints (the `RUNAWAY` / `HUNG` / `WARNING` / `TOTAL` / per-entry `DECISION` log markers). AGENTS.md itself is the deliberate exception: it is written *to* an agent, so its emphatic `NOT` / `MUST` / `ONLY` stay — this rule governs the prose a subscriber or contributor reads, not these instructions. The detector is a grep for `\b[A-Z]{2,}\b` outside backticks with the acronym + label allow-list subtracted (the sweep used to clean [model-comparison/SCORECARD.md](model-comparison/SCORECARD.md)).

## GitHub releases

- Releases are made by version tag not branch
- Version tags should be prefixed with `v`, unless prior tags are not
- Release titles must always exclude the `v` prefix
- For Python projects, wheels and srcbuilds should always be attached
  - Use existing build files **if** they match the release version

## Documentation

The project must be well documented. If existing documentation exists, follow that convention.

For new projects, do **NOT** use a monolithic readme. Instead, use the readme to provide an overview of the project, and leave specific details in friendly, bite-sized markdown-formatted pages in a `docs` directory.

## Development

```bash
uv sync
cp .env.example .env                  # COURTLISTENER_TOKEN + one *_API_KEY
cp config.example.yaml config.yaml    # list your cases / dockets / calendars
uv run case-calendar setup gcal       # optional: one-time Google Calendar OAuth
uv run case-calendar setup m365       # optional: one-time Microsoft 365 / Outlook OAuth
uv run case-calendar sync             # backfill (polling) + auto-emit to all backends
uv run case-calendar serve            # real-time webhooks + auto-emit
uv run case-calendar emit             # force re-render (no CourtListener pull)
uv run case-calendar summarize        # optional: generate AI case summaries for the index page (opt-in)
uv run case-calendar show             # dump current state

# Or, real-time push from CourtListener (no daily-quota burn):
# 1. Set CASE_CALENDAR_WEBHOOK_SECRET in .env to a long random string
# 2. uv run case-calendar serve --port 8000
# 3. Expose 127.0.0.1:8000 over public HTTPS (Caddy / Cloudflare Tunnel /
#    fly.io / etc.) and register the resulting URL in the CourtListener dashboard:
#    https://<your-host>/webhooks/case-calendar/<CASE_CALENDAR_WEBHOOK_SECRET>
#    Event type: DOCKET_ALERT.
# 4. Subscribe to docket alerts in CourtListener for each docket in your config.yaml.
```

Optional system dependencies for OCR fallback (only needed when CourtListener's `plain_text` is empty or garbled, so the project OCRs the PDF itself):

```bash
sudo apt install poppler-utils tesseract-ocr                                 # Debian / Ubuntu
sudo dnf install epel-release && sudo dnf install poppler-utils tesseract     # RHEL / CentOS / Rocky (tesseract is in EPEL)
brew install poppler tesseract                                                # macOS
```

Without these, the tool still works — it skips PDFs CourtListener hasn't processed and retries on each subsequent sync.

## Scripts (`scripts/`)

The one-shot maintenance tools and the production deployment wrappers are documented in [scripts/AGENTS.md](scripts/AGENTS.md) — the duplicate-key hazards, the deterministic-heal signals, and the `.env`-driven prod settings. Operator-facing runnable how-to is in [scripts/README.md](scripts/README.md).

## Model comparison (`model-comparison/`)

The benchmark tooling — the provider-store builder, the persistent LLM cache, the frozen-snapshot scorer, and the "run `uv run --no-sync` while a sweep is going" rule — is documented in [model-comparison/AGENTS.md](model-comparison/AGENTS.md). The reproduction method and workflow are in [model-comparison/README.md](model-comparison/README.md); the numbers behind the default-provider choice are in [model-comparison/SCORECARD.md](model-comparison/SCORECARD.md).

## Testing

How to run the suite, the testing philosophy, and the per-file integration coverage map are in [tests/AGENTS.md](tests/AGENTS.md). Read it before adding or changing tests — every behavior change ships with the test that proves it, and CI fails the build under 90% project coverage.
