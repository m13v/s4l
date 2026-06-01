#!/usr/bin/env bash
# dm-outreach-linkedin.sh — Outbound LinkedIn DM outreach.
# Scans for DM candidates (users who engaged on our posts), then sends LinkedIn
# messages to continue the conversation. Inbound DM replies are handled separately
# by engage-dm-replies-linkedin.sh.
# Called by launchd (com.m13v.social-dm-outreach-linkedin) every 6 hours.

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

# Cycle ID for cross-cycle cost accounting (see run-linkedin.sh / engage-linkedin.sh
# for the same pattern). Stamps claude_sessions.cycle_id via env inheritance.
BATCH_ID="${BATCH_ID:-dmli-$(date +%Y%m%d-%H%M%S)}"
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
acquire_lock "dm-outreach-linkedin" 2700

# Load secrets
# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
SKILL_FILE="$REPO_DIR/SKILL.md"
LOG_DIR="$REPO_DIR/skill/logs"

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/dm-outreach-linkedin-$(date +%Y-%m-%d_%H%M%S).log"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

RUN_START=$(date +%s)
log "=== LinkedIn DM Outreach Run: $(date) ==="

# DB-free since 2026-06-01: all DM state goes through the s4l.ai HTTP API
# (X-Installation auth). No DATABASE_URL needed.
PY_BIN="$(command -v python3 || echo /usr/bin/python3)"

# dm_count <status> -> integer count of linkedin dms in that status.
# Backed by GET /api/v1/dms/counts (same shape as /api/v1/replies/counts).
dm_count() {
    "$PY_BIN" -c "
import sys; sys.path.insert(0, '$REPO_DIR/scripts')
from http_api import api_get
resp = api_get('/api/v1/dms/counts', {'platform': 'linkedin'})
counts = ((resp or {}).get('data') or {}).get('counts') or []
want = '$1'
print(next((int(r.get('count', 0)) for r in counts if r.get('status') == want), 0))
" 2>/dev/null || echo 0
}

# Scan for new DM candidates first (cheap Python, writes to dms table)
log "Scanning for DM candidates (all platforms)..."
(PYTHONUNBUFFERED=1 python3 "$REPO_DIR/scripts/scan_dm_candidates.py" 2>&1 || true) | tee -a "$LOG_FILE"

DM_PENDING=$(dm_count pending)

if [ "$DM_PENDING" -eq 0 ]; then
    log "No pending LinkedIn DMs"
    python3 "$REPO_DIR/scripts/log_run.py" --script "dm_outreach_linkedin" --posted 0 --skipped 0 --failed 0 --cost 0 --elapsed $(( $(date +%s) - RUN_START ))
    exit 0
fi

log "LinkedIn: $DM_PENDING DMs to send"

