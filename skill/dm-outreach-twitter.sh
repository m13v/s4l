#!/usr/bin/env bash
# dm-outreach-twitter.sh — Outbound Twitter/X DM outreach.
# Scans for DM candidates (users who engaged on our posts), then sends Twitter DMs
# to continue the conversation. Inbound DM replies are handled separately
# by engage-dm-replies-twitter.sh.
# Called by launchd (com.m13v.social-dm-outreach-twitter) every 6 hours.

set -euo pipefail

# Cycle ID for cross-cycle cost accounting (see run-twitter-cycle.sh for the
# same pattern). Stamps claude_sessions.cycle_id so get_run_cost.py --cycle-id
# returns just this cycle's spend.
BATCH_ID="${BATCH_ID:-dmtw-$(date +%Y%m%d-%H%M%S)}"
export BATCH_ID
export SA_CYCLE_ID="$BATCH_ID"

# Bootstrap log paths early so the singleton-cleanup output below gets captured
# in the same log file the rest of the run uses.
LOG_DIR="$HOME/social-autoposter/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/dm-outreach-twitter-$(date +%Y-%m-%d_%H%M%S).log"

# Browser-profile lock first (shared with other twitter pipelines), then pipeline lock.
source "$(dirname "$0")/lock.sh"
# Harness-only browser bootstrap (twitter-agent path fully removed 2026-05-19).
# Sets MCP_CONFIG_FILE, BROWSER_INSTRUCTIONS, exports TWITTER_CDP_URL=9555.
source "$(dirname "$0")/lib/twitter-backend.sh"

acquire_lock "twitter-browser" 3600
ensure_twitter_browser_for_backend 2>&1 | tee -a "$LOG_FILE"
acquire_lock "dm-outreach-twitter" 2700

# Load secrets
# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
SKILL_FILE="$REPO_DIR/SKILL.md"

# 2026-06-02: removed the vestigial DATABASE_URL gate. This rail talks to the
# central store exclusively through the S4L HTTP API (scan_dm_candidates.py,
# dm_outreach_twitter_helper.py, log_run.py all use scripts/http_api.py). No
# direct Postgres connection is opened here, matching dm-outreach-reddit.sh and
# dm-outreach-linkedin.sh (migrated 2026-05-12).
# (LOG_DIR/LOG_FILE bootstrapped at top of script.)

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

RUN_START=$(date +%s)
log "=== Twitter DM Outreach Run: $(date) ==="

# Scan for new DM candidates first (cheap Python, writes to dms table)
log "Scanning for DM candidates (all platforms)..."
(PYTHONUNBUFFERED=1 python3 "$REPO_DIR/scripts/scan_dm_candidates.py" 2>&1 || true) | tee -a "$LOG_FILE"

DM_PENDING=$(python3 "$REPO_DIR/scripts/dm_outreach_twitter_helper.py" pending-count 2>/dev/null || echo "0")

if [ "$DM_PENDING" -eq 0 ]; then
    log "No pending Twitter DMs"
    python3 "$REPO_DIR/scripts/log_run.py" --script "dm_outreach_twitter" --posted 0 --skipped 0 --failed 0 --cost 0 --elapsed $(( $(date +%s) - RUN_START ))
    exit 0
fi

log "Twitter: $DM_PENDING DMs to send"

# Prompt-feed JSON now comes from /api/v1/dms/outreach-queue (same
# correlated other_engagement subquery, same 60-day window, same join
# graph). Helper canonicalises platform=twitter → 'x' to match the dms
# table's stored value.
DM_DATA=$(python3 "$REPO_DIR/scripts/dm_outreach_twitter_helper.py" outreach-queue 2>/dev/null || echo "[]")

