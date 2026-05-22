-- Consolidate engagement_styles_human_derived rows into engagement_styles_registry
-- so the picker, registry route, and daily synthesizer all read/write ONE table.
--
-- Why: the two-table split was wrong. The registry already carries every other
-- style (seed + model_invented + IG project-gated). Keeping human-derived
-- siloed in its own table forced engagement_styles.py to merge two sources
-- and forced generate_daily_human_style.py to write directly to the DB,
-- bypassing the "DB only via API route" rule for the website surface.
--
-- After this migration:
--   - engagement_styles_registry gains kind / platform / source_window_*
--     / source_post_ids / generation_log / generated_at columns.
--   - kind values: 'seed' (legacy, all existing rows), 'model_invented'
--     (set by the orchestrator when register_style fires), 'human_derived'
--     (set by generate_daily_human_style.py once it POSTs through the API).
--   - The 2 existing engagement_styles_human_derived rows
--     (reactive_one_beat, specific_pain_callout) move into registry with
--     kind='human_derived' and platform='twitter'.
--   - engagement_styles_human_derived is dropped.

BEGIN;

ALTER TABLE engagement_styles_registry
  ADD COLUMN IF NOT EXISTS kind                  TEXT NOT NULL DEFAULT 'seed',
  ADD COLUMN IF NOT EXISTS platform              TEXT,
  ADD COLUMN IF NOT EXISTS source_window_start   TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS source_window_end     TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS source_post_ids       INTEGER[],
  ADD COLUMN IF NOT EXISTS generation_log        TEXT,
  ADD COLUMN IF NOT EXISTS generated_at          TIMESTAMPTZ;

-- kind discriminator: allow 'seed', 'model_invented', 'human_derived'.
-- Existing rows stay as 'seed' (default). Future rows inserted by
-- register_style() with invented_by_model NOT NULL will get 'model_invented'
-- stamped by the API route (see route.ts after this migration).
ALTER TABLE engagement_styles_registry
  DROP CONSTRAINT IF EXISTS engagement_styles_registry_kind_chk;
ALTER TABLE engagement_styles_registry
  ADD CONSTRAINT engagement_styles_registry_kind_chk
  CHECK (kind IN ('seed', 'model_invented', 'human_derived'));

-- Fast lookup for "newest active human-derived style on platform X" — the
-- picker's hot path on every reply/post when the human-derived branch fires.
CREATE INDEX IF NOT EXISTS idx_registry_active_human_platform_generated
  ON engagement_styles_registry (platform, generated_at DESC)
  WHERE status = 'active' AND kind = 'human_derived';

-- Backfill existing rows: anything with invented_by_model set is a
-- model-invented row, otherwise treat as a curated seed.
UPDATE engagement_styles_registry
   SET kind = CASE
     WHEN invented_by_model IS NOT NULL AND invented_by_model <> 'unknown_legacy'
       THEN 'model_invented'
     ELSE 'seed'
   END
 WHERE kind = 'seed';

-- Migrate the two human-derived rows into the registry. Mark them as
-- 'human_derived' / platform='twitter' (the daily synthesizer is currently
-- Twitter-only; the cross-platform fan-out lands in the same migration day).
-- ON CONFLICT (name) DO NOTHING keeps the registry safe if a seed row ever
-- collides with a synthesizer name.
INSERT INTO engagement_styles_registry (
  name, description, example, note, best_in, status,
  why_existing_didnt_fit, first_post_url, first_post_id,
  first_post_platform, invented_by_model, invented_at, promoted_at,
  created_at, updated_at,
  kind, platform, source_window_start, source_window_end,
  source_post_ids, generation_log, generated_at
)
SELECT
  name, description, example, COALESCE(note, ''),
  COALESCE(best_in, '{}'::jsonb), status,
  NULL                              AS why_existing_didnt_fit,
  NULL                              AS first_post_url,
  NULL                              AS first_post_id,
  'twitter'                         AS first_post_platform,
  'daily-human-style-synthesizer'   AS invented_by_model,
  generated_at                      AS invented_at,
  generated_at                      AS promoted_at,
  generated_at                      AS created_at,
  generated_at                      AS updated_at,
  'human_derived'                   AS kind,
  'twitter'                         AS platform,
  source_window_start,
  source_window_end,
  source_post_ids,
  generation_log,
  generated_at
FROM engagement_styles_human_derived
WHERE status = 'active'
ON CONFLICT (name) DO NOTHING;

-- Drop the sidecar table and its index. After this commits, every reader
-- (picker, route, dashboard) points at engagement_styles_registry.
DROP INDEX IF EXISTS idx_eshd_active_generated_at;
DROP TABLE IF EXISTS engagement_styles_human_derived;

COMMIT;
