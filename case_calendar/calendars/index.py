"""Static index.html renderer for the public calendar feed directory.

Writes a single self-contained HTML page that lists every calendar in the
config plus the cases tracked in each, with subscribe links to the matching
ICS feeds and per-case metadata (docket links, date filed, last activity).
No external CSS/JS, no CDN — the page is one file Caddy can serve directly.

Dark mode follows the system preference via ``prefers-color-scheme`` and is
overridable by a header toggle. The page declares ``color-scheme: light dark``
on ``:root`` plus a matching ``<meta name="color-scheme">`` so the Darkreader
extension treats it as a dark-aware site and skips applying its own filter.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


def _esc(value: Any) -> str:
    """HTML-escape a value, returning '' for None."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _format_date(iso: Optional[str]) -> str:
    """Render an ISO timestamp as YYYY-MM-DD (or '' on missing / unparseable)."""
    if not iso:
        return ""
    # Accept both date-only and full ISO. We deliberately don't localize —
    # the dates here are court-business dates (filings, activity), not
    # subscriber-local hearing times.
    return iso[:10]


def _ics_links(
    ics_path: Optional[str],
    public_base_url: Optional[str],
) -> dict[str, Optional[str]]:
    """Build the subscribe URLs surfaced next to each calendar.

    Returns ``{"webcal": ..., "https": ..., "relative": ...}``. When
    ``public_base_url`` is set, ``webcal`` / ``https`` are absolute URLs
    pointing at the file the user's host serves. ``relative`` is always
    the bare filename so subscribers viewing the index over file:// can
    still click through.
    """
    if not ics_path:
        return {"webcal": None, "https": None, "relative": None}
    filename = Path(ics_path).name
    if public_base_url:
        base = public_base_url.rstrip("/")
        # Strip any scheme prefix to derive the webcal:// equivalent.
        if base.startswith("https://"):
            host_path = base[len("https://"):]
        elif base.startswith("http://"):
            host_path = base[len("http://"):]
        else:
            host_path = base
        return {
            "webcal": f"webcal://{host_path}/{filename}",
            "https": f"{base}/{filename}",
            "relative": filename,
        }
    return {"webcal": None, "https": None, "relative": filename}


