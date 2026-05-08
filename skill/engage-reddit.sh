#!/bin/bash
# Social Autoposter - Reddit engagement loop
# Runs scan_reddit_replies.py every 10 min via launchd.
# Inbox-based discovery + engage_reddit.py --limit 5 in one job.
# Skip-if-locked (timeout 0) since runs are frequent and a previous tick may still be engaging.
#
# Renamed 2026-04-29 from run-scan-reddit-replies.sh / com.m13v.social-scan-reddit-replies
# to engage-reddit.sh / com.m13v.social-engage-reddit so the file/plist/log names
# match what the dashboard already calls this job ("Engage Reddit"). The Python
# discovery module (scripts/scan_reddit_replies.py) keeps its name since other
# helpers still import from it.


set -euo pipefail

source "$(dirname "$0")/lock.sh"
acquire_lock "engage-reddit" 0

[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/engage-reddit-$(date +%Y-%m-%d_%H%M%S).log"

echo "=== Engage Reddit Run: $(date) ===" | tee "$LOG_FILE"
START_TS=$(date +%s)

PYTHONUNBUFFERED=1 python3 "$REPO_DIR/scripts/scan_reddit_replies.py" 2>&1 | tee -a "$LOG_FILE" || true

ELAPSED=$(( $(date +%s) - START_TS ))
# grep -c prints "0" AND exits 1 on zero matches, so `|| echo 0` was
# appending a second "0" and making FOUND multiline, which silently broke
# log_run.py. Use `|| FOUND=0` so the fallback only fires when the file is
# unreadable.
FOUND=$(grep -ci "new repl" "$LOG_FILE" 2>/dev/null) || FOUND=0
# Pull scan-stage counters out of the "Inbox scan complete:" line so the
# dashboard Result column can show "scanned N / new N / excluded N" on empty
# cycles instead of all-zeros. Format on disk:
#   Inbox scan complete: seen=51 new_pending=0 backfill_skipped=0 \
#       already_replied=0 excluded_author=1 unmatched_thread=0
# We rename the keys to short forms (seen->scanned, new_pending->new,
# excluded_author->excluded, unmatched_thread->unmatched) before passing to
# log_run.py via --scan.
SCAN_LINE=$(grep -m1 "^Inbox scan complete:" "$LOG_FILE" 2>/dev/null || true)
SCAN_ARG=""
if [ -n "$SCAN_LINE" ]; then
  scan_seen=$(echo "$SCAN_LINE" | grep -oE "seen=[0-9]+" | head -1 | cut -d= -f2)
  scan_new=$(echo "$SCAN_LINE" | grep -oE "new_pending=[0-9]+" | head -1 | cut -d= -f2)
  scan_excl=$(echo "$SCAN_LINE" | grep -oE "excluded_author=[0-9]+" | head -1 | cut -d= -f2)
  scan_unm=$(echo "$SCAN_LINE" | grep -oE "unmatched_thread=[0-9]+" | head -1 | cut -d= -f2)
  scan_back=$(echo "$SCAN_LINE" | grep -oE "backfill_skipped=[0-9]+" | head -1 | cut -d= -f2)
  parts=""
  [ -n "$scan_seen" ] && parts="${parts}scanned=${scan_seen},"
  [ -n "$scan_new" ]  && parts="${parts}new=${scan_new},"
  [ -n "$scan_excl" ] && parts="${parts}excluded=${scan_excl},"
  [ -n "$scan_unm" ]  && parts="${parts}unmatched=${scan_unm},"
  [ -n "$scan_back" ] && parts="${parts}backfill=${scan_back},"
  SCAN_ARG="${parts%,}"
fi
if [ -n "$SCAN_ARG" ]; then
  python3 "$REPO_DIR/scripts/log_run.py" --script "engage_reddit" --posted "$FOUND" --skipped 0 --failed 0 --cost 0 --elapsed "$ELAPSED" --scan "$SCAN_ARG" || true
else
  python3 "$REPO_DIR/scripts/log_run.py" --script "engage_reddit" --posted "$FOUND" --skipped 0 --failed 0 --cost 0 --elapsed "$ELAPSED" || true
fi

echo "=== Engage Reddit complete: $(date) (elapsed ${ELAPSED}s) ===" | tee -a "$LOG_FILE"
find "$LOG_DIR" -name "engage-reddit-*.log" -mtime +7 -delete 2>/dev/null || true
