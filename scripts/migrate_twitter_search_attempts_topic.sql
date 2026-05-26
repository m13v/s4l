-- 2026-05-26: attach search_topic to each Twitter search attempt so the
-- dashboard can aggregate per-topic dud rate / engagement / clicks, mirroring
-- the per-topic surface we already have for Reddit (reddit_search_attempts.seed).
--
-- Context: 2026-05-26 pick_search_topic.py now picks ONE topic per project per
-- cycle (log-smoothed weighted random + 10% invent). The Twitter scan model
-- then drafts queries from that topic and we already stamp twitter_candidates.
-- search_topic when they land. The attempts table was the missing link: zero-
-- result queries still have a known driving topic at scan time but it never got
-- persisted, so the dashboard cannot show dud_rate-per-topic on Twitter.
--
-- This column is optional (NULL allowed) so older rows keep working until the
-- backfill catches them up via:
--   (a) join through twitter_candidates.search_attempt_id (non-dud attempts)
--   (b) fanout via (batch_id, project_name) from a sibling non-dud attempt in
--       the same cycle (dud attempts whose siblings DID return candidates)
-- Fully-dud cycles for a project stay NULL until run-twitter-cycle.sh is
-- extended to pass search_topic through the queries_used JSON envelope.

ALTER TABLE twitter_search_attempts
    ADD COLUMN IF NOT EXISTS search_topic TEXT;

CREATE INDEX IF NOT EXISTS idx_tsa_search_topic
    ON twitter_search_attempts (search_topic, project_name, ran_at DESC)
    WHERE search_topic IS NOT NULL;
