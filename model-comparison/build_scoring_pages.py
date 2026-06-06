#!/usr/bin/env python3
"""Generate ONE self-contained HTML scoring page for per-entry ground truth.

All benchmark cases live in a single offline HTML file, each in a collapsible
section. Expand a case and every docket entry is a card showing the entry's
COMPLETE text (description + extracted PDF text — the same text the extractor
saw) plus clickable links to each attached document, so you can check the actual
PDF when the extracted text looks like bad OCR. Beside each card are the eight
action-count boxes the extractor itself emits:

    hearings:  scheduled / rescheduled / held / cancelled
    deadlines: set / rescheduled / met-filed / cancelled

You read top-to-bottom, type the counts (most rows stay 0), tick "bad OCR" when
the source text is unreadable, and hit "Download CSV" — that one CSV (all cases)
is the human ground truth ``score.py`` reads, apples-to-apples with the model's
per-entry actions (``model_entry_actions.csv``). Counts autosave to localStorage,
so a refresh can't lose work, and each case's cards render only when expanded so
the ~1,100-entry file stays responsive.

Reads the COMPLETE benchmark store (every entry's text, fetched fresh from the
v4 API by ``fetch_complete_benchmark.py`` — NOT the web UI, which is incomplete,
see freelawproject/courtlistener#7429), so a regex-dropped entry that actually
schedules a hearing is visible and gets a human count the model never could.

Multi-CourtListener-record dockets (one logical PACER docket split across
records) are deduped to one card per logical entry; genuinely separate dockets
(a district case + its appeal) stay in separate sub-sections.

Usage:
    uv run python model-comparison/build_scoring_pages.py \
        [--config config.benchmark.yaml] \
        [--store model-comparison/snapshots/complete-benchmark-store.sqlite] \
        [--out model-comparison/scoring/ground_truth_scoring.html] [--case CASE_ID ...]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

from case_calendar.cli import _cases_from_config, _load_config  # noqa: E402
from case_calendar.pdf import recap_document_url  # noqa: E402

_DEFAULT_STORE = "model-comparison/snapshots/complete-benchmark-store.sqlite"
_DEFAULT_OUT = "model-comparison/scoring/ground_truth_scoring.html"
_CL_BASE = "https://www.courtlistener.com"

CATEGORIES = [
    ("h_scheduled", "H sched"),
    ("h_rescheduled", "H resched"),
    ("h_held", "H held"),
    ("h_cancelled", "H canc"),
    ("d_set", "D set"),
    ("d_rescheduled", "D resched"),
    ("d_met_filed", "D met/filed"),
    ("d_cancelled", "D canc"),
]


def _full_url(absolute_url: Optional[str]) -> str:
    if not absolute_url:
        return ""
    return absolute_url if absolute_url.startswith("http") else _CL_BASE + absolute_url


def _docket_meta(con: sqlite3.Connection, docket_id: int) -> dict[str, Any]:
    row = con.execute(
        "SELECT docket_number, court_id, absolute_url FROM dockets WHERE docket_id=?",
        (docket_id,),
    ).fetchone()
    return dict(row) if row else {}


def _docs_for(recap_documents_json: Optional[str]) -> list[dict[str, Any]]:
    """Per-document link rows for an entry, mirroring the calendar renderer's
    URL preference (CourtListener storage -> Internet Archive fallback)."""
    docs: list[dict[str, Any]] = []
    for rd in json.loads(recap_documents_json or "[]"):
        num = rd.get("document_number")
        att = rd.get("attachment_number")
        label = f"{num}" if not att else f"{num}-{att}"
        url = recap_document_url(rd)
        if rd.get("is_sealed"):
            status = "sealed"
        elif not url:
            status = "not yet on RECAP"
        else:
            status = ""
        docs.append({"label": label or "?", "url": url or "", "status": status})
    return docs


def _doc_text(recap_documents_json: Optional[str]) -> str:
    """Concatenated extracted text the model saw, for the OCR-quality check."""
    parts: list[str] = []
    for rd in json.loads(recap_documents_json or "[]"):
        txt = (rd.get("plain_text") or "").strip()
        if txt:
            num = rd.get("document_number")
            att = rd.get("attachment_number")
            label = f"{num}" if not att else f"{num}-{att}"
            parts.append(f"[doc {label}]\n{txt}")
    return "\n\n".join(parts)


def _text_len(e: dict[str, Any]) -> int:
    return len(e.get("description") or "") + len(_doc_text(e.get("recap_documents")))


def _dedup_key(e: dict[str, Any]) -> tuple:
    """Logical-entry identity ACROSS the CourtListener records of one docket.
    PACER ``entry_number`` when present; (date_filed, description prefix) for
    paperless entries that carry text. Empty-description paperless entries have
    no reliable logical identity, so they key on ``entry_id`` and are NEVER
    merged — distinct blank-text paperless entries on one date stay separate."""
    if e["entry_number"] is not None:
        return ("num", e["entry_number"])
    desc = (e["description"] or "").strip()
    if desc:
        return ("desc", e["date_filed"], desc[:200])
    return ("uid", e["entry_id"])


def _fetch_entries(con: sqlite3.Connection, docket_id: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in con.execute(
        "SELECT entry_id, entry_number, date_filed, description, "
        "short_description, recap_documents FROM entries WHERE docket_id=?",
        (docket_id,),
    ):
        e = dict(row)
        e["docket_id"] = docket_id
        out.append(e)
    return out


def _sort_entries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows.sort(
        key=lambda e: (
            e["entry_number"] if e["entry_number"] is not None else 1 << 30,
            e["date_filed"] or "",
        )
    )
    return rows


def _entries_for_group(
    con: sqlite3.Connection, docket_ids: list[int]
) -> list[dict[str, Any]]:
    """Entries for one logical docket. A single CourtListener record is shown
    verbatim (every ``entry_id`` is a distinct entry — no merging). Only when a
    logical docket is split across MULTIPLE records do we dedup, keeping the copy
    with the most text so the human sees the fullest version of each entry."""
    if len(docket_ids) == 1:
        return _sort_entries(_fetch_entries(con, docket_ids[0]))
    best: dict[tuple, dict[str, Any]] = {}
    for did in docket_ids:
        for e in _fetch_entries(con, did):
            key = _dedup_key(e)
            cur = best.get(key)
            if cur is None or _text_len(e) > _text_len(cur):
                best[key] = e
    return _sort_entries(list(best.values()))


def build_case_entries(con: sqlite3.Connection, case: Any) -> list[dict[str, Any]]:
    """Ordered, deduped per-entry records for one case, sectioned by logical
    docket (docket_number, court_id). Each entry carries only display fields +
    the keys ``score.py`` matches on (docket_number, court, entry_number)."""
    groups: dict[tuple, list[int]] = defaultdict(list)
    meta_by_did: dict[int, dict[str, Any]] = {}
    for did in case.dockets:
        meta = _docket_meta(con, did)
        meta_by_did[did] = meta
        groups[
            (
                meta.get("docket_number") or f"(docket {did})",
                meta.get("court_id") or "?",
            )
        ].append(did)

    out: list[dict[str, Any]] = []
    for (docket_number, court), dids in sorted(groups.items()):
        cl_url = ""
        for did in dids:
            cl_url = _full_url(meta_by_did[did].get("absolute_url"))
            if cl_url:
                break
        for e in _entries_for_group(con, dids):
            out.append(
                {
                    "case_id": case.case_id,
                    "docket_number": docket_number,
                    "court": court,
                    "docket_id": e["docket_id"],
                    "entry_id": e["entry_id"],
                    "entry_number": e["entry_number"],
                    "date_filed": e["date_filed"] or "",
                    "cl_url": cl_url,
                    "description": e["description"] or "",
                    "doc_text": _doc_text(e["recap_documents"]),
                    "docs": _docs_for(e["recap_documents"]),
                }
            )
    return out


# Self-contained page: inline CSS/JS, data embedded as JSON, no external requests.
# Entry text renders via textContent in JS, so untrusted docket text can't inject
# markup. Each case's cards render lazily on first expand (the file holds ~1,100
# entries). Counts autosave to localStorage keyed by CourtListener entry_id
# (globally unique), so one combined Download CSV covers every case.
_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Per-entry ground-truth scoring</title>
<style>
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{font:15px/1.5 system-ui,sans-serif;margin:0;padding:0 0 50vh}
header.bar{position:sticky;top:0;z-index:5;background:Canvas;border-bottom:1px solid #8884;
  padding:10px 16px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
header.bar h1{font-size:16px;margin:0 8px 0 0}
.stat{font-variant-numeric:tabular-nums}
button{font:inherit;padding:6px 12px;border:1px solid #8888;border-radius:6px;background:ButtonFace;cursor:pointer}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
details.help{margin:8px 0 4px;border:1px solid #8884;border-radius:8px;padding:8px 12px}
details.help summary{font-weight:600;cursor:pointer}
details.help table{border-collapse:collapse;margin-top:8px;font-size:13px}
details.help td{border:1px solid #8884;padding:3px 8px;vertical-align:top}
details.case{border:1px solid #8886;border-radius:8px;margin:12px 0;overflow:hidden}
details.case>summary{cursor:pointer;font-weight:600;padding:10px 12px;background:#8881;
  font-variant-numeric:tabular-nums}
.casebody{padding:0 12px}
h3.section{font-size:14px;border-bottom:2px solid #8884;padding-bottom:4px;margin:18px 0 8px}
.card{border:1px solid #8884;border-radius:8px;padding:12px;margin:12px 0}
.card.reviewed{opacity:.55}
.card.badocr{outline:2px solid #d97706}
.meta{font-size:13px;color:GrayText;display:flex;gap:14px;flex-wrap:wrap;align-items:baseline}
.meta .num{font-weight:700;color:CanvasText}
.docs a{margin-right:10px}.docs .nolink{margin-right:10px;color:GrayText}
.txt{white-space:pre-wrap;word-break:break-word;margin:8px 0;max-height:18em;overflow:auto;
  background:#8881;border-radius:6px;padding:8px}
details.dt>summary{cursor:pointer;color:GrayText;font-size:13px}
.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:8px;
  border-top:1px dashed #8884;padding-top:8px}
.controls label{font-size:12px;display:flex;flex-direction:column;align-items:center;gap:2px}
.controls input[type=number]{width:48px;padding:3px;text-align:center}
.controls .grp{display:flex;gap:8px;padding:4px 8px;border:1px solid #8883;border-radius:6px}
.controls .grp.h{border-color:#3b82f680}.controls .grp.d{border-color:#10b98180}
.flags{margin-left:auto;display:flex;gap:14px;align-items:center}
.flags label{flex-direction:row}
</style></head><body>
<header class="bar">
  <h1>Ground-truth scoring</h1>
  <span class="stat">reviewed <b id="rv">0</b>/<b id="tot">0</b></span>
  <span class="stat">non-zero <b id="nz">0</b></span>
  <span class="stat">bad-OCR <b id="bo">0</b></span>
  <button id="dl">Download CSV</button>
  <button id="exp">Expand all</button>
  <button id="col">Collapse all</button>
  <span class="stat" id="saved" style="color:GrayText"></span>
</header>
<div class="wrap">
<details class="help"><summary>Counting conventions (read me)</summary>
<p>Count what <b>THIS entry does</b>, not the cumulative state — and count
<b>all</b> actions regardless of significance (major/minor). <b>A deadline is a
deadline</b>: count every filing deadline whether it's substantive (would show on
the calendar) or procedural/minor (wouldn't) — redaction-request, response,
status-report, and housekeeping deadlines all count; likewise count every
hearing. One entry often has several non-zero counts: a minute entry can record a
hearing <b>held</b> AND schedule the next one AND set deadlines. A
<b>continuance is a reschedule</b> (=1), never cancel+schedule. <b>Cancel</b> is
ONLY an explicit cancellation/vacatur with no new date. A minute entry that
records or discusses a proceeding / held hearing is <b>held</b> (+1). Tick
<b>bad OCR</b> when the source text is unreadable so neither model nor human could
fairly extract — those entries are set aside in scoring, not counted against any
model.</p>
<table>
<tr><td>H sched</td><td>a NEW hearing this entry sets</td></tr>
<tr><td>H resched</td><td>an existing hearing moved to a new date/time (a continuance counts here)</td></tr>
<tr><td>H held</td><td>a minute entry recording / discussing a proceeding or held hearing → +1 ("Minute Entry for proceedings held …", "… held on …")</td></tr>
<tr><td>H canc</td><td>an EXPLICIT cancellation / vacatur with NO replacement date (a continuance is a reschedule, not a cancel)</td></tr>
<tr><td>D set</td><td>a NEW filing deadline this entry sets</td></tr>
<tr><td>D resched</td><td>an existing deadline moved to a new date</td></tr>
<tr><td>D met/filed</td><td>the filing the deadline required was made / deadline satisfied</td></tr>
<tr><td>D canc</td><td>a deadline cancelled / withdrawn / mooted (with no new date)</td></tr>
</table></details>
<div id="root"></div>
</div>
<script>
const DATA = __DATA__;
const CATS = __CATS__;
const KEY = "ccscore:benchmark";
const saved = JSON.parse(localStorage.getItem(KEY) || "{}");
const allEntries = [];
for(const c of DATA.cases) for(const e of c.entries) allEntries.push(e);

function persist(){ localStorage.setItem(KEY, JSON.stringify(saved));
  const el=document.getElementById("saved"); el.textContent="saved"; setTimeout(()=>el.textContent="",800); }

function recount(){
  let rv=0,nz=0,bo=0;
  for(const e of allEntries){ const s=saved[e.entry_id]||{};
    if(s.reviewed) rv++; if(s.bad_ocr) bo++;
    if(CATS.some(c=>(+s[c[0]]||0)>0)) nz++; }
  document.getElementById("rv").textContent=rv;
  document.getElementById("nz").textContent=nz;
  document.getElementById("bo").textContent=bo;
  for(const c of DATA.cases){ let r=0; for(const e of c.entries){ if((saved[e.entry_id]||{}).reviewed) r++; }
    const el=document.getElementById("cv-"+c.case_id); if(el) el.textContent=r+"/"+c.entries.length; }
}

function makeCard(e){
  const s = saved[e.entry_id] || (saved[e.entry_id]={});
  const card=document.createElement("div"); card.className="card";
  if(s.reviewed) card.classList.add("reviewed");
  if(s.bad_ocr) card.classList.add("badocr");

  const meta=document.createElement("div"); meta.className="meta";
  const num=document.createElement("span"); num.className="num";
  num.textContent = (e.entry_number!=null? "#"+e.entry_number : "(paperless)"); meta.appendChild(num);
  const dt=document.createElement("span"); dt.textContent=e.date_filed; meta.appendChild(dt);
  if(e.cl_url){ const a=document.createElement("a"); a.href=e.cl_url; a.target="_blank";
    a.rel="noopener"; a.textContent="docket ↗"; meta.appendChild(a); }
  const docs=document.createElement("span"); docs.className="docs";
  for(const d of e.docs){
    if(d.url){ const a=document.createElement("a"); a.href=d.url; a.target="_blank";
      a.rel="noopener"; a.textContent="doc "+d.label+" ↗"; docs.appendChild(a); }
    else { const s2=document.createElement("span"); s2.className="nolink";
      s2.textContent="doc "+d.label+" ("+(d.status||"no link")+")"; docs.appendChild(s2); } }
  meta.appendChild(docs); card.appendChild(meta);

  const desc=document.createElement("div"); desc.className="txt";
  desc.textContent = e.description || "(no description text)"; card.appendChild(desc);
  if(e.doc_text){
    const det=document.createElement("details"); det.className="dt";
    const sm=document.createElement("summary");
    sm.textContent="extracted document text ("+e.doc_text.length+" chars) — check OCR here";
    det.appendChild(sm);
    const dtx=document.createElement("div"); dtx.className="txt"; dtx.textContent=e.doc_text;
    det.appendChild(dtx); card.appendChild(det); }

  const ctr=document.createElement("div"); ctr.className="controls";
  function grp(cls, cats){
    const g=document.createElement("div"); g.className="grp "+cls;
    for(const [k,lab] of cats){
      const l=document.createElement("label"); l.textContent=lab;
      const inp=document.createElement("input"); inp.type="number"; inp.min=0;
      inp.value=(s[k]!=null? s[k]:0);
      inp.addEventListener("input",()=>{ s[k]=+inp.value||0;
        if(s[k]>0 && !s.reviewed){ s.reviewed=true; card.classList.add("reviewed"); }
        persist(); recount(); });
      l.appendChild(inp); g.appendChild(l); }
    return g; }
  ctr.appendChild(grp("h", CATS.slice(0,4)));
  ctr.appendChild(grp("d", CATS.slice(4)));
  const flags=document.createElement("div"); flags.className="flags";
  const rl=document.createElement("label"); const rc=document.createElement("input");
  rc.type="checkbox"; rc.checked=!!s.reviewed;
  rc.addEventListener("change",()=>{ s.reviewed=rc.checked;
    card.classList.toggle("reviewed",rc.checked); persist(); recount(); });
  rl.appendChild(rc); rl.appendChild(document.createTextNode(" reviewed")); flags.appendChild(rl);
  const bl=document.createElement("label"); const bc=document.createElement("input");
  bc.type="checkbox"; bc.checked=!!s.bad_ocr;
  bc.addEventListener("change",()=>{ s.bad_ocr=bc.checked;
    card.classList.toggle("badocr",bc.checked); persist(); recount(); });
  bl.appendChild(bc); bl.appendChild(document.createTextNode(" bad OCR / unreadable")); flags.appendChild(bl);
  ctr.appendChild(flags); card.appendChild(ctr);
  return card;
}

function renderCase(c, body){
  let lastSection=null;
  for(const e of c.entries){
    const sect=e.docket_number+" ("+e.court+")";
    if(sect!==lastSection){ lastSection=sect;
      const h=document.createElement("h3"); h.className="section"; h.textContent=sect; body.appendChild(h); }
    body.appendChild(makeCard(e)); }
}

const root=document.getElementById("root");
const dets=[];
for(const c of DATA.cases){
  const det=document.createElement("details"); det.className="case"; dets.push(det);
  const sum=document.createElement("summary");
  sum.appendChild(document.createTextNode(c.name+" — reviewed "));
  const cv=document.createElement("b"); cv.id="cv-"+c.case_id; cv.textContent="0"; sum.appendChild(cv);
  sum.appendChild(document.createTextNode("/"+c.entries.length+" entries"));
  det.appendChild(sum);
  const body=document.createElement("div"); body.className="casebody"; det.appendChild(body);
  let rendered=false;
  det.addEventListener("toggle",()=>{ if(det.open && !rendered){ rendered=true; renderCase(c, body); } });
  root.appendChild(det);
}

document.getElementById("exp").addEventListener("click",()=>dets.forEach(d=>d.open=true));
document.getElementById("col").addEventListener("click",()=>dets.forEach(d=>d.open=false));
document.getElementById("dl").addEventListener("click",()=>{
  const cols=["case_id","docket_number","court","docket_id","entry_id","entry_number",
    "date_filed","reviewed","bad_ocr",...CATS.map(c=>c[0])];
  const lines=[cols.join(",")];
  for(const e of allEntries){ const s=saved[e.entry_id]||{};
    const row=[e.case_id,e.docket_number,e.court,e.docket_id,e.entry_id,
      (e.entry_number==null?"":e.entry_number),e.date_filed, s.reviewed?1:0, s.bad_ocr?1:0,
      ...CATS.map(c=>(+s[c[0]]||0))];
    lines.push(row.map(v=>{ const str=String(v);
      return /[",\n]/.test(str)? '"'+str.replace(/"/g,'""')+'"':str; }).join(",")); }
  const blob=new Blob([lines.join("\n")+"\n"],{type:"text/csv"});
  const a=document.createElement("a"); a.href=URL.createObjectURL(blob);
  a.download="ground_truth.csv"; a.click();
});

recount();
</script></body></html>
"""


