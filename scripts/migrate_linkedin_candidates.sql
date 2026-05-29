-- 2026-04-29: linkedin_candidates table mirrors twitter_candidates so the
-- LinkedIn pipeline can do query intelligence + ranked candidate selection
-- without two-phase delta-momentum (LinkedIn runs are one-off, not on a
-- 20-min cycle). The single-shot substitute for delta is engagement velocity
-- computed against post age at scrape time:
--
--   velocity_score = (reactions + 2*comments + 3*reposts) / max(age_hours, 0.5)
--
-- Comments weighted higher than reposts than reactions: live discussion is
-- the best signal that our reply will be seen by humans, not bots.
--
-- serp_quality_score (0-10) is computed per-query, not per-candidate, but
-- denormalised onto every candidate row from that query so dashboard queries
-- can filter "candidates from a healthy SERP only" without a join.
--
-- Status lifecycle: pending -> posted | skipped | expired. Same as Twitter.

CREATE TABLE IF NOT EXISTS linkedin_candidates (
    id                      SERIAL PRIMARY KEY,
    post_url                TEXT NOT NULL UNIQUE,
    activity_id             TEXT,                   -- numeric URN (16-19 digits)
    all_urns                TEXT,                   -- comma-separated URN ids seen on this post
    author_name             TEXT,
    author_profile_url      TEXT,
    author_followers        INTEGER,                -- best-effort, may be null on slim SERP rows
    post_text               TEXT,                   -- excerpt, capped at 500 chars
    post_posted_at          TIMESTAMP WITH TIME ZONE,
    age_hours               DOUBLE PRECISION,
    reactions               INTEGER NOT NULL DEFAULT 0,
    comments                INTEGER NOT NULL DEFAULT 0,
    reposts                 INTEGER NOT NULL DEFAULT 0,
    engagement_velocity     DOUBLE PRECISION NOT NULL DEFAULT 0,
    velocity_score          DOUBLE PRECISION NOT NULL DEFAULT 0,
    serp_quality_score      DOUBLE PRECISION,       -- 0-10, denormalised per query
    search_topic            TEXT,                   -- assigned project-level topic seed
    search_query            TEXT,
    matched_project         TEXT,
    language                TEXT,
    status                  TEXT NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending','posted','skipped','expired')),
    discovered_at           TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    posted_at               TIMESTAMP WITH TIME ZONE,
    post_id                 INTEGER REFERENCES posts(id),
    batch_id                TEXT,
    -- Phase A and Phase B share one persistent draft like Twitter does, so a
    -- failed CDP post doesn't waste the next cycle's redraft tokens.
    draft_reply_text        TEXT,
    draft_engagement_style  TEXT,
    drafted_at              TIMESTAMP WITH TIME ZONE
);

ALTER TABLE linkedin_candidates
    ADD COLUMN IF NOT EXISTS search_topic TEXT;

CREATE INDEX IF NOT EXISTS idx_lc_status_score
    ON linkedin_candidates(status, velocity_score DESC);

CREATE INDEX IF NOT EXISTS idx_lc_batch_id
    ON linkedin_candidates(batch_id);

CREATE INDEX IF NOT EXISTS idx_lc_post_url
    ON linkedin_candidates(post_url);

CREATE INDEX IF NOT EXISTS idx_lc_drafted_at
    ON linkedin_candidates(drafted_at)
    WHERE drafted_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_lc_search_query
    ON linkedin_candidates(search_query, discovered_at DESC)
    WHERE search_query IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_lc_search_topic
    ON linkedin_candidates(search_topic, discovered_at DESC)
    WHERE search_topic IS NOT NULL;
