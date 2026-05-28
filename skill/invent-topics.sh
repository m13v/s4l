#!/usr/bin/env bash
# invent-topics.sh — hourly topic invention job (replaces in-cycle EXPLORE_INVENT).
#
# Picks ONE project via pick_projects() (same inverse-recent-share weighting
# the post-comments cycle uses), then calls Claude to propose N new
# search_topic candidates given that project's ledger. Validates each
# proposal against the universe (exact-match + Jaccard similarity), commits
# survivors to project_search_topics with source='invented', status='active',
# and appends an audit row to state/invented_topics_audit.jsonl.
#
# Fires hourly via com.m13v.social-invent-topics.plist. Deliberately runs
# outside the 15-min cycle cadence because invention doesn't need realtime;
# new topics added at minute :00 vs :30 make no difference to engagement.

set -uo pipefail

# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/invent-topics-$(date +%Y-%m-%d_%H%M%S).log"

# Number of candidate topics to ask Claude for per attempt. Picker validation
# typically rejects 30-60% of these as dupes/near-dupes, so 4 proposals
# usually means 1-3 land. Override via INVENT_PROPOSALS_PER_RUN.
PROPOSALS="${INVENT_PROPOSALS_PER_RUN:-4}"

# Retry loop: keep re-asking Claude (steering away from already-tried topics)
# until TARGET genuinely-new non-dupe topics land or MAX_ATTEMPTS Claude calls
# are spent, then proceed with whatever survived. Tune via env.
TARGET="${INVENT_TARGET:-3}"
MAX_ATTEMPTS="${INVENT_MAX_ATTEMPTS:-5}"

{
    echo "[$(date +%Y-%m-%dT%H:%M:%S%z)] invent-topics start (proposals=$PROPOSALS target=$TARGET max_attempts=$MAX_ATTEMPTS)"
    /usr/bin/python3 "$REPO_DIR/scripts/invent_topics.py" \
        --proposals "$PROPOSALS" \
        --target "$TARGET" \
        --max-attempts "$MAX_ATTEMPTS"
    rc=$?
    echo "[$(date +%Y-%m-%dT%H:%M:%S%z)] invent-topics done rc=$rc"
    exit $rc
} 2>&1 | tee -a "$LOG_FILE"
