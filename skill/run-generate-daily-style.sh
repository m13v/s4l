#!/bin/bash
# run-generate-daily-style.sh — Synthesize ONE new human-derived
# engagement style per platform per fire, from the last 24h of top
# human replies on each platform.
#
# Cadence (launchd, com.m13v.social-daily-human-style.plist): once per day
# at 08:00 local time.
#
# Wraps scripts/generate_daily_human_style.py — which queries
# thread_top_replies per platform, calls Claude via run_claude.sh, and
# POSTs each synthesized style to the s4l.ai API route
# /api/v1/engagement-styles/registry with kind='human_derived' and
# platform=<platform>. Rows land in engagement_styles_registry alongside
# seeds and model-invented styles. The engagement_styles picker reads
# the latest active row per platform with HUMAN_DERIVED_RATE_BY_PLATFORM
# probability on each pick.
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

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

log "starting daily human-style synthesizer"

# The Python script invokes scripts/run_claude.sh internally for the Claude
# call (so cost lands in claude_sessions under script_tag=daily-human-style).
# We just stream its stdout/stderr to the log file here.
/usr/bin/python3 "$REPO_DIR/scripts/generate_daily_human_style.py" 2>&1 | tee -a "$LOG_FILE"
RC=${PIPESTATUS[0]}

log "synthesizer exit code: $RC"
exit "$RC"
