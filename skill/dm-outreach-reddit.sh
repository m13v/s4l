#!/usr/bin/env bash
# dm-outreach-reddit.sh — Outbound Reddit DM outreach.
# Scans for DM candidates (users who engaged on our posts), then sends Reddit DMs
# to continue the conversation. Inbound DM replies are handled separately by
# engage-dm-replies-reddit.sh.
# Called by launchd (com.m13v.social-dm-outreach-reddit) every 6 hours.

set -euo pipefail

# Cycle ID for cross-cycle cost accounting (matches the pattern in
# run-reddit-search.sh, engage-reddit.sh, etc.). Every claude session spawned
# in this script inherits SA_CYCLE_ID via env so log_claude_session.py stamps
# claude_sessions.cycle_id. Lets get_run_cost.py --cycle-id report THIS run's
# spend instead of bleeding into overlapping outreach cycles.
BATCH_ID="${BATCH_ID:-dmrd-$(date +%Y%m%d-%H%M%S)}"
export BATCH_ID
export SA_CYCLE_ID="$BATCH_ID"

# Pipeline lock at top (only-one-of-us guard). We DO NOT acquire
# reddit-browser at the bash level anymore — claude itself acquires it
# per-DM via scripts/reddit_browser_lock.py, only around the actual MCP
# browser operations (profile fetch + compose DM, ~30-90s per DM). This
# unblocks peer reddit pipelines (engage-reddit, dm-replies-reddit,
# link-edit-reddit, post-reddit) during the DB scan, prompt build, and
# HTTP PATCH update phases of each DM row.
source "$(dirname "$0")/lock.sh"
# reddit-harness backend (2026-05-29). Sets MCP_CONFIG_FILE (reddit-harness MCP),
# BROWSER_INSTRUCTIONS (bh_run tool surface + translation table), exports
# REDDIT_CDP_URL=:9557, and provides ensure_reddit_browser_for_backend.
# Source after lock.sh, before acquire_lock / claude -p.
source "$(dirname "$0")/lib/reddit-backend.sh"
acquire_lock "dm-outreach-reddit" 2700

# Load secrets
# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
SKILL_FILE="$REPO_DIR/SKILL.md"
LOG_DIR="$REPO_DIR/skill/logs"

# 2026-05-12 migration: bash-level DB access moved off psql / DATABASE_URL
# onto HTTP routes (/api/v1/dms*, /api/v1/dms/outreach-queue). The shell no
# longer needs Postgres credentials; everything flows through
# scripts/dm_outreach_helper.py which calls the website.
# DATABASE_URL may still be defined in .env for other tooling; we don't
# require it here. The Python http_api layer expects AUTOPOSTER_API_BASE
# (defaults to https://s4l.ai) and an installation identity header.

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/dm-outreach-reddit-$(date +%Y-%m-%d_%H%M%S).log"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

RUN_START=$(date +%s)
log "=== Reddit DM Outreach Run: $(date) ==="

# Scan for new DM candidates first (cheap Python, writes to dms table)
log "Scanning for DM candidates (all platforms)..."
(PYTHONUNBUFFERED=1 python3 "$REPO_DIR/scripts/scan_dm_candidates.py" 2>&1 || true) | tee -a "$LOG_FILE"

DM_PENDING=$(python3 "$REPO_DIR/scripts/dm_outreach_helper.py" count --platform reddit --status pending 2>/dev/null || echo "0")

if [ "$DM_PENDING" -eq 0 ]; then
    log "No pending Reddit DMs"
    python3 "$REPO_DIR/scripts/log_run.py" --script "dm_outreach_reddit" --posted 0 --skipped 0 --failed 0 --cost 0 --elapsed $(( $(date +%s) - RUN_START ))
    exit 0
fi

log "Reddit: $DM_PENDING DMs to send"

DM_DATA=$(python3 "$REPO_DIR/scripts/dm_outreach_helper.py" outreach-queue \
    --platform reddit --status pending --limit 200 \
    --other-engagement-days 60 2>/dev/null || echo "[]")

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
You are the Social Autoposter Reddit DM outreach bot.

Read $SKILL_FILE for content rules (tone, anti-AI detection, no em dashes).

$BROWSER_INSTRUCTIONS

## Task: Send Reddit DMs to continue comment conversations

These users engaged with our Reddit posts/comments. We already replied publicly. Now send a short, casual DM (Reddit Chat) to continue the conversation.

CRITICAL RULES:
1. DMs must feel like a natural continuation of the comment discussion, NOT a cold outreach or sales pitch
2. Reference the specific conversation topic, not generic "hey I saw your comment"
3. Keep it short: 1-2 sentences max, like a text message
4. No links in the first DM; earn the conversation first
5. No em dashes. Write casually, like texting a coworker.

