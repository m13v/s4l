#!/usr/bin/env bash
# linkedin-presence.sh - read-only LinkedIn session presence pass.
#
# Purpose:
#   Run a bounded, auditable browser pass in the real linkedin-harness Chrome.
#   It only views first-party LinkedIn surfaces and performs small scroll passes.
#   It does not like, follow, connect, message, comment, expand comments, or open
#   post permalinks.
#
# This is intentionally a Claude/harness pipeline, not a Python CDP action
# helper, so it stays inside the same LinkedIn browser-action boundary as the
# rest of the repo.

set -euo pipefail

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/linkedin-presence-$(date +%Y-%m-%d_%H%M%S).log"
RUN_START=$(date +%s)
BATCH_ID="lipres-$(date +%Y%m%d_%H%M%S)-$$"
export SA_CYCLE_ID="$BATCH_ID"
export SAPS_PIPELINE_NAME="linkedin-presence"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

if [ -f "$HOME/.claude/social-autoposter/linkedin.killswitch" ]; then
    log "LINKEDIN_KILLSWITCH active. Skipping LinkedIn presence pass."
    exit 0
fi

# Optional local kill switch for operators who want the plist loaded but dormant.
if [ "${LINKEDIN_PRESENCE_ENABLED:-1}" = "0" ]; then
    log "LINKEDIN_PRESENCE_ENABLED=0. Skipping."
    exit 0
fi

DRY_RUN="${LINKEDIN_PRESENCE_DRY_RUN:-0}"

# The launchd timer is fixed; vary each actual pass inside the script. Skipped
# passes do not write run_monitor rows, so the dashboard history only shows real
# browser activity.
RUN_PCT="${LINKEDIN_PRESENCE_RUN_PCT:-65}"
if [ "$DRY_RUN" != "1" ]; then
    ROLL=$(( RANDOM % 100 ))
    if [ "$ROLL" -ge "$RUN_PCT" ]; then
        log "Presence pass skipped by schedule jitter (roll=$ROLL threshold=$RUN_PCT)."
        exit 0
    fi
    JITTER_MAX="${LINKEDIN_PRESENCE_JITTER_MAX_SEC:-900}"
    if [ "$JITTER_MAX" -gt 0 ]; then
        JITTER=$(( RANDOM % (JITTER_MAX + 1) ))
        log "Sleeping ${JITTER}s before presence pass."
        sleep "$JITTER"
    fi
fi

MODE_ROLL=$(( RANDOM % 4 ))
case "$MODE_ROLL" in
    0) MODE="feed"; TARGET_URL="https://www.linkedin.com/feed/" ;;
    1) MODE="notifications"; TARGET_URL="https://www.linkedin.com/notifications/" ;;
    2) MODE="messaging"; TARGET_URL="https://www.linkedin.com/messaging/" ;;
    *) MODE="profile"; TARGET_URL="https://www.linkedin.com/in/me/" ;;
esac

SCROLLS=$(( 1 + (RANDOM % 3) ))
DWELL_A=$(( 2 + (RANDOM % 4) ))
DWELL_B=$(( 2 + (RANDOM % 4) ))
DWELL_C=$(( 2 + (RANDOM % 4) ))
SCROLL_A=$(( 420 + (RANDOM % 260) ))
SCROLL_B=$(( 420 + (RANDOM % 260) ))
SCROLL_C=$(( 420 + (RANDOM % 260) ))

log "=== LinkedIn Presence Run: $(date) (batch=$BATCH_ID mode=$MODE scrolls=$SCROLLS) ==="

# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

source "$REPO_DIR/skill/lock.sh"
source "$REPO_DIR/skill/lib/linkedin-backend.sh"

PROMPT_FILE="$(mktemp -t saps-linkedin-presence.XXXXXX)"
cleanup() {
    rm -f "$PROMPT_FILE" 2>/dev/null || true
    rm -f "$HOME/.claude/linkedin-agent-lock.json" 2>/dev/null || true
}
trap cleanup EXIT INT TERM HUP

cat > "$PROMPT_FILE" <<PROMPT_EOF
You are running a read-only LinkedIn presence pass for Social Autoposter.

$BROWSER_INSTRUCTIONS

