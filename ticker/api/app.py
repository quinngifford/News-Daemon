"""Dashboard + subscription API, served from inside the daemon process.

Deliberately in-process rather than a second service: the SSE channel can then
push an alert the instant the dispatcher fans out, with no database polling in
the path. On a 911 MB box, one process beats two plus a broker.

Binds to 127.0.0.1 by default, so nothing is exposed. Web Push requires the page
be served over HTTPS (or localhost), so for phone access put the reverse proxy in
deploy/caddy/ in front of it — see README.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"


class ApiState:
    """Live references into the running daemon. No copies, no polling."""

    def __init__(
        self,
        funnel=None,
        confirmer=None,
        adapters=None,
        store=None,
        broadcaster=None,
        canary=None,
        adjudicator=None,
        started_at: float | None = None,
        dry_run: bool = False,
    ) -> None:
        self.funnel = funnel
        self.confirmer = confirmer
        self.adapters = adapters or []
        self.store = store
        self.broadcaster = broadcaster
        self.canary = canary
        self.adjudicator = adjudicator
        self.started_at = started_at or time.time()
        self.dry_run = dry_run


def create_app(state: ApiState) -> FastAPI:
    app = FastAPI(title="news-ticker-daemon", docs_url=None, redoc_url=None)

    # ---- dashboard ----------------------------------------------------

    @app.get("/")
    async def index() -> FileResponse:
        path = WEB_DIR / "index.html"
        if not path.exists():
            raise HTTPException(404, "web/index.html not found")
        return FileResponse(path)

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    # Service workers may only control scopes at or below their own path, so
    # sw.js must be served from the root, not from /static.
    @app.get("/sw.js")
    async def service_worker() -> FileResponse:
        path = WEB_DIR / "sw.js"
        if not path.exists():
            raise HTTPException(404, "sw.js not found")
        return FileResponse(path, media_type="application/javascript",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest")
    async def manifest() -> FileResponse:
        return FileResponse(WEB_DIR / "manifest.webmanifest",
                            media_type="application/manifest+json")

    # ---- health & status ----------------------------------------------

    @app.get("/api/health")
    async def health() -> dict:
        sources = [a.health() for a in state.adapters]
        stale = [s["id"] for s in sources if s.get("stale")]
        canary_row = state.store.last_canary() if state.store else None
        canary_age_s = (
            time.time() - canary_row["t_wall"] if canary_row else None
        )
        return {
            # A pipeline with no recent canary PASS is not known to work, so
            # that is treated as unhealthy even when every source is live.
            "ok": not stale and bool(canary_row and canary_row["passed"]),
            "uptime_s": round(time.time() - state.started_at, 1),
            "dry_run": state.dry_run,
            "stale_sources": stale,
            "sources": sources,
            "funnel": state.funnel.stats if state.funnel else {},
            "canary": (
                {
                    "passed": bool(canary_row["passed"]),
                    "latency_ms": canary_row["latency_ms"],
                    "age_s": round(canary_age_s, 1) if canary_age_s else None,
                    "detail": canary_row["detail"],
                }
                if canary_row else None
            ),
            "adjudicator": state.adjudicator.stats() if state.adjudicator else None,
            "sse_subscribers": (
                state.broadcaster.subscriber_count if state.broadcaster else 0
            ),
        }

    @app.get("/api/status")
    async def status() -> dict:
        return {
            "targets": state.confirmer.status() if state.confirmer else {},
            "confirmer": state.confirmer.stats if state.confirmer else {},
        }

    @app.get("/api/alerts")
    async def alerts(limit: int = 50) -> dict:
        if not state.store:
            return {"alerts": []}
        rows = state.store.recent_alerts(min(max(limit, 1), 200))
        for r in rows:
            if r.get("trail_json"):
                try:
                    r["trail"] = json.loads(r.pop("trail_json"))
                except json.JSONDecodeError:
                    r["trail"] = []
        return {"alerts": rows}

    # ---- live event stream --------------------------------------------

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        if state.broadcaster is None:
            raise HTTPException(503, "broadcaster not available")
        queue = state.broadcaster.subscribe()

        async def gen():
            try:
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=20.0)
                    except TimeoutError:
                        # Comment frame: keeps proxies from reaping an idle
                        # stream, which would silently cost you the fastest
                        # notification channel.
                        yield ": ping\n\n"
                        continue
                    yield f"data: {payload}\n\n"
            finally:
                state.broadcaster.unsubscribe(queue)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ---- Web Push subscriptions ---------------------------------------

    @app.get("/api/vapid-public-key")
    async def vapid_public_key() -> dict:
        key = os.environ.get("TICKER_VAPID_PUBLIC_KEY", "")
        if not key:
            raise HTTPException(
                503,
                "TICKER_VAPID_PUBLIC_KEY not set — run tools/gen_vapid_keys.py",
            )
        return {"publicKey": key}

    @app.post("/api/subscribe")
    async def subscribe(request: Request) -> JSONResponse:
        if not state.store:
            raise HTTPException(503, "store not available")
        body = await request.json()
        endpoint = body.get("endpoint")
        keys = body.get("keys")
        if not endpoint or not isinstance(keys, dict):
            raise HTTPException(400, "expected {endpoint, keys:{p256dh, auth}}")
        state.store.add_subscription(
            endpoint, keys, request.headers.get("user-agent", "")
        )
        log.info("webpush: subscription registered (%d total)",
                 len(state.store.get_subscriptions()))
        return JSONResponse({"ok": True})

    @app.post("/api/unsubscribe")
    async def unsubscribe(request: Request) -> JSONResponse:
        if not state.store:
            raise HTTPException(503, "store not available")
        body = await request.json()
        endpoint = body.get("endpoint")
        if not endpoint:
            raise HTTPException(400, "expected {endpoint}")
        state.store.drop_subscription(endpoint)
        return JSONResponse({"ok": True})

    return app


async def serve(app: FastAPI, host: str = "127.0.0.1", port: int = 8080) -> None:
    """Run uvicorn as a task inside the daemon's existing event loop."""
    import uvicorn

    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning",
        access_log=False, lifespan="off",
    )
    await uvicorn.Server(config).serve()
