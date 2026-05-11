#!/bin/bash
#
# SEO pipeline cron orchestrator. Runs every 10 minutes.
#
# PARALLEL PER-PRODUCT: fans out one background lane per eligible product
# each tick. Each lane (a) refreshes DataForSEO keywords if stale (>24h),
# (b) runs run_serp_pipeline.sh for that product. Per-product locks in
# run_serp_pipeline.sh prevent double-work if a previous tick's lane is
# still processing.
#
# Previously this was a weighted-random single-product selector, which
# starved low-tier products. The parallel design gives every product its
# own lane on every tick, with a 0-30s jitter to smooth API / CPU bursts.
#
# DataForSEO per-task billing means batching wouldn't save money, so the
# jittered parallel pattern is cost-equivalent to the old serial one
# while eliminating the starvation problem.
#
# Products are eligible if landing_pages.repo in config.json exists on disk.
#
# Usage: cron_seo.sh (no args)
#

set -euo pipefail

# Cycle ID for cross-cycle cost accounting (matches the pattern in
# run-reddit-search.sh, etc.). Every claude session spawned via run_serp_pipeline.sh
# and generate_page.py inherits SA_CYCLE_ID via env, so log_claude_session.py
# stamps claude_sessions.cycle_id correctly.
BATCH_ID="${BATCH_ID:-seoserp-$(date +%Y%m%d-%H%M%S)}"
export BATCH_ID
export SA_CYCLE_ID="$BATCH_ID"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TICK_ID=$(date +%Y-%m-%d_%H%M%S)
TICK_LOG="$REPO_DIR/skill/logs/cron_seo-$TICK_ID.log"
REFRESH_DIR="$SCRIPT_DIR/.refresh_timestamps"

mkdir -p "$SCRIPT_DIR/.locks" "$REPO_DIR/skill/logs" "$REFRESH_DIR"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

RUN_START=$(date +%s)
trap '__e=$?; python3 "$SCRIPT_DIR/log_seo_run.py" --script "serp_seo" --since "$RUN_START" --failed "$__e" --elapsed "$(( $(date +%s) - RUN_START ))" >/dev/null 2>&1 || true' EXIT

echo "[$(ts)] cron_seo tick $TICK_ID starting (parallel per-product)" >> "$TICK_LOG"

# --- Reap stuck rows once per tick (shared state) ---
python3 "$SCRIPT_DIR/reap_stuck.py" >> "$TICK_LOG" 2>&1 || true

# --- Ingest any human replies to escalation emails into seo_escalations ---
# Polls i@m13v.com inbox for `Re: [SEO #N]`, flips matching rows to
# status='replied' with the human reply attached. See
# scripts/ingest_human_seo_replies.py.
python3 "$REPO_DIR/scripts/ingest_human_seo_replies.py" >> "$TICK_LOG" 2>&1 || true

# --- Resume any escalations whose human replies just landed ---
# Re-invokes generate_page.py --resume-escalation <id> for each row in
# 'replied' state. Failures leave the row in 'replied' so the next tick
# retries; cancel via `python3 seo/escalate.py cancel --id N` to stop.
python3 "$SCRIPT_DIR/resume_escalations.py" >> "$TICK_LOG" 2>&1 || true

# --- Enumerate eligible products from config ---
PRODUCTS=()
while IFS= read -r line; do
    [ -n "$line" ] && PRODUCTS+=("$line")
done < <(python3 -c "
import json, os
c = json.load(open('$REPO_DIR/config.json'))
for p in c.get('projects', []):
    if p.get('paused'):
        continue
    lp = p.get('landing_pages', {}) or {}
    repo = lp.get('repo', '')
    if repo and os.path.isdir(os.path.expanduser(repo)):
        print(p['name'])
")

echo "[$(ts)] ${#PRODUCTS[@]} eligible products: ${PRODUCTS[*]}" >> "$TICK_LOG"

# --- Lane runner (one per product, backgrounded) ---
run_lane() {
    local product="$1"
    local product_lower
    product_lower=$(echo "$product" | tr '[:upper:] ' '[:lower:]_')
    local lane_log="$REPO_DIR/skill/logs/cron_seo_${product_lower}-$TICK_ID.log"
    local refresh_file="$REFRESH_DIR/$product_lower"
    local refresh_file_legacy="$REFRESH_DIR/$(echo "$product" | tr '[:upper:]' '[:lower:]')"
    local db_helper="python3 $SCRIPT_DIR/db_helpers.py"

    # Jitter 0-29s to avoid API / CPU bursts at tick boundary
    sleep $((RANDOM % 30))

    {
        echo "[$(ts)] === Lane start: $product ==="

        # Accept either new (underscore) or legacy (space) refresh file
        local active_refresh="$refresh_file"
        if [ -f "$refresh_file_legacy" ] && [ ! -f "$refresh_file" ]; then
            active_refresh="$refresh_file_legacy"
        fi

        # Refresh keywords if stale (>24h) or missing
        local needs_refresh=false
        if [ ! -f "$active_refresh" ]; then
            needs_refresh=true
        else
            local age
            age=$(( $(date +%s) - $(stat -f %m "$active_refresh" 2>/dev/null || stat -c %Y "$active_refresh" 2>/dev/null) ))
            if [ "$age" -gt 86400 ]; then
                needs_refresh=true
            fi
        fi

        if [ "$needs_refresh" = true ]; then
            echo "[$(ts)] Refreshing keywords via DataForSEO"
            python3 "$SCRIPT_DIR/generate_keywords.py" "$product" || true
            touch "$refresh_file"
        else
            echo "[$(ts)] Keywords fresh, skipping DataForSEO refresh"
        fi

        # Check if there's pipeline work to do
        local has_work
        has_work=$($db_helper has_work "$product" 2>/dev/null || echo "no")
        if [ "$has_work" = "no" ]; then
            echo "[$(ts)] No pipeline work for $product (queue drained or all scored+skip)"
            echo "[$(ts)] === Lane done: $product (noop) ==="
            exit 0
        fi

        echo "[$(ts)] Running SERP pipeline"
        "$SCRIPT_DIR/run_serp_pipeline.sh" "$product"
        echo "[$(ts)] === Lane done: $product ==="
    } >> "$lane_log" 2>&1
}

# --- Spawn one lane per product ---
PIDS=()
for product in "${PRODUCTS[@]}"; do
    run_lane "$product" &
    PIDS+=($!)
done

echo "[$(ts)] Spawned ${#PIDS[@]} lanes (pids: ${PIDS[*]})" >> "$TICK_LOG"

# Wait for all lanes so the next launchd tick sees a clean process table
wait "${PIDS[@]}" 2>/dev/null || true

echo "[$(ts)] cron_seo tick $TICK_ID complete" >> "$TICK_LOG"

# Cleanup old logs (keep 14 days) — matches both orchestrator and per-lane filenames
find "$REPO_DIR/skill/logs" -maxdepth 1 -name "cron_seo*-*.log" -mtime +14 -delete 2>/dev/null || true
