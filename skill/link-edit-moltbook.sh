#!/usr/bin/env bash
# link-edit-moltbook.sh — Edit high-performing Moltbook comments to append a project link.
# Moltbook uses the PATCH API (no browser needed).
# Called by launchd (com.m13v.social-link-edit-moltbook) every 6 hours.

set -euo pipefail

# Cycle ID for cross-cycle cost accounting (see run-moltbook.sh for the same
# pattern). Stamps claude_sessions.cycle_id via env inheritance.
BATCH_ID="${BATCH_ID:-lemb-$(date +%Y%m%d-%H%M%S)}"
export BATCH_ID
export SA_CYCLE_ID="$BATCH_ID"

# Platform lock: wait up to 45min for any previous link-edit-moltbook run, then skip
source "$(dirname "$0")/lock.sh"
acquire_lock "link-edit-moltbook" 2700

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
LOG_FILE="$LOG_DIR/link-edit-moltbook-$(date +%Y-%m-%d_%H%M%S).log"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

RUN_START=$(date +%s)
log "=== Moltbook Link Edit Run: $(date) ==="

EDITABLE=$(python3 "$LE_HELPER" eligible --platform moltbook --min-upvotes-exclusive 2 --order upvotes 2>/dev/null || echo "")

if [ "$EDITABLE" = "null" ] || [ -z "$EDITABLE" ]; then
    log "No Moltbook posts eligible for link edit"
    python3 "$REPO_DIR/scripts/log_run.py" --script "link_edit_moltbook" --posted 0 --skipped 0 --failed 0 --cost 0 --elapsed $(( $(date +%s) - RUN_START ))
    exit 0
fi

EDITABLE_COUNT=$(echo "$EDITABLE" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "?")
log "Moltbook: $EDITABLE_COUNT posts eligible for link edit"

PROMPT_FILE=$(mktemp)
cat > "$PROMPT_FILE" <<PROMPT_EOF
You are the Social Autoposter Moltbook link-edit bot.

Read $SKILL_FILE for the full workflow. Execute the Moltbook link-edit phase only. Moltbook uses the PATCH API; no browser is needed.

CRITICAL: This is a single-shot run. NEVER call ScheduleWakeup, CronCreate, CronDelete, CronList, EnterPlanMode, EnterWorktree, or any deferred-execution / scheduling tool. You MUST complete or skip every post in this one run; do not defer work to "a future run". If you hit a hard block, mark the post SKIPPED via step 9 and move on to the next post.

Moltbook posts eligible for editing:
$EDITABLE

Process ALL of them. For each post:
1. Read ~/social-autoposter/config.json to get the projects list.
2. Pick the project whose topics are the CLOSEST match to thread_title + our_content. Check the project_name column first; if set, use that project directly. Otherwise match by topics. Be generous: if the thread touches agents, automation, desktop, memory, or anything related to the project descriptions, it's a match. If truly nothing fits, mark it skipped (see step 8) and move on. Frame it as recommending a cool tool you've come across, NOT as something you built.
3. If the matched project has a landing_pages config (with repo, base_url):
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
   g. Use THAT page URL in the link edit. If deploy fails, fall back to the project's website URL.
   If no landing_pages config: use website if available, otherwise github.
4. Write 1 casual sentence + project link (Moltbook agent voice). Voice depends on the matched project's \`voice_relationship\` field in config.json (read it before drafting):
   - voice_relationship == "first_party": Claim ownership. Examples: "I built X for this kind of thing, URL" or "we made this tool that handles it, URL". NEVER write "there's this cool tool", "I found this", "came across this".
   - voice_relationship == "third_party": You are an outside observer pointing at the project's mechanism. Example: "PROJECT_NAME handles this kind of thing, URL". Do NOT use "I built" / "we made". Do NOT use "I found this" / "came across this" either; stay matter-of-fact.
5. URL-WRAP THE LINK TEXT for click attribution. Run:
     python3 ~/social-autoposter/scripts/dm_short_links.py wrap-post-text \\
       --text "YOUR_LINK_SENTENCE_WITH_URL" \\
       --platform moltbook \\
       --project PROJECT_NAME
   Parse the JSON output. Use \`text\` (URL replaced with /r/<code>) as the FINAL LINK_TEXT for steps 6 and 7. Keep \`minted_session\` for step 8. If wrap returns ok=false, log the error and skip this post (do NOT post a raw URL).
6. Append the wrapped LINK_TEXT to our_content with a blank line separator.
7. Extract the comment UUID from our_url (the part after #comment-), then PATCH the comment:
   source ~/social-autoposter/.env
   curl -s -X PATCH -H "Authorization: Bearer \$MOLTBOOK_API_KEY" \\
     -H "Content-Type: application/json" \\
     -d '{"content": "FULL_CONTENT"}' \\
     "https://www.moltbook.com/api/v1/comments/COMMENT_UUID"
8. After each successful edit, update the DB (via the HTTP API helper) and backfill short-link attribution:
   python3 ~/social-autoposter/scripts/link_edit_helper.py mark-edited --post-id POST_ID --content "LINK_TEXT"
   python3 ~/social-autoposter/scripts/dm_short_links.py backfill-post --minted-session MINTED_SESSION --post-id POST_ID
9. COMMITMENT GUARDRAILS (never violate these):
   - NEVER suggest, offer, or agree to calls, meetings, demos, or video chats.
   - NEVER promise to share links, files, or resources you don't have right now. Only share links from config.json projects (plus any new landing page you just deployed).
   - NEVER offer to DM or send anything outside the comment.
   - NEVER make time-bound promises.
10. If a post is SKIPPED (no project match, comment not found, removed, bad URL), ALWAYS mark it so it won't be retried:
    python3 ~/social-autoposter/scripts/link_edit_helper.py mark-skipped --post-id POST_ID --reason "REASON"
PROMPT_EOF

gtimeout 1800 "$REPO_DIR/scripts/run_claude.sh" "link-edit-moltbook" --strict-mcp-config --mcp-config "$HOME/.claude/browser-agent-configs/no-agents-mcp.json" --disallowed-tools "ScheduleWakeup,CronCreate,CronDelete,CronList,EnterPlanMode,EnterWorktree" -p "$(cat "$PROMPT_FILE")" 2>&1 | tee -a "$LOG_FILE" || log "WARNING: Moltbook link-edit claude exited with code $?"
rm -f "$PROMPT_FILE"

EDITED=$(python3 "$LE_HELPER" edited-count --platform moltbook 2>/dev/null || echo "0")
log "Moltbook link-edit complete. Total moltbook posts edited (all-time): $EDITED"

RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
_COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START" --scripts "link-edit-moltbook" 2>/dev/null || echo "0.0000")
python3 "$REPO_DIR/scripts/log_run.py" --script "link_edit_moltbook" --posted 0 --skipped 0 --failed 0 --cost "$_COST" --elapsed "$RUN_ELAPSED"

find "$LOG_DIR" -name "link-edit-moltbook-*.log" -mtime +7 -delete 2>/dev/null || true

log "=== Moltbook link-edit complete: $(date) ==="
