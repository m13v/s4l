#!/usr/bin/env bash
# promote-invented-topics.sh — promote EXPLORE_INVENT topics into project_search_topics.
#
# Scans recent twitter_candidates for (matched_project, search_topic) pairs that
# don't already exist in project_search_topics and upserts them with
# source='invented', status='active'. Idempotent + skips paused/excluded rows.
#
# Fires every 15min via com.m13v.social-promote-invented-topics.plist, aligned
# with the twitter cycle cadence so promotions land before the next USE cycle
# can re-select the invention via weighted random.

set -uo pipefail

# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/promote-invented-topics-$(date +%Y-%m-%d_%H%M%S).log"

# 24h lookback is wide enough to catch every cycle's inventions even after a
# launchd hiccup, while small enough that the API page stays under the 500-row
# limit. Set PROMOTE_HOURS in the env to override.
HOURS="${PROMOTE_HOURS:-24}"

{
    echo "[$(date +%Y-%m-%dT%H:%M:%S%z)] promote-invented-topics start (hours=$HOURS)"
    /usr/bin/python3 "$REPO_DIR/scripts/promote_invented_topics.py" --hours "$HOURS"
    rc=$?
    echo "[$(date +%Y-%m-%dT%H:%M:%S%z)] promote-invented-topics done rc=$rc"
    exit $rc
} 2>&1 | tee -a "$LOG_FILE"
