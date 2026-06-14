---
title: Case Calendar documentation
---

[![CI](https://img.shields.io/github/actions/workflow/status/seanthegeek/case-calendar/ci.yml?branch=main&label=CI)](https://github.com/seanthegeek/case-calendar/actions/workflows/ci.yml)
[![codecov](https://img.shields.io/codecov/c/github/seanthegeek/case-calendar/main)](https://codecov.io/gh/seanthegeek/case-calendar)
[![License](https://img.shields.io/github/license/seanthegeek/case-calendar)](https://github.com/seanthegeek/case-calendar/blob/main/LICENSE)

Case Calendar turns federal court dockets into calendar feeds. Point it at a
case on [CourtListener](https://www.courtlistener.com/), and it writes the
hearing dates and filing deadlines into an ICS file you can subscribe to from
any calendar app — Apple, Google, Proton, Outlook, Thunderbird — and
optionally pushes the same events directly into Google Calendar or Microsoft
365.

If you're a journalist covering several cases at once, a researcher
watching a docket of public interest, or anyone tracking more cases
than you can comfortably refresh by hand, the goal is the same:
not having to ask "Wait, when is that hearing again?" because your
calendar already knows.

## Features

- **Per-case calendar feeds** in standard ICS — subscribe from Proton,
  Apple, Google, Outlook, Thunderbird, or anything else that speaks
  iCalendar.
- **Push to Google Calendar and Microsoft 365 / Outlook**, opt-in, with
  one-time OAuth. Events are deduplicated server-side so reschedules
  update existing events rather than creating new ones.
- **Filing-deadline tracking** on every docket, uniformly — the LLM tags
  each deadline major or minor, and only major ones (dispositive briefing,
  sentencing memos, PSR objections, surrender dates, and the like) reach
  subscriber calendars; procedural filings stay in the audit trail.
- **AI case summaries** (opt-in) generate a 2-4 sentence prose
  description of each case from its primary document plus any
  dispositions, with each summary's action phrases linked to the
  source court documents ("**were charged**" links to the indictment,
  "**pled guilty**" to the plea agreement). Refuses to fabricate when
  source documents are insufficient. Auto-refreshes whenever a new
  charging document, judgment, plea agreement, or dispositive ruling
  lands — or a tracked hearing or deadline changes status.
- **Real-time webhook receiver** — register a public HTTPS URL with
  CourtListener and bypass the daily polling quota entirely.
- **Static landing page** with one-click subscribe buttons, client-side
  sort, and dark mode, generated alongside the ICS files. Each case row
  carries an **upcoming-events preview** — a compact agenda of its next
  hearings and deadlines in court-local time, with an expandable "+N more"
  — so visitors can see what a calendar holds without subscribing; the
  preview shows exactly the events in the matching ICS feed.
- **Court-local timezones** preserved on every event so a 3 PM Pacific
  hearing stays "3 PM Pacific" through DST and travel.
- **Multi-docket cases** collapse into one logical case — district +
  appellate, parallel filings, cooperating co-defendants in the same
  conspiracy.

## Limitations

Case Calendar is a supplement to docket-watching, not a replacement
for it. Three constraints are inherent to the design and worth
knowing before you rely on it:

- **PACER → RECAP → CourtListener latency.** Entries reach this
  pipeline only after they appear in [PACER](https://pacer.uscourts.gov/)
  *and* someone running the [RECAP browser extension](https://free.law/recap/)
  pulls the affected docket page, which is what feeds the entry into
  CourtListener. On a high-traffic docket with reporters and
  researchers refreshing it, that lag is usually minutes. On a
  docket no one else is watching, it can be **months**, if not longer — the
  filing exists in PACER, but until someone with RECAP visits, it
  is invisible to CourtListener and therefore invisible here.
  The real-time [webhook receiver](webhooks.md) narrows the
  CourtListener-to-you portion of the chain to seconds, but it
  cannot show you something CourtListener hasn't seen yet. You
  can close this gap on dockets you care about by pulling the
  docket sheet in PACER yourself with the RECAP extension
  installed — PACER charges a few cents per page, and the
  extension uploads what you fetched into CourtListener as a
  side effect, so the next sync sees it.
- **Calendar-client refresh delays.** [Subscribed ICS feeds](subscribing.md)
  refresh on the calendar app's own schedule, not yours. Apple
  Calendar defaults to roughly hourly and is user-configurable (5
  minutes to weekly). Google Calendar runs every 8–24 hours, or
  longer, on an undocumented schedule, with no user setting and
  no manual refresh button. Proton Calendar runs every 4–16
  hours per its own documentation. Two ways to eliminate this
  lag: subscribe in a calendar app that lets you set the refresh
  interval (Apple Calendar, Thunderbird, Fastmail), or configure
  Case Calendar to push directly to
  [Google Calendar or Microsoft 365](calendars.md) —
  direct push lands the event in your calendar in
  the same emit cycle that writes the ICS file.
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

Even so, Case Calendar is not a substitute for the calendaring
software a practicing attorney uses to manage filing deadlines on
their own cases. Treat it as a convenience layer on top of the
public docket, not the authoritative record.

If you find an issue with event extraction, case summarization,
or some other bug, please
[report it](https://github.com/seanthegeek/case-calendar/issues/new/choose).

## How the docs are organized

These pages are short on purpose. Read the one you need, skip the rest.

| Page | What it covers |
| --- | --- |
| [Installation](installation.md) | Python deps, API keys, and the optional but recommended local OCR tools. |
| [Configuration](configuration.md) | The `config.yaml` file — every option, with examples. |
| [CLI reference](cli.md) | Every `case-calendar` subcommand and flag. |
| [Calendar backends](calendars.md) | ICS files, Google Calendar push, Microsoft 365 / Outlook push, attendee invites, and reminders. |
| [Cost](cost.md) | What the LLM and CourtListener APIs cost — measured per-provider numbers, rate limits, and how to measure your own spend. |
| [Local models (Ollama)](local-llms.md) | Run extraction and/or summaries on local open models — no API key, no per-token cost, and a way to benchmark local models against the hosted ones. |
| [Subscribing to a feed](subscribing.md) | Step-by-step subscribe-by-URL instructions for Apple, Google, Outlook, Proton, Fastmail, and Thunderbird. |
| [Real-time webhooks](webhooks.md) | Skip the polling quota — have CourtListener push updates to you the moment they land. |
| [AI case summaries](case-summaries.md) | The optional 2-4 sentence prose summary rendered on the index page, how it stays current, and how to fill CourtListener gaps. |
| [Public index page](public-page.md) | Generate a static `index.html` that lists every calendar, with subscribe buttons and case details — ready to put behind any HTTP server. |
| [Architecture](architecture.md) | What's going on under the hood: pipeline shape, design choices, and the data model. |
| [LLM prompts](llm-prompts.md) | The exact system prompt behind every extraction, verification, and summary call, reproduced verbatim. |
| [Development](development.md) | Set up a dev environment, run the tests, and iterate on the prompts and models cheaply. |

## See it live

A deployment of the [public index page](public-page.md) — running this
code against real federal-court dockets, with AI case summaries enabled
— is at [casecalendar.net](https://casecalendar.net/). Browse it to get
a sense of what the rendered output looks like before installing
anything.

## Quick start

If you'd rather just see it working locally:

```bash
git clone https://github.com/seanthegeek/case-calendar
cd case-calendar
uv sync
cp .env.example .env       # add COURTLISTENER_TOKEN + one *_API_KEY
cp config.example.yaml config.yaml   # list your cases
uv run case-calendar sync
```

After the first sync, `out/<your-calendar>.ics` is ready to subscribe to. The
[installation](installation.md) and [configuration](configuration.md)
pages explain each step.

## Project links

- [GitHub repository](https://github.com/seanthegeek/case-calendar) — source,
  issues, releases.
- [README](https://github.com/seanthegeek/case-calendar#readme) — motivation,
  features, architecture overview.
- [LICENSE](https://github.com/seanthegeek/case-calendar/blob/main/LICENSE) —
  open-source license.

---

*Case Calendar is an independent project. It is not affiliated with the
Administrative Office of the U.S. Courts, the Free Law Project, or
CourtListener. Calendar entries are best-effort summaries of public docket
information and are not a substitute for reading the docket itself.*
