-- 2026-04-29: track every LinkedIn search query the LLM drafted, including
-- the ones that returned ZERO usable candidates, so the next cycle can be
-- told "do not redraft these — they have been flat for the last week".
--
-- Why 7 days (vs Twitter's 24-48h)? LinkedIn cycle is sparser (manual /
-- ad-hoc launchd, no 20-min cron) so a 48h dud window collects too few
-- samples to learn from. A 7-day window mirrors Twitter's evidence density
-- at LinkedIn's call frequency.
--
-- One row per (query, project) per cycle. candidates_found = 0 marks a dud.
-- serp_quality_score (0-10) is also captured here so a query that returns
-- 30 results but they are all influencer slop ranks lower than a query that
-- returns 5 high-fit practitioner posts.
--
-- Pair with scripts/top_linkedin_queries.py (positive signal) and
-- scripts/top_dud_linkedin_queries.py (negative signal).

CREATE TABLE IF NOT EXISTS linkedin_search_attempts (
    id                  SERIAL PRIMARY KEY,
    query               TEXT NOT NULL,
    project_name        TEXT,
    search_topic        TEXT,
    candidates_found    INTEGER NOT NULL DEFAULT 0,
    serp_quality_score  DOUBLE PRECISION,           -- 0-10, LLM-rated SERP fit
    batch_id            TEXT,
    ran_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

ALTER TABLE linkedin_search_attempts
    ADD COLUMN IF NOT EXISTS search_topic TEXT;

CREATE INDEX IF NOT EXISTS idx_lsa_ran_at
    ON linkedin_search_attempts(ran_at DESC);

CREATE INDEX IF NOT EXISTS idx_lsa_dud_lookup
    ON linkedin_search_attempts(project_name, candidates_found, ran_at DESC)
    WHERE candidates_found = 0;

CREATE INDEX IF NOT EXISTS idx_lsa_low_quality
    ON linkedin_search_attempts(project_name, serp_quality_score, ran_at DESC)
    WHERE serp_quality_score IS NOT NULL AND serp_quality_score < 4.0;

CREATE INDEX IF NOT EXISTS idx_lsa_search_topic
    ON linkedin_search_attempts(project_name, search_topic, ran_at DESC)
    WHERE search_topic IS NOT NULL;
