#!/usr/bin/env bash
# github-engage.sh — GitHub Issues engagement loop
# Scan our GitHub issue comments for replies, respond to substantive ones.
# Called by launchd every 6 hours.


set -euo pipefail

# GitHub engage lock: wait up to 60min for previous run to finish, then skip
source "$(dirname "$0")/lock.sh"
acquire_lock "github" 3600

# Load secrets
# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
SKILL_FILE="$REPO_DIR/SKILL.md"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/github-engage-$(date +%Y-%m-%d_%H%M%S).log"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

# Per-cycle batch id stamped onto every claude_sessions row spawned by this
# engagement run (via SA_CYCLE_ID env -> log_claude_session.py). 2026-05-10
# cycle_id rollout.
BATCH_ID="engh-$(date +%Y%m%d-%H%M%S)-$$"
export SA_CYCLE_ID="$BATCH_ID"

RUN_START=$(date +%s)
log "=== GitHub Engagement Run: $(date) (cycle=$BATCH_ID) ==="

# HTTP-only lane (2026-06-01): all read-side counts route through the s4l.ai
# HTTP API via scripts/github_engage_helper.py. The direct-Postgres lane was
# removed; DATABASE_URL, if present in .env, is deliberately ignored. No psql,
# no DB fallback.
GH_HELPER="$REPO_DIR/scripts/github_engage_helper.py"

# ═══════════════════════════════════════════════════════
# PHASE A: Scan for replies to our GitHub comments
# ═══════════════════════════════════════════════════════
log "Phase A: Scanning GitHub issues for replies..."
python3 "$REPO_DIR/scripts/scan_github_replies.py" 2>&1 | tee -a "$LOG_FILE"

# ═══════════════════════════════════════════════════════
# PHASE A.5: Refresh engagement stats on our GitHub comments
# Reactions pulled via gh api; reply counts tallied from the replies
# table that Phase A just refreshed. Stored on posts.upvotes +
# posts.comments_count. Per-reply stats also refreshed (same call), and
# the count is forwarded to a stats_github row in the dashboard Jobs table.
# ═══════════════════════════════════════════════════════
log "Phase A.5: Updating github engagement stats (reactions + reply counts)..."
# Best-effort: stats failures (Postgres disconnects, gh rate limits) must not block
# Phase B reply handling. Subshell scopes the set-flags, `|| true` absorbs rc.
PHASE_A5_START=$(date +%s)
GH_REPLY_SUMMARY=$(mktemp -t fazm-gh-reply-summary.XXXXXX)
# Chain lock cleanup into our cleanup. A plain `trap '...' EXIT` here would
# REPLACE lock.sh's `trap _sa_release_locks EXIT INT TERM HUP`, orphaning
# /tmp/social-autoposter-github.lock across runs (root cause of the stale
# github-lock orphans seen 2026-04-29). All four signals must be covered so
# watchdog SIGTERM also frees the lock.
trap 'rm -f "$GH_REPLY_SUMMARY"; _sa_release_locks' EXIT INT TERM HUP
( set +e +o pipefail
  python3 "$REPO_DIR/scripts/stats.py" --github-only --reply-summary "$GH_REPLY_SUMMARY" 2>&1 | tee -a "$LOG_FILE"
) || true
PHASE_A5_ELAPSED=$(( $(date +%s) - PHASE_A5_START ))

GH_REPLIES_REFRESHED=0
if [ -s "$GH_REPLY_SUMMARY" ]; then
    GH_REPLIES_REFRESHED=$(python3 -c "import json; print(json.load(open('$GH_REPLY_SUMMARY')).get('github', 0))" 2>/dev/null || echo 0)
fi
GH_ACTIVE=$(python3 "$GH_HELPER" posts-active-count 2>/dev/null | tr -d '[:space:]')
[ -z "$GH_ACTIVE" ] && GH_ACTIVE=0
# Emit a stats_github row so the dashboard Jobs table shows the github stats run
# the same way it shows stats_reddit / stats_twitter.
python3 "$REPO_DIR/scripts/log_run.py" --script "stats_github" --posted "$GH_ACTIVE" --skipped 0 --failed 0 --replies-refreshed "$GH_REPLIES_REFRESHED" --cost 0 --elapsed "$PHASE_A5_ELAPSED" || true
log "Phase A.5: done (replies_refreshed=$GH_REPLIES_REFRESHED)"

