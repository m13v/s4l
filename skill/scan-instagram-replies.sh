#!/usr/bin/env bash
# scan-instagram-replies.sh — Discover new inbound comments on our Instagram
# posts via the Graph API and insert them into the `replies` table.
#
# Mirrors the pattern used by stats-instagram.sh: API-only (no browser),
# instagram-poster lock (so scan, stats, and post can't race for the same
# token-bucket), then a SUMMARY-line parsed by log_run.py for the dashboard
# Jobs panel.
#
# Logs: skill/logs/scan-instagram-replies-YYYY-MM-DD_HHMMSS.log

set -uo pipefail

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/scan-instagram-replies-$(date +%Y-%m-%d_%H%M%S).log"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }
log "=== scan-instagram-replies fire: $(date) ==="

RUN_START=$(date +%s)

# instagram-poster lock — stats, scan, daily-post, and render all share this
# lane so we don't race on the same /me/media token bucket.
# shellcheck source=lock.sh
source "$REPO_DIR/skill/lock.sh"
acquire_lock instagram-poster 30

OUTPUT_FILE="/tmp/scan-instagram-replies-$$.out"
if ! /opt/homebrew/bin/python3.11 "$REPO_DIR/scripts/scan_instagram_comments.py" 2>>"$LOG_FILE" | tee -a "$LOG_FILE" >"$OUTPUT_FILE"; then
    log "scan_instagram_comments.py exited non-zero — logging run as failed"
    DISCOVERED=0; SKIPPED=0; CHECKED=0; ALREADY=0; ACCOUNTS=0
else
    SUMMARY=$(grep '^SUMMARY:' "$OUTPUT_FILE" | tail -1)
    DISCOVERED=$(echo "$SUMMARY" | sed -n 's/.*DISCOVERED=\([0-9]*\).*/\1/p'); DISCOVERED=${DISCOVERED:-0}
    SKIPPED=$(echo "$SUMMARY" | sed -n 's/.*SKIPPED=\([0-9]*\).*/\1/p'); SKIPPED=${SKIPPED:-0}
    CHECKED=$(echo "$SUMMARY" | sed -n 's/.*CHECKED=\([0-9]*\).*/\1/p'); CHECKED=${CHECKED:-0}
    ALREADY=$(echo "$SUMMARY" | sed -n 's/.*ALREADY=\([0-9]*\).*/\1/p'); ALREADY=${ALREADY:-0}
    ACCOUNTS=$(echo "$SUMMARY" | sed -n 's/.*ACCOUNTS=\([0-9]*\).*/\1/p'); ACCOUNTS=${ACCOUNTS:-0}
fi
rm -f "$OUTPUT_FILE"

RUN_ELAPSED=$(( $(date +%s) - RUN_START ))

log "logging run: discovered=$DISCOVERED skipped=$SKIPPED checked=$CHECKED already=$ALREADY accounts=$ACCOUNTS elapsed=${RUN_ELAPSED}s"

# discovered -> posted (new pending rows are the productive output of a scan,
# same convention scan_reddit_replies / scan_github_replies use).
# skipped -> skipped. checked -> scanned (media items inspected).
/opt/homebrew/bin/python3.11 "$REPO_DIR/scripts/log_run.py" \
    --script "scan_instagram_comments" \
    --posted "$DISCOVERED" \
    --skipped "$SKIPPED" \
    --failed 0 \
    --scanned "$CHECKED" \
    --cost 0 \
    --elapsed "$RUN_ELAPSED" >>"$LOG_FILE" 2>&1 || log "log_run.py failed"

log "=== scan-instagram-replies done ==="
exit 0
