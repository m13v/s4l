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
#      to the linkedin-harness Chrome on port 9556 (2026-05-26 migration:
#      replaced the legacy ps-discovery of linkedin-agent MCP. The harness
#      is multi-client safe so no kill+reopen, no Singleton fight. The
#      LINKEDIN_CDP_URL env var exported by skill/lib/linkedin-backend.sh
#      tells linkedin_browser.py to attach via CDP directly), opens a
#      tab to /in/me/recent-activity/comments/, harvests per-comment
#      impressions / reactions / replies into a single JSON feed.
#   3. Run scripts/update_linkedin_stats_from_feed.py — writes the feed
#      into the `posts` table for rows whose `our_url` carries a
#      `?commentUrn=` (the 97 pre-existing rows from reply_to_comment +
#      the 225 rows migrated from the legacy `replies` table on
#      2026-05-11 + every new row posted 2026-05-11 onward after
#      linkedin_api.py:comment_on_post was patched to embed it).
#   4. Release the browser lock; the updater is DB-only.
#
# History note (2026-05-11): there used to be a second writer that wrote
# the same feed into the legacy `replies` table (~257 LinkedIn rows). On
# 2026-05-11 those rows were migrated into `posts` (Twitter-parity) and
# the source rows marked status='migrated'. The replies-table writer
# (scripts/update_linkedin_comment_stats_from_feed.py) and its standalone
# entrypoint (skill/stats-linkedin-comments.sh) were retired in the same
# pass. If you see references to them anywhere, they are stale and
# should be removed.
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

# LinkedIn killswitch (2026-05-27): refuse to run if a prior fire detected
# session compromise (http_999, authwall, throttle, li_at cleared).
# State: ~/.claude/social-autoposter/linkedin.killswitch
# Clear: python3 ~/social-autoposter/scripts/linkedin_killswitch.py clear
if [ -f "$HOME/.claude/social-autoposter/linkedin.killswitch" ]; then
    echo "[$(date +%H:%M:%S)] LINKEDIN_KILLSWITCH active. Aborting LinkedIn pipeline."
    echo "  Re-auth LinkedIn in harness Chrome, then: python3 ~/social-autoposter/scripts/linkedin_killswitch.py clear"
    exit 0
fi

source "$(dirname "$0")/lock.sh"
# 2026-05-26 harness migration: linkedin-backend.sh exports LINKEDIN_CDP_URL
# (http://127.0.0.1:9556) and exposes ensure_linkedin_browser_for_backend
# which probes + launches the linkedin-harness Chrome idempotently. The
# scraper picks up LINKEDIN_CDP_URL automatically via linkedin_browser.py's
# harness-cdp fast-path.
source "$(dirname "$0")/lib/linkedin-backend.sh"

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
MAX_SCROLLS=400           # 2026-05-28 set to 400 per user direction; natural stagnant>=8 bail should fire well before this (~tick 150). Safety ceiling, not target. Previous: 300 (auto-commit) <- 1000 (runaway 2026-05-27).
SCRAPER_TIMEOUT_SEC=900   # 15min outer gtimeout. Inner JS deadline now defaults to 10min via SAPS_SCRAPER_DEADLINE_MS; the 15min outer is a 5min margin for cdp_attach + page.goto + the JS deadline + finalize().

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/stats-linkedin-$(date +%Y-%m-%d_%H%M%S).log"
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

RUN_START=$(date +%s)
log "=== LinkedIn Stats Run (unified): $(date) ==="
log "mode: python (no LLM); MAX_SCROLLS=$MAX_SCROLLS; timeout=${SCRAPER_TIMEOUT_SEC}s"

