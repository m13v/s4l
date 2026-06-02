#!/usr/bin/env bash
# link-edit-github.sh — Edit existing GitHub issue comments to append a project link.
# Uses the gh CLI (no browser needed). GitHub has no upvote system, so eligibility
# is based on our comment being posted 6h+ ago (engagement = a reply in the issue thread).
# Called by launchd (com.m13v.social-link-edit-github) every 6 hours.

set -euo pipefail

# Cycle ID for cross-cycle cost accounting (see run-github.sh for the same
# pattern). Stamps claude_sessions.cycle_id via env inheritance.
BATCH_ID="${BATCH_ID:-legh-$(date +%Y%m%d-%H%M%S)}"
export BATCH_ID
export SA_CYCLE_ID="$BATCH_ID"

# Platform lock: wait up to 45min for any previous link-edit-github run, then skip
source "$(dirname "$0")/lock.sh"
acquire_lock "link-edit-github" 2700

# Load secrets
# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
SKILL_FILE="$REPO_DIR/SKILL.md"
LOG_DIR="$REPO_DIR/skill/logs"
# HTTP-only lane (2026-06-01): all reads/writes go through the s4l.ai API via
# scripts/link_edit_helper.py. No DATABASE_URL, no psql, no fallback.
LE_HELPER="$REPO_DIR/scripts/link_edit_helper.py"

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/link-edit-github-$(date +%Y-%m-%d_%H%M%S).log"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

RUN_START=$(date +%s)
log "=== GitHub Link Edit Run: $(date) ==="

# A/B gate: per-post deterministic coin flip for the page-gen lane. Mirrors
# scripts/twitter_gen_links.py's TWITTER_PAGE_GEN_RATE behavior and the
# Reddit link-edit pipeline's LINK_EDIT_REDDIT_PAGE_GEN_RATE. 0.30 means
# ~30% of eligible posts get a brand-new SEO landing page built inline via
# Claude + git push; the other ~70% fall through to the project's homepage
# with link_source='plain_url_ab_skip'. Per-post hash via Postgres
# hashtext() so the same post stays in the same lane across cron retries.
# Tunable via env var so cadence sweeps don't need code changes. 0.0
# disables page-gen entirely (link insertion still happens with plain URL);
# 1.0 restores 100% page-gen.
# DEFAULT 0.0: GitHub no longer generates custom SEO pages — every eligible
# post goes through the wrap-an-existing-link route (homepage + short link).
LINK_EDIT_GITHUB_PAGE_GEN_RATE="${LINK_EDIT_GITHUB_PAGE_GEN_RATE:-0.0}"
PAGE_GEN_RATE_PCT=$(python3 -c "v=float('$LINK_EDIT_GITHUB_PAGE_GEN_RATE'); v=max(0.0,min(1.0,v)); print(int(round(v*100)))")
log "A/B gate: LINK_EDIT_GITHUB_PAGE_GEN_RATE=$LINK_EDIT_GITHUB_PAGE_GEN_RATE (page_gen_lane='page_gen' on ~${PAGE_GEN_RATE_PCT}% of eligible posts; rest go to plain_url_ab_skip)"

EDITABLE=$(python3 "$LE_HELPER" eligible --platform github --page-gen-rate-pct "$PAGE_GEN_RATE_PCT" --order posted_at 2>/dev/null || echo "")

if [ "$EDITABLE" = "null" ] || [ -z "$EDITABLE" ]; then
    log "No GitHub posts eligible for link edit"
    python3 "$REPO_DIR/scripts/log_run.py" --script "link_edit_github" --posted 0 --skipped 0 --failed 0 --cost 0 --elapsed $(( $(date +%s) - RUN_START ))
    exit 0
fi

EDITABLE_COUNT=$(echo "$EDITABLE" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "?")
log "GitHub: $EDITABLE_COUNT posts eligible for link edit"

PROMPT_FILE=$(mktemp)
cat > "$PROMPT_FILE" <<PROMPT_EOF
You are the Social Autoposter GitHub link-edit bot.

Read $SKILL_FILE for the full workflow. Execute the GitHub link-edit phase only. GitHub edits are done via the gh CLI (no browser). GitHub has no upvote system; engagement = someone replied to our comment in the issue thread.

