#!/bin/bash
# run-instagram-daily.sh — Pick + post one Instagram Reel per fire.
#
# Cadence (set by launchd, com.m13v.social-instagram-daily.plist):
#   5 fires/day at 09:00, 12:00, 15:00, 18:00, 21:00 local time.
#
# Per-fire logic:
#   1. acquire_lock instagram-poster (file lock; IG is HTTP-only, no browser)
#   2. run scripts/ig_post_type_picker.py -> JSON {post_type, video_path, ...}
#      4:1 sliding-window picker: 4 organic + 1 product per 5 posts.
#   3. call mixer/post_to_ig.py --file <path> --post-type <type>
#      which: uploads to GCS, posts via IG Graph API, writes posted.json,
#      updates media_posts (status=posted, post_type coalesced).
#
# Exit behavior:
#   0  - posted, OR queue exhausted (logged), OR another run holds the lock
#   1  - real failure (DB error, IG API error, picker crash, etc.)
#
# Logs: skill/logs/instagram-daily-YYYY-MM-DD_HHMMSS.log

set -uo pipefail

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"

LOG_FILE="$LOG_DIR/instagram-daily-$(date +%Y-%m-%d_%H%M%S).log"
PICK_FILE="/tmp/ig_pick_$(date +%s)_$$.json"

if [ -f "$REPO_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO_DIR/.env"
  set +a
fi

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

cleanup() {
  rm -f "$PICK_FILE"
}
trap cleanup EXIT INT TERM HUP

log "=== instagram-daily fire: $(date) ==="

# Lock against overlapping fires (pure HTTP pipeline, no browser, but the
# picker reads-then-posts so two concurrent fires could pick the same row).
# 30s timeout — if a prior fire is still uploading, skip cleanly.
# shellcheck source=lock.sh
source "$REPO_DIR/skill/lock.sh"
acquire_lock instagram-poster 30

# Step 1: pick post type + video
log "step 1: ig_post_type_picker"
if ! /opt/homebrew/bin/python3.11 "$REPO_DIR/scripts/ig_post_type_picker.py" > "$PICK_FILE" 2>>"$LOG_FILE"; then
  rc=$?
  if [ "$rc" -eq 2 ]; then
    log "queue exhausted (no draft media_posts of either type) — exiting cleanly"
    exit 0
  fi
  log "picker failed rc=$rc — exiting non-zero"
  exit 1
fi

POST_TYPE=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$PICK_FILE'))['post_type'])")
VIDEO_PATH=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$PICK_FILE'))['video_path'])")
POST_NUMBER=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$PICK_FILE'))['post_number'])")
REASON=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$PICK_FILE'))['reason'])")
FALLBACK=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$PICK_FILE'))['fallback'])")

log "picker chose: post-${POST_NUMBER} type=${POST_TYPE} fallback=${FALLBACK}"
log "picker reason: ${REASON}"

if [ ! -f "$VIDEO_PATH" ]; then
  log "ERROR: picker pointed at $VIDEO_PATH but file missing on disk"
  exit 1
fi

# Step 2: post
DRY_FLAG=""
if [ "${IG_DRY_RUN:-0}" = "1" ]; then
  DRY_FLAG="--dry-run"
  log "IG_DRY_RUN=1 — passing --dry-run to post_to_ig.py"
fi

log "step 2: post_to_ig.py --file $(basename "$VIDEO_PATH") --post-type $POST_TYPE $DRY_FLAG"
if ! /opt/homebrew/bin/python3.11 "$REPO_DIR/mixer/post_to_ig.py" \
        --file "$VIDEO_PATH" --post-type "$POST_TYPE" $DRY_FLAG >>"$LOG_FILE" 2>&1; then
  log "post_to_ig.py failed — exiting non-zero"
  exit 1
fi

log "=== finished post-${POST_NUMBER} (${POST_TYPE}) successfully ==="
exit 0
