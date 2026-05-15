-- schema-postgres.sql — Neon Postgres schema (primary database)
-- Run once: psql "$DATABASE_URL" -f schema-postgres.sql

CREATE TABLE IF NOT EXISTS posts (
    id SERIAL PRIMARY KEY,
    platform TEXT NOT NULL,
    thread_url TEXT NOT NULL,
    thread_author TEXT,
    thread_author_handle TEXT,
    thread_title TEXT,
    thread_content TEXT,
    thread_engagement TEXT,
    our_url TEXT,
    our_content TEXT NOT NULL,
    our_account TEXT NOT NULL,
    posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'active',
    status_checked_at TIMESTAMP,
    engagement_updated_at TIMESTAMP,
    upvotes INTEGER,
    comments_count INTEGER,
    views INTEGER,
    source_turn_id INTEGER,
    source_summary TEXT,
    top_comment_author TEXT,
    top_comment_content TEXT,
    top_comment_upvotes INTEGER,
    top_comment_url TEXT,
    link_edited_at TIMESTAMP,
    link_edit_content TEXT
);

-- Add columns to existing deployments (safe to re-run)
ALTER TABLE posts ADD COLUMN IF NOT EXISTS link_edited_at TIMESTAMP;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS link_edit_content TEXT;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS scan_no_change_count INTEGER DEFAULT 0;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS project_name TEXT;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS feedback_report_used BOOLEAN DEFAULT FALSE;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS engagement_style TEXT;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS resurrected_at TIMESTAMP;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS campaign_id INTEGER REFERENCES campaigns(id);

