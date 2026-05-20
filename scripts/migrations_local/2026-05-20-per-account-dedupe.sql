-- Per-account dedupe scoping migration (2026-05-20)
--
-- Adds our_account to candidate / replies / dms tables (posts already has it),
-- normalizes inconsistent existing values in posts.our_account, backfills the
-- new columns with the platform-canonical handle (since only Matthew's Mac has
-- ever posted), and creates indexes the new dedupe queries depend on.
--
-- Canonical handles per platform (config.json `accounts.*`):
--   twitter   = m13v_              (no leading @)
--   reddit    = Deep_Ad1959        (no u/ prefix)
--   linkedin  = Matthew Diakonov   (display name)
--   github    = m13v
--   moltbook  = matthew-autoposter
--
-- Rows on the mk0r VM (reddit our_account = Sea_Comparison_1799 / StreetRefuse7512)
-- are preserved as-is. Test/other-author rows on Twitter (louis030195, pepe_quant_)
-- are also left alone.

BEGIN;

-- ---------- 1. Normalize posts.our_account ----------

-- Twitter: drop the leading @
UPDATE posts
   SET our_account = 'm13v_'
 WHERE platform = 'twitter'
   AND our_account = '@m13v_';

-- Reddit: strip u/ prefix
UPDATE posts
   SET our_account = 'Deep_Ad1959'
 WHERE platform = 'reddit'
   AND our_account = 'u/Deep_Ad1959';

-- Reddit: blank/null + the mis-tagged 'm13v' all roll into the real handle.
-- (Matthew's Reddit username is Deep_Ad1959; m13v is the github handle.)
UPDATE posts
   SET our_account = 'Deep_Ad1959'
 WHERE platform = 'reddit'
   AND (our_account IS NULL OR our_account = '' OR our_account = 'm13v');

-- LinkedIn: fold all four legacy variants into the canonical display name.
-- This is safe because every LinkedIn post in the table is from one human.
UPDATE posts
   SET our_account = 'Matthew Diakonov'
 WHERE platform = 'linkedin'
   AND our_account IN ('m13v',
                        'matthew-diakonov',
                        'linkedin:matthew-diakonov',
                        'matthew-autoposter');

-- ---------- 2. Add our_account columns ----------

ALTER TABLE reddit_candidates    ADD COLUMN IF NOT EXISTS our_account TEXT;
ALTER TABLE linkedin_candidates  ADD COLUMN IF NOT EXISTS our_account TEXT;
ALTER TABLE replies              ADD COLUMN IF NOT EXISTS our_account TEXT;
ALTER TABLE dms                  ADD COLUMN IF NOT EXISTS our_account TEXT;
ALTER TABLE human_dm_replies     ADD COLUMN IF NOT EXISTS our_account TEXT;

-- ---------- 3. Backfill the new columns ----------

-- Candidate tables: only one account has ever discovered candidates so far.
UPDATE reddit_candidates
   SET our_account = 'Deep_Ad1959'
 WHERE our_account IS NULL;

UPDATE linkedin_candidates
   SET our_account = 'Matthew Diakonov'
 WHERE our_account IS NULL;

-- Replies + DMs: pick per-row canonical from platform.
UPDATE replies
   SET our_account = CASE platform
     WHEN 'twitter'  THEN 'm13v_'
     WHEN 'x'        THEN 'm13v_'
     WHEN 'reddit'   THEN 'Deep_Ad1959'
     WHEN 'linkedin' THEN 'Matthew Diakonov'
     WHEN 'github'   THEN 'm13v'
     WHEN 'moltbook' THEN 'matthew-autoposter'
   END
 WHERE our_account IS NULL;

UPDATE dms
   SET our_account = CASE platform
     WHEN 'x'        THEN 'm13v_'
     WHEN 'twitter'  THEN 'm13v_'
     WHEN 'reddit'   THEN 'Deep_Ad1959'
     WHEN 'linkedin' THEN 'Matthew Diakonov'
   END
 WHERE our_account IS NULL;

-- human_dm_replies inherits via dm_id FK; leave NULL for now and let new
-- writes populate it. Reads can JOIN dms.our_account when needed.

-- ---------- 4. Indexes that the new dedupe filter relies on ----------

CREATE INDEX IF NOT EXISTS idx_posts_platform_account_thread
  ON posts (platform, our_account, thread_url)
  WHERE thread_url IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_replies_platform_account
  ON replies (platform, our_account);

CREATE INDEX IF NOT EXISTS idx_dms_platform_account
  ON dms (platform, our_account);

CREATE INDEX IF NOT EXISTS idx_reddit_candidates_account
  ON reddit_candidates (our_account);

CREATE INDEX IF NOT EXISTS idx_linkedin_candidates_account
  ON linkedin_candidates (our_account);

-- twitter_candidates already has our_account; ensure dedupe index exists.
CREATE INDEX IF NOT EXISTS idx_twitter_candidates_account
  ON twitter_candidates (our_account);

COMMIT;
