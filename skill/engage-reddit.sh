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

# Pull engage-stage counters from the canonical LOG_RUN_SUMMARY line that
# engage_reddit.py prints right before exiting. Previously engage_reddit.py
# wrote its own log_run.py row AND we wrote one here, producing two rows per
# cycle in run_monitor.log -- the python-side row had no scan info and showed
# as zeros on the dashboard. Now python emits the line, the shell parses it,
# and we write ONE row that combines engage + scan counters.
SUMMARY_LINE=$(grep -m1 "^\[engage_reddit\] LOG_RUN_SUMMARY" "$LOG_FILE" 2>/dev/null || true)
ENG_POSTED=0; ENG_SKIPPED=0; ENG_FAILED=0; ENG_COST="0.0000"; ENG_ELAPSED="$ELAPSED"; ENG_FAILURE_REASONS=""
if [ -n "$SUMMARY_LINE" ]; then
  ENG_POSTED=$(echo "$SUMMARY_LINE" | grep -oE "posted=[0-9]+" | head -1 | cut -d= -f2)
  ENG_SKIPPED=$(echo "$SUMMARY_LINE" | grep -oE "skipped=[0-9]+" | head -1 | cut -d= -f2)
  ENG_FAILED=$(echo "$SUMMARY_LINE" | grep -oE "failed=[0-9]+" | head -1 | cut -d= -f2)
  ENG_COST=$(echo "$SUMMARY_LINE" | grep -oE "cost=[0-9.]+" | head -1 | cut -d= -f2)
  # `failure_reasons=` is the last key on the line; it may be empty. Capture
  # everything after the literal token, trim trailing whitespace.
  ENG_FAILURE_REASONS=$(echo "$SUMMARY_LINE" | sed -n 's/.* failure_reasons=\([^ ]*\).*/\1/p')
  : "${ENG_POSTED:=0}" "${ENG_SKIPPED:=0}" "${ENG_FAILED:=0}" "${ENG_COST:=0.0000}"
fi

LOG_RUN_ARGS=(--script "engage_reddit" --posted "$ENG_POSTED" --skipped "$ENG_SKIPPED" --failed "$ENG_FAILED" --cost "$ENG_COST" --elapsed "$ENG_ELAPSED")
[ -n "$SCAN_ARG" ] && LOG_RUN_ARGS+=(--scan "$SCAN_ARG")
[ -n "$ENG_FAILURE_REASONS" ] && LOG_RUN_ARGS+=(--failure-reasons "$ENG_FAILURE_REASONS")
python3 "$REPO_DIR/scripts/log_run.py" "${LOG_RUN_ARGS[@]}" || true

echo "=== Engage Reddit complete: $(date) (elapsed ${ELAPSED}s) ===" | tee -a "$LOG_FILE"
find "$LOG_DIR" -name "engage-reddit-*.log" -mtime +7 -delete 2>/dev/null || true
