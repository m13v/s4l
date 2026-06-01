#!/usr/bin/env bash
# engage-linkedin.sh — LinkedIn engagement loop
# Phase A: Discover replies/mentions from LinkedIn notifications (Claude-driven MCP).
# Phase B: Respond to pending LinkedIn replies (Claude-driven, OAuth API for posting).
# Called by launchd every 3 hours.
#
# IMPORTANT: all LinkedIn browser work goes through the linkedin-harness MCP
# (bh_run, CDP-driven real Chrome on port 9556), driven by Claude (the LLM).
# Do NOT re-introduce Python Playwright scrapers, Voyager API calls
# (/voyager/api/*), comment-page scroll+expand loops, or programmatic re-login
# flows. See CLAUDE.md "LinkedIn: flagged patterns to avoid" for why.

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

# 2026-05-01: lock policy changed from "hold the entire run" to "hold only
# while a Claude phase is actively driving the browser" — same pattern as
# run-linkedin.sh. The old policy held linkedin-browser for the whole 25-45min
# cycle, starving peer pipelines (run-linkedin, dm-replies-linkedin) and
# defeating launchd schedules. The browser is only used inside the two
# run_claude.sh invocations (Phase A discovery, Phase B reply). Everything
# between (DB cleanup, pending pull, top performers, styles) is pure DB/CPU.
source "$(dirname "$0")/lock.sh"
# Browser backend bootstrap (linkedin-harness). Sets MCP_CONFIG_FILE,
# BROWSER_INSTRUCTIONS, exports LINKEDIN_CDP_URL, and provides
# ensure_linkedin_browser_for_backend. Migrated off the deprecated
# linkedin-agent MCP on 2026-05-29 (mirrors the Twitter harness migration).
source "$(dirname "$0")/lib/linkedin-backend.sh"

# Load secrets
# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
SKILL_FILE="$REPO_DIR/SKILL.md"
LOG_DIR="$REPO_DIR/skill/logs"
BATCH_SIZE=500
MCP_CONFIG="$MCP_CONFIG_FILE"

# DB-free since 2026-06-01: all reply state goes through the s4l.ai HTTP API
# (X-Installation auth). No DATABASE_URL needed; the helpers below call the API.
PY_BIN="$(command -v python3 || echo /usr/bin/python3)"

# li_reply_count <status>  -> integer count of linkedin replies in that status.
# Backed by GET /api/v1/replies/counts (same aggregate reply_db.py status uses).
li_reply_count() {
    "$PY_BIN" -c "
import sys; sys.path.insert(0, '$REPO_DIR/scripts')
from http_api import api_get
resp = api_get('/api/v1/replies/counts', {'platform': 'linkedin'})
counts = ((resp or {}).get('data') or {}).get('counts') or []
want = '$1'
print(next((int(r.get('count', 0)) for r in counts if r.get('status') == want), 0))
" 2>/dev/null || echo 0
}

# li_reset_processing <older_than_hours>  -> count of rows reset to pending.
# older_than_hours=0 means no time gate (reset every processing row). Backed by
# POST /api/v1/replies/reset-stuck.
li_reset_processing() {
    "$PY_BIN" -c "
import sys; sys.path.insert(0, '$REPO_DIR/scripts')
from http_api import api_post
resp = api_post('/api/v1/replies/reset-stuck', {'platform': 'linkedin', 'older_than_hours': int('$1')})
print(((resp or {}).get('data') or {}).get('reset_count', 0))
" 2>/dev/null || echo 0
}

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/engage-linkedin-$(date +%Y-%m-%d_%H%M%S).log"

# Per-cycle batch id stamped onto every claude_sessions row spawned by this
# engagement run (via SA_CYCLE_ID env -> log_claude_session.py). 2026-05-10
# cycle_id rollout.
BATCH_ID="enli-$(date +%Y%m%d_%H%M%S)-$$"
export SA_CYCLE_ID="$BATCH_ID"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

RUN_START=$(date +%s)
log "=== LinkedIn Engagement Run: $(date) ==="

# Load exclusions from config
EXCLUDED_AUTHORS=$(python3 -c "import json; c=json.load(open('$REPO_DIR/config.json')); print(', '.join(c.get('exclusions',{}).get('authors',[])))" 2>/dev/null || echo "")
EXCLUDED_LINKEDIN=$(python3 -c "import json; c=json.load(open('$REPO_DIR/config.json')); print(', '.join(c.get('exclusions',{}).get('linkedin_profiles',[])))" 2>/dev/null || echo "")

