#!/usr/bin/env bash
# link-edit-linkedin.sh — Edit high-performing LinkedIn comments to append a project link.
# LinkedIn is edited via the linkedin-harness browser (three-dot menu -> Edit).
# LinkedIn posts are eligible regardless of upvotes because upvote tracking is unreliable there.
# Called by launchd (com.m13v.social-link-edit-linkedin) every 6 hours.

set -euo pipefail

# LinkedIn killswitch (2026-05-27): refuse to run if a prior fire detected
# session compromise (http_999, authwall, throttle, li_at cleared).
# State: ~/.claude/social-autoposter/linkedin.killswitch
# Clear: python3 ~/social-autoposter/scripts/linkedin_killswitch.py clear
if [ -f "$HOME/.claude/social-autoposter/linkedin.killswitch" ]; then
    echo "[$(date +%H:%M:%S)] LINKEDIN_KILLSWITCH active. Aborting LinkedIn pipeline."
    echo "  Re-auth LinkedIn in harness Chrome, then: python3 ~/social-autoposter/scripts/linkedin_killswitch.py clear"
    exit 0
fi

# Cycle ID for cross-cycle cost accounting (see run-linkedin.sh for the same
# pattern). Stamps claude_sessions.cycle_id via env inheritance.
BATCH_ID="${BATCH_ID:-leli-$(date +%Y%m%d-%H%M%S)}"
export BATCH_ID
export SA_CYCLE_ID="$BATCH_ID"

# Browser-profile lock first (shared with other linkedin pipelines), then pipeline lock.
source "$(dirname "$0")/lock.sh"
# Browser backend bootstrap (linkedin-harness). Sets MCP_CONFIG_FILE,
# BROWSER_INSTRUCTIONS, exports LINKEDIN_CDP_URL, and provides
# ensure_linkedin_browser_for_backend. Migrated off the deprecated
# mcp__linkedin-agent Playwright MCP to the CDP-driven harness Chrome (port 9556).
source "$(dirname "$0")/lib/linkedin-backend.sh"
acquire_lock "linkedin-browser" 3600
ensure_linkedin_browser_for_backend
acquire_lock "link-edit-linkedin" 5400

# Load secrets
# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
SKILL_FILE="$REPO_DIR/SKILL.md"
LOG_DIR="$REPO_DIR/skill/logs"

if [ -z "${DATABASE_URL:-}" ]; then
    echo "ERROR: DATABASE_URL not set in ~/social-autoposter/.env"
    exit 1
fi

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/link-edit-linkedin-$(date +%Y-%m-%d_%H%M%S).log"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

RUN_START=$(date +%s)
log "=== LinkedIn Link Edit Run: $(date) ==="

# A/B gate: per-post deterministic coin flip for the page-gen lane. Mirrors
# scripts/twitter_gen_links.py's TWITTER_PAGE_GEN_RATE behavior and the
# Reddit link-edit pipeline's LINK_EDIT_REDDIT_PAGE_GEN_RATE. Default is now
# 0.0 (2026-05-29 policy: stop generating new SEO landing pages from link-edit;
# every eligible post falls through to the project's homepage with
# link_source='plain_url_ab_skip' and still gets the /r/<code> short-link wrap
# for attribution). Per-post hash via Postgres hashtext() so the same post
# stays in the same lane across cron retries. Tunable via env var so cadence
# sweeps don't need code changes. 0.0 disables page-gen entirely (link
# insertion still happens with plain URL); 1.0 restores 100% page-gen.
LINK_EDIT_LINKEDIN_PAGE_GEN_RATE="${LINK_EDIT_LINKEDIN_PAGE_GEN_RATE:-0.0}"
PAGE_GEN_RATE_PCT=$(python3 -c "v=float('$LINK_EDIT_LINKEDIN_PAGE_GEN_RATE'); v=max(0.0,min(1.0,v)); print(int(round(v*100)))")
log "A/B gate: LINK_EDIT_LINKEDIN_PAGE_GEN_RATE=$LINK_EDIT_LINKEDIN_PAGE_GEN_RATE (page_gen_lane='page_gen' on ~${PAGE_GEN_RATE_PCT}% of eligible posts; rest go to plain_url_ab_skip)"

