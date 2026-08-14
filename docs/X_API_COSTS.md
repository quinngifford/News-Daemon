# X API costs — read before enabling the stream

Verified against X's own pricing docs on 2026-08-13.

## The pricing landscape

X removed the flat tiers. As of **February 2026**, new developers cannot buy
Basic ($200/mo) or Pro ($5,000/mo) at all — those survive only for existing
subscribers. New accounts get **pay-per-use**:

| Item | Rate |
|---|---|
| Post **read** | **$0.005** per post |
| Post created | $0.015 ($0.20 if it contains a link) |
| User read | $0.010 |
| Monthly cap | **2,000,000 post reads** |
| Minimum spend | **None** — no subscription, start/stop anytime |
| Free allowance | **None**. Credits are prepaid in the Developer Console |
| Deduplication | Same resource within a 24h UTC window is charged once |

Note on a common misconception: several pricing blogs state filtered stream is
Pro-only. X's **official filtered-stream docs** list a pay-per-use column —
1,000 rules per project, 1,024 chars per rule, **single connection**, core
operators. So the $5,000 tier is not required. Confirm in the Developer Console
before committing, since the blogs may be describing the pre-February structure.

## Why rule design *is* the bill

With filtered stream you are charged for **every post the stream pushes at you**.
Nothing about your local processing affects the cost — only what you asked to
receive.

| Rule design | Matched posts/day | Monthly cost |
|---|---|---|
| Account-scoped + death terms *(what we ship)* | 5–30 | **$0.75 – $4.50** |
| `Trump (died OR dead OR dies)` — no account scope | ~10,000 | **~$1,500** |
| Same, during the event or a viral hoax | millions | **hits the cap** |
| The 2M read cap itself | — | **$10,000** |

**The cap is a ceiling, not a guard.** Assume no help from it.

## How this repo keeps the bill small

Four independent layers, because a runaway stream spends money while you sleep:

1. **Account-scoped rules** (`config/x_accounts.yaml`). Volume is bounded by
   physics: 25 accounts can only tweet so much, regardless of world events.
   Target names are deliberately **absent** from the rules — the free local
   funnel does target matching. So spend is minimal *and* adding a new target
   costs $0 and needs no rule change.
2. **Rate breaker** (`RateBreaker`). Disconnects for 30 min if delivered volume
   exceeds `max_posts_per_hour`. Normal volume is single digits per *day*, so a
   spike means a hoax went viral or the rules are wrong — either way, continuing
   to receive it means paying $0.005 a post to be misled.
3. **Persistent monthly budget** (`MonthlyBudget` → `var/x_budget.json`).
   Atomic writes, survives restarts. An in-memory counter would reset on every
   crash, letting a restart loop re-spend the cap repeatedly.
4. **Stale-rule reaping** (`sync_rules: true`). Rules left on the server from an
   earlier config keep matching and keep billing for posts you no longer screen.
   The adapter deletes anything not in the current config.

On top of all four: **buy only a small prepaid credit balance.** Credits are
deducted as you go, so the balance is a hard ceiling that no bug in this repo
can exceed. Start with $10.

## Enabling it

```bash
# 1. Verify rule generation and the guards offline — costs nothing.
.venv/bin/python -m tests.test_x_rules

# 2. Preview the exact rules that would be installed, without connecting.
#    Set dry_run: true in config/sources.yaml, then:
export TICKER_X_BEARER_TOKEN=...
.venv/bin/python -m ticker --dry-run

# 3. Set enabled: true and dry_run: false in config/sources.yaml.
```

The shipped rule set is a single 457-character rule covering all 25 accounts —
comfortably inside the 1,024-char limit, with room for roughly 30 more accounts
before the builder needs to chunk.

## Two constraints worth remembering

- **One connection.** Pay-per-use permits a single stream connection, so only
  one box may enable this adapter. The second box in a redundancy pair must
  leave it disabled, or the two will fight over the connection and both get 429s.
- **No free allowance.** You cannot smoke-test against the live API for free,
  which is why `tests/test_x_rules.py` verifies everything checkable offline.

## Is it worth it?

Yes, more than it looked at first. We confirmed AP and Reuters both retired their
public RSS feeds, so there is no free tier-0 wire feed to subscribe to. X is now
the most plausible route to wire-speed signal, and the adapter promotes posts
from `tier0` accounts to `Tier.WIRE`, making them fast-path eligible.

At $1–5/month with the shipped rules, the cost is negligible relative to the
latency it buys.
