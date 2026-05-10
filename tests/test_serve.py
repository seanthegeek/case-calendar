"""Webhook receiver integration tests.

Brings up an actual HTTP server on an ephemeral port and posts JSON to it.
The CourtListener client and LLM extractor are stubbed.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Iterator

import pytest

from case_calendar import llm as llm_mod
from case_calendar.serve import WebhookServer
from case_calendar.store import Store
from case_calendar.sync import CaseConfig

from .conftest import FakeCL


@pytest.fixture
def case():
    return CaseConfig(
        case_id="us-v-x", name="United States v. X",
        dockets=[100], calendar="cyber",
    )


def _make_cl() -> FakeCL:
    return FakeCL(
        dockets={100: {
            "id": 100, "court_id": "mad",
            "docket_number": "1:25-cr-00001-X",
            "case_name": "US v. X",
            "absolute_url": "/docket/100/x/",
            "date_modified": "2026-05-08T11:00:00-07:00",
        }},
        courts={"mad": {"citation_string": "D. Mass.",
                        "short_name": "Massachusetts",
                        "full_name": "District of Massachusetts"}},
    )


def _start_server(*, store, case, cl, emit_fn=None):
    secret = "test-secret-please-make-it-long-enough"
    server = WebhookServer(
        ("127.0.0.1", 0), secret=secret,
        cases=[case], store=store, cl=cl, emit_fn=emit_fn,
    )
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)
    return server, f"http://127.0.0.1:{port}", secret


@pytest.fixture
def base_url(store: Store, case, monkeypatch) -> Iterator[tuple[str, str, FakeCL]]:
    """Spin up a webhook server with a controllable FakeCL backing it."""
    monkeypatch.setattr(llm_mod, "extract_actions", lambda **kw: [{
        "type": "ADD", "hearing_key": "sentencing-x",
        "hearing_type": "sentencing", "title": "Sentencing",
        "local_date": "2026-04-14", "local_time": "15:00",
        "duration_minutes": 90, "location": "Courtroom 4",
    }])

    cl = _make_cl()
    server, url, secret = _start_server(store=store, case=case, cl=cl)
    try:
        yield url, secret, cl
    finally:
        server.shutdown()
        server.server_close()


def _post(url: str, body: dict, headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    h = {"Content-Type": "application/json", "Content-Length": str(len(data))}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        r = urllib.request.urlopen(req)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read())
        except Exception:
            payload = {}
        return e.code, payload


def _docket_alert(entries: list[dict]) -> dict:
    return {
        "webhook": {"version": 2, "event_type": 1,
                    "date_created": "2026-05-08T20:00:00Z",
                    "deprecation_date": None},
        "payload": {"results": entries},
    }


def _sample_entry(eid=1, docket=100,
                  desc="Sentencing set for 4/14/2026 03:00 PM"):
    return {
        "id": eid, "docket": docket, "entry_number": eid,
        "date_filed": "2026-01-07",
        "date_modified": "2026-01-07T08:00:00-07:00",
        "description": desc, "short_description": "",
        "recap_documents": [],
    }


class TestRoutes:
    def test_health(self, base_url):
        url, _, _ = base_url
        r = urllib.request.urlopen(f"{url}/health")
        assert r.status == 200
        assert json.loads(r.read())["status"] == "ok"

    def test_unknown_path_404(self, base_url):
        url, _, _ = base_url
        status, _ = _post(f"{url}/nope", {})
        assert status == 404

    def test_wrong_secret_403(self, base_url):
        url, _, _ = base_url
        status, _ = _post(f"{url}/webhooks/case-calendar/wrong", {})
        assert status == 403


class TestDocketAlert:
    def test_valid_payload_creates_hearing(self, base_url, store):
        url, secret, cl = base_url
        status, body = _post(
            f"{url}/webhooks/case-calendar/{secret}",
            _docket_alert([_sample_entry()]),
            headers={"Idempotency-Key": "k1"},
        )
        assert status == 200
        assert body["status"] == "ok"
        assert body["handled"]["hearing_relevant"] == 1
        rows = store.get_hearings("us-v-x")
        assert len(rows) == 1
        assert rows[0]["hearing_key"] == "sentencing-x"

    def test_idempotency_replay_is_noop(self, base_url, store):
        url, secret, _ = base_url
        body = _docket_alert([_sample_entry()])
        _post(f"{url}/webhooks/case-calendar/{secret}", body,
              headers={"Idempotency-Key": "k-dup"})
        status, resp = _post(f"{url}/webhooks/case-calendar/{secret}", body,
                             headers={"Idempotency-Key": "k-dup"})
        assert status == 200
        assert resp["status"] == "duplicate"
        # Still exactly one hearing.
        assert len(store.get_hearings("us-v-x")) == 1

    def test_fingerprint_dedup_when_idempotency_changes(self, base_url, store):
        url, secret, _ = base_url
        body = _docket_alert([_sample_entry()])
        _post(f"{url}/webhooks/case-calendar/{secret}", body,
              headers={"Idempotency-Key": "k-1"})
        # Fresh idempotency key, same entry — should be a no-op via the
        # entry-fingerprint dedup in process_entry.
        status, resp = _post(f"{url}/webhooks/case-calendar/{secret}", body,
                             headers={"Idempotency-Key": "k-2"})
        assert status == 200
        assert resp["status"] == "ok"
        assert resp["handled"]["hearing_relevant"] == 0
        assert len(store.get_hearings("us-v-x")) == 1

    def test_unknown_docket_is_skipped(self, base_url, store):
        url, secret, _ = base_url
        # A docket not in our config.
        body = _docket_alert([_sample_entry(eid=42, docket=99999)])
        status, resp = _post(f"{url}/webhooks/case-calendar/{secret}", body,
                             headers={"Idempotency-Key": "k-unknown"})
        assert status == 200
        assert resp["handled"]["skipped_unknown_dockets"] == 1
        assert resp["handled"]["hearing_relevant"] == 0

    def test_invalid_json_400(self, base_url):
        url, secret, _ = base_url
        data = b"{not json"
        req = urllib.request.Request(
            f"{url}/webhooks/case-calendar/{secret}",
            data=data,
            headers={"Content-Type": "application/json",
                     "Content-Length": str(len(data))},
        )
        try:
            urllib.request.urlopen(req)
            assert False, "expected error"
        except urllib.error.HTTPError as e:
            assert e.code == 400


class TestNonDocketEvents:
    def test_search_alert_acked_but_ignored(self, base_url, store):
        url, secret, _ = base_url
        body = {"webhook": {"event_type": 2}, "payload": {}}
        status, resp = _post(
            f"{url}/webhooks/case-calendar/{secret}", body,
            headers={"Idempotency-Key": "k-search"},
        )
        assert status == 200
        assert resp["handled"]["ignored"] is True
        # No hearing rows from a search-alert payload.
        assert store.get_hearings("us-v-x") == []


class TestAutoEmit:
    """The webhook handler runs ``emit_fn`` after each successful docket
    alert so subscribers see the update without a manual ``case-calendar
    emit`` run."""

    def test_emit_fn_called_with_affected_calendar(
        self, store: Store, case, monkeypatch,
    ):
        monkeypatch.setattr(llm_mod, "extract_actions", lambda **kw: [{
            "type": "ADD", "hearing_key": "sentencing-x",
            "hearing_type": "sentencing", "title": "Sentencing",
            "local_date": "2026-04-14", "local_time": "15:00",
            "duration_minutes": 90, "location": "Courtroom 4",
        }])
        emitted: list[set[str]] = []
        cl = _make_cl()
        server, url, secret = _start_server(
            store=store, case=case, cl=cl,
            emit_fn=lambda cals: emitted.append(set(cals)),
        )
        try:
            status, resp = _post(
                f"{url}/webhooks/case-calendar/{secret}",
                _docket_alert([_sample_entry()]),
                headers={"Idempotency-Key": "k-emit-1"},
            )
        finally:
            server.shutdown()
            server.server_close()
        assert status == 200
        assert resp["handled"]["emitted_calendars"] == ["cyber"]
        assert emitted == [{"cyber"}]

    def test_emit_fn_skipped_when_nothing_relevant(
        self, store: Store, case, monkeypatch,
    ):
        # An entry that the regex pre-filter rejects shouldn't trigger an
        # emit — the calendar didn't change.
        monkeypatch.setattr(llm_mod, "extract_actions", lambda **kw: [
            {"type": "IGNORE", "reason": "stub"},
        ])
        emitted: list[set[str]] = []
        cl = _make_cl()
        server, url, secret = _start_server(
            store=store, case=case, cl=cl,
            emit_fn=lambda cals: emitted.append(set(cals)),
        )
        try:
            status, resp = _post(
                f"{url}/webhooks/case-calendar/{secret}",
                # short_description with no hearing/deadline vocabulary so
                # the pre-filter skips it before the LLM stub even runs.
                _docket_alert([_sample_entry(desc="Notice of Attorney Appearance")]),
                headers={"Idempotency-Key": "k-emit-skip"},
            )
        finally:
            server.shutdown()
            server.server_close()
        assert status == 200
        assert resp["handled"]["hearing_relevant"] == 0
        assert resp["handled"]["emitted_calendars"] == []
        assert emitted == []

    def test_emit_failure_does_not_fail_webhook(
        self, store: Store, case, monkeypatch,
    ):
        # The store is already updated by the time emit runs — a render
        # error mustn't make CL retry the delivery (which would dup-process).
        monkeypatch.setattr(llm_mod, "extract_actions", lambda **kw: [{
            "type": "ADD", "hearing_key": "sentencing-x",
            "hearing_type": "sentencing", "title": "Sentencing",
            "local_date": "2026-04-14", "local_time": "15:00",
        }])
        def boom(_cals):
            raise RuntimeError("disk full")
        cl = _make_cl()
        server, url, secret = _start_server(
            store=store, case=case, cl=cl, emit_fn=boom,
        )
        try:
            status, resp = _post(
                f"{url}/webhooks/case-calendar/{secret}",
                _docket_alert([_sample_entry()]),
                headers={"Idempotency-Key": "k-emit-boom"},
            )
        finally:
            server.shutdown()
            server.server_close()
        assert status == 200
        assert resp["handled"]["hearing_relevant"] == 1
        assert resp["handled"]["emitted_calendars"] == []
        # Hearing row still landed — we don't lose data when the renderer fails.
        assert len(store.get_hearings("us-v-x")) == 1
