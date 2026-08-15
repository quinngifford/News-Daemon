# news-ticker-daemon

A 24/7 daemon that watches news and structural data streams for a small set of
pre-registered high-impact events — primarily the death of a named public figure
— and pushes a notification to your phone within seconds of the first credible
report.

Built for a 2 vCPU / 911 MB box: one async process, SQLite (WAL), no Redis, no
Docker, no broker.

**Start with [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).** It explains the
latency budget honestly (the race is against other bots, not the news cycle), the
tiered screening funnel that keeps AI cost at effectively zero, and — importantly
— the false-positive classes found in real live data.

---

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
```

Verify the pipeline without touching the network or spending anything:

```bash
.venv/bin/python -m ticker --canary
```

Watch live feeds flow through the funnel without any risk of notifying you:

```bash
.venv/bin/python -m ticker --dry-run --duration 120
```

Then open the dashboard at <http://127.0.0.1:8080>.

## Commands

| Command | What it does |
|---|---|
| `python -m ticker` | Run the daemon (ingest → screen → confirm → notify + dashboard) |
| `python -m ticker --dry-run` | Screen live sources, never notify. Safe at any time. |
| `python -m ticker --duration N` | Exit after N seconds; prints funnel stats and source health |
| `python -m ticker --canary` | Run the synthetic drill once, exit 0/1. Cron-friendly. |
| `python -m ticker --canary-full` | Same, but also dispatches through **live** channels |
| `python tools/validate_sources.py` | Re-probe every configured feed URL |
| `python tools/gen_vapid_keys.py` | Generate the Web Push VAPID keypair (run once) |

## Tests

None of these touch the network or cost money.

```bash
for t in test_funnel test_x_rules test_end_to_end test_live_negatives test_api; do
  .venv/bin/python -m tests.$t
