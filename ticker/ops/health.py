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
import time

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
        confirm_cycles: int = 3,
        realert_cooldown_s: float = 3600.0,
    ) -> None:
        self.adapters = adapters
        self.staleness_multiplier = staleness_multiplier
        self.heartbeat_interval_s = heartbeat_interval_s
        self.store = store
        self.on_degraded = on_degraded
        # Edge-triggering alone is not enough. It suppresses a source that stays
        # down, but a source that FLAPS presents a fresh edge every cycle, and
        # every edge was a message. Two dampers, because they stop different
        # things: confirm_cycles ignores blips shorter than a real outage, and
        # realert_cooldown_s caps how often any one source may speak.
        self.confirm_cycles = max(1, int(confirm_cycles))
        self.realert_cooldown_s = realert_cooldown_s
        self._alarmed: set[str] = set()
        self._stale_streak: dict[str, int] = {}
        self._last_alert_at: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}

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

            stale_ids = {s["id"] for s in stale}
            now = time.monotonic()

            # Sustained-staleness counter. A source must look dead for
            # confirm_cycles consecutive checks before it is believed, which
            # rides out reconnects without ever reporting them.
            for sid in stale_ids:
                self._stale_streak[sid] = self._stale_streak.get(sid, 0) + 1
            for sid in list(self._stale_streak):
                if sid not in stale_ids:
                    del self._stale_streak[sid]

            recovered = self._alarmed - stale_ids
            for sid in recovered:
                missed = self._suppressed.pop(sid, 0)
                log.info("source %s recovered%s", sid,
                         f" (suppressed {missed} repeat alert(s) while down)" if missed else "")
            self._alarmed = set(stale_ids)

            newly, muted = [], []
            for s in stale:
                sid = s["id"]
                if self._stale_streak.get(sid, 0) != self.confirm_cycles:
                    continue          # not yet confirmed, or already reported
                last = self._last_alert_at.get(sid)
                if last is not None and now - last < self.realert_cooldown_s:
                    self._suppressed[sid] = self._suppressed.get(sid, 0) + 1
                    muted.append(sid)
                    continue
                self._last_alert_at[sid] = now
                newly.append(s)

            if muted:
                log.warning("SOURCES STILL DEGRADED (alert muted by cooldown): %s", muted)
            if newly:
                log.error("SOURCES DEGRADED: %s", [s["id"] for s in newly])
                if self.on_degraded:
                    await self.on_degraded(newly)

            if self.store:
                await asyncio.to_thread(self.store.record_health, snaps)

            sd_notify("WATCHDOG=1")
            await asyncio.sleep(self.heartbeat_interval_s)
