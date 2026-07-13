#!/bin/bash
# run-instagram-daily.sh — Pick + post one Instagram Reel per fire.
#
# Cadence (set by launchd, com.m13v.social-instagram-daily.plist):
#   5 fires/day at 09:00, 12:00, 15:00, 18:00, 21:00 local time.
#
# Per-fire logic:
#   1. acquire_lock instagram-poster (file lock; IG is HTTP-only, no browser)
#   2. pick_ig_account.py -> chooses target IG account (inverse-recent-share
#      over enabled accounts in config.json instagram.accounts).
#   3. ig_post_type_picker.py --account <name> -> JSON {post_type, video_path, ...}
#      scoped to the chosen account's draft pool.
#   4. call mixer/post_to_ig.py --file <path> --post-type <type> --account <name>
#      which: uploads to GCS, posts via IG Graph API, writes posted.json,
#      updates media_posts (status=posted, post_type + target_account coalesced).
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

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

# Run accounting for dashboard Job History (Post Threads · Instagram).
# Each exit site updates POSTED_CT / SKIPPED_CT / FAILED_CT; the EXIT trap
# always emits one log_run.py line so the run shows up under
# thread_instagram, matching how thread_twitter / thread_reddit log.
RUN_START_EPOCH=$(date +%s)
POSTED_CT=0
SKIPPED_CT=0
FAILED_CT=0

cleanup() {
  local rc=$?
  rm -f "$PICK_FILE"
  if [ "$POSTED_CT" -eq 0 ] && [ "$SKIPPED_CT" -eq 0 ] && [ "$FAILED_CT" -eq 0 ]; then
    if [ "$rc" -eq 0 ]; then SKIPPED_CT=1; else FAILED_CT=1; fi
  fi
  local elapsed=$(( $(date +%s) - RUN_START_EPOCH ))
  local cost
  cost=$(/usr/bin/python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START_EPOCH" --scripts "run-instagram-daily" 2>/dev/null || echo "0.0000")
  /usr/bin/python3 "$REPO_DIR/scripts/log_run.py" \
      --script "thread_instagram" \
      --posted "$POSTED_CT" --skipped "$SKIPPED_CT" --failed "$FAILED_CT" \
      --cost "$cost" --elapsed "$elapsed" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM HUP

log "=== instagram-daily fire: $(date) ==="

# Lock against overlapping fires (pure HTTP pipeline, no browser, but the
# picker reads-then-posts so two concurrent fires could pick the same row).
# 30s timeout — if a prior fire is still uploading, skip cleanly.
# shellcheck source=lock.sh
source "$REPO_DIR/skill/lock.sh"
acquire_lock instagram-poster 30

# Step 1: pick target IG account. Per-account plists set FORCE_ACCOUNT in
# their EnvironmentVariables block so the slot is hard-pinned to one account;
# fall back to inverse-recent-share picker only when no FORCE_ACCOUNT is set
# (manual / legacy invocation).
if [ -n "${FORCE_ACCOUNT:-}" ]; then
  log "step 1: FORCE_ACCOUNT=$FORCE_ACCOUNT honored from env"
  TARGET_ACCOUNT=$(/opt/homebrew/bin/python3.11 "$REPO_DIR/scripts/pick_ig_account.py" --account "$FORCE_ACCOUNT" 2>>"$LOG_FILE")
else
  log "step 1: pick_ig_account (no FORCE_ACCOUNT in env)"
  TARGET_ACCOUNT=$(/opt/homebrew/bin/python3.11 "$REPO_DIR/scripts/pick_ig_account.py" 2>>"$LOG_FILE")
fi
if [ -z "$TARGET_ACCOUNT" ]; then
  log "pick_ig_account.py produced no account — exiting non-zero"
  FAILED_CT=1
  exit 1
fi
log "picker chose account: $TARGET_ACCOUNT"

# Step 2: pick post type + video, scoped to that account
log "step 2: ig_post_type_picker --account $TARGET_ACCOUNT"
if ! /opt/homebrew/bin/python3.11 "$REPO_DIR/scripts/ig_post_type_picker.py" \
        --account "$TARGET_ACCOUNT" > "$PICK_FILE" 2>>"$LOG_FILE"; then
  rc=$?
  if [ "$rc" -eq 2 ]; then
    log "queue exhausted for account=$TARGET_ACCOUNT (no drafts of either type) — exiting cleanly"
    SKIPPED_CT=1
    exit 0
  fi
  log "picker failed rc=$rc — exiting non-zero"
  FAILED_CT=1
  exit 1
fi

POST_TYPE=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$PICK_FILE'))['post_type'])")
VIDEO_PATH=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$PICK_FILE'))['video_path'])")
POST_NUMBER=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$PICK_FILE'))['post_number'])")
REASON=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$PICK_FILE'))['reason'])")
FALLBACK=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$PICK_FILE'))['fallback'])")

log "picker chose: account=${TARGET_ACCOUNT} post-${POST_NUMBER} type=${POST_TYPE} fallback=${FALLBACK}"
log "picker reason: ${REASON}"

if [ ! -f "$VIDEO_PATH" ]; then
  log "ERROR: picker pointed at $VIDEO_PATH but file missing on disk"
  FAILED_CT=1
  exit 1
fi

# Step 2: post
DRY_FLAG=""
if [ "${IG_DRY_RUN:-0}" = "1" ]; then
  DRY_FLAG="--dry-run"
  log "IG_DRY_RUN=1 — passing --dry-run to post_to_ig.py"
fi

log "step 3: post_to_ig.py --file $(basename "$VIDEO_PATH") --post-type $POST_TYPE --account $TARGET_ACCOUNT $DRY_FLAG"
if ! /opt/homebrew/bin/python3.11 "$REPO_DIR/mixer/post_to_ig.py" \
        --file "$VIDEO_PATH" --post-type "$POST_TYPE" --account "$TARGET_ACCOUNT" $DRY_FLAG >>"$LOG_FILE" 2>&1; then
  log "post_to_ig.py failed — exiting non-zero"
  FAILED_CT=1
  exit 1
fi

POSTED_CT=1
log "=== finished post-${POST_NUMBER} (${POST_TYPE}) on ${TARGET_ACCOUNT} successfully ==="

# Step 4: mirror the new media_posts row into the cross-platform `posts` table
# so it surfaces in the dashboard (Trends, Top, Activity, Cohort, Stats by
# Style) alongside Reddit/Twitter/LinkedIn rows. Idempotent.
log "step 4: sync_ig_to_posts"
if ! /opt/homebrew/bin/python3.11 "$REPO_DIR/scripts/sync_ig_to_posts.py" --quiet >>"$LOG_FILE" 2>&1; then
    log "sync_ig_to_posts failed (post is already on IG; will retry on next stats fire)"
fi

exit 0