_STYLES = """
:root {
  color-scheme: light dark;
  --bg: #fafaf8;
  --fg: #1c1c1c;
  --muted: #5c5c5c;
  --accent: #1a4480;
  --border: #d8d8d2;
  --card-bg: #ffffff;
  --hover-bg: #f0efe9;
}
html[data-theme="dark"] {
  color-scheme: dark;
  --bg: #16181c;
  --fg: #e5e5e5;
  --muted: #9aa0a6;
  --accent: #7ea7ee;
  --border: #2a2d33;
  --card-bg: #1e2127;
  --hover-bg: #262a31;
}
@media (prefers-color-scheme: dark) {
  html:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #16181c;
    --fg: #e5e5e5;
    --muted: #9aa0a6;
    --accent: #7ea7ee;
    --border: #2a2d33;
    --card-bg: #1e2127;
    --hover-bg: #262a31;
  }
}
* { box-sizing: border-box; }
/* Font sizing follows WCAG 2.2 body-text guidance: the browser default of
   16px is the minimum for primary reading content (the per-docket case
   summaries here), with secondary metadata held at 0.875rem (14px) — large
   enough to remain legible without crowding the dense case rows. We
   intentionally do NOT set body font-size below 16px; that would scale
   every rem-based size down with it. */
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 16px;
  line-height: 1.55;
  margin: 0;
  background: var(--bg);
  color: var(--fg);
}
header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  padding: 1.2rem 1.6rem;
  border-bottom: 1px solid var(--border);
}
header h1 { font-size: 1.5rem; margin: 0; font-weight: 600; }
header .meta { color: var(--muted); font-size: 0.9rem; }
main { padding: 1.6rem; max-width: 1100px; margin: 0 auto; }
section.calendar {
  margin-bottom: 2.4rem;
  padding: 1.2rem 1.4rem;
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 6px;
}
section.calendar > header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 1rem;
  padding: 0 0 0.6rem 0;
  border: 0;
  border-bottom: 1px solid var(--border);
  margin-bottom: 0.8rem;
  flex-wrap: wrap;
}
section.calendar h2 { font-size: 1.25rem; margin: 0; }
.subscribe a {
  margin-left: 0.6rem;
  font-size: 0.9rem;
  text-decoration: none;
  color: var(--accent);
  border: 1px solid var(--border);
  padding: 0.25rem 0.55rem;
  border-radius: 4px;
}
.subscribe a:hover { background: var(--hover-bg); }
.controls {
  display: flex;
  gap: 1rem;
  align-items: center;
  font-size: 0.9rem;
  color: var(--muted);
  margin-bottom: 0.8rem;
}
.controls select, .controls input {
  font: inherit;
  color: var(--fg);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0.2rem 0.45rem;
}
ol.cases { list-style: none; margin: 0; padding: 0; }
ol.cases > li {
  padding: 0.9rem 0;
  border-bottom: 1px solid var(--border);
}
ol.cases > li:last-child { border-bottom: 0; }
ol.cases > li.truncated { display: none; }
button.show-more {
  font: inherit;
  background: var(--bg);
  color: var(--accent);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0.4rem 0.8rem;
  margin-top: 0.8rem;
  cursor: pointer;
  font-size: 0.9rem;
}
button.show-more:hover { background: var(--hover-bg); }
ol.cases h3 { font-size: 1.15rem; margin: 0 0 0.3rem 0; font-weight: 600; }
.summary {
  margin: 0.6rem 0;
  font-size: 1rem;       /* primary reading text — keep at body size */
  line-height: 1.6;
  color: var(--fg);
}
.summary p { margin: 0 0 0.5rem 0; }
.summary p:last-child { margin-bottom: 0; }
.summary .docket-label {
  color: var(--muted);
  font-weight: 600;
  font-size: 0.85rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.dockets { margin: 0.3rem 0; padding: 0; list-style: none; font-size: 0.95rem; }
.dockets li { display: inline; }
.dockets li:not(:last-child)::after { content: " · "; color: var(--muted); }
.dockets a { color: var(--accent); text-decoration: none; }
.dockets a:hover { text-decoration: underline; }
.dates {
  font-size: 0.9rem;
  color: var(--muted);
  margin: 0.4rem 0 0 0;
}
.dates span { margin-right: 1.2rem; }
.dates b { font-weight: 600; color: var(--fg); }
button#theme-toggle {
  font: inherit;
  background: var(--bg);
  color: var(--fg);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0.25rem 0.65rem;
  cursor: pointer;
}
button#theme-toggle:hover { background: var(--hover-bg); }
footer {
  text-align: center;
  color: var(--muted);
  font-size: 0.95rem;     /* disclaimers must remain easily readable */
  padding: 1.5rem 1.2rem;
  border-top: 1px solid var(--border);
  margin-top: 2rem;
}
footer p { margin: 0.4rem 0; }
"""

# Pre-paint theme application: read the saved preference and apply it on the
# <html> element before stylesheets run, so users who picked light/dark don't
# see a flash of the wrong theme. The "auto" branch leaves data-theme unset
# so the prefers-color-scheme media query in CSS handles it.
_PREPAINT_JS = """
(function() {
  try {
    var saved = localStorage.getItem('cc-theme');
    if (saved === 'dark' || saved === 'light') {
      document.documentElement.setAttribute('data-theme', saved);
    }
  } catch (e) {}
})();
"""

_RUNTIME_JS = """
(function() {
  var root = document.documentElement;
  var btn = document.getElementById('theme-toggle');
  function currentTheme() {
    var attr = root.getAttribute('data-theme');
    if (attr === 'dark' || attr === 'light') return attr;
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  function labelFor(t) { return t === 'dark' ? 'Light mode' : 'Dark mode'; }
  function applyToggle() { btn.textContent = labelFor(currentTheme()); }
  btn.addEventListener('click', function() {
    var next = currentTheme() === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem('cc-theme', next); } catch (e) {}
    applyToggle();
  });
  applyToggle();
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyToggle);

  // Per-section sort + truncation. Each <li.case> carries
  // data-name / data-filed / data-activity; we re-append in the chosen
  // order, then hide everything past VISIBLE_DEFAULT unless the section
  // has been expanded. The hidden count + label update live in
  // applyTruncation so they stay in sync after every sort change.
  var VISIBLE_DEFAULT = 3;
  function sortCases(section) {
    var sel = section.querySelector('select.sort');
    var asc = section.querySelector('select.dir').value === 'asc';
    var key = sel.value;
    var list = section.querySelector('ol.cases');
    var items = Array.prototype.slice.call(list.children);
    items.sort(function(a, b) {
      var av = a.getAttribute('data-' + key) || '';
      var bv = b.getAttribute('data-' + key) || '';
      // Empty values sort last regardless of direction so "no data" cases
      // don't pollute the top.
      if (av === '' && bv !== '') return 1;
      if (bv === '' && av !== '') return -1;
      if (av < bv) return asc ? -1 : 1;
      if (av > bv) return asc ? 1 : -1;
      return 0;
    });
    items.forEach(function(li) { list.appendChild(li); });
    applyTruncation(section);
  }
  function applyTruncation(section) {
    var expanded = section.getAttribute('data-expanded') === 'true';
    var items = section.querySelectorAll('ol.cases > li');
    var hidden = 0;
    for (var i = 0; i < items.length; i++) {
      if (!expanded && i >= VISIBLE_DEFAULT) {
        items[i].classList.add('truncated');
        hidden++;
      } else {
        items[i].classList.remove('truncated');
      }
    }
    var btn = section.querySelector('button.show-more');
    if (btn) {
      btn.textContent = expanded
        ? 'Show fewer'
        : 'Show all (' + hidden + ' more)';
      btn.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    }
  }
  document.querySelectorAll('section.calendar').forEach(function(section) {
    section.querySelector('select.sort').addEventListener('change', function() {
      sortCases(section);
    });
    section.querySelector('select.dir').addEventListener('change', function() {
      sortCases(section);
    });
    var sm = section.querySelector('button.show-more');
    if (sm) {
      sm.addEventListener('click', function() {
        var cur = section.getAttribute('data-expanded') === 'true';
        section.setAttribute('data-expanded', cur ? 'false' : 'true');
        applyTruncation(section);
      });
    }
    sortCases(section);  // apply initial order + truncation
  });
})();
"""


