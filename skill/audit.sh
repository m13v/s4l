#!/usr/bin/env bash
# audit.sh — Post audit pipeline.
#
# Per-platform mode (preferred, driven by launchd via per-platform wrappers):
#   --platform reddit    Reddit API audit via stats.py --reddit-only
#   --platform moltbook  Moltbook API audit via stats.py --moltbook-only
#   --platform twitter   Twitter API audit via stats.py --twitter-audit
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
    linkedin)
        acquire_lock "linkedin-browser" 3600
        # Join the cross-pipeline whole-run lock (one driver per 9556 Chrome).
        # rc=78 is the reserved skip code from _acquire_linkedin_pipeline_lock;
        # it must be converted to exit 0 HERE in the parent shell (an exit
        # inside the subshell cannot stop this script). NOTE: log() is not
        # defined yet at this point, hence plain echo.
        _LI_BOOT_RC=0
        ( source "$(dirname "$0")/lib/linkedin-backend.sh"; ensure_linkedin_browser_for_backend ) || _LI_BOOT_RC=$?
        if [ "$_LI_BOOT_RC" -eq 78 ]; then
            echo "[$(date +%H:%M:%S)] linkedin-pipeline lock: peer pipeline is driving the 9556 Chrome; skipping this fire"
            exit 0
        elif [ "$_LI_BOOT_RC" -ne 0 ]; then
            echo "[$(date +%H:%M:%S)] ERROR: linkedin browser bootstrap failed (rc=$_LI_BOOT_RC)"
            exit "$_LI_BOOT_RC"
        fi
        ;;
    reddit)
        python3 "$REPO_DIR_FOR_LOCK/scripts/reddit_browser_lock.py" acquire --timeout 3600 --ttl 90 2>&1 || \
            echo "WARNING: reddit_browser_lock.py acquire failed; proceeding without lease."
        trap '_release_reddit_lease; _sa_release_locks' EXIT INT TERM HUP
        ;;
    twitter|x) acquire_lock "twitter-browser" 3600 ;;
    moltbook) ;;
    all)
        acquire_lock "linkedin-browser" 3600
        # rc=78 skip (see the `linkedin` branch). A LinkedIn skip exits the
        # WHOLE all-platform fire, matching the pipeline lock's documented
        # skip-this-fire semantics; launchd re-fires on the next cadence.
        _LI_BOOT_RC=0
        ( source "$(dirname "$0")/lib/linkedin-backend.sh"; ensure_linkedin_browser_for_backend ) || _LI_BOOT_RC=$?
        if [ "$_LI_BOOT_RC" -eq 78 ]; then
            echo "[$(date +%H:%M:%S)] linkedin-pipeline lock: peer pipeline is driving the 9556 Chrome; skipping this fire (all-platform audit)"
            exit 0
        elif [ "$_LI_BOOT_RC" -ne 0 ]; then
            echo "[$(date +%H:%M:%S)] ERROR: linkedin browser bootstrap failed (rc=$_LI_BOOT_RC)"
            exit "$_LI_BOOT_RC"
        fi
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
# HTTP-only lane (2026-06-01): all reads go through the s4l.ai API via
# scripts/audit_helper.py. No DATABASE_URL, no psql, no fallback.
AUDIT_HELPER="$REPO_DIR/scripts/audit_helper.py"

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
    log "Reddit: API audit (stats.py --reddit-only)"
    if [ -z "$PLATFORM" ]; then
        # Legacy all-platform path uses the combined default pass which also
        # covers Moltbook + Twitter, so we don't duplicate them below.
        python3 "$REPO_DIR/scripts/stats.py" >> "$LOG_FILE" 2>&1
    else
        python3 "$REPO_DIR/scripts/stats.py" --reddit-only >> "$LOG_FILE" 2>&1
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
    log "Moltbook: API audit (stats.py --moltbook-only)"
    python3 "$REPO_DIR/scripts/stats.py" --moltbook-only >> "$LOG_FILE" 2>&1
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
    TWITTER_COUNT=$(python3 "$AUDIT_HELPER" twitter-active-count 2>/dev/null || echo "0")

    if [ "$TWITTER_COUNT" -gt 0 ]; then
        log "Twitter: API audit — $TWITTER_COUNT active tweets"
        python3 "$REPO_DIR/scripts/stats.py" --twitter-audit >> "$LOG_FILE" 2>&1
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

ORPHAN_REPORT=$(python3 "$AUDIT_HELPER" orphan-report 2>/dev/null || echo "")

BROKEN_URL_COUNT=$(python3 "$AUDIT_HELPER" broken-url-count 2>/dev/null || echo "0")

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

