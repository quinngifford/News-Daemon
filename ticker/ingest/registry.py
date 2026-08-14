"""Builds adapter instances from config/sources.yaml.

Adding a source is a YAML edit plus (at most) one adapter class. Nothing
downstream of ingest knows how many sources exist or what protocols they speak.
"""

from __future__ import annotations

import logging

import httpx

from ticker.config import CONFIG_DIR, Target
from ticker.ingest.base import SourceAdapter
from ticker.ingest.rss import GoogleNewsAdapter, RssAdapter
from ticker.ingest.twitter_stream import TwitterStreamAdapter
from ticker.ingest.wikimedia_sse import WikimediaSseAdapter

log = logging.getLogger(__name__)


def make_client() -> httpx.AsyncClient:
    """One shared HTTP/2 client: connection reuse is most of the latency win.

    Cold DNS + TCP + TLS is ~85 ms from this box (measured). Keeping the pool
    warm means a poll costs one round trip instead of four.
    """
    return httpx.AsyncClient(
        http2=True,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=32,
                            keepalive_expiry=300.0),
        timeout=httpx.Timeout(10.0),
    )


def build_adapters(
    sources: list[dict],
    targets: dict[str, Target],
    client: httpx.AsyncClient,
) -> list[SourceAdapter]:
    all_aliases = [a for t in targets.values() for a in t.aliases]
    adapters: list[SourceAdapter] = []

    for cfg in sources:
        if not cfg.get("enabled", True):
            log.info("source %s disabled in config", cfg.get("id"))
            continue

        kind = cfg.get("adapter", "rss")
        try:
            if kind == "rss":
                adapters.append(RssAdapter(cfg, client))
            elif kind == "wikimedia_sse":
                adapters.append(WikimediaSseAdapter(cfg, client, targets))
            elif kind == "google_news":
                # One adapter per target: the query is built from its aliases.
                for t in targets.values():
                    adapters.append(
                        GoogleNewsAdapter(
                            {**cfg, "id": f"{cfg['id']}:{t.id}"}, client, t.aliases
                        )
                    )
            elif kind == "twitter_stream":
                adapters.append(
                    TwitterStreamAdapter(cfg, client, CONFIG_DIR / "x_accounts.yaml")
                )
            else:
                # reddit / hn / twitter_stream / market_ws are scaffolded in
                # their own modules; see docs/ARCHITECTURE.md §3.
                log.warning("adapter %r not implemented yet (source %s)",
                            kind, cfg.get("id"))
        except (KeyError, ValueError) as exc:
            log.error("bad source config %s: %s", cfg.get("id"), exc)

    log.info("built %d adapters", len(adapters))
    return adapters