# ═══════════════════════════════════════════════════════
# PHASE A: Discover new replies/mentions from LinkedIn notifications
# Claude-driven: LLM navigates linkedin-agent MCP to /notifications/, extracts
# actionable items from the notifications page DOM (NOT from Voyager API,
# NOT by opening each permalink).
# ═══════════════════════════════════════════════════════
log "Phase A: Scanning LinkedIn notifications (Claude-driven)..."

PHASE_A_PROMPT=$(mktemp)
cat > "$PHASE_A_PROMPT" <<PROMPT_EOF
You are the Social Autoposter LinkedIn discovery bot.

Read $SKILL_FILE for content rules (tone, anti-AI detection, no em dashes).

$BROWSER_INSTRUCTIONS

## Task: Discover new LinkedIn replies and mentions from the notifications page

CRITICAL - Browser agent rule: ONLY use the browser tool described in the BROWSER BACKEND block above (mcp__linkedin-harness__bh_run). NEVER use generic mcp__playwright-extension__*, mcp__isolated-browser__*, or mcp__macos-use__* tools.
CRITICAL: If a browser tool call is blocked or times out, wait 30 seconds and retry. Repeat up to 3 times. If still blocked, stop.
CRITICAL: Do NOT open individual post permalinks to fetch comment text. Everything we need is on the notifications page. Opening per-comment permalinks is a flagged scraping pattern.
CRITICAL: Do NOT call any /voyager/api/ endpoint, do NOT fetch() from the linkedin.com session. Use only UI navigation (the navigate/snapshot/run-code equivalents in the BROWSER BACKEND block).

EXCLUSIONS - do NOT engage with these accounts:
- Excluded authors: $EXCLUDED_AUTHORS
- Excluded LinkedIn profiles: $EXCLUDED_LINKEDIN
- Skip comments by "Matthew Diakonov" or "m13v" (our own account).

