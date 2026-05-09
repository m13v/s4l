#!/usr/bin/env bash
# stats-linkedin-comments.sh — LinkedIn comment-engagement stats refresh.
#
# Pure-Python pipeline (no Claude in the loop). Scrapes /in/me/recent-
# activity/comments/ via headed Chromium against the linkedin-agent's
# persistent profile, harvests OUR comments' impressions/reactions/replies
# in ONE page.evaluate, and applies them to the replies table.
#
# Replaces the previous claude -p driven version. That cost ~$0.10-0.30
# per fire (skill + system prompt + tool schemas through the model) for
# work that is 100% deterministic. This rewrite cuts the per-fire cost
# to ~$0 and the wall time to ~2 min from ~5 min.
#
# What we collect:
#   - impressions ("<N> impressions" leaf text on the comments tab)
#   - reactions   (button[aria-label*=Reaction], or 0 fallback when
#                  Like+Reply leaves are present but no count)
#   - replies     ("<N> reply" / "<N> replies" leaf)
#
# What we DO NOT collect:
#   - thread author's post stats (handled by stats-linkedin.sh)
#
# Bot-detection prevention (the 2026-04-17 LinkedIn flag was caused by
# Voyager API + per-permalink scroll-and-expand loops; the 2026-05-05
# logout was caused by a deleted scrape_linkedin_stats_browser.py that
# looped page.goto over /feed/update/<urn>/ permalinks):
#   1. ONE page.goto per fire, to /in/me/recent-activity/comments/.
#      No warmup nav, no permalink hops, no /analytics/ permalinks.
#   2. ONE page.evaluate; the slow scroll loop runs INSIDE the evaluate.
#      Randomized 600-1100px increments, randomized 1.8-3.5s pauses.
#   3. Harvest-during-scroll (Map keyed by comment_id) because LinkedIn
#      virtualizes the list and an end-only harvest misses older items.
#      MAX_SCROLLS=40 with early-stop when 4 consecutive ticks find no
#      new comments AND no scrollHeight growth.
#   4. No clicks. No "Show more". No "View analytics".
#   5. SESSION_INVALID detection: redirect to /login or /checkpoint, or
#      captcha/security-check page text -> stop, do not type credentials.
#   6. wrong_page detection: page loaded but no comment URNs and no
#      "X impressions" text -> stop, do not retry blindly.
#
# Cadence: every 4-6h. LinkedIn updates comment impressions in near-
# realtime but per-fire fingerprint risk is non-zero, so don't run hotter.

set -euo pipefail

source "$(dirname "$0")/lock.sh"

# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
PYTHON_BIN="/opt/homebrew/bin/python3"
# /usr/bin/python3 is the only interpreter with playwright installed; this
# matches engage-dm-replies.sh's call to linkedin_browser.py. The DB scripts
# stay on homebrew python where psycopg2 is installed.
SCRAPER_PYTHON_BIN="/usr/bin/python3"

# Tunables.
MAX_SCROLLS=40           # in-page scrolls; bumped from 15 to extend reach
SCRAPER_TIMEOUT_SEC=480  # whole Python run cap (2.5min scroll + overhead)

if [ -z "${DATABASE_URL:-}" ]; then
    echo "ERROR: DATABASE_URL not set in ~/social-autoposter/.env"
    exit 1
fi

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/stats-linkedin-comments-$(date +%Y-%m-%d_%H%M%S).log"
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

RUN_START=$(date +%s)
log "=== LinkedIn Comment Stats Run: $(date) ==="
log "mode: python (no LLM); MAX_SCROLLS=$MAX_SCROLLS; timeout=${SCRAPER_TIMEOUT_SEC}s"

