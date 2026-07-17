#!/bin/bash
# Archive log files older than 7 days from skill/logs/ to skill/logs-archive/.
# The dashboard (bin/server.js) does many fs.readdirSync(LOG_DIR) calls per
# pulse. Letting that directory grow to 17k+ files starves the event loop
# and the dashboard stops responding. Pruning to a sibling dir keeps the
# files around for forensics without including them in the dashboard scan.
#
# Scheduled daily by ~/Library/LaunchAgents/com.m13v.social-archive-logs.plist

set -uo pipefail

LOG_DIR="/Users/matthewdi/social-autoposter/skill/logs"
ARCHIVE_DIR="/Users/matthewdi/social-autoposter/skill/logs-archive"
DAYS="${ARCHIVE_DAYS:-7}"

mkdir -p "$ARCHIVE_DIR" "$LOG_DIR"

# Per-run summary log so the dashboard's "Other" section can find this job.
# Filename matches the JOBS[].logPrefix value in bin/server.js.
RUN_LOG="$LOG_DIR/archive-logs-$(date +%Y-%m-%d_%H%M%S).log"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$RUN_LOG"; }

if [ ! -d "$LOG_DIR" ]; then
  log "ERROR: LOG_DIR not found: $LOG_DIR"
  exit 0
fi

log "=== archive-old-logs starting (DAYS=$DAYS) ==="

# Only top-level files; do not touch claude-sessions/ or other subdirs.
# Also exclude the per-run summary we just created so we don't archive
# ourselves on long-tail edge cases.
find "$LOG_DIR" -maxdepth 1 -type f -mtime +"$DAYS" ! -name "$(basename "$RUN_LOG")" -print0 \
  | xargs -0 -I{} mv {} "$ARCHIVE_DIR/" 2>&1 | tee -a "$RUN_LOG" >/dev/null || true

# --- Size-based rotation for live launchd-*.log files (2026-07-17) -----------
# launchd holds these fds open and appends forever, so the mtime sweep above
# never touches them: their mtime is always fresh. Found in the wild at 1.9GB
# (launchd-linkedin-stdout.log) and 694MB (launchd-engage-twitter-stdout.log),
# which made "grep the logs" surface months-stale content as if it were
# current (the 2026-07-17 Neon-hostname false alarm).
# Copy-truncate: gzip the current content into logs-archive with a timestamp,
# then truncate IN PLACE (same inode) so the launchd fd keeps appending.
# Lines appended during the gzip window are lost on truncate; acceptable for
# logs, and the window is seconds. Threshold tunable via ROTATE_MAX_MB.
ROTATE_MAX_MB="${ROTATE_MAX_MB:-10}"
find "$LOG_DIR" -maxdepth 1 -type f -name "launchd-*.log" -size +"${ROTATE_MAX_MB}M" -print0 \
  | while IFS= read -r -d '' f; do
      base=$(basename "$f" .log)
      dest="$ARCHIVE_DIR/${base}-$(date +%Y%m%d-%H%M%S).log.gz"
      if gzip -c "$f" > "$dest" 2>>"$RUN_LOG"; then
        : > "$f"
        log "rotated $(basename "$f") -> $(basename "$dest") ($(du -h "$dest" | cut -f1) compressed)"
      else
        rm -f "$dest"
        log "WARN: rotate failed for $f (left untouched)"
      fi
    done

remaining=$(find "$LOG_DIR" -maxdepth 1 -type f | wc -l | tr -d ' ')
archived=$(find "$ARCHIVE_DIR" -maxdepth 1 -type f | wc -l | tr -d ' ')

log "kept=$remaining archived_total=$archived"
log "=== done ==="
