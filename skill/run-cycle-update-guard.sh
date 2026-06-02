#!/bin/bash
# run-cycle-update-guard.sh — self-update guard that runs IMMEDIATELY BEFORE
# the twitter cycle, then hands off to the real cycle wrapper.
#
# WHY A WRAPPER (and not an edit to run-twitter-cycle-singleton.sh):
#   The singleton is a locked pipeline file (chflags uchg). Per repo policy we
#   never unlock it. So the per-cycle self-update lives here, in front of it:
#   the launchd plist calls THIS script, which (throttled) checks for a newer
#   release, updates if behind, then `exec`s the singleton unchanged.
#
# THROTTLE: a headless cycle fires often (every ~60s). We do NOT want a network
#   `npm view` on every fire. The version check runs at most once per
#   CHECK_INTERVAL_SECS (default 6h), gated by a stamp file. In between, this
#   wrapper is a near-instant pass-through.
#
# DEV SAFETY: social-autoposter-update.sh refuses to update a .git checkout, so
#   this guard is a no-op update on a dev box (it still execs the cycle).

set -u

REPO_DIR="${SAPS_REPO_DIR:-$HOME/social-autoposter}"
GUARD_DIR="$REPO_DIR/skill"
UPDATER="$GUARD_DIR/social-autoposter-update.sh"
SINGLETON="$GUARD_DIR/run-twitter-cycle-singleton.sh"
STAMP="$REPO_DIR/skill/logs/.last-update-check"
CHECK_INTERVAL_SECS="${SAPS_UPDATE_CHECK_INTERVAL_SECS:-21600}"  # 6h

now="$(date +%s)"
last=0
[ -f "$STAMP" ] && last="$(cat "$STAMP" 2>/dev/null || echo 0)"
# normalize non-numeric stamp to 0
case "$last" in (*[!0-9]*) last=0 ;; esac

if [ $(( now - last )) -ge "$CHECK_INTERVAL_SECS" ]; then
  mkdir -p "$(dirname "$STAMP")" 2>/dev/null || true
  echo "$now" > "$STAMP" 2>/dev/null || true
  if [ -x "$UPDATER" ]; then
    # Never let an update hiccup block the posting cycle: run it, ignore failure.
    bash "$UPDATER" || true
  fi
fi

# Hand off to the real (locked) cycle wrapper, preserving any args/env.
exec /bin/bash "$SINGLETON" "$@"
