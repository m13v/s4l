#!/usr/bin/env bash
# topic-ledger.sh — refresh the materialized per-topic funnel ledger.
#
# Aggregates twitter_search_attempts + twitter_candidates + posts +
# post_link_clicks per (project, search_topic) and writes the result to
# ~/social-autoposter/state/topic_ledger.json (atomic temp+rename).
#
# Readers:
#   - scripts/invent_topics.py (lookup_topic_neighbors for proposal dedupe)
#   - dashboard surfaces (future)
#
# Fires every 15min via com.m13v.social-topic-ledger.plist, aligned with
# the twitter cycle cadence so invent_topics.py always reads a ledger
# that's at most 15 min stale.

set -uo pipefail

# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/topic-ledger-$(date +%Y-%m-%d_%H%M%S).log"

# 30d window matches the picker's WINDOW_DAYS so the ledger and the
# picker's signal join consistently. Override via TOPIC_LEDGER_WINDOW_DAYS
# if we ever need a different period.
WINDOW="${TOPIC_LEDGER_WINDOW_DAYS:-30}"

{
    echo "[$(date +%Y-%m-%dT%H:%M:%S%z)] topic-ledger start (window=${WINDOW}d)"
    /usr/bin/python3 "$REPO_DIR/scripts/topic_ledger.py" --window-days "$WINDOW"
    rc=$?
    echo "[$(date +%Y-%m-%dT%H:%M:%S%z)] topic-ledger done rc=$rc"
    exit $rc
} 2>&1 | tee -a "$LOG_FILE"
