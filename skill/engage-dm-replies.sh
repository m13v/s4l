#!/usr/bin/env bash
# engage-dm-replies.sh — DM conversation reply loop
# Scans Reddit Chat, LinkedIn Messages, and X/Twitter DMs for new inbound messages,
# then replies to continue the conversation.
#
# Usage:
#   engage-dm-replies.sh                    # Run all platforms
#   engage-dm-replies.sh --platform reddit  # Reddit DMs only
#   engage-dm-replies.sh --platform linkedin # LinkedIn DMs only
#   engage-dm-replies.sh --platform twitter  # Twitter DMs only
# Called by launchd every 4 hours.


set -euo pipefail

# Parse --platform flag
PLATFORM=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --platform) PLATFORM="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

if [ -n "$PLATFORM" ]; then
    case "$PLATFORM" in
        reddit|linkedin|twitter|x) ;;
        *) echo "ERROR: Unknown platform '$PLATFORM'. Use: reddit, linkedin, twitter"; exit 1 ;;
    esac
fi

# LinkedIn killswitch (2026-05-27): refuse to run --platform linkedin if a
# prior fire detected session compromise (http_999, authwall, throttle,
# li_at cleared). Other platforms unaffected.
# State: ~/.claude/social-autoposter/linkedin.killswitch
# Clear: python3 ~/social-autoposter/scripts/linkedin_killswitch.py clear
if [ "$PLATFORM" = "linkedin" ] && [ -f "$HOME/.claude/social-autoposter/linkedin.killswitch" ]; then
    echo "[$(date +%H:%M:%S)] LINKEDIN_KILLSWITCH active. Aborting LinkedIn DM-replies pipeline."
    echo "  Re-auth LinkedIn in harness Chrome, then: python3 ~/social-autoposter/scripts/linkedin_killswitch.py clear"
    exit 0
fi

LOCK_NAME="dm-replies"
[ -n "$PLATFORM" ] && LOCK_NAME="dm-replies-$PLATFORM"

# Per-cycle batch id stamped onto every claude_sessions row spawned by this
# DM-reply run (via SA_CYCLE_ID env -> log_claude_session.py). 2026-05-10
# cycle_id rollout. Suffix carries platform so multi-platform fan-outs from
# the same launchd cadence stay distinguishable.
BATCH_ID="endm-${PLATFORM:-all}-$(date +%Y%m%d-%H%M%S)-$$"
export SA_CYCLE_ID="$BATCH_ID"

# Pipeline lock at top. Platform-browser locks are acquired later, just
# before the Claude/MCP step that drives the browser, so peers can use the
# profile during our Phase 0 (Gmail + matrix-js-sdk IndexedDB ingest), DB
# scans, and prompt build. Alphabetical order is preserved at acquire time
# below for multi-platform runs to prevent deadlock.
source "$(dirname "$0")/lock.sh"

# Twitter backend lib: sourced for ensure_twitter_browser_for_backend (probes
# the harness Chrome on port 9555, launches it if down). Also exports
# TWITTER_CDP_URL so the python helpers (scripts/twitter_browser.py) attach
# directly to the harness Chrome instead of doing ps-based discovery. Sets
# MCP_CONFIG_FILE + BROWSER_INSTRUCTIONS as side effects; this script uses
# DM_MCP_CONFIG (different variable) so no conflict.
source "$(dirname "$0")/lib/twitter-backend.sh"

# Reddit backend lib (2026-05-29 harness migration): exports REDDIT_CDP_URL=:9557
# so the Python CDP helpers (scripts/reddit_browser.py send-dm/reply/unread-dms)
# attach to the dedicated reddit-harness Chrome instead of the retired
# reddit-agent (port 9222). Also provides ensure_reddit_browser_for_backend
# (launches harness Chrome on 9557 + tab cleanup). Like twitter-backend it sets
# MCP_CONFIG_FILE + BROWSER_INSTRUCTIONS, but this script drives the prompt off
# DM_MCP_CONFIG (set per-platform below) and inlines its own per-platform browser
# instructions, so those side-effect vars (MCP_CONFIG_FILE, BROWSER_INSTRUCTIONS)
# are unused here; only REDDIT_CDP_URL + ensure_reddit_browser_for_backend matter.
source "$(dirname "$0")/lib/reddit-backend.sh"

# Skip cleanly if a foreign playwright-mcp wrapper for THIS platform is alive
# (interactive Fazm Dev / IDE / another cron). Avoids the Chrome SingletonLock
# crash cascade — see dm-outreach-twitter.sh and engage-twitter.sh.
# Twitter case routes through defer_if_foreign_for_backend (always returns 1
# = proceed) since harness CDP supports multiple concurrent clients on the
# browser-harness profile, so foreign MCP wrappers never block us.
# LOG_FILE isn't set yet (line 78), so use ${LOG_FILE:-} to satisfy set -u.
if [ -n "$PLATFORM" ]; then
    case "$PLATFORM" in
        twitter|x)
            if defer_if_foreign_for_backend "${LOG_FILE:-}"; then
                exit 0
            fi
            ;;
        *)
            if defer_if_foreign_browser_mcp_active "$PLATFORM" "${LOG_FILE:-}"; then
                exit 0
            fi
            ;;
    esac
fi
acquire_lock "$LOCK_NAME" 3600

# Load secrets
# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
SKILL_FILE="$REPO_DIR/SKILL.md"
LOG_DIR="$REPO_DIR/skill/logs"
DM_SCRIPT="$REPO_DIR/scripts/dm_conversation.py"

# HTTP-only: this pipeline routes every read/write through the s4l.ai HTTP API
# (scripts/*_helper.py -> /api/v1/*). The direct-Postgres lane was removed
# 2026-06-01; DATABASE_URL is deliberately ignored, no psql, no fallback.
mkdir -p "$LOG_DIR"
LOG_SUFFIX=""
[ -n "$PLATFORM" ] && LOG_SUFFIX="-$PLATFORM"
LOG_FILE="$LOG_DIR/engage-dm-replies${LOG_SUFFIX}-$(date +%Y-%m-%d_%H%M%S).log"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

RUN_START=$(date +%s)
log "=== DM Reply Engagement Run: $(date) (platform: ${PLATFORM:-all}) ==="

# Load config
REDDIT_USERNAME=$(python3 -c "import json; c=json.load(open('$REPO_DIR/config.json')); print(c.get('accounts',{}).get('reddit',{}).get('username',''))" 2>/dev/null || echo "")
EXCLUDED_AUTHORS=$(python3 -c "import json; c=json.load(open('$REPO_DIR/config.json')); print(', '.join(c.get('exclusions',{}).get('authors',[])))" 2>/dev/null || echo "")