done
```

| Suite | Guards |
|---|---|
| `test_funnel` | 23 adversarial headlines + retraction handling |
| `test_live_negatives` | **39 real headlines captured from live feeds.** Enforces that no negative reaches the fire threshold while all true positives still fire. |
| `test_replay` | **Generalisation across 7 other people** + the replay machinery |
| `test_end_to_end` | Fast path, corroboration, source independence, attribution collapsing, retraction |
| `test_x_rules` | X rule generation + all three cost guards, fully offline |
| `test_api` | Dashboard, subscriptions, and SSE delivery over a real loopback server |

Two of these matter more than the rest, and both exist because they caught a
real bug that the others could not:

- **`test_live_negatives`** — a live run found that a story about Senator Lindsey
  Graham dying, described in wire copy as a "Trump ally", scored 0.922 and would
  have fired a **false CONFIRMED alert**.
- **`test_replay`** — every rule was tuned on Trump headlines, so it replays each
  false-positive class against Elizabeth II, Jimmy Carter, Benedict XVI, Pelé,
  Shinzō Abe, Kissinger and Berlusconi. It immediately found that
  `"Breaking: Pele is dead"` scored **0.000** — the alias-suppression rule
  treated `"Breaking:"` as a given name, which silently broke every mononym
  target. Trump was unaffected only by accident.

**If you touch `ticker/screen/rules.py`, run both.** They pull in opposite
directions — one guards precision, the other recall — which is exactly why
neither alone is sufficient.

### Backtesting against real archived coverage

```bash
.venv/bin/python tools/replay.py --verify   # check death dates against Wikidata
.venv/bin/python tools/replay.py --fetch    # download windows (slow: strict rate limit)
.venv/bin/python tools/replay.py --misses   # replay from cache, list failures
```

Ground truth is read from Wikidata P570, never from memory — when the corpus was
first drafted from recall, three of eleven Q-ids were wrong. Windows are cached
under `var/replay_cache/`, so only the first run needs network.

## Configuration

Adding a target never requires a code change, and costs no extra scan time — the
Aho-Corasick automaton scans in O(text) regardless of pattern count.

| File | Purpose |
|---|---|
| `config/targets/*.yaml` | Who to watch. Copy `trump.yaml`; it is annotated. |
| `config/sources.yaml` | Feeds and tiers. Every URL is marked verified or not. |
| `config/lexicons/*.txt` | Death vocabulary, idioms, condolence phrases, satire domains |
| `config/x_accounts.yaml` | X allow-list — **this file is your X bill** |
| `config/settings.toml` | Budgets, thresholds, ports, canary schedule |

Secrets go in `deploy/ticker.env` (gitignored):

```bash
cp deploy/ticker.env.example deploy/ticker.env && chmod 600 deploy/ticker.env
```

## Notification channels

Fan-out is parallel and idempotent on `event_id`, so a slow channel never delays
a fast one and a second box cannot double-alert you.

- **Dashboard SSE** — always on, lowest latency of all (no vendor push in the
  path), and the only channel that can guarantee an audible alarm. Requires a tab
  open with sound enabled.
- **Telegram** — fastest to set up, no store review, works on phone and desktop.
  Set `TICKER_TELEGRAM_TOKEN` / `TICKER_TELEGRAM_CHAT_ID`.
- **Web Push (PWA)** — real push with the app closed. Run
  `tools/gen_vapid_keys.py`, then serve over HTTPS.
  **On iPhone this only works if the page is added to the Home Screen** (iOS
  16.4+); a Safari tab will never receive a push.

Whichever you use, allow-list it in your phone's Focus / Do-Not-Disturb rules.
An alert that is silently suppressed at 04:00 is the failure this whole project
exists to avoid.

## Running as a service

```bash
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ticker.service
sudo systemctl enable --now ticker-canary.timer ticker-validate-sources.timer
```

- `ticker.service` — `Restart=always` plus `WatchdogSec=90`. The daemon sends
  `sd_notify` heartbeats from its event loop, so a *hung* loop is restarted too,
  not only a crashed process. Memory is capped at 420 MB so a leak degrades this
  service rather than OOM-killing `sshd` and locking you out.
- `ticker-canary.timer` — weekly drill in a **separate process**. The daemon's
  own internal canary cannot tell you the daemon is dead; this can.
- `ticker-validate-sources.timer` — weekly feed re-probe. A dead feed looks
  exactly like a quiet news day, so it must be checked actively.

For phone access, put `deploy/caddy/Caddyfile` in front — Web Push requires a
secure context, so the PWA cannot work over plain HTTP.

## Current state

Working and verified: ingest (12 live sources), screening funnel, corroboration
and retraction, canary, dashboard, SSE, subscription storage, X adapter
(offline-verified, disabled).

Telegram delivery is confirmed working end-to-end.

Not yet done:

- **The replay corpus has not been downloaded.** `tools/replay.py` is written and
  its machinery is tested offline, but GDELT's rate limit blocked the initial
  fetch. Run `tools/replay.py --fetch` when the quota resets; it caches, so this
  is a one-time cost.
- **Web Push has never delivered a real message.** Needs the dashboard served
  over HTTPS (see `deploy/caddy/`) and, on iPhone, the page added to the Home
  Screen. Telegram is verified; this channel is not.
- `reddit` / `hn` / `market_ws` adapters are configured but unimplemented.
  Reddit now needs OAuth (it 403s unauthenticated).
- Second-region redundancy is untested. Note pay-per-use X allows only **one**
  stream connection, so only one box may enable that adapter.
- `ANTHROPIC_API_KEY` is unset, so Stage-3 adjudication is off and the Stage-2
  score decides alone. Borderline items are dropped rather than judged.

## Costs

| Item | Monthly |
|---|---|
| This box | ~$8–12 |
| LLM adjudication (~1 candidate per run reaches it) | cents |
| Telegram, Web Push, all free sources | $0 |
| *Optional* X filtered stream, account-scoped | $1–5 |

Before enabling X, read [docs/X_API_COSTS.md](docs/X_API_COSTS.md). Rule design
is the bill: account-scoped costs a few dollars, an unscoped keyword rule on a
common name costs ~$1,500/month and hits a $10,000 cap.

## A note on what this is for

Trading on public news is ordinary market activity, and everything here reads
public sources. Keep it that way. Respect each source's terms and keep polling
intervals sane — being blocked exactly when you need a feed is both a compliance
and a reliability problem. Prediction venues have their own rules on resolution
sources and eligibility; check the specific market's terms before staking
anything on this.

False positives cost real money here, which is why corroboration, attribution
collapsing, and the live-negatives corpus get more attention in this codebase
than raw speed does.
