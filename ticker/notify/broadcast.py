"""In-process SSE broadcaster — the lowest-latency channel available.

When a browser tab is open, this beats every push transport: no vendor
infrastructure in the path, just a write to an already-open socket. It is also
the only channel that can guarantee an audible alert, since a page can play a
sound and no push service can promise the OS will.

Implemented as a `Channel`, so the dispatcher fans out to it in parallel with
Telegram and Web Push and knows nothing about the difference.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque

from ticker.models import Alert

log = logging.getLogger(__name__)


class SseBroadcaster:
    name = "sse"

    def __init__(self, history: int = 50, queue_size: int = 32) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._history: deque[str] = deque(maxlen=history)
        self.queue_size = queue_size
        self.delivered = 0

    # --- Channel protocol -------------------------------------------------

    async def warm(self) -> None:
        return

    async def send(self, alert: Alert) -> None:
        payload = json.dumps({
            "type": "alert",
            "event_id": alert.event_id,
            "target": alert.target_id,
            "state": alert.state.value,
            "headline": alert.headline,
            "url": alert.url,
            "score": round(alert.score, 4),
            "detect_latency_ms": alert.detect_latency_ms,
            "t_wall": alert.t_wall,
            "evidence": [
                {"source": e.source_id, "tier": int(e.tier),
                 "origin": e.origin, "negative": e.negative,
                 "headline": e.headline[:200]}
                for e in alert.evidence[:10]
            ],
        })
        self.publish(payload)

    # --- fan-out ----------------------------------------------------------

    def publish(self, payload: str) -> None:
        self._history.append(payload)
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
                self.delivered += 1
            except asyncio.QueueFull:
                # A wedged tab must never apply backpressure to the alert path.
                log.warning("sse: subscriber queue full, dropping")

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=self.queue_size)
        for past in self._history:
            try:
                q.put_nowait(past)
            except asyncio.QueueFull:
                break
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
