"""X (Twitter) filtered-stream adapter, with the cost controls built in.

Pricing reality as of 2026-08 (docs.x.com/x-api/getting-started/pricing):
  * $0.005 per post read — and with filtered stream you pay for every post the
    stream pushes at you, so RULE NARROWNESS IS THE BILL.
  * Pay-per-use is capped at 2,000,000 post reads/month. That cap is a $10,000
    ceiling, not a safety feature.
  * No subscription and no minimum, so an idle adapter costs exactly $0.
  * Pay-per-use filtered stream allows 1,000 rules, 1,024 chars per rule, and a
    SINGLE connection — which is why only one box may own this stream.

Three independent guards, because a runaway stream spends real money while you
are asleep:
  1. Account-scoped rules — volume bounded by physics (see config/x_accounts.yaml).
  2. Rolling rate breaker — disconnects if matched volume spikes (hoax, or the
     event itself going viral).
  3. Persistent monthly budget — survives restarts, so a crash-loop cannot
     silently reset the counter and re-spend the cap.

Belt and braces on top of all three: buy only a small prepaid credit balance in
the Developer Console. Credits are deducted as you go, so the balance itself is
a hard ceiling no bug of mine can exceed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from pathlib import Path

import httpx
import yaml

from ticker.ingest.base import Emit, SourceAdapter
from ticker.models import Item, Tier

log = logging.getLogger(__name__)

API = "https://api.x.com/2"
RULES_URL = f"{API}/tweets/search/stream/rules"
STREAM_URL = f"{API}/tweets/search/stream"

USD_PER_POST_READ = 0.005      # docs.x.com/x-api/getting-started/pricing
MAX_RULE_LEN = 1024            # pay-per-use limit


# --------------------------------------------------------------------------
# Rule construction
# --------------------------------------------------------------------------


def build_rules(
    accounts: list[str],
    death_terms: list[str],
    max_rule_len: int = MAX_RULE_LEN,
    tag_prefix: str = "deaths",
) -> list[dict[str, str]]:
    """Build account-scoped rules that each fit inside the length limit.

    Produces `(from:a OR from:b ...) (died OR dies OR "passed away" ...)`,
    chunking the account list as needed. Target names are deliberately absent —
    the local funnel does target matching for free.

    Raises ValueError if the death clause alone cannot fit, since silently
    truncating it would change what you are paying to receive.
    """
    if not accounts:
        raise ValueError("refusing to build an unscoped rule: account list is empty")
    if not death_terms:
        raise ValueError("death_terms is empty; rule would match all account traffic")

    def quote(t: str) -> str:
        return f'"{t}"' if " " in t else t

    death_clause = "(" + " OR ".join(quote(t) for t in death_terms) + ")"
    # " " joiner + the account clause's own parentheses
    overhead = len(death_clause) + 3
    if overhead >= max_rule_len:
        raise ValueError(
            f"death clause is {len(death_clause)} chars, leaving no room for "
            f"accounts within the {max_rule_len}-char rule limit"
        )

    rules: list[dict[str, str]] = []
    chunk: list[str] = []

    def clause_len(items: list[str]) -> int:
        # "(from:a OR from:b)" — 5 chars per "from:", 4 per " OR ", 2 parens
        return sum(len(a) + 5 for a in items) + 4 * (len(items) - 1) + 2

    for acct in accounts:
        candidate = chunk + [acct]
        if chunk and clause_len(candidate) + overhead > max_rule_len:
            rules.append(_finish(chunk, death_clause, tag_prefix, len(rules)))
            chunk = [acct]
        else:
            chunk = candidate
        if clause_len([acct]) + overhead > max_rule_len:
            raise ValueError(f"account {acct!r} cannot fit in a single rule")

    if chunk:
        rules.append(_finish(chunk, death_clause, tag_prefix, len(rules)))
    return rules


def _finish(chunk: list[str], death_clause: str, tag_prefix: str, idx: int) -> dict[str, str]:
    account_clause = "(" + " OR ".join(f"from:{a}" for a in chunk) + ")"
    return {"value": f"{account_clause} {death_clause}", "tag": f"{tag_prefix}-{idx}"}


def load_accounts(path: Path) -> tuple[list[str], set[str], list[str]]:
    """→ (all accounts, tier0 account set lowercased, death terms)."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    t0 = [str(a) for a in (raw.get("tier0") or [])]
    t1 = [str(a) for a in (raw.get("tier1") or [])]
    death = [str(t) for t in (raw.get("death_clause") or [])]
    return t0 + t1, {a.lower() for a in t0}, death


