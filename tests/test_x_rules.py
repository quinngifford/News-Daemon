"""Offline verification of X rule generation and the cost guards.

There is no free tier on X pay-per-use, so you cannot smoke-test the adapter
against the live API without spending money. Everything that can be checked
without a credential is checked here — especially the invariant that no rule is
ever unscoped, because an unscoped rule is what turns a $3/month bill into a
$10,000 one.

Run: .venv/bin/python -m tests.test_x_rules
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from ticker.config import CONFIG_DIR
from ticker.ingest.twitter_stream import (
    MAX_RULE_LEN,
    USD_PER_POST_READ,
    MonthlyBudget,
    RateBreaker,
    build_rules,
    load_accounts,
)

failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def main() -> int:
    accounts, tier0, death = load_accounts(CONFIG_DIR / "x_accounts.yaml")
    print(f"loaded {len(accounts)} accounts ({len(tier0)} tier-0), "
          f"{len(death)} death terms\n")

    rules = build_rules(accounts, death)
    longest = max(len(r["value"]) for r in rules)

    print("--- rule generation ---")
    check(len(rules) >= 1, "produces at least one rule", f"{len(rules)} rules")
    check(longest <= MAX_RULE_LEN, "every rule within length limit",
          f"longest {longest}/{MAX_RULE_LEN}")
    check(all("from:" in r["value"] for r in rules),
          "NO UNSCOPED RULES (the cost invariant)")
    check(all("died" in r["value"] for r in rules),
          "death clause present in every rule")
    check(len({r["tag"] for r in rules}) == len(rules), "rule tags are unique")

    # Every account must appear exactly once across all rules: a dropped account
    # is a blind spot, a duplicated one is double billing.
    covered: list[str] = []
    for r in rules:
        covered += [
            p.removeprefix("from:")
            for p in r["value"].split(") (")[0].strip("(").split(" OR ")
        ]
    check(sorted(covered) == sorted(accounts), "every account covered exactly once",
          f"{len(covered)} covered vs {len(accounts)} configured")

    # Target names must not leak into X rules — that is what keeps spend minimal
    # and lets you add targets for free.
    joined = " ".join(r["value"] for r in rules).lower()
    check("trump" not in joined, "no target names in X rules (spend stays minimal)")

    print("\n--- guard rails refuse unsafe input ---")
    for label, args in [
        ("rejects empty account list", ([], death)),
        ("rejects empty death terms", (accounts, [])),
    ]:
        try:
            build_rules(*args)
            check(False, label, "no exception raised")
        except ValueError:
            check(True, label)

    try:
        build_rules(accounts, ["x" * 2000])
        check(False, "rejects oversized death clause", "no exception raised")
    except ValueError:
        check(True, "rejects oversized death clause")

    print("\n--- monthly budget persists across restarts ---")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "x_budget.json"
        b1 = MonthlyBudget(p, cap_usd=1.00)
        b1.record(100)
        spent = b1.spent_usd
        check(abs(spent - 100 * USD_PER_POST_READ) < 1e-9,
              "spend math correct", f"100 posts = ${spent:.3f}")

        # A crash-loop must not reset the counter and re-spend the cap.
        b2 = MonthlyBudget(p, cap_usd=1.00)
        check(b2.posts == 100, "counter survives a restart", f"reloaded {b2.posts}")
        check(not b2.exhausted, "not exhausted below cap")

        b2.record(120)   # 220 posts = $1.10 > $1.00 cap
        check(b2.exhausted, "exhausted above cap", f"${b2.spent_usd:.2f} / $1.00")

    print("\n--- rate breaker ---")
    br = RateBreaker(max_posts=10, window_s=3600)
    tripped_at = next((i for i in range(1, 30) if br.record(1)), None)
    check(tripped_at == 11, "trips just past the limit", f"tripped at post {tripped_at}")

    print("\n--- projected cost at realistic volumes ---")
    for per_day in (5, 30, 300, 10_000):
        monthly = per_day * 30 * USD_PER_POST_READ
        print(f"     {per_day:>6} matched posts/day → ${monthly:>9,.2f}/month")
    print(f"     {'2M cap':>6} reads/month        → "
          f"${2_000_000 * USD_PER_POST_READ:>9,.2f}/month  ← the ceiling, not a guard")

    print()
    for r in rules:
        print(f"rule {r['tag']} ({len(r['value'])} chars):\n  {r['value']}\n")

    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
