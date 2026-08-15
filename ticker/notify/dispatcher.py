"""Parallel, idempotent alert fan-out.

Two rules:
  * Channels fire concurrently. A slow channel must never delay a fast one —
    sequential delivery would hand your latency budget to the slowest transport.
  * Delivery is idempotent on `event_id`, so a second daemon in another region
    can fire the same alert without double-buzzing you.

Connections are pre-warmed at startup: cold DNS + TLS at fire time costs ~200 ms
for no reason.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from ticker.models import Alert

log = logging.getLogger(__name__)


class Channel(Protocol):
    name: str

    async def warm(self) -> None:
        """Open connections / resolve DNS ahead of time."""

    async def send(self, alert: Alert) -> None:
        ...


class Dispatcher:
    def __init__(self, channels: list[Channel]) -> None:
        self.channels = channels
        self._sent: set[tuple[str, str]] = set()   # (event_id, state)
        self.stats = {"dispatched": 0, "suppressed": 0, "channel_errors": 0}

    async def warm(self) -> None:
        results = await asyncio.gather(
            *(c.warm() for c in self.channels), return_exceptions=True
        )
        for ch, r in zip(self.channels, results, strict=True):
            if isinstance(r, Exception):
                log.warning("channel %s failed to warm: %s", ch.name, r)

    async def dispatch(self, alert: Alert) -> None:
        key = (alert.event_id, alert.state.value)
        if key in self._sent:
            self.stats["suppressed"] += 1
            return
        self._sent.add(key)
        self.stats["dispatched"] += 1

        log.info(
            "DISPATCH %s %s score=%.3f latency=%sms → %d channels",
            alert.state.value.upper(),
            alert.target_id,
            alert.score,
            round(alert.detect_latency_ms) if alert.detect_latency_ms else "?",
            len(self.channels),
        )

        results = await asyncio.gather(
            *(c.send(alert) for c in self.channels), return_exceptions=True
        )
        for ch, r in zip(self.channels, results, strict=True):
            if isinstance(r, Exception):
                self.stats["channel_errors"] += 1
                # One dead channel must not mask the others; the whole point of
                # fanning out is that any single transport can fail.
                log.error("channel %s failed: %r", ch.name, r)


def format_alert(alert: Alert, markets: list[dict] | None = None) -> tuple[str, str]:
    """(title, body) shared by every channel so wording never drifts."""
    if alert.state.value == "retracted":
        title = f"RETRACTED: {alert.target_id}"
        body = "Earlier alert no longer supported by evidence.\n"
    elif alert.state.value == "confirmed":
        title = f"CONFIRMED: {alert.target_id}"
        body = ""
    else:
        title = f"{alert.state.value.upper()}: {alert.target_id}"
        body = ""

    body += f"{alert.headline}\n"
    if alert.detect_latency_ms:
        body += f"\ndetect +{alert.detect_latency_ms / 1000:.1f}s · score {alert.score:.2f}"
    if alert.url:
        body += f"\n{alert.url}"
    if alert.evidence:
        body += "\n\nEvidence:\n" + "\n".join(
            f"· T{int(e.tier)} {e.source_id}: {e.headline[:70]}"
            for e in alert.evidence[:5]
        )
    for m in markets or []:
        body += f"\n\n→ {m.get('venue', 'market')}: {m.get('url', '')}"
    return title, body
