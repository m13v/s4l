#!/usr/bin/env bash
# stats-instagram.sh — Refresh engagement stats for Instagram posts.
#
# Mirrors the per-platform stats pattern used by stats-reddit.sh / stats-twitter.sh
# but is API-only (no browser): calls IG Graph API insights for each posts row
# with platform='instagram', updates upvotes/comments_count/views, and logs the
# run so it surfaces in the dashboard Jobs panel.
#
# Logs: skill/logs/stats-instagram-YYYY-MM-DD_HHMMSS.log

set -uo pipefail

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/stats-instagram-$(date +%Y-%m-%d_%H%M%S).log"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }
log "=== stats-instagram fire: $(date) ==="

RUN_START=$(date +%s)

# Lock — instagram-poster reuses this lane; stats and post must not race.
# shellcheck source=lock.sh
source "$REPO_DIR/skill/lock.sh"
acquire_lock instagram-poster 30

# Step 1: sync any newly-posted media_posts -> posts (idempotent).
log "step 1: sync_ig_to_posts"
if ! /opt/homebrew/bin/python3.11 "$REPO_DIR/scripts/sync_ig_to_posts.py" --quiet >>"$LOG_FILE" 2>&1; then
    log "sync_ig_to_posts failed (continuing to refresh existing rows)"
fi

# Step 2: refresh stats for all platform='instagram' rows.
log "step 2: update_instagram_stats"
OUTPUT_FILE="/tmp/stats-instagram-$$.out"
if ! /opt/homebrew/bin/python3.11 "$REPO_DIR/scripts/update_instagram_stats.py" 2>>"$LOG_FILE" | tee -a "$LOG_FILE" >"$OUTPUT_FILE"; then
    log "update_instagram_stats.py exited non-zero — logging run as failed"
    CHECKED=0; UPDATED=0; NOT_FOUND=0; FAILED=0; VIEWS_REFRESHED=0
else
    SUMMARY=$(grep '^SUMMARY:' "$OUTPUT_FILE" | tail -1)
    CHECKED=$(echo "$SUMMARY" | sed -n 's/.*CHECKED=\([0-9]*\).*/\1/p'); CHECKED=${CHECKED:-0}
    UPDATED=$(echo "$SUMMARY" | sed -n 's/.*UPDATED=\([0-9]*\).*/\1/p'); UPDATED=${UPDATED:-0}
    NOT_FOUND=$(echo "$SUMMARY" | sed -n 's/.*NOT_FOUND=\([0-9]*\).*/\1/p'); NOT_FOUND=${NOT_FOUND:-0}
    FAILED=$(echo "$SUMMARY" | sed -n 's/.*FAILED=\([0-9]*\).*/\1/p'); FAILED=${FAILED:-0}
    VIEWS_REFRESHED=$(echo "$SUMMARY" | sed -n 's/.*VIEWS_REFRESHED=\([0-9]*\).*/\1/p'); VIEWS_REFRESHED=${VIEWS_REFRESHED:-0}
fi
rm -f "$OUTPUT_FILE"

RUN_ELAPSED=$(( $(date +%s) - RUN_START ))

log "logging run: checked=$CHECKED updated=$UPDATED not_found=$NOT_FOUND failed=$FAILED views_refreshed=$VIEWS_REFRESHED elapsed=${RUN_ELAPSED}s"

/opt/homebrew/bin/python3.11 "$REPO_DIR/scripts/log_run.py" \
    --script "stats_instagram" \
    --posted 0 \
    --skipped 0 \
    --failed "$FAILED" \
    --replies-refreshed 0 \
    --checked "$CHECKED" \
    --updated "$UPDATED" \
    --removed 0 \
    --unavailable 0 \
    --not-found "$NOT_FOUND" \
    --scanned "$CHECKED" \
    --changed "$UPDATED" \
    --views-refreshed "$VIEWS_REFRESHED" \
    --cost 0 \
    --elapsed "$RUN_ELAPSED" >>"$LOG_FILE" 2>&1 || log "log_run.py failed"

log "=== stats-instagram done ==="
exit 0
