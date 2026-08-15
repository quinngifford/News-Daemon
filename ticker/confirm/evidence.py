"""Weighted, independence-aware corroboration.

Two ideas do the real work here:

1. **Attribution collapsing.** "Sky News, citing AP, reports..." is not
   independent evidence — it is the AP datapoint again. Crediting the cited
   origin instead of the republisher is what stops one rumour echoed by thirty
   outlets from looking like overwhelming consensus. That exact failure mode is
   behind every historical fake-death market spike.

2. **Time decay.** Real events produce a burst of reports; rumours produce a
   trickle. Requiring evidence to *cluster* in a window encodes that difference
   for free.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ticker.config import FirePolicy
from ticker.models import Evidence, TargetState, Tier

# Weight a single datapoint contributes at full strength (before decay).
TIER_WEIGHTS: dict[Tier, float] = {
    Tier.WIRE: 1.00,        # alone crosses the threshold → fast path
    Tier.MAJOR: 0.50,       # two independent majors → 1.00
    Tier.REGIONAL: 0.20,
    Tier.SOCIAL: 0.10,
    Tier.STRUCTURAL: 0.40,  # unambiguous but vandalizable; never fires alone
}

# Total contribution any one tier may make, no matter how many items arrive.
# Without this, a Reddit brigade could manufacture a confirmation.
TIER_CAPS: dict[Tier, float] = {
    Tier.SOCIAL: 0.20,
    Tier.REGIONAL: 0.40,
    Tier.STRUCTURAL: 0.40,
}

# Fraction of the decay window during which evidence holds full weight.
FRESH_PLATEAU_FRAC = 0.25

# Guard against float drift when a weight lands exactly on a threshold.
EPS = 1e-9

# Republisher name → canonical origin id, for attribution collapsing.
ORIGIN_ALIASES: dict[str, str] = {
    "ap": "ap",
    "associated press": "ap",
    "the associated press": "ap",
    "reuters": "reuters",
    "afp": "afp",
    "agence france-presse": "afp",
    "bloomberg": "bloomberg",
    "the new york times": "nyt",
    "new york times": "nyt",
    "nyt": "nyt",
    "washington post": "wapo",
    "the washington post": "wapo",
    "bbc": "bbc",
    "cnn": "cnn",
    "npr": "npr",
    "white house": "official-us",
    "federal register": "official-us",
}


def canonical_origin(name: str | None) -> str | None:
    if not name:
        return None
    return ORIGIN_ALIASES.get(name.strip().lower())


# Origins that are wire-grade regardless of which outlet carried the copy.
# This matters more than it looks: AP and Reuters both retired their public RSS
# feeds, so there is no longer a free tier-0 *news* feed to subscribe to. The
# practical route to wire copy is an aggregator (Google News) that reprints it
# with attribution — so we recover the tier by parsing the attribution rather
# than by trusting the transport.
WIRE_ORIGINS = frozenset({"ap", "reuters", "afp", "bloomberg", "official-us"})


def effective_tier(ev: Evidence) -> Tier:
    """Promote aggregator-carried wire copy to its true tier.

    "Sky News, citing AP, reports..." arriving over a tier-2 aggregator is an AP
    datapoint, and should be weighted (and fast-pathed) as one.
    """
    origin = canonical_origin(ev.attributed_to)
    if origin in WIRE_ORIGINS:
        return Tier.WIRE
    return ev.tier


@dataclass(slots=True)
class TargetAccumulator:
    """Running evidence total for one target."""

    target_id: str
    policy: FirePolicy
    state: TargetState = TargetState.QUIET
    items: list[Evidence] = field(default_factory=list)
    fired_event_id: str | None = None

    def add(self, ev: Evidence) -> None:
        self.items.append(ev)

    def _quality(self, score: float) -> float:
        """Map a Stage-2 score to a contribution multiplier that SATURATES at 1.0.

        Tier weight answers "do I trust this source"; the rule score answers "is
        this item about the right thing". The second is a gate, not a discount —
        multiplying them conflates two different questions and silently makes the
        documented thresholds unreachable.

        Concretely: TIER_WEIGHTS[MAJOR] is 0.50 precisely so that two independent
        majors sum to 1.00 and confirm. Multiplying by a raw score of ~0.97 gives
        0.97, so that path could never fire — and with AP and Reuters no longer
        offering public RSS, two-independent-majors is the PRIMARY confirmation
        route. Saturating at min_score keeps the documented arithmetic exact
        while still discounting genuinely marginal items.
        """
        floor = max(self.policy.min_score, 0.01)
        return min(1.0, score / floor)

    def _decay(self, age_s: float) -> float:
        """Full weight while fresh, then linear decay to zero at the window edge.

        The plateau is not cosmetic. Without it, decay returns 0.99999... for any
        nonzero age, so an evidence set designed to total exactly 1.00 (two
        independent majors) lands microscopically short and never crosses the
        threshold — the thresholds become unreachable by construction.

        It is also just more defensible: a report thirty seconds old is not less
        credible than one a second old. Credibility should start eroding only
        once the absence of corroboration becomes meaningful.
        """
        plateau_s = self.policy.decay_window_s * FRESH_PLATEAU_FRAC
        if age_s <= plateau_s:
            return 1.0
        if age_s >= self.policy.decay_window_s:
            return 0.0
        return 1.0 - (age_s - plateau_s) / (self.policy.decay_window_s - plateau_s)

    def weight(self, now: float | None = None) -> float:
        """Current corroboration weight, deduplicated by origin and capped."""
        now = now if now is not None else time.time()

        # Best (highest) contribution per independent origin. Two stories from
        # the same origin are one datapoint, not two.
        per_origin: dict[str, tuple[float, Tier]] = {}
        negatives = 0.0

        for ev in self.items:
            decay = self._decay(now - ev.t_wall)
            if decay <= 0:
                continue
            tier = effective_tier(ev)
            base = TIER_WEIGHTS.get(tier, 0.1) * self._quality(ev.weight) * decay
            if ev.negative:
                negatives += base
                continue
            origin = canonical_origin(ev.attributed_to) or ev.origin
            prev = per_origin.get(origin)
            if prev is None or base > prev[0]:
                per_origin[origin] = (base, tier)

        # Apply per-tier caps across origins.
        by_tier: dict[Tier, float] = {}
        for w, tier in per_origin.values():
            by_tier[tier] = by_tier.get(tier, 0.0) + w

        total = 0.0
        for tier, w in by_tier.items():
            cap = TIER_CAPS.get(tier)
            total += min(w, cap) if cap is not None else w

        return max(0.0, total - negatives * 1.5)  # retractions bite harder than reports

    def independent_origins(self, now: float | None = None) -> set[str]:
        now = now if now is not None else time.time()
        return {
            canonical_origin(ev.attributed_to) or ev.origin
            for ev in self.items
            if not ev.negative and self._decay(now - ev.t_wall) > 0
        }

    def evaluate(self, now: float | None = None) -> TargetState:
        """Recompute state. Monotonic upward except for explicit retraction."""
        w = self.weight(now)
        p = self.policy

        if self.state is TargetState.CONFIRMED:
            # Only an explicit collapse below the watch line retracts a fire.
            if p.allow_retraction and w < p.watch_weight:
                self.state = TargetState.RETRACTED
            return self.state

        if w >= p.confirm_weight - EPS:
            self.state = TargetState.CONFIRMED
        elif w >= p.likely_weight - EPS:
            self.state = TargetState.LIKELY
        elif w >= p.watch_weight - EPS:
            self.state = TargetState.WATCH
        else:
            self.state = TargetState.QUIET
        return self.state

    def should_fast_path(self, ev: Evidence) -> bool:
        """A tier-0 wire headline needs no adjudication — seconds are the edge.

        Deliberate tradeoff, per docs/ARCHITECTURE.md §5: alert now, keep
        corroborating, retract if it collapses.
        """
        return (
            self.policy.tier0_instant
            and effective_tier(ev) is Tier.WIRE
            and not ev.negative
            and self.state is not TargetState.CONFIRMED
        )

    def trail(self, now: float | None = None) -> list[str]:
        """Auditable one-line-per-datapoint summary for the alert payload."""
        now = now if now is not None else time.time()
        out = []
        for ev in sorted(self.items, key=lambda e: e.t_wall):
            age = now - ev.t_wall
            flag = "RETRACT" if ev.negative else f"T{int(ev.tier)}"
            attrib = f" (via {ev.attributed_to})" if ev.attributed_to else ""
            out.append(f"[{flag}] {ev.source_id}{attrib} +{age:.0f}s — {ev.headline[:90]}")
        return out