CRITICAL: This is a single-shot run. NEVER call ScheduleWakeup, CronCreate, CronDelete, CronList, EnterPlanMode, EnterWorktree, or any deferred-execution / scheduling tool. You MUST complete or skip every post in this one run; do not defer work to "a future run". If you hit a hard block, mark the post SKIPPED via step 9 and move on to the next post.

GitHub posts eligible for editing:
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
4. Write 1 sentence + project link (GitHub peer tone). Voice depends on the matched project's \`voice_relationship\` field in config.json (read it before drafting):
   - voice_relationship == "first_party": Claim ownership. Examples: "fwiw we built an implementation of this, URL" or "I shipped a tool that does this, URL". NEVER write "I found this", "there's a tool", "came across this implementation".
   - voice_relationship == "third_party": You are an outside observer pointing at the project's mechanism. Example: "fwiw PROJECT_NAME has an implementation of this, URL". Do NOT use "I built" / "we shipped" / "we made". Do NOT use "I found this" / "came across this" either; stay matter-of-fact.
5. URL-WRAP THE LINK TEXT for click attribution. Run:
     python3 ~/social-autoposter/scripts/dm_short_links.py wrap-post-text \\
       --text "YOUR_LINK_SENTENCE_WITH_URL" \\
       --platform github_issues \\
       --project PROJECT_NAME
   Parse the JSON output. Use \`text\` (URL replaced with /r/<code>) as the FINAL LINK_TEXT for steps 6 and 7. Keep \`minted_session\` for step 8. If wrap returns ok=false, log the error and skip this post (do NOT post a raw URL).
6. Extract OWNER/REPO from thread_url. Extract COMMENT_ID from our_url; if not directly available, use gh api to find our comment on that issue.
7. Edit the existing comment (append the wrapped LINK_TEXT to the existing content) using gh:
   gh api repos/OWNER/REPO/issues/comments/COMMENT_ID -X PATCH -f body="EXISTING_CONTENT

   WRAPPED_LINK_TEXT"
8. After each successful edit, update the DB (via the HTTP API helper; pass link_source so we can A/B compare seo_page vs plain_url_ab_skip vs plain_url_fallback:* vs plain_url_no_lp click-through rates, same as Twitter does in scripts/twitter_gen_links.py and the Reddit link-edit pipeline does) and backfill short-link attribution:
   python3 ~/social-autoposter/scripts/link_edit_helper.py mark-edited --post-id POST_ID --content "LINK_TEXT" --source "LINK_SOURCE"
   python3 ~/social-autoposter/scripts/dm_short_links.py backfill-post --minted-session MINTED_SESSION --post-id POST_ID
9. COMMITMENT GUARDRAILS (never violate these):
   - NEVER suggest, offer, or agree to calls, meetings, demos, or video chats.
   - NEVER promise to share links, files, or resources you don't have right now. Only share links from config.json projects (plus any new landing page you just deployed).
   - NEVER offer to DM or send anything outside the comment.
   - NEVER make time-bound promises.
10. If a post is SKIPPED (no project match, comment not found, issue locked, 404, bad URL), ALWAYS mark it so it won't be retried:
    python3 ~/social-autoposter/scripts/link_edit_helper.py mark-skipped --post-id POST_ID --reason "REASON"
PROMPT_EOF

gtimeout 1800 "$REPO_DIR/scripts/run_claude.sh" "link-edit-github" --strict-mcp-config --mcp-config "$HOME/.claude/browser-agent-configs/no-agents-mcp.json" --disallowed-tools "ScheduleWakeup,CronCreate,CronDelete,CronList,EnterPlanMode,EnterWorktree" --output-format stream-json --verbose -p "$(cat "$PROMPT_FILE")" 2>&1 | tee -a "$LOG_FILE" || log "WARNING: GitHub link-edit claude exited with code $?"
rm -f "$PROMPT_FILE"

EDITED=$(python3 "$LE_HELPER" edited-count --platform github 2>/dev/null || echo "0")
log "GitHub link-edit complete. Total github posts edited (all-time): $EDITED"

RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
_COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START" --scripts "link-edit-github" 2>/dev/null || echo "0.0000")
python3 "$REPO_DIR/scripts/log_run.py" --script "link_edit_github" --posted 0 --skipped 0 --failed 0 --cost "$_COST" --elapsed "$RUN_ELAPSED"

find "$LOG_DIR" -name "link-edit-github-*.log" -mtime +7 -delete 2>/dev/null || true

log "=== GitHub link-edit complete: $(date) ==="
