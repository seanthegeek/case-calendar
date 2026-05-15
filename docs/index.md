---
title: case-calendar documentation
---

case-calendar turns federal court dockets into calendar feeds. Point it at a
case on [CourtListener](https://www.courtlistener.com/), and it writes the
hearing dates and filing deadlines into an ICS file you can subscribe to from
any calendar app — Apple, Google, Proton, Outlook, Thunderbird — and
optionally pushes the same events directly into Google Calendar or Microsoft
365.

If you're a litigator tracking a single high-stakes case, a journalist
covering several at once, or a researcher watching a docket of public
interest, the goal is the same: never have to ask "wait, when is that hearing
again?" because your calendar already knows.

## How the docs are organized

These pages are short on purpose. Read the one you need, skip the rest.

| Page | What it covers |
| --- | --- |
| [Installation](installation.md) | Python deps, API keys, and the optional but recommended local OCR tools. |
| [Configuration](configuration.md) | The `config.yaml` file — every option, with examples. |
| [CLI reference](cli.md) | Every `case-calendar` subcommand and flag. |
| [Calendar backends](calendars.md) | ICS files, Google Calendar push, Microsoft 365 / Outlook push, attendee invites, and reminders. |
| [Real-time webhooks](webhooks.md) | Skip the polling quota — have CourtListener push updates to you the moment they land. |
| [AI case summaries](case-summaries.md) | The optional 2-4 sentence prose summary rendered on the index page, how it stays current, and how to fill CourtListener gaps. |
| [Public index page](public-page.md) | Generate a static `index.html` that lists every calendar, with subscribe buttons and case details — ready to put behind any HTTP server. |
| [Architecture](architecture.md) | What's going on under the hood: pipeline shape, design choices, and the data model. |

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

*case-calendar is an independent project. It is not affiliated with the
Administrative Office of the U.S. Courts, the Free Law Project, or
CourtListener. Calendar entries are best-effort summaries of public docket
information and are not a substitute for reading the docket itself.*