EXECUTION MODEL — STRICT SEQUENTIAL, NO BATCHING (read this twice):
- Process DMs ONE AT A TIME. Run the FULL chain (profile fetch → ICP pre-check → compose → send → log) end-to-end for DM N before reading DM N+1.
- The reddit-browser lock is NOT held by the parent shell. You acquire/release it explicitly per DM (steps 1.5 and 7.5 below), so peer reddit pipelines can use the browser during your DB queries, ICP scoring, and dm_outreach_helper.py PATCH calls.
- The lock has a 90s lease that auto-renews on every reddit-agent MCP call (PreToolUse / PostToolUse hooks), so you do NOT need to manually heartbeat. Just acquire before the first browser call for the DM, release after the last one.

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
- "Hey! I noticed your comment on Reddit. I'm building something you might find interesting..." (cold pitch)
- "Great point! I'd love to connect and share what we're working on." (generic)
- "Hi there - I saw your insightful comment about AI agents..." (too formal)

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

1. Look at the row's \`target_project\`. If it's NULL, skip the ICP evaluation (set icp_precheck=unknown with notes="no_target_project") and move to step 4 — but still fetch the profile if it's cheap.

1.5. ACQUIRE the reddit-browser lock NOW (just before any reddit-agent browser call for this DM). This is the ONLY moment you may touch the browser for this DM:
   \`\`\`bash
   LOCK_OUT=\$(python3 $REPO_DIR/scripts/reddit_browser_lock.py acquire --timeout 600 2>&1)
   \`\`\`
   - If stdout starts with "OK", proceed to step 2.
   - If "BUSY", a peer reddit pipeline owns the browser and didn't release within 10 min. Mark this DM error/transient and move on to the NEXT DM:
     \`python3 $REPO_DIR/scripts/dm_outreach_helper.py patch --id DM_ID --status error --skip-reason reddit_browser_busy --claude-session-id $CLAUDE_SESSION_ID\`
   - If "ERROR", same handling as BUSY: mark error, move on. Do NOT call browser tools without the lock — collisions on the same chrome profile crash both runs.

2. Fetch the prospect's Reddit profile with the browser backend (mcp__reddit-harness__bh_run, see BROWSER BACKEND block above):
   - Navigate to https://www.reddit.com/user/THEIR_AUTHOR/
   - Read the page (snapshot / capture_screenshot per the translation table). Pull:
     - the profile bio/tagline (text under their name)
     - karma numbers (post + comment karma)
     - a 1-2 line summary of their most recent 3-5 posts/comments (titles + subreddits)
   - If the profile page is a login wall, deleted, or suspended user, record notes="profile_inaccessible" and proceed with icp_precheck=unknown.

3. Persist the profile fields via:
   \`\`\`bash
   python3 $REPO_DIR/scripts/fetch_prospect_profile.py upsert \\
       --platform reddit --author "THEIR_AUTHOR" \\
       --profile-url "https://www.reddit.com/user/THEIR_AUTHOR/" \\
       --headline "SHORT_TAGLINE_OR_BIO_FIRST_LINE" \\
       --bio "FULL_BIO_TEXT" \\
       --recent-activity "SHORT_3-5_ITEM_SUMMARY" \\
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

## How to send DMs on Reddit (use the browser backend, mcp__reddit-harness__bh_run):
1. Navigate to https://www.reddit.com/message/compose/?to=THEIR_AUTHOR
2. Reddit uses Chat now. Fill in subject (2-4 casual words) and body.
3. Submit. The send_dm / compose_dm tool returns a JSON object with an
   "ok" field and a "verified" field. The send only counts if BOTH are true.

## After each DM:

Inspect the tool's return value. There are exactly three outcomes:

(A) ok=true AND verified=true  ->  success, mark sent:
  CLAUDE_SESSION_ID=$CLAUDE_SESSION_ID python3 $REPO_DIR/scripts/dm_send_log.py \\
      --dm-id DM_ID --message "DM_TEXT" --verified

  Do NOT use dm_outreach_helper.py to set status='sent'. The helper
  refuses, and dm_send_log.py is the only path that may flip status to
  'sent'; it requires --verified, and refuses without it. This is
  intentional: prior phantom-DM bugs (~700 rows in 4/2026) came from
  prose-driven status flips that ignored the verification result.

(B) ok=false OR verified=false  ->  send did not land, mark error:
  python3 $REPO_DIR/scripts/dm_outreach_helper.py patch --id DM_ID --status error --skip-reason send_unverified --claude-session-id $CLAUDE_SESSION_ID

(C) Rate limit, account blocked, or any other thrown exception:
  python3 $REPO_DIR/scripts/dm_outreach_helper.py patch --id DM_ID --status error --skip-reason REASON --claude-session-id $CLAUDE_SESSION_ID

DMs/Chat disabled (recipient setting, not a send failure):
  python3 $REPO_DIR/scripts/dm_outreach_helper.py patch --id DM_ID --status skipped --skip-reason chat_disabled --claude-session-id $CLAUDE_SESSION_ID

7.5. RELEASE the reddit-browser lock IMMEDIATELY after the DM result is logged (success, error, or skip). This is mandatory — failing to release blocks every other reddit pipeline:
   \`\`\`bash
   python3 $REPO_DIR/scripts/reddit_browser_lock.py release
   \`\`\`
   Run this even if step 2 (profile fetch) raised, the send threw, or you're skipping the DM. Wrap the per-DM browser block (steps 2 → 7) in a way that step 7.5 ALWAYS executes (mental try/finally). The release is idempotent and safe to call multiple times. Move to the NEXT DM only after the release.

CRITICAL: ALL browser calls MUST use mcp__reddit-agent__* tools. NEVER use generic mcp__playwright-extension__*, mcp__isolated-browser__*, or mcp__macos-use__* tools. If a reddit-agent tool call is blocked or times out, wait 30 seconds and retry (up to 3 times). Do NOT fall back to any other browser tool.
PROMPT_EOF

# NOTE: We do NOT acquire reddit-browser at the bash level. Claude itself
# acquires/releases it per DM via scripts/reddit_browser_lock.py
# (steps 1.5 and 7.5 in the prompt). This keeps the lock held only during
# the actual ~30-90s reddit-agent browser ops per DM (profile fetch +
# compose), not the full ~45-min run. Peer pipelines (engage-reddit,
# dm-replies-reddit, link-edit-reddit, post-reddit) can use the profile
# during our DB queries, ICP scoring, and HTTP PATCH update phases.
#
# Pre-flight: ensure the profile isn't wedged by a prior crashed run.
# Unified lock (2026-05-10): brief Python acquire+release so the orphan-Chrome
# sweep happens once before claude starts. Python acquire honors expires_at,
# so a TTL-stale-but-PID-alive holder gets reclaimed automatically instead of
# blocking us for the full bash timeout.
log "Pre-flight: sweep orphan reddit-agent Chrome / playwright-mcp before handing off to claude..."
python3 "$REPO_DIR/scripts/reddit_browser_lock.py" acquire --timeout 60 --ttl 30 2>&1 | tee -a "$LOG_FILE" || \
    log "WARNING: reddit_browser_lock.py pre-flight acquire failed; proceeding (claude will retry per-DM)."
ensure_browser_healthy "reddit"
python3 "$REPO_DIR/scripts/reddit_browser_lock.py" release 2>/dev/null || true

gtimeout 2700 "$REPO_DIR/scripts/run_claude.sh" "dm-outreach-reddit" --strict-mcp-config --mcp-config "$HOME/.claude/browser-agent-configs/reddit-agent-mcp.json" --output-format stream-json --verbose -p "$(cat "$PROMPT_FILE")" 2>&1 | tee -a "$LOG_FILE" || log "WARNING: Reddit DM outreach claude exited with code $?"
rm -f "$PROMPT_FILE"

# Belt-and-suspenders: if claude exited without releasing the lock (e.g.
# crashed mid-DM before reaching step 7.5), free it now so peer
# pipelines aren't stuck behind a phantom holder. release_lock checks
# the lock_dir and rm-rf's it; safe even if claude already released.
python3 "$REPO_DIR/scripts/reddit_browser_lock.py" release 2>/dev/null || true

SENT=$(python3 "$REPO_DIR/scripts/dm_outreach_helper.py" count --platform reddit --status sent 2>/dev/null || echo "0")
STILL_PENDING=$(python3 "$REPO_DIR/scripts/dm_outreach_helper.py" count --platform reddit --status pending 2>/dev/null || echo "0")
log "Reddit DM outreach summary: sent (all-time)=$SENT, still_pending=$STILL_PENDING"

RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
_COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START" --scripts "dm-outreach-reddit" 2>/dev/null || echo "0.0000")
python3 "$REPO_DIR/scripts/log_run.py" --script "dm_outreach_reddit" --posted 0 --skipped 0 --failed 0 --cost "$_COST" --elapsed "$RUN_ELAPSED"

find "$LOG_DIR" -name "dm-outreach-reddit-*.log" -mtime +7 -delete 2>/dev/null || true

log "=== Reddit DM outreach complete: $(date) ==="
