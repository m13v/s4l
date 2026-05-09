-- 2026-05-09_project_search_excludes.sql
--
-- Self-improving exclusion list. When Claude rejects a candidate as off-topic
-- in Phase 2b-prep, it can also emit `proposed_excludes`: a short list of
-- specific keywords that, if added as `-term` to future searches for that
-- project, would block this entire class of false-positive upstream.
--
-- Activation gate: a term must be proposed by >=2 distinct batches before it
-- is appended to live queries, so a single bad rejection cannot mute legit
-- searches. Decay: terms with no use in 60 days and <3 distinct batches are
-- pruned by a nightly job.
--
-- Per-keyword traceability via batch_ids[] and candidate_ids[] arrays so we
-- can audit "which tweets caused this exclusion".

CREATE TABLE IF NOT EXISTS project_search_excludes (
    platform           TEXT NOT NULL,
    project            TEXT NOT NULL,
    term               TEXT NOT NULL,
    proposals          INTEGER NOT NULL DEFAULT 1,
    batch_ids          TEXT[]  NOT NULL DEFAULT ARRAY[]::TEXT[],
    candidate_ids      INTEGER[] NOT NULL DEFAULT ARRAY[]::INTEGER[],
    sample_reason      TEXT,
    first_proposed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_proposed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at       TIMESTAMPTZ,
    PRIMARY KEY (platform, project, term)
);

CREATE INDEX IF NOT EXISTS idx_pse_active_lookup
    ON project_search_excludes (platform, project)
    WHERE array_length(batch_ids, 1) >= 2;
