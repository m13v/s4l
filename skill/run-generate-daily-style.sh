#!/bin/bash
# run-generate-daily-style.sh — Synthesize one new human-derived engagement
# style per fire from the last 24h of top human Twitter replies.
#
# Cadence (launchd, com.m13v.social-daily-human-style.plist): once per day at
# 08:00 local time.
#
# Wraps scripts/generate_daily_human_style.py — which queries
# thread_top_replies, calls Claude via run_claude.sh, and inserts one row
# into engagement_styles_human_derived. The engagement_styles picker reads
# the latest active row on a 5% probability per Twitter reply.
#
# Exit codes:
#   0 — style inserted, OR insufficient input (< 3 replies, logged + skipped)
#   1 — real failure (DB error, Claude error, JSON parse failure)
#
# Logs: skill/logs/daily-human-style-YYYY-MM-DD_HHMMSS.log

set -uo pipefail

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"

LOG_FILE="$LOG_DIR/daily-human-style-$(date +%Y-%m-%d_%H%M%S).log"

if [ -f "$REPO_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO_DIR/.env"
  set +a
fi

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

log "starting daily human-style synthesizer"

# The Python script invokes scripts/run_claude.sh internally for the Claude
# call (so cost lands in claude_sessions under script_tag=daily-human-style).
# We just stream its stdout/stderr to the log file here.
/usr/bin/python3 "$REPO_DIR/scripts/generate_daily_human_style.py" 2>&1 | tee -a "$LOG_FILE"
RC=${PIPESTATUS[0]}

log "synthesizer exit code: $RC"
exit "$RC"
