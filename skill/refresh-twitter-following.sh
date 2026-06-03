#!/usr/bin/env bash
# refresh-twitter-following.sh — refresh the cached "who we follow" list for X.
#
# Scrapes x.com/<handle>/following via the harness Chrome and uploads the set to
# /api/v1/followed-accounts. score_twitter_candidates.py's follow-gate reads that
# set to skip discovered threads whose author we already follow. The follow list
# changes slowly, so launchd fires this a few times a day
# (com.m13v.social-refresh-twitter-following).
#
# Uses the SAME shared "twitter-browser" lock + harness bootstrap as
# engage-twitter.sh / run-twitter-cycle.sh, so it never races a live cycle. On
# lock contention it skips this run (exit 0) and retries next schedule.

set -uo pipefail

LOG_DIR="$HOME/social-autoposter/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/refresh-twitter-following-$(date +%Y-%m-%d_%H%M%S).log"
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

REPO_DIR="$HOME/social-autoposter"

# Shared twitter-browser lock (lock.sh installs the EXIT-trap release) + harness
# bootstrap (sets/export TWITTER_CDP_URL, provides ensure_twitter_browser_for_backend).
# shellcheck source=/dev/null
source "$(dirname "$0")/lock.sh"
# shellcheck source=/dev/null
source "$(dirname "$0")/lib/twitter-backend.sh"

log "=== Refresh Twitter following list: $(date) ==="
log "Acquiring twitter-browser lock (pid=$$)..."
if ! acquire_lock "twitter-browser" 1800 2>>"$LOG_FILE"; then
    log "twitter-browser busy (a cycle is running); skipping this refresh."
    exit 0
fi
log "twitter-browser lock held (pid=$$)"

# Probe + launch harness Chrome on port 9555 if needed, then sweep leftover tabs.
ensure_twitter_browser_for_backend 2>&1 | tee -a "$LOG_FILE"

# Load .env so http_api.py picks up AUTOPOSTER_API_BASE / AUTOPOSTER_API_KEY.
# shellcheck source=/dev/null
[ -f "$REPO_DIR/.env" ] && source "$REPO_DIR/.env"

log "Scraping following list + uploading to /api/v1/followed-accounts..."
python3 "$REPO_DIR/scripts/harvest_twitter_following.py" 2>&1 | tee -a "$LOG_FILE"
RC=${PIPESTATUS[0]}
log "harvest_twitter_following.py exit code: $RC"

# Exit 0 regardless: a benign incomplete-scrape (rc=3) or empty (rc=2) should not
# flag the launchd job as failed; the next schedule retries.
exit 0
