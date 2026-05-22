-- 2026-05-22_tail_link_variant.sql
--
-- Add tail_link_variant VARCHAR(32) to posts for the Twitter A/B test
-- that compares replies with a link tail vs. replies without one.
--
-- Values expected during the experiment:
--   'link'    -- reply was posted with the bridge-sentence + URL tail
--   'no_link' -- reply was posted without any link tail
--   NULL      -- row predates the experiment or platform where test does not apply
--
-- Idempotent: safe to re-run.

ALTER TABLE posts ADD COLUMN IF NOT EXISTS tail_link_variant VARCHAR(32);

-- Partial index: only rows that are part of the experiment.
-- Cheap because the majority of legacy rows will remain NULL.
CREATE INDEX IF NOT EXISTS idx_posts_tail_link_variant
  ON posts(tail_link_variant) WHERE tail_link_variant IS NOT NULL;
