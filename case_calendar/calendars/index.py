"""Static index.html renderer for the public calendar feed directory.

Writes a single self-contained HTML page that lists every calendar in the
config plus the cases tracked in each, with subscribe links to the matching
ICS feeds and per-case metadata (docket links, date filed, last filing).
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
            host_path = base[len("https://") :]
        elif base.startswith("http://"):
            host_path = base[len("http://") :]
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
.search-bar {
  padding: 0.9rem 1.6rem;
  border-bottom: 1px solid var(--border);
  background: var(--card-bg);
  display: flex;
  gap: 1rem;
  align-items: center;
  justify-content: center;
  flex-wrap: wrap;
}
.search-bar input {
  flex: 1;
  min-width: 200px;
  max-width: 500px;
  font: inherit;
  font-size: 1rem;
  color: var(--fg);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0.45rem 0.7rem;
}
.search-bar .status { color: var(--muted); font-size: 0.9rem; }
section.calendar.hidden-by-search { display: none; }
ol.cases > li.hidden-by-search { display: none; }
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
.subscribe a,
.subscribe button {
  margin-left: 0.6rem;
  font-size: 0.9rem;
  font-family: inherit;
  text-decoration: none;
  color: var(--accent);
  background: transparent;
  border: 1px solid var(--border);
  padding: 0.25rem 0.55rem;
  border-radius: 4px;
  cursor: pointer;
}
.subscribe a:hover,
.subscribe button:hover { background: var(--hover-bg); }
.subscribe button.is-copied { color: var(--fg); }
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
ul.tags {
  list-style: none;
  margin: 0.5rem 0 0 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
}
ul.tags li { display: inline-flex; }
button.tag {
  font: inherit;
  font-size: 0.8rem;
  color: var(--accent);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 0.1rem 0.55rem;
  cursor: pointer;
}
button.tag:hover { background: var(--hover-bg); }
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

_RUNTIME_JS = r"""
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
  // data-name / data-filed / data-last-filing / data-search; we re-append
  // in the chosen order, then hide everything past VISIBLE_DEFAULT unless
  // the section has been expanded. The hidden count + label update live in
  // applyTruncation so they stay in sync after every sort change.
  var VISIBLE_DEFAULT = 3;
  var searchInput = document.getElementById('case-search');
  var searchStatus = document.getElementById('search-status');
  function isSearching() {
    return !!(searchInput && searchInput.value && searchInput.value.trim());
  }
  // Tokenize a query into substring-AND clauses. Bare tokens split on
  // whitespace; "quoted strings" are taken whole so multi-word tags
  // survive the search round-trip. Raw case is preserved so the tag-chip
  // handler can round-trip the user's existing query verbatim; callers
  // that need case-insensitive compare lowercase at the comparison site.
  function searchTokens(query) {
    var out = [];
    var re = /"([^"]*)"|(\S+)/g;
    var m;
    while ((m = re.exec(query)) !== null) {
      var raw = m[1] !== undefined ? m[1] : m[2];
      raw = raw.trim();
      if (raw) out.push(raw);
    }
    return out;
  }
  function renderQueryTokens(tokens) {
    return tokens.map(function(t) {
      return /\s/.test(t) ? '"' + t + '"' : t;
    }).join(' ');
  }
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
    // Search overrides truncation: when filtering, every match is visible
    // and the show-more button is hidden. Items the search hid are skipped
    // when counting against VISIBLE_DEFAULT so a filtered list of 2 doesn't
    // also get truncated.
    var searching = isSearching();
    var expanded = section.getAttribute('data-expanded') === 'true' || searching;
    var items = section.querySelectorAll('ol.cases > li');
    var visibleIndex = 0;
    var hidden = 0;
    for (var i = 0; i < items.length; i++) {
      var li = items[i];
      if (li.classList.contains('hidden-by-search')) {
        li.classList.remove('truncated');
        continue;
      }
      if (!expanded && visibleIndex >= VISIBLE_DEFAULT) {
        li.classList.add('truncated');
        hidden++;
      } else {
        li.classList.remove('truncated');
      }
      visibleIndex++;
    }
    var btn = section.querySelector('button.show-more');
    if (btn) {
      if (searching) {
        btn.style.display = 'none';
      } else {
        btn.style.display = '';
        btn.textContent = expanded
          ? 'Show fewer'
          : 'Show all (' + hidden + ' more)';
        btn.setAttribute('aria-expanded', expanded ? 'true' : 'false');
      }
    }
  }
  function applySearch() {
    // AND-tokenized substring match against data-search (already lowercased
    // at render time). Empty query clears all hidden-by-search markers and
    // unhides every section; truncation then re-applies per section.
    // Tokens are extracted with the same parser the tag-chip handler uses so
    // a multi-word tag click ("white collar") writes one quoted token that
    // matches as one substring instead of two stray words.
    var raw = (searchInput && searchInput.value || '').trim();
    var tokens = raw ? searchTokens(raw).map(function(t) {
      return t.toLowerCase();
    }) : [];
    var totalShown = 0;
    document.querySelectorAll('section.calendar').forEach(function(section) {
      var items = section.querySelectorAll('ol.cases > li');
      var sectionShown = 0;
      for (var i = 0; i < items.length; i++) {
        var li = items[i];
        var haystack = li.getAttribute('data-search') || '';
        var match = true;
        for (var j = 0; j < tokens.length; j++) {
          if (haystack.indexOf(tokens[j]) === -1) { match = false; break; }
        }
        if (tokens.length === 0 || match) {
          li.classList.remove('hidden-by-search');
          sectionShown++;
        } else {
          li.classList.add('hidden-by-search');
        }
      }
      if (tokens.length > 0 && sectionShown === 0) {
        section.classList.add('hidden-by-search');
      } else {
        section.classList.remove('hidden-by-search');
      }
      totalShown += sectionShown;
      applyTruncation(section);
    });
    if (searchStatus) {
      if (tokens.length === 0) {
        searchStatus.textContent = '';
      } else {
        searchStatus.textContent = totalShown +
          (totalShown === 1 ? ' match' : ' matches');
      }
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
  if (searchInput) {
    searchInput.addEventListener('input', applySearch);
  }

  // Tag chips: clicking a tag adds it to the search box, wrapped in
  // quotes if it contains whitespace so a multi-word tag stays one
  // AND-clause. Repeat clicks on the same tag are idempotent (matched
  // case-insensitively against existing tokens), and the search runs
  // immediately so the page filters as the user clicks.
  document.querySelectorAll('button.tag').forEach(function(btn) {
    btn.addEventListener('click', function() {
      if (!searchInput) return;
      var tag = btn.getAttribute('data-tag') || '';
      if (!tag) return;
      var tokens = searchTokens(searchInput.value || '');
      var lc = tag.toLowerCase();
      var already = tokens.some(function(t) { return t.toLowerCase() === lc; });
      if (!already) tokens.push(tag);
      searchInput.value = renderQueryTokens(tokens);
      searchInput.focus();
      applySearch();
    });
  });

  // Copy-feed-URL buttons. The clipboard API only works in secure
  // contexts (https, localhost, or file://) — every realistic deployment
  // path the page lives at qualifies. On failure we surface "Copy failed"
  // so the user knows the click registered, and they can fall back to
  // right-click → copy link on the Subscribe button (same hostname).
  document.querySelectorAll('button.copy-feed').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var url = btn.getAttribute('data-url');
      if (!url || !navigator.clipboard) {
        btn.textContent = 'Copy failed';
        return;
      }
      navigator.clipboard.writeText(url).then(function() {
        btn.textContent = 'Copied!';
        btn.classList.add('is-copied');
        setTimeout(function() {
          btn.textContent = 'Copy feed URL';
          btn.classList.remove('is-copied');
        }, 1500);
      }).catch(function() {
        btn.textContent = 'Copy failed';
        setTimeout(function() {
          btn.textContent = 'Copy feed URL';
        }, 1500);
      });
    });
  });
})();
"""


def _render_subscribe(links: dict[str, Optional[str]]) -> str:
    """Render the subscribe-link cluster for one calendar.

    When a public https URL is configured we surface two actions:
    Subscribe (webcal://) for one-click add in Apple/Outlook, and a
    Copy-feed-URL button that writes the https URL to the clipboard
    for paste into Google Calendar / Proton / Thunderbird. The download
    link the page used to also offer was redundant — clicking the
    Subscribe link from a desktop browser downloads the .ics anyway,
    and the URL the new button copies is the same one Download served.

    When no public_base_url is set (operator runs without exposing the
    feeds publicly), only the Download fallback is rendered — copying
    a bare filename like ``cyber.ics`` is not useful to subscribers.
    """
    if not links["relative"]:
        return ""
    parts = ['<p class="subscribe">']
    if links["https"]:
        if links["webcal"]:
            parts.append(
                f'<a href="{_esc(links["webcal"])}" '
                f'title="One-click subscribe in Apple Calendar / Outlook">'
                f"Subscribe</a>"
            )
        # The button carries the https URL on data-url so the click
        # handler doesn't have to know which calendar it belongs to;
        # one global listener covers every button on the page.
        parts.append(
            f'<button type="button" class="copy-feed" '
            f'data-url="{_esc(links["https"])}" '
            f'title="Copy this URL into Google Calendar / Proton / etc.">'
            f"Copy feed URL</button>"
        )
    else:
        parts.append(
            f'<a href="{_esc(links["relative"])}" download '
            f'title="Download the raw .ics file">Download</a>'
        )
    parts.append("</p>")
    return "".join(parts)


def _render_summaries(
    case: dict[str, Any],
    dockets: list[dict[str, Any]],
) -> str:
    """Render the AI-generated per-docket summary block for one case.

    ``summaries`` on the case is a list of ``{docket_number, court_id,
    summary, ...}`` rows — one per logical PACER docket on the case (not
    per CourtListener docket_id; see the docket grouping design decision
    in AGENTS.md). When the case has a single summary we render the
    prose without a docket label. When the case aggregates multiple
    logical PACER dockets (e.g., a district + appellate filing), we
    label each paragraph with the docket number so subscribers can tell
    which suit each sentence refers to. Missing summaries are simply
    absent — the gate is at generation time, not display time.
    """
    summaries = case.get("summaries") or []
    if not summaries:
        return ""
    # Build a (docket_number, court_id) → label map from the docket
    # metadata. CourtListener docket_id splits sharing the same
    # (docket_number, court_id) get the same label entry, so they
    # collapse to one paragraph in the rendered output.
    label_by_group: dict[tuple[str, str], str] = {}
    for d in dockets:
        docket_number = d.get("docket_number")
        court_id = d.get("court_id")
        if not docket_number or not court_id:
            continue
        # Format: "1:24-cr-12345 (S.D.N.Y.)" — short enough to sit inline
        # at the start of a paragraph as a colored subhead.
        parts = [docket_number]
        if d.get("court_citation"):
            parts.append(f"({d['court_citation']})")
        label_by_group[(docket_number, court_id)] = " ".join(parts)

    multi = len([s for s in summaries if (s.get("summary") or "").strip()]) > 1
    paragraphs: list[str] = []
    for s in summaries:
        body = (s.get("summary") or "").strip()
        if not body:
            continue
        if multi:
            group_key = (s.get("docket_number"), s.get("court_id"))
            label = label_by_group.get(group_key) or ""
            label_html = (
                f'<span class="docket-label">{_esc(label)}</span> — ' if label else ""
            )
            paragraphs.append(f"<p>{label_html}{_esc(body)}</p>")
        else:
            paragraphs.append(f"<p>{_esc(body)}</p>")
    if not paragraphs:
        return ""
    return f'<div class="summary">{"".join(paragraphs)}</div>'


def _case_search_text(case: dict[str, Any]) -> str:
    """Build the lowercased haystack the client-side filter searches over.

    Includes the case name, every docket number + court citation, every
    per-docket summary body, and every configured tag. The JS does
    AND-tokenized substring matching against this string, so subscribers
    can search by defendant name, docket number, court, judge name (when
    it appears in the summary), tag, or any vocabulary from the prose
    without us maintaining a search index.
    """
    parts: list[str] = []
    if case.get("name"):
        parts.append(str(case["name"]))
    for d in case.get("dockets") or []:
        if d.get("docket_number"):
            parts.append(str(d["docket_number"]))
        if d.get("court_citation"):
            parts.append(str(d["court_citation"]))
    for s in case.get("summaries") or []:
        body = (s.get("summary") or "").strip()
        if body:
            parts.append(body)
    for tag in case.get("tags") or []:
        if tag:
            parts.append(str(tag))
    return " ".join(parts).lower()


def _normalize_tags(raw: Any) -> list[str]:
    """Strip + dedupe a raw tags list, mirroring ``_tags_from_config``.

    Used at the render boundary so the index reads tags the same shape
    the calendar event descriptions do: validation has already happened
    at config-load time, but the parsed ``CaseConfig`` isn't threaded
    into ``build_calendar_models``, so we re-normalize here rather than
    add a cli.py dependency.
    """
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        tag = item.strip()
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


def _render_tags(tags: list[str]) -> str:
    """Render the tag-chip list for one case.

    Each chip is a ``<button>`` carrying ``data-tag``; the runtime JS
    appends the tag to the global search box on click. We render the
    tag verbatim (the operator chose the casing) — case folding for
    matching happens against the lowercased ``data-search`` haystack.
    """
    if not tags:
        return ""
    chips = "".join(
        f'<li><button type="button" class="tag" '
        f'data-tag="{_esc(t)}" '
        f'title="Filter by tag: {_esc(t)}">{_esc(t)}</button></li>'
        for t in tags
    )
    return f'<ul class="tags">{chips}</ul>'


def _render_case(case: dict[str, Any]) -> str:
    """Render one <li> for a case row.

    ``case`` shape:
      {
        "name": "US v. X",
        "dockets": [
            {"docket_number": "1:24-cr-12345", "court_citation": "S.D.N.Y.",
             "absolute_url": "https://www.courtlistener.com/docket/...",
             "docket_id": 12345,
             "sibling_docket_ids": [12346, 12347]},  # optional — other CourtListener
                                                     # ids in the same group
            ...
        ],
        "summaries": [
            {"docket_number": "1:24-cr-12345", "court_id": "nysd",
             "summary": "..."},
            ...
        ] | [],
        "date_filed": "2025-01-15" | None,
        "last_filing_date": "2026-05-10" | None,
      }
    """
    name = _esc(case.get("name"))
    date_filed = _format_date(case.get("date_filed"))
    last_filing = _format_date(case.get("last_filing_date"))
    # Sort keys are case-insensitive (name) and ISO (dates), so direct
    # string compare on data-* attributes Just Works in the JS.
    data = (
        f'data-name="{_esc((case.get("name") or "").lower())}" '
        f'data-filed="{_esc(date_filed)}" '
        f'data-last-filing="{_esc(last_filing)}" '
        f'data-search="{_esc(_case_search_text(case))}"'
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
            # CourtListener absolute_url is a path like /docket/12345/foo/; promote to
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
        f'<ul class="dockets">{"".join(dockets_html)}</ul>' if dockets_html else ""
    )
    summary_block = _render_summaries(case, dockets)
    tags_block = _render_tags(list(case.get("tags") or []))
    dates_bits = []
    if date_filed:
        dates_bits.append(f"<span><b>Filed</b> {_esc(date_filed)}</span>")
    if last_filing:
        dates_bits.append(f"<span><b>Last filing</b> {_esc(last_filing)}</span>")
    dates_block = f'<p class="dates">{"".join(dates_bits)}</p>' if dates_bits else ""
    return (
        f"<li {data}><h3>{name}</h3>"
        f"{dockets_block}{summary_block}{tags_block}{dates_block}</li>"
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
        (
            f'<button class="show-more" type="button" aria-expanded="false">'
            f"Show all ({hidden} more)</button>"
        )
        if hidden
        else ""
    )
    return (
        f'<section class="calendar" data-cal="{_esc(calendar["id"])}" '
        f'data-expanded="false">'
        f"<header>"
        f"<h2>{_esc(calendar.get('name') or calendar['id'])}</h2>"
        f"{subscribe}"
        f"</header>"
        f'<div class="controls">'
        f"<label>Sort by "
        f'<select class="sort">'
        f'<option value="last-filing" selected>Last filing</option>'
        f'<option value="filed">Date filed</option>'
        f'<option value="name">Case name</option>'
        f"</select>"
        f"</label>"
        f"<label>Direction "
        f'<select class="dir">'
        f'<option value="desc" selected>Descending</option>'
        f'<option value="asc">Ascending</option>'
        f"</select>"
        f"</label>"
        f"</div>"
        f'<ol class="cases">{case_rows}</ol>'
        f"{show_more}"
        f"</section>"
    )


DEFAULT_SITE_DESCRIPTION = (
    "Subscribable calendar feeds for federal court hearings and filing "
    "deadlines, sourced from CourtListener and RECAP."
)


def render_index(
    *,
    calendars: Iterable[dict[str, Any]],
    site_title: str = "Case Calendar",
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
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
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
        f"<title>{_esc(site_title)}</title>\n"
        f"<style>{_STYLES}</style>\n"
        f"<script>{_PREPAINT_JS}</script>\n"
        "</head>\n"
        "<body>\n"
        "<header>\n"
        f"<h1>{_esc(site_title)}</h1>\n"
        f'<span class="meta">Generated {_esc(gen_iso)} '
        f'<button id="theme-toggle" type="button">Dark mode</button></span>\n'
        "</header>\n"
        '<div class="search-bar">\n'
        '<input type="search" id="case-search" '
        'placeholder="Search cases, dockets, courts, or summary text…" '
        'aria-label="Search cases" autocomplete="off">\n'
        '<span class="status" id="search-status" aria-live="polite"></span>\n'
        "</div>\n"
        f"<main>{sections}</main>\n"
        "<footer>"
        "<p>Hearings and deadlines come from CourtListener / RECAP.</p>"
        "<p>Case descriptions and calendar entries are generated by AI from public court filings "
        "and may contain mistakes — consult the linked dockets for authoritative "
        "information.</p>"
        "<p>Criminal defendants are presumed innocent unless and until "
        "convicted in a court of law.</p>"
        '<p>Powered by <a href="https://docs.casecalendar.net/">Case Calendar</a>.</p>'
        "</footer>\n"
        f"<script>{_RUNTIME_JS}</script>\n"
        "</body>\n"
        "</html>\n"
    )


def write_index(
    index_path: str | Path,
    *,
    calendars: Iterable[dict[str, Any]],
    site_title: str = "Case Calendar",
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
            # Build dockets_meta but collapse CourtListener docket_id splits
            # that share the same (docket_number, court_id) — they're one
            # logical PACER docket and should show as one entry in the
            # rendered output. The freshest (first-encountered) docket_id
            # in each group wins; sibling ids are kept aside on the entry
            # so subscribers / debugging can see them all.
            dockets_meta: list[dict[str, Any]] = []
            group_index: dict[tuple[Any, Any], int] = {}
            for did in docket_ids:
                meta = store.get_docket_meta(did) or {}
                court_citation = None
                if meta.get("court_id"):
                    court_citation = store.get_court_citation(meta["court_id"])
                docket_number = meta.get("docket_number")
                court_id = meta.get("court_id")
                if docket_number and court_id:
                    group_key: tuple[Any, Any] = (docket_number, court_id)
                    if group_key in group_index:
                        # Already have an entry for this group — append the
                        # docket_id to the sibling list and keep going.
                        existing = dockets_meta[group_index[group_key]]
                        existing.setdefault("sibling_docket_ids", []).append(did)
                        continue
                    group_index[group_key] = len(dockets_meta)
                dockets_meta.append(
                    {
                        "docket_id": did,
                        "docket_number": docket_number,
                        "court_id": court_id,
                        "court_citation": court_citation,
                        "absolute_url": meta.get("absolute_url"),
                    }
                )
            agg = store.get_case_aggregates(docket_ids)
            # Per-logical-docket AI summaries — opt-in feature. The list is
            # empty when the operator hasn't run `case-calendar summarize`
            # for this case, which causes the renderer to skip the summary
            # block entirely. Rows are keyed by (docket_number, court_id),
            # so a case with one logical docket spread across three CourtListener
            # docket_ids gets ONE summary, not three.
            summaries: list[dict[str, Any]] = store.get_case_summaries(c["id"])
            # Preserve config-defined docket order in the rendered output
            # so multi-docket cases read in the order the operator listed.
            # We order by group (docket_number, court_id), since a single
            # logical docket may map to multiple CourtListener docket_ids in config.
            order: dict[tuple[Any, Any], int] = {}
            for i, did in enumerate(docket_ids):
                m = store.get_docket_meta(did) or {}
                dn = m.get("docket_number")
                cid = m.get("court_id")
                if dn and cid:
                    order_key: tuple[Any, Any] = (dn, cid)
                    if order_key not in order:
                        order[order_key] = i
            summaries.sort(
                key=lambda s: order.get(
                    (s.get("docket_number"), s.get("court_id")),
                    1_000_000,
                )
            )
            case_rows.append(
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "dockets": dockets_meta,
                    "summaries": summaries,
                    "tags": _normalize_tags(c.get("tags")),
                    "date_filed": agg["date_filed"],
                    "last_filing_date": agg["last_filing_date"],
                }
            )
        out.append(
            {
                "id": cal_id,
                "name": cal_cfg.get("name", cal_id),
                "links": _ics_links(cal_cfg.get("ics_path"), public_base_url),
                "cases": case_rows,
            }
        )
    return out