# ═══════════════════════════════════════════════════════
# PHASE B: Respond to pending GitHub replies
# ═══════════════════════════════════════════════════════
PENDING_COUNT=$(python3 "$GH_HELPER" pending-count 2>/dev/null | tr -d '[:space:]')
[ -z "$PENDING_COUNT" ] && PENDING_COUNT=0

if [ "$PENDING_COUNT" -eq 0 ]; then
    log "Phase B: No pending GitHub replies. Done!"
    RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
    # Pull scan-stage counters from Phase A so the empty-engage row still shows
    # "scanned N / 0 new" instead of all-zeros. scan_github_replies.py prints:
    #   Scanning N GitHub issues for replies...
    #   GitHub scan complete: N new pending, N skipped, N errors
    GH_SCAN_PROC_LINE=$(grep -m1 -E "^Scanning [0-9]+ GitHub issues" "$LOG_FILE" 2>/dev/null || true)
    GH_SCAN_DONE_LINE=$(grep -m1 "^GitHub scan complete:" "$LOG_FILE" 2>/dev/null || true)
    GH_SCAN_ARG=""
    if [ -n "$GH_SCAN_PROC_LINE" ] || [ -n "$GH_SCAN_DONE_LINE" ]; then
      gh_scanned=$(echo "$GH_SCAN_PROC_LINE" | grep -oE "[0-9]+" | head -1)
      gh_new=$(echo "$GH_SCAN_DONE_LINE" | grep -oE "[0-9]+ new pending" | grep -oE "[0-9]+" | head -1)
      gh_skip=$(echo "$GH_SCAN_DONE_LINE" | grep -oE "[0-9]+ skipped" | grep -oE "[0-9]+" | head -1)
      gh_err=$(echo "$GH_SCAN_DONE_LINE" | grep -oE "[0-9]+ errors" | grep -oE "[0-9]+" | head -1)
      parts=""
      [ -n "$gh_scanned" ] && parts="${parts}scanned=${gh_scanned},"
      [ -n "$gh_new" ]     && parts="${parts}new=${gh_new},"
      [ -n "$gh_skip" ] && [ "$gh_skip" -gt 0 ] && parts="${parts}backfill=${gh_skip},"
      [ -n "$gh_err"  ] && [ "$gh_err"  -gt 0 ] && parts="${parts}unmatched=${gh_err},"
      GH_SCAN_ARG="${parts%,}"
    fi
    if [ -n "$GH_SCAN_ARG" ]; then
      python3 "$REPO_DIR/scripts/log_run.py" --script "engage_github" --posted 0 --skipped 0 --failed 0 --cost 0 --elapsed "$RUN_ELAPSED" --scan "$GH_SCAN_ARG"
    else
      python3 "$REPO_DIR/scripts/log_run.py" --script "engage_github" --posted 0 --skipped 0 --failed 0 --cost 0 --elapsed "$RUN_ELAPSED"
    fi
    find "$LOG_DIR" -name "github-engage-*.log" -mtime +7 -delete 2>/dev/null || true
    exit 0
fi

log "Phase B: $PENDING_COUNT pending GitHub replies to process"

# One-at-a-time thread-aware orchestrator. Each reply gets its own Claude session
# with the full issue thread fetched via gh CLI, so Claude can see our prior
# comments and decide reply-or-skip with a JSON escape hatch. See
# scripts/engage_github.py for the prompt and skip-reason contract.
python3 "$REPO_DIR/scripts/engage_github.py" --timeout 3000 2>&1 | tee -a "$LOG_FILE"

