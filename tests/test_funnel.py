"""Precision/recall smoke test for the screening cascade.

These headlines are the actual adversaries. Every NEGATIVE case below is a real
recurring pattern in political coverage that a naive "Trump" + "dies" keyword
match would fire on. If this file passes, the funnel is doing its job; if you
loosen a weight in rules.py, this is what tells you what you broke.

Run: .venv/bin/python -m tests.test_funnel
"""

from __future__ import annotations

import sys

from ticker.config import build_automaton, load_satire_domains, load_targets
from ticker.models import Item, Tier
from ticker.screen.funnel import Funnel

# (headline, url, should_produce_candidate)
CASES: list[tuple[str, str, bool]] = [
    # --- TRUE POSITIVES: must survive ------------------------------------
    ("Donald Trump has died at 79, White House confirms",
     "https://apnews.com/x", True),
    ("Breaking: Donald Trump dead at 79",
     "https://bbc.co.uk/x", True),
    ("Former President Donald J. Trump dies at his Florida home",
     "https://nytimes.com/x", True),
    ("Donald Trump, 1946-2026: the life of a disruptor",
     "https://guardian.com/x", True),
    ("President Donald Trump pronounced dead following medical emergency",
     "https://npr.org/x", True),

    # --- FALSE POSITIVES: must be killed ---------------------------------
    # #1 class: someone ELSE died and the target is reacting
    ("Trump mourns death of former campaign aide",
     "https://apnews.com/x", False),
    ("Trump pays tribute to Queen Elizabeth after her death",
     "https://bbc.co.uk/x", False),
    ("Trump attends funeral of former Senator Bob Dole",
     "https://nytimes.com/x", False),
    ("Trump orders flags at half-staff after death of Justice Anthony Kennedy",
     "https://apnews.com/x", False),
    # conditional / hypothetical
    ("What happens if Trump dies in office? A look at succession",
     "https://theatlantic.com/x", False),
    ("Analysts prepare for contingency should Trump die before the election",
     "https://reuters.com/x", False),
    # idiom
    ("Trump and Newsom in dead heat in new national poll",
     "https://thehill.com/x", False),
    ("Trump's infrastructure bill dead on arrival in the Senate",
     "https://politico.com/x", False),
    ("Trump faces a deadline on the spending deal",
     "https://cnn.com/x", False),
    # threat / attempt
    ("Man charged over death threats against Donald Trump",
     "https://apnews.com/x", False),
    ("Trump survived assassination attempt, officials say",
     "https://bbc.co.uk/x", False),
    # alias covered by a negation phrase
    ("Fred Trump, father of the president, died in 1999",
     "https://nytimes.com/x", False),
    ("Ivana Trump died at her Manhattan home, medical examiner says",
     "https://apnews.com/x", False),
    ("Trump Organization executive dies at 68",
     "https://wsj.com/x", False),
    # metaphor — alias does not even match "Trumpism" (word-boundary check)
    ("The slow political death of Trumpism",
     "https://vox.com/x", False),
    # satire domain
    ("Donald Trump dead at 79, sources confirm",
     "https://theonion.com/x", False),
    # nothing to do with the target
    ("Queen Elizabeth II has died at 96, Buckingham Palace announces",
     "https://bbc.co.uk/x", False),
    ("Stock markets rally as investors shrug off inflation data",
     "https://reuters.com/x", False),
]


def main() -> int:
    targets = load_targets()
    if "trump" not in targets:
        print("FAIL: no 'trump' target loaded from config/targets/")
        return 1

    automaton = build_automaton(targets)
    print(f"automaton: {automaton.pattern_count} patterns, "
          f"{len(targets)} target(s)\n")

    funnel = Funnel(targets, automaton, satire_domains=load_satire_domains())

    failures: list[str] = []
    for headline, url, want in CASES:
        # Fresh dedupe state per case so near-identical fixtures don't mask
        # each other — dedupe is exercised separately in test_dedupe.
        funnel.deduper.reset()

        item = Item(source_id="test", tier=Tier.MAJOR, title=headline, url=url)
        res = funnel.evaluate(item)
        got = bool(res.survivors)

        ok = got == want
        mark = "ok  " if ok else "FAIL"
        print(f"{mark} want={'FIRE' if want else 'kill'}  "
              f"{headline[:58]:<58} | {res.describe()[:78]}")
        if not ok:
            failures.append(headline)

    # Retractions must SURVIVE screening despite scoring like false positives —
    # they reach the confirmer as negative evidence. Dropping them would leave a
    # false CONFIRMED standing with no way to walk it back.
    print("\n--- retractions bypass the score floor (negative evidence) ---")
    from ticker.screen import rules as _rules
    for headline in [
        "Fact check: viral post claiming Donald Trump died is false",
        "Correction: our earlier report that Donald Trump died was inaccurate",
        "White House denies report that Donald Trump has died",
        "Donald Trump is alive and well, spokesperson says after death hoax",
    ]:
        funnel.deduper.reset()
        res = funnel.evaluate(
            Item(source_id="test", tier=Tier.WIRE, title=headline,
                 url="https://apnews.com/x")
        )
        survived = bool(res.survivors)
        flagged = _rules.is_retraction(headline)
        ok = survived and flagged
        print(f"{'ok  ' if ok else 'FAIL'} survives={survived} retraction={flagged}"
              f"  {headline[:66]}")
        if not ok:
            failures.append(f"retraction not preserved: {headline}")

    print(f"\n{funnel.report()}")
    if failures:
        print(f"\n{len(failures)}/{len(CASES)} FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\nall {len(CASES)} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
