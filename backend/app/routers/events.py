"""Event feed and the live stream. Paid users only."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.fanout import fanout
from app.models import Device, Event, User
from app.routers.ingest import client_payload
from app.security import current_user, require_entitled

router = APIRouter(prefix="/api", tags=["events"])


@router.get("/events")
def list_events(limit: int = 50, before: str | None = None,
                user: User = Depends(require_entitled),
                db: Session = Depends(get_db)) -> dict:
    """Newest first, keyset-paginated.

    Keyset rather than OFFSET: offset pagination degrades linearly as the table
    grows and can skip or repeat rows when new events arrive mid-scroll.
    """
    limit = min(max(limit, 1), 200)
    stmt = select(Event).order_by(Event.received_at.desc(), Event.id.desc())
    if before:
        anchor = db.get(Event, before)
        if anchor:
            stmt = stmt.where(Event.received_at < anchor.received_at)
    rows = db.scalars(stmt.limit(limit)).all()
    return {
        "events": [client_payload(e) for e in rows],
        "next_before": rows[-1].id if len(rows) == limit else None,
    }


@router.get("/events/stream")
async def stream(request: Request, user: User = Depends(require_entitled)):
    """SSE. Lowest-latency path: no vendor push infrastructure involved."""
    queue = fanout.subscribe()

    async def gen():
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    body = await asyncio.wait_for(queue.get(), timeout=20.0)
                except TimeoutError:
                    # Keeps proxies from reaping an idle connection, which would
                    # silently cost the user the fastest channel they have.
                    yield ": ping\n\n"
                    continue
                yield f"data: {body}\n\n"
        finally:
            fanout.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# --- push device registration ---------------------------------------------


@router.get("/push/public-key")
def push_public_key() -> dict:
    from app.config import get_settings

    return {"publicKey": get_settings().vapid_public_key}


@router.post("/push/register", status_code=201)
def register_device(body: dict, user: User = Depends(current_user),
                    db: Session = Depends(get_db)) -> dict:
    """Register a push destination. Works for webpush now, APNs/FCM later."""
    kind = (body.get("kind") or "webpush").lower()
    token = body.get("token") or body.get("endpoint")
    if not token:
        return {"ok": False, "error": "token/endpoint required"}

    existing = db.scalar(
        select(Device).where(Device.kind == kind, Device.token == token)
    )
    if existing:
        # Re-registering after reinstall: move it to whoever is logged in now.
        existing.user_id = user.id
        existing.keys = body.get("keys") or existing.keys
        existing.failures = 0
        db.commit()
        return {"ok": True, "id": existing.id, "reused": True}

    device = Device(user_id=user.id, kind=kind, token=token,
                    keys=body.get("keys") or {},
                    user_agent=(body.get("user_agent") or "")[:400])
    db.add(device)
    db.commit()
    db.refresh(device)
    return {"ok": True, "id": device.id, "reused": False}


@router.post("/push/unregister")
def unregister_device(body: dict, user: User = Depends(current_user),
                      db: Session = Depends(get_db)) -> dict:
    token = body.get("token") or body.get("endpoint")
    device = db.scalar(
        select(Device).where(Device.user_id == user.id, Device.token == token)
    )
    if device:
        db.delete(device)
        db.commit()
    return {"ok": True}


@router.post("/push/test")
async def push_test(user: User = Depends(require_entitled),
                    db: Session = Depends(get_db)) -> dict:
    """Send a test notification to this user's devices.

    Exists because 'did I set notifications up correctly?' must be answerable
    before the real event, not during it.
    """
    from app.push import SENDERS

    devices = db.scalars(select(Device).where(Device.user_id == user.id)).all()
    if not devices:
        return {"ok": False, "sent": 0, "error": "no devices registered"}

    note = {
        "title": "Test alert",
        "body": "Notifications are working. This is only a test.",
        "tag": "test", "state": "test", "url": "/", "requireInteraction": False,
    }
    sent, errors = 0, []
    for d in devices:
        ok, err = await SENDERS.get(d.kind, lambda *_: (False, "unsupported"))(d, note)
        sent += ok
        if not ok:
            errors.append(err)
    return {"ok": sent > 0, "sent": sent, "devices": len(devices),
            "errors": errors[:3]}
