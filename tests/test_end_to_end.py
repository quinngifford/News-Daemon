"""End-to-end: item → funnel → confirmer → dispatcher, with no network at all.

Covers the two paths that actually matter operationally:
  * FAST PATH — a single tier-0 wire item fires CONFIRMED immediately.
  * CORROBORATION — two independent tier-1 outlets reach the threshold together,
    and one outlet repeated twice does NOT.
  * RETRACTION — evidence collapse walks the state back.

Adjudicator is None throughout, so this makes zero API calls and costs nothing.

Run: .venv/bin/python -m tests.test_end_to_end
"""

from __future__ import annotations

import asyncio
import sys

from ticker.config import build_automaton, load_satire_domains, load_targets
from ticker.confirm.policy import Confirmer
from ticker.models import Item, TargetState, Tier
from ticker.notify.dispatcher import Dispatcher
from ticker.screen.funnel import Funnel

failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class CaptureChannel:
    """Stands in for Telegram/WebPush. Records instead of sending."""

    name = "capture"

    def __init__(self) -> None:
        self.sent: list = []

    async def warm(self) -> None:
        pass

    async def send(self, alert) -> None:
        self.sent.append(alert)


def make_stack():
    targets = load_targets()
    funnel = Funnel(targets, build_automaton(targets),
                    satire_domains=load_satire_domains())
    cap = CaptureChannel()
    confirmer = Confirmer(targets, Dispatcher([cap]), adjudicator=None, store=None)
    return funnel, confirmer, cap


async def feed(funnel, confirmer, item: Item) -> int:
    """Push one item all the way through. Returns candidates that survived."""
    funnel.deduper.reset()
    res = funnel.evaluate(item)
    for cand in res.survivors:
        await confirmer.handle(cand)
    return len(res.survivors)


async def scenario_fast_path() -> None:
    print("\n--- FAST PATH: single tier-0 wire fires immediately ---")
    funnel, confirmer, cap = make_stack()
    n = await feed(funnel, confirmer, Item(
        source_id="x:AP", tier=Tier.WIRE,
        title="Donald Trump has died at 79, White House confirms",
        url="https://x.com/AP/status/1",
    ))
    check(n == 1, "wire item survived screening")
    check(len(cap.sent) == 1, "exactly one alert dispatched", f"{len(cap.sent)} sent")
    if cap.sent:
        a = cap.sent[0]
        check(a.state is TargetState.CONFIRMED, "state is CONFIRMED",
              f"got {a.state.value}")
        check(a.detect_latency_ms is not None and a.detect_latency_ms < 500,
              "detect latency recorded and small",
              f"{a.detect_latency_ms:.1f}ms")
    check(confirmer.stats["fast_path"] == 1, "fast path counter incremented")
    check(confirmer.stats["adjudicated"] == 0, "no LLM call made (adjudicator=None)")


async def scenario_corroboration() -> None:
    print("\n--- CORROBORATION: two independent tier-1 outlets ---")
    funnel, confirmer, cap = make_stack()
    await feed(funnel, confirmer, Item(
        source_id="bbc-news", tier=Tier.MAJOR,
        title="Donald Trump dies at 79", url="https://bbc.co.uk/1"))
    state_after_one = confirmer.accumulators["trump"].state
    check(state_after_one is TargetState.LIKELY,
          "one major outlet reaches LIKELY, not CONFIRMED",
          f"got {state_after_one.value}")

    await feed(funnel, confirmer, Item(
        source_id="guardian-us", tier=Tier.MAJOR,
        title="Donald Trump has died aged 79", url="https://guardian.com/1"))
    acc = confirmer.accumulators["trump"]
    check(acc.state is TargetState.CONFIRMED,
          "second independent major reaches CONFIRMED",
          f"weight={acc.weight():.3f} state={acc.state.value}")
    check(len(acc.independent_origins()) == 2, "two independent origins counted",
          f"{sorted(acc.independent_origins())}")

    print("\n--- INDEPENDENCE: same outlet twice must NOT confirm ---")
    funnel2, confirmer2, cap2 = make_stack()
    for i in range(3):
        await feed(funnel2, confirmer2, Item(
            source_id="bbc-news", tier=Tier.MAJOR,
            title=f"Donald Trump dies at 79 (update {i})",
            url=f"https://bbc.co.uk/{i}"))
    acc2 = confirmer2.accumulators["trump"]
    check(acc2.state is not TargetState.CONFIRMED,
          "one outlet repeated 3x does not confirm",
          f"weight={acc2.weight():.3f} state={acc2.state.value}")

    print("\n--- ATTRIBUTION COLLAPSING: 'citing AP' promotes to wire ---")
    funnel3, confirmer3, cap3 = make_stack()
    await feed(funnel3, confirmer3, Item(
        source_id="google-news:trump", tier=Tier.REGIONAL,
        title="Donald Trump has died at 79, according to AP",
        url="https://news.google.com/1"))
    acc3 = confirmer3.accumulators["trump"]
    check(len(cap3.sent) >= 1, "aggregator item citing AP fast-paths as wire",
          f"{len(cap3.sent)} alert(s), weight={acc3.weight():.3f}")


async def scenario_retraction() -> None:
    print("\n--- RETRACTION: evidence collapse walks the state back ---")
    funnel, confirmer, cap = make_stack()
    await feed(funnel, confirmer, Item(
        source_id="x:AP", tier=Tier.WIRE,
        title="Donald Trump has died at 79, White House confirms",
        url="https://x.com/AP/status/1"))
    first = len(cap.sent)

    retraction = Item(
        source_id="x:AP", tier=Tier.WIRE,
        title="Correction: our earlier report that Donald Trump died was false",
        url="https://x.com/AP/status/2",
    )
    survived = await feed(funnel, confirmer, retraction)
    acc = confirmer.accumulators["trump"]
    print(f"     retraction survived screening: {survived} candidate(s); "
          f"weight now {acc.weight():.3f}; state {acc.state.value}")
    check(survived >= 1,
          "RETRACTION REACHES THE CONFIRMER (must bypass the score floor)")
    retracted = [a for a in cap.sent if a.state is TargetState.RETRACTED]
    check(bool(retracted), "RETRACTED alert dispatched",
          f"{len(cap.sent)} total alerts (was {first})")
    if retracted and first:
        check(retracted[0].event_id == cap.sent[0].event_id,
              "retraction reuses the original event_id")


async def main() -> int:
    await scenario_fast_path()
    await scenario_corroboration()
    await scenario_retraction()
    print()
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all end-to-end checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
