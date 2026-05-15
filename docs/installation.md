---
title: Installation
---

case-calendar is a Python CLI. You'll need:

- **Python 3.13** or newer.
- [**uv**](https://docs.astral.sh/uv/getting-started/installation/) for
  dependency management. `pip install uv` works in a pinch, but `uv` itself
  has its own installer that's faster on most systems.
- A **CourtListener API token** — sign up for a free account at
  [courtlistener.com](https://www.courtlistener.com/) and copy the token from
  the user-profile page.
- An **LLM API key** for one of: Anthropic, OpenAI, or Google (Gemini).
  Pick whichever you already use. The extractor pipeline uses the cheap /
  small-model tier of each provider; expect cents per case per day on a
  busy docket.

[← Back to docs](index.md)

## Install case-calendar

```bash
git clone https://github.com/seanthegeek/case-calendar
cd case-calendar
uv sync
```

That's the whole installation. `uv sync` reads `pyproject.toml` and installs
everything into a project-local virtual environment. Prefix every command
with `uv run` (e.g. `uv run case-calendar sync`) and uv handles the activation
for you.

## Configure secrets

case-calendar reads secrets from a `.env` file in the project root:

```bash
cp .env.example .env
```

Open `.env` and fill in:

```bash
COURTLISTENER_TOKEN=your_token_here
ANTHROPIC_API_KEY=sk-ant-...      # or OPENAI_API_KEY=sk-... / GEMINI_API_KEY=...
CASE_CALENDAR_WEBHOOK_SECRET=...  # only needed for `case-calendar serve`
```

You only need one LLM key. The tool auto-detects which provider to use from
whichever `*_API_KEY` is set. To force a specific provider, set
`LLM_PROVIDER=anthropic` (or `openai` / `gemini`).

> ⚠️ The `.env` file should never be committed to source control — the
> repository's `.gitignore` already lists it.

## Highly recommended: local OCR tools

CourtListener stores RECAP PDFs unedited — it does **not** re-OCR documents
contributed to RECAP. The `plain_text` CourtListener returns is whatever the
uploader's PDF carried natively, which on a non-trivial fraction of court
PDFs is either:

- **Empty** — an image-only scan from a photocopier, no embedded text.
- **Garbled** — custom font subsets with no `/ToUnicode` map produce
  multi-KB strings of `ÿ` / glyph-index tokens that the summary LLM can't
  read.

Installing the local OCR fallback lets case-calendar re-process those PDFs
itself, so the AI summary pipeline sees usable text instead of an explicit
"insufficient documents" refusal:

```bash
# Debian / Ubuntu
sudo apt install poppler-utils tesseract-ocr

# macOS
brew install poppler tesseract
```

Without these, the tool still works — it just skips un-OCR'd or garbled PDFs
and retries on each sync (no cache poisoning). But on those dockets the AI
summary will fall back to:

> *Documents available for this docket are insufficient to generate a
> reliable summary.*

Install them. The extra disk footprint is small and the OCR runs only when
the primary text extraction has already failed.

## Optional: calendar push backends

By default case-calendar writes an ICS file you can subscribe to. If you'd
rather have events show up directly in Google Calendar or Microsoft 365 /
Outlook, see the [calendar backends](calendars.md) page for the one-time
OAuth setup. Both are opt-in and require no per-command flag once
authorized.

## Verify it works

Add at least one case to `config.yaml` (see [configuration](configuration.md))
and run:

```bash
uv run case-calendar sync
```

A successful first sync prints one line per case, e.g.:

```text
[us-v-wang] entries_seen=42 entries_processed=11 actions=8 verified=11
[cybercrime] wrote 14 events -> out/cybercrime.ics
```

The ICS file is now ready to subscribe to from any calendar app. See
[calendars](calendars.md) for what to do with it.

## Next steps

- [Configuration](configuration.md) — what to put in `config.yaml`.
- [CLI reference](cli.md) — every subcommand and flag.
- [Real-time webhooks](webhooks.md) — replace polling with push.
