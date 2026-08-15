"""One-time $49.99 purchase via Stripe Checkout.

Entitlement is granted by the **webhook**, never by the browser returning to the
success URL. A success redirect is trivially forged by typing the URL; the
webhook is signed by Stripe and is the only trustworthy signal that money moved.
"""

from __future__ import annotations

import logging

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Purchase, User, utcnow
from app.security import current_user, current_user_optional

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/billing", tags=["billing"])


@router.get("/config")
def billing_config(user: User | None = Depends(current_user_optional)) -> dict:
    """Price and publishable key. Readable while logged OUT.

    Was `Depends(current_user)`, which 401'd for anyone not signed in — i.e. on
    the sign-in screen, where the price is displayed. The client swallowed it,
    so the page silently showed a placeholder price and wrongly offered the
    dev-grant button.
    """
    s = get_settings()
    return {
        "price_cents": s.price_cents,
        "price_display": f"${s.price_cents / 100:,.2f}",
        "currency": s.currency,
        "publishable_key": s.stripe_publishable_key,
        "configured": bool(s.stripe_secret_key),
        # Surfaced so the UI can warn you before a customer hits a dead button.
        "live_mode": s.stripe_secret_key.startswith("sk_live_"),
        "entitled": user.is_entitled if user else False,
    }


@router.post("/checkout")
def create_checkout(user: User = Depends(current_user),
                    db: Session = Depends(get_db)) -> dict:
    s = get_settings()
    if not s.stripe_secret_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "billing not configured")
    if user.is_entitled:
        return {"already_entitled": True, "url": None}

    stripe.api_key = s.stripe_secret_key

    line_item = (
        {"price": s.stripe_price_id, "quantity": 1}
        if s.stripe_price_id
        else {
            "price_data": {
                "currency": s.currency,
                "unit_amount": s.price_cents,
                "product_data": {
                    "name": f"{s.app_name} — lifetime access",
                    "description": "Instant breaking-news alerts. One-time payment.",
                },
            },
            "quantity": 1,
        }
    )

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[line_item],
            customer_email=user.email,
            success_url=f"{s.base_url}{s.checkout_success_path}",
            cancel_url=f"{s.base_url}{s.checkout_cancel_path}",
            # Echoed back on the webhook so we can attribute payment to a user
            # without trusting anything the browser sends.
            client_reference_id=user.id,
            metadata={"user_id": user.id},
        )
    except stripe.StripeError as exc:
        # Surface what Stripe actually said. The previous generic message meant
        # a misconfigured account looked identical to a network failure, and the
        # only way to tell them apart was reading server logs — which is exactly
        # the situation you are in when checkout silently does nothing.
        detail = getattr(exc, "user_message", None) or str(exc)
        code = getattr(exc, "code", None) or type(exc).__name__
        log.error("stripe checkout failed [%s]: %s", code, exc)

        hint = ""
        if "activate" in detail.lower() or code in ("account_invalid",
                                                    "charges_disabled"):
            hint = (" Your Stripe account is not activated for live payments yet"
                    " — complete onboarding at dashboard.stripe.com, or switch"
                    " to test keys (sk_test_/pk_test_) while developing.")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Stripe rejected the request [{code}]: {detail}{hint}",
        ) from None

    db.add(Purchase(user_id=user.id, stripe_session_id=session.id,
                    amount_cents=s.price_cents, currency=s.currency,
                    status="pending"))
    db.commit()
    return {"already_entitled": False, "url": session.url, "id": session.id}


@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    """Stripe -> us. The ONLY place entitlement is granted."""
    s = get_settings()
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    if not s.stripe_webhook_secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "webhook not configured")
    try:
        event = stripe.Webhook.construct_event(payload, sig, s.stripe_webhook_secret)
    except (ValueError, stripe.SignatureVerificationError):
        log.warning("stripe webhook: bad signature")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid signature") from None

    etype = event["type"]
    obj = event["data"]["object"]

    if etype == "checkout.session.completed":
        _grant(db, obj)
    elif etype in ("charge.refunded", "charge.dispute.created"):
        _revoke(db, obj)
    else:
        log.debug("stripe webhook: ignoring %s", etype)

    # Always 200 for handled-or-ignored, or Stripe retries indefinitely.
    return {"received": True}


def _grant(db: Session, session_obj: dict) -> None:
    user_id = (session_obj.get("client_reference_id")
               or (session_obj.get("metadata") or {}).get("user_id"))
    session_id = session_obj.get("id")

    if session_obj.get("payment_status") != "paid":
        log.info("stripe: session %s not paid yet", session_id)
        return

    user = db.get(User, user_id) if user_id else None
    if user is None:
        log.error("stripe: paid session %s has no matching user (%s)",
                  session_id, user_id)
        return

    purchase = db.scalar(
        select(Purchase).where(Purchase.stripe_session_id == session_id)
    )
    if purchase and purchase.status == "paid":
        return                     # replayed webhook; already handled

    if purchase is None:
        purchase = Purchase(user_id=user.id, stripe_session_id=session_id,
                            amount_cents=session_obj.get("amount_total") or 0,
                            currency=session_obj.get("currency") or "usd")
        db.add(purchase)

    purchase.status = "paid"
    purchase.paid_at = utcnow()
    purchase.stripe_payment_intent = session_obj.get("payment_intent")
    purchase.data = {**(purchase.data or {}), "stripe_event": "completed"}

    if user.entitled_at is None:
        user.entitled_at = utcnow()
    if not user.stripe_customer_id:
        user.stripe_customer_id = session_obj.get("customer")

    db.commit()
    log.info("stripe: entitlement granted to %s", user.email)


def _revoke(db: Session, charge_obj: dict) -> None:
    """Refund or chargeback removes access."""
    intent = charge_obj.get("payment_intent")
    if not intent:
        return
    purchase = db.scalar(
        select(Purchase).where(Purchase.stripe_payment_intent == intent)
    )
    if not purchase:
        return
    purchase.status = "refunded"
    user = db.get(User, purchase.user_id)
    if user:
        user.entitled_at = None
        log.info("stripe: entitlement revoked for %s", user.email)
    db.commit()
