"""Synthetic end-to-end drill — the most valuable ops code in this repo.

Without this you discover the pipeline is broken on the one day it matters.

It fabricates news about a person who does not exist and pushes it through the
REAL screening, confirmation and dispatch code, then asserts an alert came out
the far end inside the latency budget.

Two design decisions worth understanding:

1. **Negative probes are not optional.** A drill that only checks "does a death
   headline fire?" cannot detect a funnel that has degraded into firing on
   *everything* — which is the failure mode that costs you money. So each run
   also asserts that a condolence headline and an idiom headline do NOT fire.
   Precision and recall are both verified, or neither is.

2. **A fictional target, not a real one.** Injecting "Trump has died" through the
   live pipeline would pollute the real accumulator and could fire a real alert.
   The canary builds its own target and its own confirmer, while loading the same
   lexicons and rules as production — so a broken lexicon breaks the drill too.

Scope boundary, stated honestly: this verifies screen → confirm → notify. It does
NOT verify that live ingest adapters are receiving data. That is what the
staleness watchdog in ops/health.py is for. The two together cover the pipeline;
neither alone does.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from ticker.config import (
    FirePolicy,
    Target,
    build_automaton,
    load_satire_domains,
    load_targets,
)
from ticker.confirm.policy import Confirmer
from ticker.models import Item, TargetState, Tier
from ticker.notify.dispatcher import Dispatcher
from ticker.screen.dedupe import Deduper
from ticker.screen.funnel import Funnel

log = logging.getLogger(__name__)

# Deliberately absurd, so it can never collide with real news, and so a stray
# alert reaching your phone is unmistakably a drill.
CANARY_ID = "canary-drill"
CANARY_NAME = "Zephyrine Quillstrom"


def make_canary_target() -> Target:
    return Target(
        id=CANARY_ID,
        display_name=f"[DRILL] {CANARY_NAME}",
        aliases=[CANARY_NAME, "Quillstrom"],
        strong_aliases=[CANARY_NAME],
        birth_year=1943,
        fire=FirePolicy(),          # production defaults, on purpose
    )


@dataclass
class Probe:
    label: str
    headline: str
    tier: Tier
    should_fire: bool


PROBES: list[Probe] = [
    # Recall: the thing the system exists to do.
    Probe("positive/wire", f"{CANARY_NAME} has died at 82, officials confirm",
          Tier.WIRE, True),
    # Precision: the failure mode that costs money.
    Probe("negative/condolence",
          f"{CANARY_NAME} mourns the death of a longtime colleague",
          Tier.WIRE, False),
    Probe("negative/idiom",
          f"{CANARY_NAME} and rival locked in dead heat as polls close",
          Tier.WIRE, False),
    Probe("negative/conditional",
          f"What happens if {CANARY_NAME} dies before the vote?",
          Tier.WIRE, False),
]


@dataclass
class CanaryResult:
    passed: bool = True
    latency_ms: float = 0.0
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, label: str, ok: bool, detail: str = "") -> None:
        self.checks.append((label, ok, detail))
        if not ok:
            self.passed = False

    def detail(self) -> str:
        return "; ".join(
            f"{'ok' if ok else 'FAIL'}:{label}{f'({d})' if d else ''}"
            for label, ok, d in self.checks
        )

    def report(self) -> str:
        lines = [f"canary {'PASS' if self.passed else 'FAIL'} "
                 f"(fire latency {self.latency_ms:.1f}ms)"]
        lines += [f"  {'ok  ' if ok else 'FAIL'} {label}"
                  f"{f' — {d}' if d else ''}" for label, ok, d in self.checks]
        return "\n".join(lines)


class _CaptureChannel:
    name = "canary-capture"

    def __init__(self) -> None:
        self.sent: list = []

    async def warm(self) -> None:
        pass

    async def send(self, alert) -> None:
        self.sent.append(alert)


class Canary:
    def __init__(
        self,
        max_latency_ms: float = 8000.0,
        store=None,
        live_channels: list | None = None,
    ) -> None:
        self.max_latency_ms = max_latency_ms
        self.store = store
        # Only used in full mode: proves the real transports still work.
        self.live_channels = live_channels or []
        self.runs = 0
        self.last_result: CanaryResult | None = None

    async def run_once(self, *, full: bool = False) -> CanaryResult:
        """Push every probe through a real pipeline built from real config.

        full=True additionally dispatches through the live notification channels,
        which is the only way to catch an expired Telegram token or a phone that
        has started silencing the channel.
        """
        res = CanaryResult()
        self.runs += 1

        # Load production config so a broken lexicon or target file fails here.
        try:
            targets = dict(load_targets())
            canary_target = make_canary_target()
            targets[CANARY_ID] = canary_target
            automaton = build_automaton(targets)
            satire = load_satire_domains()
            res.add("config+automaton loads", True,
                    f"{automaton.pattern_count} patterns, {len(targets)} targets")
        except Exception as exc:  # noqa: BLE001
            res.add("config+automaton loads", False, f"{type(exc).__name__}: {exc}")
            self._persist(res)
            return res

        capture = _CaptureChannel()
        channels = [capture] + (self.live_channels if full else [])
        confirmer = Confirmer(
            {CANARY_ID: canary_target},      # canary accumulator only
            Dispatcher(channels),
            adjudicator=None,                # never spend money on a drill
            store=None,                      # never pollute the audit trail
        )

        for probe in PROBES:
            # Fresh funnel per probe: dedupe must not swallow the next probe,
            # and each probe should see a clean confirmer state for negatives.
            funnel = Funnel(targets, automaton, Deduper(), satire_domains=satire)
            before = len(capture.sent)
            t0 = time.monotonic()

            item = Item(
                source_id="canary",
                tier=probe.tier,
                title=probe.headline,
                url="https://example.invalid/canary",
            )
            try:
                for cand in funnel.evaluate(item).survivors:
                    if cand.target_id == CANARY_ID:
                        await confirmer.handle(cand)
            except Exception as exc:  # noqa: BLE001
                res.add(probe.label, False, f"raised {type(exc).__name__}: {exc}")
                continue

            elapsed_ms = (time.monotonic() - t0) * 1000
            fired = len(capture.sent) > before

            if probe.should_fire:
                res.add(probe.label, fired,
                        f"fired in {elapsed_ms:.1f}ms" if fired else "DID NOT FIRE")
                if fired:
                    res.latency_ms = elapsed_ms
                    alert = capture.sent[-1]
                    res.add("state is CONFIRMED",
                            alert.state is TargetState.CONFIRMED,
                            alert.state.value)
                    res.add("latency within budget",
                            elapsed_ms <= self.max_latency_ms,
                            f"{elapsed_ms:.1f}ms / {self.max_latency_ms:.0f}ms")
            else:
                res.add(probe.label, not fired,
                        "FIRED — precision has degraded" if fired else "correctly ignored")

            # Reset between probes so a fired positive does not keep the
            # accumulator CONFIRMED and mask a subsequent negative.
            confirmer.accumulators[CANARY_ID] = type(
                confirmer.accumulators[CANARY_ID]
            )(target_id=CANARY_ID, policy=canary_target.fire)

        self.last_result = res
        self._persist(res)

        if res.passed:
            log.info("canary PASS (%.1fms): %s", res.latency_ms, res.detail())
        else:
            # If the dispatcher itself is broken we cannot page you about it —
            # the absence of a recent PASS row in canary_runs is the backstop.
            log.error("CANARY FAILED — pipeline is not known to work:\n%s",
                      res.report())
        return res

    def _persist(self, res: CanaryResult) -> None:
        if self.store:
            try:
                self.store.record_canary(res.passed, res.latency_ms, res.detail())
            except Exception:  # noqa: BLE001
                log.exception("canary: failed to persist result")

    async def run_daily(self, hour_utc: int = 9, full_on_weekday: int | None = 0) -> None:
        """Sleep until the next scheduled hour, drill, repeat.

        full_on_weekday: 0=Monday. On that day the drill also exercises the live
        channels, so an expired credential surfaces weekly rather than never.
        Set to None to keep every run internal.
        """
        while True:
            await asyncio.sleep(self._seconds_until(hour_utc))
            full = (
                full_on_weekday is not None
                and time.gmtime().tm_wday == full_on_weekday
                and bool(self.live_channels)
            )
            try:
                await self.run_once(full=full)
            except Exception:  # noqa: BLE001
                log.exception("canary run crashed")
            await asyncio.sleep(60)   # don't re-fire inside the same hour

    @staticmethod
    def _seconds_until(hour_utc: int) -> float:
        now = time.gmtime()
        secs_now = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
        target = hour_utc * 3600
        delta = target - secs_now
        if delta <= 0:
            delta += 86400
        return float(delta)
