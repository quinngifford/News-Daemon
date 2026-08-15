"""SQLite persistence. Synchronous by design, called from a thread executor.

Nothing on the fire path blocks on this module — alerts are dispatched first and
recorded after, so a disk fsync never sits inside the latency budget.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from ticker.models import Alert, Candidate, Evidence

log = logging.getLogger(__name__)
SCHEMA = Path(__file__).with_name("schema.sql")


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- writes ---------------------------------------------------------

    def record_item(self, cand: Candidate) -> None:
        it = cand.item
        lag = None
        if it.t_source:
            lag = (time.time() - it.t_source) * 1000
        self.conn.execute(
            "INSERT INTO items (source_id, tier, target_id, title, url, score,"
            " features_json, reason, killed_at, t_source, t_ingest_wall, ingest_lag_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (it.source_id, int(it.tier), cand.target_id, it.title, it.url,
             cand.score, json.dumps(cand.features), cand.reason,
             cand.killed_at.value if cand.killed_at else None,
             it.t_source, time.time(), lag),
        )
        self.conn.commit()

    def record_evidence(self, ev: Evidence) -> None:
        self.conn.execute(
            "INSERT INTO evidence (target_id, source_id, origin, tier, weight,"
            " negative, headline, url, t_wall) VALUES (?,?,?,?,?,?,?,?,?)",
            (ev.target_id, ev.source_id, ev.origin, int(ev.tier), ev.weight,
             int(ev.negative), ev.headline, ev.url, ev.t_wall),
        )
        self.conn.commit()

    def record_alert(self, alert: Alert, weight: float, trail: list[str]) -> None:
        # UNIQUE(event_id, state) makes a re-dispatch a no-op rather than a
        # duplicate row — same idempotency guarantee as the dispatcher.
        self.conn.execute(
            "INSERT OR IGNORE INTO alerts (event_id, target_id, state, headline,"
            " url, score, weight, trail_json, detect_latency_ms, t_wall)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (alert.event_id, alert.target_id, alert.state.value, alert.headline,
             alert.url, alert.score, weight, json.dumps(trail),
             alert.detect_latency_ms, alert.t_wall),
        )
        self.conn.commit()

    def record_adjudication(self, cand: Candidate, verdict) -> None:
        self.conn.execute(
            "INSERT INTO adjudications (target_id, item_title, died, confidence,"
            " subject, reason, latency_ms, cost_usd, fell_back, t_wall)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cand.target_id, cand.item.title, int(verdict.died), verdict.confidence,
             verdict.subject, verdict.reason, verdict.latency_ms, verdict.cost_usd,
             int(verdict.fell_back), time.time()),
        )
        self.conn.commit()

    def record_health(self, snapshots: list[dict]) -> None:
        now = time.time()
        self.conn.executemany(
            "INSERT INTO source_health (source_id, items, staleness_s, stale,"
            " last_error, t_wall) VALUES (?,?,?,?,?,?)",
            [(s["id"], s["items"], s["staleness_s"], int(s["stale"]),
              s.get("last_error"), now) for s in snapshots],
        )
        self.conn.commit()

    def record_canary(self, passed: bool, latency_ms: float, detail: str) -> None:
        self.conn.execute(
            "INSERT INTO canary_runs (passed, latency_ms, detail, t_wall)"
            " VALUES (?,?,?,?)",
            (int(passed), latency_ms, detail, time.time()),
        )
        self.conn.commit()

    # --- outbound delivery queue ----------------------------------------

    def enqueue_outbox(self, event_id: str, state: str, payload: str,
                       next_attempt_at: float) -> None:
        """Queue an undelivered event. Idempotent on (event_id, state)."""
        self.conn.execute(
            "INSERT OR IGNORE INTO outbox (event_id, state, payload_json,"
            " next_attempt_at, created_at) VALUES (?,?,?,?,?)",
            (event_id, state, payload, next_attempt_at, time.time()),
        )
        self.conn.commit()

    def pending_outbox(self, now: float, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM outbox WHERE delivered_at IS NULL AND next_attempt_at <= ?"
            " ORDER BY created_at LIMIT ?",
            (now, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_outbox_delivered(self, row_id: int) -> None:
        self.conn.execute(
            "UPDATE outbox SET delivered_at = ? WHERE id = ?", (time.time(), row_id)
        )
        self.conn.commit()

    def mark_outbox_failed(self, row_id: int, error: str,
                           next_attempt_at: float) -> None:
        self.conn.execute(
            "UPDATE outbox SET attempts = attempts + 1, last_error = ?,"
            " next_attempt_at = ? WHERE id = ?",
            (error[:300], next_attempt_at, row_id),
        )
        self.conn.commit()

    def outbox_stats(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(delivered_at IS NULL) AS pending,"
            " MAX(attempts) AS max_attempts FROM outbox"
        ).fetchone()
        return {
            "total": row["total"] or 0,
            "pending": row["pending"] or 0,
            "max_attempts": row["max_attempts"] or 0,
        }

    # --- Web Push subscriptions -----------------------------------------

    def add_subscription(self, endpoint: str, keys: dict, user_agent: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO subscriptions (endpoint, keys_json, user_agent,"
            " created_at) VALUES (?,?,?,?)",
            (endpoint, json.dumps(keys), user_agent, time.time()),
        )
        self.conn.commit()

    def drop_subscription(self, endpoint: str) -> None:
        self.conn.execute("DELETE FROM subscriptions WHERE endpoint = ?", (endpoint,))
        self.conn.commit()

    def get_subscriptions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT endpoint, keys_json FROM subscriptions"
        ).fetchall()
        return [
            {"endpoint": r["endpoint"], "keys": json.loads(r["keys_json"])}
            for r in rows
        ]

    # --- reads / maintenance --------------------------------------------

    def recent_alerts(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM alerts ORDER BY t_wall DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def last_canary(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM canary_runs ORDER BY t_wall DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def prune(self, retain_days: int = 90) -> None:
        cutoff = time.time() - retain_days * 86400
        for table in ("items", "evidence", "source_health", "adjudications"):
            self.conn.execute(f"DELETE FROM {table} WHERE t_wall < ?", (cutoff,))
        self.conn.commit()  # alerts are kept forever; they are the point
