"""End-to-end: signup → paywall → entitlement → signed ingest → live delivery.

Runs a real server on 127.0.0.1 and drives it with a real HTTP client, so this
exercises auth, gating, HMAC verification, idempotency and SSE exactly as the
detector and browser will.

Run:  .venv/bin/python -m tests.test_flow
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

# Point at a scratch database BEFORE app modules read settings.
_TMP = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["INGEST_SECRET"] = "test-ingest-secret"
os.environ["ENV"] = "dev"
os.environ["ALLOW_DEV_GRANT"] = "true"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.main import app  # noqa: E402

SECRET = "test-ingest-secret"
failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def sign(body: bytes, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    mac = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def detector_payload(event_id: str, state: str = "confirmed") -> dict:
    """Exactly what ticker/notify/webhook.py:build_payload() emits."""
    return {
        "schema": "ticker.alert.v1",
        "event_id": event_id,
        "state": state,
        "target": "Donald Trump",
        "headline": "Donald Trump has died at 79, White House confirms",
        "url": "https://apnews.com/x",
        "score": 0.966,
        "detect_latency_ms": 812.5,
        "occurred_at": time.time(),
        "evidence": [{"source": "x:AP", "origin": "ap", "tier": 0,
                      "negative": False, "headline": "AP: Trump has died",
                      "url": "https://x.com/AP/1", "at": time.time()}],
        # A field this backend does not know about. It must survive verbatim.
        "future_field": {"auto_invest": True, "confidence_band": [0.9, 0.99]},
    }


async def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error",
        access_log=False, lifespan="on"))
    task = asyncio.create_task(server.serve())
    for _ in range(120):
        if getattr(server, "started", False):
            break
        await asyncio.sleep(0.05)

    try:
        async with httpx.AsyncClient(base_url=base, timeout=15.0) as c:
            print("--- signup / auth ---")
            r = await c.post("/api/auth/signup",
                             json={"email": "a@example.com", "password": "correct-horse-battery"})
            check(r.status_code == 201, "signup succeeds", str(r.status_code))
            tok = r.json()["access_token"]
            auth = {"Authorization": f"Bearer {tok}"}

            r = await c.post("/api/auth/signup",
                             json={"email": "a@example.com", "password": "another-long-password"})
            check(r.status_code == 409, "duplicate email rejected", str(r.status_code))

            r = await c.post("/api/auth/signup",
                             json={"email": "b@example.com", "password": "short"})
            check(r.status_code == 422, "weak password rejected", str(r.status_code))

            r = await c.post("/api/auth/login",
                             json={"email": "a@example.com", "password": "wrong-password-here"})
            check(r.status_code == 401, "wrong password rejected")

            r = await c.get("/api/auth/me", headers=auth)
            check(r.status_code == 200 and not r.json()["entitled"],
                  "new user is NOT entitled")

            print("\n--- paywall gating ---")
            r = await c.get("/api/events", headers=auth)
            check(r.status_code == 402, "alerts gated behind payment (402)",
                  str(r.status_code))
            r = await c.get("/api/events")
            check(r.status_code == 401, "alerts require auth at all")
            r = await c.get("/api/news")
            check(r.status_code == 200, "news is free to view")

            print("\n--- entitlement ---")
            r = await c.post("/api/auth/dev-grant", headers=auth)
            check(r.status_code == 200 and r.json()["entitled"], "dev grant entitles")
            r = await c.get("/api/events", headers=auth)
            check(r.status_code == 200, "alerts now accessible")

            print("\n--- ingest auth (the important one) ---")
            payload = detector_payload("evt-live-1")
            body = json.dumps(payload).encode()

            r = await c.post("/api/ingest/events", content=body,
                             headers={"Content-Type": "application/json"})
            check(r.status_code == 401, "unsigned ingest REJECTED",
                  "otherwise anyone could inject a fake death alert")

            r = await c.post("/api/ingest/events", content=body,
                             headers={"Content-Type": "application/json",
                                      "X-Ticker-Signature": sign(body, int(time.time()) - 9999)})
            check(r.status_code == 401, "stale signature rejected (replay guard)")

            bad = hmac.new(b"wrong", b"x", hashlib.sha256).hexdigest()
            r = await c.post("/api/ingest/events", content=body,
                             headers={"Content-Type": "application/json",
                                      "X-Ticker-Signature": f"t={int(time.time())},v1={bad}"})
            check(r.status_code == 401, "wrong secret rejected")

            print("\n--- live delivery over SSE ---")
            received: list[dict] = []

            async def reader():
                async with c.stream("GET", f"/api/events/stream?token={tok}",
                                    timeout=httpx.Timeout(10.0, read=None)) as resp:
                    check(resp.status_code == 200, "SSE opens for entitled user")
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            received.append(json.loads(line[6:]))
                            return

            rtask = asyncio.create_task(reader())
            await asyncio.sleep(0.4)

            r = await c.post("/api/ingest/events", content=body,
                             headers={"Content-Type": "application/json",
                                      "X-Ticker-Signature": sign(body)})
            check(r.status_code == 202 and not r.json()["duplicate"],
                  "signed ingest accepted", str(r.status_code))

            try:
                await asyncio.wait_for(rtask, timeout=8)
            except TimeoutError:
                rtask.cancel()
            check(len(received) == 1, "event pushed to the live client",
                  f"{len(received)}")
            if received:
                got = received[0]
                check(got["state"] == "confirmed", "state delivered")
                check(got["payload"].get("future_field", {}).get("auto_invest") is True,
                      "UNKNOWN detector field survived verbatim",
                      "this is what makes the schema extensible")

            print("\n--- idempotency ---")
            r = await c.post("/api/ingest/events", content=body,
                             headers={"Content-Type": "application/json",
                                      "X-Ticker-Signature": sign(body)})
            check(r.status_code == 202 and r.json()["duplicate"] is True,
                  "re-ingest is a no-op, not a second alert")
            r = await c.get("/api/events", headers=auth)
            check(len(r.json()["events"]) == 1, "still exactly one event stored",
                  f"{len(r.json()['events'])}")

            print("\n--- retraction is a distinct event ---")
            retr = detector_payload("evt-live-1", "retracted")
            rbody = json.dumps(retr).encode()
            r = await c.post("/api/ingest/events", content=rbody,
                             headers={"Content-Type": "application/json",
                                      "X-Ticker-Signature": sign(rbody)})
            check(r.status_code == 202 and not r.json()["duplicate"],
                  "retraction accepted alongside the confirmation")
            r = await c.get("/api/events", headers=auth)
            states = [e["state"] for e in r.json()["events"]]
            check(sorted(states) == ["confirmed", "retracted"],
                  "both states retained", str(states))

            print("\n--- content ---")
            r = await c.get("/api/market/TRUMP?days=7")
            d = r.json()
            check(r.status_code == 200 and len(d["series"]) > 0,
                  "market chart returns a series",
                  f"{len(d['series'])} points, source={d['source']}")
            check(d["source"] in ("coingecko", "synthetic"),
                  "chart labels its data source honestly", d["source"])
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        except TimeoutError:
            task.cancel()

    print()
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all backend flow checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
