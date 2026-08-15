"""Offline tests for the replay machinery, and for GENERALISATION.

Two jobs:

1. **Verify tools/replay.py works** without needing GDELT (whose quota is easily
   exhausted): title normalisation, seendate parsing, target synthesis, and the
   detect/ignore logic in replay_event().

2. **The thing nothing else in this repo tests.** Every weight in
   screen/rules.py was tuned against Donald Trump headlines. Rules tuned on one
   person can silently fail on another: different name shapes (mononyms,
   regnal numerals, non-ASCII), different honorifics, different idioms. This
   replays each false-positive class found in live Trump data against SEVEN
   other people, so overfitting surfaces as a failure here rather than as a
   missed event.

The headlines below are SYNTHETIC — written to match the structural patterns
observed in real coverage, not copied from real articles. The real-coverage
backtest is tools/replay.py itself, which needs the GDELT download.

Run: .venv/bin/python -m tests.test_replay
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from ticker.config import build_automaton, load_satire_domains  # noqa: E402
from ticker.models import Item, Tier  # noqa: E402
from ticker.screen.funnel import Funnel  # noqa: E402
from tools.replay import (  # noqa: E402
    EVENTS,
    load_or_fetch,
    make_target,
    normalise,
    parse_seendate,
    replay_event,
)

failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


# (full name used in headlines, surname/short form, a plausible other-first-name)
PEOPLE = [
    ("Queen Elizabeth II", "Elizabeth II", "Margaret"),
    ("Jimmy Carter", "Carter", "Howard"),
    ("Pope Benedict XVI", "Benedict XVI", None),
    ("Pele", "Pele", None),
    ("Shinzo Abe", "Abe", "Kenji"),
    ("Henry Kissinger", "Kissinger", "Ruth"),
    ("Silvio Berlusconi", "Berlusconi", "Paolo"),
]


def spec_for(name: str) -> dict:
    """Minimal target spec, mirroring config/replay_events.yaml shape."""
    return {
        "id": name.lower().replace(" ", "-"),
        "display_name": name,
        "aliases": [name],
        "strong_aliases": [name],
    }


def build(spec: dict) -> Funnel:
    t = make_target(spec)
    return Funnel({t.id: t}, build_automaton({t.id: t}),
                  satire_domains=load_satire_domains())


def score_of(funnel: Funnel, headline: str) -> float:
    funnel.deduper.reset()
    res = funnel.evaluate(
        Item(source_id="t", tier=Tier.MAJOR, title=headline, url="https://e.com/x")
    )
    return max((c.score for c in res.candidates), default=0.0)


def test_generalisation() -> None:
    print("--- GENERALISATION: do Trump-tuned rules work on other people? ---")
    print("    (positives must reach 0.80; negatives must stay below it)\n")

    for full, short, other_first in PEOPLE:
        spec = spec_for(full)
        funnel = build(spec)
        target_min = make_target(spec).fire.min_score

        positives = [
            f"{full} has died at 91, palace confirms",
            f"{full} dies aged 91",
            f"Breaking: {full} is dead",
        ]
        negatives = [
            # appositive — the class that would have fired falsely on Trump
            f"Alan Whitfield, longtime adviser to {full}, dies at 78",
            # condolence
            f"{full} mourns the death of a close friend",
            # future conditional
            f"What happens when {full} dies?",
            # idiom
            f"{full} and rival locked in dead heat as polls close",
            # generic subject
            f"Local man who met {full} dies after long illness",
        ]
        if other_first:
            # surname collision, only meaningful where a surname exists
            negatives.append(f"{other_first} {short} Obituary (2026) - Legacy.com")

        pos_scores = [score_of(funnel, h) for h in positives]
        neg_scores = [score_of(funnel, h) for h in negatives]
        worst_pos, worst_neg = min(pos_scores), max(neg_scores)

        ok = worst_pos >= target_min and worst_neg < target_min
        print(f"{'ok  ' if ok else 'FAIL'} {full:<20} "
              f"positives≥{worst_pos:.3f}  negatives≤{worst_neg:.3f}")
        if not ok:
            failures.append(f"generalisation: {full}")
            for h, s in zip(positives, pos_scores, strict=True):
                if s < target_min:
                    print(f"       MISSED  {s:.3f}  {h}")
            for h, s in zip(negatives, neg_scores, strict=True):
                if s >= target_min:
                    print(f"       FALSE   {s:.3f}  {h}")


def test_normalise() -> None:
    print("\n--- GDELT title normalisation ---")
    cases = [
        ("Jimmy Carter , the world oldest ex - president , dies at 100",
         "Jimmy Carter, the world oldest ex - president, dies at 100"),
        ("Pele Dies at 82 , Brazil Mourns", "Pele Dies at 82, Brazil Mourns"),
        ("U . S . president reacts", "U.S. president reacts"),
    ]
    for raw, want in cases:
        got = normalise(raw)
        check(got == want, "normalise fixes GDELT tokenisation",
              f"{got!r}" if got != want else "")


def test_seendate() -> None:
    print("\n--- seendate parsing ---")
    dt = parse_seendate("20241229T224500Z")
    check(dt is not None and dt.year == 2024 and dt.hour == 22,
          "parses GDELT seendate", str(dt))
    check(parse_seendate("garbage") is None, "rejects malformed seendate")


def test_replay_event() -> None:
    print("\n--- replay_event detect / ignore ---")
    spec = spec_for("Jimmy Carter")
    arts_pos = [
        {"title": "Jimmy Carter dies at 100 , former president",
         "seendate": "20241229T224500Z", "domain": "example.com", "url": "u1"},
        {"title": "Tributes pour in for Jimmy Carter",
         "seendate": "20241229T230000Z", "domain": "example.com", "url": "u2"},
    ]
    from datetime import UTC, datetime
    death = datetime(2024, 12, 29, tzinfo=UTC)
    r = replay_event(spec, arts_pos, "positive", death)
    check(r.detected, "detects a real death headline", f"max={r.max_score:.3f}")
    check(r.lag_minutes is not None and r.lag_minutes > 0,
          "computes lag from the death date",
          f"{r.lag_minutes:.0f}m" if r.lag_minutes else "none")

    arts_neg = [
        {"title": "Jimmy Carter enters hospice care at home",
         "seendate": "20230220T120000Z", "domain": "example.com", "url": "u3"},
        {"title": "Rosalynn Carter , wife of Jimmy Carter , dies at 96",
         "seendate": "20231119T120000Z", "domain": "example.com", "url": "u4"},
    ]
    r2 = replay_event(spec, arts_neg, "negative", None)
    check(not r2.detected,
          "ignores hospice coverage and a spouse's death",
          f"max={r2.max_score:.3f}")


def test_cache_poisoning_guard() -> None:
    print("\n--- cache-poisoning guard (a failed fetch must not persist as []) ---")
    import tools.replay as R

    with tempfile.TemporaryDirectory() as td:
        old = R.CACHE_DIR
        R.CACHE_DIR = Path(td)
        try:
            spec = {"id": "poisoned", "query": "x", "window_hours": 1,
                    "start": "2020-01-01T00:00:00Z"}
            (Path(td) / "poisoned.json").write_text("[]")
            # do_fetch=False, so a healthy cache would be returned as-is.
            got = load_or_fetch(spec, "negative", None, do_fetch=False)
            check(got is None, "empty cache file is discarded, not trusted")
            check(not (Path(td) / "poisoned.json").exists(),
                  "empty cache file is deleted so the next run refetches")
        finally:
            R.CACHE_DIR = old


def test_corpus_wellformed() -> None:
    print("\n--- corpus config is well-formed ---")
    corpus = yaml.safe_load(EVENTS.read_text(encoding="utf-8"))
    pos = corpus.get("positives", [])
    neg = corpus.get("negatives", [])
    check(len(pos) >= 5, "corpus has enough positives", f"{len(pos)}")
    check(len(neg) >= 2, "corpus has negatives", f"{len(neg)}")

    for s in pos + neg:
        for key in ("id", "wikidata_id", "display_name", "aliases", "query"):
            if key not in s:
                check(False, f"{s.get('id','?')} missing {key}")
    check(all("wikidata_id" in s for s in pos + neg),
          "every entry pins a Wikidata id (never a name search)")

    # The mistake this guards against: pre-filtering the query for death terms
    # makes Stage 1 a no-op and the whole backtest meaningless.
    bad = [s["id"] for s in pos + neg
           if any(w in s["query"].lower() for w in ("died", "dies", "dead", "obituary"))]
    check(not bad, "no query pre-filters for death vocabulary", str(bad))
    check(all(s.get("window_hours", 0) >= 24 for s in pos + neg),
          "windows are >=24h (P570 has no time of day)")


def main() -> int:
    test_generalisation()
    test_normalise()
    test_seendate()
    test_replay_event()
    test_cache_poisoning_guard()
    test_corpus_wellformed()

    print()
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all replay/generalisation checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
