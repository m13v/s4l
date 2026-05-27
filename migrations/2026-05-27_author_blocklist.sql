-- 2026-05-27_author_blocklist.sql
--
-- Per-install live exclusion list for engagement-loop / bot accounts.
--
-- Background: 2026-05-27 incident — @MandyMondayAI sent 160 replies into our
-- timeline over 48h and we replied to ~all of them ($905 orchestrator cost,
-- $6.5k transcript estimate). config.json `exclusions.twitter_accounts` only
-- protects against handles we already know about; this table is the dynamic,
-- in-band escape hatch.
--
-- Two write paths:
--   1) velocity_gate (deterministic, server-side):
--        Inside POST /api/v1/replies, before inserting a candidate, we count
--        outbound replies to the same author in the last 24h / 7d windows.
--        Aggressive thresholds (24h>=6 OR 7d>=15) auto-insert a row here with
--        classification='velocity_auto' and severity='hard', then drop the
--        incoming candidate silently (no `replies` row written).
--   2) engage_llm (judgment, prompted):
--        The Twitter/LinkedIn/GitHub engage prompts can call
--          reply_db.py blocklist add <platform> <handle> --reason "<...>"
--        when the model identifies a bot / engagement farmer / dead loop.
--
-- Read path: POST /api/v1/replies consults this table BEFORE the velocity
-- count. severity='hard' rows that are not expired -> candidate dropped
-- silently and counter incremented on the scanner stats line.
--
-- Severity:
--   hard  -> always skip at ingest, never reaches the LLM
--   soft  -> insert candidate but flag it; engage prompt sees the warning
--            and decides itself (used for borderline AI-suffix handles)
--
-- Scope: installation_id-level by default (a bot for one of our accounts is
-- a bot for all). `project` is nullable for per-project soft blocks (e.g.,
-- a competitor's handle blocked only when posting about one product).

CREATE TABLE IF NOT EXISTS author_blocklist (
    installation_id    UUID         NOT NULL,
    platform           TEXT         NOT NULL,
    handle             TEXT         NOT NULL,   -- lowercase normalized
    classification     TEXT         NOT NULL,   -- bot | engagement_loop | manual_block | velocity_auto
    severity           TEXT         NOT NULL,   -- hard | soft
    reason             TEXT         NOT NULL,
    added_by           TEXT         NOT NULL,   -- engage_llm | velocity_gate | dashboard | config_migration
    source_reply_id    INTEGER,
    source_session_id  TEXT,
    project            TEXT,                     -- NULL = applies to all projects
    expires_at         TIMESTAMPTZ,              -- NULL = forever
    hit_count          INTEGER      NOT NULL DEFAULT 0,
    last_hit_at        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (installation_id, platform, handle)
);

-- Hot-path lookup (installation_id, platform, handle) is already covered by
-- the PRIMARY KEY index, so the blocklist check in POST /api/v1/replies is
-- O(log n) on that index. The severity + expires_at filter is a cheap row-
-- level check after the seek. No additional hot-path index needed.
-- (A partial index with `expires_at > NOW()` would be redundant AND illegal
--  because NOW() isn't IMMUTABLE per Postgres index-predicate rules.)

-- Dashboard list view ordering. Most recently created first.
CREATE INDEX IF NOT EXISTS idx_blocklist_recent
    ON author_blocklist (installation_id, created_at DESC);

-- Audit: which session/reply triggered an llm-added block. Sparse, useful
-- for the dashboard drill-down.
CREATE INDEX IF NOT EXISTS idx_blocklist_source_session
    ON author_blocklist (source_session_id)
    WHERE source_session_id IS NOT NULL;