CREATE INDEX IF NOT EXISTS idx_posts_platform ON posts(platform);
CREATE INDEX IF NOT EXISTS idx_posts_resurrected_at ON posts(resurrected_at) WHERE resurrected_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS threads (
    id SERIAL PRIMARY KEY,
    platform TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    author TEXT,
    author_handle TEXT,
    title TEXT,
    content TEXT,
    engagement TEXT,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS our_posts (
    id SERIAL PRIMARY KEY,
    thread_id INTEGER REFERENCES threads(id),
    platform TEXT NOT NULL,
    url TEXT,
    content TEXT NOT NULL,
    account TEXT,
    posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS campaigns (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    platforms TEXT DEFAULT 'twitter,reddit,moltbook',
    status TEXT DEFAULT 'active',
    max_posts_per_day INTEGER DEFAULT 4,
    max_posts_total INTEGER,
    posts_made INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS max_posts_total INTEGER;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS suffix TEXT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS sample_rate NUMERIC(4,3) DEFAULT 1.000;

-- Campaign attribution lives as a single nullable column on each surface
-- table (posts, replies, dm_messages). One campaign per outbound action,
-- which matches reality. The legacy post_campaigns join table was dropped
-- 2026-04-27.

CREATE TABLE IF NOT EXISTS replies (
    id SERIAL PRIMARY KEY,
    post_id INTEGER REFERENCES posts(id),
    platform TEXT NOT NULL,
    their_comment_id TEXT NOT NULL,
    their_author TEXT,
    their_content TEXT,
    their_comment_url TEXT,
    our_reply_id TEXT,
    our_reply_content TEXT,
    our_reply_url TEXT,
    parent_reply_id INTEGER REFERENCES replies(id),
    moltbook_post_uuid TEXT,
    moltbook_parent_comment_uuid TEXT,
    depth INTEGER DEFAULT 1,
    status TEXT DEFAULT 'pending',
    skip_reason TEXT,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processing_at TIMESTAMP,
    replied_at TIMESTAMP,
    CONSTRAINT replies_platform_comment_id_unique UNIQUE (platform, their_comment_id)
);

-- Add columns to existing deployments (safe to re-run)
ALTER TABLE replies ADD COLUMN IF NOT EXISTS processing_at TIMESTAMP;
ALTER TABLE replies ADD COLUMN IF NOT EXISTS engagement_style TEXT;
ALTER TABLE replies ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE replies ADD CONSTRAINT IF NOT EXISTS replies_platform_comment_id_unique UNIQUE (platform, their_comment_id);

-- Per-reply engagement stats. Mirror posts schema so dashboards can UNION
-- the two surfaces. Populated by update_stats.py reply functions.
-- Reddit + GitHub: views always 0 (not exposed). LinkedIn + Moltbook
-- replies: not populated (LinkedIn scraping pattern banned 2026-04-17;
-- Moltbook reply API not wired). engagement_updated_at is the freshness
-- gate so reply scrapers can skip rows refreshed in the last few hours.
ALTER TABLE replies ADD COLUMN IF NOT EXISTS upvotes INTEGER DEFAULT 0;
ALTER TABLE replies ADD COLUMN IF NOT EXISTS comments_count INTEGER DEFAULT 0;
ALTER TABLE replies ADD COLUMN IF NOT EXISTS views INTEGER DEFAULT 0;
ALTER TABLE replies ADD COLUMN IF NOT EXISTS engagement_updated_at TIMESTAMP;
CREATE INDEX IF NOT EXISTS idx_replies_engagement_updated_at ON replies(engagement_updated_at);

CREATE TABLE IF NOT EXISTS dms (
    id SERIAL PRIMARY KEY,
    platform TEXT NOT NULL DEFAULT 'reddit',
    reply_id INTEGER REFERENCES replies(id),
    post_id INTEGER REFERENCES posts(id),
    their_author TEXT NOT NULL,
    their_content TEXT,
    our_dm_content TEXT,
    comment_context TEXT,
    status TEXT DEFAULT 'pending',
    skip_reason TEXT,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sent_at TIMESTAMP,
    CONSTRAINT dms_platform_author_reply_unique UNIQUE (platform, their_author, reply_id)
);

CREATE INDEX IF NOT EXISTS idx_dms_status ON dms(status);
CREATE INDEX IF NOT EXISTS idx_dms_their_author ON dms(their_author);

-- Evolve dms into conversation headers
ALTER TABLE dms ADD COLUMN IF NOT EXISTS chat_url TEXT;
ALTER TABLE dms ADD COLUMN IF NOT EXISTS conversation_status TEXT DEFAULT 'active';
ALTER TABLE dms ADD COLUMN IF NOT EXISTS tier INTEGER DEFAULT 1;
ALTER TABLE dms ADD COLUMN IF NOT EXISTS last_message_at TIMESTAMP;
ALTER TABLE dms ADD COLUMN IF NOT EXISTS message_count INTEGER DEFAULT 0;
ALTER TABLE dms ADD COLUMN IF NOT EXISTS interest_level TEXT;  -- no_response | general_discussion | cold | warm | hot | declined | not_our_prospect

-- Qualification + book-a-call conversion flow
ALTER TABLE dms ADD COLUMN IF NOT EXISTS target_project TEXT;              -- project we are pursuing for this thread (set at outreach)
ALTER TABLE dms ADD COLUMN IF NOT EXISTS qualification_status TEXT DEFAULT 'pending';  -- pending | asked | answered | qualified | disqualified
ALTER TABLE dms ADD COLUMN IF NOT EXISTS qualification_notes TEXT;
ALTER TABLE dms ADD COLUMN IF NOT EXISTS booking_link_sent_at TIMESTAMP;
ALTER TABLE dms ADD COLUMN IF NOT EXISTS first_product_mention_at TIMESTAMP;  -- stamped by set-tier on first transition to tier >= 2 (Tier 1 -> Tier 2 pivot)
ALTER TABLE dms ADD COLUMN IF NOT EXISTS icp_precheck TEXT;                -- DEPRECATED: superseded by icp_matches; kept during transition
ALTER TABLE dms ADD COLUMN IF NOT EXISTS icp_matches JSONB NOT NULL DEFAULT '[]'::jsonb;  -- [{project,label,notes,at}, ...] per-project ICP verdicts
CREATE INDEX IF NOT EXISTS idx_dms_icp_matches ON dms USING gin (icp_matches);
ALTER TABLE dms ADD COLUMN IF NOT EXISTS prospect_id INTEGER;              -- FK added below after prospects table defined
ALTER TABLE dms ADD COLUMN IF NOT EXISTS model TEXT;                       -- dominant Claude model for the outreach session

-- Per-DM short link for booking attribution. The link is hosted on the matched
-- project's marketing site (e.g. https://aiphoneordering.com/r/<code>) and
-- 302s to Cal.com with metadata[utm_content]=dm_<id> so cal_bookings closes
-- the loop. Click count + first/last click are stamped by the resolver on hit.
ALTER TABLE dms ADD COLUMN IF NOT EXISTS short_link_code TEXT;
ALTER TABLE dms ADD COLUMN IF NOT EXISTS short_link_target_url TEXT;
ALTER TABLE dms ADD COLUMN IF NOT EXISTS short_link_clicks INTEGER NOT NULL DEFAULT 0;
ALTER TABLE dms ADD COLUMN IF NOT EXISTS short_link_first_click_at TIMESTAMP;
ALTER TABLE dms ADD COLUMN IF NOT EXISTS short_link_last_click_at TIMESTAMP;
CREATE UNIQUE INDEX IF NOT EXISTS idx_dms_short_link_code ON dms(short_link_code) WHERE short_link_code IS NOT NULL;

-- Dashboard "skip until next inbound" affordance for needs_human (and any other)
-- escalations: while snoozed_until > NOW(), engage-dm-replies.sh hides the row
-- and the escalation card collapses to a "snoozed" badge. Auto-cleared by
-- dm_conversation.log_inbound() when a new inbound message arrives, which
-- re-surfaces the DM under its existing conversation_status on the next cycle.
ALTER TABLE dms ADD COLUMN IF NOT EXISTS snoozed_until TIMESTAMP;
CREATE INDEX IF NOT EXISTS idx_dms_snoozed_until ON dms(snoozed_until) WHERE snoozed_until IS NOT NULL;

-- prospects: persistent per-(platform, author) record. One person can have multiple DMs over time.
CREATE TABLE IF NOT EXISTS prospects (
    id SERIAL PRIMARY KEY,
    platform TEXT NOT NULL,
    author TEXT NOT NULL,
    profile_url TEXT,
    display_name TEXT,
    headline TEXT,
    bio TEXT,
    follower_count INTEGER,
    recent_activity TEXT,
    company TEXT,
    role TEXT,
    profile_fetched_at TIMESTAMP,
    notes TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT prospects_platform_author_unique UNIQUE (platform, author)
);

CREATE INDEX IF NOT EXISTS idx_prospects_platform_author ON prospects(platform, author);
CREATE INDEX IF NOT EXISTS idx_prospects_profile_fetched ON prospects(profile_fetched_at);

-- dms.prospect_id FK (added after prospects table exists)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'dms_prospect_id_fkey' AND table_name = 'dms'
    ) THEN
        ALTER TABLE dms ADD CONSTRAINT dms_prospect_id_fkey FOREIGN KEY (prospect_id) REFERENCES prospects(id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_dms_prospect_id ON dms(prospect_id);
CREATE INDEX IF NOT EXISTS idx_dms_target_project ON dms(target_project);
CREATE INDEX IF NOT EXISTS idx_dms_qualification_status ON dms(qualification_status);

-- dm_messages: every message in a DM conversation (ours and theirs)
CREATE TABLE IF NOT EXISTS dm_messages (
    id SERIAL PRIMARY KEY,
    dm_id INTEGER NOT NULL REFERENCES dms(id),
    direction TEXT NOT NULL CHECK (direction IN ('outbound', 'inbound')),
    author TEXT NOT NULL,
    content TEXT NOT NULL,
    message_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    logged_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_dm_messages_dm_id ON dm_messages(dm_id);
CREATE INDEX IF NOT EXISTS idx_dm_messages_direction ON dm_messages(direction);

-- human_dm_replies: stores human-authored INSTRUCTIONS for the DM-reply agent on
-- escalated DMs. Two ingest paths feed this table: (1) Gmail replies to escalation
-- emails (matching [DM #N] in the subject) ingested by ingest_human_dm_replies.py,
-- and (2) the dashboard /api/dm/:id/instructions endpoint. Phase 0 of
-- engage-dm-replies.sh treats `instructions` as direction (not literal text) and
-- has the LLM craft a natural reply from it.
-- Column 'resend_email_id' is historical; we now store the Gmail message id here
-- when the source is Gmail (NULL for dashboard inserts).
-- reply_channel selects the delivery surface: 'dm' (private only, default),
-- 'public' (post on the original public thread only), or 'both' (post publicly
-- AND send the DM, paired, same instruction text drives both). public_reply_id
-- is set by phase 0 once the public-side `replies` row is logged.
CREATE TABLE IF NOT EXISTS human_dm_replies (
    id SERIAL PRIMARY KEY,
    dm_id INTEGER NOT NULL REFERENCES dms(id),
    platform TEXT NOT NULL,
    their_author TEXT NOT NULL,
    project_name TEXT,
    instructions TEXT NOT NULL,
    email_subject TEXT,
    resend_email_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TIMESTAMP,
    reply_channel TEXT NOT NULL DEFAULT 'dm'
        CHECK (reply_channel IN ('dm', 'public', 'both')),
    public_reply_id INTEGER REFERENCES replies(id)
);

-- Backwards-compat for deployments that pre-date the channel split.
ALTER TABLE human_dm_replies
    ADD COLUMN IF NOT EXISTS reply_channel TEXT NOT NULL DEFAULT 'dm'
        CHECK (reply_channel IN ('dm', 'public', 'both'));
ALTER TABLE human_dm_replies
    ADD COLUMN IF NOT EXISTS public_reply_id INTEGER REFERENCES replies(id);

CREATE INDEX IF NOT EXISTS idx_human_dm_replies_status ON human_dm_replies(status);
CREATE INDEX IF NOT EXISTS idx_human_dm_replies_dm_id ON human_dm_replies(dm_id);
CREATE INDEX IF NOT EXISTS idx_human_dm_replies_project ON human_dm_replies(project_name);
CREATE INDEX IF NOT EXISTS idx_human_dm_replies_reply_channel ON human_dm_replies(reply_channel);
CREATE UNIQUE INDEX IF NOT EXISTS idx_human_dm_replies_gmail_id
    ON human_dm_replies(resend_email_id) WHERE resend_email_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS thread_comments (
    id SERIAL PRIMARY KEY,
    thread_id INTEGER,
    author TEXT,
    author_handle TEXT,
    content TEXT,
    engagement TEXT,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- claude_sessions: one row per `claude -p` invocation in a runner script.
-- Activity rows in posts/replies/dms reference session_id; cost is split
-- evenly across all activities sharing the same session at query time.
CREATE TABLE IF NOT EXISTS claude_sessions (
    session_id UUID PRIMARY KEY,
    script TEXT NOT NULL,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP,
    duration_ms BIGINT,
    total_cost_usd NUMERIC(10, 6),
    input_tokens BIGINT,
    output_tokens BIGINT,
    cache_read_tokens BIGINT,
    cache_creation_tokens BIGINT,
    model_breakdown JSONB,
    logged_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_claude_sessions_started ON claude_sessions(started_at DESC);

-- Dominant model id used in the session (picked by max output_tokens across
-- model_breakdown). Flat column for convenience; model_breakdown retains the
-- full per-model split for multi-model sessions.
ALTER TABLE claude_sessions ADD COLUMN IF NOT EXISTS model TEXT;

-- orchestrator_cost_usd: native SDK cost from the result line of the stream
-- (streamRes.total_cost_usd in bin/server.js). This reflects ONLY the
-- orchestrator turns and EXCLUDES Task subagent token costs (see Anthropic
-- claude-code issue #43945). It is the authoritative value Anthropic bills
-- for the orchestrator session, but undercounts when subagents are spawned.
-- Compare against total_cost_usd (manual full-transcript estimate including
-- subagents, computed by scripts/log_claude_session.py).
ALTER TABLE claude_sessions ADD COLUMN IF NOT EXISTS orchestrator_cost_usd NUMERIC(10, 6);

ALTER TABLE posts        ADD COLUMN IF NOT EXISTS claude_session_id UUID;
ALTER TABLE replies      ADD COLUMN IF NOT EXISTS claude_session_id UUID;
ALTER TABLE dms          ADD COLUMN IF NOT EXISTS claude_session_id UUID;
ALTER TABLE dm_messages  ADD COLUMN IF NOT EXISTS claude_session_id UUID;

-- Per-row model stamp, backfilled by log_claude_session.py after each session
-- ends. Lets dashboards / audits filter by model without joining claude_sessions.
ALTER TABLE dm_messages  ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE replies      ADD COLUMN IF NOT EXISTS campaign_id INTEGER REFERENCES campaigns(id);
ALTER TABLE dm_messages  ADD COLUMN IF NOT EXISTS campaign_id INTEGER REFERENCES campaigns(id);

CREATE INDEX IF NOT EXISTS idx_posts_claude_session       ON posts(claude_session_id)       WHERE claude_session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_replies_claude_session     ON replies(claude_session_id)     WHERE claude_session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_dms_claude_session         ON dms(claude_session_id)         WHERE claude_session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_dm_messages_claude_session ON dm_messages(claude_session_id) WHERE claude_session_id IS NOT NULL;

-- Precomputed dashboard snapshots. Local operator writes here via
-- scripts/precompute_dashboard_stats.py on a launchd timer; Cloud Run
-- reads the same rows so hosted clients see warm stats without needing
-- the operator's disk. Key is the snapshot filename (funnel_stats_7d,
-- activity_stats_24h, etc.) and updated_at is the source of truth for
-- freshness (bin/server.js applies a max-age like the on-disk path did).
CREATE TABLE IF NOT EXISTS dashboard_cache (
  cache_key   TEXT PRIMARY KEY,
  payload     JSONB NOT NULL,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_dashboard_cache_updated ON dashboard_cache(updated_at DESC);

-- Per-post per-day snapshot of posts.views. Written by the Reddit + Twitter
-- refresh jobs every time they scrape a current view count. The latest
-- observation for a given (post_id, day) overwrites the prior one via
-- UPSERT, so end-of-day has the final number. The dashboard computes
-- daily deltas with LAG() to render "views earned on day D".
CREATE TABLE IF NOT EXISTS post_views_daily (
  post_id     INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  day         DATE NOT NULL,
  views       INTEGER NOT NULL,
  captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (post_id, day)
);
CREATE INDEX IF NOT EXISTS idx_post_views_daily_day ON post_views_daily(day);

