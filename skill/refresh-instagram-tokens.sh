#!/usr/bin/env bash
# refresh-instagram-tokens.sh — Refresh Instagram Graph API long-lived tokens
# before they expire.
#
# IG long-lived tokens last ~60 days; this job runs daily and refreshes any
# token within REFRESH_BUFFER_DAYS (default 14d) of expiry. The .env file at
# ~/instagram-graph-api/.env is rewritten atomically on success.
#
# Lightweight (no lock needed — read+write to a file we own, no browser/MCP)
# but we take instagram-poster anyway so a poster/stats/scan run that's mid-
# flight can finish reading the existing token before we swap it.
#
# Logs: skill/logs/refresh-instagram-tokens-YYYY-MM-DD_HHMMSS.log

set -uo pipefail

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/refresh-instagram-tokens-$(date +%Y-%m-%d_%H%M%S).log"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }
log "=== refresh-instagram-tokens fire: $(date) ==="

RUN_START=$(date +%s)

# shellcheck source=lock.sh
source "$REPO_DIR/skill/lock.sh"
acquire_lock instagram-poster 30

OUTPUT_FILE="/tmp/refresh-instagram-tokens-$$.out"
if ! /opt/homebrew/bin/python3.11 "$REPO_DIR/scripts/refresh_instagram_tokens.py" 2>>"$LOG_FILE" | tee -a "$LOG_FILE" >"$OUTPUT_FILE"; then
    log "refresh_instagram_tokens.py exited non-zero"
    REFRESHED=0; SKIPPED=0; FAILED=0; ACCOUNTS=0
else
    SUMMARY=$(grep '^SUMMARY:' "$OUTPUT_FILE" | tail -1)
    REFRESHED=$(echo "$SUMMARY" | sed -n 's/.*REFRESHED=\([0-9]*\).*/\1/p'); REFRESHED=${REFRESHED:-0}
    SKIPPED=$(echo "$SUMMARY" | sed -n 's/.*SKIPPED=\([0-9]*\).*/\1/p'); SKIPPED=${SKIPPED:-0}
    FAILED=$(echo "$SUMMARY" | sed -n 's/.*FAILED=\([0-9]*\).*/\1/p'); FAILED=${FAILED:-0}
    ACCOUNTS=$(echo "$SUMMARY" | sed -n 's/.*ACCOUNTS=\([0-9]*\).*/\1/p'); ACCOUNTS=${ACCOUNTS:-0}
fi
rm -f "$OUTPUT_FILE"

RUN_ELAPSED=$(( $(date +%s) - RUN_START ))

log "logging run: refreshed=$REFRESHED skipped=$SKIPPED failed=$FAILED accounts=$ACCOUNTS elapsed=${RUN_ELAPSED}s"

/opt/homebrew/bin/python3.11 "$REPO_DIR/scripts/log_run.py" \
    --script "refresh_instagram_tokens" \
    --posted "$REFRESHED" \
    --skipped "$SKIPPED" \
    --failed "$FAILED" \
    --cost 0 \
    --elapsed "$RUN_ELAPSED" >>"$LOG_FILE" 2>&1 || log "log_run.py failed"

log "=== refresh-instagram-tokens done ==="
exit 0
