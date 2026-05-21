#!/bin/bash
# Capture top-3 human replies on threads where we recently posted a comment.
# Decoupled from the main twitter posting flow (twitter_post_plan.py is locked);
# polls every ~2 min for fresh twitter rows missing top_replies_captured_at.
#
# See:
#   schema-postgres.sql -- table thread_top_replies + posts.top_replies_captured_at
#   scripts/capture_thread_top_replies.py -- the capture script
#   scripts/refresh_thread_top_replies_stats.py -- per-row fxtwitter refresh
#
# Fires from launchd plist com.m13v.social-capture-twitter-top-replies.

set -u
cd "$(dirname "$0")/.."

REPO_DIR="$(pwd)"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"

ts() { date +"%Y-%m-%dT%H:%M:%S%z"; }
log() { echo "[$(ts)] $*" | tee -a "$LOG_DIR/capture-twitter-top-replies.log"; }

log "starting capture cycle"

# Defer if the lock skill says another browser cycle is active. We respect the
# same twitter-browser-lock that the main twitter pipeline uses (the python
# script also re-checks the lock, but failing fast here keeps the log clean).
LOCK_FILE="$HOME/.claude/twitter-browser-lock.json"
if [ -f "$LOCK_FILE" ]; then
  LOCK_AGE=$(python3 -c "import json,time,os; d=json.load(open('$LOCK_FILE')); print(int(time.time()-d.get('timestamp',0)))" 2>/dev/null || echo 9999)
  HOLDER=$(python3 -c "import json; print(json.load(open('$LOCK_FILE')).get('session_id',''))" 2>/dev/null || echo "")
  # Treat lock < 60s old as actively held → defer to next tick.
  if [ "$LOCK_AGE" -lt 60 ]; then
    log "deferring: lock held by $HOLDER (age ${LOCK_AGE}s)"
    exit 0
  fi
fi

python3 "$REPO_DIR/scripts/capture_thread_top_replies.py" \
  --window-hours 2 --limit 5 \
  >> "$LOG_DIR/capture-twitter-top-replies.log" 2>&1
RC=$?
log "capture cycle done rc=$RC"
exit $RC
