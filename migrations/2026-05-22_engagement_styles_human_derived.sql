-- 2026-05-22_engagement_styles_human_derived.sql
--
-- Daily-refreshed table of engagement styles synthesized by Claude from the
-- top-performing HUMAN replies surfaced in `thread_top_replies` over the
-- previous 24h. A new style is generated each day; the engagement_styles
-- picker pulls the latest active row 5% of the time (additive 5% branch on
-- top of the existing 5% INVENT branch + 90% scored-USE branch).
--
-- Why a new table, not a JSONB blob in engagement_styles.py: the seed style
-- list there is curated/locked. Human-derived styles are auto-generated,
-- noisy, and need their own per-row provenance (which replies seeded which
-- style, when, by which model). Keeping them in Postgres lets us:
--   - audit which human replies a given style came from
--   - flip status='deactivated' on a bad day's synthesis without redeploying
--   - join against posts/thread_top_replies later for outcome tracking
--
-- Schema rationale:
--   - name/description/example/note: same shape as the seed style dict so
--     the picker can return it uniformly.
--   - best_in: JSONB with the same {twitter: [...], reddit: [...],
--     linkedin: [...]} shape used by the seed styles for context-gating.
--   - source_*: provenance, lets a future audit trace a style back to the
--     human replies it was distilled from.
--   - source_post_ids: array of thread_top_replies.id values (NOT
--     posts.id). Stored as INTEGER[] (matching that table's PK type).
--   - status: 'active' (eligible to be picked) | 'deactivated' (kill switch
--     without dropping the row).
--   - generated_at: timestamp + DESC index for "latest active" lookups.
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS engagement_styles_human_derived (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    example TEXT,
    best_in JSONB,
    note TEXT,
    source_window_start TIMESTAMPTZ NOT NULL,
    source_window_end TIMESTAMPTZ NOT NULL,
    source_post_ids INTEGER[] NOT NULL DEFAULT '{}',
    generation_log TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- "Latest active" lookup is the hot path: picker calls this every Twitter
-- reply attempt with ~5% probability.
CREATE INDEX IF NOT EXISTS idx_eshd_active_generated_at
  ON engagement_styles_human_derived (generated_at DESC)
  WHERE status = 'active';
