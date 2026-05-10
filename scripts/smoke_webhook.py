"""Smoke test the webhook handler against a synthetic DOCKET_ALERT payload.

No CL API calls (we mock the syncer's docket-meta lookup) and no LLM calls
(we monkey-patch the extractor to return a deterministic ADD action).
This validates: route + secret check, JSON parsing, idempotency, dispatch
to process_entry, and the resulting hearing landing in the store.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import urllib.request
from pathlib import Path

# Stub the LLM before importing things that touch it.
import case_calendar.llm as llm_mod
def fake_extract(**kw):
    return [{
        "type": "ADD",
        "hearing_key": "sentencing-wang",
        "hearing_type": "sentencing",
        "title": "Sentencing",
        "local_date": "2026-04-14",
        "local_time": "11:00",
        "duration_minutes": 90,
        "location": "Courtroom 4",
        "judge": "Judge Nathaniel M. Gorton",
        "reason": "synthesized for smoke",
    }]
llm_mod.extract_actions = fake_extract

os.environ.setdefault("COURTLISTENER_TOKEN", "fake-token-for-smoke")

from case_calendar.courtlistener import CourtListener
from case_calendar.serve import WebhookServer
from case_calendar.store import Store
from case_calendar.sync import CaseConfig, CaseSyncer

# Don't actually hit CL — the syncer needs a court_id and docket meta on the
# first sighting, so prime the store and stub the network call.
class FakeCL(CourtListener):
    def __init__(self):
        self._calls = 0
    def get_docket(self, docket_id):
        self._calls += 1
        return {
            "court_id": "mad",
            "docket_number": "1:25-cr-10273-NMG",
            "case_name": "United States v. Wang",
            "absolute_url": "/docket/70678228/united-states-v-wang/",
            "date_modified": "2026-05-08T11:00:00-07:00",
        }
    def get_court(self, court_id):
        return {"citation_string": "D. Mass.", "short_name": "Massachusetts",
                "full_name": "District Court, D. Massachusetts"}
    def close(self): pass

DATA = Path("data/smoke_webhook")
if DATA.exists():
    shutil.rmtree(DATA)
store = Store(DATA / "db.sqlite")
cases = [CaseConfig(case_id="us-v-wang", name="United States v. Wang",
                    dockets=[70678228], calendar="cyber")]
secret = "test-secret-please-make-this-long-enough"

cl = FakeCL()
server = WebhookServer(("127.0.0.1", 0), secret=secret, cases=cases, store=store, cl=cl)
port = server.server_address[1]
t = threading.Thread(target=server.serve_forever, daemon=True)
t.start()
time.sleep(0.1)

base = f"http://127.0.0.1:{port}"

# --- 1. health ---
print("--- /health ---")
r = urllib.request.urlopen(f"{base}/health")
print(f"  status={r.status} body={r.read().decode()}")
assert r.status == 200

# --- 2. wrong secret ---
print("\n--- POST with wrong secret ---")
req = urllib.request.Request(
    f"{base}/webhooks/case-calendar/wrong",
    data=b"{}",
    headers={"Content-Type": "application/json", "Content-Length": "2"},
)
try:
    urllib.request.urlopen(req)
    raise SystemExit("expected 403")
except urllib.error.HTTPError as e:
    print(f"  status={e.code}")
    assert e.code == 403

# --- 3. unknown path ---
print("\n--- POST to unknown path ---")
req = urllib.request.Request(
    f"{base}/nope", data=b"{}",
    headers={"Content-Type": "application/json", "Content-Length": "2"},
)
try:
    urllib.request.urlopen(req)
    raise SystemExit("expected 404")
except urllib.error.HTTPError as e:
    print(f"  status={e.code}")
    assert e.code == 404

# --- 4. valid DOCKET_ALERT payload ---
print("\n--- POST DOCKET_ALERT ---")
payload = {
    "webhook": {
        "version": 2,
        "event_type": 1,  # DOCKET_ALERT
        "date_created": "2026-05-08T20:00:00Z",
        "deprecation_date": None,
    },
    "payload": {
        "results": [
            {
                "id": 460602652,
                "docket": 70678228,
                "entry_number": 31,
                "date_filed": "2026-01-07",
                "date_modified": "2026-01-07T08:00:00-07:00",
                "description": "Judge Nathaniel M. Gorton: ORDER entered. PROCEDURAL ORDER re sentencing hearing as to Zhenxing Wang Sentencing set for 4/14/2026 03:00 PM in Courtroom 4 (In person only) before Judge Nathaniel M. Gorton.",
                "short_description": "",
                "recap_documents": [
                    {"id": 465579840, "is_available": True, "is_sealed": None,
                     "filepath_local": "recap/.../31.pdf",
                     "filepath_ia": "https://archive.org/.../31.pdf",
                     "plain_text": "PROCEDURAL ORDER...",
                     "description": "Procedural Order re Sentencing Hearing"}
                ],
            }
        ]
    },
}
body = json.dumps(payload).encode()
req = urllib.request.Request(
    f"{base}/webhooks/case-calendar/{secret}",
    data=body,
    headers={
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "Idempotency-Key": "test-idem-key-001",
    },
)
r = urllib.request.urlopen(req)
print(f"  status={r.status} body={r.read().decode()}")
assert r.status == 200

# --- 5. idempotency: replay should be a no-op ---
print("\n--- replay same Idempotency-Key (should not reprocess) ---")
r = urllib.request.urlopen(req)
resp_body = r.read().decode()
print(f"  status={r.status} body={resp_body}")
assert r.status == 200
assert '"status": "duplicate"' in resp_body

# --- check store ---
print("\n--- Hearings in store ---")
hearings = store.get_hearings("us-v-wang")
for h in hearings:
    print(f"  [{h['status']}] {h.get('starts_at_utc')}  {h['title']}  ({h['hearing_key']})")
    print(f"     loc={h.get('location')!r} judge={h.get('judge')!r} docket_id={h.get('docket_id')}")
assert len(hearings) == 1, f"expected 1 hearing, got {len(hearings)}"
assert hearings[0]["hearing_key"] == "sentencing-wang"
assert hearings[0]["docket_id"] == 70678228
assert hearings[0]["starts_at_utc"] == "2026-04-14T15:00:00+00:00", \
    hearings[0]["starts_at_utc"]  # fake LLM returns 11:00 ET → 15:00 UTC

# --- check docket + court were cached ---
meta = store.get_docket_meta(70678228)
print(f"\n  cached docket meta: {meta}")
assert meta and meta["docket_number"] == "1:25-cr-10273-NMG"
print(f"  cached court citation: {store.get_court_citation('mad')!r}")
assert store.get_court_citation("mad") == "D. Mass."

# --- check CL was hit exactly once for the docket meta ---
print(f"\n  FakeCL.get_docket call count: {cl._calls} (expected 1)")
assert cl._calls == 1, "expected exactly 1 docket fetch (then cached)"

# --- 6. third post with NEW idempotency key, same content — should still be no-op
# because entry fingerprint dedup kicks in ---
print("\n--- POST again with NEW Idempotency-Key, same entry → fingerprint dedup ---")
req3 = urllib.request.Request(
    f"{base}/webhooks/case-calendar/{secret}",
    data=body,
    headers={
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "Idempotency-Key": "test-idem-key-002",
    },
)
r = urllib.request.urlopen(req3)
print(f"  status={r.status} body={r.read().decode()}")
print(f"  FakeCL.get_docket call count: {cl._calls} (expected still 1)")
assert cl._calls == 1
hearings_after = store.get_hearings("us-v-wang")
assert len(hearings_after) == 1, "should still be one hearing"

server.shutdown()
print("\n✓ Webhook smoke test passed.")
