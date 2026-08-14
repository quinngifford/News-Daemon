"""RSS/Atom/JSON polling with conditional GET — the ingest workhorse.

The reason 1–2 second polling across dozens of feeds is affordable: with
`If-None-Match`/`If-Modified-Since`, an unchanged feed answers 304 in ~200
bytes over an already-open HTTP/2 connection. Without conditional GET you would
be pulling full feed bodies every second and getting rate-limited for it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import feedparser
import httpx

from ticker.ingest.base import Emit, SourceAdapter
from ticker.models import Item

log = logging.getLogger(__name__)

USER_AGENT = "news-ticker-daemon/0.1 (+personal breaking-news monitor)"


class RssAdapter(SourceAdapter):
    """Polls one feed URL. Handles RSS, Atom, and JSON APIs."""

    def __init__(self, config: dict, client: httpx.AsyncClient) -> None:
        super().__init__(config)
        self.client = client
        self.url: str = config["url"]
        self.poll_interval_s: float = float(config.get("poll_interval_s", 5.0))
        self._etag: str | None = None
        self._last_modified: str | None = None
        # Bounded GUID memory: enough to span a feed page many times over,
        # small enough to never matter on a 911 MB box.
        self._seen: set[str] = set()
        self._seen_order: list[str] = []
        self._max_seen = 2000

    async def _run(self, emit: Emit) -> None:
        while True:
            started = time.monotonic()
            try:
                await self._poll(emit)
            except httpx.HTTPError as exc:
                # Transient network trouble should not restart the adapter and
                # lose our ETag; just log and keep the cadence.
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.debug("poll %s failed: %s", self.id, self.last_error)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, self.poll_interval_s - elapsed))

    async def _poll(self, emit: Emit) -> None:
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
        if self._etag:
            headers["If-None-Match"] = self._etag
        if self._last_modified:
            headers["If-Modified-Since"] = self._last_modified

        resp = await self.client.get(self.url, headers=headers, timeout=10.0)

        if resp.status_code == 304:
            return  # unchanged — the cheap, overwhelmingly common case
        if resp.status_code == 429:
            # Back off hard; being blocked is worse than being slow.
            log.warning("source %s rate-limited; backing off 60s", self.id)
            await asyncio.sleep(60)
            return
        resp.raise_for_status()

        self._etag = resp.headers.get("ETag") or self._etag
        self._last_modified = resp.headers.get("Last-Modified") or self._last_modified

        ctype = resp.headers.get("Content-Type", "")
        entries = (
            self._parse_json(resp.text)
            if "json" in ctype or self.url.endswith(".json")
            else self._parse_feed(resp.text)
        )
        for guid, title, body, url, t_source in entries:
            if self._already_seen(guid):
                continue
            await emit(
                Item(
                    source_id=self.id,
                    tier=self.tier,
                    title=title,
                    body=body,
                    url=url,
                    t_source=t_source,
                    raw={"guid": guid},
                )
            )

    def _already_seen(self, guid: str) -> bool:
        if guid in self._seen:
            return True
        self._seen.add(guid)
        self._seen_order.append(guid)
        if len(self._seen_order) > self._max_seen:
            self._seen.discard(self._seen_order.pop(0))
        return False

    def _parse_feed(self, text: str) -> list[tuple[str, str, str, str, float | None]]:
        parsed = feedparser.parse(text)
        out = []
        for e in parsed.entries:
            title = (e.get("title") or "").strip()
            if not title:
                continue
            body = (e.get("summary") or e.get("description") or "").strip()
            link = e.get("link") or ""
            guid = e.get("id") or e.get("guid") or link or title
            t_source = None
            if e.get("published"):
                try:
                    t_source = parsedate_to_datetime(e["published"]).timestamp()
                except (TypeError, ValueError):
                    pass
            out.append((guid, title, body, link, t_source))
        return out

    def _parse_json(self, text: str) -> list[tuple[str, str, str, str, float | None]]:
        """Federal Register and similar JSON document APIs."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        results = data.get("results") or data.get("items") or []
        out = []
        for r in results:
            title = (r.get("title") or r.get("name") or "").strip()
            if not title:
                continue
            body = (r.get("abstract") or r.get("summary") or "").strip()
            link = r.get("html_url") or r.get("url") or ""
            guid = str(r.get("document_number") or r.get("id") or link or title)
            out.append((guid, title, body, link, None))
        return out


class GoogleNewsAdapter(RssAdapter):
    """One query URL built per target from its aliases.

    Enormous recall for a single request, but it is an aggregator: attribution
    collapsing in confirm/evidence.py matters more here than anywhere else, or
    one rumour reprinted widely reads as consensus.
    """

    QUERY_TMPL = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

    def __init__(self, config: dict, client: httpx.AsyncClient, aliases: list[str]) -> None:
        names = " OR ".join(f'"{a}"' for a in aliases[:6])
        query = f"({names}) AND (died OR dead OR dies OR obituary)"
        config = {**config, "url": self.QUERY_TMPL.format(q=quote_plus(query))}
        super().__init__(config, client)
