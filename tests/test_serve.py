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

from .conftest import FakeCourtListener


@pytest.fixture
def case():
    return CaseConfig(
        case_id="us-v-x",
        name="United States v. X",
        dockets=[100],
        calendar="cyber",
    )


def _make_cl() -> FakeCourtListener:
    return FakeCourtListener(
        dockets={
            100: {
                "id": 100,
                "court_id": "mad",
                "docket_number": "1:25-cr-00001-X",
                "case_name": "US v. X",
                "absolute_url": "/docket/100/x/",
                "date_modified": "2026-05-08T11:00:00-07:00",
            }
        },
        courts={
            "mad": {
                "citation_string": "D. Mass.",
                "short_name": "Massachusetts",
                "full_name": "District of Massachusetts",
            }
        },
    )


def _start_server(*, store, case, cl, emit_fn=None):
    secret = "test-secret-please-make-it-long-enough"
    server = WebhookServer(
        ("127.0.0.1", 0),
        secret=secret,
        cases=[case],
        store=store,
        cl=cl,
        emit_fn=emit_fn,
    )
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)
    return server, f"http://127.0.0.1:{port}", secret


@pytest.fixture
def base_url(
    store: Store, case, monkeypatch
) -> Iterator[tuple[str, str, FakeCourtListener]]:
    """Spin up a webhook server with a controllable FakeCourtListener backing it."""
    monkeypatch.setattr(
        llm_mod,
        "extract_actions",
        lambda **kw: [
            {
                "type": "ADD_HEARING",
                "hearing_key": "sentencing-x",
                "hearing_type": "sentencing",
                "title": "Sentencing",
                "local_date": "2026-04-14",
                "local_time": "15:00",
                "duration_minutes": 90,
                "location": "Courtroom 4",
            }
        ],
    )

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
        "webhook": {
            "version": 2,
            "event_type": 1,
            "date_created": "2026-05-08T20:00:00Z",
            "deprecation_date": None,
        },
        "payload": {"results": entries},
    }


