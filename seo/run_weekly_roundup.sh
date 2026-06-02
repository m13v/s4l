#!/bin/bash
#
# Weekly Cross-Product Roundup Pipeline.
#
# Iterates every project in config.json that has `seo_roundup.category` set
# and a landing_pages.repo on disk, and generates one dated best-of listicle
# per project. Each page lives at /best/<slug>-YYYY-MM-DD on the host
# product's own domain, lists 6-10 sibling projects (including cross-industry
# picks), and wires every sibling CTA to trackCrossProductClick so clicks
# attribute to the dashboard's Cross Product column.
#
# Lanes run in parallel (one background process per product) with a 0-30s
# jitter, matching the pattern used by cron_seo.sh. Per-product locks
# prevent double-generation if a previous run is still processing.
#
# Scheduling: launchd com.m13v.seo-weekly-roundup (Mondays 07:00 local).
# Also runnable ad-hoc: ./run_weekly_roundup.sh
#
# State: writes rows to seo_keywords with source='roundup'. The dashboard
# Status tab reads these as page_published_roundup events.

set -euo pipefail

# Cycle ID for cross-cycle cost accounting.
BATCH_ID="${BATCH_ID:-seowk-$(date +%Y%m%d-%H%M%S)}"
export BATCH_ID
export SA_CYCLE_ID="$BATCH_ID"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG="$ROOT_DIR/config.json"
GENERATOR="python3 $SCRIPT_DIR/generate_page.py"

RUN_START=$(date +%s)
trap '__e=$?; python3 "$SCRIPT_DIR/log_seo_run.py" --script "seo_weekly_roundup" --since "$RUN_START" --failed "$__e" --elapsed "$(( $(date +%s) - RUN_START ))" >/dev/null 2>&1 || true' EXIT

TICK_ID=$(date +%Y-%m-%d_%H%M%S)
TICK_LOG_DIR="$SCRIPT_DIR/logs/roundup"
TICK_LOG="$TICK_LOG_DIR/tick_${TICK_ID}.log"

mkdir -p "$SCRIPT_DIR/.locks" "$TICK_LOG_DIR"

# Load .env so DATABASE_URL and API keys are available to spawned lanes.
[ -f "$ROOT_DIR/.env" ] && set -a && . "$ROOT_DIR/.env" && set +a

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(ts)] $*" | tee -a "$TICK_LOG"; }

log "=== weekly-roundup tick $TICK_ID starting ==="

# Produce the eligible product list: anything with seo_roundup.category set
# and a real landing_pages.repo on disk. Emits TSV rows: name<TAB>category.
# macOS /bin/bash is 3.2 which lacks `mapfile`, so read the lines into the
# array with a while-read loop instead.
ELIGIBLE=()
while IFS= read -r __line; do
    ELIGIBLE+=("$__line")
done < <(python3 - <<PY
import json, os
with open("$CONFIG") as f:
    cfg = json.load(f)
for p in cfg.get("projects", []):
    sr = p.get("seo_roundup") or {}
    cat = sr.get("category")
    repo = (p.get("landing_pages") or {}).get("repo", "")
    repo = os.path.expanduser(repo) if repo else ""
    if cat and repo and os.path.isdir(repo):
        print(f"{p['name']}\t{cat}")
PY
)

if [ "${#ELIGIBLE[@]}" -eq 0 ]; then
    log "no eligible projects (seo_roundup.category unset or repo missing). nothing to do."
    exit 0
fi

log "eligible projects: ${#ELIGIBLE[@]}"
for row in "${ELIGIBLE[@]}"; do
    log "  $(echo "$row" | awk -F'\t' '{printf "%-20s %s", $1, $2}')"
done

# API-quota markers. A lane's final_result_text will surface one of these
# when the org has exhausted its monthly usage or a provider-side rate limit
# trips. Once any lane reports it, every remaining lane would fail the same
# way and burn credits for nothing — short-circuit instead.
# Match both shell-output markers and Claude session JSONL signals
# (api_error_status:429 / "hit your limit" / rate_limit_event with
# "status":"rejected"). The latter only appear in the per-product
# *_stream.jsonl, never in the shell log.
#
# IMPORTANT 2026-04-29: do NOT match bare `rate_limit_event` or `rate.?limit`.
# Claude streams a `rate_limit_event` object on every successful turn as a
# heartbeat (`"status":"allowed"` / `"allowed_warning"`); those patterns trip
# on healthy runs and short-circuit after lane #1. Only `"status":"rejected"`
# signals an actual rejection.
QUOTA_MARKERS='monthly usage limit|429 Too Many|insufficient_quota|"api_error_status":429|hit your limit|"status":"rejected"'
QUOTA_HIT=0