def _render_subscribe(links: dict[str, Optional[str]]) -> str:
    """Render the subscribe-link cluster for one calendar."""
    if not links["relative"]:
        return ""
    parts = ['<p class="subscribe">']
    if links["webcal"]:
        parts.append(
            f'<a href="{_esc(links["webcal"])}" '
            f'title="One-click subscribe in Apple Calendar / Outlook">Subscribe</a>'
        )
    if links["https"]:
        parts.append(
            f'<a href="{_esc(links["https"])}" '
            f'title="Copy this URL into Google Calendar / Proton / etc.">HTTPS feed</a>'
        )
    parts.append(
        f'<a href="{_esc(links["relative"])}" download '
        f'title="Download the raw .ics file">Download</a>'
    )
    parts.append("</p>")
    return "".join(parts)


def _render_summaries(
    case: dict[str, Any], dockets: list[dict[str, Any]],
) -> str:
    """Render the AI-generated per-docket summary block for one case.

    ``summaries`` on the case is a list of ``{docket_id, summary}`` rows.
    When the case has a single docket and a single summary, we render the
    prose without a docket label. When the case aggregates multiple
    dockets, we label each paragraph with the docket number so subscribers
    can tell which suit each sentence refers to. Missing summaries are
    simply absent — the gate is at generation time, not display time.
    """
    summaries = case.get("summaries") or []
    if not summaries:
        return ""
    docket_label_by_id: dict[Any, str] = {}
    for d in dockets:
        # Format: "1:24-cr-12345 (S.D.N.Y.)" — short enough to sit inline
        # at the start of a paragraph as a colored subhead.
        parts = []
        if d.get("docket_number"):
            parts.append(d["docket_number"])
        if d.get("court_citation"):
            parts.append(f"({d['court_citation']})")
        docket_label_by_id[d.get("docket_id")] = " ".join(parts) if parts else ""

    multi = len([s for s in summaries if (s.get("summary") or "").strip()]) > 1
    paragraphs: list[str] = []
    for s in summaries:
        body = (s.get("summary") or "").strip()
        if not body:
            continue
        if multi:
            label = docket_label_by_id.get(s.get("docket_id")) or ""
            label_html = (
                f'<span class="docket-label">{_esc(label)}</span> — '
                if label else ""
            )
            paragraphs.append(f"<p>{label_html}{_esc(body)}</p>")
        else:
            paragraphs.append(f"<p>{_esc(body)}</p>")
    if not paragraphs:
        return ""
    return f'<div class="summary">{"".join(paragraphs)}</div>'


