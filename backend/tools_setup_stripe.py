"""Create the Stripe webhook endpoint and check the deployed backend.

    .venv/bin/python tools_setup_stripe.py https://YOUR-RENDER-URL

Does what the Stripe dashboard does, but without the guesswork:
  1. checks your deployed backend is up and reports what it thinks is configured
  2. creates (or reuses) the webhook endpoint pointing at it
  3. prints the signing secret to paste into Render as STRIPE_WEBHOOK_SECRET

Safe to re-run: it reuses an existing endpoint with the same URL rather than
creating duplicates.
"""

from __future__ import annotations

import sys

import httpx
import stripe

from app.config import get_settings

# What we actually act on. Adding more is harmless but these are the two the
# backend implements.
EVENTS = ["checkout.session.completed", "charge.refunded"]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1

    base = sys.argv[1].rstrip("/")
    if not base.startswith("https://"):
        print(f"ERROR: URL must start with https:// (got {base})")
        return 1

    s = get_settings()
    if not s.stripe_secret_key:
        print("ERROR: STRIPE_SECRET_KEY is not set in this backend/.env")
        return 1
    mode = "TEST" if "_test_" in s.stripe_secret_key else "LIVE"
    print(f"Stripe mode: {mode}")

    # --- 1. is the deployed backend actually up and configured? ----------
    print(f"\n1/3  checking {base} …")
    try:
        r = httpx.get(f"{base}/api/health", timeout=30.0)
        health = r.json()
        print(f"     reachable (HTTP {r.status_code})")
        print(f"     billing_configured: {health.get('billing_configured')}")
        print(f"     ingest_configured:  {health.get('ingest_configured')}")
        print(f"     push_configured:    {health.get('push_configured')}")
        if not health.get("billing_configured"):
            print("\n     ⚠ STRIPE_SECRET_KEY is NOT set on the server.")
            print("       The Purchase button will fail with 503 until it is.")
            print("       Render → your service → Environment → add it → redeploy.")
    except Exception as exc:  # noqa: BLE001
        print(f"     UNREACHABLE: {type(exc).__name__}: {str(exc)[:120]}")
        print("     A sleeping free instance can take ~60s to wake. Retry once.")
        return 1

    # --- 2. create or reuse the webhook endpoint -------------------------
    hook_url = f"{base}/api/billing/webhook"
    stripe.api_key = s.stripe_secret_key
    print(f"\n2/3  webhook endpoint → {hook_url}")

    existing = None
    for ep in stripe.WebhookEndpoint.list(limit=100).auto_paging_iter():
        if ep.url == hook_url:
            existing = ep
            break

    if existing:
        print(f"     already exists ({existing.id}), status={existing.status}")
        missing = [e for e in EVENTS if e not in existing.enabled_events]
        if missing:
            stripe.WebhookEndpoint.modify(existing.id, enabled_events=EVENTS)
            print(f"     added missing events: {', '.join(missing)}")
        print("\n     NOTE: Stripe only reveals the signing secret at creation.")
        print("     If you did not save it, delete this endpoint in the dashboard")
        print("     and re-run to get a fresh one.")
        secret = None
    else:
        ep = stripe.WebhookEndpoint.create(
            url=hook_url, enabled_events=EVENTS,
            description="Ticker — grants access after payment",
        )
        secret = ep.secret
        print(f"     created {ep.id}")

    # --- 3. what to do next ---------------------------------------------
    print("\n3/3  finish in Render")
    if secret:
        print("\n     Add this in Render → your service → Environment:")
        print(f"\n       STRIPE_WEBHOOK_SECRET = {secret}\n")
        print("     Then click 'Manual Deploy' / save so it restarts.")
    else:
        print("     Set STRIPE_WEBHOOK_SECRET in Render to this endpoint's")
        print("     signing secret (visible in the Stripe dashboard).")

    print("\n     Also confirm these are set in Render:")
    print(f"       BASE_URL              = {base}")
    print("       STRIPE_SECRET_KEY     = sk_test_…")
    print("       STRIPE_PUBLISHABLE_KEY= pk_test_…")
    print("\n     Then test with card 4242 4242 4242 4242, any future expiry,")
    print("     any CVC, any postcode.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