def _sample_entry(eid=1, docket=100, desc="Sentencing set for 4/14/2026 03:00 PM"):
    return {
        "id": eid,
        "docket": docket,
        "entry_number": eid,
        "date_filed": "2026-01-07",
        "date_modified": "2026-01-07T08:00:00-07:00",
        "description": desc,
        "short_description": "",
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

    def test_gated_health_ok(self, base_url):
        url, secret, _ = base_url
        r = urllib.request.urlopen(f"{url}/webhooks/case-calendar/{secret}/health")
        assert r.status == 200
        body = json.loads(r.read())
        assert body == {
            "status": "ok",
            "service": "case-calendar",
            "tracking": {"dockets": 1, "cases": 1},
        }

    def test_gated_health_wrong_secret_403(self, base_url):
        url, _, _ = base_url
        try:
            urllib.request.urlopen(f"{url}/webhooks/case-calendar/wrong-secret/health")
            assert False, "expected 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

    def test_gated_health_unknown_suffix_404(self, base_url):
        url, secret, _ = base_url
        try:
            urllib.request.urlopen(f"{url}/webhooks/case-calendar/{secret}/nope")
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404


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

    def test_processing_bumps_docket_short_circuit_watermark(
        self,
        base_url,
        store,
    ):
        # dockets.date_modified is the polling short-circuit cutoff:
        # if a webhook never advances it, a follow-up poll would still
        # short-circuit the docket as "unchanged since last poll" even
        # though new entries arrived via webhook.
        url, secret, _ = base_url
        _post(
            f"{url}/webhooks/case-calendar/{secret}",
            _docket_alert([_sample_entry()]),
            headers={"Idempotency-Key": "k-bump"},
        )
        assert store.docket_last_modified(100) == "2026-01-07T08:00:00-07:00"

    def test_processing_bumps_date_last_filing_from_entry(
        self,
        base_url,
        store,
    ):
        # The index page's "Last filing" column reads from
        # dockets.date_last_filing. Webhook deliveries don't refetch the
        # parent docket, so the bump in process_entry has to use the
        # entry's own date_filed as a forward-only stand-in; otherwise
        # webhook-only deployments would show stale filing dates.
        url, secret, _ = base_url
        _post(
            f"{url}/webhooks/case-calendar/{secret}",
            _docket_alert([_sample_entry()]),
            headers={"Idempotency-Key": "k-last-filing"},
        )
        assert store.get_docket_meta(100)["date_last_filing"] == "2026-01-07"

    def test_idempotency_replay_is_noop(self, base_url, store):
        url, secret, _ = base_url
        body = _docket_alert([_sample_entry()])
        _post(
            f"{url}/webhooks/case-calendar/{secret}",
            body,
            headers={"Idempotency-Key": "k-dup"},
        )
        status, resp = _post(
            f"{url}/webhooks/case-calendar/{secret}",
            body,
            headers={"Idempotency-Key": "k-dup"},
        )
        assert status == 200
        assert resp["status"] == "duplicate"
        # Still exactly one hearing.
        assert len(store.get_hearings("us-v-x")) == 1

    def test_fingerprint_dedup_when_idempotency_changes(self, base_url, store):
        url, secret, _ = base_url
        body = _docket_alert([_sample_entry()])
        _post(
            f"{url}/webhooks/case-calendar/{secret}",
            body,
            headers={"Idempotency-Key": "k-1"},
        )
        # Fresh idempotency key, same entry — should be a no-op via the
        # entry-fingerprint dedup in process_entry.
        status, resp = _post(
            f"{url}/webhooks/case-calendar/{secret}",
            body,
            headers={"Idempotency-Key": "k-2"},
        )
        assert status == 200
        assert resp["status"] == "ok"
        assert resp["handled"]["hearing_relevant"] == 0
        assert len(store.get_hearings("us-v-x")) == 1

    def test_unknown_docket_is_skipped(self, base_url, store):
        url, secret, _ = base_url
        # A docket not in our config.
        body = _docket_alert([_sample_entry(eid=42, docket=99999)])
        status, resp = _post(
            f"{url}/webhooks/case-calendar/{secret}",
            body,
            headers={"Idempotency-Key": "k-unknown"},
        )
        assert status == 200
        assert resp["handled"]["skipped_unknown_dockets"] == 1
        assert resp["handled"]["hearing_relevant"] == 0

    def test_invalid_json_400(self, base_url):
        url, secret, _ = base_url
        data = b"{not json"
        req = urllib.request.Request(
            f"{url}/webhooks/case-calendar/{secret}",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(data)),
            },
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
            f"{url}/webhooks/case-calendar/{secret}",
            body,
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
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        monkeypatch.setattr(
            llm_mod,
            "extract_actions",
            lambda **kw: [
                {
                    "type": "ADD_HEARING",
                    "hearing_key": "sentencing-x",
                    "hearing_type": "sentencing",
                    "title": "Sentencing",
                    "local_date": "2026-04-14",
                    "local_time": "15:00",
                    "duration_minutes": 90,
                    "location": "Courtroom 4",
                }
            ],
        )
        emitted: list[set[str]] = []
        cl = _make_cl()
        server, url, secret = _start_server(
            store=store,
            case=case,
            cl=cl,
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
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # An entry that the regex pre-filter rejects shouldn't trigger an
        # emit — the calendar didn't change.
        monkeypatch.setattr(
            llm_mod,
            "extract_actions",
            lambda **kw: [
                {"type": "IGNORE", "reason": "stub"},
            ],
        )
        emitted: list[set[str]] = []
        cl = _make_cl()
        server, url, secret = _start_server(
            store=store,
            case=case,
            cl=cl,
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
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # The store is already updated by the time emit runs — a render
        # error mustn't make CourtListener retry the delivery (which would dup-process).
        monkeypatch.setattr(
            llm_mod,
            "extract_actions",
            lambda **kw: [
                {
                    "type": "ADD_HEARING",
                    "hearing_key": "sentencing-x",
                    "hearing_type": "sentencing",
                    "title": "Sentencing",
                    "local_date": "2026-04-14",
                    "local_time": "15:00",
                }
            ],
        )

        def boom(_cals):
            raise RuntimeError("disk full")

        cl = _make_cl()
        server, url, secret = _start_server(
            store=store,
            case=case,
            cl=cl,
            emit_fn=boom,
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


class TestRequestErrors:
    """Coverage for the edge-case responses in WebhookHandler._read_body /
    do_GET / do_POST."""

    def test_get_unknown_path_404(self, base_url):
        url, _, _ = base_url
        try:
            urllib.request.urlopen(f"{url}/no-such-path")
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404

    def test_missing_content_length_411(self, base_url):
        url, secret, _ = base_url
        # Forge a request that omits Content-Length. Hard with urllib
        # (it auto-adds it), so drop to a raw socket.
        import socket
        from urllib.parse import urlparse

        parsed = urlparse(url)
        with socket.create_connection((parsed.hostname, parsed.port)) as s:
            s.sendall(
                f"POST /webhooks/case-calendar/{secret} HTTP/1.1\r\n"
                f"Host: {parsed.hostname}:{parsed.port}\r\n"
                f"Connection: close\r\n"
                f"\r\n".encode()
            )
            status_line = s.makefile("rb").readline().decode()
        assert "411" in status_line

    def test_bad_content_length_400(self, base_url):
        url, secret, _ = base_url
        req = urllib.request.Request(
            f"{url}/webhooks/case-calendar/{secret}",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "Content-Length": "not-a-number",
            },
        )
        try:
            urllib.request.urlopen(req)
            assert False, "expected 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400

    def test_oversized_payload_413(self, base_url):
        url, secret, _ = base_url
        # Claim a 10MB length without actually sending the bytes — the
        # server should refuse before reading anything.
        import socket
        from urllib.parse import urlparse

        parsed = urlparse(url)
        with socket.create_connection((parsed.hostname, parsed.port)) as s:
            s.sendall(
                f"POST /webhooks/case-calendar/{secret} HTTP/1.1\r\n"
                f"Host: {parsed.hostname}:{parsed.port}\r\n"
                f"Content-Length: 10000000\r\n"
                f"Connection: close\r\n"
                f"\r\n".encode()
            )
            status_line = s.makefile("rb").readline().decode()
        assert "413" in status_line

    def test_entry_with_non_integer_docket_id_skipped(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # A payload whose entry's docket field isn't coercible to int is
        # logged and skipped rather than crashing the handler.
        monkeypatch.setattr(llm_mod, "extract_actions", lambda **kw: [])
        cl = _make_cl()
        server, url, secret = _start_server(store=store, case=case, cl=cl)
        try:
            entry = _sample_entry()
            entry["docket"] = "not-a-docket"
            status, resp = _post(
                f"{url}/webhooks/case-calendar/{secret}",
                _docket_alert([entry]),
                headers={"Idempotency-Key": "k-bad-docket"},
            )
        finally:
            server.shutdown()
            server.server_close()
        assert status == 200
        assert resp["handled"]["entries_processed"] == 0

    def test_docket_id_as_digit_string_is_coerced(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # CourtListener sometimes ships the docket field as a string; the handler
        # coerces it to int so the lookup still hits docket_to_case.
        monkeypatch.setattr(
            llm_mod,
            "extract_actions",
            lambda **kw: [
                {"type": "IGNORE", "reason": "stub"},
            ],
        )
        cl = _make_cl()
        server, url, secret = _start_server(store=store, case=case, cl=cl)
        try:
            entry = _sample_entry()
            entry["docket"] = "100"  # digit string
            status, resp = _post(
                f"{url}/webhooks/case-calendar/{secret}",
                _docket_alert([entry]),
                headers={"Idempotency-Key": "k-string-docket"},
            )
        finally:
            server.shutdown()
            server.server_close()
        assert status == 200
        assert resp["handled"]["entries_processed"] == 1

    def test_process_entry_exception_continues(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # If processing one entry crashes, the handler logs and moves to the
        # next entry — never bubbles a 500 to CourtListener (which would retry).
        from case_calendar import sync as sync_mod

        original = sync_mod.CaseSyncer.process_entry

        def _flaky(self, case_, docket_id, entry, **kw):
            if entry["id"] == 1:
                raise RuntimeError("transient processing error")
            return original(self, case_, docket_id, entry, **kw)

        monkeypatch.setattr(sync_mod.CaseSyncer, "process_entry", _flaky)
        monkeypatch.setattr(
            llm_mod,
            "extract_actions",
            lambda **kw: [
                {"type": "IGNORE", "reason": "stub"},
            ],
        )

        cl = _make_cl()
        server, url, secret = _start_server(store=store, case=case, cl=cl)
        try:
            status, resp = _post(
                f"{url}/webhooks/case-calendar/{secret}",
                _docket_alert([_sample_entry(eid=1), _sample_entry(eid=2)]),
                headers={"Idempotency-Key": "k-flaky"},
            )
        finally:
            server.shutdown()
            server.server_close()
        # First entry raised; second went through. The 200 ack is the
        # contract — we don't want CourtListener re-delivering the whole batch.
        assert status == 200
        assert resp["handled"]["entries_processed"] == 1


class TestServerWide500Handler:
    def test_process_locked_exception_returns_500(
        self,
        store: Store,
        case,
        monkeypatch,
    ):
        # An unexpected error inside process_locked (e.g. lock acquisition
        # or store write) becomes a 500 — CourtListener retries, and the next attempt
        # benefits from idempotency-key dedup if applicable.
        monkeypatch.setattr(llm_mod, "extract_actions", lambda **kw: [])

        from case_calendar import serve as serve_mod

        def _boom(self, *a, **kw):
            raise RuntimeError("lock subsystem down")

        monkeypatch.setattr(serve_mod.WebhookServer, "process_locked", _boom)

        cl = _make_cl()
        server, url, secret = _start_server(store=store, case=case, cl=cl)
        try:
            try:
                urllib.request.urlopen(
                    urllib.request.Request(
                        f"{url}/webhooks/case-calendar/{secret}",
                        data=json.dumps(_docket_alert([_sample_entry()])).encode(),
                        headers={"Content-Type": "application/json"},
                    )
                )
                assert False, "expected 500"
            except urllib.error.HTTPError as e:
                assert e.code == 500
        finally:
            server.shutdown()
            server.server_close()


class TestNoIdempotencyKey:
    """CourtListener always supplies Idempotency-Key on real deliveries, but
    a manual curl POST (operator smoke test) might not. The receiver must
    process the entry once and not crash trying to mark a missing key."""

    def test_post_without_idempotency_key_processes_normally(self, base_url, store):
        url, secret, _ = base_url
        body = _docket_alert([_sample_entry()])
        # Note: NO Idempotency-Key header passed in.
        status, resp = _post(f"{url}/webhooks/case-calendar/{secret}", body)
        assert status == 200
        assert resp["status"] == "ok"
        assert len(store.get_hearings("us-v-x")) == 1


class TestServeFunction:
    def test_keyboard_interrupt_shuts_down_cleanly(
        self,
        store: Store,
        case,
        tmp_path,
    ):
        # The serve() top-level function traps KeyboardInterrupt so an
        # operator's Ctrl-C is treated as a normal shutdown. Exercise that
        # by patching serve_forever to raise immediately.
        from unittest.mock import patch

        from case_calendar.serve import serve

        cl = _make_cl()
        with patch(
            "case_calendar.serve.WebhookServer.serve_forever",
            side_effect=KeyboardInterrupt,
        ):
            # No exception escapes; server_close is called in the finally.
            serve(
                host="127.0.0.1",
                port=0,
                secret="test-secret-please-make-it-long-enough",
                cases=[case],
                store=store,
                cl=cl,
            )
