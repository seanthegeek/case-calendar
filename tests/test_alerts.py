"""Tests for ``case_calendar.alerts.ensure_docket_alerts``.

These exercise the reconcile-against-existing flow against a
``FakeCourtListener`` that records calls and lets each test seed
existing subscriptions.
"""

from __future__ import annotations

import pytest

from case_calendar.alerts import ensure_docket_alerts

from .conftest import FakeCourtListener


class TestEnsureDocketAlerts:
    def test_empty_docket_list_makes_no_calls(self):
        cl = FakeCourtListener()
        status = ensure_docket_alerts(cl, [])
        assert status == {}
        assert cl.calls == []

    def test_all_dockets_already_subscribed_no_creates(self):
        cl = FakeCourtListener(
            existing_alerts=[
                {"docket": 100, "alert_type": 1},
                {"docket": 200, "alert_type": 1},
            ]
        )
        status = ensure_docket_alerts(cl, [100, 200])
        assert status == {100: "exists", 200: "exists"}
        # One list call, zero create calls.
        assert ("list_alerts", None) in cl.calls
        assert not any(c[0] == "create_alert" for c in cl.calls)

    def test_creates_subscriptions_for_missing_dockets(self):
        cl = FakeCourtListener(existing_alerts=[{"docket": 100, "alert_type": 1}])
        status = ensure_docket_alerts(cl, [100, 200, 300])
        assert status == {100: "exists", 200: "created", 300: "created"}
        create_calls = [c for c in cl.calls if c[0] == "create_alert"]
        assert [c[1] for c in create_calls] == [200, 300]

    def test_unsubscribed_alert_type_is_treated_as_missing(self):
        # CourtListener stores alert_type=0 as "unsubscribed". The
        # reconciler must NOT treat an unsubscribed row as a live
        # subscription — otherwise an operator who manually
        # unsubscribed once would never get re-subscribed even though
        # they re-added the docket to config.yaml.
        cl = FakeCourtListener(
            existing_alerts=[
                {"docket": 100, "alert_type": 0},  # unsubscribed
            ]
        )
        status = ensure_docket_alerts(cl, [100])
        assert status == {100: "created"}

    def test_alert_without_docket_field_is_ignored(self):
        # Malformed rows (missing the docket id) shouldn't crash the
        # reconciler. They're dropped silently — the configured docket
        # gets a fresh subscription regardless.
        cl = FakeCourtListener(
            existing_alerts=[
                {"alert_type": 1},  # no docket key
                {"docket": 100, "alert_type": 1},
            ]
        )
        status = ensure_docket_alerts(cl, [100, 200])
        assert status == {100: "exists", 200: "created"}

    def test_create_failure_is_logged_and_does_not_abort(self, caplog):
        class _PartialFailureCourtListener(FakeCourtListener):
            def create_docket_alert(self, docket_id, *, alert_type=1):
                if docket_id == 200:
                    raise RuntimeError("boom — 4xx from CourtListener")
                return super().create_docket_alert(docket_id, alert_type=alert_type)

        cl = _PartialFailureCourtListener()
        with caplog.at_level("WARNING", logger="case_calendar.alerts"):
            status = ensure_docket_alerts(cl, [100, 200, 300])
        # 100 and 300 created successfully; 200 marked failed.
        assert status == {100: "created", 200: "failed", 300: "created"}
        assert any(
            "failed to create subscription for docket 200" in r.message
            for r in caplog.records
        )

    def test_list_failure_returns_all_failed_and_skips_creates(self, caplog):
        class _ListFailureCourtListener(FakeCourtListener):
            def iter_docket_alerts(self, **_):
                raise RuntimeError("transport budget exhausted")

            def create_docket_alert(self, *_a, **_kw):
                raise AssertionError(
                    "creates must not run when list_alerts failed — we'd "
                    "either spam duplicates or skip blindly"
                )

        cl = _ListFailureCourtListener()
        with caplog.at_level("WARNING", logger="case_calendar.alerts"):
            status = ensure_docket_alerts(cl, [100, 200])
        assert status == {100: "failed", 200: "failed"}
        assert any("list call failed" in r.message for r in caplog.records)
        # Transport / unexpected category — no .response on the exception.
        assert any(
            "transport / unexpected error" in r.message for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_list_failure_with_401_logs_auth_category(self, caplog):
        # When the list call fails with an exception that carries a
        # .response.status_code of 401 or 403, the log should call it
        # out as an auth error (operator needs to check the token /
        # scope) rather than the generic transport classification.
        class _Resp:
            status_code: int = 0

        class _AuthError(Exception):
            def __init__(self, status_code: int):
                super().__init__(f"HTTP {status_code}")
                self.response = _Resp()
                self.response.status_code = status_code

        class _AuthFailureCL(FakeCourtListener):
            def iter_docket_alerts(self, **_):
                raise _AuthError(401)

        with caplog.at_level("WARNING", logger="case_calendar.alerts"):
            status = ensure_docket_alerts(_AuthFailureCL(), [42])
        assert status == {42: "failed"}
        assert any(
            "auth error (HTTP 401)" in r.message and "COURTLISTENER_TOKEN" in r.message
            for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_list_failure_with_500_logs_generic_http_category(self, caplog):
        # Non-auth HTTP statuses (5xx, 422, etc.) get the generic
        # "HTTP {n} from CourtListener" classification — distinct from
        # both the auth category and the no-response-attached transport
        # category.
        class _Resp:
            status_code: int = 500

        class _ServerError(Exception):
            def __init__(self):
                super().__init__("HTTP 500")
                self.response = _Resp()

        class _Failing(FakeCourtListener):
            def iter_docket_alerts(self, **_):
                raise _ServerError()

        with caplog.at_level("WARNING", logger="case_calendar.alerts"):
            status = ensure_docket_alerts(_Failing(), [42])
        assert status == {42: "failed"}
        assert any(
            "HTTP 500 from CourtListener" in r.message for r in caplog.records
        ), [r.message for r in caplog.records]


class TestCourtListenerClientAlerts:
    """Direct tests for the new CourtListener client methods using ``httpx.MockTransport``.

    These verify the wire shape — URL, method, JSON body — without
    leaning on ``FakeCourtListener`` (which short-circuits before the
    real ``_request`` retry loop).
    """

    def test_iter_docket_alerts_follows_pagination(self):
        import httpx

        from case_calendar.courtlistener import API_BASE, CourtListener

        def handler(request: httpx.Request) -> httpx.Response:
            if "page=2" in request.url.query.decode():
                return httpx.Response(
                    200,
                    json={
                        "next": None,
                        "results": [{"docket": 102, "alert_type": 1}],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "next": f"{API_BASE}/docket-alerts/?page=2",
                    "results": [
                        {"docket": 100, "alert_type": 1},
                        {"docket": 101, "alert_type": 0},
                    ],
                },
            )

        cl = CourtListener(token="t")
        cl.client = httpx.Client(
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Token t"},
        )
        try:
            alerts = list(cl.iter_docket_alerts())
        finally:
            cl.close()
        assert [a["docket"] for a in alerts] == [100, 101, 102]

    def test_create_docket_alert_posts_expected_body(self):
        import httpx

        from case_calendar.courtlistener import API_BASE, CourtListener

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["body"] = request.content.decode()
            return httpx.Response(
                201,
                json={"id": 7, "docket": 100, "alert_type": 1},
            )

        cl = CourtListener(token="t")
        cl.client = httpx.Client(
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Token t"},
        )
        try:
            result = cl.create_docket_alert(100)
        finally:
            cl.close()
        assert result == {"id": 7, "docket": 100, "alert_type": 1}
        assert captured["method"] == "POST"
        assert captured["url"] == f"{API_BASE}/docket-alerts/"
        import json

        assert json.loads(captured["body"]) == {"docket": 100, "alert_type": 1}

    def test_post_retries_on_429(self, monkeypatch):
        # Same retry shape as _get — covered via the shared _request
        # method. One 429 + Retry-After, then success.
        import httpx

        from case_calendar.courtlistener import CourtListener

        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(201, json={"id": 1})

        cl = CourtListener(token="t")
        cl.client = httpx.Client(
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Token t"},
        )
        try:
            result = cl.create_docket_alert(100)
        finally:
            cl.close()
        assert result == {"id": 1}
        assert calls["n"] == 2


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Skip the real sleeps inside the CourtListener client's retry loop
    so tests don't burn wall-clock seconds when exercising the retry path."""
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)