# ═══════════════════════════════════════════════════════
# PHASE C: Summary
# ═══════════════════════════════════════════════════════
# engage_github.py prints a canonical LOG_RUN_SUMMARY line; we parse it and
# write ONE log_run.py row that also carries Phase A scan counters. Previously
# engage_github.py wrote its own row with no scan info and the shell wrote
# nothing on the has-work branch -- so productive cycles lost scan visibility
# and empty cycles wrote two rows (one with scan, one without).
RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
GH_SCAN_PROC_LINE=$(grep -m1 -E "^Scanning [0-9]+ GitHub issues" "$LOG_FILE" 2>/dev/null || true)
GH_SCAN_DONE_LINE=$(grep -m1 "^GitHub scan complete:" "$LOG_FILE" 2>/dev/null || true)
GH_SCAN_ARG=""
if [ -n "$GH_SCAN_PROC_LINE" ] || [ -n "$GH_SCAN_DONE_LINE" ]; then
  gh_scanned=$(echo "$GH_SCAN_PROC_LINE" | grep -oE "[0-9]+" | head -1)
  gh_new=$(echo "$GH_SCAN_DONE_LINE" | grep -oE "[0-9]+ new pending" | grep -oE "[0-9]+" | head -1)
  gh_skip=$(echo "$GH_SCAN_DONE_LINE" | grep -oE "[0-9]+ skipped" | grep -oE "[0-9]+" | head -1)
  gh_err=$(echo "$GH_SCAN_DONE_LINE" | grep -oE "[0-9]+ errors" | grep -oE "[0-9]+" | head -1)
  parts=""
  [ -n "$gh_scanned" ] && parts="${parts}scanned=${gh_scanned},"
  [ -n "$gh_new" ]     && parts="${parts}new=${gh_new},"
  [ -n "$gh_skip" ] && [ "$gh_skip" -gt 0 ] && parts="${parts}backfill=${gh_skip},"
  [ -n "$gh_err"  ] && [ "$gh_err"  -gt 0 ] && parts="${parts}unmatched=${gh_err},"
  GH_SCAN_ARG="${parts%,}"
fi

GH_SUMMARY_LINE=$(grep -m1 "^\[engage_github\] LOG_RUN_SUMMARY" "$LOG_FILE" 2>/dev/null || true)
GH_POSTED=0; GH_SKIPPED=0; GH_FAILED=0; GH_COST="0.0000"
if [ -n "$GH_SUMMARY_LINE" ]; then
  GH_POSTED=$(echo "$GH_SUMMARY_LINE"  | grep -oE "posted=[0-9]+"  | head -1 | cut -d= -f2)
  GH_SKIPPED=$(echo "$GH_SUMMARY_LINE" | grep -oE "skipped=[0-9]+" | head -1 | cut -d= -f2)
  GH_FAILED=$(echo "$GH_SUMMARY_LINE"  | grep -oE "failed=[0-9]+"  | head -1 | cut -d= -f2)
  GH_COST=$(echo "$GH_SUMMARY_LINE"    | grep -oE "cost=[0-9.]+"   | head -1 | cut -d= -f2)
  : "${GH_POSTED:=0}" "${GH_SKIPPED:=0}" "${GH_FAILED:=0}" "${GH_COST:=0.0000}"
fi

GH_LOG_RUN_ARGS=(--script "engage_github" --posted "$GH_POSTED" --skipped "$GH_SKIPPED" --failed "$GH_FAILED" --cost "$GH_COST" --elapsed "$RUN_ELAPSED")
[ -n "$GH_SCAN_ARG" ] && GH_LOG_RUN_ARGS+=(--scan "$GH_SCAN_ARG")
python3 "$REPO_DIR/scripts/log_run.py" "${GH_LOG_RUN_ARGS[@]}" || true

# Print cumulative status for visibility in the log file. One HTTP roundtrip
# for all three counts instead of three psql one-liners.
GH_COUNTS_JSON=$(python3 "$GH_HELPER" reply-counts 2>/dev/null || echo '{"pending":0,"replied":0,"skipped":0}')
TOTAL_PENDING=$(echo "$GH_COUNTS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('pending',0))" 2>/dev/null || echo "0")
TOTAL_REPLIED=$(echo "$GH_COUNTS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('replied',0))" 2>/dev/null || echo "0")
TOTAL_SKIPPED=$(echo "$GH_COUNTS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('skipped',0))" 2>/dev/null || echo "0")

log "GitHub replies cumulative: pending=$TOTAL_PENDING replied=$TOTAL_REPLIED skipped=$TOTAL_SKIPPED"

log "=== GitHub Engagement complete: $(date) ==="

# Clean up old logs
find "$LOG_DIR" -name "github-engage-*.log" -mtime +7 -delete 2>/dev/null || true
