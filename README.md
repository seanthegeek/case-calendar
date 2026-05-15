# case-calendar

Subscribable calendar feeds for federal court hearings and filing
deadlines — sourced from CourtListener / RECAP, optionally annotated
with AI case summaries, optionally pushed to Google Calendar or
Microsoft 365.

📚 **Documentation:** [https://seanthegeek.github.io/case-calendar/](https://seanthegeek.github.io/case-calendar/)
(or browse the [`docs/`](docs/index.md) folder in this repo).<br>
🌐 **Live example:** [https://casecalendar.net/](https://casecalendar.net/)
— a deployed instance with real cases, real ICS feeds, and the AI case
summaries enabled.

## Why this exists

Watching a federal docket by hand is a part-time job. PACER charges by
the page, CourtListener's free tier throttles, and the standard
workflow — refresh the docket, scan for entries that look like
scheduling orders or judgments, transcribe dates into a calendar — is
fragile and slow. One missed minute entry and you don't know your
sentencing got moved.

case-calendar automates that loop. Point it at a case and it pulls new
docket entries, identifies the ones that schedule hearings or set
filing deadlines, extracts the dates, and writes them into an ICS file
your calendar app subscribes to. Subscribers see updates within seconds
of an entry hitting the docket. The output is durable enough to track
30+ cases on a single calendar without losing fidelity on any one.

It was built for the cases where docket-watching by hand is too much:

- **Cybercrime prosecutions** with multiple defendants, separate
  dockets, parallel proceedings, and dispositions trickling in over
  years (DPRK IT-worker fraud, intrusion cases, sanctions matters).
- **Multi-docket tech litigation** with parallel filings in different
  venues under different statutes (Anthropic v. DOW, antitrust families,
  appellate companion cases).
- **National-security cases** where the public docket is sparse and
  every paperless minute entry matters.

## Features

- **Per-case calendar feeds** in standard ICS — subscribe from Proton,
  Apple, Google, Outlook, Thunderbird, or anything else that speaks
  iCalendar.
- **Push to Google Calendar and Microsoft 365 / Outlook**, opt-in, with
  one-time OAuth. Events are deduplicated server-side so reschedules
  update existing events rather than creating new ones.
- **Filing-deadline tracking** auto-detected per docket — on for civil
  and appellate, off for routine criminal, force-on per-case for
  motion-heavy litigation.
- **AI case summaries** (opt-in) generate a 2-4 sentence prose
  description of each case from its primary document plus any
  dispositions. Refuses to fabricate when source documents are
  insufficient. Auto-refreshes whenever a new judgment, plea agreement,
  or dispositive memo lands.
- **Real-time webhook receiver** — register a public HTTPS URL with
  CourtListener and bypass the daily polling quota entirely.
- **Static landing page** with one-click subscribe buttons, client-side
  sort, and dark mode, generated alongside the ICS files.
- **Court-local timezones** preserved on every event so a 3 PM Pacific
  hearing stays "3 PM Pacific" through DST and travel.
- **Multi-docket cases** collapse into one logical case — district +
  appellate, parallel filings, cooperating co-defendants in the same
  conspiracy.

## How it works

```text
CourtListener docket entries
        │
        ▼
  regex pre-filter          (drops 80%+ of entries cheaply)
        │
        ▼
  LLM extractor              (small/fast tier — Haiku, Flash Lite, etc.)
        │
        ▼
  SQLite store               (stable hearing / deadline keys)
        │
        ▼
  end-of-sync verify pass    (catches missed reschedules)
        ▼
  ICS / Google / M365 push   + optional static index.html
```

Two delivery modes feed the pipeline: `case-calendar sync` for polling
(designed to run on a cron) and `case-calendar serve` for real-time
webhooks. Both share the same per-entry processor, so an event
extracted via webhook is byte-identical to one from polling.

A separate, opt-in second LLM track handles AI case summaries on a
higher-tier model (Sonnet / GPT-5.4 / Gemini Pro). The two tracks have
independent provider / model knobs.

For the design decisions behind each piece — confidence passes,
cross-court sibling isolation, the no-fabrication rule, etc. — see
[`docs/architecture.md`](docs/architecture.md) for the concise overview
or [`AGENTS.md`](AGENTS.md) for the exhaustive reference.

## Quick start

```bash
git clone https://github.com/seanthegeek/case-calendar
cd case-calendar
uv sync
cp .env.example .env                  # add COURTLISTENER_TOKEN + one *_API_KEY
cp config.example.yaml config.yaml    # list your cases
uv run case-calendar sync
```

Subscribe your calendar app to `out/<your-calendar>.ics` and you're
done.

The [installation guide](docs/installation.md) covers the optional but
recommended local OCR setup; [configuration](docs/configuration.md)
walks through `config.yaml`; [CLI reference](docs/cli.md) lists every
subcommand. The [documentation index](docs/index.md) points to all the
rest.

## Documentation

The full documentation lives in [`docs/`](docs/index.md) and is hosted
on GitHub Pages:

- [Installation](docs/installation.md) — Python deps, API keys, OCR.
- [Configuration](docs/configuration.md) — every `config.yaml` option.
- [CLI reference](docs/cli.md) — every subcommand and flag.
- [Calendar backends](docs/calendars.md) — ICS, Google, Microsoft 365.
- [Real-time webhooks](docs/webhooks.md) — push instead of polling.
- [AI case summaries](docs/case-summaries.md) — opt-in summaries on the
  index page.
- [Public index page](docs/public-page.md) — generate `index.html` for
  public hosting.
- [Architecture](docs/architecture.md) — how the pipeline fits together.

## Status

case-calendar is open-source software under the
[LICENSE](LICENSE) in this repository. It's an independent project —
not affiliated with the Administrative Office of the U.S. Courts, the
Free Law Project, or CourtListener.

Calendar entries are best-effort summaries of public docket
information. They are not legal advice, and they are not a substitute
for reading the docket itself. Bugs in the extractor or the
verify-pass LLM can produce wrong dates; the AI summary feature is
explicitly labeled as such on the rendered page and may contain
mistakes.

## Contributing

Bug reports and pull requests welcome at
[github.com/seanthegeek/case-calendar/issues](https://github.com/seanthegeek/case-calendar/issues).
Every behavior change ships with the test that proves it
(see [AGENTS.md](AGENTS.md#testing-philosophy)); the suite is
hermetic — no real HTTP, no real LLM, no real OAuth — and runs in
about 25 seconds:

```bash
uv sync --extra test
uv run pytest
```
