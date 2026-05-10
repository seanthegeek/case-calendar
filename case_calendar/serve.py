"""HTTP receiver for CourtListener webhooks.

CourtListener pushes a ``DOCKET_ALERT`` event whenever a new filing lands on a
docket you've subscribed to via their UI. The payload contains the full
docket entry (with ``recap_documents`` inline) — the same shape as the
``/docket-entries/`` API. So a webhook-driven setup never burns daily quota
on polling that finds nothing new.

Auth model (per CL docs): no signing secret. The only protection is the
randomness of the URL itself, plus a path secret we add for belt-and-braces.
The URL must be HTTPS and the path component must be unguessable.

Each delivery includes an ``Idempotency-Key`` header. We store seen keys in
SQLite so retries from CL are no-ops. The entry-fingerprint dedup in
``process_entry`` is a second line of defense.

Run with::

    case-calendar serve --port 8000

Then put the receiver behind whatever public TLS terminator you like
(Caddy, Cloudflare Tunnel, fly.io, etc.) and register the resulting URL —
``https://<host>/webhooks/case-calendar/<CASE_CALENDAR_WEBHOOK_SECRET>`` —
in the CL dashboard with event type ``DOCKET_ALERT``.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .courtlistener import CourtListener
from .store import Store
from .sync import CaseConfig, CaseSyncer

log = logging.getLogger(__name__)

# CL uses small integer event-type codes (per their docs).
EVENT_DOCKET_ALERT = 1
EVENT_SEARCH_ALERT = 2
EVENT_RECAP_FETCH = 3

WEBHOOK_PATH_PREFIX = "/webhooks/case-calendar/"


class WebhookServer(ThreadingHTTPServer):
    """ThreadingHTTPServer carrying the syncer + secret + case index."""

    daemon_threads = True

    def __init__(
        self,
        addr: tuple[str, int],
        *,
        secret: str,
        cases: list[CaseConfig],
        store: Store,
        cl: CourtListener,
    ):
        super().__init__(addr, WebhookHandler)
        self.secret = secret
        self.store = store
        self.syncer = CaseSyncer(cl, store)
        # docket_id -> case (a docket only ever belongs to one logical case)
        self.docket_to_case: dict[int, CaseConfig] = {}
        for c in cases:
            for d in c.dockets:
                self.docket_to_case[d] = c
        # Process one webhook at a time so we don't race on the SQLite store
        # (and to keep LLM concurrency predictable).
        self._lock = threading.Lock()

    def process_locked(self, fn, *args, **kwargs):
        with self._lock:
            return fn(*args, **kwargs)


class WebhookHandler(BaseHTTPRequestHandler):
    server_version = "case-calendar/1.0"

    server: WebhookServer  # type: ignore[assignment]

    # --- noise control on the access log ---
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log.info("%s - %s", self.address_string(), format % args)

    # --- routing ---

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/health":
            self._respond(200, {"status": "ok"})
            return
        self._respond(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self.path.startswith(WEBHOOK_PATH_PREFIX):
            self._respond(404, {"error": "unknown path"})
            return
        supplied = self.path[len(WEBHOOK_PATH_PREFIX):].rstrip("/")
        if supplied != self.server.secret:
            log.warning("webhook secret mismatch from %s", self.client_address[0])
            self._respond(403, {"error": "forbidden"})
            return

        body = self._read_body()
        if body is None:
            return  # already responded
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            self._respond(400, {"error": f"invalid json: {e}"})
            return

        idem_key = self.headers.get("Idempotency-Key", "").strip()
        event_type = (data.get("webhook") or {}).get("event_type")

        # All store access happens inside the server-wide lock so multiple
        # in-flight webhook deliveries don't race on SQLite writes.
        try:
            result = self.server.process_locked(
                self._dispatch_with_idempotency, idem_key, event_type, data
            )
        except Exception:
            log.exception("webhook processing failed")
            self._respond(500, {"error": "processing error"})
            return

        self._respond(200, result)

    def _dispatch_with_idempotency(
        self, idem_key: str, event_type: Optional[int], data: dict[str, Any]
    ) -> dict[str, Any]:
        # CL retries non-2xx responses; this dedup check makes that safe.
        if idem_key and self.server.store.webhook_seen(idem_key):
            log.info("duplicate webhook %s; acking", idem_key)
            return {"status": "duplicate"}

        handled = self._dispatch(event_type, data)

        if idem_key:
            self.server.store.mark_webhook_seen(idem_key, event_type)
            with self.server.store.tx() as _:
                pass
        return {"status": "ok", "handled": handled}

    # --- handlers ---

    def _dispatch(self, event_type: Optional[int], data: dict[str, Any]) -> dict[str, Any]:
        if event_type == EVENT_DOCKET_ALERT:
            return self._handle_docket_alert(data.get("payload") or {})
        # Other event types are accepted-but-ignored for now.
        log.info("ignoring webhook event_type=%s", event_type)
        return {"event_type": event_type, "ignored": True}

    def _handle_docket_alert(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Process every entry in the payload.

        Per CL docs, payload.results contains entries shaped like the
        Docket Entry API — including ``recap_documents`` inline. ``docket``
        is an integer ID rather than a URL.
        """
        results = payload.get("results") or []
        per_case: dict[str, int] = defaultdict(int)
        skipped_unknown_dockets = 0
        processed = 0
        relevant = 0

        for entry in results:
            docket_id = entry.get("docket")
            if isinstance(docket_id, str) and docket_id.isdigit():
                docket_id = int(docket_id)
            if not isinstance(docket_id, int):
                log.warning("entry %s has no integer docket id", entry.get("id"))
                continue

            case = self.server.docket_to_case.get(docket_id)
            if not case:
                # Webhooks may fire for dockets we don't track — e.g. if the
                # user has a docket alert in CL that isn't in config.yaml.
                # Ack and move on.
                skipped_unknown_dockets += 1
                continue

            try:
                was_processed = self.server.syncer.process_entry(
                    case, docket_id, entry
                )
            except Exception:
                log.exception(
                    "process_entry failed for docket=%s entry=%s",
                    docket_id, entry.get("id"),
                )
                continue
            processed += 1
            if was_processed:
                relevant += 1
                per_case[case.case_id] += 1
            with self.server.store.tx() as _:
                pass

        return {
            "results_received": len(results),
            "entries_processed": processed,
            "hearing_relevant": relevant,
            "skipped_unknown_dockets": skipped_unknown_dockets,
            "per_case": dict(per_case),
        }

    # --- io helpers ---

    def _read_body(self) -> Optional[bytes]:
        length_hdr = self.headers.get("Content-Length")
        if not length_hdr:
            self._respond(411, {"error": "Content-Length required"})
            return None
        try:
            length = int(length_hdr)
        except ValueError:
            self._respond(400, {"error": "bad Content-Length"})
            return None
        if length > 5_000_000:
            # CL payloads are kilobytes; anything huge is suspicious.
            self._respond(413, {"error": "payload too large"})
            return None
        return self.rfile.read(length)

    def _respond(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(
    *,
    host: str,
    port: int,
    secret: str,
    cases: list[CaseConfig],
    store: Store,
    cl: CourtListener,
) -> None:
    server = WebhookServer(
        (host, port), secret=secret, cases=cases, store=store, cl=cl
    )
    log.info(
        "case-calendar webhook server listening on %s:%d "
        "(POST %s<secret> for DOCKET_ALERT)",
        host, port, WEBHOOK_PATH_PREFIX,
    )
    log.info(
        "tracking %d dockets across %d cases",
        len(server.docket_to_case), len(cases),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down webhook server")
    finally:
        server.server_close()
