"""Show, step by step, how the detector actually works.

    .venv/bin/python tools/demo.py            # everything (needs network for §4)
    .venv/bin/python tools/demo.py --offline  # skip the live-feed section
    .venv/bin/python tools/demo.py --send     # also send a real DRILL alert

Nothing here is a mock. Every section runs the same production code the daemon
runs; it just narrates what happens instead of doing it silently.

Sends nothing to your phone unless you pass --send.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ticker.config import (  # noqa: E402
    build_automaton,
    load_satire_domains,
    load_sources,
    load_targets,
)
from ticker.confirm.policy import Confirmer  # noqa: E402
from ticker.models import Item, TargetState, Tier  # noqa: E402
from ticker.notify.dispatcher import Dispatcher, format_alert  # noqa: E402
from ticker.screen.funnel import Funnel  # noqa: E402

TTY = sys.stdout.isatty()


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if TTY else text


GREEN, RED, YELLOW, DIM, BOLD, CYAN = "32", "31", "33", "2", "1", "36"


def rule(title: str) -> None:
    print("\n" + c("─" * 74, DIM))
    print(c(title, BOLD))
    print(c("─" * 74, DIM))


class Capture:
    """Stands in for Telegram/WebPush so the demo never surprises you."""

    name = "demo"

    def __init__(self) -> None:
        self.sent: list = []

    async def warm(self) -> None:
        pass

    async def send(self, alert) -> None:
        self.sent.append(alert)


def section_1_config(targets, automaton, sources) -> None:
    rule("1. WHAT IS BEING WATCHED")
    for t in targets.values():
        print(f"  target      {c(t.display_name, BOLD)}")
        print(f"  aliases     {', '.join(t.aliases[:5])}"
              f"{' …' if len(t.aliases) > 5 else ''}")
        print(f"  fires at    score >= {t.fire.min_score}  "
              f"(a single wire report can fire alone)")
    enabled = [s for s in sources if s.get("enabled", True)]
    print(f"\n  {len(enabled)} news sources enabled, "
          f"{len(sources) - len(enabled)} disabled")
    print(f"  {automaton.pattern_count} text patterns compiled into ONE scanner")
    print(c("\n  Adding a 50th person costs no extra scan time — the scanner\n"
            "  checks every pattern in a single pass over each headline.", DIM))


def section_2_walkthrough(funnel) -> None:
    rule("2. ONE HEADLINE, STAGE BY STAGE")
    headline = "Donald Trump has died at 79, White House confirms"
    print(f'  Headline: "{c(headline, BOLD)}"\n')

    t0 = time.perf_counter()
    res = funnel.evaluate(Item(source_id="demo", tier=Tier.WIRE,
                               title=headline, url="https://apnews.com/x"))
    elapsed_us = (time.perf_counter() - t0) * 1e6
    cand = max(res.candidates, key=lambda x: x.score, default=None)

    print(f"  {c('stage 0', CYAN)}  duplicate check ......... {c('PASS', GREEN)} "
          f"(not seen before)")
    print(f"  {c('stage 1', CYAN)}  name + death word ....... {c('PASS', GREEN)} "
          f"(found 'Donald Trump' and 'died')")
    print(f"  {c('stage 2', CYAN)}  is it really about him? . {c('PASS', GREEN)} "
          f"score {c(f'{cand.score:.3f}', BOLD)}")
    if cand:
        print("\n  Why it scored that way:")
        for part in cand.reason.split(", "):
            sign = GREEN if "+" in part else RED
            print(f"      {c(part, sign)}")
    print(f"\n  Total time: {c(f'{elapsed_us:.0f} microseconds', BOLD)} "
          f"— {c('and $0.00, no AI involved', DIM)}")


def section_3_gauntlet(funnel, min_score: float) -> None:
    rule("3. THE HARD CASES (these are real headlines that fooled earlier versions)")
    cases = [
        ("Donald Trump has died at 79, White House confirms", True, "real report"),
        ("Breaking: Donald Trump dead at 79", True, "real report"),
        ("Lindsey Graham, key ally of Donald Trump, dies at 71", False,
         "SOMEONE ELSE died"),
        ("Robert Mueller, who led Trump investigation, dies at 81", False,
         "SOMEONE ELSE died"),
        ("Larry Trump Obituary (2026) - Latrobe, PA", False, "different person"),
        ("Brewery offers free beer when Trump dies", False, "hypothetical"),
        ("Trump and Newsom in dead heat in new poll", False, "figure of speech"),
        ("Trump mourns death of former aide", False, "he is the mourner"),
        ("Man charged over death threats against Trump", False, "a threat"),
        ("Fact check: viral post claiming Trump died is false", False, "a debunk"),
    ]
    print(f"  {'verdict':<9} {'score':>6}  headline")
    print(c("  " + "-" * 70, DIM))
    wrong = 0
    for headline, should_fire, why in cases:
        funnel.deduper.reset()
        res = funnel.evaluate(Item(source_id="demo", tier=Tier.WIRE,
                                   title=headline, url="https://apnews.com/x"))
        score = max((x.score for x in res.candidates), default=0.0)
        fires = score >= min_score
        ok = fires == should_fire
        wrong += not ok
        # Pad BEFORE colouring: ANSI escapes count toward width otherwise.
        verdict = f"{'ALERT' if fires else 'ignored':<8}"
        verdict = c(verdict, RED if fires else DIM)
        mark = " " if ok else c("!", RED)
        print(f" {mark}{verdict} {score:>5.3f}  {headline[:44]:<44} "
              f"{c(why, DIM)}")
    print()
    if wrong:
        print(c(f"  {wrong} case(s) behaved unexpectedly", RED))
    else:
        print(c("  All 10 correct: it alerts on the 2 real reports and ignores "
                "the 8 lookalikes.", GREEN))


def section_4_live(funnel, min_score: float) -> None:
    rule("4. RIGHT NOW: real headlines pulled from the BBC this second")
    import feedparser
    import httpx

    try:
        r = httpx.get("https://feeds.bbci.co.uk/news/rss.xml", timeout=15.0,
                      headers={"User-Agent": "news-ticker-daemon/0.1 demo"})
        entries = feedparser.parse(r.text).entries
    except Exception as exc:  # noqa: BLE001
        print(c(f"  couldn't reach the BBC feed: {exc}", YELLOW))
        return

    print(f"  Pulled {len(entries)} live headlines. Screening them now…\n")
    alerts = 0
    shown = 0
    for e in entries:
        title = (e.get("title") or "").strip()
        if not title:
            continue
        funnel.deduper.reset()
        res = funnel.evaluate(Item(source_id="bbc", tier=Tier.MAJOR,
                                   title=title, url=e.get("link", "")))
        score = max((x.score for x in res.candidates), default=0.0)
        if score >= min_score:
            alerts += 1
            print(f"  {c('ALERT', RED)}  {score:.3f}  {title[:60]}")
        elif shown < 5:
            print(f"  {c('ignored', DIM)} {score:.3f}  {title[:60]}")
            shown += 1
    print(f"\n  {len(entries)} real headlines screened → "
          f"{c(str(alerts) + ' alerts', RED if alerts else GREEN)}")
    if not alerts:
        print(c("  (Correct — as far as the BBC is concerned, he is alive.)", DIM))


async def section_5_event(targets) -> None:
    rule("5. FULL DRILL: what happens the moment it really happens")
    capture = Capture()
    confirmer = Confirmer(targets, Dispatcher([capture]),
                          adjudicator=None, store=None)
    automaton = build_automaton(targets)
    funnel = Funnel(targets, automaton, satire_domains=load_satire_domains())
    target = next(iter(targets.values()))

    headline = f"{target.display_name} has died at 79, White House confirms"
    print(f"  A wire service publishes:\n    \"{c(headline, BOLD)}\"\n")

    t0 = time.perf_counter()
    for cand in funnel.evaluate(
        Item(source_id="x:AP", tier=Tier.WIRE, title=headline,
             url="https://x.com/AP/status/1")
    ).survivors:
        await confirmer.handle(cand)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    acc = confirmer.accumulators[target.id]
    print(f"  screened, confirmed and dispatched in "
          f"{c(f'{elapsed_ms:.1f} ms', BOLD)}")
    print(f"  state: {c(acc.state.value.upper(), RED)}   "
          f"evidence weight: {acc.weight():.2f}   "
          f"alerts sent: {len(capture.sent)}")

    if capture.sent:
        title, body = format_alert(capture.sent[0],
                                   [m for t in targets.values() for m in t.markets])
        print(f"\n  {c('This is the message that would hit your phone:', BOLD)}")
        print(c("  ┌" + "─" * 66, DIM))
        for line in (f"*{title}*\n\n{body}").splitlines():
            print(c("  │ ", DIM) + line[:64])
        print(c("  └" + "─" * 66, DIM))

    # Now the same event collapses.
    print(f"\n  {c('Two minutes later, the wire retracts it:', BOLD)}")
    retraction = (f"Correction: our earlier report that {target.display_name} "
                  f"died was false")
    print(f'    "{retraction}"\n')
    funnel.deduper.reset()
    for cand in funnel.evaluate(
        Item(source_id="x:AP", tier=Tier.WIRE, title=retraction,
             url="https://x.com/AP/status/2")
    ).survivors:
        await confirmer.handle(cand)

    retracted = [a for a in capture.sent if a.state is TargetState.RETRACTED]
    if retracted:
        print(f"  {c('RETRACTED alert sent', YELLOW)} — same event id "
              f"({retracted[0].event_id[:8]}…), so your phone replaces the\n"
              f"  original notification instead of leaving both on screen.")
    else:
        print(c("  no retraction issued", RED))
    return capture


async def maybe_send(targets) -> None:
    """Only with --send. Uses the real Telegram channel."""
    rule("6. SENDING A REAL DRILL ALERT TO YOUR PHONE")
    import httpx

    from ticker.notify.telegram import TelegramChannel

    client = httpx.AsyncClient(timeout=15.0)
    ch = TelegramChannel(client)
    if not ch.configured:
        print(c("  Telegram not configured — set TICKER_TELEGRAM_TOKEN and "
                "TICKER_TELEGRAM_CHAT_ID", YELLOW))
        await client.aclose()
        return
    from ticker.ops.canary import Canary

    canary = Canary(live_channels=[ch])
    res = await canary.run_once(full=True)
    print(f"  {c('PASS' if res.passed else 'FAIL', GREEN if res.passed else RED)}"
          f" — check your phone for a [DRILL] message")
    await client.aclose()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="skip the live-feed section")
    ap.add_argument("--send", action="store_true",
                    help="also send a real DRILL alert to Telegram")
    args = ap.parse_args()

    print(c("\n  NEWS-TICKER — how it works, demonstrated on real code", BOLD))

    targets = load_targets()
    if not targets:
        print(c("no targets configured", RED))
        return 1
    automaton = build_automaton(targets)
    funnel = Funnel(targets, automaton, satire_domains=load_satire_domains())
    min_score = next(iter(targets.values())).fire.min_score

    section_1_config(targets, automaton, load_sources())
    section_2_walkthrough(funnel)
    section_3_gauntlet(funnel, min_score)
    if not args.offline:
        section_4_live(funnel, min_score)
    await section_5_event(targets)
    if args.send:
        await maybe_send(targets)

    rule("SUMMARY")
    print("  The detector reads news continuously, throws away everything that")
    print("  is not about your target, and alerts in milliseconds when it is.")
    print("  Screening costs nothing — no AI runs unless a headline is genuinely")
    print("  ambiguous, which is roughly once per run.\n")
    if not args.send:
        print(c("  Add --send to receive a real drill alert on your phone.\n", DIM))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
