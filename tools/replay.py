"""Backtest the screening funnel against real historical news coverage.

    .venv/bin/python tools/replay.py            # use cache, offline after first run
    .venv/bin/python tools/replay.py --fetch    # download missing windows
    .venv/bin/python tools/replay.py --verify   # re-check death dates vs Wikidata
    .venv/bin/python tools/replay.py --misses   # show what each miss looked like

You cannot wait for the real event to find out whether this works. This replays
actual coverage of people who really did die — and people who did not — through
the UNMODIFIED funnel, and reports what would have happened.

The point it tests that nothing else does: **generalisation.** Every rule in
screen/rules.py was tuned against Donald Trump headlines. This synthesises a
target config for each person in the corpus and replays their real coverage, so
overfitting shows up as a miss on somebody else.

Two honest limitations, so nobody over-reads the output:

  * GDELT `seendate` is bucketed to 15 minutes, so detection lag is measured at
    15-minute resolution. This validates WHETHER we would have caught the first
    wave, not how many milliseconds it took. Real latency is measured live, by
    the stamps in ticker/models.py.
  * GDELT indexes a broad but incomplete slice of the web, and its titles are
    tokenised oddly (`Jimmy Carter , 100 ,`). Both are handled below, but this is
    a proxy for the live feed set, not a replica of it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import yaml  # noqa: E402

from ticker.config import (  # noqa: E402
    CONFIG_DIR,
    FirePolicy,
    Target,
    build_automaton,
    load_satire_domains,
)
from ticker.models import Item, Stage, Tier  # noqa: E402
from ticker.screen.funnel import Funnel  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "var" / "replay_cache"
EVENTS = CONFIG_DIR / "replay_events.yaml"

GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
WIKIDATA = "https://www.wikidata.org/w/api.php"
UA = {"User-Agent": "news-ticker-daemon/0.1 (backtest research)"}

# GDELT's 429 body says "one every 5 seconds", but measurement shows a stricter
# longer-window quota underneath: after a run of ~9 windows, even 7-second gaps
# returned 429 for every subsequent request. Treat 5s as a floor, not a target.
GDELT_MIN_INTERVAL_S = 15.0

# GDELT's artlist mode returns at most 250 records per query. Any window that
# hits this is a PARTIAL sample, which is why lag is reported as unreliable
# there (see EventResult.sample_capped).
GDELT_MAX_RECORDS = 250

_last_call = 0.0


class RateLimited(Exception):
    """GDELT quota exhausted. Distinct from 'this window has no articles'.

    The distinction matters more than it looks: conflating them is what let a
    failed fetch be cached as an empty result, permanently poisoning the corpus
    with a silent zero. A backtest that quietly tests nothing is worse than one
    that fails.
    """


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------


def _throttle() -> None:
    global _last_call
    wait = GDELT_MIN_INTERVAL_S - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def fetch_window(query: str, start: datetime, hours: int,
                 maxrecords: int = GDELT_MAX_RECORDS) -> list[dict]:
    """One GDELT window, throttled, with backoff on 429."""
    end = start + timedelta(hours=hours)
    params = {
        "query": f"{query} sourcelang:english",
        "mode": "artlist",
        "maxrecords": maxrecords,
        "startdatetime": start.strftime("%Y%m%d%H%M%S"),
        "enddatetime": end.strftime("%Y%m%d%H%M%S"),
        "format": "json",
        "sort": "datedesc",
    }
    last = "unknown"
    for attempt in range(4):
        _throttle()
        try:
            r = httpx.get(GDELT, params=params, timeout=45.0, headers=UA)
        except httpx.HTTPError as exc:
            last = f"network error: {exc}"
            print(f"    {last}")
            continue
        if r.status_code == 429 or "limit requests" in r.text[:200]:
            backoff = 20 * (attempt + 1)
            print(f"    rate-limited, backing off {backoff}s "
                  f"(attempt {attempt + 1}/4)")
            time.sleep(backoff)
            last = "rate limited"
            continue
        if r.status_code != 200:
            last = f"http {r.status_code}"
            print(f"    {last}")
            continue
        try:
            # A 200 with an empty list is a REAL answer: this window genuinely
            # has no matching coverage. Only that gets cached.
            return r.json().get("articles", []) or []
        except json.JSONDecodeError:
            last = f"non-JSON: {r.text[:80]}"
            print(f"    {last}")
    raise RateLimited(f"gave up after 4 attempts ({last})")


def wikidata_deaths(qids: list[str]) -> dict[str, tuple[str, str | None]]:
    """Batch lookup of label + P570. One request; per-id calls get rate-limited."""
    r = httpx.get(WIKIDATA, params={
        "action": "wbgetentities", "ids": "|".join(sorted(set(qids))),
        "props": "claims|labels", "languages": "en", "format": "json",
    }, timeout=45.0, headers=UA)
    r.raise_for_status()
    out: dict[str, tuple[str, str | None]] = {}
    for qid, e in (r.json().get("entities") or {}).items():
        label = e.get("labels", {}).get("en", {}).get("value", "?")
        claims = e.get("claims", {}).get("P570")
        died = (
            claims[0]["mainsnak"]["datavalue"]["value"]["time"][1:11]
            if claims else None
        )
        out[qid] = (label, died)
    return out


# --------------------------------------------------------------------------
# corpus handling
# --------------------------------------------------------------------------

# GDELT tokenises titles: "Jimmy Carter , 100 , dies". Left as-is this inflates
# token distances and weakens the proximity feature, making replay look worse
# than live. Normalising here keeps the comparison fair — and is confined to this
# tool so it can never affect production screening.
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?')\]])")
_SPACE_AFTER_OPEN = re.compile(r"([('\[])\s+")
_MULTISPACE = re.compile(r"\s{2,}")


def normalise(title: str) -> str:
    t = _SPACE_BEFORE_PUNCT.sub(r"\1", title or "")
    t = _SPACE_AFTER_OPEN.sub(r"\1", t)
    t = re.sub(r"\b([A-Z])\s+\.", r"\1.", t)          # "U . S ." -> "U. S."
    # then join consecutive initials: "U. S." -> "U.S."
    t = re.sub(r"\b([A-Z]\.)\s+(?=[A-Z]\.)", r"\1", t)
    return _MULTISPACE.sub(" ", t).strip()


def parse_seendate(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def make_target(spec: dict) -> Target:
    """Synthesize a target config, mirroring what a real YAML would contain."""
    return Target(
        id=spec["id"],
        display_name=spec["display_name"],
        aliases=spec["aliases"],
        strong_aliases=spec.get("strong_aliases", []),
        wikidata_id=spec.get("wikidata_id"),
        fire=FirePolicy(),          # production defaults, deliberately
    )


@dataclass
class EventResult:
    id: str
    kind: str                       # positive | negative
    n_articles: int = 0
    detected: bool = False
    lag_minutes: float | None = None
    # True when the window hit GDELT's record cap. Detection and false-positive
    # rates are unaffected by WHICH 250 articles you sample, but lag absolutely
    # is: with sort=datedesc a capped window contains only the newest articles,
    # so the "first" detection is an artifact of sampling, not of the pipeline.
    sample_capped: bool = False
    max_score: float = 0.0
    n_above_fire: int = 0
    n_candidates: int = 0
    kill_stages: dict[str, int] = field(default_factory=dict)
    top_hits: list[tuple[float, str]] = field(default_factory=list)
    misses: list[tuple[str, str]] = field(default_factory=list)


def replay_event(spec: dict, articles: list[dict], kind: str,
                 death_dt: datetime | None) -> EventResult:
    target = make_target(spec)
    targets = {target.id: target}
    funnel = Funnel(targets, build_automaton(targets),
                    satire_domains=load_satire_domains())
    res = EventResult(id=spec["id"], kind=kind, n_articles=len(articles))
    min_score = target.fire.min_score
    first_hit: datetime | None = None

    for art in articles:
        title = normalise(art.get("title", ""))
        if not title:
            continue
        seen = parse_seendate(art.get("seendate", ""))
        item = Item(
            source_id=art.get("domain", "gdelt") or "gdelt",
            tier=Tier.MAJOR,
            title=title,
            url=art.get("url", ""),
        )
        out = funnel.evaluate(item)

        if out.kill_stage:
            k = out.kill_stage.value
            res.kill_stages[k] = res.kill_stages.get(k, 0) + 1
            if kind == "positive":
                res.misses.append((k, title))
            continue

        best = max((c.score for c in out.candidates), default=0.0)
        res.max_score = max(res.max_score, best)
        if out.survivors:
            res.n_candidates += 1
        else:
            res.kill_stages[Stage.RULES.value] = (
                res.kill_stages.get(Stage.RULES.value, 0) + 1)
            if kind == "positive":
                res.misses.append((Stage.RULES.value, title))

        if best >= min_score:
            res.n_above_fire += 1
            res.detected = True
            res.top_hits.append((best, title))
            if seen and (first_hit is None or seen < first_hit):
                first_hit = seen

    if first_hit and death_dt:
        res.lag_minutes = (first_hit - death_dt).total_seconds() / 60.0
    res.sample_capped = len(articles) >= GDELT_MAX_RECORDS
    res.top_hits.sort(reverse=True)
    res.top_hits = res.top_hits[:3]
    return res


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def cache_path(event_id: str) -> Path:
    return CACHE_DIR / f"{event_id}.json"


def load_or_fetch(spec: dict, kind: str, death: str | None,
                  do_fetch: bool) -> list[dict] | None:
    path = cache_path(spec["id"])
    if path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached:
            return cached
        # An empty cache file predates the no-cache-on-failure fix, or the
        # window really is empty. Either way it tests nothing — drop it and
        # refetch rather than silently "passing" on zero articles.
        print(f"  {spec['id']}: cached file is empty, discarding")
        path.unlink(missing_ok=True)
    if not do_fetch:
        return None

    if kind == "positive":
        if not death:
            print(f"  {spec['id']}: no death date, skipping")
            return None
        start = datetime.fromisoformat(death).replace(tzinfo=UTC)
    else:
        start = datetime.fromisoformat(spec["start"].replace("Z", "+00:00"))

    print(f"  fetching {spec['id']} from {start:%Y-%m-%d %H:%M}Z "
          f"(+{spec['window_hours']}h)…")
    try:
        arts = fetch_window(spec["query"], start, spec["window_hours"])
    except RateLimited as exc:
        # Do NOT cache. A failed fetch written as [] would look like a window
        # with no coverage forever, and the backtest would silently pass on
        # nothing. Leave it missing so the next run retries.
        print(f"    FAILED, not cached: {exc}")
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(arts), encoding="utf-8")
    print(f"    cached {len(arts)} articles")
    return arts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true",
                    help="download windows that are not cached (slow: GDELT "
                         "allows one request per 5s)")
    ap.add_argument("--refresh", action="store_true", help="re-download everything")
    ap.add_argument("--verify", action="store_true",
                    help="re-check every death date against Wikidata P570")
    ap.add_argument("--misses", action="store_true",
                    help="print headlines that failed to fire (tuning aid)")
    args = ap.parse_args()

    corpus = yaml.safe_load(EVENTS.read_text(encoding="utf-8"))
    positives = corpus.get("positives", [])
    negatives = corpus.get("negatives", [])

    if args.refresh:
        for spec in positives + negatives:
            cache_path(spec["id"]).unlink(missing_ok=True)
        args.fetch = True

    # Ground truth from Wikidata, never from memory.
    qids = [s["wikidata_id"] for s in positives + negatives if s.get("wikidata_id")]
    deaths: dict[str, tuple[str, str | None]] = {}
    if args.verify or args.fetch:
        print("resolving ground truth from Wikidata P570…")
        try:
            deaths = wikidata_deaths(qids)
        except Exception as exc:  # noqa: BLE001
            print(f"  wikidata lookup failed: {exc}")

    if args.verify:
        print()
        bad = 0
        for spec in positives:
            label, died = deaths.get(spec["wikidata_id"], ("?", None))
            ok = died is not None
            print(f"  {'ok  ' if ok else 'FAIL'} {spec['wikidata_id']:<9} "
                  f"{label:<22} died={died or 'NONE'}")
            bad += not ok
        for spec in negatives:
            label, died = deaths.get(spec["wikidata_id"], ("?", None))
            ok = died is None or spec["id"] == "carter-before-death"
            print(f"  {'ok  ' if ok else 'FAIL'} {spec['wikidata_id']:<9} "
                  f"{label:<22} died={died or 'ALIVE'}  (negative control)")
        if bad:
            print(f"\n{bad} positive(s) have no P570 — corpus is wrong")
            return 1
        print("\nground truth verified")
        return 0

    results: list[EventResult] = []

    print("=== POSITIVES: real coverage of people who really died ===")
    for spec in positives:
        death = deaths.get(spec.get("wikidata_id", ""), (None, None))[1]
        arts = load_or_fetch(spec, "positive", death, args.fetch)
        if arts is None:
            print(f"  {spec['id']:<20} no cache (run --fetch)")
            continue
        death_dt = (
            datetime.fromisoformat(death).replace(tzinfo=UTC)
            if death else None
        )
        results.append(replay_event(spec, arts, "positive", death_dt))

    print("\n=== NEGATIVES: coverage of people who did NOT die ===")
    for spec in negatives:
        arts = load_or_fetch(spec, "negative", None, args.fetch)
        if arts is None:
            print(f"  {spec['id']:<20} no cache (run --fetch)")
            continue
        results.append(replay_event(spec, arts, "negative", None))

    if not results:
        print("\nNothing to replay. Run: tools/replay.py --fetch")
        return 1

    # ---- report ----
    print("\n" + "=" * 78)
    print(f"{'event':<24} {'kind':<9} {'arts':>5} {'fired':>6} {'max':>6} "
          f"{'≥fire':>6} {'lag':>9}")
    print("-" * 78)
    failures = 0
    for r in results:
        want = r.kind == "positive"
        ok = r.detected == want
        failures += not ok
        if r.lag_minutes is None:
            lag = "—"
        elif r.sample_capped:
            lag = "capped"      # partial sample: lag is not measurable
        else:
            lag = f"{r.lag_minutes:+.0f}m"
        print(f"{('ok  ' if ok else 'FAIL') + ' ' + r.id:<24} {r.kind:<9} "
              f"{r.n_articles:>5} {str(r.detected):>6} {r.max_score:>6.3f} "
              f"{r.n_above_fire:>6} {lag:>9}")

    print("\n--- detail ---")
    for r in results:
        print(f"\n{r.id} ({r.kind}, {r.n_articles} articles)")
        print(f"  killed: {r.kill_stages or '{}'}  candidates={r.n_candidates}")
        for s, t in r.top_hits:
            print(f"   {s:.3f}  {t[:70]}")
        if args.misses and r.misses:
            print(f"  misses ({len(r.misses)}):")
            for stage, t in r.misses[:6]:
                print(f"   [{stage:<9}] {t[:66]}")

    print("\n" + "=" * 78)
    if failures:
        print(f"{failures} event(s) behaved wrongly.")
        print("A positive that did not fire is a RECALL bug; a negative that "
              "fired is a SAFETY bug and matters more.")
        return 1
    print(f"PASS — all {len(results)} events behaved correctly "
          f"({sum(r.kind == 'positive' for r in results)} deaths detected, "
          f"{sum(r.kind == 'negative' for r in results)} non-deaths ignored)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