# Pull the pending DM batch + 60-day cross-thread engagement via the
# outreach-queue endpoint (no DATABASE_URL). The route mirrors the old
# json_agg join exactly and returns {rows:[...]}. We extract the array so
# DM_DATA keeps the same JSON shape the prompt consumed before the migration.
DM_DATA=$("$PY_BIN" -c "
import json, sys
sys.path.insert(0, '$REPO_DIR/scripts')
from http_api import api_get
resp = api_get('/api/v1/dms/outreach-queue', {'platform': 'linkedin', 'status': 'pending', 'limit': 200})
rows = (resp.get('data') or {}).get('rows') or []
print(json.dumps(rows))
" 2>/dev/null || echo "[]")

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
You are the Social Autoposter LinkedIn DM outreach bot.

$BROWSER_INSTRUCTIONS

Read $SKILL_FILE for content rules (tone, anti-AI detection, no em dashes).

## Task: Send LinkedIn messages to continue comment conversations

These users engaged with our LinkedIn posts/comments. We already replied publicly. Now send a short, casual message to continue the conversation.

CRITICAL RULES:
1. Messages must feel like a natural continuation of the comment discussion, NOT a cold outreach or sales pitch
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
- "Hey! I noticed your comment on LinkedIn. I'm building something you might find interesting..." (cold pitch)
- "Great point! I'd love to connect and share what we're working on." (generic)
- "Hi there, I saw your insightful comment about AI agents..." (too formal)

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

For each DM row, BEFORE you compose or send, do this in order. USE the bh_run tool from the BROWSER BACKEND block ONLY (follow its translation table for any Playwright-style step below); NEVER call /voyager/api/; NEVER run Python CDP scripts against LinkedIn.

1. Look at the row's \`target_project\`. If it's NULL, set icp_precheck=unknown with notes="no_target_project" and proceed to step 4 — but still try to capture profile basics.

2. Fetch the prospect's LinkedIn profile:
   - From the original comment thread (r.their_comment_url), click into THEIR_AUTHOR's profile link, OR
   - Search LinkedIn for THEIR_AUTHOR from the messaging UI once you have them open.
   - Read their profile header DOM (bh_run with js(...) per the translation table, or capture_screenshot + Read the PNG). Extract: headline, current company, current role, a short summary of their About/experience top section, and (if visible) 1-2 recent posts/activity items.
   - If you hit a login/checkpoint, STOP and print SESSION_INVALID; do NOT attempt to log in.
   - If the profile is private or shows only a minimal header, record what you can and note "profile_limited".

3. Persist the profile fields:
   \`\`\`bash
   python3 $REPO_DIR/scripts/fetch_prospect_profile.py upsert \\
       --platform linkedin --author "THEIR_AUTHOR" \\
       --profile-url "PROFILE_URL" \\
       --headline "HEADLINE_FROM_PROFILE" \\
       --company "CURRENT_COMPANY" \\
       --role "CURRENT_ROLE" \\
       --bio "SHORT_ABOUT_OR_SUMMARY" \\
       --recent-activity "1-2 LINE RECENT ACTIVITY SUMMARY" \\
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

## How to send messages on LinkedIn (use the bh_run tool):
1. Navigate to https://www.linkedin.com/messaging/ (bh_run: new_tab/goto_url + wait_for_load)
2. Start new message to THEIR_AUTHOR
3. Type and send the message. Click the message box (click_at_xy) then type_text; click the Send button via click_at_xy. Do NOT press Enter (Enter inserts a newline in LinkedIn's contenteditable).

## After each DM:

Inspect the send result (capture_screenshot + Read the PNG to confirm the message appeared in the thread). There are exactly three outcomes:

(A) The message was actually delivered (you saw it appear in the thread, no error toast)  ->  mark sent via the verified gateway:
  CLAUDE_SESSION_ID=$CLAUDE_SESSION_ID python3 $REPO_DIR/scripts/dm_send_log.py \\
      --dm-id DM_ID --message "DM_TEXT" --verified

  Do NOT issue a raw "UPDATE dms SET status='sent'" psql command and do NOT call
  dm_conversation.py log-outbound directly. dm_send_log.py is the ONLY path that
  may flip status to 'sent'; it requires --verified, and refuses without it. It
  also forwards to log-outbound internally with --verified, so dm_messages stays
  in sync. This is intentional: prior phantom-DM bugs (April 2026 LinkedIn Haiku
  cycle inserted 5 phantom outbound rows because the prompt let the LLM call
  log-outbound without a verified send) came from bypassing this gateway.

(B) The send did not land (no toast, message did not appear in thread)  ->  mark error:
  python3 $REPO_DIR/scripts/dm_db_update.py --dm-id DM_ID --status error --skip-reason send_unverified --claude-session-id "$CLAUDE_SESSION_ID"

(C) DMs disabled / chat blocked  ->  mark skipped:
  python3 $REPO_DIR/scripts/dm_db_update.py --dm-id DM_ID --status skipped --skip-reason chat_disabled --claude-session-id "$CLAUDE_SESSION_ID"

(D) Rate limit, account checkpoint, or any other thrown exception  ->  mark error and STOP the run:
  python3 $REPO_DIR/scripts/dm_db_update.py --dm-id DM_ID --status error --skip-reason "REASON" --claude-session-id "$CLAUDE_SESSION_ID"

CRITICAL: ALL browser calls MUST use the mcp__linkedin-harness__bh_run tool (the BROWSER BACKEND block above). NEVER use generic mcp__playwright-extension__*, mcp__isolated-browser__*, or mcp__macos-use__* tools. If a bh_run call is blocked or times out, wait 30 seconds and retry (up to 3 times). Do NOT fall back to any other browser tool.
PROMPT_EOF

ensure_linkedin_browser_for_backend 2>&1 | tee -a "$LOG_FILE"
gtimeout 2700 "$REPO_DIR/scripts/run_claude.sh" "dm-outreach-linkedin" --strict-mcp-config --mcp-config "$MCP_CONFIG_FILE" --output-format stream-json --verbose -p "$(cat "$PROMPT_FILE")" 2>&1 | tee -a "$LOG_FILE" || log "WARNING: LinkedIn DM outreach claude exited with code $?"
rm -f "$PROMPT_FILE"

SENT=$(dm_count sent)
STILL_PENDING=$(dm_count pending)
log "LinkedIn DM outreach summary: sent (all-time)=$SENT, still_pending=$STILL_PENDING"

RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
_COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START" --scripts "dm-outreach-linkedin" 2>/dev/null || echo "0.0000")
python3 "$REPO_DIR/scripts/log_run.py" --script "dm_outreach_linkedin" --posted 0 --skipped 0 --failed 0 --cost "$_COST" --elapsed "$RUN_ELAPSED"

find "$LOG_DIR" -name "dm-outreach-linkedin-*.log" -mtime +7 -delete 2>/dev/null || true

log "=== LinkedIn DM outreach complete: $(date) ==="
