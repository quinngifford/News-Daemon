"""Stage 1 — Aho-Corasick multi-pattern gate.

This is the mechanism that makes "screen every article for free" true rather
than aspirational. One automaton holds every pattern for every target; it scans
input in O(n) *independent of pattern count*, so target #50 costs no more scan
time than target #1.

Aho-Corasick matches raw substrings, so "Trump" hits inside "Trumpism" and
"dead" hits inside "deadline". Word-boundary filtering after the scan is
therefore not optional — it is the difference between this stage working and
this stage being noise.
"""

from __future__ import annotations

from pathlib import Path

import ahocorasick

from ticker.models import Match

KIND_ALIAS = "alias"
KIND_DEATH = "death"
KIND_NEGATION = "negation"
KIND_IDIOM = "idiom"
KIND_CONDOLENCE = "condolence"

# Characters that may sit adjacent to a match without breaking word-hood.
# Apostrophe is included so "Trump's" still matches alias "Trump".
_BOUNDARY_OK = set(" \t\n\r\f\v.,;:!?\"'()[]{}<>«»—–-/\\|@#*&+=~`$%^")


def _is_boundary(text: str, idx: int) -> bool:
    if idx < 0 or idx >= len(text):
        return True
    return text[idx] in _BOUNDARY_OK


def _whole_word(text: str, start: int, end: int, term: str) -> bool:
    """True when text[start:end] is delimited on both sides.

    A pattern whose own edge is punctuation supplies its own delimiter, so we
    must not also demand one from the text. Without this, the obituary
    date-range tell "1946-" can never match "1946-2026", because the character
    after the pattern is a digit.
    """
    left_ok = term[0] in _BOUNDARY_OK or _is_boundary(text, start - 1)
    right_ok = term[-1] in _BOUNDARY_OK or _is_boundary(text, end)
    return left_ok and right_ok


class Automaton:
    """Compiled pattern set shared by all targets."""

    def __init__(self) -> None:
        self._a = ahocorasick.Automaton()
        self._n = 0
        self._built = False

    def add(self, kind: str, term: str, target_id: str | None = None) -> None:
        if self._built:
            raise RuntimeError("cannot add patterns after build()")
        key = term.lower().strip()
        if not key:
            return
        # Several (kind, target) payloads can share one surface form — e.g. the
        # alias "Trump" for two different targets. Store a list per key.
        existing = self._a.get(key, None)
        payload = (kind, term, target_id)
        if existing is None:
            self._a.add_word(key, [payload])
        elif payload not in existing:
            existing.append(payload)
        self._n += 1

    def add_lexicon(self, kind: str, path: Path, target_id: str | None = None) -> int:
        """Load one term per line; '#' comments and blanks ignored."""
        count = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                self.add(kind, line, target_id)
                count += 1
        return count

    def build(self) -> Automaton:
        self._a.make_automaton()
        self._built = True
        return self

    @property
    def pattern_count(self) -> int:
        return self._n

    def scan(self, text: str) -> list[Match]:
        """Single O(len(text)) pass. Returns whole-word matches only."""
        if not self._built:
            raise RuntimeError("call build() before scan()")
        low = text.lower()
        out: list[Match] = []
        for end_idx, payloads in self._a.iter(low):
            for kind, term, target_id in payloads:
                key = term.lower().strip()
                start = end_idx - len(key) + 1
                if _whole_word(low, start, end_idx + 1, key):
                    out.append(
                        Match(
                            kind=kind,
                            term=term,
                            start=start,
                            end=end_idx + 1,
                            target_id=target_id,
                        )
                    )
        return out


def suppress_covered_aliases(matches: list[Match]) -> list[Match]:
    """Drop alias hits that sit *inside* a longer target-specific negation phrase.

    "Fred Trump died in 1999" must not register as a hit on target `trump`: the
    alias "Trump" is wholly covered by the negation phrase "Fred Trump". Same
    mechanism kills "Trump Tower", "Trump Organization", "Ivana Trump".

    This is the one false-positive class the proximity/other-person heuristics
    in rules.py cannot catch, because the confounding name *overlaps* the alias
    rather than sitting beside it.
    """
    negs = [m for m in matches if m.kind == KIND_NEGATION]
    if not negs:
        return matches
    out: list[Match] = []
    for m in matches:
        if m.kind == KIND_ALIAS and any(
            n.target_id in (None, m.target_id) and n.start <= m.start and n.end >= m.end
            for n in negs
        ):
            continue
        out.append(m)
    return out


def targets_hit(matches: list[Match]) -> set[str]:
    """Target ids that had BOTH an alias and a death term in the same item.

    This conjunction is the whole gate: an alias alone is a normal news day,
    a death term alone is somebody else's obituary.
    """
    aliases = {m.target_id for m in matches if m.kind == KIND_ALIAS and m.target_id}
    has_death = any(m.kind == KIND_DEATH for m in matches)
    return aliases if has_death else set()
