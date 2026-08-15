"""Per-device push delivery, and the Delivery audit trail.

Kept separate from the transport so APNs/FCM can be added for the native app
without touching ingest: implement `_send_<kind>` and register it in SENDERS.
"""

from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Delivery, Device, Event, User, utcnow

log = logging.getLogger(__name__)


def alert_notification(event: Event) -> dict:
    """Notification body, shared by every push transport."""
    confirmed = event.state == "confirmed"
    retracted = event.state == "retracted"
    if retracted:
        title = f"RETRACTED: {event.target}"
    elif confirmed:
        title = f"CONFIRMED: {event.target}"
    else:
        title = f"{event.state.upper()}: {event.target}"
    return {
        "title": title,
        "body": event.headline[:300],
        # Same tag across states means a RETRACTED replaces the CONFIRMED it
        # corrects instead of stacking beside it.
        "tag": event.event_id,
        "state": event.state,
        "url": event.url,
        "requireInteraction": confirmed,
        "event_id": event.event_id,
    }


async def _send_webpush(device: Device, notification: dict) -> tuple[bool, str]:
    s = get_settings()
    if not s.vapid_private_key:
        return False, "VAPID keys not configured"

    from pywebpush import WebPushException, webpush

    def _blocking() -> None:
        webpush(
            subscription_info={"endpoint": device.token, "keys": device.keys},
            data=json.dumps(notification),
            vapid_private_key=s.vapid_private_key,
            vapid_claims={"sub": s.vapid_subject},
            ttl=600,
            headers={"Urgency": "high"},
        )

    try:
        await asyncio.to_thread(_blocking)
        return True, ""
    except WebPushException as exc:
        status_code = getattr(exc.response, "status_code", None)
        return False, f"webpush {status_code}: {str(exc)[:160]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


async def _send_stub(device: Device, notification: dict) -> tuple[bool, str]:
    return False, f"{device.kind} transport not implemented yet"


SENDERS = {
    "webpush": _send_webpush,
    "apns": _send_stub,      # native iOS app
    "fcm": _send_stub,       # native Android app
}

# Endpoints returning these are permanently gone; retrying them forever would
# slow every future fan-out.
DEAD_STATUSES = ("404", "410")


async def notify_all_entitled(db: Session, event: Event) -> int:
    """Push to every device belonging to a paying user.

    Only 'confirmed' and 'retracted' are pushed. Intermediate states are
    genuine signal for the live dashboard but would train users to ignore
    notifications, which defeats the product.
    """
    if event.state not in ("confirmed", "retracted"):
        return 0

    rows = db.execute(
        select(Device, User)
        .join(User, Device.user_id == User.id)
        .where(User.entitled_at.is_not(None))
    ).all()
    if not rows:
        log.info("push: no entitled devices registered")
        return 0

    notification = alert_notification(event)
    sent = 0

    for device, user in rows:
        # Idempotent: a re-ingest of the same event will not re-notify.
        existing = db.scalar(
            select(Delivery).where(
                Delivery.event_row_id == event.id,
                Delivery.user_id == user.id,
                Delivery.channel == device.kind,
            )
        )
        if existing and existing.status == "delivered":
            continue

        record = existing or Delivery(
            event_row_id=event.id, user_id=user.id, channel=device.kind
        )
        record.attempts += 1

        sender = SENDERS.get(device.kind, _send_stub)
        ok, err = await sender(device, notification)
        if ok:
            record.status = "delivered"
            record.delivered_at = utcnow()
            record.error = None
            device.last_ok_at = utcnow()
            device.failures = 0
            sent += 1
        else:
            record.status = "failed"
            record.error = err[:500]
            device.failures += 1
            if any(code in err for code in DEAD_STATUSES):
                log.info("push: pruning dead device %s", device.id)
                db.delete(device)
        db.add(record)

    db.commit()
    log.info("push: delivered %d/%d for event %s", sent, len(rows), event.event_id[:8])
    return sent
