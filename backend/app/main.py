"""Application entrypoint.

    uvicorn app.main:app --reload            # dev
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4   # prod

Stateless by design: auth is a JWT, all shared state is in the database, and
real-time fanout goes through Redis when configured. So scaling is "run more
copies behind a load balancer", with no sticky sessions.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import Base, engine
from app.fanout import fanout
from app.routers import auth, billing, content, events, ingest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("backend")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()

    # Refuse to boot insecurely rather than run and hope nobody notices.
    if s.is_prod:
        problems = []
        if s.jwt_secret == "dev-only-insecure-change-me":
            problems.append("JWT_SECRET is still the default")
        if not s.ingest_secret:
            problems.append("INGEST_SECRET is empty (detector cannot be verified)")
        if s.allow_dev_grant:
            problems.append("ALLOW_DEV_GRANT is true in production")
        if problems:
            raise RuntimeError("refusing to start in prod: " + "; ".join(problems))

    # Dev convenience. In production use Alembic so schema changes are reviewed.
    if not s.is_prod:
        Base.metadata.create_all(bind=engine)

    await fanout.start()
    log.info("%s up (env=%s, db=%s)", s.app_name, s.env,
             s.database_url.split("://")[0])
    yield
    await fanout.stop()


app = FastAPI(title="Trump Death Watcher API", version="0.1.0", lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json")

# The web app is served from the same origin, so CORS is only needed for the
# native mobile app and any separately-hosted frontend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,      # bearer tokens, not cookies
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(billing.router)
app.include_router(ingest.router)
app.include_router(events.router)
app.include_router(content.router)


@app.get("/api/health")
async def health() -> dict:
    s = get_settings()
    return {
        "ok": True,
        "env": s.env,
        "sse_subscribers": fanout.subscriber_count,
        "events_published": fanout.published,
        "billing_configured": bool(s.stripe_secret_key),
        "ingest_configured": bool(s.ingest_secret),
        "push_configured": bool(s.vapid_private_key),
    }


@app.exception_handler(404)
async def spa_fallback(request: Request, exc):
    """Unknown non-API paths fall through to the SPA."""
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "not found"}, status_code=404)
    index = WEB_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse({"detail": "not found"}, status_code=404)


@app.get("/sw.js")
async def service_worker():
    """Must be served from the root so its scope covers the whole app."""
    return FileResponse(WEB_DIR / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
