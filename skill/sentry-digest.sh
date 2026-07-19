#!/usr/bin/env bash
# sentry-digest.sh — critical (level:error/fatal) Sentry issue digest for the
# s4l Sentry project. Replaces the raw per-issue Sentry alert email.
#
# Two-step design (mirrors ~/fazm/inbox/skill/check-query-insights.sh):
#   1. scripts/sentry_digest.py pulls issues + diffs against the ledger
#      (mechanical, no LLM). If nothing is NEW or GROWING, exit here, no
#      Claude spawn, no email, no ledger write (ledger is only ever written
#      by the investigating Claude session, see SENTRY-DIGEST-SKILL.md).
#   2. If something changed, spawn Claude to actually investigate (read the
#      crashing code, check git log, judge actionability) and send ONE
#      human-readable email, not a raw data dump.
#
# Wired by launchd/com.m13v.s4l-sentry-digest.plist (every 4 hours).

set -euo pipefail

# claude (claude-account-rotator wrapper) and gtimeout must be resolvable
# regardless of how this script is invoked; launchd's plist PATH covers this,
# but keep it explicit so a manual run also works.
export PATH="$HOME/.nvm/versions/node/v20.19.4/bin:/opt/homebrew/bin:/usr/local/bin:$HOME/claude-account-rotator/bin:$PATH"

source "$(dirname "$0")/lock.sh"
acquire_lock "sentry-digest" 1800

REPO_DIR="$HOME/social-autoposter"
SCRIPTS_DIR="$REPO_DIR/scripts"
LOG_DIR="$REPO_DIR/skill/logs"
LEDGER_PATH="$REPO_DIR/scripts/state/sentry_digest_ledger.json"
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/claude-account-rotator/bin/claude}"
PYTHON_BIN="${PYTHON_BIN:-/opt/homebrew/bin/python3.11}"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="/usr/bin/python3"
mkdir -p "$LOG_DIR"

TS=$(date +%Y-%m-%d_%H%M%S)
LOG_FILE="$LOG_DIR/sentry-digest-$TS.log"
SCAN_FILE="$LOG_DIR/sentry-digest-scan-$TS.json"
OUTCOME_FILE="$LOG_DIR/outcome-sentry-digest-$TS.json"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

cd "$REPO_DIR"

log "=== Sentry digest scan: $(date) ==="
"$PYTHON_BIN" "$SCRIPTS_DIR/sentry_digest.py" --out "$SCAN_FILE" 2>&1 | tee -a "$LOG_FILE"
SCAN_EXIT=${PIPESTATUS[0]}
if [ "$SCAN_EXIT" -ne 0 ]; then
    log "ERROR: sentry_digest.py scan failed (exit $SCAN_EXIT)"
    exit 1
fi

FIRST_RUN=$("$PYTHON_BIN" -c "import json; print(json.load(open('$SCAN_FILE'))['firstRun'])")
NEW_COUNT=$("$PYTHON_BIN" -c "import json; print(json.load(open('$SCAN_FILE'))['newCount'])")
GROWING_COUNT=$("$PYTHON_BIN" -c "import json; print(json.load(open('$SCAN_FILE'))['growingCount'])")

if [ "$FIRST_RUN" = "False" ] && [ "$NEW_COUNT" -eq 0 ] && [ "$GROWING_COUNT" -eq 0 ]; then
    log "Nothing new or growing. Skipping Claude spawn and email."
    find "$LOG_DIR" -name "sentry-digest-scan-*.json" -mtime +7 -delete 2>/dev/null || true
    find "$LOG_DIR" -name "sentry-digest-*.log" -mtime +30 -delete 2>/dev/null || true
    exit 0
fi

# Step 2: spawn Claude to investigate and email
PROMPT_FILE=$(mktemp)
cat > "$PROMPT_FILE" <<PROMPT_EOF
Read ~/social-autoposter/skill/SENTRY-DIGEST-SKILL.md for the full workflow.

## Run metadata

- First run (ledger was empty): $FIRST_RUN
- New issues this scan: $NEW_COUNT
- Growing issues this scan: $GROWING_COUNT
- Sentry auth token: read it yourself with
  security find-generic-password -s sentry-auth-token -w

## Scan result (from sentry_digest.py)

Full result is on disk at:
$SCAN_FILE

## Ledger path

$LEDGER_PATH

## Outcome file

Write your JSON outcome to:
$OUTCOME_FILE

Now run the workflow.
PROMPT_EOF

log "Spawning Claude Code session for investigation..."
log "  Scan file: $SCAN_FILE"
log "  Outcome file: $OUTCOME_FILE"

CLAUDE_EXIT=0
gtimeout 1800 "$CLAUDE_BIN" \
    -p "$(cat "$PROMPT_FILE")" \
    --dangerously-skip-permissions \
    2>&1 | tee -a "$LOG_FILE" || CLAUDE_EXIT=$?

rm -f "$PROMPT_FILE"

if [ $CLAUDE_EXIT -eq 124 ]; then
    log "ERROR: Claude Code timed out after 30 minutes"
elif [ $CLAUDE_EXIT -ne 0 ]; then
    log "WARNING: Claude Code exited with code $CLAUDE_EXIT"
fi

log "--- Post-run validation ---"
if [ -f "$OUTCOME_FILE" ]; then
    log "Outcome file found:"
    cat "$OUTCOME_FILE" >> "$LOG_FILE"
    REPORT_SENT=$("$PYTHON_BIN" -c "import json; d=json.load(open('$OUTCOME_FILE')); print(d.get('reportEmailSent', False))" 2>/dev/null || echo "False")
    LEDGER_WRITTEN=$("$PYTHON_BIN" -c "import json; d=json.load(open('$OUTCOME_FILE')); print(d.get('ledgerWritten', False))" 2>/dev/null || echo "False")
    SUMMARY=$("$PYTHON_BIN" -c "import json; d=json.load(open('$OUTCOME_FILE')); print(d.get('summary', 'No summary'))" 2>/dev/null || echo "No summary")
    log "  reportEmailSent: $REPORT_SENT"
    log "  ledgerWritten: $LEDGER_WRITTEN"
    log "  summary: $SUMMARY"
    if [ "$LEDGER_WRITTEN" != "True" ]; then
        log "WARNING: agent did not confirm writing the ledger; next run may re-flag the same issues"
    fi
else
    log "WARNING: No outcome file produced. Agent may not have completed the workflow."
fi

log "=== Done Sentry digest run (claude_exit=$CLAUDE_EXIT) ==="

find "$LOG_DIR" -name "sentry-digest-*.log" -mtime +30 -delete 2>/dev/null || true
find "$LOG_DIR" -name "sentry-digest-scan-*.json" -mtime +7 -delete 2>/dev/null || true
find "$LOG_DIR" -name "outcome-sentry-digest-*.json" -mtime +30 -delete 2>/dev/null || true