# Run products sequentially instead of in parallel. Parallel fanout used to
# fire 15 Claude sessions in a 30s window, so a single quota wall could wipe
# the entire tick; serial keeps pre-wall lanes durable and lets the loop
# break cleanly on the first wall hit. Weekly cadence has the wall-clock
# headroom — this tick can take hours.
for row in "${ELIGIBLE[@]}"; do
    if [ "$QUOTA_HIT" = "1" ]; then
        log "  skipping remaining lanes — quota hit earlier this tick"
        break
    fi
    product=$(echo "$row" | awk -F'\t' '{print $1}')
    category=$(echo "$row" | awk -F'\t' '{print $2}')
    product_lower=$(echo "$product" | tr '[:upper:] ' '[:lower:]-')
    lock="$SCRIPT_DIR/.locks/roundup_${product_lower}.lock"
    lane_log="$TICK_LOG_DIR/${TICK_ID}_${product_lower}.log"

    (
        if [ -f "$lock" ]; then
            age=$(( $(date +%s) - $(stat -f %m "$lock" 2>/dev/null || stat -c %Y "$lock" 2>/dev/null || echo 0) ))
            if [ "$age" -gt 1800 ]; then
                echo "[$(ts)] $product: stale lock (${age}s), removing" >> "$lane_log"
                rm -f "$lock"
            else
                echo "[$(ts)] $product: lock held (${age}s), skipping tick" >> "$lane_log"
                exit 0
            fi
        fi
        echo "$$" > "$lock"
        trap 'rm -f "$lock"' EXIT

        # If a recent prior tick left a pending row for this product (lane
        # bounced on quota / typecheck / commit race), retry THAT row's
        # original slug+keyword so we finish the job instead of abandoning
        # it. The slug was date-stamped to the prior run so --force is
        # required to overwrite any half-committed page file.
        pending=$(SEO_SCRIPT_DIR="$SCRIPT_DIR" python3 - "$product" <<'PY' 2>/dev/null
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.environ['SEO_SCRIPT_DIR']), 'scripts'))
from http_api import api_get, load_env
load_env()
try:
    resp = api_get('/api/v1/seo/keywords', query={'mode': 'stale_pending', 'source': 'roundup', 'product': sys.argv[1]})
    rows = (resp.get('data') or {}).get('rows') or []
    if rows:
        r = rows[0]
        print(f"{r.get('keyword') or ''}\t{r.get('slug') or ''}")
except Exception:
    pass
PY
)

        force_flag=""
        if [ -n "$pending" ]; then
            keyword=$(echo "$pending" | awk -F'\t' '{print $1}')
            slug=$(echo "$pending" | awk -F'\t' '{print $2}')
            force_flag="--force"
            echo "[$(ts)] $product: retrying pending row  slug=$slug" >> "$lane_log"
            echo "  keyword='$keyword'" >> "$lane_log"
        else
            today=$(date +%Y-%m-%d)
            month_name=$(date +%B)
            day_num=$(date +%-d)
            year=$(date +%Y)
            # Slug: best-<category-kebab>-YYYY-MM-DD. New weekly page.
            cat_slug=$(echo "$category" | tr '[:upper:] ' '[:lower:]-' | sed -E 's/[^a-z0-9-]+/-/g; s/-+/-/g; s/^-|-$//g')
            slug="best-${cat_slug}-${today}"
            keyword="Best ${category} for ${month_name} ${day_num}, ${year}"
            echo "[$(ts)] $product: starting roundup" >> "$lane_log"
            echo "  slug=$slug" >> "$lane_log"
            echo "  keyword='$keyword'" >> "$lane_log"
        fi

        # Hand off to the unified generator. content_type=cross_roundup
        # routes to /best/<slug>; trigger=roundup writes seo_keywords rows
        # with source='roundup'.
        $GENERATOR \
            --product "$product" \
            --keyword "$keyword" \
            --slug "$slug" \
            --trigger roundup \
            --content-type cross_roundup \
            $force_flag \
            >> "$lane_log" 2>&1
        exit_code=$?
        echo "[$(ts)] $product: generator exit=$exit_code" >> "$lane_log"
        exit "$exit_code"
    ) || true
    # `|| true` so a single lane's non-zero exit (or an unexpected signal
    # killing the subshell — e.g. the 22:32 claude-meter incident) doesn't
    # trip the parent's `set -e` and abandon the remaining queued lanes.

    # Quota short-circuit: the lane just finished; if its log OR its Claude
    # session JSONL contains a quota marker, the next lane will hit the same
    # wall. Flag it so the outer loop breaks on the next iteration. The
    # Claude session stream lives under seo/logs/<product_lower>/ alongside
    # the generator's other artifacts; the lane_log only captures shell
    # output, which never contains rate_limit_event.
    quota_match=""
    if grep -qiE "$QUOTA_MARKERS" "$lane_log" 2>/dev/null; then
        quota_match="$lane_log"
    else
        product_stream=$(ls -t "$SCRIPT_DIR/logs/${product_lower}/"*_stream.jsonl 2>/dev/null | head -1)
        if [ -n "$product_stream" ] && grep -qiE "$QUOTA_MARKERS" "$product_stream" 2>/dev/null; then
            quota_match="$product_stream"
        fi
    fi
    if [ -n "$quota_match" ]; then
        QUOTA_HIT=1
        log "  !! $product hit API quota ($(basename "$quota_match")) — halting tick, ${#ELIGIBLE[@]} lanes still pending"
    fi
done

if [ "$QUOTA_HIT" = "1" ]; then
    log "=== weekly-roundup tick $TICK_ID halted on quota ==="
else
    log "=== weekly-roundup tick $TICK_ID complete ==="
fi
