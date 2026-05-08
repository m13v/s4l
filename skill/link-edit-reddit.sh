#!/usr/bin/env bash
# link-edit-reddit.sh — Edit high-performing Reddit comments to append a project link.
# Splits out from the legacy engage.sh Phase D so a single platform failure
# (e.g. LinkedIn hang) no longer blocks Reddit.
# Called by launchd (com.m13v.social-link-edit-reddit) every 6 hours.

set -euo pipefail

# Pipeline lock at top. The reddit-browser lock is acquired later, just
# before the Claude/MCP step that drives the browser, so peers can use the
# profile during our DB queries and prompt build.
source "$(dirname "$0")/lock.sh"
acquire_lock "link-edit-reddit" 5400

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
LOG_FILE="$LOG_DIR/link-edit-reddit-$(date +%Y-%m-%d_%H%M%S).log"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

RUN_START=$(date +%s)
log "=== Reddit Link Edit Run: $(date) ==="

EDITABLE=$(psql "$DATABASE_URL" -t -A -c "
    SELECT json_agg(q) FROM (
        SELECT id, platform, our_url, our_content, thread_title, upvotes, project_name
        FROM posts
        WHERE status='active'
          AND platform='reddit'
          AND posted_at < NOW() - INTERVAL '6 hours'
          AND link_edited_at IS NULL
          AND our_url IS NOT NULL
          AND upvotes > 1
        ORDER BY upvotes DESC NULLS LAST
    ) q;" 2>/dev/null || echo "")

if [ "$EDITABLE" = "null" ] || [ -z "$EDITABLE" ]; then
    log "No Reddit posts eligible for link edit"
    python3 "$REPO_DIR/scripts/log_run.py" --script "link_edit_reddit" --posted 0 --skipped 0 --failed 0 --cost 0 --elapsed $(( $(date +%s) - RUN_START ))
    exit 0
fi

EDITABLE_COUNT=$(echo "$EDITABLE" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "?")
log "Reddit: $EDITABLE_COUNT posts eligible for link edit"

PROMPT_FILE=$(mktemp)
cat > "$PROMPT_FILE" <<PROMPT_EOF
You are the Social Autoposter Reddit link-edit bot.

Read $SKILL_FILE for the full workflow. Execute the Reddit link-edit phase only.

CRITICAL: ALL browser calls MUST use mcp__reddit-agent__* tools (e.g. mcp__reddit-agent__browser_navigate, mcp__reddit-agent__browser_run_code). NEVER use generic mcp__playwright-extension__*, mcp__isolated-browser__*, or mcp__macos-use__* tools. If a reddit-agent call is blocked or times out, wait 30s and retry the same agent (up to 3 times). If still blocked, skip that post.

CRITICAL: This is a single-shot run. NEVER call ScheduleWakeup, CronCreate, CronDelete, CronList, EnterPlanMode, EnterWorktree, or any deferred-execution / scheduling tool. You MUST complete or skip every post in this one run; do not defer work to "a future run". If you hit a hard block, mark the post SKIPPED via step 9 and move on to the next post.

Reddit posts eligible for editing:
$EDITABLE

Process ALL of them. For each post:
1. Read ~/social-autoposter/config.json to get the projects list.
2. Pick the project whose topics are the CLOSEST match to thread_title + our_content.
   a. First check the project_name column. If it is set AND its topics/description fit the thread, use it.
   b. If project_name is set but CLEARLY does not fit the thread (e.g. Cyrano tagged to a law firm billing thread), treat it as a bad upstream tag and scan config.json for a project that DOES fit. If you find one, use that project instead and also run: psql "\$DATABASE_URL" -c "UPDATE posts SET project_name='BETTER_PROJECT' WHERE id=POST_ID" so the correction is persisted.
   c. If project_name is NOT set, match by topics. Be generous: if the thread touches agents, automation, desktop, memory, or anything related to the project descriptions, it's a match.
   d. ONLY if no project in config.json fits at all, mark it skipped (see step 9) and move on. Frame it as recommending a cool tool you've come across, NOT as something you built.
3. If the matched project has a landing_pages config (with repo, base_url), generate a fresh SEO page for this thread by delegating to the unified generator:
   a. Decide a SHORT keyword phrase (3-6 words) that captures what page would help this thread's audience. Think SEO intent, not headline copy. Examples: "local ai agent", "macos accessibility automation", "self hosted llm inference".
   b. Derive a URL slug from the keyword: lowercase, kebab-case, alphanumeric and hyphens only, max 50 chars. Examples: "local-ai-agent", "macos-accessibility-automation".
   c. Run the unified SEO page generator (it loads the @m13v/seo-components palette, picks content type, builds the page, commits, pushes, verifies the live URL, and writes the seo_keywords row that surfaces in the dashboard activity feed). Use the Bash tool:
        python3 ~/social-autoposter/seo/generate_page.py --product PROJECT_NAME --keyword "KEYWORD_PHRASE" --slug "url-slug" --trigger reddit
      This call can take 10-40 minutes per page (Cloud Run staging-then-tag deploys on mk0r are the slow end). The final stdout is a JSON object; parse it. On success it contains "success": true and "page_url": "https://...". On failure it contains "success": false and "error": "...".
   d. If success, set LINK_URL = the \`page_url\` from the JSON output and LINK_SOURCE="seo_page".
   e. If failure (success: false in the JSON), fall back GRACEFULLY (mirrors the Twitter pipeline behavior in scripts/twitter_gen_links.py): set LINK_URL = the project's homepage from config.json (the \`website\` field for the matched project) and set LINK_SOURCE="plain_url_fallback:<reason>" where <reason> is a SHORT snake_case tag derived from the JSON error string (preferred values: timeout, no_page_url, deploy_failed, build_failed, push_failed; otherwise pick a sensible 1-3 word snake_case summary). Do NOT skip the post; continue to step 4. The short-link wrap in step 5 will still mint a /r/<code> on the project's own domain, so click attribution works on the homepage URL too.
   If the matched project has NO landing_pages config at all (genuinely unconfigured, not a generation failure), skip the page-gen step entirely: set LINK_URL = the project's website URL from config.json and LINK_SOURCE="plain_url_no_lp".
4. Write 1 casual sentence ending with LINK_URL (from step 3.d, 3.e, or the no-LP fallback). ALWAYS frame as our own creation, never as a third-party tool we just discovered. We built / made / shipped this; we are not "finding" or "stumbling on" it.
   - For Reddit (first person, claim ownership): "fwiw I built a tool for exactly this, LINK_URL", "we made this for it, LINK_URL", "I shipped a small thing that does this, LINK_URL".
   - NEVER write: "I found this", "there's a tool", "came across this", "saw this manual", "found this guide". That phrasing pretends we are a neutral commenter pointing at someone else's project. We are the authors. Say so.
5. URL-WRAP THE LINK TEXT for click attribution. This MUST run for every LINK_SOURCE (seo_page, plain_url_fallback:*, plain_url_no_lp). The wrap helper accepts homepage URLs and mints a /r/<code> on the project's own domain. Run:
     python3 ~/social-autoposter/scripts/dm_short_links.py wrap-post-text \\
       --text "YOUR_LINK_SENTENCE_WITH_URL" \\
       --platform reddit \\
       --project PROJECT_NAME
   PROJECT_NAME must be the EXACT \`name\` field from config.json (case-sensitive; e.g. "fazm" lowercase, "Cyrano", "WhatsApp MCP"). Parse the JSON output. Use \`text\` (URL replaced with /r/<code>) as the FINAL LINK_TEXT for steps 6 and 7. Keep \`minted_session\` for step 8. If wrap returns ok=false, log the error and skip this post (do NOT post a raw URL).
6. Append the wrapped LINK_TEXT to our_content with a blank line separator.
7. Navigate to old.reddit.com comment permalink via the reddit-agent browser. Click "edit", append the wrapped link text to the existing content, save, verify.
8. After each successful edit, update the DB (including link_source so we can A/B compare seo_page vs plain_url_fallback:* vs plain_url_no_lp click-through rates, same as Twitter does in scripts/twitter_gen_links.py) and backfill short-link attribution:
   psql "\$DATABASE_URL" -c "UPDATE posts SET link_edited_at=NOW(), link_edit_content='LINK_TEXT', link_source='LINK_SOURCE' WHERE id=POST_ID"
   python3 ~/social-autoposter/scripts/dm_short_links.py backfill-post --minted-session MINTED_SESSION --post-id POST_ID
9. COMMITMENT GUARDRAILS (never violate these):
   - NEVER suggest, offer, or agree to calls, meetings, demos, or video chats.
   - NEVER promise to share links, files, or resources you don't have right now. Only share links from config.json projects (plus any new landing page you just deployed).
   - NEVER offer to DM or send anything outside the comment.
   - NEVER make time-bound promises.
10. If a post is SKIPPED (no project match, comment not found, removed by moderation, bad URL), ALWAYS mark it so it won't be retried:
    psql "\$DATABASE_URL" -c "UPDATE posts SET link_edited_at=NOW(), link_edit_content='SKIPPED: REASON' WHERE id=POST_ID"
PROMPT_EOF

# Acquire the browser lock now, immediately before the Claude/MCP step.
log "Acquiring reddit-browser lock for Claude/MCP step..."
acquire_lock "reddit-browser" 3600
ensure_browser_healthy "reddit"

gtimeout 5400 "$REPO_DIR/scripts/run_claude.sh" "link-edit-reddit" --strict-mcp-config --mcp-config "$HOME/.claude/browser-agent-configs/reddit-agent-mcp.json" --disallowed-tools "ScheduleWakeup,CronCreate,CronDelete,CronList,EnterPlanMode,EnterWorktree" -p "$(cat "$PROMPT_FILE")" 2>&1 | tee -a "$LOG_FILE" || log "WARNING: Reddit link-edit claude exited with code $?"
rm -f "$PROMPT_FILE"

EDITED=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM posts WHERE platform='reddit' AND link_edited_at IS NOT NULL;" 2>/dev/null || echo "0")
log "Reddit link-edit complete. Total reddit posts edited (all-time): $EDITED"

RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
_COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START" --scripts "link-edit-reddit" 2>/dev/null || echo "0.0000")
python3 "$REPO_DIR/scripts/log_run.py" --script "link_edit_reddit" --posted 0 --skipped 0 --failed 0 --cost "$_COST" --elapsed "$RUN_ELAPSED"

find "$LOG_DIR" -name "link-edit-reddit-*.log" -mtime +7 -delete 2>/dev/null || true

log "=== Reddit link-edit complete: $(date) ==="