def _render_case(case: dict[str, Any]) -> str:
    """Render one <li> for a case row.

    ``case`` shape:
      {
        "name": "US v. X",
        "dockets": [
            {"docket_number": "1:24-cr-12345", "court_citation": "S.D.N.Y.",
             "absolute_url": "https://www.courtlistener.com/docket/...",
             "docket_id": 12345},
            ...
        ],
        "summaries": [
            {"docket_id": 12345, "summary": "..."},
            ...
        ] | [],
        "date_filed": "2025-01-15" | None,
        "activity_date": "2026-05-10T12:34:00Z" | None,
      }
    """
    name = _esc(case.get("name"))
    date_filed = _format_date(case.get("date_filed"))
    activity = _format_date(case.get("activity_date"))
    # Sort keys are case-insensitive (name) and ISO (dates), so direct
    # string compare on data-* attributes Just Works in the JS.
    data = (
        f'data-name="{_esc((case.get("name") or "").lower())}" '
        f'data-filed="{_esc(date_filed)}" '
        f'data-activity="{_esc(activity)}"'
    )
    dockets_html = []
    dockets = case.get("dockets") or []
    for d in dockets:
        label_parts = []
        if d.get("docket_number"):
            label_parts.append(_esc(d["docket_number"]))
        if d.get("court_citation"):
            label_parts.append(f"({_esc(d['court_citation'])})")
        label = " ".join(label_parts) or _esc(d.get("docket_id"))
        if d.get("absolute_url"):
            url = d["absolute_url"]
            # CL absolute_url is a path like /docket/12345/foo/; promote to
            # a full URL when needed so the link works regardless of where
            # the index.html is hosted.
            if url.startswith("/"):
                url = f"https://www.courtlistener.com{url}"
            dockets_html.append(
                f'<li><a href="{_esc(url)}" target="_blank" rel="noopener">{label}</a></li>'
            )
        else:
            dockets_html.append(f"<li>{label}</li>")
    dockets_block = (
        f'<ul class="dockets">{"".join(dockets_html)}</ul>'
        if dockets_html else ""
    )
    summary_block = _render_summaries(case, dockets)
    dates_bits = []
    if date_filed:
        dates_bits.append(f"<span><b>Filed</b> {_esc(date_filed)}</span>")
    if activity:
        dates_bits.append(f"<span><b>Last activity</b> {_esc(activity)}</span>")
    dates_block = (
        f'<p class="dates">{"".join(dates_bits)}</p>' if dates_bits else ""
    )
    return (
        f'<li {data}>'
        f'<h3>{name}</h3>'
        f'{dockets_block}'
        f'{summary_block}'
        f'{dates_block}'
        f'</li>'
    )


def _render_calendar(calendar: dict[str, Any]) -> str:
    """Render one <section class="calendar"> block.

    ``calendar`` shape:
      {
        "id": "cyber",
        "name": "Cybercrime cases",
        "links": {"webcal": ..., "https": ..., "relative": ...},
        "cases": [<case dict>, ...],
      }
    """
    subscribe = _render_subscribe(calendar["links"])
    cases = calendar.get("cases") or []
    case_rows = "".join(_render_case(c) for c in cases)
    if not case_rows:
        case_rows = '<li class="empty"><em>No cases configured.</em></li>'
    # JS hides any cases past index 2 and updates the button label after
    # sorting. We render the button with the correct initial count so users
    # who block JS still see the full list and the button is just inert.
    visible_default = 3
    hidden = max(0, len(cases) - visible_default)
    show_more = (
        f'<button class="show-more" type="button" aria-expanded="false">'
        f'Show all ({hidden} more)</button>'
    ) if hidden else ""
    return (
        f'<section class="calendar" data-cal="{_esc(calendar["id"])}" '
        f'data-expanded="false">'
        f'<header>'
        f'<h2>{_esc(calendar.get("name") or calendar["id"])}</h2>'
        f'{subscribe}'
        f'</header>'
        f'<div class="controls">'
        f'<label>Sort by '
        f'<select class="sort">'
        f'<option value="activity" selected>Last activity</option>'
        f'<option value="filed">Date filed</option>'
        f'<option value="name">Case name</option>'
        f'</select>'
        f'</label>'
        f'<label>Direction '
        f'<select class="dir">'
        f'<option value="desc" selected>Descending</option>'
        f'<option value="asc">Ascending</option>'
        f'</select>'
        f'</label>'
        f'</div>'
        f'<ol class="cases">{case_rows}</ol>'
        f'{show_more}'
        f'</section>'
    )


DEFAULT_SITE_DESCRIPTION = (
    "Subscribable calendar feeds for federal court hearings and filing "
    "deadlines, sourced from CourtListener and RECAP."
)


