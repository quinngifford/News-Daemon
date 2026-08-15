"""Outbound event publishing to the public app backend.

This is the VPS's only outward-facing job. The box itself stays private —
dashboard on 127.0.0.1, no inbound ports — and pushes detected events to a
backend that owns the web app, the mobile app, and user-facing push.

    VPS (private)                     public backend
    ingest → screen → confirm ──POST──▶ /events ──▶ web app + mobile push

Three properties this has to get right, in order of how badly they hurt:

1. **Durability.** The alert reaching your users IS the product. If the backend
   is redeploying or briefly down at the moment the event fires — exactly when
   it is most likely to be under load — an in-memory retry loses the one event
   that mattered. Failures go to a SQLite outbox and are retried until
   delivered, across restarts.

2. **Authenticity.** The backend must be able to prove an event came from this
   box and not from someone who found the endpoint. HMAC-SHA256 over
   `timestamp.body` with a shared secret, in a Stripe-style signature header.
   The timestamp is inside the signed payload so a captured request cannot be
   replayed later.

3. **Idempotency.** Retries, and a redundant second box, must not double-notify.
   Every delivery carries `event_id`; the backend should treat
   `(event_id, state)` as a primary key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time

import httpx

from ticker.models import Alert

log = logging.getLogger(__name__)

SCHEMA_VERSION = "ticker.alert.v1"
SIGNATURE_HEADER = "X-Ticker-Signature"
MAX_BACKOFF_S = 300.0


def build_payload(alert: Alert) -> dict:
    """The public contract with the backend. Additive changes only."""
    return {
        "schema": SCHEMA_VERSION,
        "event_id": alert.event_id,
        "state": alert.state.value,          # confirmed | likely | watch | retracted
        "target": alert.target_id,
        "headline": alert.headline,
        "url": alert.url,
        "score": round(alert.score, 4),
        "detect_latency_ms": alert.detect_latency_ms,
        "occurred_at": alert.t_wall,
        "evidence": [
            {
                "source": e.source_id,
                "origin": e.origin,
                "tier": int(e.tier),
                "negative": e.negative,
                "headline": e.headline[:300],
                "url": e.url,
                "at": e.t_wall,
            }
            for e in alert.evidence[:20]
        ],
    }


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """Stripe-style: t=<unix>,v1=<hex hmac of "t.body">."""
    mac = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={mac}"


def verify(secret: str, header: str, body: bytes, tolerance_s: int = 300) -> bool:
    """Reference verifier — mirror this on the backend.

    Included here so the two sides cannot drift, and so the contract is testable
    from this repo without the backend existing yet.
    """
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
        got = parts["v1"]
    except (ValueError, KeyError):
        return False
    if abs(time.time() - ts) > tolerance_s:
        return False          # stale: someone is replaying a captured request
    expected = hmac.new(
        secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, got)


class WebhookChannel:
    name = "webhook"

    def __init__(
        self,
        client: httpx.AsyncClient,
        store,
        url: str = "",
        secret_env: str = "TICKER_WEBHOOK_SECRET",
        timeout_s: float = 5.0,
    ) -> None:
        self.client = client
        self.store = store
        self.url = url or os.environ.get("TICKER_WEBHOOK_URL", "")
        self.secret = os.environ.get(secret_env, "")
        self.timeout_s = timeout_s
        self.delivered = 0
        self.failed = 0
        if not self.url:
            log.warning("webhook inactive: set TICKER_WEBHOOK_URL "
                        "(events will still be queued once configured)")
        elif not self.secret:
            log.error("webhook URL set but %s is empty — refusing to send "
                      "UNSIGNED events", secret_env)

    @property
    def configured(self) -> bool:
        return bool(self.url and self.secret)

    async def warm(self) -> None:
        """Pre-establish DNS + TLS so firing is one write on a warm socket."""
        if not self.configured:
            return
        try:
            await self.client.head(self.url, timeout=self.timeout_s)
        except httpx.HTTPError:
            pass          # backend may not implement HEAD; the connection is what matters

    async def _post(self, payload: dict) -> tuple[bool, str]:
        body = json.dumps(payload, separators=(",", ":")).encode()
        ts = int(time.time())
        headers = {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: sign(self.secret, ts, body),
            # Lets the backend dedupe without parsing the body.
            "Idempotency-Key": f"{payload['event_id']}:{payload['state']}",
            "User-Agent": "news-ticker-daemon/0.1",
        }
        try:
            r = await self.client.post(self.url, content=body, headers=headers,
                                       timeout=self.timeout_s)
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if 200 <= r.status_code < 300:
            return True, ""
        return False, f"http {r.status_code}: {r.text[:120]}"

    async def send(self, alert: Alert) -> None:
        """Fast path: POST immediately; on failure fall back to the outbox.

        The POST is attempted before any disk write so a healthy backend sees the
        event with no fsync in the latency path. Durability only costs something
        when it is actually needed.
        """
        if not self.url:
            return
        payload = build_payload(alert)
        if not self.secret:
            # Queue rather than send unsigned: the backend cannot trust it, but
            # the event is not lost once a secret is configured.
            self._enqueue(payload, "no signing secret configured")
            return

        ok, err = await self._post(payload)
        if ok:
            self.delivered += 1
            log.info("webhook delivered %s/%s", alert.event_id[:8], alert.state.value)
        else:
            self.failed += 1
            log.error("webhook POST failed (%s) — queued for retry", err)
            self._enqueue(payload, err)

    def _enqueue(self, payload: dict, error: str) -> None:
        if not self.store:
            log.error("no store: event %s CANNOT be retried and is LOST",
                      payload.get("event_id"))
            return
        self.store.enqueue_outbox(
            payload["event_id"], payload["state"],
            json.dumps(payload), time.time() + 5.0,
        )
        log.info("webhook queued %s/%s for retry (%s)",
                 payload["event_id"][:8], payload["state"], error[:80])

    async def deliver_pending(self) -> int:
        """Drain whatever the outbox owes. Called on a loop and at startup."""
        if not self.configured or not self.store:
            return 0
        now = time.time()
        rows = self.store.pending_outbox(now)
        sent = 0
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                self.store.mark_outbox_delivered(row["id"])   # unparseable: drop
                continue
            ok, err = await self._post(payload)
            if ok:
                self.store.mark_outbox_delivered(row["id"])
                self.delivered += 1
                sent += 1
                log.info("webhook retry delivered %s/%s after %d attempt(s)",
                         payload["event_id"][:8], payload["state"],
                         row["attempts"] + 1)
            else:
                # Exponential backoff, capped. Never give up entirely: a
                # multi-hour backend outage must not discard the event.
                delay = min(2 ** min(row["attempts"], 12), MAX_BACKOFF_S)
                self.store.mark_outbox_failed(row["id"], err, now + delay)
                log.warning("webhook retry failed (%s), next in %.0fs", err, delay)
        return sent

    def stats(self) -> dict:
        s = {"delivered": self.delivered, "failed": self.failed,
             "configured": self.configured, "url_set": bool(self.url)}
        if self.store:
            s.update(self.store.outbox_stats())
        return s
