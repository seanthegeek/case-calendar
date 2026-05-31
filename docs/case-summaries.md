---
title: AI case summaries
---

A subscribable calendar of dates is useful. A subscribable calendar that also
tells you *what each case is about* in a couple of sentences is more useful —
especially when you're tracking 30 cases and can't remember which Wang is
which.

When enabled, Case Calendar generates a 2-4 sentence prose summary for each
docket and renders it on the [public index page](public-page.md) next to
the case row. Summaries are opt-in, off by default, and only run on dockets
where the source documents actually support a confident answer (the LLM is
instructed to refuse rather than fabricate when they don't).

[← Back to docs](index.md)

## What gets summarized

The summary pipeline pulls three sets of source documents for each docket:

1. **Primary document** — the latest indictment / superseding indictment /
   information for criminal dockets; the latest amended complaint / complaint
   / petition for civil. Establishes who's involved and what the case is
   about.
2. **Disposition documents** — judgments, plea agreements, verdict forms,
   orders of dismissal, dispositive memoranda. Anything that materially
   changes "where does the case stand". A dispositive order in a busy civil
   case can be a hundred pages back from the latest entry, so the pipeline
   walks several pages of the docket newest-first to find it.
3. **Operator-provided documents** (optional, see
   [`extra_documents`](#extra_documents) below) — anything you've manually
   pointed the pipeline at to fill a CourtListener data gap.

Those documents — plus a structured scaffold of the hearings and deadlines
the extractor already recorded — go into a single LLM call. The model returns
prose; Case Calendar persists it to the `case_summaries` table.

The page-rendered output looks like:

> Mr. Jones is charged in the Northern District of Texas with one count of
> wire fraud conspiracy and five counts of wire fraud for his alleged role
> in a $2.6 million online romance-scam scheme. He pled guilty to the
> conspiracy count on January 14, 2025 pursuant to a plea agreement;
> sentencing is scheduled for May 28, 2026. Co-defendant Smith remains a
> fugitive abroad.

A live deployment with real summaries on real federal-court dockets is
at [casecalendar.net](https://casecalendar.net/).

## Inline document links

Summaries hyperlink the words themselves, the way a news article does. In the
example above, **is charged** would link to the indictment, **pled guilty**
to the plea agreement, and (on a concluded case) **was sentenced** to the
judgment. Only the short action phrase is linked — the leading verb is kept
inside the link ("was charged", not just "charged"), and the trailing detail
(the connecting preposition and everything after it: what they were charged
with, the sentence terms, the dollar amounts, the dates) stays as plain text,
so a charge or sentence never turns into one long run-on link. The link lands on the supporting document's PDF —
CourtListener's own copy (`storage.courtlistener.com`) when available, with
the Internet Archive mirror as a fallback (the same URL the calendar event
bodies link to). There are no footnote numbers or "(see Doc 1)" markers — just
the phrase a reader would naturally tap.

The model decides which phrase each document supports (it is the one that read
them), so any document the pipeline feeds it can be linked — primary documents,
dispositions, and operator-provided [`extra_documents`](#extra_documents)
alike, not a fixed list of phrases. Under the hood, each document is shown to
the model with a short reference token that never reaches the page; the model
links a phrase to a token, and Case Calendar resolves the token to a real URL
before storing the summary. A phrase the model can't tie to a document it was
actually given — or one whose document has no openable URL (a paperless minute
order, a not-yet-uploaded or sealed PDF) — is left as plain, unlinked text. A
summary can never link to a document that wasn't in the set the model
summarized from.

There is nothing to configure; links appear automatically once summaries are
enabled.

## Enabling summaries

Add a top-level block to `config.yaml`:

```yaml
case_summaries:
  enabled: true
  # provider: anthropic
  # model: claude-sonnet-4-6
  # allow_ocr: true
  # debounce_seconds: 300
```

| Key | Required | Purpose |
| --- | --- | --- |
| `enabled` | yes | Master switch. Defaults to `false`. |
| `provider` | no | Force a specific provider (`anthropic` / `openai` / `gemini`) for the summary track. When unset, falls back to `LLM_SUMMARY_PROVIDER`, then `LLM_PROVIDER`, then auto-detects from whichever API keys are set in summary key-priority order (anthropic > gemini > openai). With keys for all three present, the summary track defaults to Anthropic (Sonnet 4.6). |
| `model` | no | Override the model. Defaults to Sonnet / GPT-5.4 / Gemini Pro depending on provider. |
| `allow_ocr` | no | Run local OCR fallback on PDFs CourtListener hasn't extracted. Defaults to `true`. Set to `false` to skip tesseract entirely. |
| `debounce_seconds` | no | Webhook-only. How many seconds of quiet to wait after the last summary-relevant entry before re-running the LLM. Defaults to 300. Polling syncs ignore this — they regenerate immediately. |

When `enabled: true`, summaries auto-refresh as part of `sync` and `serve`:
whenever the syncer sees a new primary document or disposition — or whenever
a hearing or deadline changes posture (gets marked held / cancelled, or
rescheduled), even when no new document accompanies it — it flips the row's
`stale` flag. At the end of the sync (or after the debounce timer fires in
`serve`), the pipeline regenerates every stale row before re-emitting the
index. The page reflects the case's current posture without you running
anything manually.

## Cost

Summaries add a higher-tier model on top of the always-on extraction cost.
Measured per-provider backfill numbers, the CourtListener API rate limits, the
price-table caveats, and how to read the `llm-tokens` log lines all live on the
dedicated [Cost](cost.md) page — it covers the whole pipeline, since extraction
and CourtListener quota cost something even with summaries off.

## The "insufficient documents" refusal

The summary LLM is instructed to refuse rather than fabricate when its
inputs are too sparse to support a confident summary. If the primary
document text is empty (image-only PDF that didn't OCR), garbled (custom
font subsets — see the [installation](installation.md#highly-recommended-local-ocr-tools)
page), or otherwise lacks the substance needed to identify the parties and
the gist of the charges or claims, the model emits this exact sentence
verbatim:

> Documents available for this docket are insufficient to generate a
> reliable summary.

That gets stored and rendered like any other summary. Subscribers see the
honest acknowledgement instead of a plausible-sounding hallucination, and
operators can grep for the sentence in the database to find dockets that
need attention (typically: install poppler/tesseract for local OCR, or
point `extra_documents` at an out-of-band source).

This refusal is one of several truthfulness guardrails the summary prompt
enforces, all backed by a deterministic post-generation guard (prompt rules
alone are soft). What the model is told, and how the guard's layers fit
together, are covered in
[Data quality guardrails](architecture.md#summaries-state-only-what-the-documents-support)
in the architecture overview.

## Multi-docket aggregation

For cases that span multiple logical PACER dockets (district + appellate;
co-defendants on separate dockets; parallel filings in different venues),
the AI summary is generated **per logical docket** — one summary per
distinct `(docket_number, court_id)` pair — then rendered as a labeled
paragraph block on the index page:

> **3:24-cv-00100 (N.D. Cal.):** The district court suit alleges …
>
> **24-12345 (9th Cir.):** The Ninth Circuit appeal challenges …

To frame the litigation strategy for the model, add an `aggregation_note`
on the case:

```yaml
- id: anthropic-v-dow
  name: "Anthropic v. DOW"
  calendar: tech
  dockets: [72380208, 72379655, 73136734]
  aggregation_note: >-
    Parallel suits challenging separate Department of War actions taken
    under distinct statutory authorities, each filed in the proper venue
    for the action it targets.
```

The note is *only* shown to the summarizer. It's not rendered to
subscribers. Keep it short and factual — the model uses it as framing,
not as text to copy.

### CourtListener sibling dockets pool into one summary

When multiple CourtListener `docket_id` values resolve to the **same**
`(docket_number, court_id)` — typically because the upstream
`pacer_case_id` changed mid-life and CourtListener stored the docket
under two or more IDs — Case Calendar treats them as one logical PACER
docket. The summary pipeline pools entries across every sibling
`docket_id` in the group (deduplicating by PACER `entry_number`),
generates a single summary, and renders one paragraph in the index. So
the Akhter case listed as `dockets: [71989485, 73333500, 73320754]`
where all three share docket number `1:25-cr-00307` in E.D. Va. produces
one Sonnet call, one stored summary, and one paragraph — not three
near-duplicate slices. See
[CourtListener sibling dockets](configuration.md#courtlistener-sibling-dockets-same-docket-number-different-docket_ids)
for how to spot when this is happening and how to list them in `config.yaml`.

## extra_documents

CourtListener and PACER sometimes don't surface documents the public
should be able to see. Two real failure modes the project has hit:

- **Sealed-then-unsealed entries** that the clerk hasn't yet unhidden or
  re-uploaded. The indictment is technically public (the seal was
  lifted in connection with extradition), but it is still missing
  from PACER.
- **CourtListener metadata bugs.** A PDF is in CourtListener's storage
  bucket but the v4 API reports `is_available: false` because the file
  was uploaded under an older `pacer_case_id` than the docket's current
  one ([CourtListener bug #7345](https://github.com/freelawproject/courtlistener/issues/7345)).

For those cases, point Case Calendar at the document directly. Each entry
needs three fields:

```yaml
- id: us-v-zewei
  name: "United States v. Zewei"
  calendar: cybercrime
  dockets: [70789744]
  extra_documents:
    - docket: 70789744
      url: https://www.justice.gov/opa/media/1407196/dl
      note: >-
        This PDF is the unsealed indictment in S.D. Tex. case
        4:23-cr-00523 (United States v. Xu Zewei).
```

| Field | Purpose |
| --- | --- |
| `docket` | Must be one of this case's `dockets` ids. |
| `url` | Absolute `https://` URL to a PDF. Anywhere — DoJ press releases, archived storage URLs, court websites. |
| `note` | Required. Tells the summary LLM what the document is and why it was added. The note rides into the prompt as trusted operator metadata; the document text itself is still treated as untrusted (the same way CourtListener / PACER text is). |

Case Calendar fetches the bytes through the same pypdf → OCR fallback chain
as it does for CourtListener documents, then feeds them to the LLM as their
own labeled section. Each entry's LLM block is headed
`OPERATOR-PROVIDED DOCUMENT (sourced outside CourtListener)` with the
operator's `note` line beneath it.

**Keep the `note` short** — one sentence that identifies the document by
name plus a case citation. The note is data fed to the summary LLM. Bug
numbers, workaround details, or "remove this once CourtListener fixes it" all belong
in a `#` comment in `config.yaml`, *not* in `note`. The LLM is summarizing
the case for public subscribers; any mention of CourtListener internals
or tooling state in its output would be both off-topic and a leak of
internal context.

Remove each `extra_documents` entry once the upstream gap closes.

## Legal disclaimers

The index page renders a static `<footer>` block carrying two disclaimers:

> *Case descriptions are generated by AI and may contain mistakes.*
>
> *Criminal defendants are presumed innocent unless and until convicted
> in a court of law.*

Both are rendered by the page template, not by the LLM. The legally-loaded
text is stable regardless of model output or prompt revision. The summary
prompt explicitly tells the model NOT to include these — they're the
renderer's responsibility.

## Next steps

- [Public index page](public-page.md) — how summaries get rendered.
- [Configuration](configuration.md) — the complete `config.yaml` reference.