ACTIVE=$(python3 "$AUDIT_HELPER" status-count --status active 2>/dev/null || echo "?")
DELETED=$(python3 "$AUDIT_HELPER" status-count --status deleted 2>/dev/null || echo "?")
REMOVED=$(python3 "$AUDIT_HELPER" status-count --status removed 2>/dev/null || echo "?")

log "Post status: active=$ACTIVE deleted=$DELETED removed=$REMOVED"

# Log run to persistent monitor.
RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
AUDIT_FAILED=$(( (STEP1_EXIT != 0 ? 1 : 0) + (STEP2_EXIT != 0 ? 1 : 0) + (STEP3_EXIT != 0 ? 1 : 0) ))
SCRIPT_TAG="audit${PLATFORM:+-$PLATFORM}"

# Sum per-platform STATS_JSON lines emitted by stats.py into log_run.py flags so
# the dashboard Job History row shows real counters (scanned/checked/changed/
# replies-refreshed/removed) instead of the legacy posted=<active_count> mush.
# Each platform's stats.py print is followed by one `STATS_JSON: {...}` line;
# we read them all from $LOG_FILE and aggregate by kind. Missing keys default to
# 0 so the existing log_run.py flag surface stays unchanged.
read -r SCANNED CHECKED CHANGED DELETED ERRORS REPLIES_REFRESHED REPLIES_FRESH THREADS_SCANNED THREADS_WRITTEN <<<"$(
    python3 - "$LOG_FILE" <<'PY'
import json, sys, re
log_path = sys.argv[1]
agg = dict(scanned=0, checked=0, changed=0, deleted=0, errors=0,
           replies_refreshed=0, replies_fresh=0,
           threads_scanned=0, threads_written=0)
try:
    with open(log_path) as f:
        for line in f:
            m = re.search(r"STATS_JSON:\s*(\{.*\})\s*$", line)
            if not m:
                continue
            try:
                d = json.loads(m.group(1))
            except Exception:
                continue
            kind = d.get("kind")
            if kind == "posts":
                agg["scanned"] += int(d.get("total", 0) or 0)
                agg["checked"] += int(d.get("checked", 0) or 0)
                agg["changed"] += int(d.get("changed", 0) or 0)
                agg["deleted"] += int(d.get("deleted", 0) or 0) + int(d.get("removed", 0) or 0)
                agg["errors"]  += int(d.get("errors", 0) or 0)
            elif kind == "replies":
                agg["replies_refreshed"] += int(d.get("updated", 0) or 0)
                agg["replies_fresh"]     += int(d.get("fresh", 0) or 0)
            elif kind == "thread_snapshots":
                agg["threads_scanned"] += int(d.get("scanned", 0) or 0)
                agg["threads_written"] += int(d.get("written", 0) or 0)
except FileNotFoundError:
    pass
print(agg["scanned"], agg["checked"], agg["changed"], agg["deleted"],
      agg["errors"], agg["replies_refreshed"], agg["replies_fresh"],
      agg["threads_scanned"], agg["threads_written"])
PY
)"
SCANNED="${SCANNED:-0}"
CHECKED="${CHECKED:-0}"
CHANGED="${CHANGED:-0}"
DELETED="${DELETED:-0}"
ERRORS="${ERRORS:-0}"
REPLIES_REFRESHED="${REPLIES_REFRESHED:-0}"
REPLIES_FRESH="${REPLIES_FRESH:-0}"
THREADS_SCANNED="${THREADS_SCANNED:-0}"
THREADS_WRITTEN="${THREADS_WRITTEN:-0}"

# Roll API errors from stats.py into the dashboard `failed` pill alongside
# step-exit counts (same convention stats.sh uses).
AUDIT_FAILED=$(( AUDIT_FAILED + ERRORS ))

log "Per-run counters: scanned=$SCANNED checked=$CHECKED changed=$CHANGED removed=$DELETED errors=$ERRORS replies_refreshed=$REPLIES_REFRESHED replies_fresh=$REPLIES_FRESH thread_snapshots_written=$THREADS_WRITTEN"

python3 "$REPO_DIR/scripts/log_run.py" \
    --script "$SCRIPT_TAG" \
    --posted 0 \
    --skipped 0 \
    --failed "$AUDIT_FAILED" \
    --replies-refreshed "$REPLIES_REFRESHED" \
    --checked "$CHECKED" \
    --updated "$CHANGED" \
    --removed "$DELETED" \
    --scanned "$SCANNED" \
    --changed "$CHANGED" \
    --cost 0 \
    --elapsed "$RUN_ELAPSED"

log "=== Audit Pipeline complete (${LOG_TAG}): $(date) ==="

# Clean up old logs (keep last 14 days) — covers both audit-all-* and audit-<platform>-*.
find "$LOG_DIR" -name "audit-*.log" -mtime +14 -delete 2>/dev/null || true
