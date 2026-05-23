-- 2026-05-23: early_reply_candidates for the twitterapi.io webhook early-reply watcher.
--
-- Webhook source: twitterapi.io filter rules (one per monitored author, e.g.
-- @mckaywrigley, @yuchenj_uw, @simonw, @alexalbert__, @ericzakariasson).
-- Rules POST to /api/webhooks/twitterapi-io in bin/server.js whenever a tweet
-- matches `from:<handle>`. We persist every fire here in TEST MODE
-- (status='observed') and do NOT auto-draft or auto-post. A future pass will
-- promote rows to status='pending_draft' once we trust the firehose.
--
-- Why a separate table and not twitter_candidates: the discovery pipeline's
-- twitter_candidates table is wide (50+ columns: assigned_style,
-- search_attempt_id, cycle_variant, draft_new_style, batch_id, ...) and every
-- column is meaningful to the scoring/posting cycle. The early-reply rail
-- arrives from a different source (push, not pull), needs to record source-
-- specific metadata (rule_id, monitored_handle, reply_count_at_arrival), and
-- should not contaminate the existing analytics that filter by
-- twitter_candidates.source / matched_project. Keep this table narrow.
--
-- Per CLAUDE.md "No retention pruning, ever": do NOT add a DELETE-by-age job
-- against this table. Every row stays forever (status flips OK; row drops not).

CREATE TABLE IF NOT EXISTS early_reply_candidates (
    id                       SERIAL PRIMARY KEY,

    -- Source identification (which rule fired)
    source                   TEXT NOT NULL DEFAULT 'twitterapi_webhook_early_reply',
    rule_id                  TEXT,                  -- twitterapi.io rule_id
    rule_tag                 TEXT,                  -- the handle (e.g. 'simonw')
    monitored_handle         TEXT,                  -- canonical author handle we watch

    -- Tweet identification
    tweet_id                 TEXT NOT NULL,         -- snowflake ID as string (avoid bigint overflow risk)
    tweet_url                TEXT NOT NULL,
    author_handle            TEXT,                  -- author.userName from payload
    author_followers         INTEGER,
    tweet_text               TEXT,
    tweet_posted_at          TIMESTAMPTZ,           -- parsed from tweet.createdAt
    is_reply                 BOOLEAN,
    in_reply_to_id           TEXT,
    conversation_id          TEXT,
    language                 TEXT,

    -- Engagement snapshot at the moment we received the webhook (t0).
    -- This is critical: early-reply value depends on getting in before reply
    -- count spikes. reply_count_at_arrival lets us measure how early we were.
    reply_count_at_arrival   INTEGER,
    like_count_at_arrival    INTEGER,
    retweet_count_at_arrival INTEGER,
    view_count_at_arrival    INTEGER,
    quote_count_at_arrival   INTEGER,
    bookmark_count_at_arrival INTEGER,

    -- Routing
    project                  TEXT NOT NULL DEFAULT 'fazm',

    -- Lifecycle
    status                   TEXT NOT NULL DEFAULT 'observed',  -- observed | pending_draft | drafted | replied | skipped | expired
    received_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at             TIMESTAMPTZ,
    skip_reason              TEXT,

    -- Raw payload kept for forensic / re-parse purposes. The webhook payload
    -- is the source of truth; if we discover a parse bug we re-derive from
    -- this column rather than re-fetching from twitterapi.io.
    raw_payload              JSONB
);

-- One row per tweet per monitored handle. Same tweet from a different rule
-- (e.g. handle changes monitored sets) would re-insert; that's a corner case
-- worth catching but not blocking.
CREATE UNIQUE INDEX IF NOT EXISTS uq_early_reply_candidates_tweet_handle
    ON early_reply_candidates (tweet_id, monitored_handle);

CREATE INDEX IF NOT EXISTS idx_early_reply_candidates_received_at
    ON early_reply_candidates (received_at DESC);

CREATE INDEX IF NOT EXISTS idx_early_reply_candidates_status
    ON early_reply_candidates (status);

CREATE INDEX IF NOT EXISTS idx_early_reply_candidates_project
    ON early_reply_candidates (project);

CREATE INDEX IF NOT EXISTS idx_early_reply_candidates_monitored_handle
    ON early_reply_candidates (monitored_handle);
