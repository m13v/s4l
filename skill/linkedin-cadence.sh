#!/bin/bash
# linkedin-cadence.sh — enforce the 4-active-day / 2-day-break LinkedIn
# posting schedule (user instruction, 2026-07-11). Fired every 15 minutes by
# launchd (com.m13v.social-linkedin-cadence). All logic lives in
# scripts/linkedin_cadence.py; this wrapper just logs the tick.

set -uo pipefail
export PATH="/opt/homebrew/bin:$PATH"

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/linkedin-cadence.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG" >&2; }

PY="/opt/homebrew/bin/python3"
[ -x "$PY" ] || PY="/usr/bin/python3"

"$PY" "$REPO_DIR/scripts/linkedin_cadence.py" enforce >>"$LOG" 2>&1
