#!/bin/bash
# Idempotent supervisor for the twitter-harness on-screen overlay watcher.
#
# WHAT: keeps exactly ONE `harness_overlay.py watch` process alive. That watcher
# injects the status overlay into the twitter-harness Chrome window so a human
# watching the harness sees what the pipeline is doing.
#
# WHY a supervisor: the overlay only renders WHILE the watch process runs. It was
# previously a manual, local-only process, so it never appeared on headless /
# remote installs. This script makes it self-starting from BOTH install lanes:
#   - Lane A (npm/cli): launchd job `com.m13v.social-overlay-watch` (StartInterval
#     60 + RunAtLoad) re-invokes this script every minute; the pgrep guard makes
#     re-invocation a no-op while the watcher is already up.
#   - Lane B (.mcpb / pure MCP): the MCP calls this script on draft_cycle /
#     autopilot-enable / show_browser_to_user, threading SAPS_PYTHON + SAPS_LOG_DIR.
#
# IDEMPOTENT: safe to call on a 60s timer. If a watcher is already running it
# exits 0 immediately. Otherwise it spawns one detached (nohup, own session) and
# returns; the spawned watcher then runs until the machine/MCP tears it down.
#
# This script is intentionally NOT locked: the overlay UX is expected to evolve.

set -u

# --- resolve the repo this script lives in (works from launchd + MCP cwd) -----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
OVERLAY_PY="${REPO_DIR}/scripts/harness_overlay.py"

if [ ! -f "${OVERLAY_PY}" ]; then
  echo "[overlay-watch] harness_overlay.py not found at ${OVERLAY_PY}; nothing to do" >&2
  exit 0
fi

# --- idempotency guard: at most one watcher, ever ----------------------------
# Match the full `harness_overlay.py watch` invocation (NOT a bare `grep -r`, so
# this never trips the BSD-grep-on-FIFO hang noted in the repo CLAUDE.md).
if pgrep -f "harness_overlay.py watch" >/dev/null 2>&1; then
  exit 0
fi

# --- resolve a python interpreter --------------------------------------------
# harness_overlay.py self-heals to a playwright-capable interpreter on its own
# (see _ensure_playwright_interpreter), so any python3 that exists is fine to
# launch with. Prefer the MCP-threaded SAPS_PYTHON (owned uv runtime), then the
# usual suspects.
PYBIN=""
for _cand in \
  "${SAPS_PYTHON:-}" \
  "/opt/homebrew/bin/python3.11" \
  "/usr/bin/python3" \
  "/opt/homebrew/bin/python3"
do
  if [ -n "${_cand}" ] && [ -x "${_cand}" ]; then PYBIN="${_cand}"; break; fi
done
if [ -z "${PYBIN}" ]; then
  PYBIN="$(command -v python3 2>/dev/null || true)"
fi
if [ -z "${PYBIN}" ]; then
  echo "[overlay-watch] no python3 found; cannot start overlay watcher" >&2
  exit 0
fi

# --- env the watcher needs ---------------------------------------------------
# SAPS_LOG_DIR: where harness_overlay.py reads cycle logs to decide busy/idle.
# Default to this repo's skill/logs (MCP overrides it to the materialized repo).
export SAPS_LOG_DIR="${SAPS_LOG_DIR:-${REPO_DIR}/skill/logs}"
# CDP target for the twitter harness Chrome (honor BYO-Chrome installs).
export TWITTER_CDP_URL="${TWITTER_CDP_URL:-http://127.0.0.1:9555}"
mkdir -p "${SAPS_LOG_DIR}" 2>/dev/null || true
WATCH_LOG="${SAPS_LOG_DIR}/overlay-watch.log"

# --- spawn detached ----------------------------------------------------------
cd "${REPO_DIR}" || exit 0
echo "[overlay-watch] $(date '+%Y-%m-%d %H:%M:%S') starting watcher py=${PYBIN} cdp=${TWITTER_CDP_URL} log=${SAPS_LOG_DIR}" >>"${WATCH_LOG}" 2>&1
# Spawn the watcher in a NEW SESSION so it outlives this supervisor.
# When launchd fires this script (StartInterval 60), the script is the job's
# main process; the moment it exits, launchd reaps the WHOLE job process group.
# `nohup` only blocks SIGHUP, not that group SIGKILL, so a plain `nohup ... &`
# child dies the instant we `exit 0` (it survives only when this runs from an
# interactive shell, which is NOT a launchd job). macOS ships no setsid(1), so
# use the python we already resolved to os.setsid() off the launchd group, then
# exec the real watcher. The watcher's own interpreter self-heal (os.execv)
# keeps the new session.
nohup "${PYBIN}" -c 'import os, sys
try:
    os.setsid()
except OSError:
    pass
os.execv(sys.argv[1], sys.argv[1:])' "${PYBIN}" "${OVERLAY_PY}" watch >>"${WATCH_LOG}" 2>&1 &
disown 2>/dev/null || true
exit 0
