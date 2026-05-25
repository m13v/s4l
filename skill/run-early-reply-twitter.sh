#!/usr/bin/env bash
# run-early-reply-twitter.sh — early-reply rail driver for the twitterapi.io
# webhook flow. Reads status='observed' rows from early_reply_candidates
# (populated by bin/server.js when filter rules fire on monitored handles),
# pre-filters, scores by views/min, asks Claude to judge ONE candidate, and on
# a 'draft' decision posts via the twitter-harness Chrome.
#
# Separate from run-twitter-cycle.sh by design (different source = webhook
# push vs search pull, different cadence = ~5 min vs 15 min, narrower scope =
# fazm only at launch, different daily cap = 3). The two rails share the
# twitter-browser lock, the engagement-style picker, dm_short_links, and the
# log_post.py writer — but their decision logic and table schemas are disjoint.
#
# Called by launchd com.m13v.social-early-reply (disabled by default —
# `launchctl bootstrap gui/$(id -u) launchd/com.m13v.social-early-reply.plist`
# to enable after dry-run verification).

set -euo pipefail

BATCH_ID="${BATCH_ID:-earlyreply-$(date +%Y%m%d-%H%M%S)}"
export BATCH_ID
export SA_CYCLE_ID="$BATCH_ID"

LOG_DIR="$HOME/social-autoposter/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run-early-reply-twitter-$(date +%Y-%m-%d_%H%M%S).log"

source "$(dirname "$0")/lock.sh"
source "$(dirname "$0")/lib/twitter-backend.sh"

acquire_lock "twitter-browser" 1800
ensure_twitter_browser_for_backend 2>&1 | tee -a "$LOG_FILE"
acquire_lock "early-reply-twitter" 900

# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"

if [ -z "${DATABASE_URL:-}" ]; then
    echo "ERROR: DATABASE_URL not set in ~/social-autoposter/.env" | tee -a "$LOG_FILE"
    exit 1
fi

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

RUN_START=$(date +%s)
log "=== Early-reply Twitter Run: $(date) batch_id=$BATCH_ID ==="

# Run the pipeline. The Python script prints exactly ONE JSON summary line on
# stdout (final line). All progress noise goes to its own prints + stderr.
SUMMARY_FILE=$(mktemp -t early_reply_summary.XXXXXX.json)
set +e
python3 "$REPO_DIR/scripts/early_reply_cycle.py" 2>&1 | tee -a "$LOG_FILE" | tail -1 > "$SUMMARY_FILE"
PY_RC=${PIPESTATUS[0]}
set -e

POSTED=0
SKIPPED=0
FAILED=0
if [ "$PY_RC" = "0" ] && [ -s "$SUMMARY_FILE" ]; then
    POSTED=$(python3 -c "import json,sys;d=json.load(open('$SUMMARY_FILE'));print(d.get('posted',0))" 2>/dev/null || echo 0)
    SKIPPED=$(python3 -c "import json,sys;d=json.load(open('$SUMMARY_FILE'));print(d.get('skipped',0))" 2>/dev/null || echo 0)
    FAILED=$(python3 -c "import json,sys;d=json.load(open('$SUMMARY_FILE'));print(d.get('failed',0))" 2>/dev/null || echo 0)
else
    log "Pipeline exited rc=$PY_RC; counting as failed."
    FAILED=1
fi
rm -f "$SUMMARY_FILE"

RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
_COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --cycle-id "$BATCH_ID" 2>/dev/null || echo "0.0000")
log "Summary: posted=$POSTED skipped=$SKIPPED failed=$FAILED cost=\$$_COST elapsed=${RUN_ELAPSED}s"

python3 "$REPO_DIR/scripts/log_run.py" \
    --script "early_reply_twitter" \
    --posted "$POSTED" \
    --skipped "$SKIPPED" \
    --failed "$FAILED" \
    --cost "$_COST" \
    --elapsed "$RUN_ELAPSED" 2>/dev/null || true

find "$LOG_DIR" -name "run-early-reply-twitter-*.log" -mtime +7 -delete 2>/dev/null || true

log "=== Early-reply Twitter complete: $(date) ==="
