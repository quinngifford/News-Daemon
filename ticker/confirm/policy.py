"""Candidate → evidence → decision → alert.

This module owns the fast-path/safety tradeoff you chose: a single tier-0 wire
fires immediately without waiting for adjudication or corroboration, and the
system keeps gathering evidence afterwards so it can retract.

Everything here is deliberate about ordering: notify FIRST on the fast path,
persist and adjudicate after. Writing to SQLite before buzzing the phone would
put a disk fsync inside your latency budget for no benefit.
"""

from __future__ import annotations

import logging
import time

from ticker.adjudicate.llm import Adjudicator
from ticker.config import Target
from ticker.confirm.evidence import TargetAccumulator
from ticker.models import Alert, Candidate, Evidence, Stage, TargetState, Tier, now_ns
from ticker.notify.dispatcher import Dispatcher
from ticker.screen import rules

log = logging.getLogger(__name__)


class Confirmer:
    def __init__(
        self,
        targets: dict[str, Target],
        dispatcher: Dispatcher,
        adjudicator: Adjudicator | None = None,
        store=None,
    ) -> None:
        self.targets = targets
        self.dispatcher = dispatcher
        self.adjudicator = adjudicator
        self.store = store
        self.accumulators: dict[str, TargetAccumulator] = {
            tid: TargetAccumulator(target_id=tid, policy=t.fire)
            for tid, t in targets.items()
        }
        self.stats = {"evidence": 0, "fast_path": 0, "adjudicated": 0, "alerts": 0}

    async def handle(self, cand: Candidate) -> None:
        target = self.targets[cand.target_id]
        acc = self.accumulators[cand.target_id]

        ev = Evidence(
            target_id=cand.target_id,
            source_id=cand.item.source_id,
            tier=cand.item.tier,
            weight=cand.score,
            url=cand.item.url,
            headline=cand.item.title,
            attributed_to=rules.find_attribution(cand.item.text),
            negative=self._is_retraction(cand),
        )

        # --- FAST PATH: tier-0 wire, strong score, alert before anything else ---
        if (
            acc.should_fast_path(ev)
            and cand.score >= target.fire.min_score
            and not ev.negative
        ):
            acc.add(ev)
            self.stats["evidence"] += 1
            self.stats["fast_path"] += 1
            acc.state = TargetState.CONFIRMED
            await self._fire(target, acc, cand, TargetState.CONFIRMED)
            # Adjudicate afterwards purely for the audit trail and for the
            # retraction decision — the alert has already left.
            await self._maybe_adjudicate(cand, target, post_hoc=True)
            return

        # --- NORMAL PATH: adjudicate before it can contribute full weight ---
        verdict = await self._maybe_adjudicate(cand, target, post_hoc=False)
        if verdict is not None:
            if not verdict.died:
                log.info(
                    "adjudicator rejected %s: %s (subject=%s)",
                    cand.target_id, verdict.reason, verdict.subject,
                )
                return
            # Trust the model's confidence over the rule score once it has run.
            ev.weight = max(cand.score, verdict.confidence)

        acc.add(ev)
        self.stats["evidence"] += 1
        if self.store:
            self.store.record_evidence(ev)

        before = acc.state
        after = acc.evaluate()
        if after is not before and after in (
            TargetState.LIKELY,
            TargetState.CONFIRMED,
            TargetState.RETRACTED,
        ):
            await self._fire(target, acc, cand, after)

    def _is_retraction(self, cand: Candidate) -> bool:
        # Shared with the funnel, which uses it to bypass the score floor so the
        # retraction can reach us at all. See rules.is_retraction().
        return rules.is_retraction(cand.item.text)

    async def _maybe_adjudicate(
        self, cand: Candidate, target: Target, *, post_hoc: bool
    ):
        if self.adjudicator is None or cand.score < target.fire.llm_min_score:
            return None
        verdict = await self.adjudicator.adjudicate(cand, target.display_name)
        self.stats["adjudicated"] += 1
        if self.store:
            self.store.record_adjudication(cand, verdict)
        log.info(
            "adjudicated %s: died=%s conf=%.2f cost=$%.5f %s%s",
            cand.target_id, verdict.died, verdict.confidence, verdict.cost_usd,
            verdict.reason, " (post-hoc)" if post_hoc else "",
        )
        return verdict

    async def _fire(
        self,
        target: Target,
        acc: TargetAccumulator,
        cand: Candidate,
        state: TargetState,
    ) -> None:
        # Reuse the event_id across state transitions so the notifier's
        # idempotency and the PWA's notification `tag` both collapse correctly:
        # a CONFIRMED then RETRACTED pair is one story, not two.
        alert = Alert(
            target_id=target.display_name,
            state=state,
            headline=cand.item.title,
            url=cand.item.url,
            score=cand.score,
            evidence=list(acc.items),
            detect_latency_ms=(now_ns() - cand.item.t_ingest_ns) / 1e6,
        )
        if acc.fired_event_id:
            alert.event_id = acc.fired_event_id
        else:
            acc.fired_event_id = alert.event_id

        await self.dispatcher.dispatch(alert)
        self.stats["alerts"] += 1
        if self.store:
            self.store.record_alert(alert, weight=acc.weight(), trail=acc.trail())

    def status(self) -> dict:
        now = time.time()
        return {
            tid: {
                "state": acc.state.value,
                "weight": round(acc.weight(now), 3),
                "origins": sorted(acc.independent_origins(now)),
                "evidence_count": len(acc.items),
            }
            for tid, acc in self.accumulators.items()
        }
