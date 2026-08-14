"""Liveness. A monitor that dies silently is worse than no monitor.

Two independent failure detectors, because they catch different things:

  * **Staleness watchdog** — a source that stopped delivering. A feed that 404s
    or a stream that stalls looks exactly like a quiet news day, so silence must
    be alarmed on explicitly.
  * **systemd watchdog** — a hung event loop. `Restart=always` only catches a
    process that *exits*; sd_notify heartbeats catch one that is still running
    but no longer working.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket

log = logging.getLogger(__name__)


def sd_notify(state: str) -> bool:
    """Minimal sd_notify. Avoids a dependency on python-systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):          # abstract namespace socket
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(state.encode())
        return True
    except OSError as exc:
        log.debug("sd_notify failed: %s", exc)
        return False


class HealthMonitor:
    def __init__(
        self,
        adapters: list,
        staleness_multiplier: float = 4.0,
        heartbeat_interval_s: float = 15.0,
        store=None,
        on_degraded=None,          # async callable(list[dict]) → alert the operator
    ) -> None:
        self.adapters = adapters
        self.staleness_multiplier = staleness_multiplier
        self.heartbeat_interval_s = heartbeat_interval_s
        self.store = store
        self.on_degraded = on_degraded
        self._alarmed: set[str] = set()

    def snapshot(self) -> list[dict]:
        return [a.health() for a in self.adapters]

    async def run(self) -> None:
        sd_notify("READY=1")
        while True:
            snaps = self.snapshot()
            stale = [
                s for s in snaps
                if any(
                    a.id == s["id"] and a.is_stale(self.staleness_multiplier)
                    for a in self.adapters
                )
            ]

            # Edge-triggered: alarm on transition into staleness, and log
            # recovery. Level-triggered would spam you every 15 seconds.
            newly = [s for s in stale if s["id"] not in self._alarmed]
            recovered = self._alarmed - {s["id"] for s in stale}
            for sid in recovered:
                log.info("source %s recovered", sid)
            self._alarmed = {s["id"] for s in stale}

            if newly:
                log.error("SOURCES DEGRADED: %s", [s["id"] for s in newly])
                if self.on_degraded:
                    await self.on_degraded(newly)

            if self.store:
                await asyncio.to_thread(self.store.record_health, snaps)

            sd_notify("WATCHDOG=1")
            await asyncio.sleep(self.heartbeat_interval_s)
