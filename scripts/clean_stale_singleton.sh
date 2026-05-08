#!/usr/bin/env bash
# clean_stale_singleton.sh — Remove stale Chrome singleton symlinks from a
# browser profile if (and only if) the PID they reference is dead.
#
# Background: when Chrome exits ungracefully (SIGKILL, system sleep, force
# quit, jetsam), it leaves Singleton{Lock,Cookie,Socket} + RunningChromeVersion
# symlinks behind. On the next launch Chrome sees them, fails to talk to the
# (now-dead) PID listed in SingletonLock, and pops "Something went wrong when
# opening your profile. Some features may be unavailable" once per service
# (cookies, prefs, history, sync, ...) — typically 7 dialogs. Until the user
# clicks all of them, no pages load and the pipeline hangs.
#
# Safe to call before any Chrome launch on the same profile. Idempotent.
# Refuses to clean if the SingletonLock PID is still alive (so we never
# yank locks out from under a running Chrome — including a real user
# session attached to the same profile).
#
# Usage: clean_stale_singleton.sh <profile_dir>
#   e.g. clean_stale_singleton.sh ~/.claude/browser-profiles/twitter

set -uo pipefail

profile_dir="${1:-}"
if [ -z "$profile_dir" ] || [ ! -d "$profile_dir" ]; then
    echo "[clean_stale_singleton] usage: $0 <profile_dir>" >&2
    exit 0  # never block the pipeline on a misuse; just no-op
fi

lock_link="$profile_dir/SingletonLock"

# No lock = nothing to clean.
if [ ! -L "$lock_link" ] && [ ! -e "$lock_link" ]; then
    exit 0
fi

# SingletonLock target format: <hostname>-<pid>
target=$(readlink "$lock_link" 2>/dev/null || echo "")
pid="${target##*-}"

if [ -n "$pid" ] && [[ "$pid" =~ ^[0-9]+$ ]]; then
    if kill -0 "$pid" 2>/dev/null; then
        # Live Chrome owns this profile. Do NOT touch.
        echo "[clean_stale_singleton] ${profile_dir##*/}: SingletonLock PID $pid alive; leaving locks intact." >&2
        exit 0
    fi
fi

# Stale: PID dead, malformed, or unreadable. Nuke the singletons so Chrome
# can launch cleanly. Also drop RunningChromeVersion which Chrome cross-checks.
rm -f "$profile_dir/SingletonLock" \
      "$profile_dir/SingletonCookie" \
      "$profile_dir/SingletonSocket" \
      "$profile_dir/RunningChromeVersion"

echo "[clean_stale_singleton] ${profile_dir##*/}: cleared stale singleton locks (was PID ${pid:-unknown})." >&2
exit 0
