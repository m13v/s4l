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

# Cycle ID for cross-cycle cost accounting. Stamps claude_sessions.cycle_id
# via env inheritance.
BATCH_ID="${BATCH_ID:-seotp-$(date +%Y%m%d-%H%M%S)}"
export BATCH_ID
export SA_CYCLE_ID="$BATCH_ID"

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

# IMPORTANT: this pipeline (and every social-autoposter pipeline) authenticates
# Claude via the `claude` CLI's OAuth subscription, NEVER via ANTHROPIC_API_KEY.
# Strip the env var if anything upstream set it, so the CLI uses OAuth even
# when a sibling project (e.g. Hindsight, Fazm) has exported a key in the
# parent shell. Matches the pattern already used in scripts/engage_reddit.py
# and scripts/engage_github.py.
unset ANTHROPIC_API_KEY

_timestamp() { date -u +%Y-%m-%d_%H%M%S; }

_insert_keyword() {
    # Arg order: product, keyword, slug. source='top_page', status='pending',
    # score=2.0. ON CONFLICT keeps the newest brief without nuking state.
    PROD="$1" KW="$2" SLUG="$3" python3 - <<'PY'
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.environ['SEO_SCRIPT_DIR']), 'scripts'))
from http_api import api_post, load_env
load_env()
api_post('/api/v1/seo/keywords', {
    'top_page': True,
    'product': os.environ['PROD'],
    'keyword': os.environ['KW'],
    'slug': os.environ['SLUG'],
})
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
pending_rows=$(SEO_SCRIPT_DIR="$SCRIPT_DIR" python3 - <<'PY' 2>/dev/null
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.environ['SEO_SCRIPT_DIR']), 'scripts'))
from http_api import api_get, load_env
load_env()
try:
    resp = api_get('/api/v1/seo/keywords', query={'mode': 'stale_top_page'})
    for r in (resp.get('data') or {}).get('rows') or []:
        print("\t".join([r.get('product') or '', r.get('keyword') or '', r.get('slug') or '']))
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

    # Build per-target prompt: global winner + target project config.
    #
    # Output protocol: XML-tagged fields, NOT a single JSON object.
    # Why: this script runs through `claude -p` (OAuth subscription, no schema
    # enforcement), and on 2026-05-07 the Terminator target failed because the
    # model truncated the closing `}` of a JSON blob, leaving `"...end_url"`
    # with no `}` for `rfind("}")` to find. With independently-delimited XML
    # tags, each field is recoverable on its own — the parser does three
    # `re.search` calls, not one nested-JSON walk. Anthropic's own prompting
    # guide recommends XML over JSON for exactly this kind of structured
    # response from free-text completion.
    PROPOSAL_PROMPT="$PER_LOG_DIR/${TS}.proposal.prompt"
    PROPOSAL_FILE="$PER_LOG_DIR/${TS}.proposal.json"

    python3 - "$BRIEF_FILE" "$TARGET_PRODUCT" > "$PROPOSAL_PROMPT" <<'PY'
import json, sys
from datetime import datetime
brief = json.load(open(sys.argv[1]))
target_product = sys.argv[2]
target = next((t for t in brief.get("targets", []) if t["product"] == target_product), None)
if not target:
    print(f"ERROR: target {target_product} not in brief", file=sys.stderr)
    sys.exit(1)
proj = target["project_config"]
winner = brief["winner"]

# Date awareness for time-sensitive keywords. Without this, the model echoes
# whatever month/year is in the global winner's slug (e.g. "april-2026") even
# after the calendar rolled to the next month, producing dead-on-arrival pages.
_now = datetime.now()
CURRENT_MONTH = _now.strftime("%B")
CURRENT_MONTH_LOWER = _now.strftime("%B").lower()
CURRENT_YEAR = _now.strftime("%Y")
CURRENT_DATE_HUMAN = _now.strftime("%B %Y")

