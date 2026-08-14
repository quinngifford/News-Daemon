"""Stage 3 — LLM adjudication for the handful of items that survive screening.

The economics inversion that makes this affordable: the model never sees routine
volume. Expected traffic is single-digit calls per day, so you can afford real
judgment on exactly the cases where rules are weakest — and the bill stays in
the cents-per-month range regardless of how busy the news cycle is.

Two non-negotiables:
  * A hard spend ceiling, so a lexicon mistake or a retry loop cannot run up a
    bill while you are asleep.
  * Fail OPEN to the Stage-2 score. Adjudication being unavailable must never
    mean notification being unavailable — that would turn an API outage into a
    missed event.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from anthropic import AsyncAnthropic

from ticker.models import Candidate

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a strict verification filter in a breaking-news pipeline. You are given \
one news item and the name of a person being monitored.

Decide whether the item asserts, as current fact, that THIS SPECIFIC PERSON has \
died. Be ruthless about these traps:
  - the person is reacting to, mourning, or commemorating someone else's death
  - hypothetical, conditional, or "what if" framings
  - figurative death (a bill, a campaign, an ideology, a career)
  - threats, attempts, injuries, or survived incidents
  - satire, fiction, rumour, or an item debunking a death claim
  - a different person who shares part of the name

Answer with ONLY a JSON object, no prose:
{"died": true|false, "confidence": 0.0-1.0, "subject": "<who the item is about>", \
"reason": "<one short clause>"}"""


@dataclass
class Verdict:
    died: bool
    confidence: float
    subject: str
    reason: str
    latency_ms: float
    cost_usd: float
    fell_back: bool = False


class Adjudicator:
    def __init__(
        self,
        client: AsyncAnthropic,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 300,
        timeout_s: float = 6.0,
        monthly_budget_usd: float = 2.00,
        # Prices change; keep them in config rather than hardcoded in logic.
        usd_per_mtok_in: float = 1.00,
        usd_per_mtok_out: float = 5.00,
        fail_open: bool = True,
    ) -> None:
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.monthly_budget_usd = monthly_budget_usd
        self.usd_per_mtok_in = usd_per_mtok_in
        self.usd_per_mtok_out = usd_per_mtok_out
        self.fail_open = fail_open
        self.spent_usd = 0.0
        self.calls = 0
        self._period = time.gmtime().tm_mon

    def _roll_period(self) -> None:
        month = time.gmtime().tm_mon
        if month != self._period:
            self._period = month
            self.spent_usd = 0.0

    def _fallback(self, cand: Candidate, why: str) -> Verdict:
        """Defer to the Stage-2 score rather than blocking the pipeline."""
        log.warning("adjudicator falling back (%s) for %s", why, cand.target_id)
        return Verdict(
            died=cand.score >= 0.80,
            confidence=cand.score,
            subject=cand.target_id,
            reason=f"fallback:{why}",
            latency_ms=0.0,
            cost_usd=0.0,
            fell_back=True,
        )

    async def adjudicate(self, cand: Candidate, display_name: str) -> Verdict:
        self._roll_period()
        if self.spent_usd >= self.monthly_budget_usd:
            return self._fallback(cand, "budget")

        prompt = (
            f"Person monitored: {display_name}\n"
            f"Source: {cand.item.source_id} (tier {int(cand.item.tier)})\n"
            f"URL: {cand.item.url}\n"
            f"Item:\n{cand.item.text[:1500]}"
        )
        t0 = time.monotonic()
        try:
            resp = await self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                timeout=self.timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 — never let this stall a fire
            return self._fallback(cand, type(exc).__name__)

        latency_ms = (time.monotonic() - t0) * 1000
        cost = (
            resp.usage.input_tokens / 1e6 * self.usd_per_mtok_in
            + resp.usage.output_tokens / 1e6 * self.usd_per_mtok_out
        )
        self.spent_usd += cost
        self.calls += 1

        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        try:
            # Tolerate a stray fence even though the prompt forbids prose.
            if text.startswith("```"):
                text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
            data = json.loads(text)
        except (json.JSONDecodeError, IndexError):
            return self._fallback(cand, "unparseable_response")

        return Verdict(
            died=bool(data.get("died")),
            confidence=float(data.get("confidence", 0.0)),
            subject=str(data.get("subject", "")),
            reason=str(data.get("reason", ""))[:200],
            latency_ms=latency_ms,
            cost_usd=cost,
        )

    def stats(self) -> dict:
        return {
            "calls": self.calls,
            "spent_usd": round(self.spent_usd, 4),
            "budget_usd": self.monthly_budget_usd,
        }