def render_index(
    *,
    calendars: Iterable[dict[str, Any]],
    site_title: str = "case-calendar",
    site_description: str = DEFAULT_SITE_DESCRIPTION,
    generated_at: Optional[datetime] = None,
) -> str:
    """Render the full index.html as a string.

    The page is self-contained: inline CSS and JS, no external requests.
    Pass ``calendars`` as the output of :func:`build_calendar_models`.
    """
    if generated_at is None:
        generated_at = datetime.now(timezone.utc)
    sections = "".join(_render_calendar(c) for c in calendars)
    gen_iso = generated_at.replace(microsecond=0).isoformat()
    return (
        '<!doctype html>\n'
        '<html lang="en">\n'
        '<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        # The color-scheme meta + matching :root declaration tell Darkreader
        # (and any user-agent dark-mode heuristic) that the page handles its
        # own dark theme — Darkreader's "detect dark theme" feature respects
        # this and won't double-darken our palette.
        '<meta name="color-scheme" content="light dark">\n'
        # Function-based description, not case-list-based: the tracked cases
        # change but the site's purpose doesn't, so this stays stable across
        # rebuilds and deployments.
        f'<meta name="description" content="{_esc(site_description)}">\n'
        f'<title>{_esc(site_title)}</title>\n'
        f'<style>{_STYLES}</style>\n'
        f'<script>{_PREPAINT_JS}</script>\n'
        '</head>\n'
        '<body>\n'
        '<header>\n'
        f'<h1>{_esc(site_title)}</h1>\n'
        f'<span class="meta">Generated {_esc(gen_iso)} '
        f'<button id="theme-toggle" type="button">Dark mode</button></span>\n'
        '</header>\n'
        f'<main>{sections}</main>\n'
        '<footer>'
        '<p>Subscribe to a calendar above, or download the raw .ics. '
        'Hearings and deadlines come from CourtListener / RECAP.</p>'
        '<p>Case descriptions are generated by AI from public court filings '
        'and may contain mistakes — consult the linked dockets for authoritative '
        'information.</p>'
        '<p>Criminal defendants are presumed innocent unless and until '
        'convicted in a court of law.</p>'
        '</footer>\n'
        f'<script>{_RUNTIME_JS}</script>\n'
        '</body>\n'
        '</html>\n'
    )


def write_index(
    index_path: str | Path,
    *,
    calendars: Iterable[dict[str, Any]],
    site_title: str = "case-calendar",
    site_description: str = DEFAULT_SITE_DESCRIPTION,
) -> None:
    """Render and write the index page to ``index_path``."""
    path = Path(index_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_index(
            calendars=calendars,
            site_title=site_title,
            site_description=site_description,
        ),
        encoding="utf-8",
    )


def build_calendar_models(
    cfg: dict[str, Any],
    store: Any,
    *,
    public_base_url: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Assemble the calendar/case data the renderer needs from cfg + store.

    The result is the shape :func:`render_index` consumes. Calendars appear
    in config-declaration order; within each calendar, cases also follow
    config order — the client-side JS resorts on load.
    """
    # Group cases by calendar id, preserving config order within each group.
    cases_by_cal: dict[str, list[dict[str, Any]]] = {}
    for c in cfg.get("cases") or []:
        cases_by_cal.setdefault(c["calendar"], []).append(c)

    out: list[dict[str, Any]] = []
    for cal_id, cal_cfg in (cfg.get("calendars") or {}).items():
        case_rows: list[dict[str, Any]] = []
        for c in cases_by_cal.get(cal_id, []):
            docket_ids = list(c.get("dockets") or [])
            dockets_meta: list[dict[str, Any]] = []
            for did in docket_ids:
                meta = store.get_docket_meta(did) or {}
                court_citation = None
                if meta.get("court_id"):
                    court_citation = store.get_court_citation(meta["court_id"])
                dockets_meta.append({
                    "docket_id": did,
                    "docket_number": meta.get("docket_number"),
                    "court_id": meta.get("court_id"),
                    "court_citation": court_citation,
                    "absolute_url": meta.get("absolute_url"),
                })
            agg = store.get_case_aggregates(docket_ids)
            # Per-docket AI summaries — opt-in feature. The list is empty when
            # the operator hasn't run `case-calendar summarize` for this case,
            # which causes the renderer to skip the summary block entirely.
            summaries: list[dict[str, Any]] = []
            get_summaries = getattr(store, "get_case_summaries", None)
            if callable(get_summaries):
                summaries = get_summaries(c["id"])
                # Preserve config-defined docket order in the rendered output
                # so multi-docket cases read in the order the operator listed.
                order = {did: i for i, did in enumerate(docket_ids)}
                summaries.sort(key=lambda s: order.get(s.get("docket_id"), 1_000_000))
            case_rows.append({
                "id": c.get("id"),
                "name": c.get("name"),
                "dockets": dockets_meta,
                "summaries": summaries,
                "date_filed": agg["date_filed"],
                "activity_date": agg["activity_date"],
            })
        out.append({
            "id": cal_id,
            "name": cal_cfg.get("name", cal_id),
            "links": _ics_links(cal_cfg.get("ics_path"), public_base_url),
            "cases": case_rows,
        })
    return out
