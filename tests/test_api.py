"""API + SSE tests. No network, no server process — ASGI transport in-process.

Covers the delivery half of the system, which until now had zero coverage:
health/status shape, subscription storage, and that an alert dispatched through
the SSE channel actually reaches a connected browser.

Run: .venv/bin/python -m tests.test_api
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx

from ticker.api.app import ApiState, create_app
from ticker.config import build_automaton, load_targets
from ticker.models import Alert, Evidence, TargetState, Tier
from ticker.notify.broadcast import SseBroadcaster
from ticker.notify.dispatcher import Dispatcher
from ticker.screen.funnel import Funnel
from ticker.store.db import Store

failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def make_alert(state: TargetState = TargetState.CONFIRMED) -> Alert:
    return Alert(
        target_id="Donald Trump",
        state=state,
        headline="Donald Trump has died at 79, White House confirms",
        url="https://apnews.com/x",
        score=0.966,
        detect_latency_ms=1234.5,
        evidence=[Evidence(
            target_id="trump", source_id="x:AP", tier=Tier.WIRE, weight=0.97,
            url="https://x.com/AP/1", headline="Donald Trump has died at 79",
        )],
    )


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def sse_over_real_server(app, broadcaster: SseBroadcaster) -> None:
    """SSE needs a real server, on 127.0.0.1 only.

    httpx.ASGITransport buffers the whole response body — it waits for an ASGI
    `more_body: False` that an infinite event stream never sends — so the
    in-process transport used above cannot test streaming at all. Since the SSE
    channel is the lowest-latency notification path in the system, it gets a real
    (loopback-bound, ephemeral-port) server rather than no coverage.
    """
    import uvicorn

    print("\n--- SSE over a real server: alert reaches a browser ---")
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error",
        access_log=False, lifespan="off",
    ))
    task = asyncio.create_task(server.serve())

    try:
        # Wait for bind rather than sleeping a guessed interval.
        for _ in range(100):
            if getattr(server, "started", False):
                break
            await asyncio.sleep(0.05)
        check(getattr(server, "started", False), "test server started",
              f"127.0.0.1:{port}")

        received: list[dict] = []
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as rc:
            async with rc.stream("GET", "/api/events",
                                 timeout=httpx.Timeout(10.0, read=None)) as resp:
                check(resp.status_code == 200, "SSE stream opens",
                      str(resp.status_code))
                check("text/event-stream" in resp.headers.get("content-type", ""),
                      "SSE content-type correct")

                async def pump() -> None:
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            received.append(json.loads(line[6:]))
                            if len(received) >= 2:
                                return

                reader = asyncio.create_task(pump())
                await asyncio.sleep(0.3)
                check(broadcaster.subscriber_count == 1,
                      "broadcaster sees the connected client")

                # Through the real Dispatcher, so this exercises the same path a
                # live alert takes rather than poking the broadcaster directly.
                dispatcher = Dispatcher([broadcaster])
                await dispatcher.dispatch(make_alert())
                await dispatcher.dispatch(make_alert(TargetState.RETRACTED))

                try:
                    await asyncio.wait_for(reader, timeout=8.0)
                except TimeoutError:
                    reader.cancel()

        check(len(received) == 2, "SSE delivered both alerts", f"got {len(received)}")
        if len(received) == 2:
            check(received[0]["state"] == "confirmed", "first is confirmed")
            check(received[1]["state"] == "retracted", "second is retracted")
            check(received[0]["detect_latency_ms"] == 1234.5,
                  "detect latency preserved to the browser")
            check(received[0]["evidence"][0]["source"] == "x:AP",
                  "evidence trail reaches the browser")
            check(received[0]["event_id"] != "", "event_id present for dedupe")
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except TimeoutError:
            task.cancel()


async def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "t.db")
        targets = load_targets()
        broadcaster = SseBroadcaster()
        state = ApiState(
            funnel=Funnel(targets, build_automaton(targets)),
            confirmer=None, adapters=[], store=store,
            broadcaster=broadcaster, dry_run=True,
        )
        app = create_app(state)
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as c:
            print("--- static assets ---")
            for path, needle in [
                ("/", "NEWS-TICKER"),
                ("/sw.js", "showNotification"),
                ("/manifest.webmanifest", "standalone"),
                ("/static/app.js", "EventSource"),
                ("/static/icon.svg", "<svg"),
            ]:
                r = await c.get(path)
                ok = r.status_code == 200 and needle in r.text
                check(ok, f"GET {path}", f"{r.status_code}")

            print("\n--- health & status ---")
            r = await c.get("/api/health")
            h = r.json()
            check(r.status_code == 200, "GET /api/health", str(r.status_code))
            check("funnel" in h and "sources" in h, "health has expected shape")
            check(h["ok"] is False,
                  "health NOT ok with no canary run",
                  "a pipeline with no canary PASS is not known to work")
            check(h["dry_run"] is True, "dry_run reported")

            store.record_canary(True, 3.2, "ok:all")
            h2 = (await c.get("/api/health")).json()
            check(h2["ok"] is True, "health ok once canary has passed")
            check(h2["canary"]["passed"] is True, "canary surfaced in health")

            r = await c.get("/api/status")
            check(r.status_code == 200, "GET /api/status", str(r.status_code))

            print("\n--- alerts list ---")
            r = await c.get("/api/alerts")
            check(r.json()["alerts"] == [], "alerts empty on fresh db")
            a = make_alert()
            store.record_alert(a, weight=1.0, trail=["[T0] x:AP +0s — died"])
            rows = (await c.get("/api/alerts")).json()["alerts"]
            check(len(rows) == 1, "recorded alert is listed")
            check(rows[0]["state"] == "confirmed", "alert state round-trips")
            check(isinstance(rows[0].get("trail"), list), "trail_json decoded")

            print("\n--- web push subscription ---")
            r = await c.get("/api/vapid-public-key")
            check(r.status_code == 503,
                  "vapid key 503s when unconfigured",
                  "clear signal to run tools/gen_vapid_keys.py")

            r = await c.post("/api/subscribe", json={"endpoint": "x"})
            check(r.status_code == 400, "subscribe rejects malformed body",
                  str(r.status_code))

            sub = {"endpoint": "https://fcm.example/abc",
                   "keys": {"p256dh": "k", "auth": "a"}}
            r = await c.post("/api/subscribe", json=sub)
            check(r.status_code == 200, "subscribe accepts valid body")
            check(len(store.get_subscriptions()) == 1, "subscription persisted")
            await c.post("/api/subscribe", json=sub)
            check(len(store.get_subscriptions()) == 1,
                  "re-subscribing does not duplicate")
            r = await c.post("/api/unsubscribe", json={"endpoint": sub["endpoint"]})
            check(r.status_code == 200 and not store.get_subscriptions(),
                  "unsubscribe removes it")

        await sse_over_real_server(app, broadcaster)

        # Must come AFTER dispatch, or there is nothing to replay.
        print("\n--- SSE history replay on reconnect ---")
        hist_q = broadcaster.subscribe()
        check(hist_q.qsize() == 2,
              "reconnecting client replays recent alerts",
              f"{hist_q.qsize()} buffered")
        broadcaster.unsubscribe(hist_q)
        check(broadcaster.subscriber_count == 0, "unsubscribe cleans up")

        store.close()

    print()
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all API/SSE checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
