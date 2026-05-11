#!/bin/bash
#
# Top-traffic page improvement pipeline.
#
# Every run:
#   1. pick_top_page.py  -> builds a brief for the single highest-pageview
#                           page on the project's domain in the last 24h
#                           (exit 2 = no traffic, skip cleanly)
#   2. improve_page.py   -> hands the brief to a Claude session running
#                           inside the product's website repo, captures the
#                           stream-json trace, records before/after metrics
#                           and diff in seo_page_improvements
#
# If invoked with no argument, iterates every project where
# landing_pages.improve_enabled == true in config.json.
#
# Usage:
#   ./seo/run_improve_pipeline.sh                 # all enabled projects
#   ./seo/run_improve_pipeline.sh PieLine         # one project
#   ./seo/run_improve_pipeline.sh --list-enabled  # preview selection

set -uo pipefail

# Cycle ID for cross-cycle cost accounting.
BATCH_ID="${BATCH_ID:-seoimp-$(date +%Y%m%d-%H%M%S)}"
export BATCH_ID
export SA_CYCLE_ID="$BATCH_ID"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG="$ROOT_DIR/config.json"
PICK="python3 $SCRIPT_DIR/pick_top_page.py"
IMPROVE="python3 $SCRIPT_DIR/improve_page.py"

mkdir -p "$SCRIPT_DIR/.locks/improve" "$SCRIPT_DIR/logs"

RUN_START=$(date +%s)
trap '__e=$?; python3 "$SCRIPT_DIR/log_seo_run.py" --script "seo_improve" --since "$RUN_START" --failed "$__e" --elapsed "$(( $(date +%s) - RUN_START ))" >/dev/null 2>&1 || true' EXIT

# Load .env for DATABASE_URL etc.
[ -f "$ROOT_DIR/.env" ] && set -a && source "$ROOT_DIR/.env" && set +a

# Pull PostHog personal key from keychain if not already in env
if [ -z "${POSTHOG_PERSONAL_API_KEY:-}" ]; then
    POSTHOG_PERSONAL_API_KEY=$(security find-generic-password -s "PostHog-Personal-API-Key-m13v" -w 2>/dev/null || true)
    export POSTHOG_PERSONAL_API_KEY
fi

_enabled_products() {
    python3 -c "
import json
with open('$CONFIG') as f:
    c = json.load(f)
for p in c.get('projects', []):
    lp = p.get('landing_pages') or {}
    # Also require weight > 0 so paused projects (Clone, tenxats,
    # macOS Session Replay as of 2026-05-08) don't get SEO improve
    # work. Posting + Moltbook + SERP/GSC + top-pages + top-posts
    # all gate on weight > 0; this brings improve into line.
    if lp.get('improve_enabled') and (p.get('weight') or 0) > 0:
        print(p.get('name'))
"
}

_run_one() {
    local PRODUCT="$1"
    local LOWER
    LOWER=$(echo "$PRODUCT" | tr '[:upper:]' '[:lower:]')
    local LOCK_FILE="$SCRIPT_DIR/.locks/improve/${LOWER}.lock"
    local LOG_DIR="$SCRIPT_DIR/logs/${LOWER}/improve"
    mkdir -p "$LOG_DIR"
    local TS
    TS=$(date -u +%Y%m%d-%H%M%S)
    local LOG_FILE="$LOG_DIR/${TS}.log"

    log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE" >&2; }

    log "=== improve run: $PRODUCT ==="

    if [ -f "$LOCK_FILE" ]; then
        local LOCK_AGE
        LOCK_AGE=$(( $(date +%s) - $(stat -f %m "$LOCK_FILE" 2>/dev/null || stat -c %Y "$LOCK_FILE" 2>/dev/null) ))
        if [ "$LOCK_AGE" -gt 3600 ]; then
            log "stale lock (${LOCK_AGE}s), removing"
            rm -f "$LOCK_FILE"
        else
            log "already running for $PRODUCT (lock age ${LOCK_AGE}s); skipping"
            return 0
        fi
    fi
    echo "$$" > "$LOCK_FILE"
    trap 'rm -f "$LOCK_FILE"' RETURN

    local BRIEF="$LOG_DIR/${TS}_brief.json"
    log "building brief -> $BRIEF"
    if ! $PICK --product "$PRODUCT" --out "$BRIEF" 2>>"$LOG_FILE"; then
        local code=$?
        if [ "$code" = "2" ]; then
            log "no 24h traffic; skipping"
            rm -f "$LOCK_FILE"
            trap - RETURN
            return 0
        fi
        log "ERROR building brief (exit $code)"
        rm -f "$LOCK_FILE"
        trap - RETURN
        return "$code"
    fi

    log "handing brief to Claude..."
    $IMPROVE --brief "$BRIEF" 2>>"$LOG_FILE" | tee -a "$LOG_FILE"
    local rc=${PIPESTATUS[0]}

    log "=== done (exit $rc) ==="
    rm -f "$LOCK_FILE"
    trap - RETURN
    return "$rc"
}

