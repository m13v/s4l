#!/bin/bash
# Social Autoposter - X/Twitter thread follow-up scanner
# Revisits our recent X replies and captures depth-2+ public follow-ups
# that the /notifications scraper misses (when @-tag is dropped in nested replies).
# Companion to scan_twitter_mentions_browser.py (run via engage-twitter.sh).
# Scheduled overnight by launchd (1:14 AM only). Waits up to 30min for the
# twitter-browser lock to free, then yields. Single overnight firing chosen
# because twitter-cycle parallel firings keep the lock busy during waking
# hours; 13:14 PM firing was dropped 2026-05-19 after weeks of "skipping"
# bails (acquire_lock timeout=0 was the original yield strategy, replaced
# with a 1800s wait that lets the scan run when twitter-cycle is quieter).


set -euo pipefail

# Bootstrap log paths early so the singleton-cleanup output below gets captured
# in the same log file the rest of the run uses.
LOG_DIR="$HOME/social-autoposter/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/scan-twitter-followups-$(date +%Y-%m-%d_%H%M%S).log"

# Browser-profile lock shared with all twitter pipelines.
source "$(dirname "$0")/lock.sh"
# Harness-only browser bootstrap (twitter-agent path fully removed 2026-05-19).
# scan_twitter_thread_followups.py uses twitter_browser.py functions, which
# honor TWITTER_CDP_URL exported by the lib.
source "$(dirname "$0")/lib/twitter-backend.sh"
acquire_lock "twitter-browser" 1800
ensure_twitter_browser_for_backend 2>&1 | tee -a "$LOG_FILE" || true
_ensure_rc="${PIPESTATUS[0]}"
[ "$_ensure_rc" != "0" ] && echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] WARNING: twitter-harness bootstrap failed (rc=$_ensure_rc); continuing anyway, downstream browser calls may fail" | tee -a "$LOG_FILE"
acquire_lock "scan-twitter-followups" 0

[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
# (LOG_DIR/LOG_FILE bootstrapped at top of script.)

echo "=== Scan Twitter Follow-ups Run: $(date) ===" | tee -a "$LOG_FILE"
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
