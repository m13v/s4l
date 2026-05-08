#!/bin/bash
#
# Cross-project top-pages SEO generation pipeline.
#
# Strategy: pick ONE globally top-scoring page across every project with
# landing_pages.top_pages_enabled = true, then REPLICATE that topical
# momentum onto EVERY enabled project's website by asking Claude Opus to
# propose a project-specific adjacent keyword/slug per target.
#
# Pipeline per invocation:
#   1. pick_top_pages.py --global-mode -> single global winner (product,
#       path, score) + targets[] list of every enabled project + top-N
#       ranking across all projects.
#   2. for each target project:
#       a. claude (Opus) -> propose ONE adjacent keyword+slug adapted for
#           THIS project's audience, riding the global winner's concept.
#       b. seo_keywords row -> INSERT (product, keyword, slug,
#           source='top_page', status='pending', score=2.0). UNIQUE is
#           (product, keyword) so the same keyword on different products
#           is fine.
#       c. generate_page.py --trigger top_page --product <target> ...
#
# Pages produced here show up in the dashboard Activity tab as
# 'page_published_top' (see the activity UNION in bin/server.js).
#
# Usage:
#   ./seo/run_top_pages_pipeline.sh                 # global mode, all targets
#   ./seo/run_top_pages_pipeline.sh --list-enabled  # preview target selection

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG="$ROOT_DIR/config.json"
PICK="python3 $SCRIPT_DIR/pick_top_pages.py"
GENERATOR="python3 $SCRIPT_DIR/generate_page.py"
DB="python3 $SCRIPT_DIR/db_helpers.py"

RUN_START=$(date +%s)
# NOTE: do NOT set an EXIT trap here. The global-lock cleanup at line ~105
# overwrites EXIT traps (bash keeps only one per signal), so the logging
# trap was being silently replaced and run_monitor.log never got a row for
# this pipeline. Both lock-cleanup and logging now live in a single combined
# trap installed AFTER the lock is created.

# Retry wrapper for `claude` (guards against auto-update unlink window).
# shellcheck source=./claude_helpers.sh
source "$SCRIPT_DIR/claude_helpers.sh"

LOG_ROOT="$SCRIPT_DIR/logs"
LOCK_ROOT="$SCRIPT_DIR/.locks/top_pages"
mkdir -p "$LOG_ROOT" "$LOCK_ROOT"

# Load .env for DATABASE_URL, BOOKINGS_DATABASE_URL, OAuth, etc.
[ -f "$ROOT_DIR/.env" ] && set -a && source "$ROOT_DIR/.env" && set +a

# PostHog key comes from keychain if launchd didn't inject it.
if [ -z "${POSTHOG_PERSONAL_API_KEY:-}" ]; then
    POSTHOG_PERSONAL_API_KEY=$(security find-generic-password -s "PostHog-Personal-API-Key-m13v" -w 2>/dev/null || true)
    export POSTHOG_PERSONAL_API_KEY
fi

# Anthropic API key for the structured-output proposal step (propose_keyword.py).
# Tries project-specific key first, then existing user keys. propose_keyword.py
# itself has the same fallback logic, so this export is just a fast-path that
# also surfaces a missing-key warning early.
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    for _svc in "Anthropic API Key Social-Autoposter" "Anthropic API Key Hindsight" "Anthropic API Key Fazm" "Claude API"; do
        ANTHROPIC_API_KEY=$(security find-generic-password -s "$_svc" -w 2>/dev/null || true)
        [ -n "$ANTHROPIC_API_KEY" ] && break
    done
    export ANTHROPIC_API_KEY
    if [ -z "$ANTHROPIC_API_KEY" ]; then
        echo "WARN: no ANTHROPIC_API_KEY available; propose_keyword.py will fail" >&2
    fi
fi

_timestamp() { date -u +%Y-%m-%d_%H%M%S; }

