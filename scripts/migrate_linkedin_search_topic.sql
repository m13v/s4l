-- 2026-05-29: split LinkedIn assigned search_topic from literal search_query.
--
-- search_topic is picked once per project cycle from project_search_topics,
-- while search_query is the specific LinkedIn SERP phrase drafted from that
-- topic. Keeping both mirrors the Twitter pipeline and lets topic analytics
-- aggregate many literal query attempts under one project-level concept.

ALTER TABLE linkedin_candidates
    ADD COLUMN IF NOT EXISTS search_topic TEXT;

ALTER TABLE linkedin_search_attempts
    ADD COLUMN IF NOT EXISTS search_topic TEXT;

CREATE INDEX IF NOT EXISTS idx_lc_search_topic
    ON linkedin_candidates(search_topic, discovered_at DESC)
    WHERE search_topic IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_lsa_search_topic
    ON linkedin_search_attempts(project_name, search_topic, ran_at DESC)
    WHERE search_topic IS NOT NULL;
