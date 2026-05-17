# case-calendar

[![CI](https://img.shields.io/github/actions/workflow/status/seanthegeek/case-calendar/ci.yml?branch=main&label=CI)](https://github.com/seanthegeek/case-calendar/actions/workflows/ci.yml)
[![codecov](https://img.shields.io/codecov/c/github/seanthegeek/case-calendar/main)](https://codecov.io/gh/seanthegeek/case-calendar)
[![License](https://img.shields.io/github/license/seanthegeek/case-calendar)](LICENSE)

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
fragile and slow. One missed minute entry and you don't know a
sentencing got moved.

case-calendar automates that loop. Point it at a case and it pulls new
docket entries, identifies the ones that schedule hearings or set
filing deadlines, extracts the dates, and writes them into an ICS file
your calendar app subscribes to. The ICS file updates within seconds
of a new entry reaching CourtListener via the real-time webhook path;
how soon CourtListener itself sees an entry, and how soon your
calendar app then re-fetches the ICS file, are both outside this
project's control (see [Limitations](#limitations) below).

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
  regex pre-filter          (drops most non-hearing entries cheaply)
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

## Limitations

case-calendar is a supplement to docket-watching, not a replacement
for it. Three constraints are inherent to the design and worth
knowing before you rely on it:

- **PACER → RECAP → CourtListener latency.** Entries reach this
  pipeline only after they appear in PACER *and* someone running
  the [RECAP browser extension](https://free.law/recap/) pulls the
  affected docket page, which is what feeds the entry into
  CourtListener. On a high-traffic docket with reporters and
  researchers refreshing it, that lag is usually minutes. On a
  docket no one else is watching, it can be **months**, if not
  longer — the filing exists in PACER, but until someone with
  RECAP visits, it is invisible to CourtListener and therefore
  invisible here. The
  real-time [webhook receiver](docs/webhooks.md) narrows the
  CourtListener-to-you portion of the chain to seconds, but it
  cannot show you something CourtListener hasn't seen yet. You
  can close this gap on dockets you care about by pulling the
  docket sheet in PACER yourself with the RECAP extension
  installed — PACER charges a few cents per page, and the
  extension uploads what you fetched into CourtListener as a
  side effect, so the next sync sees it.
- **Calendar-client refresh delays.** Subscribed ICS feeds
  refresh on the calendar app's own schedule, not yours. Apple
  Calendar defaults to roughly hourly and is user-configurable
  (5 minutes to weekly). Google Calendar runs every 8–24 hours,
  or longer, on an undocumented schedule, with no user setting
  and no manual refresh button. Proton Calendar runs every
  4–16 hours per its own documentation. Two ways to eliminate
  this lag: subscribe in a calendar app that lets you set the
  refresh interval (Apple Calendar, Thunderbird, Fastmail), or
  configure case-calendar to push directly to [Google Calendar
  or Microsoft 365](docs/calendars.md) — direct push lands the
  event in your calendar in the same emit cycle that writes the
  ICS file.
- **Extraction errors.** The cheap regex pre-filter and the
  small/fast LLM the extractor uses can miss an atypical clerk
  notation, misread a date from a garbled PDF, or fail to
  recognize a reschedule the first time it sees one. The
  end-of-sync verify pass catches many of these, not all. Audit
  against the source docket before relying on a date for anything
  consequential.

With the first two latencies mitigated — the webhook receiver
plus an occasional self-pulled docket sheet on the upstream side,
and a controllable-refresh client or direct push on the downstream
side — extraction errors are the remaining risk for inaccurate or
outdated calendar information.

Even so, case-calendar is not a substitute for the calendaring
software a practicing attorney uses to manage filing deadlines on
their own cases. Treat it as a convenience layer on top of the
public docket, not the authoritative record.

If you find an issue with event extraction, case summarization,
or some other bug, please
[report it](https://github.com/seanthegeek/case-calendar/issues/new/choose).

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

Calendar entries are not legal advice and are not a substitute for
reading the docket itself. See [Limitations](#limitations) above for
the specific failure modes worth knowing about.

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