prompt = f"""You are a senior SEO strategist. A global ranking across multiple
sibling products identified ONE top-performing page in the last 24h, scored by
a weighted composite of pageviews, email_signups, schedule_clicks,
get_started_clicks, and bookings.

TODAY IS {CURRENT_DATE_HUMAN}.

GLOBAL WINNER (source of topical momentum):
  product: {winner['product']}
  page:    {winner['page_url']}
  score:   {winner['score']}
  metrics: {json.dumps(winner['metrics'])}

Your job: propose ONE NEW adjacent landing page for the TARGET PROJECT below
that rides the same topical wave, adapted for that project's audience and
positioning. Do NOT copy the winning slug verbatim, propose a slug and
keyword that fits the target's voice and ICP.

TARGET PROJECT:
  name:        {target['product']}
  domain:      {target['domain']}
  website:     {target['website']}
  positioning: {json.dumps(proj.get('qualification', {}), ensure_ascii=False)}
  description: {proj.get('description', '')}

TOP 10 RANKING (for context across all projects):
"""
for r in brief.get("ranking", [])[:10]:
    prompt += f"  {r['score']:>6} {r['product']:20} {r['page_url']}\n"

prompt += f"""
RESEARCH FIRST (HARD REQUIREMENT, do not skip):
Your training cutoff predates {CURRENT_DATE_HUMAN}. You do NOT know what
shipped recently. You MUST call WebSearch before proposing anything.
A proposal without web-grounded evidence is invalid output.

Run AT LEAST 3 WebSearch queries (more is fine). Suggested queries:

  1. Topical area from the global winner, filtered to recent news:
     - "<topic from winner> {CURRENT_MONTH} {CURRENT_YEAR}"
     - "<topic> news {CURRENT_YEAR}" / "<topic> latest release"
     - "<vendor or category> announcement {CURRENT_MONTH} {CURRENT_YEAR}"
  2. TARGET project's audience-specific news:
     - "<target ICP / use case> new tool {CURRENT_YEAR}"
     - "<target category> launch {CURRENT_MONTH} {CURRENT_YEAR}"
  3. If a specific model / product / release surfaces (a new LLM, a
     new framework, a new API, a vendor announcement), search that thing
     by name to confirm it shipped in the last ~30 days and pull
     1-2 concrete details (version number, release date, capability claim).
     Use WebFetch on the most relevant result if a search snippet alone
     is not enough.

Use what you find. The proposed page must ride the global winner's
topical momentum AND be grounded in something that demonstrably happened
recently (cite the source URL in your concept field).

PROPOSAL SHAPES (pick whichever best fits what your research surfaced):

A. SINGLE-BLOCKBUSTER (preferred when one notable release dominates).
   One post about ONE specific recent thing: a new model, a new product,
   a vendor launch, a feature drop. The keyword can be the product/model
   name itself ("claude opus 4.7 deep dive") or a how-to/explainer about
   it ("how to use <new thing> for <use case>"). Date does NOT need to
   appear in the slug or keyword; freshness comes from the post being
   about a real {CURRENT_DATE_HUMAN} event.

B. ROUNDUP/DIGEST (preferred when multiple notable releases happened).
   Covers several recent releases in the topical area. SHOULD include
   "{CURRENT_MONTH_LOWER} {CURRENT_YEAR}" or "{CURRENT_YEAR}" in the
   keyword/slug ("ai model releases {CURRENT_MONTH_LOWER} {CURRENT_YEAR}").

C. COMPARISON / HOW-TO with a fresh hook (acceptable when one specific
   recent change makes an old comparison newly relevant). Example:
   "X vs Y after the new Z release". Dated phrasing optional.

D. EVERGREEN comparison/how-to. Use ONLY if WebSearch returned no
   relevant recent news. Default to A or B whenever your research
   surfaced something concrete.

Rules:
- keyword must be a 3-8 word search phrase a human would actually type
  for the TARGET project's audience.
- slug must be kebab-case, ASCII, <= 64 chars, unique on the target site.
- concept must be 1-2 sentences explaining the angle, citing the specific
  news/release you found via WebSearch (vendor name, version, or event)
  so the downstream generator can verify and write a grounded page.
- Never echo a stale month/year (any month != "{CURRENT_MONTH_LOWER}"
  or year != "{CURRENT_YEAR}") into a dated slug/keyword.

OUTPUT FORMAT (STRICT, read carefully):

Respond with EXACTLY these three XML tags, on separate lines, in this
order, and nothing else. No preamble. No JSON. No code fences. No
trailing commentary. The fields are independently parsed; do not nest
tags or wrap them in any container.

  <keyword>your 3-8 word phrase here</keyword>
  <slug>your-kebab-case-slug-here</slug>
  <concept>your 1-2 sentence angle with vendor/version/source URL here</concept>
"""
sys.stdout.write(prompt)
PY

    # Model: inherit the global default from ~/.claude/settings.json
    # (currently "claude-opus-4-7"). Setting the model in one place lets the
    # whole social-autoposter stack run on the same opus build without each
    # pipeline pinning its own version string that drifts. CLAUDE_MODEL env
    # still wins for one-off overrides.
    #
    # The conditional `${CLAUDE_MODEL:+--model "$CLAUDE_MODEL"}` expands to
    # nothing when CLAUDE_MODEL is unset/empty (so the CLI uses settings.json),
    # and to `--model <value>` when it's set. This sidesteps the macOS bash 3.2
    # set -u empty-array bug that killed every target after the auto-commit
    # agent's c2b5e61 change on 2026-05-08.
    #
    # WebSearch + WebFetch are REQUIRED for this call. The prompt instructs
    # the model to run >=3 WebSearch queries to ground proposals in real
    # current-month news (since the model's training cutoff predates today).
    # Without --allowed-tools and --dangerously-skip-permissions, headless
    # Claude silently denies tool calls and the model falls back to priors,
    # which is how we ended up with zero "may-2026" pages despite April having
    # 28 of them.
    #
    # No --max-turns: the model needs an unbounded number of tool turns to
    # do real research (search, optionally fetch a result page, then propose).
    if ! claude_with_retry ${CLAUDE_MODEL:+--model "$CLAUDE_MODEL"} --print --output-format json \
            --allowed-tools "WebSearch,WebFetch" \
            --dangerously-skip-permissions \
            < "$PROPOSAL_PROMPT" > "$PROPOSAL_FILE" 2>>"$PER_LOG"; then
        echo "  claude proposal failed (after retries)"
        exit 10
    fi

    # Parse: extract <keyword>, <slug>, <concept> from the model's text
    # response. The text is wrapped in the CLI's stream-result envelope
    # (`{"type":"result","is_error":false,"result":"..."}`) so we first
    # peel off the envelope, then run three independent regex extractions
    # against the inner text. Each tag is recoverable on its own; if the
    # model truncates mid-output we still get whichever fields completed
    # before the truncation (vs. the old JSON parser which lost everything
    # when the trailing `}` was dropped).
    PARSED=$(python3 - "$PROPOSAL_FILE" <<'PY'
import json, re, sys
raw = open(sys.argv[1]).read().strip()
# Claude CLI may emit multiple JSON objects when a 429 rate-limit fires and
# the CLI auto-retries: line 1 is is_error:true, line 2 is the real result.
# Take the LAST is_error:false envelope.
inner = None
for line in raw.splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        obj, _ = json.JSONDecoder().raw_decode(line)
        if isinstance(obj, dict) and not obj.get("is_error"):
            inner = obj.get("result") if isinstance(obj.get("result"), str) else None
    except Exception:
        pass
if inner is None:
    # Fallback: the whole file might be a single multi-line JSON object.
    s = raw.find("{")
    if s >= 0:
        try:
            obj, _ = json.JSONDecoder().raw_decode(raw[s:])
            if isinstance(obj, dict) and not obj.get("is_error"):
                inner = obj.get("result") if isinstance(obj.get("result"), str) else None
        except Exception:
            pass
if inner is None:
    # Last resort: assume the file IS the model text (no CLI envelope).
    inner = raw
def grab(tag, text):
    m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, flags=re.DOTALL)
    if not m:
        return ""
    return m.group(1).strip()
kw = grab("keyword", inner)
slug = grab("slug", inner)
concept = grab("concept", inner)
if not kw or not slug:
    # Diagnostic: dump a short prefix of what we tried to parse so the log
    # tells us whether the model omitted the tags entirely or formatted them
    # in an unexpected way.
    snippet = inner.replace("\n", " ")[:240]
    print(f"ERR missing_fields keyword={bool(kw)} slug={bool(slug)} concept={bool(concept)} got={snippet!r}", file=sys.stderr)
    sys.exit(1)
# Tabs/newlines inside fields would corrupt the awk split downstream; flatten.
kw = re.sub(r"\s+", " ", kw)
slug = re.sub(r"\s+", "", slug)
concept = re.sub(r"\s+", " ", concept)
print(f"{kw}\t{slug}\t{concept}")
PY
)
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
