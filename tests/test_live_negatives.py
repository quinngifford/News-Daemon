"""Regression test against REAL headlines captured from live feeds.

The hard invariant: no headline in tests/fixtures/live_negatives.txt may score
at or above `fire_policy.min_score`. That threshold is what enables fast-path
firing and what lets two independent tier-1 outlets reach CONFIRMED, so a false
positive at or above it is a false alert on your phone — and, given what this
system is for, a trade on bad information.

The Lindsey Graham cluster is why this file exists. On 2026-08-13 it was
reported by the Guardian (0.922), Bloomberg (0.837), Al-Monitor (0.837) and NYT
(0.757). Two independent tier-1 origins above min_score would have CONFIRMED
Trump's death from a story about a senator.

Run: .venv/bin/python -m tests.test_live_negatives
"""

from __future__ import annotations

import sys
from pathlib import Path

from ticker.config import build_automaton, load_satire_domains, load_targets
from ticker.models import Item, Tier
from ticker.screen.funnel import Funnel

FIXTURE = Path(__file__).parent / "fixtures" / "live_negatives.txt"

# True positives must keep working. Precision fixes that kill recall are worse
# than the bug they fix, because the whole system exists to catch this one event.
TRUE_POSITIVES = [
    "Donald Trump has died at 79, White House confirms",
    "Breaking: Donald Trump dead at 79",
    "Former President Donald J. Trump dies at his Florida home",
    "President Donald Trump pronounced dead following medical emergency",
    "Donald Trump, 1946-2026: the life of a disruptor",
    "Donald Trump has died, according to AP",
    "Trump dies at 79",
    "Donald Trump died Tuesday at his home in Florida, his family said",
]


def load_fixture() -> list[str]:
    return [
        ln.strip()
        for ln in FIXTURE.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


def main() -> int:
    targets = load_targets()
    target = targets["trump"]
    min_score = target.fire.min_score
    llm_floor = target.fire.llm_min_score
    funnel = Funnel(targets, build_automaton(targets),
                    satire_domains=load_satire_domains())

    negatives = load_fixture()
    print(f"{len(negatives)} real negatives, {len(TRUE_POSITIVES)} true positives")
    print(f"min_score={min_score} (fire threshold)  "
          f"llm_min_score={llm_floor} (adjudication floor)\n")

    violations: list[tuple[str, float, str]] = []
    reaching_llm: list[tuple[str, float]] = []
    scores: list[float] = []

    for headline in negatives:
        funnel.deduper.reset()
        res = funnel.evaluate(
            Item(source_id="fixture", tier=Tier.MAJOR, title=headline,
                 url="https://example.com/x")
        )
        best = max((c.score for c in res.candidates), default=0.0)
        scores.append(best)
        cand = max(res.candidates, key=lambda c: c.score, default=None)
        if best >= min_score:
            violations.append((headline, best, cand.reason if cand else ""))
        elif best >= llm_floor:
            reaching_llm.append((headline, best))

    print("--- SAFETY INVARIANT: no negative may reach min_score ---")
    if violations:
        print(f"FAIL {len(violations)} negative(s) at or above {min_score}:")
        for h, s, why in sorted(violations, key=lambda x: -x[1]):
            print(f"     {s:.3f}  {h[:78]}")
            print(f"            [{why[:100]}]")
    else:
        print(f"ok   0 of {len(negatives)} negatives reach {min_score} "
              f"(max was {max(scores):.3f})")

    print(f"\n--- COST: negatives still reaching the LLM (>= {llm_floor}) ---")
    print(f"     {len(reaching_llm)} of {len(negatives)}"
          f" — each costs a fraction of a cent and is correctly rejected there")
    for h, s in sorted(reaching_llm, key=lambda x: -x[1])[:8]:
        print(f"     {s:.3f}  {h[:76]}")

    print("\n--- RECALL: true positives must still fire ---")
    missed: list[tuple[str, float]] = []
    for headline in TRUE_POSITIVES:
        funnel.deduper.reset()
        res = funnel.evaluate(
            Item(source_id="fixture", tier=Tier.WIRE, title=headline,
                 url="https://apnews.com/x")
        )
        best = max((c.score for c in res.candidates), default=0.0)
        ok = best >= min_score
        print(f"{'ok  ' if ok else 'FAIL'} {best:.3f}  {headline[:70]}")
        if not ok:
            missed.append((headline, best))

    print()
    failed = bool(violations or missed)
    if violations:
        print(f"{len(violations)} SAFETY VIOLATION(S) — these would fire falsely")
    if missed:
        print(f"{len(missed)} MISSED TRUE POSITIVE(S) — recall regression")
    if not failed:
        print(f"PASS — {len(negatives)} real negatives all below the fire "
              f"threshold, all {len(TRUE_POSITIVES)} true positives still fire")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
