#!/bin/bash
#
# DataForSEO pipeline orchestrator.
# Picks the next unscored or pending keyword (generated via DataForSEO keyword
# research) for a product, scores it via SERP analysis, and if it passes the
# threshold, triggers page generation in the product's website repo.
#
# Parallel to run_gsc_pipeline.sh which generates pages from real GSC queries.
# This one hunts for new opportunities; the GSC one captures proven demand.
#
# All state is stored in Postgres (seo_keywords table).
#
# Usage:
#   ./run_serp_pipeline.sh <product_name> [--score-only] [--generate-only]
#
# Scoring is inline Claude. Page generation is delegated to generate_page.py
# (the unified generator shared with run_gsc_pipeline.sh). No templates —
# the generator uses a creative brief prompt and discovers components
# dynamically in the target repo.
#
# Requires: python3, claude CLI, psycopg2
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG="$ROOT_DIR/config.json"
LOCK_DIR="$SCRIPT_DIR/.locks"
DB="python3 $SCRIPT_DIR/db_helpers.py"
GENERATOR="python3 $SCRIPT_DIR/generate_page.py"

# Retry wrapper for `claude` (guards against auto-update unlink window).
# shellcheck source=./claude_helpers.sh
source "$SCRIPT_DIR/claude_helpers.sh"

PRODUCT="${1:?Usage: $0 <product_name> [--score-only] [--generate-only]}"
MODE="${2:-full}"  # full, --score-only, --generate-only

PRODUCT_LOWER=$(echo "$PRODUCT" | tr '[:upper:]' '[:lower:]')
LOCK_FILE="$LOCK_DIR/$PRODUCT_LOWER.lock"
LOG_DIR="$SCRIPT_DIR/logs/$PRODUCT_LOWER"

mkdir -p "$LOCK_DIR" "$LOG_DIR"

# --- Lock ---
if [ -f "$LOCK_FILE" ]; then
    LOCK_AGE=$(( $(date +%s) - $(stat -f %m "$LOCK_FILE" 2>/dev/null || stat -c %Y "$LOCK_FILE" 2>/dev/null) ))
    if [ "$LOCK_AGE" -gt 1800 ]; then
        echo "Stale lock (${LOCK_AGE}s old), removing"
        rm -f "$LOCK_FILE"
    else
        echo "Pipeline already running for $PRODUCT (lock age: ${LOCK_AGE}s)"
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
        print(p.get('website', ''))
        break
")