_insert_keyword() {
    # Arg order: product, keyword, slug. source='top_page', status='pending',
    # score=2.0. ON CONFLICT keeps the newest brief without nuking state.
    PROD="$1" KW="$2" SLUG="$3" python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ['SEO_SCRIPT_DIR'])
import db_helpers
conn = db_helpers.get_conn()
cur = conn.cursor()
cur.execute(
    "INSERT INTO seo_keywords (product, keyword, slug, source, status, score) "
    "VALUES (%s, %s, %s, 'top_page', 'pending', 2.0) "
    "ON CONFLICT (product, keyword) DO UPDATE SET "
    "  slug = EXCLUDED.slug, "
    "  source = 'top_page', "
    "  status = CASE WHEN seo_keywords.status IN ('done','in_progress') "
    "               THEN seo_keywords.status ELSE 'pending' END, "
    "  score = GREATEST(seo_keywords.score, 2.0), "
    "  updated_at = NOW()",
    (os.environ['PROD'], os.environ['KW'], os.environ['SLUG']),
)
conn.commit()
cur.close(); conn.close()
print(f"inserted/updated seo_keywords: {os.environ['PROD']} / {os.environ['KW']}")
PY
}

# List-enabled preview: delegates to picker.
if [ "${1:-}" = "--list-enabled" ]; then
    $PICK --list-enabled
    exit 0
fi

# Global lock so two runs can't race the picker/generators.
GLOBAL_LOCK="$LOCK_ROOT/_global.lock"
if [ -f "$GLOBAL_LOCK" ]; then
    AGE=$(( $(date +%s) - $(stat -f %m "$GLOBAL_LOCK" 2>/dev/null || stat -c %Y "$GLOBAL_LOCK" 2>/dev/null) ))
    if [ "$AGE" -lt 3600 ]; then
        echo "=== top-pages pipeline: global lock held (age ${AGE}s), skip"
        exit 0
    fi
    rm -f "$GLOBAL_LOCK"
fi
echo "$$" > "$GLOBAL_LOCK"
# Combined EXIT trap: lock cleanup + run_monitor.log row. Both operations
# must live here because bash only keeps one trap per signal. If you split
# them across two `trap … EXIT` statements, the second one wins and the
# first is silently dropped (which is what historically caused seo_top_pages
# to be invisible in the dashboard Job History).
trap '__e=$?; rm -f "$GLOBAL_LOCK"; python3 "$SCRIPT_DIR/log_seo_run.py" --script "seo_top_pages" --since "$RUN_START" --failed "$__e" --elapsed "$(( $(date +%s) - RUN_START ))" >/dev/null 2>&1 || true' EXIT

TS=$(_timestamp)
LOG_DIR="$LOG_ROOT/_global/top_pages"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/${TS}.log"

echo "=== Top-Pages pipeline (cross-project): $TS ===" | tee -a "$LOG_FILE"

# API-quota markers. Once any lane surfaces one of these, every remaining
# lane would fail the same way and burn credits — short-circuit instead.
# We also catch Claude session JSONL signals (api_error_status:429,
# "hit your limit", rate_limit_event with "status":"rejected") because those
# never appear in the shell .log, only in the per-product _stream.jsonl.
#
# IMPORTANT 2026-04-29: do NOT match bare `rate_limit_event` or `rate.?limit`.
# Claude streams a `rate_limit_event` object on every successful turn as a
# heartbeat (`"status":"allowed"` / `"allowed_warning"`); those patterns trip
# on healthy runs and short-circuit after lane #1. Only `"status":"rejected"`
# signals an actual rejection.
QUOTA_MARKERS='monthly usage limit|429 Too Many|insufficient_quota|"api_error_status":429|hit your limit|"status":"rejected"'
QUOTA_HIT=0

# _quota_check FILE [FILE ...] -> 0 if any quota marker is in any of the
# given files. Skips files that don't exist.
_quota_check() {
    for f in "$@"; do
        [ -n "$f" ] && [ -f "$f" ] && grep -qiE "$QUOTA_MARKERS" "$f" 2>/dev/null && return 0
    done
    return 1
}

