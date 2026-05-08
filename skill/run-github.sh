#!/bin/bash
# Social Autoposter - GitHub Issues momentum-gated posting.
#
# Delegates to scripts/post_github.py which implements:
#   - Phase 1: gh search across project topics, snapshot T0 comment + reaction counts
#   - Sleep (T0 -> T1 momentum window, default 600s, owned by post_github --sleep)
#   - Phase 2a: re-fetch same issues, compute delta_score
#   - Phase 2b: adaptive cap (default 1, bump to 3 when >=3 candidates show momentum),
#              Claude drafts (one-shot, no Bash tools), Python posts via `gh issue comment`
#              and persists search_topic, language, engagement_style to the posts table.
#
# Reduction levers baked in:
#   (1) Historical (project, style) engagement block in drafter prompt.
#   (2) top_search_topics feedback so high-scoring seeds get preferred.
#   (3) Adaptive cap gated by per-cycle momentum.
#   (4) T0 -> T1 delta filter: stale issues drop out before Claude sees them.
#   (5) Pre-filter eliminates Claude's tool budget; one JSON in one shot.
#
# Called by launchd. Cadence is owned by the .plist, not this script.

set -euo pipefail

[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/github-$(date +%Y-%m-%d_%H%M%S).log"
RUN_START=$(date +%s)

# Idempotent run_monitor.log emitter wired to EXIT/INT/TERM/HUP. Without this,
# a SIGTERM landing during the post_github.py loop (after `gh issue comment`
# committed a comment but before the script's own log_run.py call at the
# bottom of main()) silently drops the run from run_monitor.log. The
# dashboard reads run_monitor.log, so the operator-visible "last post_github
# cycle" stays stuck on a stale entry while real comments continue landing
# unrecorded. Mirrors the same fix shipped to run-reddit-search.sh,
# run-twitter-cycle.sh, and run-linkedin.sh.
#
# Detection mechanism: post_github.py writes a "=== SUMMARY: elapsed=" line
# to stdout (teed into $LOG_FILE) IMMEDIATELY before its own log_run.py
# call. Presence of that marker in $LOG_FILE means Python wrote its own
# summary, so the trap no-ops. Absence means SIGTERM landed before Python
# could finish, and the trap emits a "sigterm:1" placeholder so the cycle
# is at least visible on the dashboard.
#
# We can't re-derive accurate POSTED counts from the DB at trap-time the way
# linkedin/twitter do — github posts go through `gh issue comment` and only
# get a posts-table row from inside post_github.py's log_post call, so a
# SIGTERM mid-loop may leave 1-2 comments live on github.com with no posts
# row yet. The placeholder marks the cycle as failed=1 + sigterm:1 so the
# operator notices; manual reconciliation if the count matters.
_SA_RUN_SUMMARY_EMITTED=0
_sa_emit_run_summary_oneshot() {
    [ "${_SA_RUN_SUMMARY_EMITTED:-0}" = "1" ] && return 0
    _SA_RUN_SUMMARY_EMITTED=1
    # Python wrote its own summary? Trust it.
    if [ -n "${LOG_FILE:-}" ] && [ -f "${LOG_FILE:-}" ] \
        && grep -q "=== SUMMARY: elapsed=" "$LOG_FILE" 2>/dev/null; then
        return 0
    fi
    local elapsed cost
    elapsed=$(( $(date +%s) - ${RUN_START:-$(date +%s)} ))
    cost=$(timeout 10 python3 "$REPO_DIR/scripts/get_run_cost.py" \
                --since "${RUN_START:-0}" \
                --scripts "post_github" \
                2>/dev/null || echo "0.0000")
    python3 "$REPO_DIR/scripts/log_run.py" \
        --script post_github \
        --posted 0 --skipped 0 --failed 1 \
        --cost "$cost" --elapsed "$elapsed" \
        --failure-reasons "sigterm:1" 2>/dev/null || true
}
trap _sa_emit_run_summary_oneshot EXIT INT TERM HUP

echo "=== GitHub Issues Run: $(date) ===" | tee "$LOG_FILE"

python3 "$REPO_DIR/scripts/post_github.py" 2>&1 | tee -a "$LOG_FILE"

echo "=== Run complete: $(date) ===" | tee -a "$LOG_FILE"

find "$LOG_DIR" -name "github-*.log" -mtime +7 -delete 2>/dev/null || true
