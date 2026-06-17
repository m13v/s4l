#!/bin/bash
# Health check for the browser session-lock fix (2026-06-16).
# Full context: docs/twitter_browser_lock.md. Logic test: scripts/test_browser_lock.py.
#
# Prints [ok ] / [BAD] / [i  ] lines so you can tell at a glance whether the fix is
# present in code AND behaving in production. Exits non-zero if any [BAD] is found.
#
#   bash scripts/check_browser_lock_health.sh [HOURS]   # default lookback 24h
#
# What it checks (see docs §4/§5 for the why):
#   1. fix still present in twitter_browser.py + linkedin_browser.py (catch a revert)
#   2. defect-b `rm -f` has NOT crept back into skill/*.sh
#   3. reclaim markers firing  = fix actively catching dead holders (positive signal)
#   4. starvation giveups WITHOUT the peer-alive tell = defect (a) recurring (BAD)
#   5. shell-lock trap_rm owner=OTHER = a pipeline deleted a LIVE peer's lock (BAD)
set -u
cd "$(dirname "$0")/.."
LOGS="skill/logs"
HOURS="${1:-24}"
bad=0

# Dated per-run logs touched in the window. (NOT launchd-*.log: those are append-only,
# so their mtime says nothing about when a line inside was written.)
recent=$(find "$LOGS" -maxdepth 1 -name '*2026-*.log' -mmin "-$((HOURS*60))" 2>/dev/null | grep -v 'launchd-' || true)
[ -z "$recent" ] && recent="/dev/null"   # guard: never let grep read stdin

echo "== browser-lock health (last ${HOURS}h of dated per-run logs) =="

# 1. fix present in code
if grep -q _is_python_holder_alive scripts/twitter_browser.py 2>/dev/null \
   && grep -q _is_python_holder_alive scripts/linkedin_browser.py 2>/dev/null; then
  echo "[ok ] fix present (twitter_browser.py + linkedin_browser.py)"
else
  echo "[BAD] fix MISSING from code -> reverted (re-apply from docs/twitter_browser_lock.md)"; bad=1
fi

# 2. defect-b rm -f gone from shells (anchored so the explanatory comment is not a hit).
#    Scope to *.sh only -- never recurse skill/ (it holds a huge claude-sessions/ tree).
if grep -hEq '^[[:space:]]*rm -f .*twitter-browser-lock\.json' skill/*.sh skill/lib/*.sh 2>/dev/null; then
  echo "[BAD] defect-b: an actual 'rm -f ...twitter-browser-lock.json' is back in skill/*.sh"; bad=1
else
  echo "[ok ] no rm -f of the session lock in skill/*.sh"
fi

# 3. positive: reclaim markers (each = a dead holder caught that USED to starve the fleet)
rec=$(grep -hoE '\[browser_lock\] reclaimed .*reason=[a-z_]+' $recent 2>/dev/null | wc -l | tr -d ' ')
recd=$(grep -hoE '\[browser_lock\] reclaimed .*reason=dead_python' $recent 2>/dev/null | wc -l | tr -d ' ')
echo "[i  ] reclaim markers fired: ${rec:-0} (of which dead_python: ${recd:-0}) -- 0 is fine if nothing crashed"

# 4. starvation: twitter giveup without 'peer alive' / linkedin profile_locked without 'peer_alive'
bad_tw=$(grep -hE 'locked by session .* giving up' $recent 2>/dev/null | grep -vc 'peer alive' || true)
bad_li=$(grep -hE 'profile_locked' $recent 2>/dev/null | grep -vc 'peer_alive' || true)
bad_tw=${bad_tw:-0}; bad_li=${bad_li:-0}
if [ "$bad_tw" -gt 0 ] || [ "$bad_li" -gt 0 ]; then
  echo "[BAD] old-format starvation giveups: twitter=$bad_tw linkedin=$bad_li (defect a recurring?)"; bad=1
else
  echo "[ok ] no old-format starvation giveups (twitter + linkedin)"
fi

# 5. shell-lock: dangerous trap_rm owner=OTHER (deleting a live peer's shell lock).
#    NOTE: 'event=stale_reclaim ... owner=OTHER' is LEGITIMATE (reclaiming a dead holder),
#    only 'event=trap_rm ... owner=OTHER' is the bad one.
if [ -f "$LOGS/lock-events.log" ]; then
  to=$(grep -cE 'event=trap_rm .*owner=OTHER' "$LOGS/lock-events.log" 2>/dev/null || echo 0)
  if [ "${to:-0}" -gt 0 ]; then
    echo "[BAD] shell trap_rm owner=OTHER x$to (a pipeline deleted a LIVE peer's shell lock)"; bad=1
  else
    echo "[ok ] shell-lock: no trap_rm owner=OTHER (live-lock deletes)"
  fi
fi

echo "=================================================="
if [ "$bad" -ne 0 ]; then
  echo "RESULT: ATTENTION NEEDED (see [BAD] above)"; exit 1
fi
echo "RESULT: HEALTHY"
