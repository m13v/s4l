#!/usr/bin/env bash
# audit.sh — Post audit pipeline.
#
# Per-platform mode (preferred, driven by launchd via per-platform wrappers):
#   --platform reddit    Reddit API audit via update_stats.py --reddit-only
#   --platform moltbook  Moltbook API audit via update_stats.py --moltbook-only
#   --platform twitter   Twitter API audit via update_stats.py --twitter-audit
#   --platform linkedin  Retired 2026-04-17 (flagged CDP pattern). Engagement
#                        stats now collected via stats.sh Step 4 (linkedin-agent
#                        MCP, headed Chrome). Branch kept as no-op so the
#                        audit-linkedin launchd job doesn't error.
#
# Every run also executes the orphan/summary step at the end (DB-only, cheap).
# With no --platform, runs all four sequentially (legacy manual path).


set -uo pipefail

# Parse args.
PLATFORM=""
while [ $# -gt 0 ]; do
    case "$1" in
        --platform)    PLATFORM="${2:-}"; shift 2 ;;
        --platform=*)  PLATFORM="${1#--platform=}"; shift ;;
        *)             shift ;;
    esac
done

case "$PLATFORM" in
    ""|reddit|twitter|linkedin|moltbook) ;;
    *)
        echo "audit.sh: invalid --platform '$PLATFORM' (expected reddit, twitter, linkedin, or moltbook)" >&2
        exit 2
        ;;
esac

# Per-platform lock name so all four can run concurrently, but a second
# invocation of the same platform waits. Legacy no-platform run keeps the
# original "audit" lock name.
LOCK_NAME="audit${PLATFORM:+-$PLATFORM}"

# Browser-profile lock first (shared across pipelines using the same browser),
# then the pipeline-specific lock. moltbook has no shared browser profile.
#
# Reddit uses the unified Python lease (2026-05-10) — TTL-aware, auto-decays
# during Claude idle gaps so peer pipelines can use the profile. The MCP
# proxy heartbeats expires_at on every reddit-agent call. LinkedIn/Twitter
# still use the bash lock (no MCP-proxy heartbeat wiring yet).
source "$(dirname "$0")/lock.sh"
REPO_DIR_FOR_LOCK="$HOME/social-autoposter"
_release_reddit_lease() {
    timeout 3 python3 "$REPO_DIR_FOR_LOCK/scripts/reddit_browser_lock.py" release 2>/dev/null || true
}
case "${PLATFORM:-all}" in
    linkedin) acquire_lock "linkedin-browser" 3600 ;;
    reddit)
        python3 "$REPO_DIR_FOR_LOCK/scripts/reddit_browser_lock.py" acquire --timeout 3600 --ttl 90 2>&1 || \
            echo "WARNING: reddit_browser_lock.py acquire failed; proceeding without lease."
        trap '_release_reddit_lease; _sa_release_locks' EXIT INT TERM HUP
        ;;
    twitter|x) acquire_lock "twitter-browser" 3600 ;;
    moltbook) ;;
    all)
        acquire_lock "linkedin-browser" 3600
        python3 "$REPO_DIR_FOR_LOCK/scripts/reddit_browser_lock.py" acquire --timeout 3600 --ttl 90 2>&1 || \
            echo "WARNING: reddit_browser_lock.py acquire failed; proceeding without lease."
        trap '_release_reddit_lease; _sa_release_locks' EXIT INT TERM HUP
        acquire_lock "twitter-browser" 3600
        ;;
esac
acquire_lock "$LOCK_NAME" 3600

# Load secrets
# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
SKILL_FILE="$REPO_DIR/SKILL.md"
LOG_DIR="$REPO_DIR/skill/logs"

if [ -z "${DATABASE_URL:-}" ]; then
    echo "ERROR: DATABASE_URL not set in ~/social-autoposter/.env"
    exit 1
fi

mkdir -p "$LOG_DIR"
LOG_TAG="${PLATFORM:-all}"
LOG_FILE="$LOG_DIR/audit-${LOG_TAG}-$(date +%Y-%m-%d_%H%M%S).log"

log() { echo "[$(date +%H:%M:%S)] $*" >> "$LOG_FILE"; echo "[$(date +%H:%M:%S)] $*"; }

RUN_START=$(date +%s)
log "=== Audit Pipeline Run (${LOG_TAG}): $(date) ==="

# Decide which steps run for this invocation.
if [ -z "$PLATFORM" ]; then
    RUN_REDDIT=1; RUN_MOLTBOOK=1; RUN_TWITTER=1; RUN_LINKEDIN=1
else
    RUN_REDDIT=0; RUN_MOLTBOOK=0; RUN_TWITTER=0; RUN_LINKEDIN=0
    case "$PLATFORM" in
        reddit)   RUN_REDDIT=1 ;;
        moltbook) RUN_MOLTBOOK=1 ;;
        twitter)  RUN_TWITTER=1 ;;
        linkedin) RUN_LINKEDIN=1 ;;
    esac
fi

STEP1_EXIT=0
STEP2_EXIT=0
STEP3_EXIT=0

# ═══════════════════════════════════════════════════════
# Reddit API audit
# ═══════════════════════════════════════════════════════
if [ "$RUN_REDDIT" -eq 1 ]; then
    log "Reddit: API audit (update_stats.py --reddit-only)"
    if [ -z "$PLATFORM" ]; then
        # Legacy all-platform path uses the combined default pass which also
        # covers Moltbook + Twitter, so we don't duplicate them below.
        python3 "$REPO_DIR/scripts/update_stats.py" >> "$LOG_FILE" 2>&1
    else
        python3 "$REPO_DIR/scripts/update_stats.py" --reddit-only >> "$LOG_FILE" 2>&1
    fi
    STEP1_EXIT=$?
    if [ "$STEP1_EXIT" -ne 0 ]; then
        log "Reddit: FAILED (exit $STEP1_EXIT)"
    else
        log "Reddit: Done"
    fi