EDITABLE=$(psql "$DATABASE_URL" -t -A -c "
    SELECT json_agg(q) FROM (
        SELECT id, platform, our_url, our_content, thread_title, upvotes, project_name,
               CASE WHEN ((hashtext(id::text) % 100) + 100) % 100 < ${PAGE_GEN_RATE_PCT}
                    THEN 'page_gen' ELSE 'ab_skip' END AS page_gen_lane
        FROM posts
        WHERE status='active'
          AND platform='linkedin'
          AND posted_at < NOW() - INTERVAL '6 hours'
          AND link_edited_at IS NULL
          AND our_url IS NOT NULL
        ORDER BY upvotes DESC NULLS LAST
    ) q;" 2>/dev/null || echo "")

if [ "$EDITABLE" = "null" ] || [ -z "$EDITABLE" ]; then
    log "No LinkedIn posts eligible for link edit"
    python3 "$REPO_DIR/scripts/log_run.py" --script "link_edit_linkedin" --posted 0 --skipped 0 --failed 0 --cost 0 --elapsed $(( $(date +%s) - RUN_START ))
    exit 0
fi

EDITABLE_COUNT=$(echo "$EDITABLE" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "?")
log "LinkedIn: $EDITABLE_COUNT posts eligible for link edit"

PROMPT_FILE=$(mktemp)
cat > "$PROMPT_FILE" <<PROMPT_EOF
You are the Social Autoposter LinkedIn link-edit bot.

$BROWSER_INSTRUCTIONS

Read $SKILL_FILE for the full workflow. Execute the LinkedIn link-edit phase only.

CRITICAL: ALL browser calls MUST use the mcp__linkedin-harness__bh_run tool (the BROWSER BACKEND block above; follow its translation table for any Playwright-style step). NEVER use generic mcp__playwright-extension__*, mcp__isolated-browser__*, or mcp__macos-use__* tools. If a bh_run call is blocked or times out, wait 30s and retry (up to 3 times). If still blocked, skip that post.

CRITICAL: This is a single-shot run. NEVER call ScheduleWakeup, CronCreate, CronDelete, CronList, EnterPlanMode, EnterWorktree, or any deferred-execution / scheduling tool. You MUST complete or skip every post in this one run; do not defer work to "a future run". If you hit a hard block, mark the post SKIPPED via step 9 and move on to the next post.

LinkedIn posts eligible for editing:
$EDITABLE

Process ALL of them. For each post:
1. Read ~/social-autoposter/config.json to get the projects list.
2. Pick the project whose topics are the CLOSEST match to thread_title + our_content. Check the project_name column first; if set, use that project directly. Otherwise match by topics. Be generous: if the thread touches agents, automation, desktop, memory, or anything related to the project descriptions, it's a match. If truly nothing fits, mark it skipped (see step 9) and move on. Frame it as recommending a cool tool you've come across, NOT as something you built.
3. PAGE-GEN LANE GATE — read the post's \`page_gen_lane\` field (set deterministically by the pipeline; do NOT override).
   - If \`page_gen_lane == "ab_skip"\`: SKIP the full SEO page generation entirely. Set LINK_URL = the matched project's homepage from config.json (the \`website\` field) and LINK_SOURCE="plain_url_ab_skip". Continue to step 4. The /r/<code> short-link wrap in step 5 still mints attribution on the project's own domain, so we get click data for this lane to compare against seo_page lane CTR.
   - If \`page_gen_lane == "page_gen"\` AND the matched project has a landing_pages config: continue to step 3a below.
   - If \`page_gen_lane == "page_gen"\` BUT the matched project has NO landing_pages config: skip page-gen, set LINK_URL = project homepage (website if available, otherwise github), LINK_SOURCE="plain_url_no_lp", continue to step 4.

3a. If the matched project has a landing_pages config (with repo, base_url):
   a. Think about what SEO-optimized guide page would fit this specific thread naturally. Consider the thread's audience, their pain points, industry jargon, and what they'd actually find useful. The page should NOT feel like a landing page; it should feel like a genuine 1000-2000 word guide or resource.
   b. cd into the project repo (landing_pages.repo)
   c. Look at existing pages under src/app/t/ to understand the site's style, layout components (Navbar, Footer), and theme
   d. Create a NEW standalone page as src/app/t/{seo-friendly-slug}/page.tsx; this is a real Next.js page with its own Metadata export, not a JSON entry. Include:
      - Proper <Metadata> with title, description, openGraph, twitter tags
      - Reuse the site's Navbar and Footer components (import or inline them)
      - Use the CTAButton component from @/components/cta-button for ALL call-to-action buttons (it tracks clicks in PostHog automatically). Import: import { CTAButton } from "@/components/cta-button";
      - A full article-style page: hero headline, table of contents, 5-7 content sections, comparison tables with real numbers, bullet lists with specific data points, and a CTA section at the bottom
      - The content must be 1000-2000 words. Pull real context from the project's config (pricing, features, proof_points, competitive_positioning) and from web research to make it concrete and authoritative
      - Naturally mention the product as ONE solution among the options discussed; don't make the whole page a sales pitch
   e. git add the new page && git commit -m "Add guide: SHORT_DESCRIPTION" && git push
   f. Wait ~35s for Vercel deploy, then curl -sI {base_url}/t/{slug} to verify HTTP 200
   g. On success, set LINK_URL = the deployed page URL and LINK_SOURCE="seo_page". On deploy failure, fall back GRACEFULLY: set LINK_URL = the project's homepage from config.json (the \`website\` field), set LINK_SOURCE="plain_url_fallback:deploy_failed". Do NOT skip the post; continue to step 4.
4. Write 1 sentence + project link (LinkedIn professional tone, claim ownership): "I've been building something for this, URL" or "we shipped a tool that does this, URL". ALWAYS frame as our own creation. NEVER write "I found this", "there's a tool", "came across this guide". We are the authors. Say so.
5. URL-WRAP THE LINK TEXT for click attribution. Run:
     python3 ~/social-autoposter/scripts/dm_short_links.py wrap-post-text \\
       --text "YOUR_LINK_SENTENCE_WITH_URL" \\
       --platform linkedin \\
       --project PROJECT_NAME
   Parse the JSON output. Use \`text\` (URL replaced with /r/<code>) as the FINAL LINK_TEXT for steps 6 and 7. Keep \`minted_session\` for step 8. If wrap returns ok=false, log the error and skip this post (do NOT post a raw URL).
6. Append the wrapped LINK_TEXT to our_content with a blank line separator.
7. Navigate to the post URL via the bh_run browser (new_tab/goto_url + wait_for_load), find our comment, click the three-dot menu on it (click_at_xy), click "Edit", append the wrapped link text to the existing content (click the edit box then type_text), save, then verify with capture_screenshot + Read the PNG.
8. After each successful edit, update the DB (including link_source so we can A/B compare seo_page vs plain_url_ab_skip vs plain_url_fallback:* vs plain_url_no_lp click-through rates, same as Twitter does in scripts/twitter_gen_links.py and the Reddit link-edit pipeline does) and backfill short-link attribution:
   psql "\$DATABASE_URL" -c "UPDATE posts SET link_edited_at=NOW(), link_edit_content='LINK_TEXT', link_source='LINK_SOURCE' WHERE id=POST_ID"
   python3 ~/social-autoposter/scripts/dm_short_links.py backfill-post --minted-session MINTED_SESSION --post-id POST_ID
9. COMMITMENT GUARDRAILS (never violate these):
   - NEVER suggest, offer, or agree to calls, meetings, demos, or video chats.
   - NEVER promise to share links, files, or resources you don't have right now. Only share links from config.json projects (plus any new landing page you just deployed).
   - NEVER offer to DM or send anything outside the comment.
   - NEVER make time-bound promises.
10. If a post is SKIPPED (no project match, comment not found, removed, bad URL, session not logged in), ALWAYS mark it so it won't be retried:
    psql "\$DATABASE_URL" -c "UPDATE posts SET link_edited_at=NOW(), link_edit_content='SKIPPED: REASON' WHERE id=POST_ID"
PROMPT_EOF

ensure_linkedin_browser_for_backend 2>&1 | tee -a "$LOG_FILE"
gtimeout 5400 "$REPO_DIR/scripts/run_claude.sh" "link-edit-linkedin" --strict-mcp-config --mcp-config "$MCP_CONFIG_FILE" --disallowed-tools "ScheduleWakeup,CronCreate,CronDelete,CronList,EnterPlanMode,EnterWorktree" -p "$(cat "$PROMPT_FILE")" 2>&1 | tee -a "$LOG_FILE" || log "WARNING: LinkedIn link-edit claude exited with code $?"
rm -f "$PROMPT_FILE"

EDITED=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM posts WHERE platform='linkedin' AND link_edited_at IS NOT NULL;" 2>/dev/null || echo "0")
log "LinkedIn link-edit complete. Total linkedin posts edited (all-time): $EDITED"

RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
_COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START" --scripts "link-edit-linkedin" 2>/dev/null || echo "0.0000")
python3 "$REPO_DIR/scripts/log_run.py" --script "link_edit_linkedin" --posted 0 --skipped 0 --failed 0 --cost "$_COST" --elapsed "$RUN_ELAPSED"

find "$LOG_DIR" -name "link-edit-linkedin-*.log" -mtime +7 -delete 2>/dev/null || true

log "=== LinkedIn link-edit complete: $(date) ==="