Task:
- Mode: $MODE
- URL: $TARGET_URL
- Scroll passes: $SCROLLS
- Scroll amounts, in order: $SCROLL_A, $SCROLL_B, $SCROLL_C
- Dwell seconds, in order: $DWELL_A, $DWELL_B, $DWELL_C

Hard rules:
- Use only mcp__linkedin-harness__bh_run.
- Do not post, comment, react, like, repost, follow, connect, send messages, or submit forms.
- Do not open individual post permalinks.
- Do not click "Show more comments", "Load earlier replies", "See more", or any comment-expansion control.
- Do not call /voyager/api/*, fetch(), XHR, or any internal LinkedIn endpoint.
- If a login, checkpoint, captcha, authwall, or verify-you-are-human page appears, print exactly SESSION_INVALID and stop. Do not try to log in.
- In messaging mode, stay on the messaging sidebar/list. Do not open a conversation and do not read private thread contents.

Workflow:
1. Reuse the existing tab and navigate to $TARGET_URL:
   bh_run('goto_url("$TARGET_URL"); wait_for_load()')
2. Read the current URL and a small page text sample using js(). If the URL or page text indicates login/checkpoint/captcha/authwall, print SESSION_INVALID and stop.
3. Capture one screenshot and read it so you visually confirm the surface loaded.
4. Perform exactly $SCROLLS bounded scroll pass(es) on the loaded surface using the bh_run scroll(direction, amount) helper, with the amounts/dwells above. Do not use window.scrollBy. Do not click anything.
5. Capture one final screenshot.
6. Print exactly one summary line:
   LINKEDIN_PRESENCE_SUMMARY: mode=$MODE pages=1 scrolls=$SCROLLS session=ok

Keep the run short and quiet. This is a read-only session maintenance pass, not discovery, scraping, or engagement.
PROMPT_EOF

if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN: would run mode=$MODE url=$TARGET_URL scrolls=$SCROLLS"
    exit 0
fi

acquire_lock "linkedin-browser" 1800
ensure_linkedin_browser_for_backend 2>&1 | tee -a "$LOG_FILE"

TIMEOUT_BIN="$(command -v gtimeout || command -v timeout || true)"
PRESENCE_RC=0
set +e
if [ -n "$TIMEOUT_BIN" ]; then
    "$TIMEOUT_BIN" 900 "$REPO_DIR/scripts/run_claude.sh" "linkedin-presence" \
        --strict-mcp-config --mcp-config "$MCP_CONFIG_FILE" \
        --output-format stream-json --verbose \
        -p "$(cat "$PROMPT_FILE")" 2>&1 | tee -a "$LOG_FILE"
else
    "$REPO_DIR/scripts/run_claude.sh" "linkedin-presence" \
        --strict-mcp-config --mcp-config "$MCP_CONFIG_FILE" \
        --output-format stream-json --verbose \
        -p "$(cat "$PROMPT_FILE")" 2>&1 | tee -a "$LOG_FILE"
fi
PRESENCE_RC=${PIPESTATUS[0]}
set -e

release_lock "linkedin-browser"

ELAPSED=$(( $(date +%s) - RUN_START ))
COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" \
    --since "$RUN_START" --scripts "linkedin-presence" 2>/dev/null || echo "0.0000")

FAILED=0
FAILURE_REASONS=""
if grep -q "SESSION_INVALID" "$LOG_FILE" 2>/dev/null; then
    FAILED=1
    FAILURE_REASONS="session_invalid:1"
elif [ "$PRESENCE_RC" -ne 0 ]; then
    FAILED=1
    if [ "$PRESENCE_RC" = "124" ]; then
        FAILURE_REASONS="timeout:1"
    else
        FAILURE_REASONS="claude_or_browser_error:1"
    fi
fi

python3 "$REPO_DIR/scripts/log_run.py" --script "presence_linkedin" \
    --posted 0 --skipped 0 --failed "$FAILED" \
    --cost "$COST" --elapsed "$ELAPSED" \
    --scanned 1 --checked 1 \
    --scan "pages=1,scrolls=$SCROLLS" \
    ${FAILURE_REASONS:+--failure-reasons "$FAILURE_REASONS"} \
    2>/dev/null || true

find "$LOG_DIR" -name "linkedin-presence-*.log" -mtime +14 -delete 2>/dev/null || true
log "=== LinkedIn presence complete: $(date) rc=$PRESENCE_RC failed=$FAILED ==="