# Coverage hint. Reads via the s4l.ai HTTP API (no DATABASE_URL needed); the
# linkedin-engagement-comments GET returns every addressable row, so we just
# count them. Purely informational; never blocks the run.
COVERAGE=$("$PYTHON_BIN" -c "
import sys; sys.path.insert(0, '$REPO_DIR/scripts')
from http_api import api_get
resp = api_get('/api/v1/linkedin-engagement-comments')
rows = (resp.get('data') or {}).get('rows') or []
print(f'posts={len(rows)}')
" 2>/dev/null || echo "posts=?")
log "Active LinkedIn comments addressable by this feed: $COVERAGE"

FEED_JSON="$LOG_DIR/stats-linkedin-feed-$(date +%Y%m%d_%H%M%S).json"
POSTS_SUMMARY_JSON=$(mktemp -t fazm-li-posts-summary.XXXXXX).json
SCRAPER_STDOUT=$(mktemp -t fazm-li-scrape.XXXXXX).json

# Forensic-bundle directory. The scraper writes screenshots, html, cookies,
# console.jsonl / navigation.jsonl / network.jsonl + a Python traceback on
# any non-ok return path here, then tar.gz's it and prints the path to
# stderr as `[scrape_linkedin] debug_bundle=<tarball>`. We grep that out of
# the captured stderr below.
#
# On session_invalid / captcha_or_checkpoint specifically, we promote the
# tarball to skill/logs/linkedin-debug-failures/ — that subdir is NOT swept
# by the 14-day retention sweep at the end of this script. Permanent
# archive so the next failure can be diff'd byte-for-byte against the last
# known good/bad bundle.
DEBUG_BUNDLE_BASE="$LOG_DIR/linkedin-debug"
DEBUG_BUNDLE_DIR="$DEBUG_BUNDLE_BASE/$(date +%Y%m%d_%H%M%S)"
DEBUG_FAILURES_DIR="$LOG_DIR/linkedin-debug-failures"
mkdir -p "$DEBUG_BUNDLE_BASE" "$DEBUG_FAILURES_DIR"

# 1. Acquire the linkedin-browser lock. Two CDP clients hammering the same
#    DOM corrupt each other's evaluate() calls, so the lock matters even
#    though we no longer launch a second Chrome.
#
#    DELIBERATELY do NOT call ensure_browser_healthy "linkedin" — that
#    helper SIGKILLs the linkedin-agent MCP and clears Singleton lockfiles
#    so a second Chrome can launch on the same profile. With the 2026-05-26
#    harness cutover, scrape_linkedin_comment_stats.py CDP-attaches to the
#    linkedin-harness Chrome (port 9556) which is multi-client safe.
acquire_lock "linkedin-browser" 1800

# Probe + launch harness Chrome idempotently if it's down. Safe to call under
# the linkedin-browser lock; harness CDP supports concurrent clients on the
# same profile so no SingletonLock fight.
ensure_linkedin_browser_for_backend

# 2. Run the headed-Chromium scraper (single scrape, shared between writers).
log "Launching headed Chromium scraper..."
log "Debug bundle dir (pre-tar): $DEBUG_BUNDLE_DIR"
SCRAPER_RC=0
set +e
SOCIAL_AUTOPOSTER_LINKEDIN_COMMENT_STATS=1 \
/opt/homebrew/bin/gtimeout "$SCRAPER_TIMEOUT_SEC" \
    "$SCRAPER_PYTHON_BIN" "$REPO_DIR/scripts/scrape_linkedin_comment_stats.py" \
        --out "$FEED_JSON" \
        --max-scrolls "$MAX_SCROLLS" \
        --debug-dir "$DEBUG_BUNDLE_DIR" \
    > "$SCRAPER_STDOUT" 2>&1
SCRAPER_RC=$?
set -e

# Always release the browser lock; updaters are DB-only.
release_lock "linkedin-browser"
# 2026-05-26 harness migration: the linkedin-agent JSON lockfile still gets
# written by linkedin_browser._acquire_browser_lock for serialization between
# concurrent Python invocations; sweep it on the way out so it doesn't
# accumulate stale entries.
rm -f "$HOME/.claude/linkedin-agent-lock.json"

# Echo scraper output to log.
cat "$SCRAPER_STDOUT" | tee -a "$LOG_FILE"

# Surface the debug-bundle tarball path. The scraper writes a single
# `[scrape_linkedin] debug_bundle=<path>` line to stderr right before exit;
# grep it back out so it's visible in the orchestrator log without needing
# to unpack the tarball.
DEBUG_TARBALL=$(grep -m1 -E '^\[scrape_linkedin\] debug_bundle=' "$SCRAPER_STDOUT" | sed -E 's/^\[scrape_linkedin\] debug_bundle=//')
if [ -n "$DEBUG_TARBALL" ] && [ -f "$DEBUG_TARBALL" ]; then
    log "Debug bundle: $DEBUG_TARBALL"
else
    log "Debug bundle: <missing — scraper did not emit debug_bundle marker>"
fi

# Also surface the linkedin_browser mode line — this is the #1 signal for
# answering "did we cdp_attach or cold_launch?" after a failure.
BROWSER_MODE_LINE=$(grep -m1 -E '^\[linkedin_browser\] mode=' "$SCRAPER_STDOUT" || true)
if [ -n "$BROWSER_MODE_LINE" ]; then
    log "Browser mode: $BROWSER_MODE_LINE"
else
    log "Browser mode: <missing — _connect_to_running_or_launch never logged>"
fi

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

    # Permanent archive of session_invalid / captcha tarballs. We never
    # want to wake up to another 14-line "session_invalid" log file with
    # no way to forensically inspect the DOM that triggered it. Keep these
    # forever (or until the user manually cleans the dir).
    if [ "$SCRAPER_ERROR" = "session_invalid" ] \
       || [ "$SCRAPER_ERROR" = "captcha_or_checkpoint" ]; then
        log "SESSION_INVALID — abort run, do not retry."
        if [ -n "$DEBUG_TARBALL" ] && [ -f "$DEBUG_TARBALL" ]; then
            FAILURE_COPY="$DEBUG_FAILURES_DIR/$(basename "$DEBUG_TARBALL" .tar.gz)__${SCRAPER_ERROR}.tar.gz"
            cp -p "$DEBUG_TARBALL" "$FAILURE_COPY" 2>/dev/null \
                && log "Archived failure bundle: $FAILURE_COPY" \
                || log "WARN: failed to archive failure bundle to $FAILURE_COPY"
        else
            log "WARN: no debug tarball available to archive for $SCRAPER_ERROR"
        fi
    fi

    if [ ! -s "$FEED_JSON" ]; then
        log "No feed JSON produced; skipping updater."
        rm -f "$SCRAPER_STDOUT" "$POSTS_SUMMARY_JSON"
        RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
        "$PYTHON_BIN" "$REPO_DIR/scripts/log_run.py" \
            --script "stats_linkedin" \
            --posted 0 --skipped 0 --failed 1 \
            --cost "0.0000" --elapsed "$RUN_ELAPSED" \
            2>/dev/null || true
        log "=== LinkedIn stats failed: $(date) ==="
        exit 1
    fi
    log "Feed JSON exists despite rc=$SCRAPER_RC; running updater anyway."
fi

# 3. Apply to `posts` (Twitter-parity table; sole stats target).
log "Writer: posts table..."
"$PYTHON_BIN" "$REPO_DIR/scripts/update_linkedin_stats_from_feed.py" \
    --from-json "$FEED_JSON" \
    --summary   "$POSTS_SUMMARY_JSON" \
    2>&1 | tee -a "$LOG_FILE" \
    || log "WARNING: posts updater exited with code $?"

# 4. Surface counters.
REFRESHED_POSTS=0
NOT_FOUND=0
if [ -s "$POSTS_SUMMARY_JSON" ]; then
    REFRESHED_POSTS=$("$PYTHON_BIN" -c "import json; print(json.load(open('$POSTS_SUMMARY_JSON')).get('refreshed', 0))" 2>/dev/null || echo 0)
    NOT_FOUND=$("$PYTHON_BIN" -c "import json; print(json.load(open('$POSTS_SUMMARY_JSON')).get('not_found', 0))" 2>/dev/null || echo 0)
fi
log "Comment stats refresh: posts=$REFRESHED_POSTS total=$REFRESHED_POSTS unmatched=$NOT_FOUND"

# 5. Log run to persistent monitor.
RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
"$PYTHON_BIN" "$REPO_DIR/scripts/log_run.py" --script "stats_linkedin" \
    --posted "$REFRESHED_POSTS" --skipped 0 --failed 0 \
    --cost "0.0000" --elapsed "$RUN_ELAPSED" \
    2>/dev/null || true

# Cleanup.
rm -f "$POSTS_SUMMARY_JSON" "$SCRAPER_STDOUT"
find "$LOG_DIR" -name "stats-linkedin-*.log"  -mtime +14 -delete 2>/dev/null || true
find "$LOG_DIR" -name "stats-linkedin-feed-*.json" -mtime +7 -delete 2>/dev/null || true

# Debug-bundle retention. Two layers:
#   - linkedin-debug/<ts>/        : per-fire unpacked dirs, 14d
#   - linkedin-debug/<ts>.tar.gz  : per-fire tarballs, 14d
#   - linkedin-debug-failures/    : permanent archive of session_invalid /
#                                   captcha tarballs; NEVER swept here.
# Adjust the +14 numbers if disk pressure becomes an issue; do NOT add the
# failures dir to the find sweep without explicit user instruction.
find "$DEBUG_BUNDLE_BASE" -maxdepth 1 -type d -name "20*" -mtime +14 -exec rm -rf {} + 2>/dev/null || true
find "$DEBUG_BUNDLE_BASE" -maxdepth 1 -type f -name "20*.tar.gz" -mtime +14 -delete 2>/dev/null || true

log "=== LinkedIn stats complete: $(date) ==="
