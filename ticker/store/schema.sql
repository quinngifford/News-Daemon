-- SQLite schema. WAL mode, single writer, no server process — the right fit for
-- a 911 MB box, and the audit trail is what makes post-mortems and threshold
-- tuning possible.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;   -- WAL + NORMAL is durable enough for an audit log
PRAGMA busy_timeout = 5000;

-- Only items that reached Stage 2. Logging all ~50k/day rejections would dwarf
-- the signal; the funnel counters cover aggregate volume.
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY,
    source_id     TEXT    NOT NULL,
    tier          INTEGER NOT NULL,
    target_id     TEXT,
    title         TEXT    NOT NULL,
    url           TEXT,
    score         REAL,
    features_json TEXT,
    reason        TEXT,
    killed_at     TEXT,              -- NULL = survived screening
    t_source      REAL,
    t_ingest_wall REAL    NOT NULL,
    ingest_lag_ms REAL               -- t_ingest - t_source, when publisher gave one
);
CREATE INDEX IF NOT EXISTS idx_items_target_time ON items(target_id, t_ingest_wall DESC);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source_id, t_ingest_wall DESC);

CREATE TABLE IF NOT EXISTS evidence (
    id            INTEGER PRIMARY KEY,
    target_id     TEXT    NOT NULL,
    source_id     TEXT    NOT NULL,
    origin        TEXT    NOT NULL,  -- after attribution collapsing
    tier          INTEGER NOT NULL,
    weight        REAL    NOT NULL,
    negative      INTEGER NOT NULL DEFAULT 0,
    headline      TEXT,
    url           TEXT,
    t_wall        REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_target ON evidence(target_id, t_wall DESC);

CREATE TABLE IF NOT EXISTS alerts (
    id                INTEGER PRIMARY KEY,
    event_id          TEXT    NOT NULL,
    target_id         TEXT    NOT NULL,
    state             TEXT    NOT NULL,   -- watch|likely|confirmed|retracted
    headline          TEXT,
    url               TEXT,
    score             REAL,
    weight            REAL,
    trail_json        TEXT,               -- full evidence trail at fire time
    detect_latency_ms REAL,
    t_wall            REAL    NOT NULL,
    UNIQUE(event_id, state)               -- enforces dispatch idempotency
);
CREATE INDEX IF NOT EXISTS idx_alerts_time ON alerts(t_wall DESC);

-- Web Push subscriptions from the PWA.
CREATE TABLE IF NOT EXISTS subscriptions (
    endpoint   TEXT PRIMARY KEY,
    keys_json  TEXT NOT NULL,
    user_agent TEXT,
    created_at REAL NOT NULL,
    last_ok_at REAL
);

-- LLM adjudication log: every call, its verdict, its cost. Keeps the "AI is
-- effectively free here" claim auditable rather than assumed.
CREATE TABLE IF NOT EXISTS adjudications (
    id          INTEGER PRIMARY KEY,
    target_id   TEXT NOT NULL,
    item_title  TEXT,
    died        INTEGER,
    confidence  REAL,
    subject     TEXT,
    reason      TEXT,
    latency_ms  REAL,
    cost_usd    REAL,
    fell_back   INTEGER NOT NULL DEFAULT 0,
    t_wall      REAL NOT NULL
);

-- Per-source liveness history, for the staleness watchdog and for spotting a
-- feed that quietly stopped updating weeks ago.
CREATE TABLE IF NOT EXISTS source_health (
    id           INTEGER PRIMARY KEY,
    source_id    TEXT NOT NULL,
    items        INTEGER,
    staleness_s  REAL,
    stale        INTEGER,
    last_error   TEXT,
    t_wall       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_health_source ON source_health(source_id, t_wall DESC);

-- Daily end-to-end drill results. If this table has no recent PASS row, the
-- pipeline is not known to work.
CREATE TABLE IF NOT EXISTS canary_runs (
    id          INTEGER PRIMARY KEY,
    passed      INTEGER NOT NULL,
    latency_ms  REAL,
    detail      TEXT,
    t_wall      REAL NOT NULL
);