fi

# ═══════════════════════════════════════════════════════
# Moltbook API audit
# ═══════════════════════════════════════════════════════
# Skip in legacy mode — already covered by the combined pass above.
if [ "$RUN_MOLTBOOK" -eq 1 ] && [ -n "$PLATFORM" ]; then
    log "Moltbook: API audit (update_stats.py --moltbook-only)"
    python3 "$REPO_DIR/scripts/update_stats.py" --moltbook-only >> "$LOG_FILE" 2>&1
    MOLTBOOK_EXIT=$?
    if [ "$MOLTBOOK_EXIT" -ne 0 ]; then
        log "Moltbook: FAILED (exit $MOLTBOOK_EXIT)"
    else
        log "Moltbook: Done"
    fi
fi

# ═══════════════════════════════════════════════════════
# Twitter API audit (fxtwitter — no browser)
# ═══════════════════════════════════════════════════════
if [ "$RUN_TWITTER" -eq 1 ]; then
    TWITTER_COUNT=$(psql "$DATABASE_URL" -t -A -c "
        SELECT COUNT(*) FROM posts
        WHERE platform='twitter' AND status='active' AND our_url IS NOT NULL;" 2>/dev/null || echo "0")

    if [ "$TWITTER_COUNT" -gt 0 ]; then
        log "Twitter: API audit — $TWITTER_COUNT active tweets"
        python3 "$REPO_DIR/scripts/update_stats.py" --twitter-audit >> "$LOG_FILE" 2>&1
        STEP2_EXIT=$?
        if [ "$STEP2_EXIT" -ne 0 ]; then
            log "Twitter: FAILED (exit $STEP2_EXIT)"
        else
            log "Twitter: Done"
        fi
    else
        log "Twitter: SKIPPED — no active Twitter posts to audit"
    fi
fi

# ═══════════════════════════════════════════════════════
# LinkedIn audit — retired 2026-04-17 (flagged CDP fingerprint).
# Post-engagement stats are now collected in stats.sh Step 4 via the
# linkedin-agent MCP (headed Chrome). Deletion detection is not currently
# covered; if needed, extend stats.sh Step 4 to parse 404 / "This post
# isn't available" screens.
# ═══════════════════════════════════════════════════════
if [ "$RUN_LINKEDIN" -eq 1 ]; then
    log "LinkedIn: SKIPPED — CDP audit retired (see stats.sh Step 4 for engagement stats via MCP)"
fi

# ═══════════════════════════════════════════════════════
# Orphan / stale post detection + summary (DB-only, every run)
# ═══════════════════════════════════════════════════════
log "Orphan/stale detection"

ORPHAN_REPORT=$(psql "$DATABASE_URL" -t -A -c "
    SELECT platform, status, COUNT(*)
    FROM posts
    WHERE status NOT IN ('active', 'deleted', 'removed')
    GROUP BY platform, status
    ORDER BY platform, status;" 2>/dev/null || echo "")

BROKEN_URL_COUNT=$(psql "$DATABASE_URL" -t -A -c "
    SELECT COUNT(*)
    FROM posts
    WHERE status = 'active'
      AND (our_url IS NULL OR our_url = '' OR our_url NOT LIKE 'http%');" 2>/dev/null || echo "0")

if [ -n "$ORPHAN_REPORT" ]; then
    log "WARNING: Posts with non-standard status:"
    echo "$ORPHAN_REPORT" | while IFS='|' read -r plat stat cnt; do
        log "  $plat $stat: $cnt"
    done
fi
if [ "$BROKEN_URL_COUNT" -gt 0 ]; then
    log "WARNING: $BROKEN_URL_COUNT active posts with missing/invalid our_url"
fi
if [ -z "$ORPHAN_REPORT" ] && [ "$BROKEN_URL_COUNT" = "0" ]; then
    log "Orphan/stale: Clean (no orphans, no broken URLs)"
fi

log "Summary"

ACTIVE=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM posts WHERE status='active';" 2>/dev/null || echo "?")
DELETED=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM posts WHERE status='deleted';" 2>/dev/null || echo "?")
REMOVED=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM posts WHERE status='removed';" 2>/dev/null || echo "?")

log "Post status: active=$ACTIVE deleted=$DELETED removed=$REMOVED"

# Log run to persistent monitor.
RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
AUDIT_FAILED=$(( (STEP1_EXIT != 0 ? 1 : 0) + (STEP2_EXIT != 0 ? 1 : 0) + (STEP3_EXIT != 0 ? 1 : 0) ))
SCRIPT_TAG="audit${PLATFORM:+-$PLATFORM}"
python3 "$REPO_DIR/scripts/log_run.py" --script "$SCRIPT_TAG" --posted "$ACTIVE" --skipped 0 --failed "$AUDIT_FAILED" --cost 0 --elapsed "$RUN_ELAPSED"

log "=== Audit Pipeline complete (${LOG_TAG}): $(date) ==="

# Clean up old logs (keep last 14 days) — covers both audit-all-* and audit-<platform>-*.
find "$LOG_DIR" -name "audit-*.log" -mtime +14 -delete 2>/dev/null || true
