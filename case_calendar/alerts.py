"""Ensure every configured docket has an active CourtListener docket-alert
subscription.

CourtListener fires DOCKET_ALERT webhooks only for dockets the
authenticated account has subscribed to via ``/api/rest/v4/docket-alerts/``.
Manually subscribing each docket in the CourtListener UI works but is
error-prone — operators forget, new cases get added to ``config.yaml``
and silently miss webhook updates until the next polling sync catches
them. This module closes that gap: ``ensure_docket_alerts`` lists the
account's existing alerts, compares against the docket IDs configured
in ``config.yaml``, and creates a subscription for any docket that
doesn't already have one.

Failure semantics are deliberately permissive — if CourtListener
returns a 4xx on a specific docket's create call (e.g. the docket
doesn't exist on CourtListener yet, or the account already has a row
that didn't show up in the list paginator), we log a warning and
continue. The polling sync path still works without webhook alerts;
this feature is about reducing manual setup, not adding a hard
dependency.

The whole flow is gated on the top-level ``ensure_docket_alerts``
config flag (default true). Operators who configure their alerts via
some other surface — bulk CSV upload to CourtListener, a separate
admin tool — set the flag to false to opt out.
"""

from __future__ import annotations

import logging
from typing import Iterable

from .courtlistener import CourtListener

log = logging.getLogger(__name__)

# CourtListener alert_type values. Only ``SUBSCRIBED`` is created by
# this module; ``UNSUBSCRIBED`` rows are treated as "no active
# subscription" when reconciling against the configured set.
_ALERT_TYPE_SUBSCRIBED = 1


def ensure_docket_alerts(
    cl: CourtListener, docket_ids: Iterable[int]
) -> dict[int, str]:
    """Ensure each ``docket_id`` has an active subscription on CourtListener.

    Returns a status dict mapping each input docket id to one of:

    * ``"exists"`` — an active subscription was already in place; no
      POST was issued.
    * ``"created"`` — a new subscription was created via POST.
    * ``"failed"`` — the create call raised; logged at WARNING with
      the exception type and message. The caller's loop (sync / serve)
      continues — webhook setup is auxiliary, not load-bearing.

    Listing existing alerts is one paginated GET per ~100 alerts; create
    calls are one POST per missing docket. Both run under the
    ``CourtListener`` client's shared retry / rate-limit machinery, so a
    burst of new dockets won't trip the 300/day bucket without being
    visible in the log.
    """
    docket_ids = list(docket_ids)
    if not docket_ids:
        return {}

    try:
        existing = {
            int(alert["docket"])
            for alert in cl.iter_docket_alerts()
            if int(alert.get("alert_type", 0)) == _ALERT_TYPE_SUBSCRIBED
            and alert.get("docket") is not None
        }
    except Exception as e:
        # Listing failed entirely. Don't half-attempt creates against an
        # unknown baseline — that would either spam duplicate POSTs or
        # skip everything if the caller's account already has subscriptions
        # we couldn't see. Distinguish the failure shape in the log so the
        # operator knows which way to investigate:
        #   - 401/403 from CourtListener (HTTPStatusError with that
        #     status) → check ``COURTLISTENER_TOKEN`` env var, account
        #     status, and that the token has the docket-alerts scope.
        #   - Transport errors (httpx network-layer) → CourtListener may
        #     be degraded; the next sync will retry.
        #   - Anything else → unexpected; the exception type tells the
        #     story.
        status_code = getattr(getattr(e, "response", None), "status_code", None)
        if status_code in (401, 403):
            category = (
                f"auth error (HTTP {status_code}) — check "
                "COURTLISTENER_TOKEN and the account's docket-alerts scope"
            )
        elif status_code is not None:
            category = f"HTTP {status_code} from CourtListener"
        else:
            category = (
                f"transport / unexpected error ({type(e).__name__}) — "
                "likely transient, next sync will retry"
            )
        log.warning(
            "ensure_docket_alerts: list call failed: %s: %s; skipping "
            "alert reconciliation this run (%s)",
            type(e).__name__,
            e,
            category,
        )
        return {did: "failed" for did in docket_ids}

    status: dict[int, str] = {}
    for did in docket_ids:
        if did in existing:
            status[did] = "exists"
            continue
        try:
            cl.create_docket_alert(did)
            status[did] = "created"
            log.info("ensure_docket_alerts: created subscription for docket %s", did)
        except Exception as e:
            status[did] = "failed"
            log.warning(
                "ensure_docket_alerts: failed to create subscription for docket %s: %s",
                did,
                e,
            )
    return status
