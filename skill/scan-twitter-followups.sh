#!/bin/bash
# Social Autoposter - X/Twitter thread follow-up scanner
# Revisits our recent X replies and captures depth-2+ public follow-ups
# that the /notifications scraper misses (when @-tag is dropped in nested replies).
# Companion to scan_twitter_mentions_browser.py (run via engage-twitter.sh).
# Scheduled ~once per day by launchd; skip-if-locked so it yields to the engage loop.


set -euo pipefail

# Browser-profile lock shared with all twitter pipelines.
source "$(dirname "$0")/lock.sh"
acquire_lock "twitter-browser" 0
# Drop stale Chrome singleton symlinks before launch (see clean_stale_singleton.sh).
bash "$HOME/social-autoposter/scripts/clean_stale_singleton.sh" "$HOME/.claude/browser-profiles/twitter" || true
ensure_browser_healthy "twitter"
acquire_lock "scan-twitter-followups" 0

[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/scan-twitter-followups-$(date +%Y-%m-%d_%H%M%S).log"

echo "=== Scan Twitter Follow-ups Run: $(date) ===" | tee "$LOG_FILE"
START_TS=$(date +%s)

DAYS="${FOLLOWUP_DAYS:-14}"
MAX_URLS="${FOLLOWUP_MAX_URLS:-40}"
SCROLL_COUNT="${FOLLOWUP_SCROLLS:-3}"

PYTHONUNBUFFERED=1 python3 "$REPO_DIR/scripts/scan_twitter_thread_followups.py" \
    --days "$DAYS" --max-urls "$MAX_URLS" --scroll-count "$SCROLL_COUNT" \
    2>&1 | tee -a "$LOG_FILE" || true

ELAPSED=$(( $(date +%s) - START_TS ))
# grep -c prints "0" AND exits 1 on zero matches, so `|| echo 0` was
# appending a second "0" and making FOUND multiline, which silently broke
# log_run.py. Use `|| FOUND=0` so the fallback only fires when the file is
# unreadable.
FOUND=$(grep -c "NEW follow-up:" "$LOG_FILE" 2>/dev/null) || FOUND=0
python3 "$REPO_DIR/scripts/log_run.py" --script "scan_twitter_followups" --posted "$FOUND" --skipped 0 --failed 0 --cost 0 --elapsed "$ELAPSED" || true

echo "=== Scan Twitter Follow-ups complete: $(date) (elapsed ${ELAPSED}s, found ${FOUND}) ===" | tee -a "$LOG_FILE"
find "$LOG_DIR" -name "scan-twitter-followups-*.log" -mtime +7 -delete 2>/dev/null || true
