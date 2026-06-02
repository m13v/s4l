#!/bin/bash
#
# GSC SEO Inbox Pipeline.
# Fetches real search queries from Google Search Console for a product,
# picks the highest-impression pending query, and hands off to the unified
# generator (generate_page.py). No SERP scoring step — GSC queries are
# already proven search demand.
#
# Parallel to run_serp_pipeline.sh which hunts for new opportunities via
# DataForSEO keyword research + SERP scoring.
#
# Usage:
#   ./run_gsc_pipeline.sh <product_name>
#
# Requires gsc_property in config.json landing_pages block.
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG="$ROOT_DIR/config.json"
GENERATOR="python3 $SCRIPT_DIR/generate_page.py"

PRODUCT="${1:?Usage: $0 <product_name>}"
PRODUCT_LOWER=$(echo "$PRODUCT" | tr '[:upper:]' '[:lower:]')
LOCK_FILE="$SCRIPT_DIR/.locks/gsc_${PRODUCT_LOWER}.lock"
LOG_DIR="$SCRIPT_DIR/logs/gsc_${PRODUCT_LOWER}"
TIMESTAMP=$(date -u +%Y%m%d-%H%M%S)

mkdir -p "$SCRIPT_DIR/.locks" "$LOG_DIR"

# Load .env for DATABASE_URL
[ -f "$ROOT_DIR/.env" ] && source "$ROOT_DIR/.env"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_DIR/${TIMESTAMP}.log"; }

# --- Lock ---
if [ -f "$LOCK_FILE" ]; then
    LOCK_AGE=$(( $(date +%s) - $(stat -f %m "$LOCK_FILE" 2>/dev/null || stat -c %Y "$LOCK_FILE" 2>/dev/null) ))
    if [ "$LOCK_AGE" -gt 1800 ]; then
        log "Stale lock (${LOCK_AGE}s), removing"
        rm -f "$LOCK_FILE"
    else
        log "Pipeline already running for $PRODUCT (lock age: ${LOCK_AGE}s)"
        exit 0
    fi
fi
echo "$$" > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

# --- Read product config ---
REPO_PATH=$(python3 -c "
import json, os
with open('$CONFIG') as f:
    c = json.load(f)
for p in c.get('projects', []):
    if p['name'].lower() == '$PRODUCT_LOWER':
        repo = p.get('landing_pages', {}).get('repo', '')
        print(os.path.expanduser(repo))
        break
")

WEBSITE=$(python3 -c "
import json
with open('$CONFIG') as f:
    c = json.load(f)
for p in c.get('projects', []):
    if p['name'].lower() == '$PRODUCT_LOWER':
        print(p.get('landing_pages', {}).get('base_url') or p.get('website', ''))
        break
")

GSC_PROPERTY=$(python3 -c "
import json
with open('$CONFIG') as f:
    c = json.load(f)
for p in c.get('projects', []):
    if p['name'].lower() == '$PRODUCT_LOWER':
        print(p.get('landing_pages', {}).get('gsc_property', ''))
        break
")

if [ -z "$GSC_PROPERTY" ]; then
    log "ERROR: no gsc_property configured for $PRODUCT in config.json"
    exit 1
fi

if [ -z "$REPO_PATH" ] || [ ! -d "$REPO_PATH" ]; then
    log "ERROR: repo not found at $REPO_PATH"
    exit 1
fi

log "=== GSC Pipeline: $PRODUCT ==="
log "  Repo: $REPO_PATH"
log "  Website: $WEBSITE"
log "  GSC: $GSC_PROPERTY"

# --- Step 1: Fetch GSC queries into Postgres ---
log "Step 1: Fetching GSC queries..."
python3 "$SCRIPT_DIR/fetch_gsc_queries.py" --product "$PRODUCT" >> "$LOG_DIR/${TIMESTAMP}.log" 2>&1
FETCH_EXIT=$?
if [ "$FETCH_EXIT" -ne 0 ]; then
    log "Step 1: FAILED (exit $FETCH_EXIT)"
    exit "$FETCH_EXIT"
fi
log "Step 1: Done"

# --- Step 2: Pick next pending query (>= 5 impressions, highest first) ---
NEXT_JSON=$(python3 "$SCRIPT_DIR/gsc_helpers.py" pick "$PRODUCT" 2>/dev/null)

if [ -z "$NEXT_JSON" ]; then
    log "No pending queries with >= 5 impressions. Done."
    exit 0
fi

QUERY=$(echo "$NEXT_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['query'])")
SLUG=$(echo "$QUERY" | python3 -c "
import sys, re
q = sys.stdin.read().strip().lower()
slug = re.sub(r'[^a-z0-9]+', '-', q).strip('-')
slug = slug[:80]
print(slug)
")

log "Next query: '$QUERY' (slug: $SLUG)"

# --- Forbidden-keyword guard ---
# Block keyword patterns the product's content policy rules out (e.g. Vipassana
# forbids technique-instruction pages). Mark skip so we don't fetch this query
# again next tick.
FORBIDDEN_MATCH=$(python3 "$SCRIPT_DIR/db_helpers.py" check_forbidden "$PRODUCT" "$QUERY")
if [ "$FORBIDDEN_MATCH" != "ok" ]; then
    log "Forbidden keyword pattern matched: '$FORBIDDEN_MATCH'. Marking skip."
    python3 "$SCRIPT_DIR/gsc_helpers.py" mark "$PRODUCT" "$QUERY" skip "forbidden_keyword: $FORBIDDEN_MATCH" 2>/dev/null
    exit 0
fi

# --- Step 3: Mark in_progress ---
python3 "$SCRIPT_DIR/gsc_helpers.py" mark "$PRODUCT" "$QUERY" in_progress 2>/dev/null

# --- Step 4: Hand off to unified generator ---
# Generator owns: prompt, stream-json tool capture, git verification,
# and state transition (done on success, pending on failure).
log "Step 4: Invoking generate_page.py (trigger=gsc)..."
$GENERATOR --product "$PRODUCT" --keyword "$QUERY" --slug "$SLUG" --trigger gsc \
    2>&1 | tee -a "$LOG_DIR/${TIMESTAMP}.log"
GEN_EXIT=${PIPESTATUS[0]}

if [ "$GEN_EXIT" -ne 0 ]; then
    log "Step 4: Generator failed (exit $GEN_EXIT); state reset to pending."
    exit "$GEN_EXIT"
fi

log "Step 4: Done."

# --- Summary ---
COUNTS=$(python3 "$SCRIPT_DIR/gsc_helpers.py" summary "$PRODUCT" 2>/dev/null)
log "=== GSC Pipeline complete: $PRODUCT | $COUNTS ==="
