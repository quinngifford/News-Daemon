"""Verify every URL in config/sources.yaml actually works.

Feeds rot silently, and a dead feed is indistinguishable from a quiet news day —
which is the single most dangerous failure mode this system has. Run weekly:

    .venv/bin/python tools/validate_sources.py

    0 0 * * 1 cd /home/ubuntu/news-ticker-daemon && .venv/bin/python \
        tools/validate_sources.py --quiet || echo "SOURCE VALIDATION FAILED"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import feedparser  # noqa: E402
import httpx  # noqa: E402

from ticker.config import load_sources  # noqa: E402

UA = "news-ticker-daemon/0.1 (source validator)"


async def check(client: httpx.AsyncClient, src: dict) -> dict:
    sid = src.get("id", "?")
    url = src.get("url")
    adapter = src.get("adapter", "rss")
    out = {"id": sid, "adapter": adapter, "url": url, "ok": False, "detail": ""}

    if not url:
        out["detail"] = "no url (built per-target at runtime)"
        out["ok"] = adapter in ("google_news",)
        return out

    if url.startswith("wss://"):
        out["detail"] = "websocket — not checked here"
        return out

    try:
        if adapter == "wikimedia_sse":
            # Stream a few lines rather than waiting for a body that never ends.
            async with client.stream(
                "GET", url,
                headers={"Accept": "text/event-stream", "User-Agent": UA},
                timeout=httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=10.0),
            ) as r:
                if r.status_code != 200:
                    out["detail"] = f"HTTP {r.status_code}"
                    return out
                seen = 0
                async for line in r.aiter_lines():
                    if line.startswith("data:"):
                        seen += 1
                        if seen >= 3:
                            break
                out["ok"] = seen >= 3
                out["detail"] = f"HTTP 200, {seen} events received"
            return out

        r = await client.get(url, headers={"User-Agent": UA}, timeout=15.0)
        if r.status_code != 200:
            out["detail"] = f"HTTP {r.status_code}"
            return out

        cond = []
        if r.headers.get("ETag"):
            cond.append("ETag")
        if r.headers.get("Last-Modified"):
            cond.append("Last-Modified")

        if "json" in r.headers.get("Content-Type", "") or url.endswith(".json"):
            data = json.loads(r.text)
            n = len(data.get("results") or data.get("items") or [])
        else:
            n = len(feedparser.parse(r.text).entries)

        out["ok"] = n > 0
        out["detail"] = (
            f"HTTP 200, {n} entries, "
            f"conditional-GET: {'+'.join(cond) if cond else 'NONE (polls full body!)'}"
        )
    except Exception as exc:  # noqa: BLE001 — report, never crash the sweep
        out["detail"] = f"{type(exc).__name__}: {exc}"
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="only print failures")
    args = ap.parse_args()

    sources = load_sources()
    enabled = [s for s in sources if s.get("enabled", True)]
    skipped = len(sources) - len(enabled)

    async with httpx.AsyncClient(http2=True, follow_redirects=True) as client:
        results = await asyncio.gather(*(check(client, s) for s in enabled))

    failures = [r for r in results if not r["ok"]]
    for r in results:
        if args.quiet and r["ok"]:
            continue
        mark = "ok  " if r["ok"] else "FAIL"
        print(f"{mark} tier? {r['id']:<28} {r['detail']}")
        if not r["ok"] and r["url"]:
            print(f"       {r['url'][:110]}")

    print(f"\n{len(results) - len(failures)}/{len(results)} sources healthy"
          f"{f', {skipped} disabled' if skipped else ''}")
    if failures:
        print("Dead sources are silent failures. Fix or remove them from "
              "config/sources.yaml.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
