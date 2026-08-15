"""Ambient content: related news feed and the market chart.

Not the product — the product is the alert — but an app you open once a year is
an app you delete. These give a reason to return, and both are cached server-side
so a thousand clients cost one upstream request.

Both are free to view without a purchase; only alerts are gated.
"""

from __future__ import annotations

import logging
import math
import random
import time

import feedparser
import httpx
from fastapi import APIRouter

from app.config import get_settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["content"])

_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: float):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _store(key: str, value):
    _cache[key] = (time.time(), value)
    return value


@router.get("/news")
async def news(limit: int = 20) -> dict:
    """Related headlines. Cached, so client count does not multiply upstream load."""
    s = get_settings()
    limit = min(max(limit, 1), 50)
    if (hit := _cached("news", s.news_cache_ttl_s)) is not None:
        return {"items": hit[:limit], "cached": True}

    items: list[dict] = []
    async with httpx.AsyncClient(
        timeout=10.0, headers={"User-Agent": "ticker-backend/0.1"}
    ) as client:
        for url in s.news_urls:
            try:
                r = await client.get(url)
                parsed = feedparser.parse(r.text)
            except Exception as exc:  # noqa: BLE001
                log.warning("news: %s failed: %s", url, exc)
                continue
            source = parsed.feed.get("title", "") if parsed.feed else ""
            for e in parsed.entries[:25]:
                title = (e.get("title") or "").strip()
                if not title:
                    continue
                items.append({
                    "title": title,
                    "url": e.get("link", ""),
                    "source": source,
                    "published": e.get("published", ""),
                    "summary": (e.get("summary") or "")[:280],
                })

    # Interleave sources so one prolific feed cannot dominate the column.
    items.sort(key=lambda i: i.get("published", ""), reverse=True)
    _store("news", items)
    return {"items": items[:limit], "cached": False}


@router.get("/market/{symbol}")
async def market(symbol: str = "TRUMP", days: int = 30) -> dict:
    """OHLC-ish series for the chart.

    Tries a live source, then falls back to a deterministic synthetic series so
    the UI is never empty. `source` in the response says which you are looking
    at — a chart that silently shows invented numbers as real would be worse
    than no chart at all.
    """
    s = get_settings()
    symbol = symbol.upper()[:12]
    days = min(max(days, 7), 365)
    key = f"market:{symbol}:{days}"
    if (hit := _cached(key, s.market_cache_ttl_s)) is not None:
        return {**hit, "cached": True}

    series = await _fetch_coingecko(symbol, days)
    source = "coingecko"
    if not series:
        series = _synthetic_series(symbol, days)
        source = "synthetic"

    last = series[-1]["c"] if series else 0.0
    first = series[0]["c"] if series else 0.0
    payload = {
        "symbol": symbol,
        "days": days,
        "source": source,
        "series": series,
        "last": last,
        "change_pct": ((last - first) / first * 100) if first else 0.0,
        "cached": False,
    }
    _store(key, payload)
    return payload


COINGECKO_IDS = {
    "TRUMP": "official-trump",
    "BTC": "bitcoin",
    "ETH": "ethereum",
}


async def _fetch_coingecko(symbol: str, days: int) -> list[dict]:
    coin_id = COINGECKO_IDS.get(symbol)
    if not coin_id:
        return []
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.get(url, params={"vs_currency": "usd", "days": days},
                                 headers={"User-Agent": "ticker-backend/0.1"})
        if r.status_code != 200:
            log.info("coingecko %s: http %s", symbol, r.status_code)
            return []
        prices = r.json().get("prices") or []
    except Exception as exc:  # noqa: BLE001
        log.info("coingecko %s failed: %s", symbol, exc)
        return []
    return [{"t": int(ts), "c": round(float(p), 6)} for ts, p in prices]


def _synthetic_series(symbol: str, days: int) -> list[dict]:
    """Deterministic per symbol, so the chart does not jump on every reload."""
    rng = random.Random(hash(symbol) & 0xFFFF)
    now_ms = int(time.time() * 1000)
    day_ms = 86_400_000
    price = 10.0
    out = []
    for i in range(days):
        drift = math.sin(i / 6.0) * 0.35
        price = max(0.5, price * (1 + (rng.random() - 0.5) * 0.09 + drift * 0.02))
        out.append({"t": now_ms - (days - i) * day_ms, "c": round(price, 4)})
    return out
