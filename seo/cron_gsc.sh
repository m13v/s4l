#!/bin/bash
#
# GSC SEO Inbox cron orchestrator. Runs every 10 minutes.
#
# PARALLEL PER-PRODUCT: fans out one background lane per eligible product
# (products with landing_pages.gsc_property configured). Each lane invokes
# run_gsc_pipeline.sh, which internally refreshes GSC query data via the
# Search Console API, picks the highest-impression pending query, and
# hands off to generate_page.py.
#
# Per-product locks in run_gsc_pipeline.sh prevent double-work if a
# previous tick's lane is still processing.
#
# Previously this picked a single product via weighted-random selection,
# which starved every product except fazm (the one with the largest queue).
# The parallel design gives every GSC-configured product its own lane.
#
# Usage: cron_gsc.sh (no args)
#

set -euo pipefail

# Cycle ID for cross-cycle cost accounting. Stamps claude_sessions.cycle_id
# via env inheritance through run_gsc_pipeline.sh + generate_page.py.
BATCH_ID="${BATCH_ID:-seogsc-$(date +%Y%m%d-%H%M%S)}"
export BATCH_ID
export SA_CYCLE_ID="$BATCH_ID"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TICK_ID=$(date +%Y-%m-%d_%H%M%S)
TICK_LOG="$REPO_DIR/skill/logs/cron_gsc-$TICK_ID.log"

mkdir -p "$SCRIPT_DIR/.locks" "$REPO_DIR/skill/logs"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

RUN_START=$(date +%s)
trap '__e=$?; python3 "$SCRIPT_DIR/log_seo_run.py" --script "gsc_seo" --since "$RUN_START" --failed "$__e" --elapsed "$(( $(date +%s) - RUN_START ))" >/dev/null 2>&1 || true' EXIT

echo "[$(ts)] cron_gsc tick $TICK_ID starting (parallel per-product)" >> "$TICK_LOG"

# --- Reap stuck rows once per tick (shared state) ---
python3 "$SCRIPT_DIR/reap_stuck.py" >> "$TICK_LOG" 2>&1 || true

# --- Ingest human replies to escalation emails into seo_escalations ---
# Polls i@m13v.com inbox for `Re: [SEO #N]`, flips matching rows to
# status='replied' with the human reply attached. Moved here from the
# retired cron_seo.sh on 2026-05-16 because cron_gsc.sh is the most
# frequent active SEO cron (every 10 minutes). The model_initiated
# trigger has been removed from generate_page.py; the remaining
# triggers (setup_gate, reaper_stuck) still need ingest + resume.
# See scripts/ingest_human_seo_replies.py.
python3 "$REPO_DIR/scripts/ingest_human_seo_replies.py" >> "$TICK_LOG" 2>&1 || true

# --- Resume any escalations whose human replies just landed ---
# Re-invokes generate_page.py --resume-escalation <id> for each row in
# 'replied' state. Failures leave the row in 'replied' so the next tick
# retries; cancel via `python3 seo/escalate.py cancel --id N` to stop.
python3 "$SCRIPT_DIR/resume_escalations.py" >> "$TICK_LOG" 2>&1 || true

# --- Enumerate eligible products (repo + gsc_property) ---
PRODUCTS=()
while IFS= read -r line; do
    [ -n "$line" ] && PRODUCTS+=("$line")
done < <(python3 -c "
import json, os
c = json.load(open('$REPO_DIR/config.json'))
for p in c.get('projects', []):
    lp = p.get('landing_pages', {}) or {}
    repo = lp.get('repo', '')
    gsc = lp.get('gsc_property', '')
    if repo and gsc and os.path.isdir(os.path.expanduser(repo)):
        print(p['name'])
")

echo "[$(ts)] ${#PRODUCTS[@]} eligible products: ${PRODUCTS[*]}" >> "$TICK_LOG"

# --- Lane runner (one per product, backgrounded) ---
run_lane() {
    local product="$1"
    local product_lower
    product_lower=$(echo "$product" | tr '[:upper:] ' '[:lower:]_')
    local lane_log="$REPO_DIR/skill/logs/cron_gsc_${product_lower}-$TICK_ID.log"

    # Jitter 0-29s to avoid GSC API / CPU bursts
    sleep $((RANDOM % 30))

    {
        echo "[$(ts)] === Lane start: $product ==="
        "$SCRIPT_DIR/run_gsc_pipeline.sh" "$product"
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

echo "[$(ts)] cron_gsc tick $TICK_ID complete" >> "$TICK_LOG"

# Cleanup old logs (keep 14 days)
find "$REPO_DIR/skill/logs" -maxdepth 1 -name "cron_gsc*-*.log" -mtime +14 -delete 2>/dev/null || true
