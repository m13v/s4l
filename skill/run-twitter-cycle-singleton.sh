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
#   1. EXTERNAL CYCLE DETECT (post 2026-05-22 hardening): pgrep for any
#      run-twitter-cycle.sh that is NOT a descendant of this wrapper. Covers:
#        a) orphans from the pre-singleton launchd config (PPID=1 after parent
#           setsid wrapper exited), and
#        b) any manual / out-of-band cycle invocations.
#      If any external cycle is alive, log "skipped: external" and exit 0
#      WITHOUT killing anything. The next launchd fire (15 min later) re-runs
#      this check, so we naturally converge to one-at-a-time once the orphan
#      finishes its work on its own.
#   2. mkdir-based atomic singleton claim at /tmp/sa-twitter-cycle-singleton.lock
#      - If lock dir holds a live PID, log "skipped" and exit 0 (launchd is happy).
#      - If lock dir holds a dead PID, clear it and proceed.
#   3. Run run-twitter-cycle.sh in the FOREGROUND. The wrapper stays alive for
#      the full cycle duration (60-90 min), which also engages launchd's built-in
#      "don't fire while same Label is running" behavior. Belt + suspenders.
#   4. Trap EXIT to release the singleton lock on any exit path.
#
# NEVER KILL: this wrapper does not SIGTERM/SIGKILL any process. Per user
# instruction 2026-05-22 "I don't want to kill anything, going forward".
# Convergence to one-at-a-time is via skip-on-detect, not preemption.
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

# --- Phase 0: external-cycle detect (skip, never kill) -----------------------
# pgrep -f matches against the full cmdline. The cycle script is invoked as
# `/bin/bash <repo>/skill/run-twitter-cycle.sh` with no args, so its cmdline
# ends with `run-twitter-cycle.sh`. The wrapper's own cmdline ends with
# `run-twitter-cycle-singleton.sh`, so anchoring the pattern with `\.sh$`
# excludes us. Filter $$ as well as a belt-and-suspenders guard.
EXTERNAL_CYCLE_PIDS=$(pgrep -f 'skill/run-twitter-cycle\.sh$' 2>/dev/null | grep -v "^$$\$" | tr '\n' ' ' | sed 's/ *$//')
if [ -n "$EXTERNAL_CYCLE_PIDS" ]; then
  log "[singleton] skipped: external run-twitter-cycle.sh alive pids=[$EXTERNAL_CYCLE_PIDS] (self=$$, never killing per user instruction)"
  exit 0
fi

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