# _latest_stream_jsonl DIR -> path to most-recent *_stream.jsonl in DIR.
_latest_stream_jsonl() {
    [ -d "$1" ] || return 0
    ls -t "$1"/*_stream.jsonl 2>/dev/null | head -1
}

# Phase 1: drain stale pending rows from prior ticks. Each pending row is a
# (product, keyword, slug) that a prior run proposed and inserted but whose
# generator exited mid-flight (quota, typecheck, commit race, etc.). Retry
# the generator directly — the proposal already lives in seo_keywords, so
# we don't need the picker or the opus proposal step again. --force overrides
# any half-committed page file on disk.
DRAINED_PRODUCTS=()
pending_rows=$(python3 - <<'PY' 2>/dev/null
import os, psycopg2
try:
    conn = psycopg2.connect(os.environ['DATABASE_URL'])
    cur = conn.cursor()
    cur.execute(
        "SELECT product, keyword, slug FROM seo_keywords "
        "WHERE source='top_page' AND status='pending' "
        "AND updated_at > NOW() - INTERVAL '14 days' "
        "ORDER BY updated_at ASC"
    )
    for r in cur.fetchall():
        print("\t".join(r))
except Exception:
    pass
PY
)

if [ -n "$pending_rows" ]; then
    echo "=== draining stale pending top_page rows ===" | tee -a "$LOG_FILE"
    while IFS=$'\t' read -r DRAIN_PRODUCT DRAIN_KW DRAIN_SLUG; do
        [ -z "$DRAIN_PRODUCT" ] && continue
        DRAIN_LOWER=$(echo "$DRAIN_PRODUCT" | tr '[:upper:]' '[:lower:]')
        DRAIN_LOG_DIR="$LOG_ROOT/${DRAIN_LOWER}/top_pages"
        mkdir -p "$DRAIN_LOG_DIR"
        DRAIN_LOG="$DRAIN_LOG_DIR/${TS}_retry.log"
        echo "=== retry: $DRAIN_PRODUCT / $DRAIN_SLUG ===" | tee -a "$LOG_FILE" "$DRAIN_LOG"
        $GENERATOR --product "$DRAIN_PRODUCT" --keyword "$DRAIN_KW" --slug "$DRAIN_SLUG" --trigger top_page --force >> "$DRAIN_LOG" 2>&1
        RETRY_RC=$?
        echo "=== retry rc=$RETRY_RC ===" | tee -a "$LOG_FILE" "$DRAIN_LOG"
        DRAINED_PRODUCTS+=("$DRAIN_PRODUCT")
        DRAIN_STREAM=$(_latest_stream_jsonl "$DRAIN_LOG_DIR")
        if _quota_check "$DRAIN_LOG" "$DRAIN_STREAM"; then
            QUOTA_HIT=1
            echo "  !! $DRAIN_PRODUCT hit API quota during retry — halting tick" | tee -a "$LOG_FILE"
            break
        fi
    done <<< "$pending_rows"
fi

if [ "$QUOTA_HIT" = "1" ]; then
    {
        echo "=== Top-Pages pipeline halted on quota ==="
        echo "  drained (${#DRAINED_PRODUCTS[@]}): ${DRAINED_PRODUCTS[*]:-none}"
    } | tee -a "$LOG_FILE"
    exit 0
fi

# Pick global winner + full target list.
BRIEF_FILE="$LOG_DIR/${TS}.brief.json"
if ! $PICK --global-mode --days 14 --top-n 15 --out "$BRIEF_FILE" 2>>"$LOG_FILE"; then
    RC=$?
    if [ "$RC" -eq 2 ]; then
        echo "  no signal in last 14d across enabled projects, skip" | tee -a "$LOG_FILE"
        exit 0
    fi
    echo "  global picker failed (rc=$RC); see $LOG_FILE" | tee -a "$LOG_FILE"
    exit 1
fi

# Echo winner summary.
python3 - "$BRIEF_FILE" <<'PY' | tee -a "$LOG_FILE"
import json, sys
b = json.load(open(sys.argv[1]))
w = b["winner"]
print(f"  GLOBAL WINNER: {w['product']} {w['page_url']}")
print(f"    score={w['score']} metrics={w['metrics']}")
print(f"  targets={len(b.get('targets', []))}:")
for t in b.get("targets", []):
    print(f"    - {t['product']:20} {t['domain']}")
PY

# Extract target list (product name per line) for the fan-out loop.
TARGETS=$(python3 - "$BRIEF_FILE" <<'PY'
import json, sys
b = json.load(open(sys.argv[1]))
for t in b.get("targets", []):
    print(t["product"])
PY
)

OVERALL_RC=0
OK_TARGETS=()
FAIL_TARGETS=()
while read -r TARGET_PRODUCT; do
    [ -z "$TARGET_PRODUCT" ] && continue

    if [ "$QUOTA_HIT" = "1" ]; then
        echo "=== $TARGET_PRODUCT: skipping — quota hit earlier this tick ===" | tee -a "$LOG_FILE"
        continue
    fi

    # If Phase 1 already drained a pending row for this product, don't also
    # ship today's fresh page — one top_page per product per tick is enough.
    already_drained=0
    for dp in "${DRAINED_PRODUCTS[@]:-}"; do
        if [ "$dp" = "$TARGET_PRODUCT" ]; then
            already_drained=1
            break
        fi
    done
    if [ "$already_drained" = "1" ]; then
        echo "=== $TARGET_PRODUCT: covered by retry phase, skipping fresh proposal ===" | tee -a "$LOG_FILE"
        OK_TARGETS+=("$TARGET_PRODUCT")
        continue
    fi

    LOWER=$(echo "$TARGET_PRODUCT" | tr '[:upper:]' '[:lower:]')
    PER_LOCK="$LOCK_ROOT/${LOWER}.lock"
    PER_LOG_DIR="$LOG_ROOT/${LOWER}/top_pages"
    mkdir -p "$PER_LOG_DIR"
    PER_LOG="$PER_LOG_DIR/${TS}.log"

    # Per-project lock (30min stale).
    if [ -f "$PER_LOCK" ]; then
        PAGE_AGE=$(( $(date +%s) - $(stat -f %m "$PER_LOCK" 2>/dev/null || stat -c %Y "$PER_LOCK" 2>/dev/null) ))
        if [ "$PAGE_AGE" -lt 1800 ]; then
            echo "=== $TARGET_PRODUCT: per-project lock held (${PAGE_AGE}s), skip" | tee -a "$LOG_FILE" "$PER_LOG"
            continue
        fi
        rm -f "$PER_LOCK"
    fi
    echo "$$" > "$PER_LOCK"

    # Brace group is a subshell because of the pipe to tee. Any assignment
    # to OVERALL_RC inside it is lost, and `continue` only breaks out of the
    # subshell, never the outer while. So we exit the subshell with a
    # distinct rc and let the outer loop tally via PIPESTATUS.
    #   0     ok
    #   10    claude proposal failed (after retries)
    #   11    proposal parse failed
    #   12    slug already exists (treated as ok / no work)
    #   other generator rc passthrough
    {
    echo "=== Top-Pages target: $TARGET_PRODUCT (ts=$TS) ==="

    # Per-target proposal: structured-output via Anthropic SDK (propose_keyword.py).
    #
    # Replaced the prior `claude -p --output-format json` flow on 2026-05-08.
    # The CLI flow was free-text completion with a "please return JSON" hint;
    # nothing enforced schema, so the model occasionally truncated the closing
    # brace (Terminator hit this on 2026-05-07) and the shell-side parser had
    # a 60-line regex fallback to salvage broken output.
    #
    # propose_keyword.py uses the SDK's tool-use mode with a strict JSON schema
    # for {keyword, slug, concept}, plus the server-side web_search tool for
    # grounding. The API itself rejects malformed tool input, so the model
    # cannot emit truncated/invalid JSON. No fallback parser needed.
    PROPOSAL_FILE="$PER_LOG_DIR/${TS}.proposal.json"
    if ! python3 "$SCRIPT_DIR/propose_keyword.py" "$BRIEF_FILE" "$TARGET_PRODUCT" \
            > "$PROPOSAL_FILE" 2>>"$PER_LOG"; then
        echo "  propose_keyword.py failed; see $PER_LOG"
        exit 10
    fi

    PARSED=$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
kw = (d.get("keyword") or "").strip()
slug = (d.get("slug") or "").strip()
concept = (d.get("concept") or "").strip()
if not kw or not slug:
    print("ERR missing_fields", file=sys.stderr); sys.exit(1)
print(f"{kw}\t{slug}\t{concept}")
' "$PROPOSAL_FILE")
    if [ -z "$PARSED" ]; then
        echo "  proposal parse failed; see $PROPOSAL_FILE"
        exit 11
    fi

    KEYWORD=$(printf '%s' "$PARSED" | awk -F'\t' '{print $1}')
    SLUG=$(printf '%s' "$PARSED" | awk -F'\t' '{print $2}')
    CONCEPT=$(printf '%s' "$PARSED" | awk -F'\t' '{print $3}')
    echo "  keyword: $KEYWORD"
    echo "  slug:    $SLUG"
    echo "  concept: $CONCEPT"

    # Guard: skip if generator already has a completed page with this slug.
    SLUG_CHECK=$($DB check_slug "$TARGET_PRODUCT" "$SLUG")
    if [ "$SLUG_CHECK" = "exists" ]; then
        echo "  slug '$SLUG' already done on $TARGET_PRODUCT; skipping"
        exit 12
    fi

    SEO_SCRIPT_DIR="$SCRIPT_DIR" _insert_keyword "$TARGET_PRODUCT" "$KEYWORD" "$SLUG" 2>&1

    echo "--- generate_page.py --trigger top_page ---"
    $GENERATOR --product "$TARGET_PRODUCT" --keyword "$KEYWORD" --slug "$SLUG" --trigger top_page 2>&1
    GEN_RC=$?
    if [ "$GEN_RC" -ne 0 ]; then
        echo "  generator failed on $TARGET_PRODUCT (rc=$GEN_RC)"
        exit "$GEN_RC"
    fi
    exit 0
    } 2>&1 | tee -a "$PER_LOG" "$LOG_FILE"
    TARGET_RC=${PIPESTATUS[0]}
    rm -f "$PER_LOCK"

    case "$TARGET_RC" in
        0|12)
            echo "=== $TARGET_PRODUCT ok ===" | tee -a "$LOG_FILE"
            OK_TARGETS+=("$TARGET_PRODUCT")
            ;;
        *)
            echo "=== $TARGET_PRODUCT failed (rc=$TARGET_RC) ===" | tee -a "$LOG_FILE"
            FAIL_TARGETS+=("$TARGET_PRODUCT(rc=$TARGET_RC)")
            OVERALL_RC="$TARGET_RC"
            ;;
    esac

    # Quota short-circuit: if this target hit the usage wall, the next one
    # will too. Set the flag so the loop stops on the next iteration.
    PER_STREAM=$(_latest_stream_jsonl "$PER_LOG_DIR")
    if _quota_check "$PER_LOG" "$PER_STREAM"; then
        QUOTA_HIT=1
        echo "  !! $TARGET_PRODUCT hit API quota — halting tick" | tee -a "$LOG_FILE"
    fi

done <<< "$TARGETS"

{
    echo "=== Top-Pages pipeline finished rc=$OVERALL_RC ==="
    echo "  ok    (${#OK_TARGETS[@]}): ${OK_TARGETS[*]:-none}"
    echo "  fail  (${#FAIL_TARGETS[@]}): ${FAIL_TARGETS[*]:-none}"
} | tee -a "$LOG_FILE"
exit "$OVERALL_RC"
