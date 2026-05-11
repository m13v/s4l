#!/usr/bin/env bash
# stats-linkedin.sh — Unified LinkedIn stats refresh.
#
# Single pipeline that mirrors the Twitter logic shape: one source of truth
# (LinkedIn's /in/me/recent-activity/comments/ activity tab), one DB write
# path across all LinkedIn engagement rows. Replaces:
#   - The deprecated skill/stats.sh Step 4 (which called the now-stubbed
#     scripts/scrape_linkedin_stats_browser.py and silently no-op'd).
#   - The standalone skill/stats-linkedin-comments.sh, which only updated
#     the legacy `replies` table. This script kept calling that updater
#     too so we don't lose the ~173 replies-table rows.
#
# What this does, in order:
#   1. Acquire the linkedin-browser lock (serializes against run-linkedin.sh
#      / engage-linkedin.sh / dm-outreach-linkedin.sh / engage-dm-replies.sh).
#   2. Run scripts/scrape_linkedin_comment_stats.py ONCE. It CDP-attaches
#      to the running linkedin-agent MCP Chrome (no second Chrome spawned;
#      the kill+reopen cadence flagged on 2026-05-06 is gone), opens a
#      tab to /in/me/recent-activity/comments/, harvests per-comment
#      impressions / reactions / replies into a single JSON feed.
#   3. Run scripts/update_linkedin_comment_stats_from_feed.py — writes
#      the feed into the legacy `replies` table (older pipeline shape;
#      ~173 rows total).
#   4. Run scripts/update_linkedin_stats_from_feed.py — writes the feed
#      into the `posts` table for rows whose `our_url` carries a
#      `?commentUrn=` (the 97 pre-existing rows that already had it
#      from reply_to_comment + all new rows posted 2026-05-11 onward
#      after linkedin_api.py:comment_on_post was patched to embed it).
#   5. Release the browser lock; updaters are DB-only.
#
# Why one scrape, two writers:
#   The same activity feed contains every comment we made. Scraping twice
#   would double LinkedIn fingerprint risk for zero new data. One scrape
#   + two updaters keeps the URN→stats mapping consistent across both
#   tables and stays well inside the anti-bot envelope.
#
# Bot-detection prevention (carries over the carve-out from
# stats-linkedin-comments.sh; do NOT loosen):
#   - ONE page.goto per fire to /in/me/recent-activity/comments/.
#   - ONE page.evaluate; scroll + harvest happen inside the same JS run.
#   - No clicks, no permalink hops, no "Show more", no Voyager API.
#   - SESSION_INVALID detection: redirect to /login or /checkpoint -> stop.
#
# Cadence: every 4-6h. LinkedIn updates impressions in near real time but
# per-fire fingerprint risk is non-zero; do not run hotter.

set -euo pipefail

source "$(dirname "$0")/lock.sh"

# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
PYTHON_BIN="/opt/homebrew/bin/python3"
# /usr/bin/python3 is the only interpreter with playwright installed; this
# matches engage-dm-replies.sh's call to linkedin_browser.py. DB scripts
# stay on homebrew python where psycopg2 is installed.
SCRAPER_PYTHON_BIN="/usr/bin/python3"

# Tunables.
MAX_SCROLLS=40           # in-page scrolls
SCRAPER_TIMEOUT_SEC=480  # whole scraper run cap (~2.5min scroll + overhead)

if [ -z "${DATABASE_URL:-}" ]; then
    echo "ERROR: DATABASE_URL not set in ~/social-autoposter/.env"
    exit 1
fi

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/stats-linkedin-$(date +%Y-%m-%d_%H%M%S).log"
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

RUN_START=$(date +%s)
log "=== LinkedIn Stats Run (unified): $(date) ==="
log "mode: python (no LLM); MAX_SCROLLS=$MAX_SCROLLS; timeout=${SCRAPER_TIMEOUT_SEC}s"

