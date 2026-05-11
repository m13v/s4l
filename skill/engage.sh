#!/usr/bin/env bash
# engage.sh — Reply engagement loop (Reddit + Moltbook only)
# Phase B: Claude drafts and posts replies to pending inbound replies (batched)
# Phase C: Cleanup + summary
# Reply discovery runs separately via engage-reddit.sh and run-scan-moltbook-replies.sh
# (launchd: com.m13v.social-engage-reddit, com.m13v.social-scan-moltbook-replies).
# LinkedIn and Twitter engagement live in engage-linkedin.sh / engage-twitter.sh.
# Link-editing (formerly Phase D) is now split per-platform:
#   skill/link-edit-{reddit,moltbook,linkedin,github}.sh
# Outbound DM outreach (formerly Phase E) is now split per-platform:
#   skill/dm-outreach-{reddit,linkedin,twitter}.sh
# Called by launchd every 4 hours.

set -euo pipefail

# Load secrets
# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

# Cycle ID for cross-cycle cost accounting. Every claude session spawned in this
# script (directly or via subprocess) inherits SA_CYCLE_ID via env, and
# log_claude_session.py stamps it onto claude_sessions.cycle_id. Lets
# get_run_cost.py --cycle-id return THIS cycle's spend instead of summing every
# concurrent engage cycle in the same time window.
BATCH_ID="${BATCH_ID:-engmix-$(date +%Y%m%d-%H%M%S)}"
export BATCH_ID
export SA_CYCLE_ID="$BATCH_ID"

# Portable platform helpers (gtimeout, stat_mtime, platform_notify)
# shellcheck source=/dev/null
source "$(dirname "${BASH_SOURCE[0]}")/lib/platform.sh"

REPO_DIR="$HOME/social-autoposter"
SKILL_FILE="$REPO_DIR/SKILL.md"
LOG_DIR="$REPO_DIR/skill/logs"
BATCH_SIZE=200

if [ -z "${DATABASE_URL:-}" ]; then
    echo "ERROR: DATABASE_URL not set in ~/social-autoposter/.env"
    exit 1
fi

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/engage-$(date +%Y-%m-%d_%H%M%S).log"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

log "=== Engagement Loop Run: $(date) ==="

# Reply discovery (scan_reddit_replies.py, scan_moltbook_replies.py) now runs on its own
# launchd schedules (com.m13v.social-engage-reddit, com.m13v.social-scan-moltbook-replies,
# both twice/day). engage.sh only processes replies already written to the DB. This removes
# contention on the Reddit rate limit.

# Phase D (link editing) is now handled by per-platform scripts:
#   skill/link-edit-reddit.sh, link-edit-moltbook.sh, link-edit-linkedin.sh, link-edit-github.sh
# Each runs on its own launchd schedule so a single-platform failure cannot block the others.

# ═══════════════════════════════════════════════════════
# PHASE B: Reddit + Moltbook reply engagement
# Process in batches of $BATCH_SIZE to avoid prompt size limits
# ═══════════════════════════════════════════════════════

