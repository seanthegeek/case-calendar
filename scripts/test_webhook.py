"""One-shot helper to POST a CourtListener-shaped payload at a webhook receiver.

Usage:
    uv run python scripts/test_webhook.py <url> <payload.json>
    uv run python scripts/test_webhook.py <url> -            # read JSON from stdin

A fresh ``Idempotency-Key`` is generated per invocation so repeated runs
exercise the full pipeline rather than short-circuiting on the dedup
table. Pass ``--idempotency-key <key>`` to override (e.g. to verify the
duplicate-ack path).

Example (using the bundled fixture):
    uv run python scripts/test_webhook.py \\
        https://webhook.casecalendar.com/webhooks/case-calendar/<SECRET> \\
        scripts/test_webhook_payload.json
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import uuid
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Full webhook URL including the path secret")
    parser.add_argument(
        "payload",
        help="Path to a JSON file containing the payload, or '-' to read stdin",
    )
    parser.add_argument(
        "--idempotency-key",
        default=None,
        help="Override the Idempotency-Key header (default: random UUID)",
    )
    parser.add_argument(
        "--event-type",
        type=int,
        default=None,
        help="Override webhook.event_type (default: leave payload untouched)",
    )
    args = parser.parse_args()

    if args.payload == "-":
        body_text = sys.stdin.read()
    else:
        body_text = Path(args.payload).read_text()

    data = json.loads(body_text)
    if args.event_type is not None:
        data.setdefault("webhook", {})["event_type"] = args.event_type

    body = json.dumps(data).encode("utf-8")
    idem_key = args.idempotency_key or str(uuid.uuid4())

    req = urllib.request.Request(
        args.url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": idem_key,
            "User-Agent": "case-calendar-webhook-tester/1.0",
        },
    )

    print(f"POST {args.url}")
    print(f"Idempotency-Key: {idem_key}")
    print(f"Body: {len(body)} bytes")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status = e.code
        payload = e.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        print(f"connection error: {e}", file=sys.stderr)
        return 2

    print(f"\nHTTP {status}")
    print(payload)
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