# Coverage hint across both tables.
COVERAGE=$("$PYTHON_BIN" -c "
import sys; sys.path.insert(0, '$REPO_DIR/scripts')
import db as dbmod; dbmod.load_env(); db = dbmod.get_conn()
cur = db.execute(\"\"\"SELECT
    (SELECT COUNT(*) FROM replies
       WHERE platform='linkedin' AND status IN ('replied','posted')
         AND our_reply_url IS NOT NULL AND our_reply_url ~ 'commentUrn') AS replies_n,
    (SELECT COUNT(*) FROM posts
       WHERE platform='linkedin' AND status IN ('active','removed')
         AND our_url IS NOT NULL AND our_url ILIKE '%commentUrn%')        AS posts_n
\"\"\")
r = cur.fetchone(); print(f\"replies={r['replies_n']} posts={r['posts_n']}\")
" 2>/dev/null || echo "replies=? posts=?")
log "Active LinkedIn comments addressable by this feed: $COVERAGE"

FEED_JSON="$LOG_DIR/stats-linkedin-feed-$(date +%Y%m%d_%H%M%S).json"
REPLIES_SUMMARY_JSON=$(mktemp -t fazm-li-replies-summary.XXXXXX).json
POSTS_SUMMARY_JSON=$(mktemp -t fazm-li-posts-summary.XXXXXX).json
SCRAPER_STDOUT=$(mktemp -t fazm-li-scrape.XXXXXX).json

# 1. Acquire the linkedin-browser lock. Two CDP clients hammering the same
#    DOM corrupt each other's evaluate() calls, so the lock matters even
#    though we no longer launch a second Chrome.
#
#    DELIBERATELY do NOT call ensure_browser_healthy "linkedin" — that
#    helper SIGKILLs the linkedin-agent MCP and clears Singleton lockfiles
#    so a second Chrome can launch on the same profile. With the 2026-05-08
#    cutover, scrape_linkedin_comment_stats.py CDP-attaches to the running
#    MCP Chrome instead, so there's no second Chrome to make room for.
#    Killing the MCP would just be the exact kill+reopen cadence LinkedIn
#    anti-bot flagged on 2026-05-06.
acquire_lock "linkedin-browser" 1800

# 2. Run the headed-Chromium scraper (single scrape, shared between writers).
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

# Always release the browser lock; updaters are DB-only.
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

    if [ "$SCRAPER_ERROR" = "session_invalid" ] \
       || [ "$SCRAPER_ERROR" = "captcha_or_checkpoint" ]; then
        log "SESSION_INVALID — abort run, do not retry."
    fi

    if [ ! -s "$FEED_JSON" ]; then
        log "No feed JSON produced; skipping both updaters."
        rm -f "$SCRAPER_STDOUT" "$REPLIES_SUMMARY_JSON" "$POSTS_SUMMARY_JSON"
        RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
        "$PYTHON_BIN" "$REPO_DIR/scripts/log_run.py" \
            --script "stats_linkedin" \
            --posted 0 --skipped 0 --failed 1 \
            --cost "0.0000" --elapsed "$RUN_ELAPSED" \
            2>/dev/null || true
        log "=== LinkedIn stats failed: $(date) ==="
        exit 1
    fi
    log "Feed JSON exists despite rc=$SCRAPER_RC; running updaters anyway."
fi

# 3. Apply to `replies` (legacy table).
log "Writer 1/2: replies table..."
"$PYTHON_BIN" "$REPO_DIR/scripts/update_linkedin_comment_stats_from_feed.py" \
    --from-json "$FEED_JSON" \
    --summary   "$REPLIES_SUMMARY_JSON" \
    2>&1 | tee -a "$LOG_FILE" \
    || log "WARNING: replies updater exited with code $?"

# 4. Apply to `posts` (Twitter-parity table).
log "Writer 2/2: posts table..."
"$PYTHON_BIN" "$REPO_DIR/scripts/update_linkedin_stats_from_feed.py" \
    --from-json "$FEED_JSON" \
    --summary   "$POSTS_SUMMARY_JSON" \
    2>&1 | tee -a "$LOG_FILE" \
    || log "WARNING: posts updater exited with code $?"

# 5. Surface combined counters.
REFRESHED_REPLIES=0
REFRESHED_POSTS=0
NOT_FOUND=0
if [ -s "$REPLIES_SUMMARY_JSON" ]; then
    REFRESHED_REPLIES=$("$PYTHON_BIN" -c "import json; print(json.load(open('$REPLIES_SUMMARY_JSON')).get('refreshed', 0))" 2>/dev/null || echo 0)
fi
if [ -s "$POSTS_SUMMARY_JSON" ]; then
    REFRESHED_POSTS=$("$PYTHON_BIN" -c "import json; print(json.load(open('$POSTS_SUMMARY_JSON')).get('refreshed', 0))" 2>/dev/null || echo 0)
    NOT_FOUND=$("$PYTHON_BIN" -c "import json; print(json.load(open('$POSTS_SUMMARY_JSON')).get('not_found', 0))" 2>/dev/null || echo 0)
fi
TOTAL_REFRESHED=$(( REFRESHED_REPLIES + REFRESHED_POSTS ))
log "Comment stats refresh: replies=$REFRESHED_REPLIES posts=$REFRESHED_POSTS total=$TOTAL_REFRESHED unmatched=$NOT_FOUND"

# 6. Log run to persistent monitor.
RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
"$PYTHON_BIN" "$REPO_DIR/scripts/log_run.py" --script "stats_linkedin" \
    --posted "$TOTAL_REFRESHED" --skipped 0 --failed 0 \
    --cost "0.0000" --elapsed "$RUN_ELAPSED" \
    2>/dev/null || true

# Cleanup.
rm -f "$REPLIES_SUMMARY_JSON" "$POSTS_SUMMARY_JSON" "$SCRAPER_STDOUT"
find "$LOG_DIR" -name "stats-linkedin-*.log"  -mtime +14 -delete 2>/dev/null || true
find "$LOG_DIR" -name "stats-linkedin-feed-*.json" -mtime +7 -delete 2>/dev/null || true

log "=== LinkedIn stats complete: $(date) ==="
