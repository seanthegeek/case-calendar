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
| `provider` | no | Force a specific provider (`anthropic` / `openai` / `gemini`). Defaults to whichever LLM key is set, or `LLM_SUMMARY_PROVIDER`. |
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

Summaries run on a higher-tier model than the extractor pipeline — the
synthesis task warrants the upgrade. Defaults:

| Provider | Default model |
| --- | --- |
| Anthropic | Claude Sonnet 4.6 |
| OpenAI | GPT-5.4 |
| Gemini | Gemini 2.5 Pro |

Budget roughly **$0.10–0.60 per docket for the first run**, near-zero on
subsequent runs (existing rows are reused unless the docket got a new
primary document or disposition). On a 30-case calendar you'll probably
spend a few dollars to backfill and pennies a week thereafter.

To force a regeneration after a model upgrade or prompt change:

```bash
uv run case-calendar summarize --force
# or, bundled into a polling sync to share the CourtListener session:
uv run case-calendar sync --force-summaries
```

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

This rule is one of several guardrails baked into the prompt. The model is
also told:

- A trial *date* in a scheduling order is not proof a trial occurred.
  Don't say "tried before a jury" unless there's a verdict form or
  judgment-after-trial.
- If you mention a hearing, state the date. Vague phrasing
  ("a hearing is scheduled") that hides whether the date is past or
  unverified is forbidden.
- Past-dated hearings that the docket hasn't confirmed as held or
  vacated must be described as past-but-unconfirmed, not as upcoming.
- When a judgment is provided, the imposed sentence (term of
  imprisonment, supervised release, fine, restitution) must appear in
  the summary verbatim.
- A defendant's custody status ("remains a fugitive", "in custody",
  "at large") may be stated only when a document establishes it. When the
  record doesn't, the status is described as **unknown** — never inferred
  from the absence of an arrest entry on the docket.
- Don't assert the absence of hearings, deadlines, or a disposition.
  A docket can be sealed or only partly mirrored in RECAP, so "no hearings
  are set" / "no disposition has been entered" can be quietly wrong; the
  summary states what *is* in the record and stays silent on the rest.
- State a specific dollar figure (restitution, forfeiture, fine) only when
  it appears legibly in the documents. Hand-filled court forms OCR into
  noise, and a number reconstructed from garbled text is a fabrication —
  the summary says "ordered to pay restitution" without inventing an amount,
  and **omits it silently**: it does not explain *why* the number is missing
  ("not clearly legible", "could not be read"), because the order is legible
  to a human — the gap is our OCR's, not the document's, and it isn't
  subscriber-facing.
- The system prompt does NOT render the legal disclaimers ("AI-generated,
  may contain mistakes" + presumption of innocence) — those are baked
  into the page template so the language stays stable regardless of
  model output.

## The post-generation guard

Prompt rules are *soft* — a model can ignore them, and for a brand-new
case there's no earlier good summary to fall back on, so a slip would reach
subscribers. So a deterministic guard runs on every generated summary
before it's stored, as a hard backstop to the prompt rules above:

- **Absence-of-record and unsupported-custody claims** (a "no disposition
  has been entered" in any phrasing, or a "remains at large" the documents
  don't support) trigger **one regeneration** with the specific problem
  fed back to the model. Whichever attempt is cleaner is kept; if the
  problem persists, the summary is still stored but a warning is logged
  for review. The summary is never blocked.
- **Dates and dollar amounts** that can't be traced to the hearings /
  deadlines scaffold, the source documents, or the operator-supplied notes
  (the `aggregation_note` and any `extra_documents` notes) are **logged for
  operator review** (not retried — dates appear in nearly every summary and
  harmless formatting differences would otherwise cause churn). This is
  the check that catches a hallucinated restitution figure or an invented
  hearing date — while still allowing a figure you deliberately supply in a
  note (e.g. a sentencing date conveyed to an appeal docket's summary).

The guard is why the project can run summaries unattended: a wrong fact on
a public calendar is worse than a missing one, and the guard makes the
wrong-fact case either self-correct or surface in the logs.

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
