#!/bin/bash
# Social Autoposter - Moltbook reply scanner
# Runs scan_moltbook_replies.py on its own launchd schedule.
# Pure API; typically finishes in <1min, so uses a short 15min lock wait.


set -euo pipefail

source "$(dirname "$0")/lock.sh"
acquire_lock "scan-moltbook-replies" 900

[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run-scan-moltbook-replies-$(date +%Y-%m-%d_%H%M%S).log"

echo "=== Scan Moltbook Replies Run: $(date) ===" | tee "$LOG_FILE"
START_TS=$(date +%s)

PYTHONUNBUFFERED=1 python3 "$REPO_DIR/scripts/scan_moltbook_replies.py" 2>&1 | tee -a "$LOG_FILE" || true

ELAPSED=$(( $(date +%s) - START_TS ))
# grep -c prints "0" AND exits 1 on zero matches, so `|| echo 0` was
# appending a second "0" and making FOUND multiline, which silently broke
# log_run.py. Use `|| FOUND=0` so the fallback only fires when the file is
# unreadable.
FOUND=$(grep -ci "new repl" "$LOG_FILE" 2>/dev/null) || FOUND=0
# Pull scan-stage counters out of the "Notification scan complete:" line that
# scan_moltbook_replies.py prints. Same key format as Reddit's scanner. Renders
# as scanned/new/excluded pills on the dashboard scan-replies row so empty
# cycles read as "scanned N / 0 new" instead of all-zeros.
SCAN_LINE=$(grep -m1 "^Notification scan complete:" "$LOG_FILE" 2>/dev/null || true)
SCAN_ARG=""
if [ -n "$SCAN_LINE" ]; then
  scan_seen=$(echo "$SCAN_LINE" | grep -oE "seen=[0-9]+" | head -1 | cut -d= -f2)
  scan_new=$(echo "$SCAN_LINE" | grep -oE "new_pending=[0-9]+" | head -1 | cut -d= -f2)
  scan_excl=$(echo "$SCAN_LINE" | grep -oE "excluded_author=[0-9]+" | head -1 | cut -d= -f2)
  scan_self=$(echo "$SCAN_LINE" | grep -oE "self_filtered=[0-9]+" | head -1 | cut -d= -f2)
  scan_back=$(echo "$SCAN_LINE" | grep -oE "backfill_skipped=[0-9]+" | head -1 | cut -d= -f2)
  scan_excl_total=$(( ${scan_excl:-0} + ${scan_self:-0} ))
  parts=""
  [ -n "$scan_seen" ] && parts="${parts}scanned=${scan_seen},"
  [ -n "$scan_new" ]  && parts="${parts}new=${scan_new},"
  [ "$scan_excl_total" -gt 0 ] && parts="${parts}excluded=${scan_excl_total},"
  [ -n "$scan_back" ] && parts="${parts}backfill=${scan_back},"
  SCAN_ARG="${parts%,}"
fi
if [ -n "$SCAN_ARG" ]; then
  python3 "$REPO_DIR/scripts/log_run.py" --script "scan_moltbook_replies" --posted "$FOUND" --skipped 0 --failed 0 --cost 0 --elapsed "$ELAPSED" --scan "$SCAN_ARG" || true
else
  python3 "$REPO_DIR/scripts/log_run.py" --script "scan_moltbook_replies" --posted "$FOUND" --skipped 0 --failed 0 --cost 0 --elapsed "$ELAPSED" || true
fi

echo "=== Scan Moltbook Replies complete: $(date) (elapsed ${ELAPSED}s) ===" | tee -a "$LOG_FILE"
find "$LOG_DIR" -name "run-scan-moltbook-replies-*.log" -mtime +7 -delete 2>/dev/null || true