def render_page(cases_data: list[dict[str, Any]]) -> str:
    data = {"cases": cases_data}
    # Escape "</" so a literal "</script>" in untrusted docket text can't close
    # the embedded <script> block early. "<\/" is the same string in JS and
    # decodes cleanly under JSON.parse.
    blob = json.dumps(data).replace("</", "<\\/")
    return _PAGE.replace("__DATA__", blob).replace("__CATS__", json.dumps(CATEGORIES))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", default="config.benchmark.yaml")
    ap.add_argument("--store", default=_DEFAULT_STORE)
    ap.add_argument("--out", default=_DEFAULT_OUT)
    ap.add_argument("--case", action="append", help="only this case id (repeatable)")
    args = ap.parse_args(argv)

    store = Path(args.store)
    if not store.exists():
        raise SystemExit(f"store not found: {store} — run fetch_complete_benchmark.py")

    cfg = _load_config(args.config)
    cases = _cases_from_config(cfg)
    if args.case:
        want = set(args.case)
        cases = [c for c in cases if c.case_id in want]
        if not cases:
            raise SystemExit(f"no matching cases for {sorted(want)}")

    con = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cases_data: list[dict[str, Any]] = []
    total = 0
    try:
        for case in cases:
            entries = build_case_entries(con, case)
            cases_data.append(
                {"case_id": case.case_id, "name": case.name, "entries": entries}
            )
            total += len(entries)
            print(f"  {case.case_id}: {len(entries)} entries")
    finally:
        con.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_page(cases_data), encoding="utf-8")
    mb = out.stat().st_size / 1_000_000
    print(
        f"wrote {out} ({mb:.1f} MB, {len(cases_data)} cases, {total} entries) — "
        "open in a browser, score, Download CSV"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
