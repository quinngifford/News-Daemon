"""Daemon entrypoint. Wires ingest → screen → confirm → notify and runs forever.

    .venv/bin/python -m ticker              # run the daemon
    .venv/bin/python -m ticker --dry-run    # screen live sources, never notify

The single-process design is deliberate: on a 2 vCPU / 911 MB box, one asyncio
loop with a bounded queue beats multiple processes plus a broker, and the
screening stages are microseconds — they are not the bottleneck.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from ticker.adjudicate.llm import Adjudicator
from ticker.api.app import ApiState, create_app, serve
from ticker.config import (
    build_automaton,
    load_satire_domains,
    load_settings,
    load_sources,
    load_targets,
)
from ticker.confirm.policy import Confirmer
from ticker.ingest.registry import build_adapters, make_client
from ticker.models import Item
from ticker.notify.broadcast import SseBroadcaster
from ticker.notify.dispatcher import Dispatcher
from ticker.notify.telegram import TelegramChannel
from ticker.notify.webpush import WebPushChannel
from ticker.ops.canary import Canary
from ticker.ops.health import HealthMonitor, sd_notify
from ticker.screen.dedupe import Deduper
from ticker.screen.funnel import Funnel
from ticker.store.db import Store

log = logging.getLogger("ticker")


async def run(dry_run: bool = False, duration: float | None = None) -> None:
    settings = load_settings()
    targets = load_targets()
    if not targets:
        log.error("no enabled targets in config/targets/ — nothing to watch")
        return

    log.info("watching %d target(s): %s", len(targets),
             ", ".join(t.display_name for t in targets.values()))

    automaton = build_automaton(targets)
    log.info("automaton compiled: %d patterns", automaton.pattern_count)

    scr = settings.get("screen", {})
    funnel = Funnel(
        targets,
        automaton,
        Deduper(
            ttl_s=scr.get("dedupe_ttl_s", 3600),
            hamming_threshold=scr.get("dedupe_hamming_threshold", 3),
            max_entries=scr.get("dedupe_max_entries", 50_000),
        ),
        satire_domains=load_satire_domains(),
    )

    store = Store(settings.get("store", {}).get("path", "var/ticker.db"))
    client = make_client()

    # --- notification channels ------------------------------------------
    markets = [m for t in targets.values() for m in t.markets]
    # The SSE broadcaster is safe in dry-run: it only feeds an open dashboard,
    # it cannot buzz a phone or spend money.
    broadcaster = SseBroadcaster()
    channels: list = [broadcaster]
    if not dry_run:
        nt = settings.get("notify", {})
        enabled = nt.get("channels", ["telegram"])
        if "telegram" in enabled:
            channels.append(TelegramChannel(client, markets=markets))
        if "webpush" in enabled:
            wp = nt.get("webpush", {})
            channels.append(
                WebPushChannel(
                    get_subscriptions=store.get_subscriptions,
                    drop_subscription=store.drop_subscription,
                    subject=wp.get("vapid_subject", "mailto:you@example.com"),
                    ttl_s=wp.get("ttl_s", 300),
                    urgency=wp.get("urgency", "high"),
                    markets=markets,
                )
            )
    dispatcher = Dispatcher(channels)
    await dispatcher.warm()
    if len(channels) == 1 and not dry_run:
        log.warning("ONLY the dashboard SSE channel is active — no phone alerts. "
                    "Set TICKER_TELEGRAM_TOKEN / run tools/gen_vapid_keys.py")

    # --- adjudicator (optional) ------------------------------------------
    adj_cfg = settings.get("adjudicate", {})
    adjudicator = None
    if adj_cfg.get("enabled", True) and os.environ.get("ANTHROPIC_API_KEY"):
        from anthropic import AsyncAnthropic

        adjudicator = Adjudicator(
            AsyncAnthropic(),
            model=adj_cfg.get("model", "claude-haiku-4-5-20251001"),
            max_tokens=adj_cfg.get("max_tokens", 300),
            timeout_s=adj_cfg.get("timeout_s", 6.0),
            monthly_budget_usd=adj_cfg.get("monthly_budget_usd", 2.00),
            fail_open=adj_cfg.get("fail_open_to_rules_score", True),
        )
        log.info("adjudicator enabled (%s, budget $%.2f/mo)",
                 adjudicator.model, adjudicator.monthly_budget_usd)
    else:
        log.warning("adjudicator disabled — Stage-2 score decides alone "
                    "(set ANTHROPIC_API_KEY to enable)")

    confirmer = Confirmer(targets, dispatcher, adjudicator, store)

    # --- ingest → queue → screen ----------------------------------------
    queue: asyncio.Queue[Item] = asyncio.Queue(
        maxsize=settings.get("daemon", {}).get("queue_maxsize", 5000)
    )

    async def emit(item: Item) -> None:
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            # Visible backpressure beats unbounded memory growth. If this fires,
            # screening cannot keep up with ingest and you need to know.
            log.error("QUEUE FULL — dropping item from %s", item.source_id)

    adapters = build_adapters(load_sources(), targets, client)
    if not adapters:
        log.error("no ingest adapters built — check config/sources.yaml")
        return

    async def screen_loop() -> None:
        while True:
            item = await queue.get()
            try:
                result = funnel.evaluate(item)
                for cand in result.candidates:
                    # Persist everything that reached Stage 2, survivor or not:
                    # the rejections are the training data for tuning.
                    await asyncio.to_thread(store.record_item, cand)
                    if cand.alive:
                        log.info("CANDIDATE %s score=%.3f [%s] %s",
                                 cand.target_id, cand.score, cand.reason,
                                 item.title[:100])
                        if not dry_run:
                            await confirmer.handle(cand)
            except Exception:
                log.exception("screening failed for item from %s", item.source_id)
            finally:
                queue.task_done()

    async def stats_loop() -> None:
        while True:
            await asyncio.sleep(300)
            log.info("funnel: %s | queue=%d | confirmer=%s",
                     funnel.report(), queue.qsize(), confirmer.stats)

    async def maintenance_loop() -> None:
        """Prune the audit trail daily.

        Every item reaching Stage 2 is recorded, so without this the database
        grows without bound. On a box with ~3.4 GB free that is a slow-motion
        outage: the daemon dies of a full disk at an unpredictable moment, which
        is precisely the failure this system cannot have. Alerts are never
        pruned — they are the point.
        """
        retain = int(settings.get("store", {}).get("retain_days", 90))
        while True:
            await asyncio.sleep(86400)
            try:
                await asyncio.to_thread(store.prune, retain)
                size_mb = (
                    store.path.stat().st_size / 1e6 if store.path.exists() else 0
                )
                log.info("pruned rows older than %dd; db now %.1f MB",
                         retain, size_mb)
            except Exception:
                log.exception("maintenance prune failed")

    health = HealthMonitor(
        adapters,
        staleness_multiplier=settings.get("ops", {}).get("staleness_multiplier", 4.0),
        heartbeat_interval_s=settings.get("ops", {}).get("heartbeat_interval_s", 15),
        store=store,
    )

    tasks = [
        asyncio.create_task(a.run(emit), name=f"ingest:{a.id}") for a in adapters
    ]
    tasks += [
        asyncio.create_task(screen_loop(), name="screen"),
        asyncio.create_task(health.run(), name="health"),
        asyncio.create_task(stats_loop(), name="stats"),
        asyncio.create_task(maintenance_loop(), name="maintenance"),
    ]

    api_cfg = settings.get("api", {})
    api_state = ApiState(
        funnel=funnel, confirmer=confirmer, adapters=adapters, store=store,
        broadcaster=broadcaster, adjudicator=adjudicator, dry_run=dry_run,
    )
    tasks.append(
        asyncio.create_task(
            serve(create_app(api_state),
                  host=api_cfg.get("host", "127.0.0.1"),
                  port=int(api_cfg.get("port", 8080))),
            name="api",
        )
    )
    log.info("dashboard on http://%s:%s",
             api_cfg.get("host", "127.0.0.1"), api_cfg.get("port", 8080))

    can_cfg = settings.get("ops", {}).get("canary", {})
    if can_cfg.get("enabled", True):
        canary = Canary(
            max_latency_ms=can_cfg.get("max_latency_ms", 8000),
            store=store,
            live_channels=channels,
        )
        # Drill immediately at startup: if the pipeline is broken, you want to
        # know now, not at 09:00 tomorrow.
        await canary.run_once()
        tasks.append(
            asyncio.create_task(
                canary.run_daily(
                    hour_utc=can_cfg.get("hour_utc", 9),
                    full_on_weekday=can_cfg.get("full_on_weekday", 0),
                ),
                name="canary",
            )
        )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    # Tell systemd we are up. Sent explicitly here rather than relying on the
    # health task being scheduled first — with Type=notify, a missing READY=1
    # means systemd kills the service at TimeoutStartSec.
    sd_notify("READY=1")

    log.info("daemon up: %d adapters, dry_run=%s%s", len(adapters), dry_run,
             f", exiting after {duration:.0f}s" if duration else "")
    if duration:
        try:
            await asyncio.wait_for(stop.wait(), timeout=duration)
        except TimeoutError:
            log.info("duration elapsed")
    else:
        await stop.wait()

    log.info("final funnel stats: %s", funnel.report())
    log.info("source health:")
    for h in health.snapshot():
        log.info("  %-34s items=%-5d stale=%-5s %s",
                 h["id"], h["items"], h["stale"], h.get("last_error") or "")
    log.info("shutting down")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await client.aclose()
    store.close()


def main() -> int:
    ap = argparse.ArgumentParser(prog="ticker")
    ap.add_argument("--dry-run", action="store_true",
                    help="screen live sources but never notify")
    ap.add_argument("--canary", action="store_true",
                    help="run the synthetic drill once, print the report, exit "
                         "(exit 0 = pass; suitable for cron)")
    ap.add_argument("--canary-full", action="store_true",
                    help="as --canary, but also dispatch through the LIVE "
                         "notification channels to verify credentials")
    ap.add_argument("--duration", type=float, default=None,
                    help="exit after N seconds (for timed dry-run observation)")
    ap.add_argument("--log-level", default=None)
    args = ap.parse_args()

    settings = load_settings()
    level = args.log_level or settings.get("daemon", {}).get("log_level", "INFO")
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    if args.canary or args.canary_full:
        return _run_canary(settings, full=args.canary_full)

    try:
        asyncio.run(run(dry_run=args.dry_run, duration=args.duration))
    except KeyboardInterrupt:
        pass
    return 0


def _run_canary(settings: dict, *, full: bool) -> int:
    """Standalone drill. Exit code is the signal, so cron can alert on failure."""
    can_cfg = settings.get("ops", {}).get("canary", {})
    channels: list = []
    if full:
        from ticker.ingest.registry import make_client

        async def _with_live() -> int:
            client = make_client()
            chans = [TelegramChannel(client)]
            d = Dispatcher(chans)
            await d.warm()
            c = Canary(max_latency_ms=can_cfg.get("max_latency_ms", 8000),
                       live_channels=chans)
            res = await c.run_once(full=True)
            print(res.report())
            await client.aclose()
            return 0 if res.passed else 1

        return asyncio.run(_with_live())

    canary = Canary(max_latency_ms=can_cfg.get("max_latency_ms", 8000),
                    live_channels=channels)
    res = asyncio.run(canary.run_once())
    print(res.report())
    return 0 if res.passed else 1


if __name__ == "__main__":
    sys.exit(main())