# Reset any 'processing' items older than 2 hours back to 'pending'
# These are items the agent physically posted but crashed before marking 'replied'.
# The in-browser already-replied check (below) prevents re-posting duplicates.
RESET_COUNT=$(psql "$DATABASE_URL" -t -A -c "
    UPDATE replies SET status='pending'
    WHERE status='processing' AND processing_at < NOW() - INTERVAL '2 hours'
    RETURNING id;" | wc -l | tr -d ' ')
[ "$RESET_COUNT" -gt 0 ] && log "Phase B: Reset $RESET_COUNT stuck 'processing' items back to pending"

# Load exclusions from config for injection into Claude prompts
EXCLUDED_AUTHORS=$(python3 -c "import json; c=json.load(open('$REPO_DIR/config.json')); print(', '.join(c.get('exclusions',{}).get('authors',[])))" 2>/dev/null || echo "")
EXCLUDED_TWITTER=$(python3 -c "import json; c=json.load(open('$REPO_DIR/config.json')); print(', '.join(c.get('exclusions',{}).get('twitter_accounts',[])))" 2>/dev/null || echo "")
EXCLUDED_LINKEDIN=$(python3 -c "import json; c=json.load(open('$REPO_DIR/config.json')); print(', '.join(c.get('exclusions',{}).get('linkedin_profiles',[])))" 2>/dev/null || echo "")

# Generate engagement style and content rules from shared module
source "$REPO_DIR/skill/styles.sh"
STYLES_BLOCK=$(generate_styles_block reddit replying)

# Top performers feedback report (platform-wide) — fed to the engagement agent
# so replies learn from past post performance just like the posting pipelines do.
TOP_REPORT=$(python3 "$REPO_DIR/scripts/top_performers.py" --platform reddit 2>/dev/null || echo "(top performers report unavailable)")

BATCH_NUM=0

while true; do
    PENDING_COUNT=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM replies WHERE status='pending' AND platform NOT IN ('linkedin', 'x');")

    if [ "$PENDING_COUNT" -eq 0 ]; then
        log "Phase B: No pending replies remaining. Done!"
        break
    fi

    BATCH_NUM=$((BATCH_NUM + 1))
    BATCH_ACTUAL=$((PENDING_COUNT < BATCH_SIZE ? PENDING_COUNT : BATCH_SIZE))
    log "Phase B batch $BATCH_NUM: Processing $BATCH_ACTUAL of $PENDING_COUNT pending replies"

    PHASE_B_PROMPT=$(mktemp)
    PENDING_DATA=$(psql "$DATABASE_URL" -t -A -c "
        SELECT json_agg(q) FROM (
            SELECT r.id, r.platform, r.their_author,
                   r.their_content as their_content,
                   r.their_comment_url, r.their_comment_id, r.depth,
                   p.thread_title as thread_title,
                   p.thread_url, p.our_content as our_content, p.our_url,
                   CASE WHEN p.thread_url = p.our_url THEN 1 ELSE 0 END as is_our_original_post
            FROM replies r
            JOIN posts p ON r.post_id = p.id
            WHERE r.status='pending' AND r.platform NOT IN ('linkedin', 'x')
            ORDER BY
                CASE WHEN p.thread_url = p.our_url THEN 0 ELSE 1 END,
                r.discovered_at ASC
            LIMIT $BATCH_SIZE
        ) q;")

    # Write the header portion of the prompt
    cat > "$PHASE_B_PROMPT" <<PROMPT_HEADER
You are the Social Autoposter engagement bot.

Read $SKILL_FILE for the full workflow, content rules, and platform details.

EXCLUSIONS — do NOT engage with these accounts (skip and mark as 'skipped' with reason 'excluded_author'):
- Excluded authors: $EXCLUDED_AUTHORS
- Excluded Twitter accounts: $EXCLUDED_TWITTER
- Excluded LinkedIn profiles: $EXCLUDED_LINKEDIN

CRITICAL — Browser agent rule: Each platform MUST use its dedicated browser agent. NEVER use generic mcp__playwright-extension__*, mcp__isolated-browser__*, or mcp__macos-use__* tools.
- Reddit: mcp__reddit-agent__* tools (e.g. mcp__reddit-agent__browser_navigate)
- Twitter: mcp__twitter-agent__* tools (e.g. mcp__twitter-agent__browser_navigate)
- LinkedIn: mcp__linkedin-agent__* tools (e.g. mcp__linkedin-agent__browser_navigate)
Each agent has its own browser lock. Using the wrong agent bypasses the lock and causes session conflicts.
CRITICAL: If a browser agent tool call is blocked or times out, DO NOT fall back to any other browser tool (especially not macos-use). Wait 30 seconds and retry the same agent. Repeat up to 3 times.
CRITICAL: TECHNICAL FAILURES ARE NOT TERMINAL. If after retries the action still failed for any technical reason (browser blocked, MCP timeout, page rendering issue, reddit/moltbook unreachable, CDP_ERROR, no_response), DO NOT call reply_db.py skipped. Leave the row in 'processing' status and move on to the next pending item. The next engage run's start-of-script cleanup resets stuck 'processing' rows back to 'pending' and retries automatically.
CRITICAL: ONLY call reply_db.py skipped for content/policy reasons (e.g., light_acknowledgment, drive_by_self_promo, hostile_user, mod_removal, troll, off_topic, meta_callout_acknowledged, excluded_author, cross_pipeline_disengage). NEVER skip for technical browser/network failures: those must be retry-able.

PROMPT_HEADER

    # NOTE: LinkedIn and Twitter discovery+engagement are handled by separate dedicated scripts:
    # - engage-linkedin.sh (launchd: com.m13v.social-engage-linkedin, every 3h)
    # - engage-twitter.sh  (launchd: com.m13v.social-engage-twitter, every 3h)
    # This Phase B only handles Reddit replies.

    # Append the main reply processing section
    cat >> "$PHASE_B_PROMPT" <<PROMPT_BODY
## Respond to pending replies (batch $BATCH_NUM: $BATCH_ACTUAL of $PENDING_COUNT total)

### Priority order:
1. **Replies on our original posts** (is_our_original_post=1) — highest priority
2. **Direct questions** ("what tool", "how do you", "can you share")
3. **Everything else** — general engagement

### Tiered link strategy:
- **Tier 1 (default):** No link. Genuine engagement, expand topic.
- **Tier 2 (natural mention):** Conversation touches a topic matching a project in config. Recommend it casually as a tool you've come across.
- **Tier 3 (direct ask):** They ask for link/tool/source. Give it immediately.


## FEEDBACK FROM PAST PERFORMANCE (use this to write better replies):
$TOP_REPORT

$STYLES_BLOCK

### Commitment guardrails (applies to ALL comment replies):
- NEVER suggest, offer, or agree to calls, meetings, demos, or video chats.
- NEVER promise to share specific links, files, or resources you don't currently have. Only share links from config.json projects.
- NEVER offer to "DM you" or "send you" something unless you can deliver it right now in the reply.
- NEVER make time-bound promises ("I'll share it tomorrow", "will post it this week").
- If someone asks for a call/meeting/demo, just keep the conversation going in the thread. Don't commit to anything outside the comment thread.

Here are the $BATCH_ACTUAL replies to process:
$PENDING_DATA

CRITICAL: Process EVERY reply in this batch. For each: either post a response and mark as 'replied', OR mark as 'skipped' with a skip_reason (light acknowledgments, trolls, crypto spam, DM requests, not directed at us).

CRITICAL: For ALL database operations, use the reply_db.py helper (NOT raw psql):
  python3 $REPO_DIR/scripts/reply_db.py processing ID          # BEFORE browser action
  python3 $REPO_DIR/scripts/reply_db.py replied ID "reply text" [url] [engagement_style] [is_recommendation]   # AFTER posting. engagement_style is TONE (critic, storyteller, etc). Pass "1" for is_recommendation ONLY when the reply casually recommends a project (Tier 2/3); leave blank otherwise.
  python3 $REPO_DIR/scripts/reply_db.py skipped ID "reason"
  python3 $REPO_DIR/scripts/reply_db.py skip_batch '{"ids":[1,2,3],"reason":"..."}'
  python3 $REPO_DIR/scripts/reply_db.py status
NEVER use psql directly. reply_db.py is faster (persistent connection, no env sourcing).

MANDATORY reply flow for every item:
  Step 1: python3 reply_db.py processing ID      ← mark BEFORE touching browser
  Step 2: post reply via browser
  Step 3: python3 reply_db.py replied ID "text" [url] [engagement_style] [is_recommendation]   ← mark AFTER success. engagement_style is TONE (e.g. critic, storyteller). Pass is_recommendation="1" ONLY when you casually mentioned a project (Tier 2/3); leave blank otherwise. Tone and intent are independent.
If Step 3 fails, the item stays 'processing' and will be reset to 'pending' on the next run — safe to retry.

GitHub issues engagement is handled by a separate pipeline (github-engage.sh). Skip any github replies in this batch.
LinkedIn and Twitter engagement are handled by separate pipelines (engage-linkedin.sh, engage-twitter.sh). This batch contains ONLY Reddit and Moltbook replies.

For **reddit** — use the reddit-agent browser (mcp__reddit-agent__* tools) with this FAST posting method (browser_run_code):
1. First, pre-compose ALL reply texts before opening the browser. Decide skip/reply and draft text for every item.
2. For each reply: run python3 reply_db.py processing ID, then call mcp__reddit-agent__browser_navigate to their_comment_url.
3. Then use a SINGLE browser_run_code call with this exact Playwright pattern:
\`\`\`javascript
async (page) => {
  const OUR_USERNAME = 'Deep_Ad1959';
  const thing = await page.\$('#thing_t1_COMMENT_ID');
  if (!thing) return 'ERROR: comment not found';

  // Check if we already replied (handles crash-recovery re-runs)
  const existingReplies = await thing.\$\$('.child .comment');
  for (const reply of existingReplies) {
    const author = await reply.\$eval('.author', el => el.textContent).catch(() => '');
    if (author === OUR_USERNAME) return 'already_replied';
  }

  await thing.evaluate(el => {
    const btn = el.querySelector('.flat-list a[onclick*="reply"]');
    if (btn) btn.click();
  });
  await page.waitForSelector('#thing_t1_COMMENT_ID .usertext-edit textarea', { timeout: 3000 });
  const textarea = await thing.\$('.usertext-edit textarea');
  await textarea.fill(REPLY_TEXT_HERE);
  await thing.evaluate(el => {
    const btn = el.querySelector('.usertext-edit button.save, .usertext-edit .save');
    if (btn) btn.click();
  });
  await page.waitForTimeout(2000);
  const newComments = await thing.\$\$('.child .comment .bylink');
  return newComments.length > 0 ? await newComments[newComments.length - 1].getAttribute('href') : null;
}
\`\`\`
Replace COMMENT_ID with the Reddit comment ID (from their_comment_id, without t1_ prefix).
Replace REPLY_TEXT_HERE with a JS string literal of the reply text.
IMPORTANT: Use thing.evaluate() for clicks — do NOT use replyBtn.click() directly as it causes Playwright timeouts.
If the JS returns 'already_replied': call reply_db.py replied ID "" to clean up without posting again.
If the JS returns null (no permalink found): call reply_db.py replied ID "text" with no URL — do NOT store the string 'posted' or their_comment_url as the URL.
4. Update DB using reply_db.py (see CRITICAL section above).
5. Navigate directly to the next reply — no need to close tabs.

Do NOT use browser_snapshot, browser_click, or browser_type for Reddit replies. browser_run_code is 5x faster.
Do NOT extract permalinks from snapshots — use the JS return value or skip it.
Do NOT store 'posted' or their_comment_url as our_reply_url — store null/no URL if the permalink is unavailable.
CRITICAL: ALL Reddit browser calls MUST use mcp__reddit-agent__* tools (e.g. mcp__reddit-agent__browser_run_code, mcp__reddit-agent__browser_navigate). NEVER use generic mcp__playwright-extension__*, mcp__isolated-browser__*, or mcp__macos-use__* tools for Reddit.

After every 10 replies, run: python3 $REPO_DIR/scripts/reply_db.py status
PROMPT_BODY

    gtimeout 5400 "$REPO_DIR/scripts/run_claude.sh" "engage-reddit-phaseB" --strict-mcp-config --mcp-config "$HOME/.claude/browser-agent-configs/reddit-agent-mcp.json" -p "$(cat "$PHASE_B_PROMPT")" 2>&1 | tee -a "$LOG_FILE" || log "WARNING: Phase B batch $BATCH_NUM claude exited with code $?"
    rm -f "$PHASE_B_PROMPT"

    # Check if we actually made progress (avoid infinite loop)
    NEW_PENDING=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM replies WHERE status='pending' AND platform NOT IN ('linkedin', 'x');")
    if [ "$NEW_PENDING" -ge "$PENDING_COUNT" ]; then
        log "WARNING: No progress made in batch $BATCH_NUM ($PENDING_COUNT -> $NEW_PENDING). Stopping to avoid infinite loop."
        break
    fi
    log "Batch $BATCH_NUM complete: $PENDING_COUNT -> $NEW_PENDING pending"
done

# Reset any items left in 'processing' after subprocess exit (tech-failure
# retry path: agent leaves rows here on browser/MCP failure rather than
# calling reply_db.py skipped, so the next run picks them up automatically).
# Filter to platforms this script handles (not linkedin/x).
POST_RESET=$(psql "$DATABASE_URL" -t -A -c "
    WITH upd AS (
        UPDATE replies SET status='pending'
        WHERE status='processing' AND platform NOT IN ('linkedin', 'x')
        RETURNING id
    ) SELECT COUNT(*) FROM upd;")
[ "$POST_RESET" -gt 0 ] && log "Post-run: Reset $POST_RESET 'processing' Reddit/Moltbook items back to pending"

# Phase E (outbound DM outreach) is now handled by per-platform scripts:
#   skill/dm-outreach-reddit.sh, dm-outreach-linkedin.sh, dm-outreach-twitter.sh
# Each runs on its own launchd schedule so a single-platform failure cannot block the others.

# ═══════════════════════════════════════════════════════
# PHASE C: Cleanup
# ═══════════════════════════════════════════════════════
log "Phase C: Cleanup"

TOTAL_PENDING=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM replies WHERE status='pending';")
TOTAL_REPLIED=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM replies WHERE status='replied';")
TOTAL_SKIPPED=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM replies WHERE status='skipped';")
TOTAL_ERRORS=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM replies WHERE status='error';")

DM_PENDING=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM dms WHERE status='pending';" 2>/dev/null || echo "0")
DM_SENT=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM dms WHERE status='sent';" 2>/dev/null || echo "0")
DM_SKIPPED=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM dms WHERE status='skipped';" 2>/dev/null || echo "0")
DM_ERRORS=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM dms WHERE status='error';" 2>/dev/null || echo "0")

log "Replies summary: pending=$TOTAL_PENDING replied=$TOTAL_REPLIED skipped=$TOTAL_SKIPPED errors=$TOTAL_ERRORS"
log "DMs summary: pending=$DM_PENDING sent=$DM_SENT skipped=$DM_SKIPPED errors=$DM_ERRORS"

# Delete old logs
find "$LOG_DIR" -name "engage-*.log" -mtime +7 -delete 2>/dev/null || true

log "=== Engagement loop complete: $(date) ==="
