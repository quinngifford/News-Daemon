"""Outbound delivery to the public app backend — signing, durability, retry.

Runs a real receiving server on 127.0.0.1 that verifies the HMAC signature
exactly as the backend should, then takes it DOWN mid-test to prove an event
fired during an outage is not lost.

That last property is the one worth testing. Everything else in this repo is
about detecting the event; if the delivery to your users drops it, none of that
mattered.

Run: .venv/bin/python -m tests.test_webhook
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import tempfile
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request

from ticker.models import Alert, Evidence, TargetState, Tier
from ticker.notify.dispatcher import Dispatcher
from ticker.notify.webhook import SIGNATURE_HEADER, WebhookChannel, sign, verify
from ticker.store.db import Store

SECRET = "test-secret-do-not-use"
failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def make_alert(state: TargetState = TargetState.CONFIRMED) -> Alert:
    return Alert(
        target_id="Donald Trump", state=state,
        headline="Donald Trump has died at 79, White House confirms",
        url="https://apnews.com/x", score=0.966, detect_latency_ms=812.5,
        evidence=[Evidence(
            target_id="trump", source_id="x:AP", tier=Tier.WIRE, weight=0.97,
            url="https://x.com/AP/1", headline="Donald Trump has died at 79",
        )],
    )


class Backend:
    """Stand-in for the public app backend."""

    def __init__(self) -> None:
        self.received: list[dict] = []
        self.bad_signature = 0
        self.idempotency_keys: list[str] = []
        self.app = FastAPI()

        @self.app.post("/events")
        async def events(request: Request):
            body = await request.body()
            header = request.headers.get(SIGNATURE_HEADER, "")
            if not verify(SECRET, header, body):
                self.bad_signature += 1
                return {"ok": False}, 401
            self.idempotency_keys.append(request.headers.get("Idempotency-Key", ""))
            self.received.append(json.loads(body))
            return {"ok": True}


async def serve(app: FastAPI, port: int):
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error",
        access_log=False, lifespan="off"))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if getattr(server, "started", False):
            break
        await asyncio.sleep(0.05)
    return server, task


async def stop(server, task):
    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=5)
    except TimeoutError:
        task.cancel()


def test_signature_unit() -> None:
    print("--- signature ---")
    body = b'{"event_id":"abc"}'
    ts = int(time.time())
    header = sign(SECRET, ts, body)
    check(verify(SECRET, header, body), "valid signature verifies")
    check(not verify("wrong-secret", header, body), "wrong secret rejected")
    check(not verify(SECRET, header, b'{"event_id":"tampered"}'),
          "tampered body rejected")
    old = sign(SECRET, ts - 10_000, body)
    check(not verify(SECRET, old, body),
          "stale timestamp rejected", "blocks replay of a captured request")
    check(not verify(SECRET, "garbage", body), "malformed header rejected")


async def main() -> int:
    test_signature_unit()

    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "t.db")
        port = free_port()
        url = f"http://127.0.0.1:{port}/events"
        backend = Backend()

        import os
        os.environ["TICKER_WEBHOOK_SECRET"] = SECRET

        async with httpx.AsyncClient() as client:
            ch = WebhookChannel(client, store, url=url)
            check(ch.configured, "channel configured with url + secret")

            print("\n--- happy path ---")
            server, task = await serve(backend.app, port)
            dispatcher = Dispatcher([ch])
            await dispatcher.dispatch(make_alert())
            check(len(backend.received) == 1, "backend received the event",
                  f"{len(backend.received)}")
            check(backend.bad_signature == 0, "signature accepted by backend")
            if backend.received:
                p = backend.received[0]
                check(p["schema"] == "ticker.alert.v1", "schema version present")
                check(p["state"] == "confirmed", "state delivered")
                check(p["detect_latency_ms"] == 812.5, "latency delivered")
                check(p["evidence"][0]["source"] == "x:AP", "evidence delivered")
            check(backend.idempotency_keys[0].endswith(":confirmed"),
                  "Idempotency-Key sent", backend.idempotency_keys[0][-20:])
            check(store.outbox_stats()["pending"] == 0,
                  "nothing queued when delivery succeeds")

            print("\n--- backend DOWN when the event fires ---")
            await stop(server, task)
            alert2 = make_alert(TargetState.RETRACTED)
            await dispatcher.dispatch(alert2)
            pending = store.outbox_stats()["pending"]
            check(pending == 1, "event queued instead of lost", f"pending={pending}")
            check(len(backend.received) == 1, "backend saw nothing while down")

            print("\n--- durability across a process restart ---")
            store.close()
            store2 = Store(Path(td) / "t.db")
            check(store2.outbox_stats()["pending"] == 1,
                  "queued event survives restart",
                  "an in-memory retry would have lost it here")

            print("\n--- backend comes back ---")
            ch2 = WebhookChannel(client, store2, url=url)
            server, task = await serve(backend.app, port)
            # First attempt is scheduled 5s out; wait for it to become due.
            deadline = time.time() + 12
            delivered = 0
            while time.time() < deadline and not delivered:
                delivered = await ch2.deliver_pending()
                if not delivered:
                    await asyncio.sleep(1.0)
            check(delivered == 1, "queued event delivered on recovery",
                  f"{delivered}")
            check(len(backend.received) == 2, "backend now has both events",
                  f"{len(backend.received)}")
            if len(backend.received) == 2:
                check(backend.received[1]["state"] == "retracted",
                      "the retraction survived the outage")
                check(backend.received[1]["event_id"] == alert2.event_id,
                      "event_id preserved through the outbox")
            check(store2.outbox_stats()["pending"] == 0, "outbox drained")

            print("\n--- refuses to send unsigned ---")
            os.environ.pop("TICKER_WEBHOOK_SECRET")
            ch3 = WebhookChannel(client, store2, url=url)
            check(not ch3.configured, "channel not configured without a secret")
            before = len(backend.received)
            await ch3.send(make_alert(TargetState.LIKELY))
            check(len(backend.received) == before,
                  "no unsigned event sent to the backend")
            check(store2.outbox_stats()["pending"] == 1,
                  "unsigned event queued, not dropped")

            await stop(server, task)
            store2.close()

    print()
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all webhook checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