### Step 1: Load existing reply comment IDs (dedup)
\`\`\`bash
source ~/social-autoposter/.env
psql "\$DATABASE_URL" -t -A -c "SELECT their_comment_id FROM replies WHERE platform='linkedin';"
\`\`\`
Skip any notification whose comment URN is already in this list.

### Step 2: Load author+post pairs we already engaged with
\`\`\`bash
psql "\$DATABASE_URL" -t -A -c "SELECT DISTINCT r.their_author || '|||' || p.our_url FROM replies r JOIN posts p ON r.post_id = p.id WHERE r.platform='linkedin' AND r.status IN ('replied','pending','processing');"
\`\`\`
Skip any notification whose (author, post) pair is already here. One reply per author per thread.

### Step 3: Load our LinkedIn posts for matching
\`\`\`bash
psql "\$DATABASE_URL" -t -A -c "SELECT id, our_url FROM posts WHERE platform='linkedin' AND status='active';"
\`\`\`

### Step 4: Navigate to LinkedIn notifications and verify session
Navigate (per the BROWSER BACKEND block) to https://www.linkedin.com/notifications/

Take a snapshot/screenshot and verify the page is the notifications feed (not a login/checkpoint page). If you see login, captcha, or a verification challenge, STOP immediately and print: SESSION_INVALID — do not attempt to log in. Exit.

### Step 5: Load more notifications
Scroll the page down a few times to lazy-load notifications. If a "Show more results" button is visible, click it — up to 5 times total, with a pause of 2-3 seconds between clicks. Stop if the button disappears.

### Step 6: Extract actionable notifications from the notifications page DOM
Run this DOM-extraction JS via the BROWSER BACKEND block's run-code equivalent
(bh_run with js("""...""")). It is a single read of the already-loaded page — do
NOT navigate to any other URL. The js() helper runs the body in page context and
returns the result, so pass exactly this body (it ends with \`return\`):

\`\`\`javascript
const actionable = [];
const actionablePhrases = [
  'replied to your comment',
  'mentioned you in a comment',
  'mentioned you in this',
  'commented on your post',
  'commented on your update',
];

for (const article of document.querySelectorAll('article')) {
  const text = (article.innerText || '').toLowerCase();
  const matched = actionablePhrases.find(p => text.includes(p));
  if (!matched) continue;

  const strong = article.querySelector('strong');
  const author = strong ? strong.textContent.trim() : 'unknown';

  const link = article.querySelector('a[href*="commentUrn"]') ||
               article.querySelector('a[href*="replyUrn"]') ||
               article.querySelector('a[href*="feed/update"]');
  const href = link ? link.getAttribute('href') : null;
  if (!href) continue;

  // Extract activity ID and commentUrn from the href
  const activityMatch = href.match(/urn:li:activity:(\d+)/);
  const activityId = activityMatch ? activityMatch[1] : null;
  const commentUrnMatch = href.match(/commentUrn=([^&]+)/);
  const commentUrn = commentUrnMatch ? decodeURIComponent(commentUrnMatch[1]) : null;

  // Best-effort snippet: text inside the article, minus the author header
  const snippet = (article.innerText || '').replace(/\s+/g, ' ').trim();

  actionable.push({
    type: matched,
    author,
    href: href.startsWith('http') ? href : ('https://www.linkedin.com' + href),
    activity_id: activityId,
    comment_urn: commentUrn,
    snippet,
  });
}
return JSON.stringify(actionable);
\`\`\`

### Step 7: For each extracted notification, decide whether to insert
For each item:
- If comment_urn is null OR activity_id is null: skip (no_comment_urn)
- If comment_urn is in the Step 1 dedup list: skip (already_tracked)
- If author matches an excluded account or is our own: skip (excluded_author / own_account)
- Build author_post_key = author + '|||' + our_url-for-this-post. If this pair is in the Step 2 list: skip (author_already_engaged)
- Find matching post_id from Step 3 by activity_id in the our_url. If none: create one via INSERT (use PROJECT_NAME matched from config.json projects[].topics against the post topic, thread_url = https://www.linkedin.com/feed/update/urn:li:activity:\$ACTIVITY_ID/, our_url same).

Insert the reply:
\`\`\`bash
psql "\$DATABASE_URL" -c "INSERT INTO replies (post_id, platform, their_comment_id, their_author, their_content, their_comment_url, depth, status) VALUES (POST_ID, 'linkedin', 'COMMENT_URN', 'AUTHOR', 'SNIPPET', 'HREF', 1, 'pending');"
\`\`\`

### Step 8: Summary
Print:
- N new replies discovered
- N already tracked
- N author already engaged on thread
- N excluded
- N own account
- N no comment URN

### Step 8b: Structured scan-summary marker line (REQUIRED)
After the human-readable summary above, print EXACTLY ONE line with this
format (no other text on the line, all values are integers):

LINKEDIN_SCAN_SUMMARY: scanned=<TOTAL_NOTIFICATIONS_INSPECTED> new=<NEW_INSERTED> already=<ALREADY_TRACKED> excluded=<EXCLUDED+OWN_ACCOUNT> unmatched=<NO_COMMENT_URN>

The wrapper shell script greps this line to surface scan-stage pills
(scanned / new / excluded) on the dashboard Result column. If a counter
doesn't apply this run, emit it as 0 anyway. The line MUST start with
"LINKEDIN_SCAN_SUMMARY:" at column 0 with that exact spelling and casing.
PROMPT_EOF

# Acquire linkedin-browser ONLY around the Phase A Claude run. lock.sh is
# FIFO-queued, so a peer pipeline (run-linkedin, dm-replies-linkedin) that's
# mid-run blocks here rather than skipping. run_claude.sh auto-exports
# SA_PIPELINE_LOCKED=1 + SA_PIPELINE_PLATFORM so the PreToolUse hook
# (~/.claude/hooks/linkedin-agent-lock.sh) skips the cross-session block check.
acquire_lock "linkedin-browser" 3600
ensure_linkedin_browser_for_backend 2>&1 | tee -a "$LOG_FILE"

gtimeout 1800 "$REPO_DIR/scripts/run_claude.sh" "engage-linkedin-phaseA" --strict-mcp-config --mcp-config "$MCP_CONFIG" --output-format stream-json --verbose -p "$(cat "$PHASE_A_PROMPT")" 2>&1 | tee -a "$LOG_FILE" || log "WARNING: Phase A claude exited with code $?"

release_lock "linkedin-browser"
# Defense-in-depth: explicitly clear the hook-layer lockfile so the next
# pipeline cycle's PreToolUse never sees a stale entry from us. The
# run_claude.sh exit trap already does this in the happy path; this
# repeat is harmless and covers SIGKILL of run_claude.sh.
rm -f "$HOME/.claude/linkedin-agent-lock.json"
rm -f "$PHASE_A_PROMPT"

# ═══════════════════════════════════════════════════════
# PHASE B: Respond to pending LinkedIn replies
# Claude-driven. Posts via OAuth API (api.linkedin.com/v2/socialActions) by
# default (documented, authorized integration). Falls back to linkedin-agent
# MCP browser click-through if API errors.
# ═══════════════════════════════════════════════════════

# Reset any 'processing' items older than 2 hours back to 'pending'
RESET_COUNT=$(psql "$DATABASE_URL" -t -A -c "
    UPDATE replies SET status='pending'
    WHERE platform='linkedin' AND status='processing' AND processing_at < NOW() - INTERVAL '2 hours'
    RETURNING id;" | wc -l | tr -d ' ')
[ "$RESET_COUNT" -gt 0 ] && log "Phase B: Reset $RESET_COUNT stuck 'processing' LinkedIn items back to pending"

PENDING_COUNT=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM replies WHERE platform='linkedin' AND status='pending';")

if [ "$PENDING_COUNT" -eq 0 ]; then
    log "Phase B: No pending LinkedIn replies. Done!"
else
    log "Phase B: $PENDING_COUNT pending LinkedIn replies to process"

    PENDING_DATA=$(psql "$DATABASE_URL" -t -A -c "
        SELECT json_agg(q) FROM (
            SELECT r.id, r.platform, r.their_author,
                   r.their_content as their_content,
                   r.their_comment_url, r.their_comment_id, r.depth,
                   p.thread_title as thread_title,
                   p.thread_url, p.our_content as our_content, p.our_url,
                   CASE WHEN p.thread_url = p.our_url THEN 1 ELSE 0 END as is_our_original_post,
                   p.project_name
            FROM replies r
            JOIN posts p ON r.post_id = p.id
            WHERE r.platform='linkedin' AND r.status='pending'
            ORDER BY
                CASE WHEN p.thread_url = p.our_url THEN 0 ELSE 1 END,
                r.discovered_at ASC
            LIMIT $BATCH_SIZE
        ) q;")

    # Per-project voice map (so each reply can be drafted in the matched project's voice)
    PROJECTS_VOICE_JSON=$(python3 -c "
import json
c = json.load(open('$REPO_DIR/config.json'))
print(json.dumps({p['name']: p.get('voice', {}) for p in c.get('projects', []) if p.get('voice')}, indent=2))
" 2>/dev/null || echo "{}")

    # Engagement-style picker (2026-05-31 LinkedIn alignment to Twitter): pick
    # ONE assigned style per reply iteration PROGRAMMATICALLY, mirroring
    # engage-twitter.sh. The picked style flows two places: (1) --style filter
    # for top_performers.py so the per-style exemplars match the assignment,
    # (2) saps_render_style_block so the prompt embeds the same assignment. On
    # invent mode picked_style is empty and top_performers stays unfiltered.
    # Replaces the legacy generate_styles_block (which discarded the pick and
    # let the model invent freely).
    source "$REPO_DIR/skill/styles.sh"
    STYLE_ASSIGN_FILE=$(mktemp -t saps_linkedin_eng_assign_XXXXXX.json)
    saps_pick_style linkedin replying "$STYLE_ASSIGN_FILE" >/dev/null 2>&1 || true
    PICKED_STYLE=$(python3 -c "
import json
try:
    with open('$STYLE_ASSIGN_FILE') as f:
        d = json.load(f)
    print(d.get('style') or '')
except Exception:
    print('')
" 2>/dev/null)
    PICKED_MODE=$(python3 -c "
import json
try:
    with open('$STYLE_ASSIGN_FILE') as f:
        d = json.load(f)
    print(d.get('mode') or 'use')
except Exception:
    print('use')
" 2>/dev/null)
    STYLES_BLOCK=$(saps_render_style_block "$STYLE_ASSIGN_FILE" linkedin replying)
    rm -f "$STYLE_ASSIGN_FILE" 2>/dev/null || true

    # Top performers feedback report — filtered to the picked style when in
    # 'use' mode so the few-shot exemplars match the assignment.
    if [ -n "$PICKED_STYLE" ]; then
        TOP_REPORT=$(python3 "$REPO_DIR/scripts/top_performers.py" --platform linkedin --style "$PICKED_STYLE" 2>/dev/null || echo "(top performers report unavailable)")
    else
        TOP_REPORT=$(python3 "$REPO_DIR/scripts/top_performers.py" --platform linkedin 2>/dev/null || echo "(top performers report unavailable)")
    fi

    PHASE_B_PROMPT=$(mktemp)
    cat > "$PHASE_B_PROMPT" <<PROMPT_EOF
You are the Social Autoposter LinkedIn engagement bot.

Read $SKILL_FILE for the full workflow, content rules, and platform details.

$BROWSER_INSTRUCTIONS

EXCLUSIONS - do NOT engage with these accounts (skip and mark as 'skipped' with reason 'excluded_author'):
- Excluded authors: $EXCLUDED_AUTHORS
- Excluded LinkedIn profiles: $EXCLUDED_LINKEDIN

### BOT / ENGAGEMENT-LOOP ESCAPE HATCH (use sparingly, but use it)
We maintain a universal author blocklist in Postgres (\`author_blocklist\`),
consulted at /api/v1/replies POST time. A single block recorded by ANY of
our accounts/installs applies to EVERY future engagement from EVERY of our
accounts — universal scope, by design. The velocity gate already covers
"this handle has gotten too many replies from us in 24h/7d"; this lane is
for the LLM-judgment cases velocity cannot catch.

When to add a block (your judgment, exercised CONSERVATIVELY):
- The profile is plainly an AI/bot account: templated phrasing, generic
  filler answers, name/headline reads "AI growth agent" / "comments on
  posts for you", bio is engagement-farming boilerplate
- We are clearly stuck in a reciprocal engagement loop with this profile
  (they comment on every one of our posts, we reply to every one of theirs,
  no substance is exchanged)
- The profile is engagement farming (mass low-effort comments across the
  platform, not actually engaging with the topic)

DO NOT add a block for: someone we disagree with, a hostile-but-human
critic, a low-quality but human comment, or a single bad interaction.
Skip those (status='skipped') — blocking is permanent until manually
removed and applies to all our accounts.

How to add the block (run BEFORE marking the current reply skipped). The
handle to pass is the URL-vanity portion of /in/<vanity>/ (e.g. for
linkedin.com/in/jane-doe-123/, pass jane-doe-123):
  python3 \$REPO_DIR/scripts/reply_db.py blocklist add linkedin HANDLE \\
    --reason "<one-line judgment>" \\
    --classification {bot|engagement_loop} \\
    --source-reply-id REPLY_ID

Then mark the current reply skipped with a clear reason:
  python3 \$REPO_DIR/scripts/reply_db.py skipped REPLY_ID "blocklist_added:HANDLE"

CRITICAL - Browser agent rule: ONLY use the browser tool described in the BROWSER BACKEND block above (mcp__linkedin-harness__bh_run). NEVER use generic mcp__playwright-extension__*, mcp__isolated-browser__*, or mcp__macos-use__* tools.
CRITICAL: If a browser tool call is blocked or times out, DO NOT fall back to any other browser tool. Wait 30 seconds and retry. Repeat up to 3 times.
CRITICAL: TECHNICAL FAILURES ARE NOT TERMINAL. If after retries the action still failed for any technical reason (browser blocked, MCP timeout, page rendering issue, linkedin.com unreachable, linkedin_api.py 5xx), DO NOT call reply_db.py skipped. Leave the row in 'processing' status and move on to the next pending item. The next engage run's start-of-script cleanup resets stuck 'processing' rows back to 'pending' and retries automatically.
CRITICAL: ONLY call reply_db.py skipped for content/policy reasons (e.g., light_acknowledgment, drive_by_self_promo, hostile_user, off_topic, troll, excluded_author). NEVER skip for technical browser/network failures: those must be retry-able.
CRITICAL: Do NOT call /voyager/api/ endpoints. Posting goes through linkedin_api.py (OAuth api.linkedin.com). Browser is the fallback only.

## Respond to pending LinkedIn replies ($PENDING_COUNT total)

### Priority order:
1. **Replies on our original posts** (is_our_original_post=1) - highest priority
2. **Direct questions** ("what tool", "how do you", "can you share")
3. **Everything else** - general engagement

### Tiered link strategy:
- **Tier 1 (default):** No link. Genuine engagement, expand topic.
- **Tier 2 (natural mention):** Conversation touches a topic matching a project in config. Recommend it casually as a tool you've come across.
- **Tier 3 (direct ask):** They ask for link/tool/source. Give it immediately.

## FEEDBACK FROM PAST PERFORMANCE (use this to write better replies):
$TOP_REPORT

$STYLES_BLOCK

## Per-project voice map
For each reply you draft, look up the matched project's voice block below and apply it: follow \`voice.tone\`, never violate any item in \`voice.never\`, mirror \`voice.examples\` / \`voice.examples_good\` when present.
$PROJECTS_VOICE_JSON

## Resolving the parent post (replaces the old prompt-blob index)
Each pending row's \`project_name\` is a best-effort guess. After navigating the thread (Step 2), extract the activity_id from the page URL/comment URN and resolve it via:
  python3 $REPO_DIR/scripts/lookup_post.py linkedin ACTIVITY_ID
Returns JSON: {"project": "fazm", "our_content": "...full text...", "thread_url": "..."} or {"project": null} if it's not one of our posts.

Here are the replies to process:
$PENDING_DATA

CRITICAL: Reply in the SAME LANGUAGE as the message you are responding to. Match the language exactly.
CRITICAL: Process EVERY reply. For each: either post a response and mark as 'replied', OR mark as 'skipped' with a skip_reason.

CRITICAL: For ALL database operations, use the reply_db.py helper (NOT raw psql):
  python3 $REPO_DIR/scripts/reply_db.py processing ID          # BEFORE posting
  python3 $REPO_DIR/scripts/reply_db.py replied ID "reply text" [url] [engagement_style] [is_recommendation]   # AFTER posting. engagement_style is TONE (critic, storyteller, etc). Pass "1" for is_recommendation ONLY when the reply casually recommends a project (Tier 2/3); leave blank otherwise.
  python3 $REPO_DIR/scripts/reply_db.py skipped ID "reason"
  python3 $REPO_DIR/scripts/reply_db.py skip_batch '{"ids":[1,2,3],"reason":"..."}'
  python3 $REPO_DIR/scripts/reply_db.py status
NEVER use psql directly for reply status updates.

### Project tracking on replies
When you recommend a project in a reply (Tier 2 or Tier 3), set project_name on the reply:
  source ~/social-autoposter/.env
  psql "\$DATABASE_URL" -c "UPDATE replies SET project_name='PROJECT_NAME' WHERE id=REPLY_ID;"

MANDATORY reply flow for every item:
  Step 1: python3 reply_db.py processing ID      <- mark BEFORE posting
  Step 2: NAVIGATE TO THE THREAD AND READ CONTEXT (mandatory, do NOT skip).
          Do NOT draft a reply from the notification snippet alone — the snippet
          is truncated and lacks the parent post content + sibling replies.
          a) Navigate (per the BROWSER BACKEND block) to their_comment_url
          b) Snapshot/screenshot (per the BROWSER BACKEND block) to read:
             - the FULL parent post text (our original post if this is on our thread)
             - the immediate ancestor of their_comment_id
             - sibling replies (so you don't repeat what someone else already said)
          c) Extract the activity_id (the long numeric string after \`urn:li:activity:\`)
             from the URL or comment URN. Resolve it:
               python3 $REPO_DIR/scripts/lookup_post.py linkedin ACTIVITY_ID
             If the response has a non-null \"project\", that's our post — OVERRIDE
             the reply row and use that project's voice for drafting:
               source ~/social-autoposter/.env
               psql "\$DATABASE_URL" -c "UPDATE replies SET project_name='RESOLVED_PROJECT' WHERE id=REPLY_ID;"
             Use the returned \"our_content\" as the FULL text of the post being
             replied to (more accurate than the truncated our_content in PENDING_DATA).
             If \"project\" is null, we're a guest in someone else's thread; keep
             the existing project_name and follow global content rules.
  Step 3: Draft the reply using the resolved project's voice + the ASSIGNED
          engagement style. This cycle: mode=$PICKED_MODE style='${PICKED_STYLE:-(invent)}'.
          In USE mode ($PICKED_MODE=use) apply the assigned style '${PICKED_STYLE}'
          verbatim; do NOT pick a different style and do NOT invent one. In INVENT
          mode ($PICKED_MODE=invent) craft a NEW snake_case style name not in the
          curated block above and pass it as the [engagement_style] arg in Step 5.
          Professional but casual. NEVER em dashes. Match parent post language.
  Step 4: post reply (OAuth API first, browser fallback)
  Step 5: python3 reply_db.py replied ID "text" [url] [engagement_style] [is_recommendation]   <- mark AFTER success. engagement_style is the style name you applied (in USE mode the assigned '${PICKED_STYLE}'). is_recommendation="1" only when you mentioned a project (Tier 2/3).
If Step 5 fails, the item stays 'processing' and will be reset to 'pending' on the next run.

For LinkedIn replies - use the OAuth API first:
1. Extract the activity ID from their_comment_url or their_comment_id.
   - From their_comment_id like \`urn:li:comment:(activity:7438226125077549056,7438815640536170496)\`, the activity ID is \`7438226125077549056\` and the full URN is the parent_comment_urn.
   - From their_comment_url, extract the activity ID from the URL path.
2. Post the reply via API:
   \`\`\`bash
   python3 $REPO_DIR/scripts/linkedin_api.py reply ACTIVITY_ID "PARENT_COMMENT_URN" "YOUR REPLY TEXT" --project "RESOLVED_PROJECT_NAME" --reply-id REPLY_ID
   \`\`\`
   Replace RESOLVED_PROJECT_NAME with the project from lookup_post.py (or the row's project_name if lookup returned null).
   Replace REPLY_ID with the numeric id from the current reply row.
   The API will automatically wrap any URLs in the reply text with short tracking links and backfill the reply_id after posting.
   This returns JSON with {ok, reply_urn, permalink}. Use permalink as the reply URL.
   On {"ok": true}: skip step 3 (browser fallback) and skip the browser-based
   verification in step 5 below — the API success response (with reply_urn) is
   itself authoritative. Mark replied with the permalink. Do NOT navigate the
   browser to verify; that would burn the linkedin-browser lock for no gain.
3. If the API call fails (e.g., token expired, comment deleted), fall back to the linkedin-agent browser:
   - UTM-WRAP YOUR_REPLY_TEXT FIRST. The browser-typing path has no Python wrap
     layer, so a bare URL would be posted as-is and we'd lose attribution. Run:
       python3 \$REPO_DIR/scripts/dm_short_links.py utm-text \\
         --platform linkedin \\
         --project RESOLVED_PROJECT_NAME \\
         --text "YOUR_REPLY_TEXT"
     Use the printed string going forward. No DB write happens. Safe to run
     unconditionally (no-op when YOUR_REPLY_TEXT contains zero URLs).
   - Navigate to their_comment_url (per the BROWSER BACKEND block)
   - Snapshot/screenshot to find the comment, click Reply (click_at_xy on the Reply control), type the (UTM-wrapped) text, submit
   - Do NOT aggressively scroll-and-expand comments; if the comment isn't visible after a normal scroll, mark as 'skipped' with reason 'comment_not_found'
4. If both API and browser fail, mark as 'skipped' with reason 'comment_not_found'.

5. POST-SUBMIT VERIFICATION (mandatory, BROWSER-FALLBACK PATH ONLY).
   If you posted via the OAuth API in step 2 and got {"ok": true}, SKIP this
   block entirely — the API response is the verification. Run this block ONLY
   when step 3's browser fallback was used. Verify visually + via DOM (the
   harness has no network-capture tool; do NOT try to read /voyager or socialActions
   network traffic — that is a flagged pattern anyway):
   5a. Capture a screenshot (bh_run print(capture_screenshot())) and Read the PNG to
       check for a toast / submit feedback.
   5b. Read the DOM via the BROWSER BACKEND block's run-code equivalent (bh_run with
       js("""...""")) and check:
         (a) a fresh comment by 'Matthew Diakonov' / 'You' is rendered under their_comment
         (b) NO 'could not be created' / 'try again' / 'something went wrong' toast text
         (c) reply editor textbox cleared (empty contenteditable)
   5c. SUCCESS = (a) passes with no toast. REJECTED = error toast present OR our
       reply not visible in the DOM.
6. If REJECTED, do NOT call reply_db.py replied. Mark soft-blocked:
     python3 $REPO_DIR/scripts/reply_db.py skipped ID "soft_blocked: <verbatim toast or 'quiet_fail_count_unchanged'>"
   Then STOP this row and move to the next pending reply.
7. If step 5 SUCCESS (or step 2 OAuth success), mark replied via Step 5 of the
   MANDATORY reply flow above (reply_db.py replied ID "text" [url] [style] [is_recommendation]).

After every 10 replies, run: python3 $REPO_DIR/scripts/reply_db.py status
PROMPT_EOF

    # Re-acquire linkedin-browser ONLY for Phase B. The lock was released after
    # Phase A so peer pipelines could use the browser during our DB-pull /
    # styles-prep window (~1-3s). FIFO ticket queue in lock.sh ensures
    # fairness if a peer or parallel cycle grabbed it in the meantime.
    acquire_lock "linkedin-browser" 3600
    ensure_linkedin_browser_for_backend 2>&1 | tee -a "$LOG_FILE"

    gtimeout 5400 "$REPO_DIR/scripts/run_claude.sh" "engage-linkedin-phaseB" --strict-mcp-config --mcp-config "$MCP_CONFIG" --output-format stream-json --verbose -p "$(cat "$PHASE_B_PROMPT")" 2>&1 | tee -a "$LOG_FILE" || log "WARNING: Phase B claude exited with code $?"

    release_lock "linkedin-browser"
    # Defense-in-depth: explicit hook-lockfile cleanup; see Phase A note.
    rm -f "$HOME/.claude/linkedin-agent-lock.json"
    rm -f "$PHASE_B_PROMPT"
fi

# Reset any items left in 'processing' after subprocess exit (tech-failure
# retry path: agent leaves rows here on browser/MCP failure rather than
# calling reply_db.py skipped, so the next run picks them up automatically).
POST_RESET=$(psql "$DATABASE_URL" -t -A -c "
    WITH upd AS (
        UPDATE replies SET status='pending'
        WHERE platform='linkedin' AND status='processing'
        RETURNING id
    ) SELECT COUNT(*) FROM upd;")
[ "$POST_RESET" -gt 0 ] && log "Post-run: Reset $POST_RESET 'processing' LinkedIn items back to pending"

# ═══════════════════════════════════════════════════════
# Cleanup
# ═══════════════════════════════════════════════════════
TOTAL_PENDING=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM replies WHERE platform='linkedin' AND status='pending';")
TOTAL_REPLIED=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM replies WHERE platform='linkedin' AND status='replied';")
TOTAL_SKIPPED=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM replies WHERE platform='linkedin' AND status='skipped';")

log "LinkedIn summary: pending=$TOTAL_PENDING replied=$TOTAL_REPLIED skipped=$TOTAL_SKIPPED"

# Log run to persistent monitor
RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
_COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START" --scripts "engage-linkedin-phaseA" "engage-linkedin-phaseB" 2>/dev/null || echo "0.0000")
# Pull Phase A scan-stage counters from the structured marker line Claude emits
# at the end of Phase A: "LINKEDIN_SCAN_SUMMARY: scanned=N new=N already=N
# excluded=N unmatched=N". If the marker is missing (Claude failed before
# printing or the prompt drifted), fall back to no scan= segment and the
# dashboard renders the old way.
LI_SCAN_LINE=$(grep -m1 "^LINKEDIN_SCAN_SUMMARY:" "$LOG_FILE" 2>/dev/null || true)
LI_SCAN_ARG=""
if [ -n "$LI_SCAN_LINE" ]; then
  li_scanned=$(echo "$LI_SCAN_LINE" | grep -oE "scanned=[0-9]+" | head -1 | cut -d= -f2)
  li_new=$(echo "$LI_SCAN_LINE" | grep -oE "new=[0-9]+" | head -1 | cut -d= -f2)
  li_already=$(echo "$LI_SCAN_LINE" | grep -oE "already=[0-9]+" | head -1 | cut -d= -f2)
  li_excl=$(echo "$LI_SCAN_LINE" | grep -oE "excluded=[0-9]+" | head -1 | cut -d= -f2)
  li_unm=$(echo "$LI_SCAN_LINE" | grep -oE "unmatched=[0-9]+" | head -1 | cut -d= -f2)
  parts=""
  [ -n "$li_scanned" ] && parts="${parts}scanned=${li_scanned},"
  [ -n "$li_new" ]     && parts="${parts}new=${li_new},"
  [ -n "$li_already" ] && [ "$li_already" -gt 0 ] && parts="${parts}already=${li_already},"
  [ -n "$li_excl" ]    && [ "$li_excl"    -gt 0 ] && parts="${parts}excluded=${li_excl},"
  [ -n "$li_unm" ]     && [ "$li_unm"     -gt 0 ] && parts="${parts}unmatched=${li_unm},"
  LI_SCAN_ARG="${parts%,}"
fi
if [ -n "$LI_SCAN_ARG" ]; then
  python3 "$REPO_DIR/scripts/log_run.py" --script "engage_linkedin" --posted "$TOTAL_REPLIED" --skipped "$TOTAL_SKIPPED" --failed 0 --cost "$_COST" --elapsed "$RUN_ELAPSED" --scan "$LI_SCAN_ARG"
else
  python3 "$REPO_DIR/scripts/log_run.py" --script "engage_linkedin" --posted "$TOTAL_REPLIED" --skipped "$TOTAL_SKIPPED" --failed 0 --cost "$_COST" --elapsed "$RUN_ELAPSED"
fi

# Delete old logs
find "$LOG_DIR" -name "engage-linkedin-*.log" -mtime +7 -delete 2>/dev/null || true

log "=== LinkedIn engagement complete: $(date) ==="
