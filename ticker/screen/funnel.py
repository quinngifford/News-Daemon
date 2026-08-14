"""The screening cascade: Stage 0 → 1 → 2, orchestrated.

Each stage is ~100x more expensive and ~100x rarer than the one before it. The
whole point is that nothing here costs money per item, so the LLM in
ticker/adjudicate/ only ever sees the handful of items that survive.

`evaluate()` returns the full picture including *why* something died; `process()`
is the hot-path convenience wrapper that returns only survivors. Tests and
tools/replay.py use `evaluate()`, because "which stage killed the true positive
that got away" is the question that actually tunes this system.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from ticker.config import Target
from ticker.models import Candidate, Item, Stage
from ticker.screen import rules
from ticker.screen.automaton import Automaton, suppress_covered_aliases, targets_hit
from ticker.screen.dedupe import Deduper


@dataclass(slots=True)
class FunnelResult:
    item: Item
    kill_stage: Stage | None = None            # set when killed before scoring
    candidates: list[Candidate] = field(default_factory=list)  # alive AND killed

    @property
    def survivors(self) -> list[Candidate]:
        return [c for c in self.candidates if c.alive]

    def describe(self) -> str:
        if self.kill_stage:
            return f"killed at {self.kill_stage.value}"
        if not self.candidates:
            return "no target matched"
        best = max(self.candidates, key=lambda c: c.score)
        verdict = "SURVIVED" if best.alive else f"killed at {Stage.RULES.value}"
        return f"{verdict} score={best.score:.3f} [{best.reason}]"


class Funnel:
    def __init__(
        self,
        targets: dict[str, Target],
        automaton: Automaton,
        deduper: Deduper | None = None,
        satire_domains: frozenset[str] = frozenset(),
    ) -> None:
        self.targets = targets
        self.automaton = automaton
        self.deduper = deduper or Deduper()
        self.satire_domains = satire_domains
        self.stats = {
            "items": 0,
            "killed_dedupe": 0,
            "killed_automaton": 0,
            "killed_rules": 0,
            "candidates": 0,
            "retractions": 0,
        }

    def _is_satire(self, url: str) -> bool:
        if not url or not self.satire_domains:
            return False
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        return any(host == d or host.endswith("." + d) for d in self.satire_domains)

    def evaluate(self, item: Item, *, use_dedupe: bool = True) -> FunnelResult:
        """Run one item through the cascade and report everything that happened."""
        self.stats["items"] += 1
        res = FunnelResult(item=item)

        # --- Stage 0: near-duplicate suppression -------------------------
        # Key on the TITLE, not title+body. Live data showed the same wire
        # headline arriving from several feeds with different summaries, which
        # defeated SimHash on the combined text and produced duplicate
        # candidates — i.e. duplicate LLM calls for one story. Screening still
        # uses the full text; only the dedupe key is narrowed.
        if use_dedupe and self.deduper.check_and_add(item.title):
            self.stats["killed_dedupe"] += 1
            res.kill_stage = Stage.DEDUPE
            return res

        # --- Stage 1: one O(n) automaton pass over all patterns ----------
        matches = suppress_covered_aliases(self.automaton.scan(item.text))
        hit_ids = targets_hit(matches)
        if not hit_ids:
            self.stats["killed_automaton"] += 1
            res.kill_stage = Stage.AUTOMATON
            return res

        # --- Stage 2: disambiguation, per target that was hit ------------
        satire = self._is_satire(item.url)
        for target_id in sorted(hit_ids):
            target = self.targets.get(target_id)
            if target is None:
                continue

            # Void alias hits that are a different person sharing the surname
            # ("Larry Trump Obituary"). If nothing survives, this item was never
            # about our target at all.
            alias_terms = {a.lower() for a in target.aliases}
            foreign = rules.foreign_given_name_spans(
                item.text, matches, target_id, alias_terms
            )
            if foreign:
                matches_t = [
                    m for m in matches
                    if not (m.kind == "alias" and m.target_id == target_id
                            and (m.start, m.end) in foreign)
                ]
                if not any(m.kind == "alias" and m.target_id == target_id
                           for m in matches_t):
                    self.stats["killed_rules"] += 1
                    continue
            else:
                matches_t = matches

            features = rules.extract_features(
                item.text,
                matches_t,
                target_id,
                proximity_window=target.proximity_window,
                strong_aliases=target.strong_alias_set,
                satire_source=satire,
            )
            score = rules.score(features)
            cand = Candidate(
                item=item,
                target_id=target_id,
                score=score,
                features=features,
                matches=[m for m in matches_t if m.target_id in (None, target_id)],
                reason=rules.explain(features),
            )
            # Retractions bypass the score floor. They read as false positives
            # to the rules ("was false" trips the threat pattern), so filtering
            # them on score would suppress corrections to bad fires — leaving a
            # false CONFIRMED standing with no way to walk it back.
            retraction = rules.is_retraction(item.text)

            # Below this floor it is not even worth an LLM call. The policy's
            # min_score decides what may actually fire; see confirm/policy.py.
            if score < target.fire.llm_min_score and not retraction:
                cand.killed_at = Stage.RULES
                self.stats["killed_rules"] += 1
            else:
                if retraction:
                    cand.reason = f"RETRACTION (floor bypassed), {cand.reason}"
                    self.stats["retractions"] += 1
                self.stats["candidates"] += 1
            res.candidates.append(cand)
        return res

    def process(self, item: Item) -> list[Candidate]:
        """Hot path: survivors only."""
        return self.evaluate(item).survivors

    def report(self) -> str:
        st = self.stats
        n = max(st["items"], 1)
        return (
            f"items={st['items']} "
            f"dedupe={st['killed_dedupe']} ({st['killed_dedupe'] / n:.1%}) "
            f"automaton={st['killed_automaton']} ({st['killed_automaton'] / n:.1%}) "
            f"rules={st['killed_rules']} "
            f"candidates={st['candidates']}"
        )
