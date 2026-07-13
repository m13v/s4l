#!/usr/bin/env bash
# engage-moltbook.sh — MoltBook reply engagement loop
# Calls engage_reddit.py --platform moltbook to process pending MoltBook replies.
# Discovery runs separately via run-scan-moltbook-replies.sh.
# Called by launchd every 10 minutes.

set -euo pipefail

source "$(dirname "$0")/lock.sh"
acquire_lock "engage-moltbook" 3600

[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

# Cycle ID for cross-cycle cost accounting (see engage.sh / run-reddit-search.sh
# for the same pattern). engage_reddit.py's claude subprocess inherits this via
# env, and log_claude_session.py stamps claude_sessions.cycle_id.
BATCH_ID="${BATCH_ID:-enmb-$(date +%Y%m%d-%H%M%S)}"
export BATCH_ID
export SA_CYCLE_ID="$BATCH_ID"

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/engage-moltbook-$(date +%Y-%m-%d_%H%M%S).log"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

RUN_START=$(date +%s)
log "=== MoltBook Engage Run: $(date) ==="

python3 "$REPO_DIR/scripts/engage_reddit.py" --platform moltbook 2>&1 | tee -a "$LOG_FILE" || log "WARNING: engage_reddit.py exited non-zero"

RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
log "=== MoltBook Engage complete: $(date) (elapsed ${RUN_ELAPSED}s) ==="

find "$LOG_DIR" -name "engage-moltbook-*.log" -mtime +7 -delete 2>/dev/null || true
