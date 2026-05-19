#!/bin/bash
# Social Autoposter dashboard funnel cache pre-warmer.
#
# /api/funnel/per-day shells out to scripts/funnel_per_day.py which issues one
# HogQL query per metric per project against PostHog. On a cold cache a single
# project's call takes 5-25s; without pre-warming the dashboard's per-project
# breakdown timed out on 19 of 23 projects' funnel fetch on first page-load
# and rendered them as silent zeros.
#
# Strategy: serial calls (not parallel — PostHog rate-limits, and the python
# script already fans out per-metric internally), longer per-call timeout
# than the dashboard uses (180s vs 30s frontend), against both the launchd
# dashboard (3141) and the dev --watch instance (3142, if alive).
#
# Scheduled by com.m13v.social-funnel-prewarm.plist every 240s. The server
# cache TTL is 300s, so a 240s cadence keeps cache continuously hot.

set -uo pipefail

REPO_DIR="${REPO_DIR:-/Users/matthewdi/social-autoposter}"
LOG_DIR="${REPO_DIR}/skill/logs"
LOG_FILE="${LOG_DIR}/prewarm-funnel.log"
mkdir -p "$LOG_DIR"

ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }
log() { echo "[$(ts)] $*" >> "$LOG_FILE"; }

# Single-instance guard. A full cycle takes 2-5min (25 projects x 2 day-windows
# x N ports, each call 5-180s); launchd fires every 240s, so without this guard
# the script stacks (saw 2 stale processes from 11:22 + 11:33 on 2026-05-19
# both still running at 11:35, multiplying PostHog + dashboard pg-pool load and
# wedging both Get Started cards and per-project breakdown).
# macOS ships no flock(1), so we use a PID file: a previous process's PID is
# considered live iff `kill -0 PID` succeeds and the proc is still bash.
PID_FILE="/tmp/social-autoposter-prewarm-funnel.pid"
if [ -f "$PID_FILE" ]; then
  prev=$(cat "$PID_FILE" 2>/dev/null || true)
  if [ -n "$prev" ] && kill -0 "$prev" 2>/dev/null && ps -p "$prev" -o comm= 2>/dev/null | grep -qE "bash|sh"; then
    log "another prewarm cycle (pid=$prev) in progress; skipping this tick"
    exit 0
  fi
fi
echo "$$" > "$PID_FILE"
trap 'rm -f "$PID_FILE"' EXIT INT TERM

projects=()
while IFS= read -r line; do
  [ -n "$line" ] && projects+=("$line")
done < <(jq -r '.projects[].name' "$REPO_DIR/config.json")

# Discover live dashboard ports. Probe root URL (no auth, cheap).
ports=()
for port in 3141 3142; do
  if curl -sS -o /dev/null -w "%{http_code}" --max-time 3 "http://127.0.0.1:$port/" 2>/dev/null | grep -qE "^(200|301|302|401|403)$"; then
    ports+=("$port")
  fi
done

if [ "${#ports[@]}" -eq 0 ]; then
  log "no dashboard listeners on 3141 or 3142; bailing"
  exit 0
fi

log "start projects=${#projects[@]} ports=${ports[*]}"

# Warm one call at a time. The bottleneck is PostHog HogQL latency, not local
# CPU; serializing means cache builds up monotonically as each project lands.
# Per-call timeout 180s — generous enough that even worst-case cold projects
# finish, but capped so a wedged PostHog can't hang the launchd job forever.
ok=0
fail=0
slow=0

call_one() {
  local url="$1"
  local label="$2"
  local t code
  read -r code t < <(curl -sS -o /dev/null -w "%{http_code} %{time_total}" --max-time 180 "$url" 2>/dev/null || echo "000 -1")
  if [ "$code" = "200" ]; then
    ok=$((ok+1))
    # Anything over 10s is "slow"; useful signal that the cache was cold here.
    if awk "BEGIN{exit !($t > 10)}"; then slow=$((slow+1)); fi
  else
    fail=$((fail+1))
    log "fail $label code=$code time=${t}s"
  fi
}

for port in "${ports[@]}"; do
  for days in 30 91; do
    # Top-chart "all projects" rollup first — most important call, and it
    # populates the PostHog-side connection cache for the per-project loop
    # that follows.
    call_one "http://127.0.0.1:$port/api/funnel/per-day?days=$days" \
             "port=$port days=$days project=__all__"
    for p in "${projects[@]}"; do
      enc=$(printf '%s' "$p" | jq -sRr @uri)
      call_one "http://127.0.0.1:$port/api/funnel/per-day?days=$days&project=$enc" \
               "port=$port days=$days project=$p"
    done
  done
done

log "done ok=$ok fail=$fail slow=$slow"