# Per-project qualification context for ICP pre-check
PROJECTS_QUALIFICATION=$(python3 -c "
import json
c = json.load(open('$REPO_DIR/config.json'))
for p in c.get('projects', []):
    q = p.get('qualification') or {}
    if not q:
        continue
    print(f\"- {p['name']}:\")
    if q.get('must_have'):
        print(f\"    must_have: {' ; '.join(q['must_have'])}\")
    if q.get('disqualify'):
        print(f\"    disqualify: {' ; '.join(q['disqualify'])}\")
" 2>/dev/null || echo "")

export CLAUDE_SESSION_ID=$(uuidgen | tr 'A-Z' 'a-z')

PROMPT_FILE=$(mktemp)
cat > "$PROMPT_FILE" <<PROMPT_EOF
You are the Social Autoposter Twitter/X DM outreach bot.

Read $SKILL_FILE for content rules (tone, anti-AI detection, no em dashes).

## Task: Send Twitter/X DMs to continue comment conversations

These users engaged with our Twitter/X posts/comments. We already replied publicly. Now send a short, casual DM to continue the conversation.

CRITICAL RULES:
1. DMs must feel like a natural continuation of the comment discussion, NOT a cold outreach or sales pitch
2. Reference the specific conversation topic, not generic "hey I saw your comment"
3. Keep it short: 1-2 sentences max, like a text message
4. No links in the first DM; earn the conversation first
5. No em dashes. Write casually, like texting a coworker.

## COMMITMENT GUARDRAILS (violating any of these is a critical failure)
- **NEVER suggest, offer, or agree to calls, meetings, demos, or video chats.** Keep everything in the DM thread.
- **NEVER agree to podcast appearances, X Spaces, interviews, or live events.**
- **NEVER offer to move the conversation to another platform** (Telegram, Discord, email, etc.). Stay in the current DM thread.
- **NEVER promise to share specific links, files, or resources you don't have right now.** If you don't have it in config.json projects, don't promise it.
- **NEVER make time-bound commitments** ("this week", "tomorrow", "Thursday"). Don't commit the human to any schedule.
- **NEVER say "I'm in [city]"** or share location/personal details not in config.json.
- If someone asks for any of the above, respond naturally but deflect: keep the conversation going in the DM without making promises. Example: "honestly easier to hash it out here, what specifically are you trying to set up?"

DM EXAMPLES (good):
- "yo your point about token costs scaling with agent count hit home, we're dealing with the exact same thing. what's your setup look like?"
- "that workaround you mentioned for the accessibility API crash is clever, did it hold up in production?"
- "curious how you ended up going with that approach for the MCP server, we tried something similar"

DM EXAMPLES (bad):
- "Hey! I noticed your tweet. I'm building something you might find interesting..." (cold pitch)
- "Great point! I'd love to connect and share what we're working on." (generic)
- "Hi there, I saw your insightful tweet about AI agents..." (too formal)

## Users to DM:
$DM_DATA

## Cross-thread engagement awareness
Each row may include an \`other_engagement\` array: this user's other recent (60-day) interactions with our posts on the same platform. Each entry has thread_title, their_content snippet, our_reply_content snippet, depth (>1 = public follow-up to our reply in a thread), status, replied_at.

Use it as context for the DM:
- If the most recent other_engagement entry is on the SAME thread with depth>1 and replied_at < 6 hours ago, they're actively continuing the public conversation. Prefer a lighter-touch DM, or open with an acknowledgment of the ongoing thread instead of introducing a new angle.
- If they've engaged on multiple other threads, it signals genuine interest. The DM can be slightly more direct without feeling cold.
- Do NOT quote their other comments back at them or enumerate their history. It's context, not content.

## Per-project ICP criteria (used for the pre-check step, NOT to skip sending):
$PROJECTS_QUALIFICATION

## Pre-send profile fetch + ICP pre-check (MANDATORY per DM, no filter)

For each DM row, BEFORE you compose or send, do this in order:

1. Look at the row's \`target_project\`. If it's NULL, set icp_precheck=unknown with notes="no_target_project" and proceed to step 4 — but still try to capture profile basics.

2. Fetch the prospect's X/Twitter profile using the browser tools from the BROWSER BACKEND block above:
   - Navigate to https://x.com/THEIR_AUTHOR (strip any leading @).
   - Snapshot the page. Extract: display name, handle, bio text, follower count, pinned/top-of-feed recent tweet topic summary.
   - If the profile is suspended, protected, or empty, capture what you can and note "profile_limited" or "profile_inaccessible".
   - MANDATORY BLOCK CHECK (do this on every profile visit, not just when a
     send later fails): read the page text (snapshot or DOM query) and check
     for the literal string "has blocked you" (case-insensitive). This is the
     ONLY sufficient evidence for concluding we're blocked -- never infer it
     from a missing Message button, a screenshot, or a send failure alone
     (a missing Message button just as often means "DMs closed", which is
     chat_disabled below, not a block).
     If that exact text IS present:
       a. Skip this DM: python3 $REPO_DIR/scripts/dm_conversation.py mark-skipped \\
            --dm-id DM_ID --reason "blocked_by_user"
       b. Hard-block the author so future items from them are never drafted
          (shared universal blocklist, same mechanism engage-twitter uses):
            python3 $REPO_DIR/scripts/reply_db.py blocklist add x THEIR_AUTHOR \\
              --reason "X reported this author has blocked our account (dm-outreach-twitter, profile check)" \\
              --classification blocked_by_author \\
              --added-by block_probe
       c. Move on to the next DM row; do not attempt to send.
     If that text is NOT present, proceed normally -- do not skip or block
     based on any other signal from this step.

3. Persist the profile fields:
   \`\`\`bash
   python3 $REPO_DIR/scripts/fetch_prospect_profile.py upsert \\
       --platform twitter --author "THEIR_AUTHOR" \\
       --profile-url "https://x.com/THEIR_AUTHOR" \\
       --display-name "DISPLAY_NAME" \\
       --headline "SHORT_BIO_FIRST_LINE" \\
       --bio "FULL_BIO_TEXT" \\
       --follower-count N \\
       --recent-activity "SHORT_RECENT_TWEETS_SUMMARY" \\
       --notes "ANY_SIGNAL_WORTH_REMEMBERING" \\
       --link-dm DM_ID
   \`\`\`
   Omit any flag whose value is empty or unknown. \`--link-dm\` also wires dms.prospect_id.

4. Evaluate ICP match against EVERY project listed in "Per-project ICP criteria" above (not only target_project). For each project compare the profile + their_content + comment_context against its must_have (satisfy at least one) and disqualify (trigger ANY = fail), and pick one label: icp_match, icp_miss, disqualified, or unknown. Upsert one entry per project:
   \`\`\`bash
   python3 $REPO_DIR/scripts/dm_conversation.py set-icp-precheck \\
       --dm-id DM_ID --project PROJECT_NAME --label LABEL --notes "SHORT_RATIONALE"
   \`\`\`
   Run this once per project from the list. Each call upserts one entry in dms.icp_matches (JSONB array) keyed by project.

5. If ANY entry in icp_matches has label=disqualified, skip the send: run \`python3 scripts/dm_conversation.py mark-skipped --dm-id DM_ID --reason "disqualified: PROJECT - SHORT_NOTES"\` and move on. \`icp_miss\` alone does NOT gate; send when every project scored miss. Only explicit \`disqualified\` blocks the opener.

## How to send Twitter/X DMs (use the browser tools from the BROWSER BACKEND block):
1. Navigate to https://x.com/messages
2. **ENCRYPTED DM PASSCODE**: Twitter may show an "Enter your passcode" or "encrypted_dm_passcode_required" dialog before you can access DMs. If you see this dialog:
   a. Find the passcode input field in the snapshot
   b. Type the passcode: $TWITTER_DM_PASSCODE
   c. Click "Confirm" or press Enter
   d. Wait for the DM inbox to load
   The passcode is loaded from .env as TWITTER_DM_PASSCODE.
3. Start new message to THEIR_AUTHOR
4. Type and send the message.

## After each DM:

Inspect the send_dm tool's return value. There are exactly three outcomes:

(A) ok=true AND verified=true  ->  success, mark sent:
  CLAUDE_SESSION_ID=$CLAUDE_SESSION_ID python3 $REPO_DIR/scripts/dm_send_log.py \\
      --dm-id DM_ID --message "DM_TEXT" --verified

  Do NOT issue a raw "UPDATE dms SET status='sent'" psql command. The
  dm_send_log.py script is the only path that may flip status to 'sent';
  it requires --verified, and refuses without it. This is intentional:
  prior phantom-DM bugs (~700 rows in 4/2026) came from prose-driven
  status flips that ignored the verification result.

  After the DM lands, capture the current page URL by evaluating window.location.href in the browser (use the JS-eval tool from the BROWSER BACKEND block) and stamp it onto the DM row so the dashboard's "open chat" button works:
  python3 $REPO_DIR/scripts/dm_conversation.py set-url --dm-id DM_ID --url "CHAT_URL"
  The validator only accepts /i/chat/<id> or /messages/<id>; if the URL is something else (you got bounced to a profile or inbox), skip this step.

(B) ok=false OR verified=false  ->  send did not land, mark error via /api/v1/dms/DM_ID PATCH (DO NOT shell out to psql):
  python3 $REPO_DIR/scripts/dm_db_update.py --dm-id DM_ID --status error --skip-reason send_unverified --claude-session-id "$CLAUDE_SESSION_ID"

(C) Rate limit, account blocked, or any other thrown exception:
  python3 $REPO_DIR/scripts/dm_db_update.py --dm-id DM_ID --status error --skip-reason REASON --claude-session-id "$CLAUDE_SESSION_ID"

DMs disabled (recipient setting, not a send failure):
  python3 $REPO_DIR/scripts/dm_db_update.py --dm-id DM_ID --status skipped --skip-reason chat_disabled --claude-session-id "$CLAUDE_SESSION_ID"

CRITICAL: ALL browser calls MUST use the tools listed in the BROWSER BACKEND block above (the Twitter-dedicated MCP for this run). NEVER use generic mcp__playwright-extension__*, mcp__isolated-browser__*, or mcp__macos-use__* tools. If a browser tool call is blocked or times out, wait 30 seconds and retry (up to 3 times). Do NOT fall back to any other browser tool.

## CRITICAL FAILURE MODE: Twitter browser tools not registered
If at the START of this run you cannot see ANY of the browser tools listed in the BROWSER BACKEND block above, OR every browser call fails with "MCP server not connected" / "no such tool" / similar, this is a transient infrastructure failure (Chrome profile collision, wedged MCP wrapper, lock acquired but profile still held by another process). It is NOT an error condition for the prospects in the queue.

Do EXACTLY this:
1. Make NO database changes. Do NOT mark any row as 'error', 'skipped', or anything else.
2. Print a single line to stdout: \`MCP_UNAVAILABLE: twitter browser tools not registered; aborting with rows left at status=pending\`
3. Exit cleanly. The launchd schedule will retry on the next cycle, by which point the wedged profile holder will likely have timed out.

Burning rows with skip_reason='twitter_agent_mcp_unavailable: ...' is a regression that on 2026-05-12 nuked 7 warm leads (efemjoba, gpuops, josesaezmerino, AIDailyGems, alkimiadev, RobertDMellish, kunaljeweller). The d.id IS NULL filter in scan_dm_candidates.py then permanently blocked them from re-discovery. Do not do this.
PROMPT_EOF

gtimeout 2700 "$REPO_DIR/scripts/run_claude.sh" "dm-outreach-twitter" --strict-mcp-config --mcp-config "$MCP_CONFIG_FILE" --output-format stream-json --verbose -p "${BROWSER_INSTRUCTIONS}

$(cat "$PROMPT_FILE")" 2>&1 | tee -a "$LOG_FILE" || log "WARNING: Twitter DM outreach claude exited with code $?"
rm -f "$PROMPT_FILE"

# Belt-and-suspenders: if Claude (despite the prompt instructions added 2026-05-13)
# marked any row with skip_reason='twitter_agent_mcp_unavailable: ...' or
# 'mcp_unavailable' or similar transient-MCP signals THIS run, revert it back
# to status='pending' so the next cycle can retry. Scoped to this run via
# claude_session_id to avoid clobbering legitimate older rows.
#
# The scan_dm_candidates.py discover query uses `LEFT JOIN dms ... WHERE d.id IS NULL`
# so once a reply has any dms row (even status='error'), it never re-appears as a
# candidate. That's how the 2026-05-12 incident permanently lost 7 warm leads.
# This revert closes the gap by ensuring transient-MCP failures don't lock rows
# into a status=error state that the scanner can't see past.
# MCP-failure recovery sweep now lives at /api/v1/dms/recover-mcp-failures
# (same UPDATE WHERE filter, same RETURNING shape). Helper prints the
# recovered_count integer so this $() capture is byte-equivalent.
RECOVERED=$(python3 "$REPO_DIR/scripts/dm_outreach_twitter_helper.py" \
    recover-mcp --session-id "$CLAUDE_SESSION_ID" 2>/dev/null || echo "0")
if [ "$RECOVERED" -gt 0 ]; then
    log "Reverted $RECOVERED row(s) from status='error' (transient MCP failure) back to pending"
fi

# Final summary counts: one HTTP roundtrip via /api/v1/dms/counts vs the
# two psql one-liners. Helper prints "<sent> <pending>" space-separated.
_DM_SUMMARY=$(python3 "$REPO_DIR/scripts/dm_outreach_twitter_helper.py" summary 2>/dev/null || echo "0 0")
SENT=$(echo "$_DM_SUMMARY" | awk '{print $1}')
STILL_PENDING=$(echo "$_DM_SUMMARY" | awk '{print $2}')
: "${SENT:=0}"
: "${STILL_PENDING:=0}"
log "Twitter DM outreach summary: sent (all-time)=$SENT, still_pending=$STILL_PENDING"

RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
_COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START" --scripts "dm-outreach-twitter" 2>/dev/null || echo "0.0000")
python3 "$REPO_DIR/scripts/log_run.py" --script "dm_outreach_twitter" --posted 0 --skipped 0 --failed 0 --cost "$_COST" --elapsed "$RUN_ELAPSED"

find "$LOG_DIR" -name "dm-outreach-twitter-*.log" -mtime +7 -delete 2>/dev/null || true

log "=== Twitter DM outreach complete: $(date) ==="
