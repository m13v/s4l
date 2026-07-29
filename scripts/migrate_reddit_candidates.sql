-- 2026-05-06: persistent reddit_candidates queue + reddit_batches phase tracking.
--
-- Mirrors twitter_candidates / twitter_batches. Goal: stop dropping
-- post-attempts on transient failures (CDP timeout, comment_box_not_found,
-- not_logged_in, browser crash). Locked threads at submit time used to burn
-- the entire discover+ripen+draft pipeline for one wholesale loss. With this
-- queue:
--   - transient failures stay status='pending', attempt_count++; the next
--     cycle's Phase 0 salvages them while still fresh and replays the post
--     phase (drafts are reused when <DRAFT_TTL old, mirroring Twitter).
--   - permanent failures (thread_locked, archived, deleted, account_blocked)
--     are marked status='failed' so we never re-evaluate them.
--   - status='posted' rows carry post_id back to posts() for join-friendly
--     dashboards.
--
-- Phase 0 (added to run-reddit-search.sh) hard-expires pending rows older
-- than FRESHNESS_HOURS (24h, longer than Twitter's 6h since Reddit threads
-- stay actionable longer) and re-assigns still-fresh orphaned rows to the
-- new batch_id. Eligibility: attempt_count < MAX_ATTEMPTS (3) AND
-- (last_attempt_at IS NULL OR last_attempt_at < NOW() - RETRY_BACKOFF (30m)).

CREATE TABLE IF NOT EXISTS reddit_candidates (
    id                      SERIAL PRIMARY KEY,
    thread_url              TEXT NOT NULL UNIQUE,
    thread_author           TEXT,
    thread_title            TEXT,
    subreddit               TEXT,
    matched_project         TEXT,
    search_topic            TEXT,
    -- T0 / T1 metrics from ripen_reddit_plan.py. delta_score is the
    -- composite (Δup + 4*Δcomments) used by the floor gate; kept here so the
    -- dashboard can plot velocity per candidate without recomputing.
    score_t0                INTEGER,
    comments_t0             INTEGER,
    score_t1                INTEGER,
    comments_t1             INTEGER,
    delta_score             DOUBLE PRECISION,
    t1_checked_at           TIMESTAMPTZ,
    -- Draft persistence: lets a salvaged row skip the LLM redraft cost when
    -- drafted_at is fresh (<60 min). Mirrors twitter_candidates.draft_*.
    draft_text              TEXT,
    draft_engagement_style  TEXT,
    drafted_at              TIMESTAMPTZ,
    -- Queue state.
    status                  TEXT DEFAULT 'pending',
    batch_id                TEXT,
    attempt_count           INTEGER NOT NULL DEFAULT 0,
    last_attempt_at         TIMESTAMPTZ,
    last_failure_reason     TEXT,
    -- post linkage. NULL until status='posted' and log_post returns a row.
    post_id                 INTEGER REFERENCES posts(id) ON DELETE SET NULL,
    posted_at               TIMESTAMPTZ,
    -- Discovery context. NOW() at INSERT time; used by Phase 0 to compute
    -- staleness for hard-expire (compare against FRESHNESS_HOURS).
    discovered_at           TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT reddit_candidates_status_check
        CHECK (status IN ('pending','posted','skipped','expired','failed'))
);

-- Indexes mirror twitter_candidates' access patterns:
--   * batch_id           — Phase 0 salvage UPDATE, per-cycle COUNT(*)
--   * status+attempt     — Phase 0 salvage SELECT (pending, attempt<3)
--   * drafted_at         — draft TTL check on salvaged rows
--   * thread_url         — log_post lookup, dedup
CREATE INDEX IF NOT EXISTS idx_rc_batch_id        ON reddit_candidates(batch_id);
CREATE INDEX IF NOT EXISTS idx_rc_status_attempt  ON reddit_candidates(status, attempt_count, last_attempt_at)
    WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_rc_drafted_at      ON reddit_candidates(drafted_at)
    WHERE drafted_at IS NOT NULL;

-- reddit_batches (twitter_batches-style phase tracking) was created here on
-- 2026-05-06 but no code ever wrote to it; dropped 2026-07-29.