# Coverage hint.
ACTIVE_COUNT=$("$PYTHON_BIN" -c "
import sys; sys.path.insert(0, '$REPO_DIR/scripts')
import db as dbmod; dbmod.load_env(); db = dbmod.get_conn()
cur = db.execute(\"\"\"SELECT COUNT(*) AS n FROM replies
  WHERE platform='linkedin' AND status IN ('replied', 'posted')
    AND our_reply_url IS NOT NULL AND our_reply_url ~ 'commentUrn'\"\"\")
print(cur.fetchone()['n'])
" 2>/dev/null || echo "0")
log "Active LinkedIn replies in DB (coverage target across fires): $ACTIVE_COUNT"

FEED_JSON="$LOG_DIR/stats-linkedin-comments-feed-$(date +%Y%m%d_%H%M%S).json"
SUMMARY_JSON=$(mktemp -t fazm-li-comments-summary.XXXXXX).json
SCRAPER_STDOUT=$(mktemp -t fazm-li-comments-scrape.XXXXXX).json

# 1. Acquire the bash linkedin-browser lock to serialize against
#    run-linkedin.sh / engage-linkedin.sh / dm-outreach-linkedin.sh /
#    engage-dm-replies.sh. Two CDP clients hammering the same DOM
#    corrupt each other's evaluate() calls, so the lock still matters
#    even though we no longer launch a second Chrome.
#
# 2. DELIBERATELY do NOT call ensure_browser_healthy "linkedin" here.
#    That helper SIGKILLs the linkedin-agent MCP Chrome and clears
#    SingletonLock/Cookie/Socket so a second Chrome can launch on the
#    same profile dir. With the 2026-05-08 cutover,
#    scrape_linkedin_comment_stats.py CDP-attaches to the running MCP
#    Chrome via _connect_to_running_or_launch (reads
#    DevToolsActivePort), opens a tab in the existing BrowserContext,
#    runs the harvest, then closes ONLY the tab. There is no second
#    Chrome to make room for, and killing the MCP would just be the
#    exact kill+reopen cadence that LinkedIn anti-bot flagged on
#    2026-05-06.
#
#    The Python helper still falls back to launch_persistent_context
#    if the MCP is genuinely cold (DevToolsActivePort missing or
#    socket dead). In that case the profile dir is free and no kill
#    is needed there either; the helper just clears stale Singleton*
#    files and launches.
acquire_lock "linkedin-browser" 1800

# 2. Run the headed-Chromium scraper.
log "Launching headed Chromium scraper..."
SCRAPER_RC=0
set +e
SOCIAL_AUTOPOSTER_LINKEDIN_COMMENT_STATS=1 \
/opt/homebrew/bin/gtimeout "$SCRAPER_TIMEOUT_SEC" \
    "$SCRAPER_PYTHON_BIN" "$REPO_DIR/scripts/scrape_linkedin_comment_stats.py" \
        --out "$FEED_JSON" \
        --max-scrolls "$MAX_SCROLLS" \
    > "$SCRAPER_STDOUT" 2>&1
SCRAPER_RC=$?
set -e

# Always release the browser lock; updater is DB-only and doesn't need it.
release_lock "linkedin-browser"
rm -f "$HOME/.claude/linkedin-agent-lock.json"

# Echo scraper output to log.
cat "$SCRAPER_STDOUT" | tee -a "$LOG_FILE"

if [ "$SCRAPER_RC" -ne 0 ]; then
    log "ERROR: scraper exited rc=$SCRAPER_RC"
    SCRAPER_ERROR=$("$PYTHON_BIN" -c "
import json, sys
try:
    obj = json.load(open('$SCRAPER_STDOUT'))
    print(obj.get('error', 'unknown'))
except Exception:
    print('parse_failed')
" 2>/dev/null || echo "unknown")
    log "scraper error code: $SCRAPER_ERROR"

    # Honor the SESSION_INVALID convention by surfacing a token both
    # humans and the watchdog can grep on.
    if [ "$SCRAPER_ERROR" = "session_invalid" ] \
       || [ "$SCRAPER_ERROR" = "captcha_or_checkpoint" ]; then
        log "SESSION_INVALID — abort run, do not retry."
    fi

    # Don't run the updater if we don't have a feed.
    if [ ! -s "$FEED_JSON" ]; then
        log "No feed JSON produced; skipping updater."
        rm -f "$SCRAPER_STDOUT" "$SUMMARY_JSON"
        RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
        "$PYTHON_BIN" "$REPO_DIR/scripts/log_run.py" \
            --script "stats_linkedin_comments" \
            --posted 0 --skipped 0 --failed 1 \
            --cost "0.0000" --elapsed "$RUN_ELAPSED" \
            2>/dev/null || true
        log "=== LinkedIn comment stats failed: $(date) ==="
        exit 1
    fi
    log "Feed JSON exists despite rc=$SCRAPER_RC; running updater anyway."
fi

# 3. Apply to DB.
"$PYTHON_BIN" "$REPO_DIR/scripts/update_linkedin_comment_stats_from_feed.py" \
    --from-json "$FEED_JSON" \
    --summary   "$SUMMARY_JSON" \
    2>&1 | tee -a "$LOG_FILE" \
    || log "WARNING: updater exited with code $?"

# 4. Surface counters.
if [ -s "$SUMMARY_JSON" ]; then
    REFRESHED=$("$PYTHON_BIN" -c "import json; print(json.load(open('$SUMMARY_JSON')).get('refreshed', 0))" 2>/dev/null || echo 0)
    NOT_FOUND=$("$PYTHON_BIN" -c "import json; print(json.load(open('$SUMMARY_JSON')).get('not_found', 0))" 2>/dev/null || echo 0)
    log "Comment stats refresh: refreshed=$REFRESHED unmatched=$NOT_FOUND"
else
    log "No summary sidecar produced; assuming zeroed run."
    REFRESHED=0
    NOT_FOUND=0
fi

# 5. Log run to persistent monitor.
RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
"$PYTHON_BIN" "$REPO_DIR/scripts/log_run.py" --script "stats_linkedin_comments" \
    --posted "$REFRESHED" --skipped 0 --failed 0 \
    --cost "0.0000" --elapsed "$RUN_ELAPSED" \
    2>/dev/null || true

# Cleanup.
rm -f "$SUMMARY_JSON" "$SCRAPER_STDOUT"
find "$LOG_DIR" -name "stats-linkedin-comments-*.log"  -mtime +14 -delete 2>/dev/null || true
find "$LOG_DIR" -name "stats-linkedin-comments-feed-*.json" -mtime +7 -delete 2>/dev/null || true

log "=== LinkedIn comment stats complete: $(date) ==="
