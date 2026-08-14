"""Wikimedia EventStreams — free, push, and one of the best oracles available.

An edit that adds a death date to the target's article, or writes Wikidata P570
(date of death), is a near-real-time and *structurally unambiguous* signal.
Editors are fast; they frequently beat outlet homepages.

Throughput note that matters on this box: `recentchange` carries ~40-500
events/second all day (measured: ~44/s). Calling json.loads() on every one would
burn a meaningful slice of 2 vCPUs for nothing, so we apply the funnel's own
philosophy one layer earlier — a raw substring pre-filter on the undecoded line
before paying for JSON parsing. Measured: 1,321 of 1,323 events skipped.

Two bugs found by running this against the live firehose, both worth keeping in
mind if you touch it:

  1. Needles must be lowercased. `_worth_parsing` lowercases the line, so a
     needle of b"P570" could never match — silently disabling the single most
     valuable structural signal while looking perfectly healthy.
  2. Wikidata events carry titles like "Q22686", not "Donald Trump". Matching a
     P570 write is useless unless you can say WHOSE. Hence the explicit
     wikidata_id / wikipedia_title mapping in the target config, and the
     synthesized screenable text below.
"""

from __future__ import annotations

import json
import logging
import time

import httpx

from ticker.config import Target
from ticker.ingest.base import Emit, SourceAdapter
from ticker.models import Item

log = logging.getLogger(__name__)

P_DATE_OF_DEATH = "p570"          # lowercase: compared against a lowercased line
DEATH_CATEGORY_HINT = "deaths"    # "Category:2026 deaths"


class WikimediaSseAdapter(SourceAdapter):
    def __init__(
        self,
        config: dict,
        client: httpx.AsyncClient,
        targets: dict[str, Target],
    ) -> None:
        super().__init__(config)
        self.client = client
        self.url: str = config.get(
            "url", "https://stream.wikimedia.org/v2/stream/recentchange"
        )
        self.wikis: set[str] = set(config.get("wikis", ["enwiki", "wikidatawiki"]))

        # Explicit entity → target maps. Exact matching here avoids the
        # "Donald Trump Jr." class of collision that loose alias matching invites.
        self.by_qid: dict[str, Target] = {
            t.wikidata_id.lower(): t for t in targets.values() if t.wikidata_id
        }
        self.by_page: dict[str, Target] = {
            (t.wikipedia_title or t.display_name).lower(): t for t in targets.values()
        }

        needles = {P_DATE_OF_DEATH.encode(), DEATH_CATEGORY_HINT.encode()}
        needles |= {q.encode() for q in self.by_qid}
        needles |= {p.encode() for p in self.by_page}
        needles |= {
            a.lower().encode()
            for t in targets.values()
            for a in t.aliases
            if len(a) >= 4
        }
        # Sorted by length so the cheapest, most selective checks run first.
        self.needles: list[bytes] = sorted(needles, key=len)

        self.parsed = 0
        self.skipped = 0
        self.matched = 0
        self.last_raw_at = time.monotonic()

    def _worth_parsing(self, raw: bytes) -> bool:
        low = raw.lower()
        return any(n in low for n in self.needles)

    async def _run(self, emit: Emit) -> None:
        headers = {"Accept": "text/event-stream", "User-Agent": "news-ticker-daemon/0.1"}
        # No read timeout: this stream is meant to stay open indefinitely. The
        # staleness watchdog in ops/health.py detects a silent stall — and it
        # correctly caught this adapter delivering nothing during the first
        # live dry-run.
        timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
        async with self.client.stream(
            "GET", self.url, headers=headers, timeout=timeout
        ) as resp:
            resp.raise_for_status()
            log.info("wikimedia: stream open, %d needles", len(self.needles))
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                # Liveness is measured here, on raw arrivals — see staleness_s().
                self.last_raw_at = time.monotonic()
                raw = line[5:].strip().encode()
                if not self._worth_parsing(raw):
                    self.skipped += 1
                    continue
                self.parsed += 1
                item = self._to_item(raw)
                if item is not None:
                    self.matched += 1
                    await emit(item)

    def _to_item(self, raw: bytes) -> Item | None:
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if ev.get("wiki") not in self.wikis:
            return None
        if ev.get("type") not in ("edit", "new"):
            return None

        wiki = ev.get("wiki", "")
        title = ev.get("title") or ""
        comment = ev.get("comment") or ""
        low_title, low_comment = title.lower(), comment.lower()

        target: Target | None = None
        signal = ""

        if wiki == "wikidatawiki":
            target = self.by_qid.get(low_title)
            if target is None:
                return None
            if P_DATE_OF_DEATH in low_comment or "date of death" in low_comment:
                # Synthesize text the funnel can actually screen: the display
                # name supplies the alias, "date of death" supplies the death
                # term. A raw wiki comment alone contains neither.
                signal = "Wikidata date of death (P570) written"
            elif DEATH_CATEGORY_HINT in low_comment:
                signal = "Wikidata death-related change"
            else:
                return None

        elif wiki == "enwiki":
            target = self.by_page.get(low_title)
            if target is None:
                return None
            # Let the edit summary supply the death vocabulary; the funnel
            # decides. An ordinary edit to the page yields no death term and is
            # killed at the automaton stage, which is exactly right.
            signal = "Wikipedia edit"

        if target is None:
            return None

        text = f"{target.display_name} — {signal}: {comment}"

        return Item(
            source_id=f"{self.id}:{wiki}",
            tier=self.tier,
            title=text,
            body="",
            url=(ev.get("meta") or {}).get("uri", "") or "",
            t_source=float(ev["timestamp"]) if ev.get("timestamp") else None,
            raw={
                "wiki": wiki,
                "page": title,
                "comment": comment,
                "user": ev.get("user"),
                "bot": ev.get("bot", False),
                "target_id": target.id,
                "signal": signal,
            },
        )

    def staleness_s(self) -> float:
        """Time since the last RAW event, not the last emitted item.

        Deliberate override. This adapter correctly filters ~1,800 firehose
        events down to only those about a registered target, which may be one
        edit a day — so measuring staleness on emissions would alarm constantly
        and train you to ignore the watchdog.

        Raw arrivals are the real liveness signal, and they are dense (~40/s),
        which makes this the fastest stall detector of any source we have.
        """
        return time.monotonic() - self.last_raw_at

    def health(self) -> dict:
        h = super().health()
        h.update({
            "parsed": self.parsed,
            "prefiltered_out": self.skipped,
            "matched": self.matched,
            # Distinguish "stream is dead" from "nothing newsworthy happened".
            "since_last_raw_s": round(self.staleness_s(), 1),
            "since_last_emit_s": round(time.monotonic() - self.last_item_at, 1),
        })
        return h