# Load projects for context (booking link + qualification criteria per project)
PROJECTS=$(python3 -c "
import json
c = json.load(open('$REPO_DIR/config.json'))
for p in c.get('projects', []):
    line = f\"- {p['name']}: {p.get('description','')} | website: {p.get('website','')} | github: {p.get('github','')}\"
    if p.get('booking_link'):
        line += f\" | booking_link: {p['booking_link']}\"
    if p.get('booking_link_auto_share'):
        line += ' | booking_link_auto_share: true'
    q = p.get('qualification') or {}
    qs = q.get('questions') or ([q['question']] if q.get('question') else [])
    if qs:
        line += ' | qualifying_questions: [' + ' || '.join(qs) + ']'
    if q.get('must_have'):
        line += f\" | must_have: {' ; '.join(q['must_have'])}\"
    if q.get('disqualify'):
        line += f\" | disqualify: {' ; '.join(q['disqualify'])}\"
    print(line)
" 2>/dev/null || echo "")

# ═══════════════════════════════════════════════════════
# Find conversations needing replies (platform-filtered)
# ═══════════════════════════════════════════════════════

# Platform filtering is now applied server-side by /api/v1/dms/engage (it folds
# 'x'/'twitter' into one platform, equality otherwise, and treats an absent
# platform as "all"). The old client-side PLATFORM_SQL_FILTER string was
# removed with the psql lane on 2026-06-01; $PLATFORM is passed straight to the
# helper subcommands instead.

# Get conversations where the last message is inbound (they replied OR a backend
# signal like a short-link click landed as a synthetic inbound row). Click signals
# are inserted into dm_messages by the /r/<code> redirector with
# direction='inbound', author='__click_signal__', content='[CLICK_SIGNAL] ...'.
# That makes them surface here on the same `last_in > last_out` rail as typed
# replies, with no special branching.
# HTTP-only via /api/v1/dms/engage?mode=pending (scripts/dm_engage_helper.py).
# The helper prints the JSON array of rows when any exist, else the literal
# string 'null' (matching psql's `json_agg(...) -> NULL` echoed as "null"), so
# the guard below is unchanged. Platform filter (x/twitter fold, equality
# otherwise, no-platform => all) is applied server-side.
PENDING_PLAT_ARG=""
[ -n "$PLATFORM" ] && PENDING_PLAT_ARG="--platform $PLATFORM"
PENDING_CONVOS=$(python3 "$REPO_DIR/scripts/dm_engage_helper.py" pending $PENDING_PLAT_ARG --limit 30 2>/dev/null || echo "null")

if [ "$PENDING_CONVOS" = "null" ] || [ -z "$PENDING_CONVOS" ]; then
    log "No conversations needing replies. Checking platforms for new inbound messages..."
else
    CONVO_COUNT=$(echo "$PENDING_CONVOS" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")
    log "Found $CONVO_COUNT conversations needing replies from DB"
fi

# ═══════════════════════════════════════════════════════
# PHASE 0: Send pending human replies from email escalations
# ═══════════════════════════════════════════════════════
# Platform filter for Phase 0: when running a specific platform cycle, only
# process replies targeted at that platform. Empty PLATFORM = all platforms
# (manual runs). This prevents parallel platform cycles from racing on the
# same rows and clobbering each other's status updates.
HR_PLATFORM_ARG=""
if [ -n "$PLATFORM" ]; then
    # Pass the platform straight through to /api/v1/human-dm-replies, which
    # folds 'x'/'twitter' into one platform server-side (matches the legacy
    # filter). Empty PLATFORM = all platforms (manual runs).
    HR_PLATFORM_ARG="--platform $PLATFORM"
fi

# Ingest any human replies that have landed in the matt@s4l.ai inbox since the
# last run. Parses [DM #N] from the subject, strips quoted history, inserts
# into human_dm_replies with status='pending'. Safe to run every cycle (no-op
# when inbox is empty; deduped by Gmail message id).
log "Phase 0: ingesting human DM replies from Gmail inbox..."
python3 "$REPO_DIR/scripts/ingest_human_dm_replies.py" 2>&1 | while IFS= read -r _line; do
    log "  [ingest] $_line"
done || true

# Pending + retry queue via /api/v1/human-dm-replies (HTTP-only, no direct DB).
# Helper prints a JSON array when rows exist and nothing when none, so the
# guard below falls through to the "no replies" branch exactly like psql's
# NULL -> empty string did. Non-array output (an API/transport error) is
# logged loudly and treated as "no replies" instead of being silently parsed.
_HR_OUT=$(python3 "$REPO_DIR/scripts/human_dm_replies_helper.py" pending $HR_PLATFORM_ARG 2>&1)
if printf '%s' "$_HR_OUT" | head -c1 | grep -q '\['; then
    HUMAN_REPLIES="$_HR_OUT"
else
    if [ -n "$_HR_OUT" ]; then
        log "Phase 0: human-dm-replies pending fetch issue (treating as no replies):"
        log "$_HR_OUT"
    fi
    HUMAN_REPLIES=""
fi

if [ "$HUMAN_REPLIES" != "null" ] && [ -n "$HUMAN_REPLIES" ]; then
    HR_COUNT=$(echo "$HUMAN_REPLIES" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")
    log "Phase 0: $HR_COUNT pending human replies to send"

    PHASE0_PROMPT=$(mktemp)
    cat > "$PHASE0_PROMPT" <<PHASE0_EOF
You are the Social Autoposter DM delivery bot.

Read $SKILL_FILE for content rules (tone, anti-AI detection, no em dashes).

## Task: Deliver pending human replies on the correct channel(s)

The following replies were written by the human operator (via email or the dashboard) as INSTRUCTIONS for how to respond. Use each reply as a prompt, understand the intent, tone, and key points, then craft a natural reply that:
- Matches the conversational tone of the thread (casual, texting style, 1-3 sentences)
- Incorporates the human's key points and decisions
- Sounds like the same person who sent the previous outbound messages in the conversation
- Follows all the HARD RULES and COMMITMENT GUARDRAILS from Phase D

The human's reply is your DIRECTION, not the literal message. Think of it as "the human told you what to say, now say it naturally."

Each pending reply has a \`reply_channel\` field that selects the delivery surface:
- \`dm\` (default, legacy): send only as a private DM
- \`public\`: post only as a public reply on the original public thread (their comment that started the DM)
- \`both\`: do BOTH, post the public reply AND send the DM (paired delivery, same instruction text drives both)

Pending human replies:
$HUMAN_REPLIES

### Step 0. ESCAPE HATCH — reclassify before delivering

Before drafting any message, check whether the human's instruction is actually a directive ABOUT the escalation rather than a message to send. The human writes these by replying to escalation emails, so they sometimes use the reply field to issue meta-commands. Examples:

- "Remove this escalation", "cancel", "dismiss", "ignore", "false alarm", "no need to reply"
- "Skip this one", "don't send", "not relevant"
- "Mark as handled", "I already replied manually"
- "Disqualify", "block", "spam"

If the instruction text clearly matches one of these intents (use judgment, the human writes casually), DO NOT send anything on any channel. Instead:

\`\`\`bash
# Mark the human reply row as cancelled so it never retries (sent_at is
# auto-stamped server-side on the cancelled transition)
cd ~/social-autoposter && python3 scripts/human_dm_replies_helper.py patch --id REPLY_ID --status cancelled --last-error "human reclassified: <SHORT_REASON>"

# Optionally update the underlying conversation if the intent says so:
#   - "disqualify"/"block"/"spam"  → set-status disqualified
#   - "mark as handled"/"already replied"  → set-status active
#   - "skip"/"dismiss"/"remove escalation"  → leave conversation as-is, just clear the flag
cd ~/social-autoposter && python3 scripts/dm_conversation.py set-status --dm-id DM_ID --status STATUS_OR_OMIT
\`\`\`

Log clearly in your summary which rows were reclassified and why. Only proceed to Step A-D for instructions that are genuine messages to send.

For each remaining reply, branch on \`reply_channel\`:

### Step A. Always read context first

\`\`\`bash
cd ~/social-autoposter && python3 scripts/dm_conversation.py history --dm-id DM_ID
\`\`\`

### Step B. If \`reply_channel\` is \`public\` or \`both\`: deliver the public reply

The \`public_target_url\` field is THEIR public comment that originally led to this DM thread (from the joined \`replies\` row via \`dms.reply_id\`). The \`public_target_post_url\` is the parent post URL for context. If \`public_target_url\` is null, fall back to \`our_prior_public_reply_url\` (we can reply under our own previous reply on the same thread).

1. Craft a natural public reply based on the human's instructions. Public replies are visible to everyone, so keep them appropriate for a public audience: friendly, helpful, concise, and on-brand. The instruction text typically asks you to share a link, so include it naturally in the public reply.
2. Navigate to \`public_target_url\` on the correct platform and post the public reply:
   - **Reddit** (mcp__reddit-harness__bh_run tool, CDP-driven real Chrome on port 9557): use bh_run('goto_url("COMMENT_URL"); wait_for_load()') to navigate, then bh_run scripts to click reply on the target comment, type, submit (click_at_xy on the computed center of the textarea, type_text, then click the Comment/Save button). See lib/reddit-backend.sh BROWSER_INSTRUCTIONS for the full Playwright -> bh_run translation table. Capture the resulting comment URL.
   - **LinkedIn** (mcp__linkedin-harness__bh_run tool, CDP-driven real Chrome on port 9556): use bh_run('goto_url("POST_URL"); wait_for_load()') to navigate, then bh_run scripts to expand the target comment and reply (click_at_xy on the computed center of the contenteditable box, type_text, then click the Reply/Post button; never press Enter). See lib/linkedin-backend.sh BROWSER_INSTRUCTIONS for the full Playwright -> bh_run translation table. Capture the resulting comment URL.
   - **X/Twitter** (mcp__twitter-harness__bh_run tool, CDP-driven real Chrome on port 9555): use bh_run('goto_url("TWEET_URL"); wait_for_load()') to navigate, then bh_run scripts to click the reply button, type, and post. See lib/twitter-backend.sh BROWSER_INSTRUCTIONS for the full Playwright -> bh_run translation table. Capture the resulting status URL.
3. Insert a fresh \`replies\` row capturing the public reply (use the \`their_comment_id\` from \`public_target_comment_id\` so the dedup index does not collide; if null, synthesize a unique id like \`hr_<REPLY_ID>_pub\`):
   \`\`\`bash
   # Prints the new replies.id to stdout. Include --post-id only when
   # PUBLIC_TARGET_POST_ID is not null; omit it otherwise. A duplicate
   # their_comment_id returns the existing row's id (no error).
   cd ~/social-autoposter && python3 scripts/human_dm_replies_helper.py insert-public-reply --platform PLATFORM --comment-id COMMENT_ID --author THEIR_AUTHOR --comment-url PUBLIC_TARGET_URL --our-content "CRAFTED_PUBLIC_REPLY" --our-url OUR_NEW_PUBLIC_REPLY_URL --depth 2 --post-id PUBLIC_TARGET_POST_ID
   \`\`\`
4. Stamp the \`replies.id\` back onto the human instruction so the dashboard can pair them:
   \`\`\`bash
   cd ~/social-autoposter && python3 scripts/human_dm_replies_helper.py patch --id REPLY_ID --public-reply-id NEW_REPLY_ID
   \`\`\`

### Step C. If \`reply_channel\` is \`dm\` or \`both\`: deliver the DM

1. Craft a natural DM based on the human's instructions and the conversation context.
2. Navigate to the conversation on the correct platform using \`chat_url\` (or find the conversation with their_author).
   - **Reddit Chat** (mcp__reddit-harness__bh_run tool, CDP-driven real Chrome on port 9557)
   - **LinkedIn Messages** (mcp__linkedin-harness__bh_run tool, CDP-driven real Chrome on port 9556)
   - **X/Twitter DMs** (mcp__twitter-harness__bh_run tool), if encrypted DM passcode dialog appears, enter: $TWITTER_DM_PASSCODE
3. Type and send the crafted DM.
4. Log the outbound message (log what you ACTUALLY SENT, not the human's instructions). Pass --verified ONLY when the browser tool returned verified=true. If verification failed, log nothing and let the next cycle retry; never pass --verified speculatively:
   \`\`\`bash
   cd ~/social-autoposter && python3 scripts/dm_conversation.py log-outbound --dm-id DM_ID --content "THE_CRAFTED_DM_YOU_SENT" --verified
   \`\`\`

### Step D. Always finalize after the channel work succeeds

ONLY mark the human reply as sent after every required channel succeeded for it. For \`both\`, that means the public reply landed AND the DM landed. Partial success counts as failure (see error handling below).

\`\`\`bash
cd ~/social-autoposter && python3 scripts/human_dm_replies_helper.py patch --id REPLY_ID --status sent
cd ~/social-autoposter && python3 scripts/dm_conversation.py set-status --dm-id DM_ID --status active
\`\`\`

### Error handling

If any required channel fails, increment the attempts counter and record the reason. Use a short error string (single line, no quotes); for partial \`both\` failures include which side failed:
\`\`\`bash
cd ~/social-autoposter && python3 scripts/human_dm_replies_helper.py patch --id REPLY_ID --status failed --increment-attempts --last-error "ERROR_REASON"
\`\`\`
Rows with \`status = 'failed'\` AND \`attempts < 3\` will be picked up automatically on the next Phase 0 run for this platform. After 3 attempts they stay failed and stop retrying, notify the human in the run summary so they can handle manually.

Idempotency for \`both\` retries: if \`public_reply_id\` is already set when you re-process a failed row, the public side is already live, do NOT post it again, only redo the DM side.

Note: each Phase 0 run is scoped to a single platform ($PLATFORM), so you will only see replies for that platform here. Do not worry about replies for other platforms.
PHASE0_EOF

    # The main Claude agent session will process this prompt alongside phases A-D
    PHASE0_INSTRUCTIONS=$(cat "$PHASE0_PROMPT")
    rm -f "$PHASE0_PROMPT"
else
    log "Phase 0: No pending human replies"
    PHASE0_INSTRUCTIONS=""
fi

# ═══════════════════════════════════════════════════════
# PHASE A: Scan Reddit Chat for new inbound messages
# ═══════════════════════════════════════════════════════
log "Phase A: Scanning Reddit Chat for new inbound messages..."

# Phase A.0: Ingest Reddit Chat inbounds directly from the matrix-js-sdk
# IndexedDB cache before the LLM runs. Replaces the old "scan sidebar +
# click into each unread room" flow (which was silently broken: the
# scan_reddit_chat.js selector hadn't matched the post-migration DOM in
# weeks, leaving 200+ unread rooms invisible to the pipeline).
#
# ingest-unread reads Matrix state the Reddit client already synced, upserts
# dms rows (backfilling chat_url), and logs each inbound m.room.message with
# its Matrix event_id as the dedup key. Idempotent — re-runs dedup via
# dm_messages.event_id's UNIQUE partial index. When this completes, the
# pending-replies query and dashboard both see the full unread backlog; the
# LLM then only has to decide who to reply to, not where to find inbounds.
if [ -z "$PLATFORM" ] || [ "$PLATFORM" = "reddit" ]; then
    log "Phase A.0: ingesting Reddit Chat inbounds from matrix-js-sdk IndexedDB..."
    _INGEST_OUT=$(mktemp)
    if python3 "$REPO_DIR/scripts/reddit_chat_sync.py" ingest-unread > "$_INGEST_OUT" 2>/dev/null; then
        python3 -c "
import json, sys
d = json.load(open('$_INGEST_OUT'))
s = d.get('stats', {}) or {}
fields = ['rooms_scanned','rooms_new_dms','chat_urls_backfilled','inbound_inserted','inbound_deduped']
parts = [f'{k}={s.get(k, 0)}' for k in fields]
errs = len(s.get('errors') or [])
parts.append(f'errors={errs}')
print(' '.join(parts))
" 2>/dev/null | while IFS= read -r _line; do log "  [ingest] $_line"; done
    else
        log "  [ingest] WARNING: reddit_chat_sync.py ingest-unread failed; Reddit Chat backlog may be stale"
    fi
    rm -f "$_INGEST_OUT"
fi

# Get list of known Reddit DM authors to match against chat rooms
KNOWN_REDDIT_AUTHORS=$(python3 "$REPO_DIR/scripts/dm_engage_helper.py" reddit-authors 2>/dev/null || echo "")

# Pre-build tool-rule lines and per-platform phase sections OUTSIDE the outer
# heredoc. bash 3.2 (the macOS system bash) mis-parses nested `$(if cond; then
# cat <<'EOF' ... EOF fi)` inside an unquoted heredoc when the if is false,
# treating the inner heredoc body as shell code and reporting "bad substitution:
# no closing `)'" at the outer heredoc's start line. Building these as plain
# variables first avoids the parser quirk.
TOOL_RULE_REDDIT=""
TOOL_RULE_LINKEDIN=""
TOOL_RULE_TWITTER=""
if [ -z "$PLATFORM" ] || [ "$PLATFORM" = "reddit" ]; then
    TOOL_RULE_REDDIT="- Reddit Chat: use Python CDP scripts (scripts/reddit_browser.py, which attach to the reddit-harness Chrome on port 9557 via REDDIT_CDP_URL) for scanning/reading, fall back to the mcp__reddit-harness__bh_run tool for chat SPA operations. See lib/reddit-backend.sh BROWSER_INSTRUCTIONS for the Playwright -> bh_run translation table. Do NOT use mcp__reddit-agent__* (retired)."
fi
if [ -z "$PLATFORM" ] || [ "$PLATFORM" = "linkedin" ]; then
    TOOL_RULE_LINKEDIN="- LinkedIn Messages: use the mcp__linkedin-harness__bh_run tool ONLY (CDP-driven real Chrome on port 9556). Do NOT call /voyager/api/ endpoints, do NOT run raw Python CDP scripts against LinkedIn. See lib/linkedin-backend.sh BROWSER_INSTRUCTIONS for the Playwright -> bh_run translation table."
fi
if [ -z "$PLATFORM" ] || [ "$PLATFORM" = "twitter" ] || [ "$PLATFORM" = "x" ]; then
    TOOL_RULE_TWITTER="- X/Twitter DMs: use Python CDP scripts (scripts/twitter_browser.py) ONLY"
fi

PHASE0_BLOCK=""
if [ -n "$PHASE0_INSTRUCTIONS" ]; then
    PHASE0_BLOCK="$PHASE0_INSTRUCTIONS

---

After completing Phase 0 (human replies), proceed with the scanning and auto-reply phases below.
"
fi

# Human Reply Knowledge Base via /api/v1/human-dm-replies?mode=kb (HTTP-only).
HUMAN_REPLY_KB=$(python3 "$REPO_DIR/scripts/human_dm_replies_helper.py" kb --limit 20 2>/dev/null || echo "")

PHASE_A_BLOCK=""
if [ -z "$PLATFORM" ] || [ "$PLATFORM" = "reddit" ]; then
    IFS= read -r -d '' PHASE_A_BLOCK <<'PHASE_A_EOF' || true
## PHASE A: Scan Reddit for new messages

Reddit Chat inbounds were already ingested before you started (Phase A.0 in
this run's shell log — reddit_chat_sync.py ingest-unread). Every unread chat
room's last ~30 messages are already in dm_messages with Matrix event_id set,
partner usernames resolved, chat_urls backfilled, and dms.conversation_status
flipped to 'needs_reply' for new inbounds. You do NOT need to navigate
reddit.com/chat, click into any rooms, run scan_reddit_chat.js, or call
log-inbound for Reddit chat rooms. Doing so is wasted work and risks double-
counting (event_id dedup will block it but don't even try).

1. Scan the legacy Reddit inbox for comment replies and classic PMs:
   ```bash
   cd ~/social-autoposter && python3 scripts/reddit_browser.py unread-dms
   ```
   Returns JSON with: author, subject, preview_short, time, thread_url, type.
   Type = 'pm' or 'comment_reply' are the ones to handle here. Type='chat'
   entries from this script are unreliable (selector is stale) and should
   be IGNORED — chat rooms were already handled by Phase A.0.

2. Find Reddit conversations needing a reply (includes both newly-ingested
   chat rooms and any legacy PMs logged via step 1):
   ```bash
   cd ~/social-autoposter && python3 scripts/dm_conversation.py pending
   ```
   Scope to Reddit when needed via their_author + platform columns. This is
   the authoritative list; don't reconstruct it from sidebar scrapes.

3. For each Reddit PM/comment-reply surfaced by step 1 that isn't already in
   dms, create a row and log the inbound (chat rooms already have rows from
   Phase A.0):
   a. `ensure-dm` is idempotent — returns existing id if present, creates one
      if missing, and auto-links reply_id/post_id from their most recent
      public comment:
      ```bash
      cd ~/social-autoposter && python3 scripts/dm_conversation.py ensure-dm --platform reddit --author "USERNAME" --chat-url "THREAD_URL"
      ```
      Prints `DM_ID=<n>`. `THREAD_URL` must be the PM thread URL
      `https://old.reddit.com/message/messages/<id>`, NOT a post/subreddit
      URL. A validator rejects anything else; omit the flag if you don't
      have it.
   b. Log inbound (uses event-id dedup when available; plain content match
      otherwise):
      ```bash
      python3 scripts/dm_conversation.py log-inbound --dm-id DM_ID --author "USERNAME" --content "MESSAGE_TEXT"
      ```

For every Reddit chat room flagged as needs_reply by the `pending` query,
open it in the reddit-harness browser only to SEND a reply — not to read.
The conversation history is available via:
   ```bash
   python3 scripts/dm_conversation.py history --dm-id DM_ID
   ```
PHASE_A_EOF
fi

PHASE_B_BLOCK=""
if [ -z "$PLATFORM" ] || [ "$PLATFORM" = "linkedin" ]; then
    IFS= read -r -d '' PHASE_B_BLOCK <<'PHASE_B_EOF' || true
## PHASE B: Scan LinkedIn Messages for new messages

CRITICAL: use the mcp__linkedin-harness__bh_run tool for ALL LinkedIn browser work (CDP-driven real Chrome on port 9556). Do NOT call /voyager/api/ endpoints. Do NOT open individual post permalinks to scrape; stay inside the messaging UI. See lib/linkedin-backend.sh BROWSER_INSTRUCTIONS for the full Playwright -> bh_run translation table.

1. Navigate to https://www.linkedin.com/messaging/ : bh_run('new_tab("https://www.linkedin.com/messaging/"); wait_for_load()').
   Capture a screenshot (bh_run('print(capture_screenshot())')) and Read it. If the page is a login/checkpoint/verification challenge, STOP and print SESSION_INVALID, do not attempt to log in.

2. Extract the FULL list of conversations (read AND unread) with a single bh_run js() call. We need every visible thread's URL so we can backfill chat_url for historical DM rows, not just the unread cohort:

   bh_run('''
   print(js("""
     const items = [];
     const threads = document.querySelectorAll('a.msg-conversation-listitem__link, a[href*="/messaging/thread/"]');
     for (const a of threads) {
       const href = a.getAttribute('href') || '';
       if (!href.includes('/messaging/thread/')) continue;
       const container = a.closest('li, article') || a;
       const unreadBadge = container.querySelector('.notification-badge--show, [aria-label*="unread" i], [data-test-unread]');
       const text = (container.innerText || '').trim();
       const nameEl = container.querySelector('h3, .msg-conversation-listitem__participant-names');
       const partner = nameEl ? nameEl.textContent.trim() : '';
       items.push({
         thread_url: href.startsWith('http') ? href : ('https://www.linkedin.com' + href),
         partner,
         preview: text,
         unread: !!unreadBadge,
       });
     }
     return JSON.stringify(items);
   """))
   ''')

   Save the entire returned array (not just unread) to /tmp/linkedin_threads.json, then backfill chat URLs for any existing DM row still missing one (uses `author=partner`, `chat_url=thread_url`):
   ```bash
   python3 -c "import json,sys; d=json.load(open('/tmp/linkedin_threads.json')); print(json.dumps([{'author': r['partner'], 'chat_url': r['thread_url']} for r in d]))" \
     | python3 scripts/dm_conversation.py backfill-urls --platform linkedin
   ```

3. For each thread where unread is true:
   a. Navigate to thread_url: bh_run('goto_url("THREAD_URL"); wait_for_load()').
   b. Capture a screenshot and Read it. Read the last ~5 messages. Determine which are inbound vs from us.
   c. Identify the sender from the partner name.
   d. Ensure a DM row exists (idempotent, auto-links their prior LinkedIn comment engagement if any):
      ```bash
      cd ~/social-autoposter && python3 scripts/dm_conversation.py ensure-dm --platform linkedin --author "PERSON_NAME" --chat-url "THREAD_URL"
      ```
      Capture the `DM_ID=<n>` line for the next step. `THREAD_URL` must be `https://www.linkedin.com/messaging/thread/<id>/` (the value we scraped from `thread_url` in step 2, never a profile or feed URL). The validator refuses anything else.
   e. Log inbound messages:
      ```bash
      python3 scripts/dm_conversation.py log-inbound --dm-id DM_ID --author "PERSON_NAME" --content "MESSAGE_TEXT"
      ```

4. Do NOT aggressively scroll or click "Load earlier messages" in every thread. Only read what's immediately visible after the initial navigation. If the most recent inbound message is not visible, move on.
PHASE_B_EOF
fi

PHASE_C_BLOCK=""
if [ -z "$PLATFORM" ] || [ "$PLATFORM" = "twitter" ] || [ "$PLATFORM" = "x" ]; then
    IFS= read -r -d '' PHASE_C_BLOCK <<'PHASE_C_EOF' || true
## PHASE C: Scan X/Twitter DMs for new messages

1. Get ALL Twitter DM conversations visible in the sidebar (the script returns the full list, not only unread) and write them to /tmp/twitter_threads.json:
   ```bash
   python3 scripts/twitter_browser.py unread-dms > /tmp/twitter_threads.json
   ```
   This handles the encrypted DM passcode automatically (loaded from .env TWITTER_DM_PASSCODE).
   Returns JSON array with: author, handle, preview, time, thread_url, is_from_us, has_unread.

1a. Backfill chat URLs for any existing X DM row still missing one. Cheap, idempotent, fills buttons for historical rows whose chat is still in the sidebar:
   ```bash
   python3 -c "import json,sys; d=json.load(open('/tmp/twitter_threads.json')); print(json.dumps([{'author': r.get('handle') or r.get('author'), 'chat_url': r['thread_url']} for r in (d if isinstance(d, list) else [])]))" \
     | python3 scripts/dm_conversation.py backfill-urls --platform x
   ```

1b. **REQUIRED:** Filter the sidebar dump down to threads that actually need inspection. The filter combines sidebar signals (is_from_us, has_unread, time) with the DB's last outbound message_at to drop threads where we already sent the most recent message. This is what saves the run from $30+ in unnecessary `read-conversation` calls:
   ```bash
   python3 scripts/dm_conversation.py filter-inbox --platform x --file /tmp/twitter_threads.json > /tmp/twitter_threads_to_inspect.json
   ```
   The summary line goes to stderr (`in=N kept=M skipped=K (...breakdown...)`). The filtered JSON array on stdout contains only threads worth opening, each enriched with `_filter_reason` (sidebar_unread / no_db_row / outbound_older_than_window) and `_dm_id`. **Use this file as the inspection list in step 2, not the raw scan.**

2. For each conversation in `/tmp/twitter_threads_to_inspect.json`, read the full messages:
   ```bash
   python3 scripts/twitter_browser.py read-conversation "THREAD_URL"
   ```
   Returns JSON with: partner_name, partner_handle, messages (each with sender, content, time, is_from_us), total_found. Do NOT iterate over the raw `/tmp/twitter_threads.json` — that re-introduces the all-threads-every-cycle waste this filter exists to prevent.

3. For each conversation:
   a. Identify the sender from the partner_name/partner_handle
   b. **CRITICAL: Only log messages where is_from_us is false as inbound.** Skip our own messages.
   c. Ensure a DM row exists (idempotent, auto-links any prior public reply on X from this handle):
      ```bash
      cd ~/social-autoposter && python3 scripts/dm_conversation.py ensure-dm --platform x --author "PARTNER_HANDLE" --chat-url "THREAD_URL"
      ```
      Use the printed `DM_ID=<n>` for every subsequent log-inbound on this conversation. `THREAD_URL` must be `https://x.com/i/chat/<ids>` (the value from `thread_url` returned by `twitter_browser.py unread-dms`, never the tweet URL or the profile URL). The validator refuses anything else. If the filter step in 1b already attached `_dm_id`, you can skip this call for that thread.
   d. Log inbound messages:
      ```bash
      python3 scripts/dm_conversation.py log-inbound --dm-id DM_ID --author "PARTNER_HANDLE" --content "MESSAGE_TEXT"
      ```
   e. **REQUIRED at the end of every read-conversation, even when nothing was logged in step d** (e.g. you saw only old messages already in our DB, or only a reaction/typing-indicator). Stamp `dms.last_inspected_at` so the next cycle's filter-inbox knows we already verified this thread:
      ```bash
      python3 scripts/dm_conversation.py mark-inspected --dm-id DM_ID
      ```
      Skipping this on a "nothing new" thread is the bug that makes us re-open the same cold conversation every cycle. Always run it. The next filter-inbox will skip the thread for 24h unless a fresh inbound or outbound is logged in the meantime.
PHASE_C_EOF
fi

# Precompute the active reddit campaign suffix + sample_rate so the prompt
# can inline the literal text. If the LLM falls back to mcp__reddit-harness__bh_run
# (skipping the CDP path that injects the suffix at the tool layer), the
# literal value lets it append the suffix by hand at the documented rate.
# When no active campaign exists, both vars resolve to empty strings and the
# prompt's "if empty, do nothing extra" branch fires.
# HTTP-only via /api/v1/campaigns (scripts/dm_engage_helper.py). Both helper
# subcommands write with no trailing newline (matching the old `tr -d '\n'`).
REDDIT_CAMPAIGN_SUFFIX_LITERAL=$(python3 "$REPO_DIR/scripts/dm_engage_helper.py" reddit-campaign-suffix 2>/dev/null || echo "")
REDDIT_CAMPAIGN_SAMPLE_RATE=$(python3 "$REPO_DIR/scripts/dm_engage_helper.py" reddit-campaign-sample-rate 2>/dev/null || echo "")

PHASE_A_PROMPT=$(mktemp)
cat > "$PHASE_A_PROMPT" <<PROMPT_EOF
You are the Social Autoposter DM reply engagement bot.

Read $SKILL_FILE for content rules (tone, anti-AI detection, no em dashes).

## Task: Scan for new inbound DM messages and reply to continue conversations

CRITICAL - Tool rules:
$TOOL_RULE_REDDIT
$TOOL_RULE_LINKEDIN
$TOOL_RULE_TWITTER
NEVER use generic mcp__playwright-extension__*, mcp__isolated-browser__*, or mcp__macos-use__* tools.
If a script or tool call fails, wait 30 seconds and retry (up to 3 times).
CRITICAL: Reply in the SAME LANGUAGE as the inbound message. Match the language exactly.

## CRITICAL FAILURE MODE: platform-agent MCP tools not registered
If at the START of this run you cannot see ANY of the platform's browser tools available (mcp__twitter-harness__bh_run for Twitter, mcp__reddit-harness__bh_run for Reddit, mcp__linkedin-harness__bh_run for LinkedIn), OR every browser call fails with "MCP server not connected" / "no such tool" / similar, this is a transient infrastructure failure (Chrome profile collision, wedged MCP wrapper, lock acquired but profile still held by another process). It is NOT an error condition for the conversations in the queue.

Do EXACTLY this:
1. Make NO database changes. Do NOT mark any row as 'error', 'skipped', 'failed', or anything else. Do NOT call log-outbound, mark-skipped, set-status, mark-inspected, increment-attempts, or any other status-mutating helper. Do NOT write skip_reason on any dms/dm_messages/human_dm_replies row.
2. Print a single line to stdout: \`MCP_UNAVAILABLE: <platform>-agent tools not registered; aborting with rows untouched\`
3. Exit cleanly. The launchd schedule will retry on the next cycle, by which point the wedged profile holder will likely have timed out.

Burning rows with skip_reason='twitter_agent_mcp_unavailable: ...' on outbound DM rows is a regression that on 2026-05-12 nuked 7 warm leads (efemjoba, gpuops, josesaezmerino, AIDailyGems, alkimiadev, RobertDMellish, kunaljeweller). The LEFT JOIN d.id IS NULL filter in scan_dm_candidates.py then permanently blocked them from re-discovery (since fixed 2026-05-13 to treat transient skip_reasons as non-blocking, but the right behavior remains: do not write the row in the first place).

$PHASE0_BLOCK
Our projects (for context when conversations touch relevant topics):
$PROJECTS

## Human Reply Knowledge Base

Past human replies to escalated DMs (use as reference for tone and approach when handling similar conversations):
$HUMAN_REPLY_KB

$PHASE_A_BLOCK

$PHASE_B_BLOCK

$PHASE_C_BLOCK

## PHASE D: Reply to all conversations with pending inbound messages

After scanning, query for all conversations needing replies:
\`\`\`bash
cd ~/social-autoposter && python3 scripts/dm_conversation.py pending
\`\`\`

Known conversations from the database that already need replies:
$PENDING_CONVOS

## SURFACE — read the \`surface\` field on every row before composing

Each conversation has a \`surface\` field that tells you what kind of channel the inbound actually came from. The composing rules and tool routing differ. Do not skip this check.

- **\`surface\` = "real_dm"** — true private message thread (Reddit Chat, Reddit PM, LinkedIn Messages, X DM). \`chat_url\` is set. Replies go via the DM-send tool (Step 4). Treat this as a normal 1:1 DM conversation.

- **\`surface\` = "public_comment_chain"** — the inbound is a PUBLIC comment on one of our threads, not a private message. \`chat_url\` is null. \`origin_public_comment_url\` points to the comment that started this exchange and \`our_prior_public_reply_url\` points to our last public reply on that same thread. The engage_reddit / engage_linkedin pipelines are the primary handler for these threads, not this one. By the time you see this row here, our most recent public reply already exists (\`our_prior_public_reply_url\` is set). Default action: SKIP, no message, no stale flip — the public-reply pipeline owns it. ONLY post here when there's a clear reason this loop should escalate to a private DM (e.g. obvious buying intent in a public reply that warrants moving to DM). When you do escalate, route via the platform's DM tool and set the new chat_url with backfill-urls; never reply publicly from this loop.

## CLICK_SIGNAL inbound rows — read this before composing any reply

Some "inbound" rows in \`conversation_history\` are NOT typed messages from the prospect. They are backend signals inserted by the short-link redirector when the prospect silently clicks the booking URL we DM'd them. They look like:

\`\`\`json
{ "direction": "inbound", "author": "__click_signal__", "content": "[CLICK_SIGNAL] short link clicked" }
\`\`\`

If the LATEST inbound for a thread starts with \`[CLICK_SIGNAL]\`:

1. Treat as a soft trigger to nudge, not a typed message. Do NOT quote the signal back, do NOT acknowledge it, do NOT mention you know they clicked. The prospect must not know the click is observable. Phrases like "saw you checked out the link", "noticed you opened", "in case you missed it" are BANNED.
2. Compose a soft Mode A nudge, 1-2 sentences max, low-pressure. Acceptable shapes:
   - A genuine follow-up question tied to something specific from an EARLIER real inbound in conversation_history.
   - A casual "how's it going with X" referencing context they shared earlier.
   - If conversation_history has NO real inbound messages from them (we DM'd, they clicked, never replied), prefer SKIP. A cold-click nudge with no prior typed exchange reads as creepy.
3. Do NOT re-share the booking link. They already clicked it. Re-sending crosses into spam.
4. Do NOT push for a call, demo, or meeting in a click-driven nudge. Keep the door open, that's all.
5. The "ONE message per conversation per run" hard rule still applies. The "Last message is outbound -> SKIP" rule in Step 0 does NOT apply when the latest inbound is a CLICK_SIGNAL (the synthetic inbound IS the new signal that puts this row in the queue).
6. Idempotency is automatic: once you send your nudge, our outbound is newer than the click signal and the row drops out of the queue on the next cycle. If they click again later, a new \`[CLICK_SIGNAL]\` row is inserted and the thread re-enters the queue naturally.

## CORE PHILOSOPHY

Real conversations with real people. Sound like a person, not a bot. But rapport alone is not the goal: when the topic allows, ask the qualifying question, and share the booking link when the prospect qualifies.

## HARD RULES (violating any of these is a critical failure)

1. **ONE message per conversation per run.** Never send 2+ messages. If you already sent one, move on.
2. **NEVER send a message if the last message is outbound.** Check conversation_history first. If we sent last, SKIP entirely.
3. **Don't open with a pitch or lead with "btw I built..." / "actually I built..." / "I'm working on...".** Leading with a product pitch kills rapport in a cold DM. If your own product genuinely fits the conversation, recommend it and be honest that it's yours; never pose as an unaffiliated user.
4. **NEVER recommend a product in the first message.** Count the total messages. If there are fewer than 2 messages total (i.e. they haven't replied to us yet), stay in rapport-building mode. No links, no product names. Once they've replied at least once (2+ messages), recommend only if the conversation naturally leads there.
5. **NEVER fabricate context** like "I've been thinking about your question" or "started putting together a test suite" to create a pretext for sharing.

## DECISION FLOW (for each conversation)

### Step 0: Should we reply at all?
Check conversation_history. SKIP (do nothing, don't mark stale) if:
- Last message is already outbound (we sent last, waiting for their reply)
- Their message is a PURE polite brush-off with no new question/topic ("thanks", "cool", "will check it out", "good luck"). EXCEPTIONS — do NOT skip if EITHER (a) they thank you AND ask a new question, raise a new point, or float a new idea (even tentatively, even in ESL/translated register like "Thank you for your guidance, these data are encrypted, right?"); OR (b) **ICP rescue** — the thread is ICP-relevant and never qualified: the prospect (or the public post that started this DM) plausibly fits a project in \$PROJECTS, AND \`qualification_status\` is still \`pending\`, AND every prior outbound was \`mode=rapport\` (we never pitched, never asked a qualifier). On (b), reply ONCE with one natural Mode A qualifier per Step 2.5 — anchor it on something concrete from the thread or the original post (their stack, what they're shipping, the pain they hinted at), one casual texting-voice sentence, no "btw I built", no link, no pitch. Run Step 2.4 first to assign \`target_project\` if NULL; if the matched project has empty \`qualifying_questions\` AND empty \`must_have\`, synthesize a single qualifier from the conversation's domain instead of auto-disqualifying. After sending, set \`qualification_status = asked\` and let their next answer (or 14-day silence) decide qualified / disqualified / stale — not the brush-off itself. The rescue fires AT MOST ONCE per thread; if \`qualification_status\` is anything other than \`pending\`, the brush-off SKIP applies normally.
- Their message is a one-word/emoji response with nothing to respond to
- The conversation has no natural continuation (no open question from them, no new direction, no implicit invitation to keep going)
- \`qualification_status\` is already \`disqualified\` (we closed on a prior turn; do not generate fresh rapport on later inbounds)

### Step 1: Should a HUMAN handle this? (with booking link exception)

**BOOKING LINK AUTO-SHARE (config-driven):**
Booking links are stored in \`config.json\` per project and injected into the \$PROJECTS context block above. A project is auto-share eligible ONLY if BOTH fields are present:
- \`booking_link\`: the URL to share
- \`booking_link_auto_share: true\`: the flag authorizing the bot to send it

The DM row carries \`target_project\` (set at outreach time, from post or topic match) and a \`qualification_status\` (pending / asked / answered / qualified / disqualified). Use \`target_project\` first; if it's NULL, fall back to \`project_name\`. Never substitute another project's booking link.

If the matched project has \`booking_link_auto_share: true\` AND they just asked outright for a call/meeting/demo/scheduled time AND \`qualification_status\` is already \`qualified\` (or the prospect's own message plus the conversation+prospect profile clearly satisfy the project's \`must_have\` list and don't trigger any \`disqualify\` item):
- Do NOT flag for human.
- Compose the reply with the project's raw \`booking_link\` URL pasted verbatim from \$PROJECTS. The send tool wraps it transparently into \`https://<website>/r/<code>\` before typing, so click + booking attribution is automatic. \`booking_link_sent_at\` auto-stamps on first wrap. Mint is idempotent — re-runs of the same DM + same URL reuse the existing code. (For LinkedIn DMs only, the wrap happens via an explicit pre-send step in Step 4; for Reddit/Twitter the browser tools wrap in-process.)
- Example shape: "yeah for sure, here's a link to grab a time: <booking_link from config>"
- After sending, lock the project and raise tier (booking_link_sent_at is already stamped by the wrap pipeline; explicit mark-booking-sent below is idempotent and serves as audit-trail belt-and-suspenders):
  \`\`\`bash
  python3 scripts/dm_conversation.py set-project --dm-id DM_ID --project "EXACT_PROJECT_NAME_FROM_CONFIG"
  python3 scripts/dm_conversation.py set-target-project --dm-id DM_ID --project "EXACT_PROJECT_NAME_FROM_CONFIG"
  python3 scripts/dm_conversation.py set-tier --dm-id DM_ID --tier 3
  python3 scripts/dm_conversation.py set-qualification --dm-id DM_ID --status qualified --notes "ASKED_FOR_CALL"
  python3 scripts/dm_conversation.py mark-booking-sent --dm-id DM_ID
  \`\`\`

If they asked for a call/demo but \`qualification_status\` is still \`pending\` or \`asked\` AND the must_have/disqualify bar isn't clearly met yet, do NOT share the booking link yet — drop into Step 2.5 and qualify first, or compose a Mode A rapport reply that surfaces the qualifying question naturally.

If the conversation's matched project has a \`booking_link\` but \`booking_link_auto_share\` is false (or the field is missing), the bot must NOT share that link automatically — flag for human instead.

**Flag for human (do NOT auto-reply) if:**
- They asked for a call/meeting/demo BUT the matched project lacks \`booking_link_auto_share: true\` in config
- They asked for a call/meeting/demo AND the prospect clearly fails the project's \`disqualify\` list (e.g., competitor, wrong geography, wrong scale) — a human needs to decide whether to decline
- They invited us to a podcast, interview, or event
- They offered a collaboration or business proposal
- They asked to move to another platform (Telegram, email, etc.)
- They need a specific personal commitment ("when are you free?", "can you demo this?") that isn't a booking link scenario
- They asked about pricing or business terms (UNLESS config has pricing for that project — then answer from config)
- Their LATEST inbound message expresses distress about themselves (e.g., "I'm burned out", "nothing works for me"). Philosophical or polemical arguments do not qualify even if the surrounding thread title uses dark language. Subreddit titles, other users' comments, and the broader thread context never trigger this rule on their own.
- The conversation is 8+ messages deep and going really well (high-value relationship) AND isn't a booking link scenario
- You're not sure how to respond authentically

**NEVER flag for human (set interest to not_our_prospect and skip instead) if:**
- They are pitching US a product, service, offer, agency, or partnership deal
- They treat us as a potential buyer/customer for their thing
- They offered a call/demo/meeting but the call is about THEIR work, THEIR product, or THEIR workflow — not a buying interest in one of our products
- They work in an unrelated domain with no realistic fit to our project list
- Peer/colleague chatter with no buyer signal
- Philosophical, political, or polemical pushback about ideas (meditation, productivity, AI, religion, etc.). Human escalation is for actionable requests (calls, business proposals, personal decisions we have to make), not a prospect disagreeing with an idea. Classify under general_discussion or declined and let Step 2 compose a Mode A rapport reply, or skip if not our prospect.
These waste inbox attention. Set \`--interest not_our_prospect\`, skip the reply, and move on.

\`\`\`bash
cd ~/social-autoposter && python3 scripts/dm_conversation.py flag-human --dm-id DM_ID --reason "REASON"
\`\`\`
Then SKIP. Do NOT reply.

### Step 2: Compose the reply (rapport OR product mention + booking link)

Every reply is one of two modes. Pick the mode based on the timeline rule and Mode B triggers below; everything else in this step (2.4 through 2.8) then shapes the content.

- **Mode A (rapport)**: conversational/continuation reply. No product name, no GitHub link, no booking link.
- **Mode B (product pivot)**: the reply introduces the best-fit project from \$PROJECTS, with the booking link appended when Step 2.8 eligibility is met.

**TIMELINE RULE (overrides the default):**
- Total messages 1-3 in the thread: Mode A is the default. Switch to Mode B only if the Mode B triggers in Step 2.7 are already met.
- Total messages 4+: you MUST be in Mode B. Either mention the best-fit product naturally OR, if no project in \$PROJECTS plausibly fits this prospect, set \`qualification_status = disqualified\` per Step 2.5 with a one-line reason and stay in Mode A permanently.

  **Auto-disqualify triggers — narrowed.** A "peer-shaped" or "technical-discussion" thread is NOT by itself a reason to disqualify. Engineers, hobbyist OSS contributors, teachers/coaches, and community runners can ALSO be buyers; we cannot tell from rapport alone. Before flipping to \`disqualified\`, you MUST have asked at least one qualifying question per Step 2.5 (one of the project's \`qualifying_questions\` suggestions, your own qualifier off a \`must_have\` item, or a synthesized qualifier from \`must_have\` if no list is configured) and seen their answer. Only flip to \`disqualified\` when one of these is unambiguously true on the *answered turn*:
  - their reply explicitly confirms a config-level \`disqualify\` item (e.g. "I work at QA Wolf", "we ship native iOS only", "I'm a student with no production app");
  - their reply explicitly disclaims the \`must_have\` (e.g. "no, I'm not actually shipping anything", "I just teach this, I don't build anymore");
  - they're currently shipping a competing **commercial** product in the same space and asking for our contributions/roadmap input.
  Otherwise: stay at \`asked\` or \`answered\` and either ask one short clarifier or stay in Mode A rapport. The default for ambiguous peer-pattern threads is **ask, don't dismiss**.

  If target_project exists in \$PROJECTS and has no \`qualifying_questions\` AND no \`must_have\` (truly empty qualification block), only then may you set \`disqualified\` here with reason "no qualification config — cannot ask"; this should be rare.

  Do NOT let a thread drift past 3 outbound turns of pure rapport without either pitching, asking the qualifier, or disqualifying *post-answer*. \`scripts/dm_conversation.py log-outbound\` enforces this with a TIMELINE BLOCKED error once msg_count >= 3 and both qualification_status=pending and icp_matches=[].

Total messages = inbound + outbound count in this thread (use conversation_history length). The message you're about to send counts, so by the time you send message 4 it must be a Mode B pivot (or a Mode A rapport reply after a disqualification has been recorded).

**SHARED STYLE RULES (both modes):**
- Be 1-3 sentences max. Texting style, not essay style.
- Reference something SPECIFIC from their message (proves you read it).
- Either share a relevant experience, ask a genuine follow-up question, or acknowledge what they said.
- Sound like a real person texting, not a customer success bot.
- No em dashes. Lowercase is fine. Casual.
- NEVER repeat a question or point you already made in a previous message.

COMMITMENT GUARDRAILS (never violate these in any reply):
- NEVER suggest, offer, or agree to calls, meetings, demos, or video chats UNLESS the matched project in \$PROJECTS has \`booking_link_auto_share: true\` AND \`qualification_status = qualified\` on the DM row. In that case append the config booking_link to the Mode B reply per Step 2.8. Otherwise keep it in the DM.
- NEVER agree to podcast appearances, X Spaces, interviews, or live events.
- NEVER offer to move to another platform (Telegram, Discord, email, etc.). Stay in this DM thread.
- NEVER promise to share specific links or resources you don't have right now in config.json projects.
- NEVER make time-bound commitments ("this week", "tomorrow", "Thursday").
- NEVER share location ("I'm in SF") or personal details not in config.json.
- If they push for any of the above, deflect naturally: "honestly easier to hash it out here" or ask a follow-up question to keep the convo going in the DM.

### Step 2.4: Rescore ICP against every project (and optionally switch target_project)

Before the qualification funnel, rescore this prospect against EVERY project listed in \$PROJECTS that has qualification criteria. A conversation can reveal new facts (role, company, domain), and the best-fit project may have changed since the initial scan.

For each project in \$PROJECTS with qualification criteria:
- Compare the prospect profile (headline, company, role, bio, recent_activity, notes) + conversation history against that project's \`must_have\` and \`disqualify\` lists.
- Pick one label: icp_match, icp_miss, disqualified, unknown.
- Upsert one entry per project:
  \`\`\`bash
  python3 scripts/dm_conversation.py set-icp-precheck \\
      --dm-id DM_ID --project PROJECT_NAME --label LABEL --notes "SHORT_RATIONALE"
  \`\`\`
  Repeat for every project. Each call upserts one entry in dms.icp_matches (JSONB array) keyed by project.

Then decide whether to switch \`target_project\`:
- If a project OTHER than the current target_project scores \`icp_match\` AND the current target_project scores \`icp_miss\` or \`disqualified\`, switch target_project to that better-fit project.
- If nothing scores \`icp_match\`, leave target_project alone.
- If the current target_project is NULL and a project scores \`icp_match\`, set it to that project.
- When switching, log the change:
  \`\`\`bash
  python3 scripts/dm_conversation.py set-target-project --dm-id DM_ID --project NEW_PROJECT
  python3 scripts/dm_conversation.py set-qualification --dm-id DM_ID --status \$CURRENT_STATUS --notes "target: OLD -> NEW (reason: SHORT_WHY)"
  \`\`\`

The qualification funnel in Step 2.5 then runs against the (possibly updated) target_project.

### Step 2.5: Qualification funnel (runs whenever target_project has \`qualifying_questions\` OR a \`must_have\` block)

Goal: before we ever drop a booking link or pitch, OR before we disqualify a peer-shaped thread, know whether the prospect matches the project's must_have list and doesn't trigger the disqualify list. Do this as a natural conversational question, not a form.

Pull the matched project's \`qualifying_questions\` (list of 3 example questions, see below), \`must_have\`, and \`disqualify\` from the \$PROJECTS block. If the DM has no target_project (and no project_name) AND message_count < 4, skip this step entirely; Step 2 already produced a rapport reply. If message_count >= 4 and Step 2.4 couldn't assign a target_project, set \`qualification_status = disqualified\` here with a one-line reason like "no product fit after rescore" and send a short Mode A close in Step 2 — do NOT keep generating substantive rapport turn after turn.

**\`qualifying_questions\` are SUGGESTIONS, not mandates.** Each project ships 3 example questions hitting different angles (stack/setup, scale/scope, pain/intent). Your job is to:
1. Pick the ONE that fits this specific conversation best (e.g. they already mentioned their stack → skip the stack question and pick the scale or pain one).
2. Paraphrase it into your own texting voice — never paste verbatim.
3. OR, if none of the 3 fits naturally given what's been said, write a different one-sentence qualifier that probes a must_have item.
4. OR, if asking any qualifier right now would feel forced or interrogative (e.g. you have nothing concrete to anchor it to yet), DO NOT ask. Stay in Mode A rapport. Asking is encouraged but never required on a specific turn — the only hard rule is that you must have asked at least once before flipping to \`disqualified\`.

If the target project has \`must_have\` but the \`qualifying_questions\` list is missing or empty, synthesize one from the first \`must_have\` item:
- Assrt must_have "engineering team or solo founder shipping a web app with real users that needs end-to-end test coverage" → "you maintaining e2e tests on a web app that's already in front of real users, or skipping that layer right now?"
- Cyrano must_have "property manager or HOA board running multifamily" → "you running operations across multiple units, or just looking out for your own place?"
- Generic shape: take the first must_have, strip the role label, turn it into one casual yes/no-ish question.

Never paste anything verbatim. One sentence. One question. Texting voice.

Branch on \`qualification_status\` of the DM row:

1. \`pending\` → we have never asked yet.
   - If fewer than 2 total messages exist, do NOT ask yet. Stay in Mode A rapport from Step 2.
   - Otherwise look at the project's \`qualifying_questions\` list (3 suggestions). Pick the ONE that best fits the *current* conversation:
     - If they've already revealed their stack/tools, skip the stack question and pick the scale or pain one.
     - If they've already revealed scale, pick the pain or stack one.
     - If they've revealed nothing concrete, the stack/setup question is usually the safest opener.
     Paraphrase your pick into one casual texting-voice sentence. Never paste verbatim. Never stack two of them in the same reply.
   - If none of the 3 suggestions fits naturally given what's been said, you may write your own one-sentence qualifier that probes a \`must_have\` item, OR (if no \`qualifying_questions\` list exists) synthesize one from the first \`must_have\` item per the Step 2.5 header.
   - **Asking is optional on any given turn.** If asking would feel forced (no anchor in the inbound, conversation hasn't surfaced anything to attach to, you'd be jumping out of register), it's fine to stay in Mode A rapport this turn and try again next turn. Leave \`qualification_status = pending\` and don't log an ASKED note.
   - **Peer-pattern guard**: if the conversation reads as substantive peer-to-peer technical discussion in the project's domain (engineering war stories, ops/testing/AI workflow discussion, sharing their own setup), DO NOT auto-disqualify. Peers are often buyers in disguise. Ask the qualifier first (or wait for a better anchor and ask later); flip to \`disqualified\` only after their *answer* makes the no-fit clear (per the narrowed triggers in Step 2's TIMELINE RULE).
   - **TIMELINE backstop**: by the 4th total message you SHOULD have either asked one of the qualifiers, or — if nothing in \$PROJECTS plausibly fits AND no must_have can be turned into a qualifier — set \`qualification_status = disqualified\` with a one-line reason and stop pitching. If a fit *exists* but you've genuinely had no place to land the question yet, you may carry pending one more turn; don't disqualify just to satisfy the deadline.
   - When you do ask, after sending, mark status as \`asked\`:
     \`\`\`bash
     python3 scripts/dm_conversation.py set-qualification --dm-id DM_ID --status asked --notes "ASKED: short paraphrase of what we asked"
     \`\`\`

2. \`asked\` → we already asked on a prior turn. The inbound we're processing now is (usually) their answer. Evaluate:
   - Read their latest inbound message plus the prospect profile fields (headline, bio, company, role, recent_activity, notes) that are attached to the DM row.
   - Cross-check against the project's \`must_have\` and \`disqualify\` lists from \$PROJECTS.
   - Set status to \`qualified\` ONLY if EITHER (A) OR (B) holds, AND no disqualify item is triggered:

     **(A) Role/persona fit + soft engagement co-signal**
     - must_have role/persona is satisfied: they ARE the target persona, OR they explicitly know/work with someone who is, OR their stated role plausibly maps to the use case.
     - AND they are meaningfully engaged in the conversation: substantive replies, asking technical questions, sharing setup details, comparing approaches. NOT one-word acks ("cool", "thanks", "will check it out") or polite brush-offs.

     **(B) Explicit try/buy intent (regardless of role)**
     - "how do I install / get access / sign up"
     - "what does it cost / pricing"
     - "I want to try this" / "send me a link"
     - "can you demo this on my stack"

     Neither path is satisfied by interest signals alone ("looks cool", "starred", "dope stuff", "wanna take this to the DMs" while already in DMs, "I'll check it out"). Those are interest, not qualification. If the prospect fits role-wise but shows only soft interest without substantive engagement, leave status at \`asked\` and stay in rapport.
   - If they trigger ANY disqualify item: set status to \`disqualified\` with the rationale.
   - If the answer is ambiguous, vague, or off-topic: set status to \`answered\` and compose ONE follow-up clarifier in Step 2. Do not keep grinding; 1 follow-up max before letting it rest.
     \`\`\`bash
     python3 scripts/dm_conversation.py set-qualification --dm-id DM_ID --status qualified --notes "SHORT_REASON"
     # or --status disqualified --notes "SHORT_REASON"
     # or --status answered  --notes "SHORT_REASON"
     \`\`\`

3. \`answered\` → we already asked a clarifier on the prior turn. Evaluate now and land on \`qualified\` or \`disqualified\`; do not ask a third time.

4. \`qualified\` → proceed to Step 2.8 (auto-share the booking link if eligible).

5. \`disqualified\` → send a short, polite close (1-2 sentences, no new open question), never the booking link, never a pitch. Then in Step 5b set \`--interest not_our_prospect\` AND run \`set-status --status stale\` so later inbounds don't resurface the thread. If the row is ALREADY \`disqualified\` on entry to this step (we closed on a prior turn and they replied again), SKIP entirely in Step 0 rather than generating fresh rapport.

### Step 2.6: Use prospect profile context in the reply

The DM payload now includes prospect_headline, prospect_bio, prospect_company, prospect_role, prospect_recent_activity, and prospect_notes (best-effort; some may be NULL). If any of these are set, weave at most ONE specific detail into your reply so it sounds like we know who they are (e.g., referencing their company's stage, the role they hold, or a recent thing they shipped). Never dump the whole profile; never mention that we scraped it. If the fields are all NULL, don't apologize, just write the reply without.

### Step 2.7: Mode B triggers and product mention

You are in Mode B on this turn if EITHER of the following is true:
- **Organic trigger**: they described a problem a project in \$PROJECTS solves, OR they asked for tool recommendations.
- **Timeline trigger (Step 2)**: total messages in this thread are 4+. By this point Mode B is mandatory unless the DM is already \`qualification_status = disqualified\`, in which case stay in Mode A permanently.

AND ALL of the following are true (floor conditions, never pivot without them):
- There are 2+ total messages in the conversation (they have replied at least once). This mirrors HARD RULE 4.
- The mention fits naturally in the reply without any "btw" or topic change.
- You would genuinely recommend this tool to a friend in their situation.
- If target_project has \`qualifying_questions\` (or a \`must_have\`), \`qualification_status\` is NOT \`disqualified\`.

If none of these conditions hold AND the thread is 4+ messages, the timeline trigger still forces a decision: either find a plausible fit and pivot, or set \`qualification_status = disqualified\` via Step 2.5 and send a Mode A reply. Do NOT keep ping-ponging rapport forever.

Pick the best-fit project from \$PROJECTS using the (possibly updated) target_project from Step 2.4. If sharing a link, embed it in a natural sentence. Never make the link the point of the message.
Good: "yeah there's this tool terminator that does that, github.com/mediar-ai/terminator - the accessibility API approach avoids the screenshot reliability issues you mentioned"
Bad: "btw I built a tool for that, check out github.com/mediar-ai/terminator if you're curious"

**Universal link wrapping**: every URL you paste — booking links, GitHub repos, blog posts, our own website pages, third-party references — is auto-wrapped by the send tool into \`https://<website>/r/<code>\` for click attribution. You paste raw URLs in your reply (the GOOD/BAD examples above show raw URLs for readability); the tool replaces them with wrapped versions before typing. **Constraint**: if you mention a URL pointing at a project not currently in \`target_projects[]\`, the send tool refuses with \`link_wrap_failed\` / \`needed_project=X\`. Before retrying, append the project to the union:
\`\`\`bash
python3 scripts/dm_conversation.py set-target-project --dm-id DM_ID --append --project "X"
\`\`\`
This is by design: any link in a reply is, by definition, a target project for that thread. The union grows; we never remove projects.

When you pivot to Mode B (with or without a booking link), stamp project and tier:
\`\`\`bash
python3 scripts/dm_conversation.py set-project --dm-id DM_ID --project "EXACT_PROJECT_NAME_FROM_CONFIG"
python3 scripts/dm_conversation.py set-target-project --dm-id DM_ID --project "EXACT_PROJECT_NAME_FROM_CONFIG"
python3 scripts/dm_conversation.py set-tier --dm-id DM_ID --tier 2
\`\`\`
\`set-target-project\` always extends \`target_projects[]\` (deduped) so future link wraps for this project succeed without an extra \`--append\`. \`set-tier\` auto-stamps \`first_product_mention_at = NOW()\` on first transition to tier >= 2. Soft pivots (category named, product name deferred) stay at tier 1 until the next turn; see PIVOT EXAMPLES below.

### Step 2.8: Append the booking link (Mode B only, when eligible)

If all of the following are true, append the project's booking link to the Mode B reply you're about to send:
- \`qualification_status = qualified\` for this DM (set in Step 2.5 or earlier)
- The matched project has \`booking_link\` AND \`booking_link_auto_share: true\` in config
- \`booking_link_sent_at\` is NULL (we haven't already sent it)
- The conversation is at a natural place to propose a call (they've surfaced pain or asked for more; not just "cool"). You do NOT need them to have explicitly asked for a call; see the updated COMMITMENT GUARDRAILS in Step 2.

Paste the raw \`booking_link\` from \$PROJECTS verbatim. The send tool wraps it into \`https://<website>/r/<code>\` automatically for click + booking attribution. \`booking_link_sent_at\` auto-stamps on first wrap; the legacy \`mark-booking-sent\` call below is idempotent (audit-trail belt + suspenders).

Phrase it naturally, one sentence, link embedded:
"makes sense, if you want to see how it'd work on your setup, grab a time here: <booking_link>"

After sending, stamp project + tier 3 + mark booking sent (runs in addition to the Step 2.7 set-project/set-tier; set-tier 3 supersedes tier 2):
\`\`\`bash
python3 scripts/dm_conversation.py set-project --dm-id DM_ID --project "EXACT_PROJECT_NAME_FROM_CONFIG"
python3 scripts/dm_conversation.py set-target-project --dm-id DM_ID --project "EXACT_PROJECT_NAME_FROM_CONFIG"
python3 scripts/dm_conversation.py set-tier --dm-id DM_ID --tier 3
python3 scripts/dm_conversation.py mark-booking-sent --dm-id DM_ID
\`\`\`

Never send the booking link twice. If \`booking_link_sent_at\` is not NULL, Step 2.8 is a no-op; let Step 2.7 handle any tool mention normally. (The wrap pipeline is also idempotent — re-wrapping the same URL for the same DM returns the existing code rather than minting a new one.)

### Step 3.5: ACQUIRE the reddit-browser lease (REDDIT DMs ONLY)

**Reddit only.** Skip this step entirely for LinkedIn and Twitter DMs.

Before driving any reddit browser tool (mcp__reddit-harness__bh_run or the Python CDP scripts) for THIS DM, acquire the reddit-browser lease. The lease is held only around THIS DM's Step 4 + Step 5; the next DM acquires its own. Peer reddit pipelines (run-reddit-search post phase, engage-reddit, link-edit-reddit, dm-outreach-reddit) can use the browser between our DMs.

\`\`\`bash
LOCK_OUT=\$(python3 ~/social-autoposter/scripts/reddit_browser_lock.py acquire --timeout 600 --ttl 90 2>&1)
\`\`\`

- If \`LOCK_OUT\` starts with "OK", you hold the lease. Proceed to Step 4.
- If "BUSY", a peer reddit pipeline owns the browser and didn't release within 10 min. Treat as a TRANSIENT skip: log nothing, do NOT call Step 4, move on to the NEXT DM. The conversation stays pending and the next launchd cycle will retry. Do NOT attempt Step 4 without the lease; collisions on the same Chrome profile crash both runs.

\`scripts/reddit_browser.py\` heartbeats the lease on every CDP subprocess, so on the PRIMARY (Python CDP) path you do NOT need to manually heartbeat. The mcp__reddit-harness__bh_run fallback does NOT auto-heartbeat (the harness MCP has no lock-proxy wrapper, unlike the retired reddit-agent), so keep any bh_run fallback brief: a single compose + send completes well within the 90s TTL. Just acquire before Step 4's first browser call for the DM and release in Step 5.5 below.

### Step 4: Send the reply

Reddit dms split into two surfaces (pick by whether \`chat_url\` is set on the dms row):

**Reddit Chat** (chat_url set; true DM — try CDP first, fall back to mcp__reddit-harness__bh_run browser):
\`\`\`bash
cd ~/social-autoposter && python3 scripts/reddit_browser.py send-dm "CHAT_URL" "YOUR_REPLY_TEXT" DM_ID
\`\`\`
Pass the conversation's DM_ID as the third positional arg so the tool can self-log the outbound (some rows have empty chat_url which would otherwise miss). The tool may append a campaign suffix to the message before typing; trust its return — \`message_sent\` is what was actually delivered.
If the CDP script returns {ok:false} (Reddit Chat SPA may not render via CDP), fall back to using the mcp__reddit-harness__bh_run tool (CDP-driven real Chrome on port 9557; see lib/reddit-backend.sh BROWSER_INSTRUCTIONS for the Playwright -> bh_run translation table). **The CDP path wraps URLs into /r/<code> automatically; the MCP fallback bypasses that wrap, so any project URL in the reply must be wrapped here explicitly or click attribution is lost. A 7d audit found 41 real-DM messages mentioning project URLs and ZERO of them produced a minted short link, almost entirely traceable to this fallback gap.** **STDERR_FALLBACK_MARKER (REQUIRED FIRST STEP):** before doing anything else on the MCP fallback path, emit a single line to stderr exactly: \`[reddit-chat-mcp-fallback] dm_id=DM_ID reason="<short>"\`. This is the only stable trace we have for fallback frequency; cron logs are grepped for this marker.

0. **Pre-send URL wrap (REQUIRED if YOUR_REPLY_TEXT contains any URL, bare-domain shorthands like fazm.ai included).** Run the same wrap CLI the LinkedIn flow uses:
   \`\`\`bash
   WRAPPED_TEXT=\$(python3 scripts/dm_short_links.py wrap-text --dm-id DM_ID --text "YOUR_REPLY_TEXT" 2>/tmp/dm_wrap_err)
   if [ \$? -ne 0 ]; then
     cat /tmp/dm_wrap_err >&2
     # Common failure: target_project_required. Append the project to the union and retry:
     #   python3 scripts/dm_conversation.py set-target-project --dm-id DM_ID --append --project "X"
     # Then re-run the wrap-text command above.
     exit 1
   fi
   \`\`\`
   Use \$WRAPPED_TEXT (NOT YOUR_REPLY_TEXT) as the input to browser_type AND as the \`--content\` for the post-send log-outbound. If YOUR_REPLY_TEXT has no URLs, wrap-text returns it unchanged; running it is still cheap and harmless, so always run it.
1. Navigate to the chat room (use chat_url if available, or find via sidebar)
2. Type \$WRAPPED_TEXT in the message input
3. Press Enter to send
4. **Campaign suffix on the MCP fallback:** if $REDDIT_CAMPAIGN_SUFFIX_LITERAL is non-empty (the shell precomputes it from the active reddit campaign), flip a coin AT $REDDIT_CAMPAIGN_SAMPLE_RATE and, on heads, type the suffix verbatim at the end of the message before pressing Enter. The CDP path does this automatically; the MCP fallback must do it by hand or the campaign attribution is lost.

**Reddit comment reply** (chat_url empty / null; the inbound is a public comment, not a chat message — use the CDP path so suffix injection runs at the tool layer):
\`\`\`bash
cd ~/social-autoposter && python3 scripts/reddit_browser.py reply "COMMENT_PERMALINK" "YOUR_REPLY_TEXT" DM_ID
\`\`\`
Pass DM_ID as the third positional arg so the tool logs to dm_messages with auto-attribution. The tool injects the active campaign suffix at \`sample_rate\`; \`reply_text\` in the JSON return is what was actually posted. \`COMMENT_PERMALINK\` is the inbound comment URL on reddit.com (the tool normalizes to old.reddit.com internally).
If CDP returns {ok:false, error:"subreddit_blocked"}, the comment is in a sub on \`subreddit_bans.comment_blocked\` and the tool has already auto-closed the DM (when dm_id was passed). Treat this as a clean SKIP — do NOT fall back to MCP, do NOT flag-human, do NOT retry. Move on to the next conversation.
If CDP returns {ok:false} with any other non-recoverable error, fall back to the mcp__reddit-harness__bh_run tool (CDP-driven real Chrome on port 9557; see lib/reddit-backend.sh BROWSER_INSTRUCTIONS for the Playwright -> bh_run translation table) to type the reply on the post page. On the MCP fallback path, the same Step-4 suffix rule applies — if $REDDIT_CAMPAIGN_SUFFIX_LITERAL is set, append it verbatim at $REDDIT_CAMPAIGN_SAMPLE_RATE before submitting; if $REDDIT_CAMPAIGN_SUFFIX_LITERAL is empty, do nothing extra. **Also: emit \`[reddit-comment-mcp-fallback] dm_id=DM_ID reason="<short>"\` to stderr first, then run the same Pre-send URL wrap step (\`python3 scripts/dm_short_links.py wrap-text --dm-id DM_ID --text "YOUR_REPLY_TEXT"\`) and use \$WRAPPED_TEXT as the input to browser_type. The wrap is cheap and harmless on URL-free replies; required when any URL is present so click attribution survives.**

**LinkedIn Messages** (mcp__linkedin-harness__bh_run tool ONLY, no raw Python CDP, no /voyager/api/. See lib/linkedin-backend.sh BROWSER_INSTRUCTIONS for the Playwright -> bh_run translation table):

0. **Pre-send URL wrap (REQUIRED if YOUR_REPLY_TEXT contains any URL).** LinkedIn types via the harness without a Python pre-pass, so the wrap step that reddit/twitter handle in-process must happen explicitly here:
   \`\`\`bash
   WRAPPED_TEXT=\$(python3 scripts/dm_short_links.py wrap-text --dm-id DM_ID --text "YOUR_REPLY_TEXT" 2>/tmp/dm_wrap_err)
   if [ \$? -ne 0 ]; then
     cat /tmp/dm_wrap_err >&2
     # Common failure: target_project_required. Append the project to the union and retry:
     #   python3 scripts/dm_conversation.py set-target-project --dm-id DM_ID --append --project "X"
     # Then re-run the wrap-text command above.
     exit 1
   fi
   \`\`\`
   Use \$WRAPPED_TEXT (NOT YOUR_REPLY_TEXT) as the text you type via type_text AND as the \`--content\` for the post-send log-outbound. If YOUR_REPLY_TEXT has no URLs, wrap-text returns it unchanged; running it is still cheap and harmless, so always run it.
1. Navigate to THREAD_URL: bh_run('goto_url("THREAD_URL"); wait_for_load()').
2. Capture a screenshot (bh_run('print(capture_screenshot())')) and Read it. If you see login, captcha, or checkpoint, STOP and print SESSION_INVALID. Do not attempt to re-login.
3. Find the message input (contenteditable, aria-label typically "Write a message"). Compute its center from getBoundingClientRect via js(), click_at_xy(X, Y) to focus it, then bh_run('type_text(...)') with \$WRAPPED_TEXT.
4. Click the Send button (aria-label "Send", role=button): compute its center via js() and bh_run('click_at_xy(X, Y)'). Do NOT press Enter to send (Enter inserts a newline in LinkedIn's contenteditable).
5. Capture a screenshot and confirm the message appears in the thread as the newest outbound bubble. If not visible, mark this convo as failed (do not retry more than once per run).

**X/Twitter DMs** (Python CDP script, no browser MCP needed):
\`\`\`bash
cd ~/social-autoposter && python3 scripts/twitter_browser.py send-dm "THREAD_URL" "YOUR_REPLY_TEXT" DM_ID
\`\`\`
Pass the conversation's DM_ID as the third positional arg so the tool can self-log the outbound and auto-attribute to any active Twitter campaign whose suffix it appended. Returns JSON with {ok: true, thread_url, verified, applied_campaigns, message_sent} on success. \`message_sent\` is the literal text that landed (which may include the campaign suffix appended at \`sample_rate\`). Handles the encrypted DM passcode automatically.
On {ok:false}, treat these errors as TERMINAL for this run: \`rate_limited\`, \`conversation_not_found_in_sidebar\`, \`message_box_not_found\`, \`tweet_not_found\`. They mean platform-level state we can't fix mid-cycle. SKIP the conversation, do NOT retry, do NOT flag-human, do NOT log-outbound. The next launchd cycle handles its own backoff. The generic "retry up to 3 times" rule does NOT apply to these errors — retrying a rate_limited burns more X-side budget.

### Step 5: Log the reply
\`\`\`bash
cd ~/social-autoposter && python3 scripts/dm_conversation.py log-outbound --dm-id DM_ID --content "MESSAGE_SENT_FROM_TOOL_JSON" --verified
\`\`\`
Use \`message_sent\` from the send-dm JSON as the content (NOT YOUR_REPLY_TEXT) so any campaign suffix the tool appended is captured, enabling auto-attribution. For Twitter, the send-dm helper has already self-logged when DM_ID was passed; this call is dedup-protected and acts as a no-op confirmation. Pass --verified ONLY when the browser tool returned verified=true (or you visually confirmed the message in the thread). The flag is a hard gate: log-outbound refuses to insert without it. If verification failed, log nothing and let the next cycle retry; never pass --verified speculatively. The log-outbound command also has a dedup guard. If it says "DEDUP BLOCKED" or "VERIFY BLOCKED", the message was NOT logged. Do not retry.

### Step 5.5: RELEASE the reddit-browser lease (REDDIT DMs ONLY)

**Reddit only.** Skip this step entirely for LinkedIn and Twitter DMs.

RELEASE the reddit-browser lease IMMEDIATELY after the DM result is logged (success, error, or skip). This is mandatory; failing to release blocks every other reddit pipeline for up to 90s of TTL idle decay:

\`\`\`bash
python3 ~/social-autoposter/scripts/reddit_browser_lock.py release
\`\`\`

Run this even if Step 4 raised, the send threw, or Step 5 failed. Wrap the per-DM browser block (Step 4 + Step 5) in a way that Step 5.5 ALWAYS executes (mental try/finally). The release is idempotent and safe to call multiple times. Move to the NEXT DM only after the release. Step 5b classify/log work below is DB-only (no browser) and runs unlocked, so peer pipelines can use the browser during it.

### Step 5b: Classify interest level AND mode (REQUIRED on every reply)

After replying (or deciding to SKIP/flag/stale), classify two things and write BOTH. These are separate commands, one call each, every turn, no exceptions.

**(i) interest_level**: the prospect's signal, based on their LATEST inbound plus the full arc. Can go up or down as the conversation evolves.

\`\`\`bash
python3 scripts/dm_conversation.py set-interest --dm-id DM_ID --interest LEVEL
\`\`\`

LEVEL is one of (pick the single best fit; the ladder roughly goes no_response → general_discussion → warm → hot, with cold / not_our_prospect / declined as off-ramps):
- **no_response** — we messaged them and they have never replied. This flow only runs on threads with an inbound message, so you will rarely pick this; it is set upstream by the classifier/DB for untouched outreach. Do not pick it once there is any inbound content.
- **general_discussion** — default baseline AFTER they have replied but BEFORE any product-relevant signal has appeared. Use this for early-stage threads where the topic hasn't yet touched anything our products solve, no product has been mentioned by either side, and you're still getting to know each other. This is what most tier-1 threads should be until they surface a real pain point.
- **hot** — explicit buying or trial signals DIRECTED AT ONE OF OUR PRODUCTS (Terminator, Fazm, PieLine, Cyrano, vipassana.cool, Octolens): asked for the link/demo/trial/pricing for our product, said "tell me more" about our product, said they already use or want to use our product, booked a call to discuss our product, gave us an email for follow-up about our product. A call offer that is about THEIR workflow, THEIR product, or an adjacent topic is NOT hot — it's warm or not_our_prospect depending on fit. The buy signal must point at something we sell.
- **warm** — engaged and problem-aware: asking substantive follow-up questions, describing their exact pain in detail, comparing tools, acknowledging the use case, multi-turn back-and-forth where they keep the thread alive AND the thread is in a domain one of our products could serve. Not yet a direct ask, but the conversation has real traction.
- **cold** — polite but shallow AFTER the conversation already touched a relevant topic: one-liners ("cool", "thanks", "will check it out", "interesting"), they disengaged from a thread that had product relevance, conversational small talk that used to have a product angle and no longer does. (If the thread never had a product angle, use general_discussion instead.)
- **not_our_prospect** — engaged but in the wrong direction: they're pitching US (offering services, leads, a sale), they treat us as a potential customer/buyer, they work in an unrelated domain, or it's a peer/colleague exchange with no realistic buyer fit. The conversation may be friendly and ongoing, but they are not a candidate for our products.
- **declined** — explicit negative: "not interested", "stop messaging", "this isn't for me", confrontational tone, accused us of being a bot/spam, asked us to leave them alone.

Rules:
- Base the label on THEIR latest message plus the full history, not on how OUR reply landed.
- If you already set a level on a prior turn and the new message doesn't change the signal, re-set the same level anyway (confirms it's current).
- Never mark hot unless there's a concrete buying/trial/demo signal from them. Don't inflate.
- Default new replied-to threads to general_discussion, not cold. Cold means "engagement faded after product relevance appeared"; general_discussion means "product relevance hasn't appeared yet." (no_response is reserved for threads where they have not replied at all, which this flow does not process.)
- Move to not_our_prospect as soon as it's clear they're pitching us or have no buyer fit — don't let those sit as warm.
- If they move from warm → declined (e.g., shut the conversation down), update to declined.

**(ii) mode**: the posture of the OUTBOUND reply we just sent. This describes what we said on this turn, not what the prospect feels. Reversible; can flip back and forth as the thread evolves.

\`\`\`bash
python3 scripts/dm_conversation.py set-mode --dm-id DM_ID --mode MODE
\`\`\`

MODE is exactly one of:
- **rapport**: our reply contained no product name, no GitHub link, no booking link. Conversational continuation, qualifying question folded into rapport, or polite brush-off. This is Mode A from Step 2.
- **pitch**: our reply named a project from \$PROJECTS, shared a project link, or appended the booking_link. This is Mode B from Step 2 (with or without Step 2.8's booking link).

Rules:
- Base the label on what WE sent on THIS turn, not on the thread's overall direction.
- \`mode\` is INDEPENDENT of \`tier\` and \`first_product_mention_at\`. A thread that pitched on turn 2 (tier=2, first_product_mention_at stamped) can still have \`mode='rapport'\` on turn 5 if we stepped back to a casual reply.
- If you SKIPPED (no reply sent) or flagged for human, do NOT call set-mode; mode only updates on turns where we actually sent an outbound message.
- Re-setting the same mode as the prior turn is fine and expected (e.g. three rapport turns in a row all re-stamp \`rapport\`).
- \`mode\` is included in the PENDING_CONVOS payload so you can see what the previous outbound was labeled.

### Step 6: Let go when it's time
Mark as stale ONLY if ALL of these are true (be conservative — when in doubt, SKIP without marking stale):
- Their last message contains NO new question, NO new topic, NO open thought, NO implicit invitation to continue
- AND one of: pure ending ("thanks", "bye", "good luck", "will check it out") with nothing else; OR no reply from them in 7+ days after a surface-level exchange; OR 2+ consecutive outbound messages with no reply (something went wrong previously)
- "Thanks + new question" or "thanks + new musing" is NEVER a stale trigger. Reply, do not mark stale. Warm + ICP-matched threads especially: if they are still asking or musing, the thread is alive.
- **ICP rescue overrides stale**: NEVER auto-stale a brush-off when the thread is ICP-relevant (target_project assignable in \$PROJECTS or already assigned) AND \`qualification_status = pending\` AND every prior outbound was \`mode=rapport\` (we never pitched, never asked a qualifier). Per Step 0 exception (b), the correct move on this turn is to send ONE last natural qualifier and set \`qualification_status = asked\`; THEIR answer (or 14-day silence) is what tells us to stale, not their polite close.
\`\`\`bash
python3 scripts/dm_conversation.py set-status --dm-id DM_ID --status stale
\`\`\`

## REPLY EXAMPLES BY INBOUND TYPE

Use these as tone/shape references, not templates. Every reply must reference a specific detail from the inbound. Adapt to the conversation, don't copy the wording.

### Type 1: Simple positive ("yeah", "sure", "cool, tell me more")

Short acknowledgment plus ONE substantive continuation. No product name unless 2+ messages in. No link unless qualified.

GOOD: "nice, what's the stack you're running it on? curious how it handles the auth flow"
GOOD: "yeah same, we spent a week last month on a bug where the action completed but the state never flushed"
BAD: "Great to hear! I'd love to tell you more about our solution. Here's a link: ..." (product-first, formal register, no hook)
BAD: "btw I built something for that" (self-promo, banned phrase per HARD RULES)

### Type 2: Engaged with detail (they describe their setup or ask a specific question)

Answer the specific thing first, then ONE follow-up. Reference a concrete detail they mentioned.

GOOD: "the parallel agents thing is what got us too, ended up scoping each one to its own worktree so they can't step on each other. how many do you run at once?"
GOOD: "yeah 30s polling is rough, we moved to event-driven with a tiny socket listener and the cost dropped like 80%"
BAD: "Thanks for sharing! That sounds really interesting. Would you like to hop on a call to discuss further?" (generic, unearned call offer, violates COMMITMENT GUARDRAILS)
BAD: "Your question about X is really good. Here's what we do: [paragraph of marketing copy]" (essay-style, not texting register)

### Type 2b: Folding ONE qualifying question into a rapport reply (Step 2.5 \`pending\` -> \`asked\`)

When the thread has 2+ messages AND a relevant topic has surfaced AND \`qualification_status = pending\`, you MAY pick ONE question from the matched project's \`qualifying_questions\` list (3 suggestions: stack, scale, pain) and paraphrase it as a natural sentence inside your normal rapport reply. Pick the one that fits the conversation best — skip the stack one if they already named their stack, skip the scale one if they already gave numbers, etc. If none of the 3 fits naturally, write your own one-sentence qualifier off a \`must_have\` item, OR skip asking this turn entirely and try again next turn. Asking is encouraged but never required on a specific turn.

Never paste any suggestion verbatim. Never stack two questions in one reply. Never make it feel like a form.

GOOD (suggestions for Terminator included "what are you trying to automate?" + "running it for clients or in-house?" — they already mentioned a client project, so we pick the in-house/clients angle): "the orchestration drift thing killed us too, ended up with one worktree per agent just to stop them fighting. is this for an internal tool or something you're shipping to customers?"
GOOD (suggestions for fde10x included "what's the agent/eval problem?" — they just described the eval pain, so we pick the timeline/owner angle): "yeah the CAC math on cold outbound is brutal right now. who on your side is the technical owner if you decided to ship this in the next month?"
BAD: "What does your E2E testing stack look like today, mostly manual, Playwright, Cypress, or nothing yet?" (verbatim paste of the suggestion, interrogation register)
BAD: "Quick question: what's your team size, industry, and current tooling?" (multiple questions stacked, form-style)

After sending, mark status as \`asked\` with a one-line paraphrase of what we asked (see Step 2.5 item 1).

### Type 3: Direct question ("what tool do you use for X?", "how do you handle Y?")

Answer the question directly. Only name a product if it's genuinely relevant AND the thread is 2+ messages in. Embed any link in a natural sentence, never lead with it.

GOOD: "we use terminator for the desktop automation side, github.com/mediar-ai/terminator. went with the accessibility API approach after screenshot-based kept flaking on retina displays"
GOOD: "honestly still figuring it out, ended up with a cron that fires every 4h and posts to a slack channel, not pretty but it works"
BAD: "Check out our amazing product at [link]! It does exactly what you need." (sales register)
BAD: "Great question! Before I answer, can I ask what your use case is?" (dodge, no answer given)

### Type 4: Hesitant or skeptical ("not sure", "we tried something like this before", "probably won't work for us")

Validate the hesitation, don't argue. Low-pressure continuation. No link, no product name.

GOOD: "yeah same reason we kept putting it off, the last tool we tried ate our logs and we spent two weeks recovering. what happened when you tried it before?"
GOOD: "makes sense, the ROI math only works past a certain team size. what's your setup now?"
BAD: "But our solution is different! Here's why you should reconsider: ..." (defensive, pushy)
BAD: "No worries, if you change your mind, here's my calendar: ..." (premature calendar share, they didn't ask)

### Type 4b: One-shot clarifier after an ambiguous answer (Step 2.5 \`asked\` -> \`answered\`)

We already folded one of the qualifying_questions (or a synthesized qualifier) into a prior turn. Their latest reply is vague, partial, or off-topic. Compose ONE narrow clarifier that references their actual words. Do NOT ask a second distinct question. Do NOT press a third time on a later turn; if it's still ambiguous after this, let it rest and evaluate based on what you have.

GOOD (they said "yeah we use some automation stuff"): "got it, when you say automation is that mostly CI scripts or also stuff that drives the desktop/browser UI?"
GOOD (they said "kinda both I guess"): "fair, is the team already paying for something for this or still stitching it together in-house?"
BAD: "Can you clarify? Also, what's your budget, team size, and timeline?" (stacked questions, interrogation)
BAD: "To qualify you properly I need to know X, Y, Z" (form register, exposes the sales machinery)

After sending, mark status as \`answered\` with a one-line rationale (see Step 2.5 item 2). On the next turn, land on \`qualified\` or \`disqualified\` based on the full picture; do not ask a third time.

### Type 5: They asked for a call, demo, or meeting

BOOKING LINK LOGIC: only share a link if (a) matched project has booking_link_auto_share: true in config.json, (b) qualification_status is already qualified, (c) booking_link_sent_at is NULL. Otherwise either qualify first (Step 2.5) or flag for human. ALWAYS mint via \`python3 scripts/dm_short_links.py mint --dm-id DM_ID\` and paste the printed short URL — never the raw cal.com URL from \$PROJECTS.

GOOD (qualified, config allows auto-share): "yeah for sure, grab a time here: <minted short URL from dm_short_links.py>"
GOOD (not yet qualified, folding in one of the project's qualifying_questions naturally — pick the one that fits the inbound): "happy to dig in, what's the team size you're running this across?"
GOOD (project has no auto-share, or they fail disqualify list): flag for human, do NOT reply in this run.
BAD: "Absolutely! Here's my calendar: calendly.com/my-made-up-link" (fabricated link, never invent one)
BAD: "Let's do Thursday at 2pm!" (time-bound commitment, violates COMMITMENT GUARDRAILS)

### Type 6: They're pitching US (agency, service, their product, their workflow)

Set interest to not_our_prospect. Short polite reply or skip. Do NOT flag for human. Do NOT pitch back.

GOOD: "appreciate it, not a fit right now but good luck with it"
GOOD: skip entirely (if their message doesn't warrant a reply)
BAD: "Thanks for reaching out! Here's what WE do: ..." (turning their pitch into ours)
BAD: flagging for human (wastes inbox attention for non-buyer)

### Type 7: Philosophical disagreement or polemic (meditation, AI doomerism, productivity takes)

Rapport reply, no product, no booking link, no human flag. Keep it conversational.

GOOD: "yeah the framing where everyone gets enlightened in 10 days feels like a marketing thing to me too. 7 courses in and the gains are subtle, mostly in how i notice reactivity before it flares"
BAD: "Would you like to try vipassana.cool? It's designed for people like you." (forcing product into an unrelated philosophical thread)
BAD: flagging for human (escalation is for actionable requests, not disagreements about ideas)

## PIVOT EXAMPLES (Tier 1 -> Tier 2): general chat -> product mention

The single most consequential move in a thread. Before composing any Mode B pivot, verify ALL four Step 2.7 conditions: 2+ total messages, the Mode B trigger is satisfied (they surfaced a problem a project solves, OR they asked for tools, OR the thread has reached 4+ messages and the timeline rule forces a decision), the product fits naturally in the reply with no "btw" register, and you'd genuinely recommend it to a friend in their situation. If the thread is still under 4 messages and none of the organic triggers hit, stay in Tier 1 rapport.

At the pivot turn, fire these writes together (alongside Step 5b's \`set-interest\`):
\`\`\`bash
python3 scripts/dm_conversation.py set-project   --dm-id ID --project "PROJECT_NAME_FROM_CONFIG"
python3 scripts/dm_conversation.py set-tier      --dm-id ID --tier 2
python3 scripts/dm_conversation.py set-interest  --dm-id ID --interest warm
\`\`\`
\`set-tier\` automatically stamps \`first_product_mention_at = NOW()\` on the first transition to tier >= 2. Do not set that column by hand.

### Trigger A: they explicitly asked for a tool

GOOD (Terminator, asked about desktop automation): "we use terminator for the desktop automation side, github.com/mediar-ai/terminator. went with the accessibility API approach after screenshot-based kept flaking on retina displays"
GOOD (Octolens, asked about mention tracking): "honestly octolens has been the one that stuck, octolens.com. picks up reddit/x/youtube/hn mentions in one feed so i stopped running manual searches"

### Trigger B: they described a pain a project solves (no explicit tool ask)

GOOD (Terminator, they described retina/screenshot flake): "yeah the retina flake was our whole week last month. ended up on terminator, github.com/mediar-ai/terminator, since it drives the accessibility tree directly, killed the flake in one afternoon"
GOOD (Octolens, they described manual search pain): "that's the exact reason we stopped doing it manually. octolens.com catches reddit/x/youtube/hn mentions in one feed, daily email is enough that i barely check the dashboard"

### Soft pivot: category first, name next turn if they bite

GOOD (desktop automation, no product name yet): "yeah the retina flake was our whole week last month. ended up moving to an accessibility-API runner instead of screenshots, solved it immediately"

For the soft-pivot turn, do NOT set tier to 2 yet. Keep tier at 1 and do NOT call \`set-project\`. If they reply asking "what tool?", the NEXT turn becomes Type 3 and completes the pivot with \`set-tier 2\` + \`set-project\` + \`set-interest warm\` together.

### BAD examples

BAD: "btw I built a tool for that, check out github.com/mediar-ai/terminator if you're curious" (HARD RULE 3, "btw I built" is banned self-promo)
BAD: "What you're describing is exactly what Terminator solves. Would you like to try it? Here's the link: ..." (sales register, product-first, unearned pivot)
BAD: pivoting on the very first outbound (HARD RULE 4, need 2+ total messages in the thread)
BAD: pivoting to Terminator when the pain is about brand mentions (HARD RULE 5, product must fit the actual pain)

## ANTI-PATTERNS TO AVOID (learned from past mistakes)
- Sending two messages before getting a reply (got us called out as AI)
- Dropping a GitHub link in the second message of a conversation
- Pivoting from their topic to desktop automation/accessibility APIs when it's irrelevant
- Using the same opener pattern ("honestly still juggling...", "that's basically the bet I'm making...")
- Asking a question you already asked in a previous message
- Pitching vipassana.cool to someone who just mentioned meditation casually
- Saying "cool I'll hit you up on [platform]" when you can't actually do that

After processing all conversations, print a summary:
- How many human replies delivered (Phase 0)
- How many new inbound messages found per platform
- How many replies sent
- How many flagged for human attention (list each with reason)
- How many left alone (no reply needed)
- How many marked stale
PROMPT_EOF

# Select MCP config based on platform
DM_MCP_CONFIG="$HOME/.claude/browser-agent-configs/all-agents-mcp.json"
if [ -n "$PLATFORM" ]; then
    case "$PLATFORM" in
        reddit)   DM_MCP_CONFIG="$HOME/.claude/browser-agent-configs/reddit-harness-mcp.json" ;;
        linkedin) DM_MCP_CONFIG="$HOME/.claude/browser-agent-configs/linkedin-harness-mcp.json" ;;
        twitter|x) DM_MCP_CONFIG="$HOME/.claude/browser-agent-configs/twitter-harness-mcp.json" ;;
    esac
fi

# Acquire platform-browser lock(s) now, immediately before the Claude/MCP
# step. Alphabetical order in the multi-platform branch prevents deadlock
# with other pipelines that also acquire multiple browser locks.
#
# Reddit lock strategy (migrated 2026-05-13): the reddit-browser lease is
# acquired/released PER DM inside the Claude prompt (Step 4-pre / Step 5-post),
# mirroring the link-edit-reddit.sh and dm-outreach-reddit.sh pattern. Holding
# the lease around the whole 5400s Claude turn monopolised the browser for
# the full DM batch (~5-15 min) while peer reddit pipelines sat blocked during
# Claude's compose + classify phases. The brief pre-flight below runs the
# one-shot ensure_browser_healthy / Singleton-lock clear under a 30s lease so
# the cycle starts on a clean profile; the prompt then acquires per-DM.
#
# LinkedIn/Twitter still use the long bash lock because they don't have the
# per-MCP-call heartbeat proxy wired through their MCP configs yet.
log "Acquiring platform-browser lock(s) for Claude/MCP step..."
case "${PLATFORM:-all}" in
    linkedin) acquire_lock "linkedin-browser" 3600; ( source "$(dirname "$0")/lib/linkedin-backend.sh"; ensure_linkedin_browser_for_backend ) ;;
    reddit)
        # Reddit: brief pre-flight acquire+ensure+release ONLY. Per-DM
        # acquire/release happens inside the Claude prompt.
        log "Reddit pre-flight: brief acquire + ensure_reddit_browser_for_backend (harness 9557) + release..."
        python3 "$REPO_DIR/scripts/reddit_browser_lock.py" acquire --timeout 60 --ttl 30 2>&1 | tee -a "$LOG_FILE" || \
            log "WARNING: reddit pre-flight acquire BUSY; ensure_reddit_browser_for_backend will run anyway; per-DM acquires inside the prompt will retry."
        ensure_reddit_browser_for_backend
        python3 "$REPO_DIR/scripts/reddit_browser_lock.py" release 2>/dev/null || true
        ;;
    twitter|x) acquire_lock "twitter-browser" 3600; ensure_twitter_browser_for_backend ;;
    all)
        acquire_lock "linkedin-browser" 3600
        ( source "$(dirname "$0")/lib/linkedin-backend.sh"; ensure_linkedin_browser_for_backend )
        # Reddit: brief pre-flight only (same as the `reddit` branch above).
        log "Reddit pre-flight: brief acquire + ensure_reddit_browser_for_backend (harness 9557) + release..."
        python3 "$REPO_DIR/scripts/reddit_browser_lock.py" acquire --timeout 60 --ttl 30 2>&1 | tee -a "$LOG_FILE" || \
            log "WARNING: reddit pre-flight acquire BUSY; per-DM acquires inside the prompt will retry."
        ensure_reddit_browser_for_backend
        python3 "$REPO_DIR/scripts/reddit_browser_lock.py" release 2>/dev/null || true
        acquire_lock "twitter-browser" 3600
        ensure_twitter_browser_for_backend
        ;;
esac

# ============================================================================
# EARLY-EXIT GATE (added 2026-04-29 to skip Claude on empty cycles)
# ----------------------------------------------------------------------------
# Before invoking Claude (~$5-20 per run on Opus, mostly burned reading
# huge LinkedIn DOM snapshots), check whether there's *anything* to do:
#
#   1. DB-side: any active DM where the most recent message is inbound?
#      (Cheap: ~50ms SQL.) Catches every conversation we're already
#      tracking that needs a reply.
#
#   2. Live-side, LinkedIn only: scrape /messaging/ sidebar via the
#      read-only helper (scripts/linkedin_browser.py unread-dms) and
#      count threads with the unread badge. Catches brand-new inbound
#      from prospects we haven't logged a DM row for yet.
#
# If both signals say "nothing", we log run with cost=0 and exit cleanly.
# ============================================================================
NEEDS_CLAUDE=false
GATE_REASON=""

# Helper: count DMs where last message is inbound, per platform. Includes
# both 'active' and 'needs_reply' because Phase A.0 ingest flips status to
# 'needs_reply' the moment a fresh inbound lands; gating on 'active' alone
# silently skipped every fresh inbound (bug surfaced 2026-04-30 with 4 warm
# leads stuck unanswered for hours/days).
needs_reply_count_for() {
    # HTTP-only via /api/v1/dms/engage?mode=needs_reply. The endpoint folds
    # 'x'/'twitter' into one platform server-side (matches the legacy
    # PLATFORM_SQL_FILTER), so passing either is safe. Prints an integer, or
    # '?' on transport failure (the gate treats '?' as "don't trust, wake
    # Claude" just like the old psql-error path did).
    local plat="$1"
    python3 "$REPO_DIR/scripts/dm_engage_helper.py" needs-reply --platform "$plat" 2>/dev/null | tr -d ' \n' || echo "?"
}

# DB-side check across in-scope platforms.
for plat_check in ${PLATFORM:-reddit linkedin twitter}; do
    case "$plat_check" in
        x) plat_check="twitter" ;;  # canonical display name; query covers both
    esac
    NR=$(needs_reply_count_for "$plat_check")
    if [ "$NR" != "0" ] && [ "$NR" != "?" ] && [ -n "$NR" ]; then
        NEEDS_CLAUDE=true
        GATE_REASON="db: ${plat_check} has ${NR} convos with inbound>outbound"
        log "[gate] ${GATE_REASON}"
        break
    fi
done

# Live-side LinkedIn pre-check (only when DB said nothing AND LinkedIn is
# in scope). Read-only sidebar scrape via headed Chromium, ~5s, $0.
#
# We capture stderr (don't /dev/null it) so a Playwright traceback is
# auditable in the run log. We also surface total_threads and the first
# partner name so post-hoc you can tell "really empty inbox" from "session
# bad / DOM didn't render" — both of which would otherwise look identical
# (ok=true, unread_count=0). total_threads==0 is treated as a soft failure
# because our account always has 10+ sidebar threads.
if ! $NEEDS_CLAUDE && { [ -z "$PLATFORM" ] || [ "$PLATFORM" = "linkedin" ]; }; then
    log "[gate] DB says nothing pending; running LinkedIn live sidebar pre-check..."
    LI_PRECHECK_STDERR="/tmp/li_precheck_stderr.$$"
    LI_PRECHECK_PARSED="/tmp/li_precheck_parsed.$$"
    # `set -e` + `set -o pipefail` is on. Three things matter:
    #   1. Caller env var: the helper now requires
    #      SOCIAL_AUTOPOSTER_LINKEDIN_PRECHECK=1 (added to stop wandering
    #      Claude planners from smoke-testing it and racing the profile).
    #   2. Capture helper exit code via `&& a || b` so a non-zero exit
    #      doesn't trip set -e.
    #   3. Parse the JSON in a SINGLE python invocation that tolerates
    #      empty/whitespace input (json.loads on `\n` from echo crashes,
    #      tripping pipefail+set -e and silently killing the script — the
    #      bug that hid behind every prior "log frozen at precheck" run).
    LI_PRECHECK=$(SOCIAL_AUTOPOSTER_LINKEDIN_PRECHECK=1 \
        LINKEDIN_CDP_URL="${LINKEDIN_CDP_URL:-http://127.0.0.1:9556}" \
        PYTHONPATH="$HOME/Library/Python/3.9/lib/python/site-packages" \
        /usr/bin/python3 "$REPO_DIR/scripts/linkedin_browser.py" unread-dms 2>"$LI_PRECHECK_STDERR") \
        && LI_EXIT=0 || LI_EXIT=$?
    LI_OK="" LI_UNREAD="" LI_TOTAL="" LI_FIRST_PARTNER="" LI_ERROR=""
    /usr/bin/python3 - "$LI_PRECHECK_PARSED" "$LI_PRECHECK" << 'PYEOF' || true
import json, shlex, sys
out_path = sys.argv[1]
raw = (sys.argv[2] if len(sys.argv) > 2 else "").strip()
try:
    d = json.loads(raw) if raw else {}
except Exception:
    d = {}
threads = d.get("threads") or []
first = (threads[0] if threads else {}) or {}
fields = {
    "LI_OK": d.get("ok"),
    "LI_UNREAD": d.get("unread_count", 0),
    "LI_TOTAL": d.get("total_threads", 0),
    "LI_FIRST_PARTNER": first.get("partner", ""),
    "LI_ERROR": d.get("error", ""),
}
with open(out_path, "w") as f:
    for k, v in fields.items():
        f.write(f"{k}={shlex.quote(str(v) if v is not None else '')}\n")
PYEOF
    if [ -s "$LI_PRECHECK_PARSED" ]; then
        # shellcheck disable=SC1090
        source "$LI_PRECHECK_PARSED"
    fi
    rm -f "$LI_PRECHECK_PARSED"
    log "[gate] linkedin precheck: exit=$LI_EXIT ok=$LI_OK total=$LI_TOTAL unread=$LI_UNREAD first_partner='${LI_FIRST_PARTNER}' error='${LI_ERROR}'"
    if [ -s "$LI_PRECHECK_STDERR" ]; then
        log "[gate] linkedin precheck stderr (first 20 lines):"
        head -20 "$LI_PRECHECK_STDERR" | sed 's/^/[gate]   /' | tee -a "$LOG_FILE" >/dev/null
    fi
    rm -f "$LI_PRECHECK_STDERR"
    if [ "$LI_OK" = "True" ] && [ "$LI_TOTAL" != "0" ] && [ -n "$LI_TOTAL" ] && [ "$LI_UNREAD" = "0" ]; then
        log "[gate] LinkedIn sidebar pre-check: 0 unread of $LI_TOTAL threads"
    elif [ "$LI_OK" = "True" ] && [ "$LI_TOTAL" != "0" ] && [ -n "$LI_TOTAL" ]; then
        NEEDS_CLAUDE=true
        GATE_REASON="linkedin live: ${LI_UNREAD} unread of ${LI_TOTAL} threads in sidebar"
        log "[gate] ${GATE_REASON}"
    elif [ "$LI_OK" = "True" ]; then
        # ok=true but total_threads=0 is suspicious. Could be: session
        # silently expired (no /login redirect), DOM markup changed, or
        # a TargetClosedError mid-scan that the helper didn't catch.
        # Don't drop work; fall through to Claude.
        NEEDS_CLAUDE=true
        GATE_REASON="linkedin pre-check returned 0 total threads (suspect bad session or DOM); falling through to Claude"
        log "[gate] ${GATE_REASON}"
    else
        # Helper failed (session_invalid, profile_locked, crash, etc).
        NEEDS_CLAUDE=true
        GATE_REASON="linkedin pre-check failed (ok=$LI_OK error=$LI_ERROR); falling through to Claude"
        log "[gate] ${GATE_REASON}"
    fi
fi

# Live-side Twitter pre-check (mirror of the LinkedIn one above). Required
# because Twitter inbound DMs are only ingested into the dms/dm_messages tables
# when Claude runs Phase B of this script — but the DB-side gate above would
# never see them and skip Claude entirely. That's bug B in the 2026-05-01..
# 05-13 inbound-DM cliff: 0 X DMs ingested for 13 days after the gate landed.
#
# Read-only sidebar scrape via headed Chromium (~5s, $0). Identical safety
# envelope to the LinkedIn one: capture stderr for auditability, parse JSON
# defensively, fall through to Claude on any failure rather than dropping work.
#
# Disable with SOCIAL_AUTOPOSTER_DISABLE_TWITTER_PRECHECK=1 if it starts
# misbehaving; the cycle will then run Claude on every fire (high cost, but
# never silently drops inbound).
if ! $NEEDS_CLAUDE \
   && { [ -z "$PLATFORM" ] || [ "$PLATFORM" = "twitter" ] || [ "$PLATFORM" = "x" ]; } \
   && [ "${SOCIAL_AUTOPOSTER_DISABLE_TWITTER_PRECHECK:-0}" != "1" ]; then
    log "[gate] DB says nothing pending; running Twitter live sidebar pre-check..."
    TW_PRECHECK_STDERR="/tmp/tw_precheck_stderr.$$"
    TW_PRECHECK_PARSED="/tmp/tw_precheck_parsed.$$"
    TW_PRECHECK=$(/usr/bin/python3 "$REPO_DIR/scripts/twitter_browser.py" unread-dms 2>"$TW_PRECHECK_STDERR") \
        && TW_EXIT=0 || TW_EXIT=$?
    TW_OK="" TW_UNREAD="" TW_TOTAL="" TW_FIRST_PARTNER="" TW_ERROR=""
    /usr/bin/python3 - "$TW_PRECHECK_PARSED" "$TW_PRECHECK" << 'PYEOF' || true
import json, shlex, sys
out_path = sys.argv[1]
raw = (sys.argv[2] if len(sys.argv) > 2 else "").strip()
try:
    d = json.loads(raw) if raw else None
except Exception:
    d = None

# twitter_browser.unread_dms returns either:
#  - a LIST of {has_unread, author, thread_url, ...} on success
#  - a DICT {"ok": False, "error": "..."} on rate limit / not_on_dm_page
#  - None / unparseable on script crash
total = 0
unread = 0
first_partner = ""
ok = False
err = ""
if isinstance(d, list):
    ok = True
    total = len(d)
    for c in d:
        if c.get("has_unread"):
            unread += 1
            if not first_partner:
                first_partner = c.get("author") or c.get("handle") or ""
elif isinstance(d, dict):
    ok = bool(d.get("ok"))
    err = d.get("error", "") or ""
else:
    err = "unparseable_json"

fields = {
    "TW_OK": "True" if ok else "False",
    "TW_UNREAD": unread,
    "TW_TOTAL": total,
    "TW_FIRST_PARTNER": first_partner,
    "TW_ERROR": err,
}
with open(out_path, "w") as f:
    for k, v in fields.items():
        f.write(f"{k}={shlex.quote(str(v) if v is not None else '')}\n")
PYEOF
    if [ -s "$TW_PRECHECK_PARSED" ]; then
        # shellcheck disable=SC1090
        source "$TW_PRECHECK_PARSED"
    fi
    rm -f "$TW_PRECHECK_PARSED"
    log "[gate] twitter precheck: exit=$TW_EXIT ok=$TW_OK total=$TW_TOTAL unread=$TW_UNREAD first_partner='${TW_FIRST_PARTNER}' error='${TW_ERROR}'"
    if [ -s "$TW_PRECHECK_STDERR" ]; then
        log "[gate] twitter precheck stderr (first 20 lines):"
        head -20 "$TW_PRECHECK_STDERR" | sed 's/^/[gate]   /' | tee -a "$LOG_FILE" >/dev/null
    fi
    rm -f "$TW_PRECHECK_STDERR"
    if [ "$TW_OK" = "True" ] && [ "$TW_TOTAL" != "0" ] && [ -n "$TW_TOTAL" ] && [ "$TW_UNREAD" = "0" ]; then
        log "[gate] Twitter sidebar pre-check: 0 unread of $TW_TOTAL threads"
    elif [ "$TW_OK" = "True" ] && [ "$TW_TOTAL" != "0" ] && [ -n "$TW_TOTAL" ]; then
        NEEDS_CLAUDE=true
        GATE_REASON="twitter live: ${TW_UNREAD} unread of ${TW_TOTAL} threads in sidebar"
        log "[gate] ${GATE_REASON}"
    elif [ "$TW_OK" = "True" ]; then
        # ok=true but total_threads=0 means the helper returned an empty list.
        # Could be: encryption passcode prompt, session expired, DOM changed,
        # or a real empty inbox. Don't drop work; fall through to Claude.
        NEEDS_CLAUDE=true
        GATE_REASON="twitter pre-check returned 0 total threads (suspect bad session or passcode); falling through to Claude"
        log "[gate] ${GATE_REASON}"
    else
        # Helper failed (rate limit, not_on_dm_page, crash, etc).
        NEEDS_CLAUDE=true
        GATE_REASON="twitter pre-check failed (ok=$TW_OK error=$TW_ERROR); falling through to Claude"
        log "[gate] ${GATE_REASON}"
    fi
fi

if ! $NEEDS_CLAUDE; then
    log "[gate] All signals say nothing to do; skipping Claude invocation."
    rm -f "$PHASE_A_PROMPT"
    RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
    # Log a zero-cost run so the dashboard shows the cycle fired.
    if [ -z "$PLATFORM" ] || [ "$PLATFORM" = "reddit" ]; then
        python3 "$REPO_DIR/scripts/log_run.py" --script "dm_replies_reddit" --posted 0 --skipped 0 --failed 0 --cost "0.0" --elapsed "$RUN_ELAPSED" 2>/dev/null || true
    fi
    if [ -z "$PLATFORM" ] || [ "$PLATFORM" = "linkedin" ]; then
        python3 "$REPO_DIR/scripts/log_run.py" --script "dm_replies_linkedin" --posted 0 --skipped 0 --failed 0 --cost "0.0" --elapsed "$RUN_ELAPSED" 2>/dev/null || true
    fi
    if [ -z "$PLATFORM" ] || [ "$PLATFORM" = "twitter" ] || [ "$PLATFORM" = "x" ]; then
        python3 "$REPO_DIR/scripts/log_run.py" --script "dm_replies_twitter" --posted 0 --skipped 0 --failed 0 --cost "0.0" --elapsed "$RUN_ELAPSED" 2>/dev/null || true
    fi
    log "=== DM reply engagement complete (gated, cost=\$0): $(date) ==="
    exit 0
fi
# ============================================================================
# END EARLY-EXIT GATE
# ============================================================================

gtimeout 5400 "$REPO_DIR/scripts/run_claude.sh" "engage-dm-replies" --strict-mcp-config --mcp-config "$DM_MCP_CONFIG" --output-format stream-json --verbose -p "$(cat "$PHASE_A_PROMPT")" 2>&1 | tee -a "$LOG_FILE" || log "WARNING: DM reply claude exited with code $?"
rm -f "$PHASE_A_PROMPT"

# Belt-and-suspenders: free the reddit-browser lease if it's still held.
# Idempotent — release prints OK / NOT_HELD / HELD_BY_OTHER. Mirrors
# link-edit-reddit.sh:185. Only fires when reddit was in scope this run.
case "${PLATFORM:-all}" in
    reddit|all)
        python3 "$REPO_DIR/scripts/reddit_browser_lock.py" release 2>/dev/null || true
        ;;
esac

# ═══════════════════════════════════════════════════════
# Cleanup
# ═══════════════════════════════════════════════════════
DM_SUMMARY=$(python3 "$REPO_DIR/scripts/dm_engage_helper.py" summary 2>/dev/null || echo "{}")

log "DM pipeline summary: $DM_SUMMARY"

# Log run to persistent monitor per platform.
# posted  = DM replies actually sent during this run's window (per-platform, per-window)
# skipped = conversations currently marked stale (per-platform, cumulative snapshot)
dm_counts_for() {
    local plat="$1"
    # HTTP-only via /api/v1/dms/engage?mode=run_counts. The endpoint folds
    # 'x'/'twitter' server-side (the dms table stores X rows as platform='x')
    # and counts outbound messages since RUN_START + current stale convos. The
    # helper prints 'POSTED STALE' (space-separated) so the `read -r` below is
    # unchanged.
    python3 "$REPO_DIR/scripts/dm_engage_helper.py" run-counts --platform "$plat" --since "$RUN_START" 2>/dev/null
}
RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
_COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START" --scripts "engage-dm-replies" 2>/dev/null || echo "0.0000")
if [ -z "$PLATFORM" ] || [ "$PLATFORM" = "reddit" ]; then
    read -r R_POSTED R_STALE <<< "$(dm_counts_for reddit)"
    python3 "$REPO_DIR/scripts/log_run.py" --script "dm_replies_reddit" --posted "${R_POSTED:-0}" --skipped "${R_STALE:-0}" --failed 0 --cost "$_COST" --elapsed "$RUN_ELAPSED"
fi
if [ -z "$PLATFORM" ] || [ "$PLATFORM" = "linkedin" ]; then
    read -r L_POSTED L_STALE <<< "$(dm_counts_for linkedin)"
    python3 "$REPO_DIR/scripts/log_run.py" --script "dm_replies_linkedin" --posted "${L_POSTED:-0}" --skipped "${L_STALE:-0}" --failed 0 --cost "$_COST" --elapsed "$RUN_ELAPSED"
fi
if [ -z "$PLATFORM" ] || [ "$PLATFORM" = "twitter" ] || [ "$PLATFORM" = "x" ]; then
    read -r T_POSTED T_STALE <<< "$(dm_counts_for twitter)"
    python3 "$REPO_DIR/scripts/log_run.py" --script "dm_replies_twitter" --posted "${T_POSTED:-0}" --skipped "${T_STALE:-0}" --failed 0 --cost "$_COST" --elapsed "$RUN_ELAPSED"
fi

# Report flagged conversations needing human attention (emails already sent per-DM during flagging)
FLAGGED_COUNT=$(python3 "$REPO_DIR/scripts/dm_engage_helper.py" flagged-count 2>/dev/null || echo "0")

if [ "$FLAGGED_COUNT" -gt 0 ] 2>/dev/null; then
    log "ACTION REQUIRED: $FLAGGED_COUNT conversations flagged for human attention (escalation emails already sent per-DM)"
    log "Run: python3 ~/social-autoposter/scripts/dm_conversation.py show-flagged"

    platform_notify "Social DM Escalation" "$FLAGGED_COUNT DM conversations need your attention"
fi

# Delete old logs
find "$LOG_DIR" -name "engage-dm-replies-*.log" -mtime +7 -delete 2>/dev/null || true

log "=== DM reply engagement complete: $(date) ==="