# --------------------------------------------------------------------------
# Cost guards
# --------------------------------------------------------------------------


class MonthlyBudget:
    """Persistent spend counter. Survives restarts on purpose.

    An in-memory counter would reset on every crash, so a restart loop could
    re-spend the monthly cap repeatedly. That is precisely the failure mode that
    costs money unattended.
    """

    def __init__(self, path: Path, cap_usd: float, usd_per_post: float = USD_PER_POST_READ):
        self.path = Path(path)
        self.cap_usd = cap_usd
        self.usd_per_post = usd_per_post
        self.posts = 0
        self.month = time.strftime("%Y-%m", time.gmtime())
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if data.get("month") == self.month:
            self.posts = int(data.get("posts", 0))
        # A stale month means a fresh budget; nothing to carry over.

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"month": self.month, "posts": self.posts,
                        "usd": round(self.spent_usd, 4)}),
            encoding="utf-8",
        )
        tmp.replace(self.path)   # atomic: a torn write must not lose the counter

    def _roll(self) -> None:
        month = time.strftime("%Y-%m", time.gmtime())
        if month != self.month:
            self.month, self.posts = month, 0

    def record(self, n: int = 1) -> None:
        self._roll()
        self.posts += n
        self._save()

    @property
    def spent_usd(self) -> float:
        return self.posts * self.usd_per_post

    @property
    def exhausted(self) -> bool:
        self._roll()
        return self.spent_usd >= self.cap_usd

    def status(self) -> dict:
        return {
            "month": self.month,
            "posts": self.posts,
            "spent_usd": round(self.spent_usd, 4),
            "cap_usd": self.cap_usd,
        }


class RateBreaker:
    """Rolling-window circuit breaker on delivered post volume.

    Normal matched volume for account-scoped death rules is single digits per
    day. A spike means a hoax has gone viral or the rules are wrong — either
    way, keep receiving it and you are paying $0.005 a post to be misled.
    """

    def __init__(self, max_posts: int, window_s: float = 3600.0):
        self.max_posts = max_posts
        self.window_s = window_s
        self._hits: deque[float] = deque()
        self.trips = 0

    def record(self, n: int = 1) -> bool:
        now = time.monotonic()
        for _ in range(n):
            self._hits.append(now)
        cutoff = now - self.window_s
        while self._hits and self._hits[0] < cutoff:
            self._hits.popleft()
        if len(self._hits) > self.max_posts:
            self.trips += 1
            return True
        return False

    @property
    def count(self) -> int:
        return len(self._hits)


# --------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------


