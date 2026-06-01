#!/bin/bash
# run-twitter-cycle-singleton.sh — launchd entry wrapper for the twitter
# post-comments cycle.
#
# CONCURRENCY: this wrapper NO LONGER enforces one-at-a-time. As of 2026-06-01
# that is handled inside run-twitter-cycle.sh itself via:
#     preflight_acquire_slot_or_skip "twitter-cycle" 1
# which EVERY launch path hits (launchd, MCP draft_cycle/autopilot, manual
# `bash skill/run-twitter-cycle.sh`). The old guard that used to live here
# (Phase 0 external-cycle detect + the /tmp/sa-twitter-cycle-singleton.lock
# mkdir claim) only governed the launchd path, never the MCP/manual paths, so
# it let out-of-band cycles slip through and then conflicted with the in-script
# gate. It was redundant belt-and-suspenders and is removed. Single source of
# truth for concurrency is now the preflight slot gate.
#
# SNAPSHOT (the one load-bearing job left here): copy the cycle script to a temp
# file and run THAT, so an in-place edit of run-twitter-cycle.sh mid-run (e.g.
# the auto-commit agent committing a rewrite) cannot corrupt bash's byte-offset
# execution of a live cycle. run-twitter-cycle.sh hardcodes
# REPO_DIR="$HOME/social-autoposter" (no $0/BASH_SOURCE), so the /tmp copy
# resolves the repo identically.
# 2026-05-28 incident: commit 0ac29141 landed mid-run, byte-offset misaligned,
# the cycle skipped its unconditional release_lock + Variant-A sleep, then
# re-acquired its own still-held twitter-browser lock and self-deadlocked.
#
# NEVER KILL: per user instruction 2026-05-22, this wrapper does not
# SIGTERM/SIGKILL anything. With concurrency now enforced by the slot gate,
# there is nothing to preempt anyway.
#
# Logs:
#   - skill/logs/twitter-cycle-singleton.log -> wrapper start/done + snapshot events
#   - launchd stdout/stderr (configured in the plist) receive the cycle's output

set -u

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
SINGLETON_LOG="$LOG_DIR/twitter-cycle-singleton.log"
CYCLE_SCRIPT="$REPO_DIR/skill/run-twitter-cycle.sh"
SNAPSHOT=""

mkdir -p "$LOG_DIR"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" >> "$SINGLETON_LOG"; }

# Release the snapshot temp file on any exit path.
trap 'rm -f "$SNAPSHOT"; log "[wrapper] done pid=$$ rc=${EXIT_CODE:-?}"' EXIT

log "[wrapper] start pid=$$"

SNAPSHOT="/tmp/sa-twitter-cycle-snapshot-$$.sh"
cp "$CYCLE_SCRIPT" "$SNAPSHOT"
if ! /bin/bash -n "$SNAPSHOT" 2>/dev/null; then
  log "[wrapper] snapshot syntax check failed (caught mid-edit?); aborting, launchd retries next fire"
  exit 1
fi

# Foreground run of the snapshot. Do NOT exec (so the trap fires on completion).
/bin/bash "$SNAPSHOT"
EXIT_CODE=$?
exit "$EXIT_CODE"
