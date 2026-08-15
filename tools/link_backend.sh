#!/usr/bin/env bash
# Point this detector at the public backend.
#
#   ./tools/link_backend.sh https://ticker-api.onrender.com
#
# Run this once, after Render is deployed. It sets TICKER_WEBHOOK_URL, restarts
# the daemon, and verifies the backend actually answers.
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_FILE="deploy/ticker.env"

if [ $# -ne 1 ]; then
  echo "usage: $0 https://YOUR-RENDER-URL"
  echo "  (no trailing slash, no /api/... path — just the base URL)"
  exit 1
fi

BASE="${1%/}"                      # tolerate a trailing slash
URL="$BASE/api/ingest/events"

if [[ "$BASE" != https://* ]]; then
  echo "ERROR: must start with https:// (got: $BASE)"
  exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found"
  exit 1
fi

if ! grep -q '^TICKER_WEBHOOK_SECRET=' "$ENV_FILE"; then
  echo "ERROR: TICKER_WEBHOOK_SECRET missing from $ENV_FILE"
  exit 1
fi

echo "1/4  checking the backend is reachable…"
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$BASE/api/health" || echo 000)
if [ "$CODE" != "200" ]; then
  echo "     WARNING: $BASE/api/health returned $CODE (expected 200)."
  echo "     A free Render instance may be asleep; it will wake on first use."
  read -r -p "     Continue anyway? [y/N] " ok
  [[ "$ok" =~ ^[Yy]$ ]] || exit 1
else
  echo "     backend is up"
fi

echo "2/4  writing TICKER_WEBHOOK_URL…"
cp "$ENV_FILE" "$ENV_FILE.bak"
grep -v '^TICKER_WEBHOOK_URL=' "$ENV_FILE.bak" > "$ENV_FILE"
echo "TICKER_WEBHOOK_URL=$URL" >> "$ENV_FILE"
chmod 600 "$ENV_FILE"
echo "     $URL"

echo "3/4  restarting the detector…"
sudo systemctl restart ticker.service
sleep 6
systemctl is-active --quiet ticker.service \
  && echo "     ticker.service is active" \
  || { echo "     FAILED — sudo journalctl -u ticker.service -n 40"; exit 1; }

echo "4/4  sending a test event through the real pipeline…"
set -a; . "$ENV_FILE"; set +a
.venv/bin/python - <<'PY'
import asyncio, os, sys, tempfile
from pathlib import Path
import httpx
from ticker.notify.webhook import WebhookChannel
from ticker.store.db import Store
from ticker.models import Alert, Evidence, TargetState, Tier

async def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "probe.db")
        async with httpx.AsyncClient() as c:
            ch = WebhookChannel(c, store, url=os.environ["TICKER_WEBHOOK_URL"])
            if not ch.configured:
                print("     ERROR: channel not configured (missing URL or secret)")
                return 1
            alert = Alert(
                target_id="LINK TEST — not a real event",
                state=TargetState.WATCH,
                headline="Connection test from the detector. Ignore.",
                url="", score=0.0, detect_latency_ms=0.0,
                evidence=[Evidence(target_id="test", source_id="link-test",
                                   tier=Tier.SOCIAL, weight=0.0, url="",
                                   headline="link test")],
            )
            await ch.send(alert)
            s = ch.stats()
            if s["delivered"] == 1:
                print(f"     delivered OK (event {alert.event_id[:8]})")
                return 0
            print(f"     NOT delivered: {s}")
            print("     The event is queued and will retry automatically.")
            return 1
asyncio.run(main()) or sys.exit(0)
PY

echo
echo "Linked. The detector now publishes to $BASE"
echo "Check it arrived:  $BASE/api/health   (events_published should be >= 1)"
