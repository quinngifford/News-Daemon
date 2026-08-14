"""Core data types.

Every item carries monotonic timestamps from the moment of ingest so that
per-hop latency is measurable for the life of the project. See
docs/ARCHITECTURE.md §1 — you cannot tune what you do not measure.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


def now_ns() -> int:
    """Monotonic nanoseconds. Never wall-clock for latency math."""
    return time.monotonic_ns()


class Tier(int, Enum):
    WIRE = 0        # AP, official government proclamation. Can fire alone.
    MAJOR = 1       # NYT, BBC, WaPo, CNN, Guardian...
    REGIONAL = 2    # regional outlets, aggregators
    SOCIAL = 3      # Reddit, HN, X. Never fires alone; capped contribution.
    STRUCTURAL = 4  # Wikidata P570, Wikipedia death date. Unambiguous but vandalizable.


class Stage(str, Enum):
    DEDUPE = "dedupe"
    AUTOMATON = "automaton"
    RULES = "rules"
    LLM = "llm"
    CONFIRM = "confirm"


class TargetState(str, Enum):
    QUIET = "quiet"
    WATCH = "watch"
    LIKELY = "likely"
    CONFIRMED = "confirmed"
    RETRACTED = "retracted"


@dataclass(slots=True)
class Item:
    """One unit of news: a headline, a tweet, a wiki edit."""

    source_id: str
    tier: Tier
    title: str
    body: str = ""
    url: str = ""
    t_source: float | None = None            # publisher epoch seconds, if provided
    t_ingest_ns: int = field(default_factory=now_ns)
    raw: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}" if self.body else self.title

    def age_ms(self) -> float:
        return (now_ns() - self.t_ingest_ns) / 1e6


@dataclass(slots=True)
class Match:
    """A single automaton hit, with byte span for proximity math."""

    kind: str          # alias | death | negation | idiom | condolence | satire
    term: str
    start: int
    end: int
    target_id: str | None = None


@dataclass(slots=True)
class Candidate:
    """An item that survived at least the automaton stage, plus its scoring."""

    item: Item
    target_id: str
    score: float = 0.0
    features: dict[str, float] = field(default_factory=dict)
    matches: list[Match] = field(default_factory=list)
    killed_at: Stage | None = None
    reason: str = ""

    @property
    def alive(self) -> bool:
        return self.killed_at is None


@dataclass(slots=True)
class Evidence:
    """One datapoint supporting (or undermining) a target event."""

    target_id: str
    source_id: str
    tier: Tier
    weight: float
    url: str
    headline: str
    t_wall: float = field(default_factory=time.time)
    attributed_to: str | None = None   # set when this republishes another source
    negative: bool = False             # retraction / correction

    @property
    def origin(self) -> str:
        """Who actually observed this. Collapses republishers onto their source."""
        return self.attributed_to or self.source_id


@dataclass(slots=True)
class Alert:
    target_id: str
    state: TargetState
    headline: str
    url: str
    score: float
    evidence: list[Evidence] = field(default_factory=list)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    t_wall: float = field(default_factory=time.time)
    detect_latency_ms: float | None = None
