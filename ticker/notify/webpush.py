"""Web Push (VAPID) channel for the installed PWA.

Setup:
  1. python tools/gen_vapid_keys.py   → writes the keypair, prints exports
  2. Serve web/ over HTTPS (see deploy/caddy/Caddyfile).
  3. Open the app, grant notification permission; the subscription POSTs to
     /api/subscribe and is stored in SQLite.

IMPORTANT iOS caveat: Safari only delivers web push to a PWA that the user has
added to the home screen (iOS 16.4+). A browser tab will never receive it. The
onboarding UI in web/index.html says so explicitly, because otherwise this
channel appears to work and silently never fires.

pywebpush is synchronous, so sends run in a thread executor to keep the event
loop free.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from pywebpush import WebPushException, webpush

from ticker.models import Alert
from ticker.notify.dispatcher import format_alert

log = logging.getLogger(__name__)


class WebPushChannel:
    name = "webpush"

    def __init__(
        self,
        get_subscriptions,          # callable → list[dict] of subscription info
        drop_subscription,          # callable(endpoint) for 404/410 cleanup
        private_key_env: str = "TICKER_VAPID_PRIVATE_KEY",
        subject: str = "mailto:you@example.com",
        ttl_s: int = 300,
        urgency: str = "high",
        markets: list[dict] | None = None,
    ) -> None:
        self.get_subscriptions = get_subscriptions
        self.drop_subscription = drop_subscription
        self.private_key = os.environ.get(private_key_env, "")
        self.subject = subject
        self.ttl_s = ttl_s
        self.urgency = urgency
        self.markets = markets or []
        if not self.private_key:
            log.warning("webpush inactive: set %s", private_key_env)

    @property
    def configured(self) -> bool:
        return bool(self.private_key)

    async def warm(self) -> None:
        # Nothing to pre-open: each push is a fresh request to the browser
        # vendor's endpoint. Listed for interface symmetry.
        return

    async def send(self, alert: Alert) -> None:
        if not self.configured:
            return
        title, body = format_alert(alert, self.markets)
        payload = json.dumps(
            {
                "title": title,
                "body": body,
                "tag": alert.event_id,     # replaces rather than stacks
                "state": alert.state.value,
                "url": alert.url,
                "requireInteraction": True,
            }
        )
        subs = self.get_subscriptions()
        if not subs:
            log.warning("webpush: no subscriptions registered")
            return
        await asyncio.gather(
            *(self._send_one(s, payload) for s in subs), return_exceptions=True
        )

    async def _send_one(self, sub: dict, payload: str) -> None:
        def _blocking() -> None:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=self.private_key,
                vapid_claims={"sub": self.subject},
                ttl=self.ttl_s,
                headers={"Urgency": self.urgency},
            )

        try:
            await asyncio.to_thread(_blocking)
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
                # Subscription is permanently gone — prune it so the list does
                # not fill with dead endpoints that slow every future fan-out.
                self.drop_subscription(sub.get("endpoint", ""))
                log.info("webpush: pruned expired subscription")
            else:
                raise
