-- 2026-05-22_twitter_assigned_style.sql
--
-- Close the Twitter engagement-style enforcement gap.
--
-- Before this migration, the Twitter cycle picked an engagement style via
-- s4l_pick_style (USE mode 95% / INVENT mode 5%) and baked the assignment
-- into the prompt, but the post pipeline (twitter_post_plan.py) consumed
-- whatever engagement_style the model returned without coercion. Two
-- failure modes resulted:
--   (a) USE drift: model picks a different style than assigned, post is
--       logged with the drifted style, picker authority silently lost.
--   (b) INVENT lost: model invents a new style name + new_style block,
--       but neither the candidate row nor the post row carry the block,
--       so /api/v1/engagement-styles/registry never sees it. Registry
--       cannot grow from Twitter traffic.
--
-- These columns close both gaps. The Twitter cycle now writes the
-- picker's assignment into the candidate row at draft time, and
-- twitter_post_plan.py reads it back + calls validate_or_register so
-- USE drift is coerced and INVENT new_style blocks are POSTed to the
-- registry endpoint just like Reddit/GitHub/Moltbook do.
--
-- Columns:
--   assigned_style    TEXT  -- the style name the picker pinned in USE mode;
--                              NULL in INVENT mode.
--   assigned_mode     TEXT  -- 'use' or 'invent'. Drives validate_or_register
--                              behaviour at post time.
--   draft_new_style   JSONB -- the model's new_style block when INVENT fired
--                              and the model returned one. Shape:
--                              {description, example, why_existing_didnt_fit, note?}
--                              (matches _REQUIRED_NEW_STYLE_FIELDS in
--                              scripts/engagement_styles.py). NULL otherwise.
--
-- Idempotent: safe to re-run.

ALTER TABLE twitter_candidates
  ADD COLUMN IF NOT EXISTS assigned_style  TEXT,
  ADD COLUMN IF NOT EXISTS assigned_mode   TEXT,
  ADD COLUMN IF NOT EXISTS draft_new_style JSONB;
