-- 2026-05-12_generation_trace.sql
--
-- Add generation_trace JSONB to posts so we can audit, per-post:
--   - which few-shot examples Claude saw (top_performers report verbatim)
--   - which top_search_topics report Claude saw
--   - the recent-comments cluster Claude saw
--   - prompt size (chars), model, generated_at
--
-- Motivation: as of 2026-05-12 the generator pulls examples from
-- top_performers.py (now click-weighted) and feeds them into the prompt,
-- but nothing in the database recorded WHICH examples were used for a
-- given output. This made A/B-style prompt iteration impossible: a post
-- with 0 clicks vs a post with 70 clicks had no audit trail of what
-- context produced each, so we could not say "the gold-tier prompt for
-- mk0r was the one that included the Persian-prompt-writing example".
--
-- Shape (version 1):
--   {
--     "version": 1,
--     "generated_at": "ISO-8601 UTC",
--     "model": "claude-opus-4-7",
--     "platform": "github",
--     "project": "mk0r",
--     "prompt_chars": 12345,
--     "examples": {
--       "top_performers_text": "...full text Claude saw...",
--       "top_search_topics_text": "...full text Claude saw...",
--       "recent_comment_ids": [123, 124, 125, 126, 127]
--     },
--     "scoring": {
--       "score_formula": "clicks*10 + comments*3 + upvotes_net",
--       "min_score_floor": 5
--     }
--   }
--
-- Idempotent: safe to re-run.

ALTER TABLE posts ADD COLUMN IF NOT EXISTS generation_trace JSONB;

-- Partial index: only generations with a trace. Cheap because most legacy
-- rows will be NULL until backfill, and lookups will always filter to the
-- presence of the column anyway.
CREATE INDEX IF NOT EXISTS idx_posts_generation_trace_not_null
  ON posts(id) WHERE generation_trace IS NOT NULL;