DIFFERENTIATOR=$(python3 -c "
import json
with open('$CONFIG') as f:
    c = json.load(f)
for p in c.get('projects', []):
    if p['name'].lower() == '$PRODUCT_LOWER':
        print(p.get('differentiator', ''))
        break
")

if [ -z "$REPO_PATH" ]; then
    echo "Error: no landing_pages.repo configured for $PRODUCT in config.json"
    exit 1
fi

if [ ! -d "$REPO_PATH" ]; then
    echo "Error: repo not found at $REPO_PATH"
    exit 1
fi

echo "=== SERP Pipeline: $PRODUCT ==="
echo "  Repo: $REPO_PATH"
echo "  Website: $WEBSITE"
echo ""

# --- Step 1: Pick next keyword from Postgres ---
NEXT=$($DB pick "$PRODUCT")

if [ "$NEXT" = "NONE" ] || [ "$NEXT" = "null" ]; then
    echo "No keywords to process. Generate more with: python3 generate_keywords.py $PRODUCT"
    exit 0
fi

KEYWORD=$(echo "$NEXT" | python3 -c "import json,sys; print(json.load(sys.stdin)['keyword'])")
SLUG=$(echo "$NEXT" | python3 -c "import json,sys; print(json.load(sys.stdin)['slug'])")
STATUS=$(echo "$NEXT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status','unscored'))")

# If --generate-only, skip unscored keywords
if [ "$MODE" = "--generate-only" ] && [ "$STATUS" = "unscored" ]; then
    echo "No pending keywords to generate. Score some first."
    exit 0
fi

echo "Next keyword: $KEYWORD (slug: $SLUG, status: $STATUS)"

TIMESTAMP=$(date -u +%Y%m%d-%H%M%S)
LOG_FILE="$LOG_DIR/${TIMESTAMP}_${SLUG}.log"

# --- Step 2: Score via SERP (if unscored) ---
if [ "$STATUS" = "unscored" ] && [ "$MODE" != "--generate-only" ]; then
    echo ""
    echo "--- SERP Scoring ---"

    # Mark as scoring in Postgres
    $DB update "$PRODUCT" "$KEYWORD" scoring

    # Run Claude to score the SERP (with retry wrapper for auto-update races).
    #
    # Output protocol: XML-tagged fields, NOT a single JSON object. The prior
    # JSON-blob format was vulnerable to the same truncation failure that
    # broke Terminator in run_top_pages_pipeline.sh on 2026-05-07: the model
    # dropped the closing `}` and the `find('{')...rfind('}')` parser lost
    # the whole envelope. Independently delimited XML tags are recoverable
    # on their own, three regex extractions, no nested-JSON walk.
    claude_with_retry -p "You are a SERP analyst. Score this keyword for the product '$PRODUCT'.

Product: $PRODUCT
Website: $WEBSITE
Differentiator: $DIFFERENTIATOR

Keyword to score: \"$KEYWORD\"

Use WebSearch to analyze the SERP for this keyword. Score 3 signals (0-2 each):

Signal 1: Product Angle Gap (40% weight)
Search: \"$KEYWORD $PRODUCT_LOWER\" or similar
- Score 2: No pages specifically address this from $PRODUCT's angle ($DIFFERENTIATOR)
- Score 1: 1-2 generic pages exist
- Score 0: Multiple competitors already cover this well

Signal 2: Result Quality Gap (35% weight)
Search: \"$KEYWORD\"
- Score 2: Top results are thin (<500 words), outdated (>1 year), or off-topic
- Score 1: Decent content but lacks depth or specificity
- Score 0: Comprehensive, authoritative pages already exist

Signal 3: Commercial Fit (25% weight)
- Score 2: Exactly what $PRODUCT does
- Score 1: Moderate fit, some caveats
- Score 0: Poor fit for the product

OUTPUT FORMAT (STRICT, read carefully):
Respond with EXACTLY these five XML tags, on separate lines, in this order, and nothing else. No preamble. No JSON. No code fences. No trailing commentary. Each field is independently parsed; do not nest tags or wrap them in any container.

  <signal1>0|1|2</signal1>
  <signal2>0|1|2</signal2>
  <signal3>0|1|2</signal3>
  <score>weighted composite e.g. 1.45</score>
  <notes>1-2 sentence SERP observation</notes>
" --output-format json 2>"$LOG_FILE" | tee "$LOG_FILE.score"

    # Parse XML-tagged score and update Postgres. Three steps:
    #   1. Peel off the CLI's stream-result envelope (`{type:result, result:"<text>"}`)
    #      to get the model's actual response text.
    #   2. Extract each tag with its own regex; no nested-JSON walk.
    #   3. Coerce numeric fields and update DB.
    SEO_SCORE_FILE="$LOG_FILE.score" SEO_PRODUCT="$PRODUCT" SEO_KEYWORD="$KEYWORD" SEO_SCRIPT_DIR="$SCRIPT_DIR" \
    python3 -c "
import json, re, sys, os
sys.path.insert(0, os.environ['SEO_SCRIPT_DIR'])
from db_helpers import update_status

product = os.environ['SEO_PRODUCT']
keyword = os.environ['SEO_KEYWORD']

def extract_inner(raw):
    # Iterate every line; take the LAST is_error:false envelope (skips 429
    # auto-retry's first error line). Falls back to whole-blob, then to raw.
    inner = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(line)
            if isinstance(obj, dict):
                if obj.get('is_error'):
                    raise RuntimeError(f'Claude error: {obj.get(\"result\",\"unknown\")}')
                if isinstance(obj.get('result'), str):
                    inner = obj['result']
        except json.JSONDecodeError:
            pass
    if inner is None:
        s = raw.find('{')
        if s >= 0:
            try:
                obj, _ = json.JSONDecoder().raw_decode(raw[s:])
                if isinstance(obj, dict):
                    if obj.get('is_error'):
                        raise RuntimeError(f'Claude error: {obj.get(\"result\",\"unknown\")}')
                    if isinstance(obj.get('result'), str):
                        inner = obj['result']
            except json.JSONDecodeError:
                pass
    return inner if inner is not None else raw

def grab(tag, text):
    m = re.search(rf'<{tag}>\s*(.*?)\s*</{tag}>', text, flags=re.DOTALL)
    return m.group(1).strip() if m else ''

def to_int(v, default=0):
    try:
        return int(float(v))
    except Exception:
        return default

def to_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default

try:
    raw = open(os.environ['SEO_SCORE_FILE']).read().strip()
    inner = extract_inner(raw)

    s1 = to_int(grab('signal1', inner))
    s2 = to_int(grab('signal2', inner))
    s3 = to_int(grab('signal3', inner))
    score_raw = grab('score', inner)
    notes = grab('notes', inner)

    if score_raw:
        score = to_float(score_raw)
    else:
        # Compute from signals if score tag was omitted/garbled.
        score = round(s1 * 0.40 * 2 + s2 * 0.35 * 2 + s3 * 0.25 * 2, 2) / 2

    if not (s1 or s2 or s3 or score_raw):
        snippet = inner.replace('\n', ' ')[:240]
        raise RuntimeError(f'No XML signals parsed; got={snippet!r}')

    status = 'pending' if score >= 1.5 else 'skip'  # raised 2026-05-05 from 1.0 to cut dead-weight pages

    update_status(product, keyword, status,
        score=score, signal1=s1, signal2=s2, signal3=s3, notes=notes)
    print(f'SCORED: {score} -> {status} (signals: {s1},{s2},{s3})')

except Exception as e:
    print(f'ERROR parsing score: {e}')
    update_status(product, keyword, 'unscored')
    sys.exit(1)
" || exit 1

    # Re-read status after scoring
    STATUS=$(SEO_PRODUCT="$PRODUCT" SEO_KEYWORD="$KEYWORD" SEO_SCRIPT_DIR="$SCRIPT_DIR" \
    python3 -c "
import sys, os
seo_dir = os.environ['SEO_SCRIPT_DIR']
sys.path.insert(0, os.path.join(os.path.dirname(seo_dir), 'scripts'))
from http_api import api_get, load_env
load_env()
resp = api_get('/api/v1/seo/keywords', query={'mode': 'get',
    'product': os.environ['SEO_PRODUCT'], 'keyword': os.environ['SEO_KEYWORD']})
row = resp.get('data')
print(row.get('status') if row else 'unknown')
")
fi

# --- Step 3: Generate page (if pending) ---
if [ "$STATUS" = "pending" ] && [ "$MODE" != "--score-only" ]; then
    echo ""
    echo "--- Page Generation ---"
    echo "Keyword: $KEYWORD"
    echo "Repo: $REPO_PATH"

    # Check if slug already exists as a done page
    SLUG_CHECK=$($DB check_slug "$PRODUCT" "$SLUG")
    if [ "$SLUG_CHECK" = "exists" ]; then
        echo "Slug '$SLUG' already exists as a completed page. Skipping."
        $DB update "$PRODUCT" "$KEYWORD" done
        exit 0
    fi

    # Mark as in_progress (generator expects caller to hold the row)
    $DB update "$PRODUCT" "$KEYWORD" in_progress

    # Hand off to the unified generator. It owns prompt, tool capture,
    # git verification, and state transition to done/pending.
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Invoking generate_page.py" >> "$LOG_FILE"
    $GENERATOR --product "$PRODUCT" --keyword "$KEYWORD" --slug "$SLUG" --trigger serp \
        2>&1 | tee -a "$LOG_FILE"
    GEN_EXIT=${PIPESTATUS[0]}

    if [ "$GEN_EXIT" -eq 0 ]; then
        echo ""
        echo "=== Page generated for: $KEYWORD ==="
    else
        echo ""
        echo "=== Generation failed (exit $GEN_EXIT); state reset to pending by generator ==="
    fi
else
    if [ "$STATUS" = "skip" ]; then
        echo "Keyword scored below threshold, skipping page generation."
    fi
fi

# --- Report from Postgres ---
echo ""
echo "=== Pipeline Report: $PRODUCT ==="
$DB report "$PRODUCT"