class TwitterStreamAdapter(SourceAdapter):
    def __init__(
        self,
        config: dict,
        client: httpx.AsyncClient,
        accounts_path: Path,
        state_dir: Path = Path("var"),
    ) -> None:
        super().__init__(config)
        self.client = client
        self.bearer = os.environ.get(
            config.get("bearer_token_env", "TICKER_X_BEARER_TOKEN"), ""
        )
        self.accounts, self.tier0_accounts, self.death_terms = load_accounts(accounts_path)
        self.budget = MonthlyBudget(
            state_dir / "x_budget.json",
            cap_usd=float(config.get("monthly_budget_usd", 10.0)),
        )
        self.breaker = RateBreaker(
            max_posts=int(config.get("max_posts_per_hour", 300)),
            window_s=3600.0,
        )
        self.breaker_cooldown_s = float(config.get("breaker_cooldown_s", 1800.0))
        self.sync_rules = bool(config.get("sync_rules", True))
        self.dry_run = bool(config.get("dry_run", False))
        self.posts_received = 0

    @property
    def configured(self) -> bool:
        return bool(self.bearer)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.bearer}",
            "User-Agent": "news-ticker-daemon/0.1",
        }

    # --- rule management ------------------------------------------------

    async def _get_rules(self) -> list[dict]:
        r = await self.client.get(RULES_URL, headers=self._headers(), timeout=15.0)
        r.raise_for_status()
        return r.json().get("data") or []

    async def _replace_rules(self, wanted: list[dict[str, str]]) -> None:
        """Make the server's rule set match `wanted`, deleting anything else.

        Stale rules left over from an earlier config are a silent cost leak:
        they keep matching and you keep paying for posts you no longer screen.
        """
        existing = await self._get_rules()
        existing_vals = {e["value"] for e in existing}
        wanted_vals = {w["value"] for w in wanted}

        stale = [e["id"] for e in existing if e["value"] not in wanted_vals]
        if stale:
            log.info("x: deleting %d stale rule(s)", len(stale))
            r = await self.client.post(
                RULES_URL, headers=self._headers(),
                json={"delete": {"ids": stale}}, timeout=15.0,
            )
            r.raise_for_status()

        missing = [w for w in wanted if w["value"] not in existing_vals]
        if missing:
            log.info("x: adding %d rule(s)", len(missing))
            r = await self.client.post(
                RULES_URL, headers=self._headers(),
                json={"add": missing}, timeout=15.0,
            )
            r.raise_for_status()
            errors = r.json().get("errors") or []
            for e in errors:
                log.error("x: rule rejected: %s", e)
            if errors:
                raise RuntimeError(f"{len(errors)} rule(s) rejected by X")

        if not stale and not missing:
            log.info("x: %d rule(s) already in sync", len(wanted))

    # --- main loop ------------------------------------------------------

    async def _run(self, emit: Emit) -> None:
        if not self.configured:
            log.warning("x adapter inactive: set TICKER_X_BEARER_TOKEN")
            await asyncio.sleep(3600)
            return

        rules = build_rules(self.accounts, self.death_terms)
        log.info(
            "x: %d rule(s) covering %d accounts; longest %d/%d chars",
            len(rules), len(self.accounts),
            max(len(r["value"]) for r in rules), MAX_RULE_LEN,
        )

        if self.dry_run:
            for r in rules:
                log.info("x DRY-RUN rule %s: %s", r["tag"], r["value"])
            await asyncio.sleep(3600)
            return

        if self.budget.exhausted:
            log.error("x: monthly budget exhausted (%s) — not connecting",
                      self.budget.status())
            await asyncio.sleep(3600)
            return

        if self.sync_rules:
            await self._replace_rules(rules)

        params = {
            "tweet.fields": "created_at,author_id,text",
            "expansions": "author_id",
            "user.fields": "username,name",
        }
        # No read timeout: the stream is long-lived and sends keep-alive
        # newlines. ops/health.py's staleness watchdog detects a silent stall.
        timeout = httpx.Timeout(connect=15.0, read=None, write=15.0, pool=15.0)

        log.info("x: connecting to filtered stream")
        async with self.client.stream(
            "GET", STREAM_URL, headers=self._headers(), params=params, timeout=timeout
        ) as resp:
            if resp.status_code == 429:
                # Single connection on pay-per-use: a 429 usually means another
                # process (or the other box) already holds the stream.
                log.error("x: 429 — another connection is likely already open. "
                          "Pay-per-use allows ONE stream connection.")
                await asyncio.sleep(300)
                return
            resp.raise_for_status()

            async for line in resp.aiter_lines():
                if not line.strip():
                    continue          # keep-alive newline: free, ignore

                if self.budget.exhausted:
                    log.error("x: budget cap hit mid-stream (%s) — disconnecting",
                              self.budget.status())
                    return

                item = self._to_item(line)
                if item is None:
                    continue

                # Charged on delivery, whether or not we can parse it.
                self.budget.record(1)
                self.posts_received += 1

                if self.breaker.record(1):
                    log.error(
                        "x: RATE BREAKER TRIPPED — %d posts in the last hour "
                        "(limit %d). Disconnecting for %.0f min. Spent this "
                        "month: $%.2f",
                        self.breaker.count, self.breaker.max_posts,
                        self.breaker_cooldown_s / 60, self.budget.spent_usd,
                    )
                    await asyncio.sleep(self.breaker_cooldown_s)
                    return

                await emit(item)

    def _to_item(self, line: str) -> Item | None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        data = payload.get("data")
        if not data:
            return None

        users = {
            u["id"]: u for u in (payload.get("includes", {}).get("users") or [])
        }
        author = users.get(data.get("author_id", ""), {})
        username = author.get("username", "")

        # A wire account's post is wire-grade evidence; everything else on the
        # allow-list is a major outlet.
        tier = Tier.WIRE if username.lower() in self.tier0_accounts else Tier.MAJOR

        return Item(
            source_id=f"x:{username}" if username else self.id,
            tier=tier,
            title=data.get("text", ""),
            url=f"https://x.com/{username}/status/{data['id']}" if username else "",
            t_source=None,
            raw={
                "author": username,
                "author_name": author.get("name", ""),
                "matched_rules": [r.get("tag") for r in payload.get("matching_rules", [])],
            },
        )

    def health(self) -> dict:
        h = super().health()
        h.update(
            {
                "posts_received": self.posts_received,
                "budget": self.budget.status(),
                "hour_volume": self.breaker.count,
                "breaker_trips": self.breaker.trips,
            }
        )
        return h
