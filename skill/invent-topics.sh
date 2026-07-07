#!/usr/bin/env bash
# invent-topics.sh — topic invention job (replaces in-cycle EXPLORE_INVENT).
#
# Picks ONE project via pick_projects() (same inverse-recent-share weighting
# the post-comments cycle uses), then runs queue-routed Claude turns to
# propose new search_topic candidates given that project's ledger. Validates
# each proposal against the universe (exact-match + Jaccard similarity),
# commits survivors to project_search_topics with source='invented',
# status='active', and POSTs an audit row to /api/v1/invented-topics-audit.
#
# Queue-native (2026-07-06): invoked by the run-draft-and-publish.sh kicker
# on EVERY install (state-file gate, S4L_INVENT_EVERY_HOURS), operator Mac
# included. Every Claude turn is a job on the local claude-queue drained by
# the Desktop scheduled-task worker; there is no claude CLI dependency. The
# operator-only launchd job com.m13v.social-invent-topics is retired.

set -uo pipefail

# Honor S4L_REPO_DIR (set by the MCP wrapper + the kicker on .mcpb installs)
# so the plugin's installed package resolves its own scripts.
REPO_DIR="${S4L_REPO_DIR:-$HOME/social-autoposter}"

# shellcheck source=/dev/null
[ -f "$REPO_DIR/.env" ] && source "$REPO_DIR/.env"

PY="${S4L_PYTHON:-python3}"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/invent-topics-$(date +%Y-%m-%d_%H%M%S).log"

# Number of candidate topics to ask Claude for per attempt. One topic per loop
# matches the new supply-test rhythm: invent ONE topic, draft its queries,
# supply-test, gate on supply, decide whether to loop again. With the post-
# 2026-05-29 dupe-retry-doesn't-burn-attempts behavior, asking for more than
# one is wasteful (a dupe-only Claude call retries cost-free anyway).
# Override via INVENT_PROPOSALS_PER_RUN.
PROPOSALS="${INVENT_PROPOSALS_PER_RUN:-1}"

# Stop the run as soon as ONE topic clears the supply floor — the qualifying
# tweet count IS the real target, not "how many topics qualified." A single
# topic with supply >= SUPPLY_FLOOR fresh tweets is enough; no need to keep
# burning Claude calls on additional topics that hour. MAX_ATTEMPTS caps the
# loop only if the project is genuinely dry (no qualifier in N tries).
TARGET="${INVENT_TARGET:-1}"
MAX_ATTEMPTS="${INVENT_MAX_ATTEMPTS:-5}"

{
    echo "[$(date +%Y-%m-%dT%H:%M:%S%z)] invent-topics start (proposals=$PROPOSALS target=$TARGET max_attempts=$MAX_ATTEMPTS)"
    S4L_REPO_DIR="$REPO_DIR" "$PY" "$REPO_DIR/scripts/invent_topics.py" \
        --proposals "$PROPOSALS" \
        --target "$TARGET" \
        --max-attempts "$MAX_ATTEMPTS"
    rc=$?
    echo "[$(date +%Y-%m-%dT%H:%M:%S%z)] invent-topics done rc=$rc"
    exit $rc
} 2>&1 | tee -a "$LOG_FILE"
