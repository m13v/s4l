#!/bin/bash
# linkedin-recovery.sh — hourly auto-recovery for the LinkedIn killswitch.
#
# Problem this solves: when LinkedIn returns an HTTP 999 / authwall, the
# killswitch (scripts/linkedin_killswitch.py) engages and every LinkedIn
# pipeline self-aborts at startup until a human re-auths and clears the flag.
# Most of the time the 999 is transient (a momentary rate-limit), the session
# cookies stay valid, and the only thing keeping LinkedIn down is the flag
# itself, which never auto-clears. That stranded the pipeline overnight on
# 2026-06-03.
#
# This job, fired hourly by launchd (com.m13v.social-linkedin-recovery), does:
#   1. recover-check — proceed ONLY if the killswitch is active AND has been so
#      for >= LINKEDIN_RECOVERY_MIN_AGE_HOURS (default 24h). The 24h wait is the
#      anti-bot rule: let the session sit idle after a 999 rather than hammering
#      the login wall every tick. Not eligible -> exit immediately (no Chrome).
#   2. Bring up the linkedin-harness Chrome (port 9556) via
#      ensure_linkedin_browser_for_backend (also takes the pipeline lock).
#   3. recover — a gentle read-only probe (ONE nav to /feed/, ONE nav to the
#      recent-activity/comments endpoint that tripped it). If healthy, it clears
#      the killswitch and emails [LI KILL] RECOVERED.
#
# When the flag clears, the six LinkedIn launchd jobs resume on their next fire
# (they all gate on the killswitch file). There is NO launchctl load/unload:
# the jobs were never unloaded, only gated, so clearing the flag is the resume.
#
# This script is a no-op (instant exit, no Chrome) on every hour the killswitch
# is inactive or younger than the threshold, so it is safe to leave loaded.

set -uo pipefail

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/linkedin-recovery.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG" >&2; }

PY="/opt/homebrew/bin/python3"
[ -x "$PY" ] || PY="/usr/bin/python3"

# Gate: only proceed if the killswitch is active AND >= threshold old.
# No Chrome launch otherwise — this is the common (no-op) path.
if ! "$PY" "$REPO_DIR/scripts/linkedin_killswitch.py" recover-check >>"$LOG" 2>&1; then
    exit 0
fi

log "killswitch eligible for auto-recovery; bringing up harness Chrome for gentle probe"

# linkedin-backend.sh exports LINKEDIN_CDP_URL + LINKEDIN_DISCOVER_PYTHON and
# provides ensure_linkedin_browser_for_backend (launches port-9556 Chrome and
# acquires the cross-pipeline lock). Identify ourselves as the lock holder.
export SAPS_PIPELINE_NAME="linkedin-recovery"
# shellcheck disable=SC1091
source "$REPO_DIR/skill/lib/linkedin-backend.sh"

if ! ensure_linkedin_browser_for_backend; then
    log "ERROR: could not bring up linkedin-harness Chrome; will retry next hour"
    exit 0
fi

# The probe needs a Playwright-capable interpreter (3.14 lacks it; the backend
# resolves a working one into LINKEDIN_DISCOVER_PYTHON).
PROBE_PY="${LINKEDIN_DISCOVER_PYTHON:-$PY}"
RESULT="$("$PROBE_PY" "$REPO_DIR/scripts/linkedin_killswitch.py" recover \
    --cdp-url "$LINKEDIN_CDP_URL" 2>>"$LOG")"
log "recover result: $RESULT"

# On recovery the flag is now gone; the six LinkedIn jobs resume on their next
# launchd fire. Nothing to load/unload here.
exit 0
