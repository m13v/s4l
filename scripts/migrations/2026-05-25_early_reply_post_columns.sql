-- 2026-05-25: extra columns on early_reply_candidates for the posting pipeline.
--
-- Pairs with scripts/early_reply_cycle.py + skill/run-early-reply-twitter.sh.
-- The original 2026-05-23 migration created the table in observe-only TEST
-- mode (status flips between observed | pending_draft | drafted | replied |
-- skipped | expired); now that we're actually posting, we need to record the
-- posting outcome on the row itself:
--
--   posted_at         - timestamp of successful reply
--   our_reply_url     - x.com/<handle>/status/<id> of our reply tweet
--   engagement_style  - the style the picker assigned for this draft
--   post_id           - posts.id of the corresponding row in `posts`
--   batch_id          - the early-reply cycle batch that processed this row
--   last_error        - last error message (for failed rows, debugging only)
--
-- Daily cap is computed off this table (status='posted' rows in the last
-- 24h), NOT off posts.source, so we do NOT need to add a `source` column
-- to the wide `posts` table. The early_reply rail owns its own accounting.
--
-- Per CLAUDE.md "No retention pruning, ever": rows stay forever; status
-- flips OK; row drops not.

ALTER TABLE early_reply_candidates
    ADD COLUMN IF NOT EXISTS posted_at       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS our_reply_url   TEXT,
    ADD COLUMN IF NOT EXISTS engagement_style TEXT,
    ADD COLUMN IF NOT EXISTS post_id         INTEGER,
    ADD COLUMN IF NOT EXISTS batch_id        TEXT,
    ADD COLUMN IF NOT EXISTS last_error      TEXT;

-- Daily-cap query path: SELECT COUNT(*) WHERE status='posted' AND
-- posted_at > NOW() - INTERVAL '24 hours'. status is already indexed; add
-- a partial index for the posted-only sweep so the cap check stays O(log N)
-- even as the observed-row archive grows.
CREATE INDEX IF NOT EXISTS idx_early_reply_candidates_posted_at
    ON early_reply_candidates (posted_at DESC)
    WHERE status = 'posted';