# API-quota markers. When a per-product log contains one of these, every
# remaining product would fail the same way and burn credits for nothing —
# short-circuit the outer loop instead. Matches the roundup / top_pages
# pipelines so behavior stays consistent across SEO jobs.
#
# We grep BOTH the per-product .log (shell wrapper output) AND the matching
# Claude session _stream.jsonl, because the actual rate-limit signal from
# Claude lives only in the JSONL stream as `"api_error_status":429` /
# `"You've hit your limit"` / a rate_limit_event with `"status":"rejected"`.
# Pre-2026-04-29 this only grepped the .log and let the loop walk into 9
# sequential failures.
#
# IMPORTANT 2026-04-29: do NOT match bare `rate_limit_event` or `rate.?limit`.
# Claude streams a `rate_limit_event` object on EVERY successful turn as a
# heartbeat (e.g. `"status":"allowed"` or `"allowed_warning"`), so those
# patterns trip on every healthy run and short-circuit after product #1.
# Only the `"status":"rejected"` variant signals an actual rejection.
QUOTA_MARKERS='monthly usage limit|429 Too Many|insufficient_quota|"api_error_status":429|hit your limit|"status":"rejected"'

_product_log_latest() {
    # Path to the most-recent per-product log under $SCRIPT_DIR/logs/<lower>/improve.
    local lower
    lower=$(echo "$1" | tr '[:upper:]' '[:lower:]')
    ls -t "$SCRIPT_DIR/logs/${lower}/improve/"*.log 2>/dev/null | head -1
}

_product_stream_latest() {
    # Path to the most-recent Claude session JSONL for this product. The
    # improve runner names these <ts>_<slug>_stream.jsonl alongside the .log.
    local lower
    lower=$(echo "$1" | tr '[:upper:]' '[:lower:]')
    ls -t "$SCRIPT_DIR/logs/${lower}/improve/"*_stream.jsonl 2>/dev/null | head -1
}

case "${1:-}" in
    --list-enabled)
        _enabled_products
        exit 0
        ;;
    "")
        PRODUCTS=$(_enabled_products)
        if [ -z "$PRODUCTS" ]; then
            echo "no projects have landing_pages.improve_enabled=true in $CONFIG" >&2
            exit 0
        fi
        overall=0
        quota_hit=0
        while IFS= read -r p; do
            [ -z "$p" ] && continue
            if [ "$quota_hit" = "1" ]; then
                echo "[$(date '+%H:%M:%S')] skipping $p — quota hit earlier this tick" >&2
                continue
            fi
            _run_one "$p" || overall=$?
            # Quota short-circuit: peek at the latest per-product .log AND the
            # Claude session _stream.jsonl. Claude rate-limit signals only
            # land in the JSONL stream, never in the shell .log, so checking
            # only the .log lets the loop walk into N sequential failures.
            plog=$(_product_log_latest "$p")
            pstream=$(_product_stream_latest "$p")
            quota_match=""
            if [ -n "$plog" ] && grep -qiE "$QUOTA_MARKERS" "$plog" 2>/dev/null; then
                quota_match="$plog"
            elif [ -n "$pstream" ] && grep -qiE "$QUOTA_MARKERS" "$pstream" 2>/dev/null; then
                quota_match="$pstream"
            fi
            if [ -n "$quota_match" ]; then
                quota_hit=1
                echo "[$(date '+%H:%M:%S')] !! $p hit API quota ($(basename "$quota_match")) — halting tick" >&2
            fi
        done <<< "$PRODUCTS"
        exit "$overall"
        ;;
    *)
        _run_one "$1"
        exit $?
        ;;
esac
