#!/bin/bash
# Social Autoposter - MoltBook phased posting cycle.
#
# Delegates to scripts/run_moltbook_cycle.py which implements:
#   - Phase 1: scan hot + new via MoltBook API, snapshot T0
#   - Sleep 600s (T0 -> T1 momentum window)
#   - Phase 2a: re-poll for T1, compute delta_score
#   - Phase 2b: adaptive cap (default 2, bump to 5 when >=3 candidates
#              show real-time momentum), Claude drafts, Python posts
#
# Three reduction levers baked in:
#   (1) Historical (project, style) engagement block injected into the drafter prompt.
#   (2) Adaptive cap gated by per-cycle momentum.
#   (3) T0 -> T1 delta filter: dead threads drop out before Claude sees them.
#
# Called by launchd every 30 minutes.

set -euo pipefail

[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run-moltbook-$(date +%Y-%m-%d_%H%M%S).log"

# Per-cycle batch id stamped onto every claude_sessions row spawned by this
# run (via SA_CYCLE_ID env -> log_claude_session.py). 2026-05-10 cycle_id rollout.
BATCH_ID="mbcycle-$(date +%Y%m%d-%H%M%S)-$$"
export SA_CYCLE_ID="$BATCH_ID"

echo "=== MoltBook Post Run: $(date) (cycle=$BATCH_ID) ===" | tee "$LOG_FILE"

python3 "$REPO_DIR/scripts/run_moltbook_cycle.py" 2>&1 | tee -a "$LOG_FILE"

echo "=== Run complete: $(date) ===" | tee -a "$LOG_FILE"

find "$LOG_DIR" -name "run-moltbook-*.log" -mtime +7 -delete 2>/dev/null || true
