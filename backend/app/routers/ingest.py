"""Receives detected events from the VPS detector.

This is the only endpoint the detector talks to. It is authenticated by HMAC
signature rather than a bearer token, so a leaked read token cannot be used to
inject fake death alerts — which, given what users do with these alerts, is the
single most damaging thing an attacker could do here.

Idempotent on (event_id, state): retries from the detector's outbox, and a
redundant second detector box, both collapse to one row and one notification.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.fanout import fanout
from app.models import Event
from app.push import notify_all_entitled
from app.security import verify_detector_signature

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ingest", tags=["ingest"])

SIGNATURE_HEADER = "X-Ticker-Signature"


def _parse_ts(value) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


@router.post("/events", status_code=status.HTTP_202_ACCEPTED)
async def ingest_event(request: Request, db: Session = Depends(get_db)) -> dict:
    body = await request.body()
    signature = request.headers.get(SIGNATURE_HEADER, "")

    if not verify_detector_signature(signature, body):
        log.warning("ingest: rejected unsigned/invalid request from %s",
                    request.client.host if request.client else "?")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None

    event_id = payload.get("event_id")
    state = payload.get("state")
    if not event_id or not state:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "event_id and state are required")

    existing = db.scalar(
        select(Event).where(Event.event_id == event_id, Event.state == state)
    )
    if existing:
        # Not an error: the detector retries until it gets a 2xx, so a duplicate
        # means the pipeline is working. Returning 200 lets it stop retrying.
        return {"ok": True, "duplicate": True, "id": existing.id}

    event = Event(
        event_id=event_id,
        state=state,
        target=payload.get("target") or "unknown",
        headline=payload.get("headline") or "",
        url=payload.get("url") or "",
        score=payload.get("score"),
        detect_latency_ms=payload.get("detect_latency_ms"),
        occurred_at=_parse_ts(payload.get("occurred_at")),
        schema_version=payload.get("schema") or "ticker.alert.v1",
        # Stored verbatim: fields this backend does not yet understand are
        # retained and served to clients unchanged.
        payload=payload,
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        # Two replicas ingested the same retry concurrently. Both are correct;
        # the unique constraint decides.
        db.rollback()
        existing = db.scalar(
            select(Event).where(Event.event_id == event_id, Event.state == state)
        )
        return {"ok": True, "duplicate": True,
                "id": existing.id if existing else None}
    db.refresh(event)

    log.info("ingest: %s %s (%s)", state, event_id[:8], event.target)

    # Live clients first — an SSE push is milliseconds and needs no vendor.
    await fanout.publish(client_payload(event))
    # Then the slower per-device push, which may block on FCM/APNs.
    await notify_all_entitled(db, event)

    return {"ok": True, "duplicate": False, "id": event.id}


def client_payload(event: Event) -> dict:
    """What clients receive. Extra detector fields ride along in `payload`."""
    return {
        "type": "alert",
        "id": event.id,
        "event_id": event.event_id,
        "state": event.state,
        "target": event.target,
        "headline": event.headline,
        "url": event.url,
        "score": event.score,
        "detect_latency_ms": event.detect_latency_ms,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
        "received_at": event.received_at.isoformat() if event.received_at else None,
        "schema": event.schema_version,
        "payload": event.payload,
    }
