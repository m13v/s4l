#!/bin/bash
# Refresh engagement counts on captured human top-replies via fxtwitter API.
# Mirrors stats-twitter.sh cadence (4x/day). The python script is read-only
# against twitter.com (only hits api.fxtwitter.com), so no browser lock needed.
#
# See:
#   scripts/refresh_thread_top_replies_stats.py -- the refresh script
#   scripts/capture_thread_top_replies.py -- the capture script (sibling)
#
# Fires from launchd plist com.m13v.social-refresh-twitter-top-replies-stats.

set -u
cd "$(dirname "$0")/.."

REPO_DIR="$(pwd)"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"

ts() { date +"%Y-%m-%dT%H:%M:%S%z"; }
log() { echo "[$(ts)] $*" | tee -a "$LOG_DIR/refresh-twitter-top-replies-stats.log"; }

log "starting refresh cycle"

python3 "$REPO_DIR/scripts/refresh_thread_top_replies_stats.py" \
  --stale-hours 5 --limit 300 \
  >> "$LOG_DIR/refresh-twitter-top-replies-stats.log" 2>&1
RC=$?
log "refresh cycle done rc=$RC"
exit $RC
