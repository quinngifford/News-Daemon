"""Configuration loading: targets, lexicons, sources, settings.

A target is a YAML file so that adding a new person to watch never requires a
code change — see config/targets/trump.yaml for the annotated reference.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ticker.screen.automaton import (
    KIND_ALIAS,
    KIND_CONDOLENCE,
    KIND_DEATH,
    KIND_IDIOM,
    KIND_NEGATION,
    Automaton,
)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
LEXICON_DIR = CONFIG_DIR / "lexicons"
TARGET_DIR = CONFIG_DIR / "targets"


@dataclass(slots=True)
class FirePolicy:
    tier0_instant: bool = True       # a single wire headline alerts immediately
    min_score: float = 0.80          # Stage-2 score floor before anything proceeds
    llm_min_score: float = 0.45      # below this, not even worth adjudicating
    confirm_weight: float = 1.00     # accumulator threshold for CONFIRMED
    watch_weight: float = 0.20
    likely_weight: float = 0.50
    decay_window_s: float = 900.0    # evidence older than this stops counting
    allow_retraction: bool = True


@dataclass(slots=True)
class Target:
    id: str
    display_name: str
    aliases: list[str]
    strong_aliases: list[str] = field(default_factory=list)
    birth_year: int | None = None
    # Explicit entity ids for the structural oracles. Wikidata events carry
    # titles like "Q22686", not names, so without this a P570 write cannot be
    # attributed to anyone. See ingest/wikimedia_sse.py.
    wikidata_id: str | None = None
    wikipedia_title: str | None = None
    proximity_window: int = 12
    extra_death_terms: list[str] = field(default_factory=list)
    extra_negations: list[str] = field(default_factory=list)
    fire: FirePolicy = field(default_factory=FirePolicy)
    markets: list[dict] = field(default_factory=list)
    enabled: bool = True

    @property
    def strong_alias_set(self) -> frozenset[str]:
        return frozenset(a.lower() for a in self.strong_aliases)


def load_targets(target_dir: Path = TARGET_DIR) -> dict[str, Target]:
    targets: dict[str, Target] = {}
    for path in sorted(target_dir.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        fire = FirePolicy(**(raw.pop("fire_policy", {}) or {}))
        t = Target(fire=fire, **raw)
        if t.enabled:
            targets[t.id] = t
    return targets


def build_automaton(targets: dict[str, Target], lexicon_dir: Path = LEXICON_DIR) -> Automaton:
    """Compile one automaton covering every target and every shared lexicon.

    Cost is O(total pattern length) once at startup; scanning afterwards is
    independent of how many targets are registered.
    """
    a = Automaton()

    a.add_lexicon(KIND_DEATH, lexicon_dir / "death_terms.txt")
    a.add_lexicon(KIND_IDIOM, lexicon_dir / "idioms.txt")
    a.add_lexicon(KIND_CONDOLENCE, lexicon_dir / "condolence.txt")

    for t in targets.values():
        for alias in t.aliases:
            a.add(KIND_ALIAS, alias, target_id=t.id)
        for term in t.extra_death_terms:
            a.add(KIND_DEATH, term, target_id=t.id)
        # Phrases that CONTAIN an alias but refer to something else entirely
        # ("Fred Trump", "Trump Tower"). suppress_covered_aliases() uses these
        # to void the inner alias hit.
        for term in t.extra_negations:
            a.add(KIND_NEGATION, term, target_id=t.id)
        # "1946-" style obituary date ranges are a strong structural tell
        if t.birth_year:
            for dash in ("-", "–", "—"):
                a.add(KIND_DEATH, f"{t.birth_year}{dash}", target_id=t.id)

    return a.build()


def load_satire_domains(lexicon_dir: Path = LEXICON_DIR) -> frozenset[str]:
    path = lexicon_dir / "satire_domains.txt"
    if not path.exists():
        return frozenset()
    return frozenset(
        line.split("#", 1)[0].strip().lower()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.split("#", 1)[0].strip()
    )


def load_settings(path: Path = CONFIG_DIR / "settings.toml") -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_sources(path: Path = CONFIG_DIR / "sources.yaml") -> list[dict]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return raw.get("sources", [])
