"""Real-time fanout to connected clients.

Single replica uses an in-process bus. Set REDIS_URL and the same code fans out
across every app server — necessary once you run more than one, because a client
holding an SSE connection to replica A must still receive an event ingested by
replica B.

The interface is intentionally tiny (publish / subscribe) so a different bus
(NATS, SNS) can be dropped in without touching the routers.
"""

from __future__ import annotations

import asyncio
import json
import logging

from app.config import get_settings

log = logging.getLogger(__name__)


class Fanout:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._redis = None
        self._task: asyncio.Task | None = None
        self.published = 0

    # --- lifecycle ----------------------------------------------------

    async def start(self) -> None:
        url = get_settings().redis_url
        if not url:
            log.info("fanout: in-process (single replica)")
            return
        try:
            import redis.asyncio as aioredis
        except ImportError:
            log.error("fanout: REDIS_URL set but `redis` not installed; "
                      "falling back to in-process. Multi-replica WILL drop events.")
            return
        self._redis = aioredis.from_url(url, decode_responses=True)
        self._task = asyncio.create_task(self._consume())
        log.info("fanout: redis at %s", url.split("@")[-1])

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._redis:
            await self._redis.aclose()

    async def _consume(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe("events")
        async for msg in pubsub.listen():
            if msg.get("type") == "message":
                self._local_publish(msg["data"])

    # --- pub/sub ------------------------------------------------------

    async def publish(self, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":"))
        self.published += 1
        if self._redis:
            # Redis echoes back to every replica including this one, so do not
            # also deliver locally or subscribers here get it twice.
            await self._redis.publish("events", body)
        else:
            self._local_publish(body)

    def _local_publish(self, body: str) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(body)
            except asyncio.QueueFull:
                # A wedged client must never apply backpressure to ingest.
                log.warning("fanout: subscriber queue full, dropping")

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


fanout = Fanout()
