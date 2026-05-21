-- 2026-05-21: stamp the exact discovering search_attempt onto each candidate.
--
-- Why: the dashboard's per-query stats joined candidates -> attempts via
-- (batch_id, project_name), which fans every candidate out to every attempt
-- in its batch. When a batch ran multiple queries for one project (Phase 1
-- model fans out 1-N queries per project — observed up to 4), each posted
-- candidate got credited to every query — including the ones that returned
-- zero tweets. User caught a Runner row showing Dud%=100% AND Posts=1 on
-- the same line.
--
-- The literal X advanced-search string lives in twitter_search_attempts.query;
-- the candidate row only has search_topic (the natural-language seed), so the
-- two never line up textually. New column records the exact attempt the
-- candidate came out of. Old rows stay NULL and the dashboard SQL falls back
-- to the legacy batch fanout for those.

ALTER TABLE twitter_candidates
    ADD COLUMN IF NOT EXISTS search_attempt_id INTEGER
    REFERENCES twitter_search_attempts(id);

CREATE INDEX IF NOT EXISTS idx_tc_search_attempt_id
    ON twitter_candidates(search_attempt_id)
    WHERE search_attempt_id IS NOT NULL;
