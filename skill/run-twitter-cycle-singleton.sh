#!/bin/bash
# run-twitter-cycle-singleton.sh — singleton wrapper invoked by launchd.
#
# Purpose: enforce ONE running twitter post-comments cycle at any time.
#
# History: prior to 2026-05-22, run-twitter-cycle-launchd.sh used a Python
# double-fork (os.setsid) to defeat launchd's natural overlap suppression so
# up to 4 cycles could stack and serialize through the twitter-browser lock.
# Net effect: ~50% of every cycle's wall time was spent queued on the lock,
# and one slow cycle could push three more behind it. The user requested
# singleton semantics on 2026-05-22.
#
# How this wrapper works:
#   1. mkdir-based atomic singleton claim at /tmp/sa-twitter-cycle-singleton.lock
#      - If lock dir holds a live PID, log "skipped" and exit 0 (launchd is happy).
#      - If lock dir holds a dead PID, clear it and proceed.
#   2. Run run-twitter-cycle.sh in the FOREGROUND. The wrapper stays alive for
#      the full cycle duration (60-90 min), which also engages launchd's built-in
#      "don't fire while same Label is running" behavior. Belt + suspenders.
#   3. Trap EXIT to release the singleton lock on any exit path.
#
# Logs:
#   - /Users/matthewdi/social-autoposter/skill/logs/twitter-cycle-singleton.log
#     -> tracks singleton skip/start/done lifecycle events.
#   - launchd stdout/stderr (already configured in the plist) continue to
#     receive the cycle's own output.

set -u

LOCK_DIR="/tmp/sa-twitter-cycle-singleton.lock"
REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
SINGLETON_LOG="$LOG_DIR/twitter-cycle-singleton.log"
CYCLE_SCRIPT="$REPO_DIR/skill/run-twitter-cycle.sh"

mkdir -p "$LOG_DIR"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" >> "$SINGLETON_LOG"; }

# Try to atomically claim the singleton slot.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  HOLDER_PID="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [ -n "$HOLDER_PID" ] && kill -0 "$HOLDER_PID" 2>/dev/null; then
    log "[singleton] skipped: prior cycle pid=$HOLDER_PID still alive"
    exit 0
  fi
  # Stale lock: holder is gone. Reclaim.
  log "[singleton] stale lock cleared (dead pid=${HOLDER_PID:-unknown})"
  rm -rf "$LOCK_DIR"
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "[singleton] could not reclaim lock after clearing stale; aborting"
    exit 1
  fi
fi
echo "$$" > "$LOCK_DIR/pid"

# Release lock on any exit path.
trap 'rm -rf "$LOCK_DIR"; log "[singleton] done pid=$$ rc=${EXIT_CODE:-?}"' EXIT

log "[singleton] start pid=$$"

# Foreground run. Do NOT exec (so the trap fires on completion).
/bin/bash "$CYCLE_SCRIPT"
EXIT_CODE=$?
exit "$EXIT_CODE"
