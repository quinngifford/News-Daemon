"""The one contract every source adapter implements.

Adding a source must never require touching the funnel, the confirmer, or the
notifier. Adapters do exactly two things: produce Items, and tell the watchdog
they are alive.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from ticker.models import Item, Tier

log = logging.getLogger(__name__)

Emit = Callable[[Item], Awaitable[None]]


class SourceAdapter(ABC):
    """Base class for all ingest sources.

    Subclasses implement `_run`. The base class owns restart-with-backoff and
    heartbeat bookkeeping so no adapter can forget to do either.
    """

    #: stable id, used in evidence rows and the source-independence logic
    id: str = "unnamed"
    #: see ticker.models.Tier — determines evidence weight and fast-path eligibility
    tier: Tier = Tier.SOCIAL
    #: if no item arrives within staleness_multiplier * this, the watchdog alarms
    expected_cadence_s: float = 3600.0

    def __init__(self, config: dict) -> None:
        self.config = config
        self.id = config.get("id", self.id)
        self.tier = Tier(config.get("tier", int(self.tier)))
        self.expected_cadence_s = float(
            config.get("expected_cadence_s", self.expected_cadence_s)
        )
        self.last_item_at: float = time.monotonic()
        self.last_error: str | None = None
        self.items_emitted = 0
        self._backoff = 1.0

    # --- subclass hook -----------------------------------------------------

    @abstractmethod
    async def _run(self, emit: Emit) -> None:
        """Produce items until cancelled. Raise to trigger managed restart."""

    # --- supervision -------------------------------------------------------

    async def run(self, emit: Emit) -> None:
        """Run forever, restarting on failure with exponential backoff.

        An adapter crash must degrade one source, never take down the daemon.
        """
        while True:
            try:
                await self._run(self._wrap(emit))
                # A clean return from a streaming adapter is itself suspicious.
                log.warning("source %s returned unexpectedly; restarting", self.id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — deliberate catch-all
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("source %s failed: %s", self.id, self.last_error)
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, 60.0)

    def _wrap(self, emit: Emit) -> Emit:
        """Stamp bookkeeping on every item without each adapter remembering to."""

        async def _emit(item: Item) -> None:
            self.last_item_at = time.monotonic()
            self.items_emitted += 1
            self._backoff = 1.0          # sustained delivery resets backoff
            await emit(item)

        return _emit

    # --- health ------------------------------------------------------------

    def staleness_s(self) -> float:
        return time.monotonic() - self.last_item_at

    def is_stale(self, multiplier: float = 4.0) -> bool:
        """Silence is the failure mode you cannot otherwise detect."""
        return self.staleness_s() > self.expected_cadence_s * multiplier

    def health(self) -> dict:
        return {
            "id": self.id,
            "tier": int(self.tier),
            "items": self.items_emitted,
            "staleness_s": round(self.staleness_s(), 1),
            "stale": self.is_stale(),
            "last_error": self.last_error,
        }
