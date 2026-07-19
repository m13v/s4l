#!/bin/bash
# Social Autoposter - Original Twitter thread poster
#
# Picks one (project, topic_angle) target via pick_twitter_thread_target.py,
# which enforces:
#   1. Hard global cap of 3 original threads per UTC calendar day.
#   2. Per-(project, topic_angle) floor window (default 2 days).
#   3. Per-project inverse-share weighting (don't pile on one project).
#
# Then spawns a Claude session with the twitter-harness browser to research, draft, and post
# ONE original thread (1-6 tweets, chained as a Twitter thread).
#
# Called by launchd. See com.m13v.social-twitter-threads.plist.
# Mirror of skill/run-reddit-threads.sh; deviations are commented.

set -euo pipefail

[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

# Cycle ID for cross-cycle cost accounting (see run-twitter-cycle.sh for the
# same pattern). Stamps claude_sessions.cycle_id so get_run_cost.py --cycle-id
# reports per-cycle spend instead of bleeding across concurrent runs.
BATCH_ID="${BATCH_ID:-twthr-$(date +%Y%m%d-%H%M%S)}"
export BATCH_ID
export SA_CYCLE_ID="$BATCH_ID"

REPO_DIR="$HOME/social-autoposter"
CONFIG_FILE="$REPO_DIR/config.json"
SKILL_FILE="$REPO_DIR/SKILL.md"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run-twitter-threads-$(date +%Y-%m-%d_%H%M%S).log"

echo "=== Twitter Threads Run: $(date) ===" | tee "$LOG_FILE"
RUN_START_EPOCH=$(date +%s)

# Diagnostic trap (parallel to reddit version): log line + cmd before set -e exits.
trap 'rc=$?; echo "SCRIPT DIED line=$LINENO cmd=\"$BASH_COMMAND\" exit=$rc" | tee -a "$LOG_FILE" >&2' ERR

# Pipeline lock at top. Browser lock acquired later, just before the Claude/MCP step.
source "$REPO_DIR/skill/lock.sh"
# Harness-only browser bootstrap (twitter-agent path fully removed 2026-05-19).
# Sets MCP_CONFIG_FILE + BROWSER_INSTRUCTIONS for the Claude SDK call below.
source "$REPO_DIR/skill/lib/twitter-backend.sh"
acquire_lock "twitter-threads" 600

# Engagement styles
source "$REPO_DIR/skill/styles.sh"
STYLES_BLOCK=$(generate_styles_block twitter posting)

# Pick target. The picker enforces the daily cap; exit 3 = cap reached, exit 2 = no eligible angle.
set +e
TARGET_JSON=$(/usr/bin/python3 "$REPO_DIR/scripts/pick_twitter_thread_target.py" --json 2>&1)
PICK_RC=$?
set -e
if [ "$PICK_RC" -eq 3 ]; then
  echo "DAILY_CAP_REACHED: skipping this fire (3 threads per UTC day)." | tee -a "$LOG_FILE"
  exit 0
fi
if [ "$PICK_RC" -eq 2 ]; then
  echo "NO_ELIGIBLE_TARGET: every (project,angle) is inside its floor window. Stopping." | tee -a "$LOG_FILE"
  exit 0
fi
if [ "$PICK_RC" -ne 0 ]; then
  echo "PICKER_FAILED rc=$PICK_RC output=$TARGET_JSON" | tee -a "$LOG_FILE"
  exit 0
fi

PROJECT=$(echo "$TARGET_JSON" | /usr/bin/python3 -c "import sys,json; print(json.load(sys.stdin)['project']['name'])")
TOPIC_ANGLE=$(echo "$TARGET_JSON" | /usr/bin/python3 -c "import sys,json; print(json.load(sys.stdin)['topic_angle'])")
DAILY_COUNT=$(echo "$TARGET_JSON" | /usr/bin/python3 -c "import sys,json; print(json.load(sys.stdin)['daily_count_today'])")
DAILY_CAP=$(echo "$TARGET_JSON" | /usr/bin/python3 -c "import sys,json; print(json.load(sys.stdin)['daily_cap'])")

echo "Target: project=$PROJECT" | tee -a "$LOG_FILE"
echo "Angle:  $TOPIC_ANGLE"      | tee -a "$LOG_FILE"
echo "Daily:  $DAILY_COUNT/$DAILY_CAP posts today (UTC)" | tee -a "$LOG_FILE"

# Posting account
POST_ACCOUNT=$(/usr/bin/python3 -c "
import json
c = json.load(open('$CONFIG_FILE'))
print((c.get('accounts',{}).get('twitter',{}).get('handle','@m13v_')).lstrip('@'))
")

# Per-project context block (same JSON-driven shape as reddit version).
# Reads twitter_threads first, falls back to threads if a key is absent there.
export PROJECT_ENV="$PROJECT"
export CONFIG_PATH="$CONFIG_FILE"
CONTEXT_BLOCK=$(/usr/bin/python3 <<'PYEOF'
import json, datetime, os
CONFIG = os.environ['CONFIG_PATH']
name = os.environ['PROJECT_ENV']
c = json.load(open(CONFIG))
proj = next((p for p in c['projects'] if p['name'] == name), None)
if not proj:
    print("(project not found)")
    raise SystemExit(0)

tt = proj.get('twitter_threads') or {}
t  = proj.get('threads') or {}            # fallback for content_sources/dynamic_context
lp = proj.get('landing_pages') or {}

def first(*keys):
    """Return the first non-empty value across (tt, t) for any of the given keys."""
    for src in (tt, t):
        for k in keys:
            v = src.get(k)
            if v:
                return v
    return None

out = []
out.append(f"Project: {proj['name']}")
out.append(f"Description: {proj.get('description','').strip()}")
if proj.get('website'): out.append(f"Website: {proj['website']}")
if lp.get('base_url'):  out.append(f"Base URL: {lp['base_url']}")
if proj.get('content_angle'):
    out.append(f"\nContent angle: {proj['content_angle']}")

voice = proj.get('voice')
if voice:
    out.append(f"\nVoice tone: {voice.get('tone','')}")
    if voice.get('never'):
        out.append("Voice never: " + "; ".join(voice['never']))

# Dynamic day counter
dc = first('dynamic_context') or {}
day = dc.get('day_counter')
if day:
    base = day['base_count']
    ref = datetime.date.fromisoformat(day['ref_date'])
    days = (datetime.date.today() - ref).days
    count = base + days
    label = day.get('label','day count')
    out.append(f"\nLive {label}: {count}+")
for f in dc.get('static_facts') or []:
    out.append(f"- {f}")

# Source paths
out.append("\n## Product source (READ for context before drafting)")
repo = lp.get('repo','')
if repo:
    rp = os.path.expanduser(repo)
    status = "" if os.path.isdir(rp) else " [MISSING ON DISK]"
    out.append(f"- Website repo: {rp}{status}")
for s in lp.get('product_source') or []:
    p = os.path.expanduser(s.get('path',''))
    status = "" if os.path.isdir(p) else " [MISSING]"
    desc = s.get('description','').strip()
    out.append(f"- {p}{status}\n  {desc}")

# content_sources
cs = first('content_sources') or {}
if cs.get('guide_dir'):
    gd = os.path.expanduser(cs['guide_dir'])
    out.append(f"\nGuide dir (read page.tsx files here for specific detail): {gd}")
if cs.get('link_base'):
    out.append(f"Link base for any URL you include: {cs['link_base']}")
if cs.get('readme_url'):
    out.append(f"README url: {cs['readme_url']}")
if cs.get('read_instructions'):
    out.append(cs['read_instructions'])

print("\n".join(out))
PYEOF
)

echo "--- Context block ---" | tee -a "$LOG_FILE"
echo "$CONTEXT_BLOCK"        | tee -a "$LOG_FILE"
echo "---------------------" | tee -a "$LOG_FILE"

# Prompt context loaders below all route through scripts/twitter_threads_helper.py
# (HTTP /api/v1/posts) instead of three direct psql one-liners as of 2026-05-18.
# Filters preserved byte-equivalent: thread_url = our_url, project + window +
# status WHERE clauses. Post 2026-05-23, the '(mention - no original post)'
# placeholder rows no longer live in `posts` (they moved to the dedicated
# `mentions` table), so the legacy NOT ILIKE '(mention%' guard was dropped.

# Recent originals by us in last 14 days for THIS project (avoid repeats).
RECENT_POSTS=$(python3 "$REPO_DIR/scripts/twitter_threads_helper.py" \
  recent-posts --project "$PROJECT" --days 14 --limit 10 \
  2>/dev/null || echo "(api error)")

# Recent engagement styles for this project on Twitter.
RECENT_STYLES=$(python3 "$REPO_DIR/scripts/twitter_threads_helper.py" \
  recent-styles --project "$PROJECT" --limit 5 \
  2>/dev/null || echo "(api error)")

# Top performers (tone calibration) — composite (upvotes + 3*comments + views/100).
TOP_POSTS=$(python3 "$REPO_DIR/scripts/twitter_threads_helper.py" \
  top-posts --project "$PROJECT" --limit 8 \
  2>/dev/null || echo "(api error)")

# Structured output schema. The model returns a "tweets" array (1-6 items)
# representing a single chained Twitter thread, plus the same compliance fields
# as the reddit version.
RESULT_SCHEMA='{"type":"object","properties":{"research_files_read":{"type":"array","items":{"type":"string"}},"topic_angle":{"type":"string"},"engagement_style":{"type":"string"},"tweets":{"type":"array","minItems":1,"maxItems":6,"items":{"type":"string","maxLength":280},"description":"1-6 chained tweets. First is the hook, each <=280 chars."},"permalink":{"type":["string","null"],"description":"URL of the FIRST tweet in the thread, or null if aborted"},"abort_reason":{"type":["string","null"]},"source_summary":{"type":"string","description":"Rich summary: (a) topic angle and why, (b) source files read, (c) specific details used"}},"required":["research_files_read","topic_angle","engagement_style","tweets","permalink","abort_reason","source_summary"]}'

# Pre-generate session id so the prompt's inline INSERT can stamp it.
export CLAUDE_SESSION_ID=$(uuidgen | tr 'A-Z' 'a-z')

# --- Phase 0: link resolution (mirrors run-twitter-cycle.sh Phase 2b-gen) ---
# Resolve LINK_URL + LINK_SOURCE BEFORE acquiring the browser lock, so the
# 10-40 min generate_page.py mint (when A/B lands in the gen lane) does not
# block other twitter pipelines on the browser. Reuses scripts/twitter_gen_links.py
# unchanged; we feed it a single-candidate plan synthesised from the
# (project, topic_angle) the picker already chose.
THREADS_PLAN_FILE="/tmp/twitter_threads_link_$(date +%s)_$$.json"
PROJECT_ENV="$PROJECT" TOPIC_ANGLE_ENV="$TOPIC_ANGLE" CONFIG_PATH="$CONFIG_FILE" \
PLAN_FILE_ENV="$THREADS_PLAN_FILE" \
/usr/bin/python3 <<'PYEOF' 2>&1 | tee -a "$LOG_FILE"
import json, os, re

CONFIG = os.environ['CONFIG_PATH']
project = os.environ['PROJECT_ENV']
topic_angle = os.environ['TOPIC_ANGLE_ENV']
plan_file = os.environ['PLAN_FILE_ENV']

c = json.load(open(CONFIG))
proj = next((p for p in c['projects'] if p['name'] == project), None)
lp = (proj or {}).get('landing_pages') or {}
has_lp = bool(lp.get('repo') and lp.get('base_url'))

def slugify(s):
    s = re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')
    return s[:50].rstrip('-')

slug = slugify(topic_angle)
keyword = topic_angle.strip()

plan = {"candidates": [{
    "candidate_id": 0,
    "matched_project": project,
    "has_landing_pages": has_lp,
    "link_keyword": keyword,
    "link_slug": slug,
}]}
with open(plan_file, 'w') as f:
    json.dump(plan, f)
print(f"[link-prep] project={project!r} has_lp={has_lp} slug={slug!r}")
PYEOF

echo "[link-gen] running twitter_gen_links.py for ${PROJECT} (no browser lock held)..." | tee -a "$LOG_FILE"
/usr/bin/python3 "$REPO_DIR/scripts/twitter_gen_links.py" --plan "$THREADS_PLAN_FILE" 2>&1 | tee -a "$LOG_FILE"
GEN_EXIT=${PIPESTATUS[0]:-1}
if [ "$GEN_EXIT" -ne 0 ]; then
  echo "[link-gen] WARN: twitter_gen_links.py exited $GEN_EXIT, continuing with whatever link it set" | tee -a "$LOG_FILE"
fi

LINK_URL=$(/usr/bin/python3 -c "
import json
try:
    p = json.load(open('$THREADS_PLAN_FILE'))
    print((p.get('candidates') or [{}])[0].get('link_url') or '')
except Exception:
    print('')
")
LINK_SOURCE=$(/usr/bin/python3 -c "
import json
try:
    p = json.load(open('$THREADS_PLAN_FILE'))
    print((p.get('candidates') or [{}])[0].get('link_source') or '')
except Exception:
    print('')
")
rm -f "$THREADS_PLAN_FILE"
export LINK_URL LINK_SOURCE
echo "[link-gen] resolved LINK_URL='${LINK_URL}' LINK_SOURCE='${LINK_SOURCE}'" | tee -a "$LOG_FILE"

# Build the prompt rule for tweet 1's link. Mandatory when we resolved a URL,
# omitted otherwise (e.g. project has no website AND no landing_pages config).
if [ -n "$LINK_URL" ]; then
  LINK_LEN=${#LINK_URL}
  TWEET1_BUDGET=$(( 280 - LINK_LEN - 1 ))
  LINK_RULE="MANDATORY LINK: end your FIRST tweet with EXACTLY this URL preceded by a single space: ${LINK_URL}
     - Reserve room: tweet 1 text BEFORE the URL must be <= ${TWEET1_BUDGET} chars (the URL itself is ${LINK_LEN} chars).
     - Do NOT paraphrase, shorten, or wrap the URL. Do NOT include the link in tweets 2+ (X downranks link-heavy threads).
     - The shell verifies post-flight that tweets[0] ends with this exact URL; if not, the post is logged with link_source='link_missing'."
else
  LINK_RULE="No link: this project has no website or landing_pages configured this cycle. Do NOT include any URL in any tweet."
fi
export LINK_RULE

# Acquire browser lock right before MCP step.
acquire_lock "twitter-browser" 3600
ensure_twitter_browser_for_backend 2>&1 | tee -a "$LOG_FILE" || true
_ensure_rc="${PIPESTATUS[0]}"
[ "$_ensure_rc" != "0" ] && echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] WARNING: twitter-harness bootstrap failed (rc=$_ensure_rc); continuing anyway, downstream browser calls may fail" | tee -a "$LOG_FILE"

# Campaign wiring: pre-submit suffix injection (Twitter has no edit API, so we
# can't mirror reddit-threads' post-submit edit pattern; instead we instruct the
# model to end the LAST tweet with the literal suffix and verify post-flight).
# Dice are rolled in Python before Claude runs. campaigns.posts_made is bumped
# only on a verified-suffix outcome (parallels reddit-threads' verified-edit
# semantics). Multi-suffix concatenation matches post_reddit.py behavior.
# HTTP-only lane (2026-06-01): active twitter campaigns come from the s4l.ai
# API (/api/v1/campaigns?status=active&platform=twitter&has_suffix=true&
# with_budget_remaining=true), mirroring post_reddit.load_active_reddit_campaigns.
# No DATABASE_URL, no db.get_conn(), no fallback.
CAMPAIGN_ENV=$(/usr/bin/python3 <<'PYEOF'
import json, os, random, sys
sys.path.insert(0, os.path.join(os.environ.get("HOME",""), "social-autoposter", "scripts"))
from http_api import api_get

resp = api_get("/api/v1/campaigns", query={
    "status": "active",
    "platform": "twitter",
    "has_suffix": "true",
    "with_budget_remaining": "true",
    "limit": 500,
})
rows = ((resp or {}).get("data") or {}).get("campaigns") or []

applied_ids = []
suffix_parts = []
for r in rows:
    cid = int(r["id"])
    suffix = r.get("suffix")
    rate = float(r.get("sample_rate") if r.get("sample_rate") is not None else 1.0)
    if suffix and random.random() < rate:
        applied_ids.append(cid)
        suffix_parts.append(suffix)

print(json.dumps({
    "ids_csv": ",".join(str(i) for i in applied_ids),
    "suffix": "".join(suffix_parts),
}))
PYEOF
)
CAMPAIGN_IDS=$(/usr/bin/python3 -c "import json,sys; print(json.loads(sys.stdin.read())['ids_csv'])" <<< "$CAMPAIGN_ENV")
CAMPAIGN_SUFFIX=$(/usr/bin/python3 -c "import json,sys; print(json.loads(sys.stdin.read())['suffix'])" <<< "$CAMPAIGN_ENV")

if [ -n "$CAMPAIGN_IDS" ]; then
  # Build the prompt block. The suffix is wrapped in backticks for the model's
  # benefit but the model must NOT include the backticks in the actual tweet.
  CAMPAIGN_BLOCK="## ACTIVE CAMPAIGN ATTRIBUTION (mandatory, non-negotiable)

The LAST tweet of your thread MUST end with EXACTLY this literal suffix (preserve any leading whitespace, do not include the surrounding backticks):
\`${CAMPAIGN_SUFFIX}\`

Do not paraphrase, translate, capitalize, punctuate, or wrap it in quotes.
Reserve enough characters in the LAST tweet so the suffix fits within the 280-char cap.
The same text must appear (a) at the end of the last entry in your tweets array AND (b) at the end of the actual posted tweet (these will be verified)."
  echo "[campaign-twitter-thread] applying campaign_ids=${CAMPAIGN_IDS} suffix='${CAMPAIGN_SUFFIX}'" | tee -a "$LOG_FILE"
else
  CAMPAIGN_BLOCK=""
  echo "[campaign-twitter-thread] no active campaigns fired (or none active)" | tee -a "$LOG_FILE"
fi

# Capture Claude output to a temp file so non-zero exit doesn't swallow stderr.
CLAUDE_TMP=$(mktemp)
set +e
"$REPO_DIR/scripts/run_claude.sh" "run-twitter-threads" --strict-mcp-config --mcp-config "$MCP_CONFIG_FILE" -p --output-format json --json-schema "$RESULT_SCHEMA" "${BROWSER_INSTRUCTIONS}

You are posting an ORIGINAL Twitter thread for the ${PROJECT} project as @${POST_ACCOUNT}.

## Config & Rules
Read $SKILL_FILE for content rules and anti-AI-detection checklist.
You may also open $CONFIG_FILE for the full project block if you need anything not summarized below.

## Target
Project: ${PROJECT}
Topic angle (use this, do NOT pick a different one): ${TOPIC_ANGLE}

## Project context (live-assembled)
${CONTEXT_BLOCK}

${STYLES_BLOCK}

## Recent originals by us for ${PROJECT} (last 14d, DO NOT recycle phrasing or closer)
Each entry shows: first 120 chars |ENDING| last 80 chars. Vary your closer.
${RECENT_POSTS}

## Recent engagement styles for ${PROJECT} on Twitter (avoid repeating back-to-back)
${RECENT_STYLES}

## Top performing ${PROJECT} originals (match tone)
${TOP_POSTS}

${CAMPAIGN_BLOCK}

## Workflow

1. RESEARCH (required): Read the product source paths listed in the context block. Pull 1-2 concrete, specific details from the source code or docs to anchor the thread. Generic threads get ignored.

2. SCAN THE TIMELINE: Navigate to https://x.com/home using the navigate tool from the BROWSER BACKEND block above to get a quick read on what is being said in our space today.
   - Read 5-10 recent tweets from accounts in adjacent topics (other indie devs, AI tooling, macOS automation, whatever fits the project).
   - Note the current vocabulary, hot takes, and any thread that is getting unusually high engagement.
   - Close the tab.

3. DRAFT the thread.
   - 1 to 6 tweets. Each <= 280 characters (hard cap). The first tweet must work as a standalone hook (people may only see that one).
   - Use the assigned topic_angle (above). Pick an engagement_style from the styles list that fits and is NOT one of the last 3 used for this project.
   - No em dashes anywhere. Commas, periods, plain '-' only.
   - No hashtag spam. At most ONE hashtag total in the entire thread, only if it is genuinely the topical tag people search.
   - No emojis at the start of a tweet. At most one per tweet, and only if it adds meaning.
   - Lowercase first character on most tweets feels natural on X. Do not uniformly lowercase every sentence (that is an AI tell). Mix it.
   - At least one imperfection (sentence fragment, aside, run-on) somewhere in the thread.
   - Ground at least one claim in a specific detail from the source you read in step 1.
   - VARY YOUR CLOSER. Banned closers: 'curious if anyone', 'anyone else', 'thoughts?', 'has anyone'. Sometimes end with a statement, sometimes mid-thought, sometimes a specific question.
   - ${LINK_RULE}

4. POST via the browser tools from the BROWSER BACKEND block above:
   - Navigate to https://x.com/compose/post.
   - Fill the first tweet into the textarea selected by [data-testid='tweetTextarea_0']. If the contenteditable does not accept .value=, use the type tool from the BROWSER BACKEND block to type the text directly into the focused element.
   - For each subsequent tweet (if any): click the button with data-testid='addButton' (the small '+' that appends a new tweet to the chain), then fill its textarea. The new textarea will be data-testid='tweetTextarea_1', then 'tweetTextarea_2', etc.
   - When all tweets are filled, click the button with data-testid='tweetButton' (label varies between 'Post' and 'Post all'). Wait 4 seconds.
   - Capture the URL of the FIRST posted tweet:
       - Navigate to https://x.com/${POST_ACCOUNT} and read the most recent pinned-or-top tweet's permalink (browser_evaluate: document.querySelector('article a[href*=\"/status/\"]').href). Confirm its text matches the first tweet you posted.
   - Close the tab.

5. DO NOT touch the database. The shell wrapper handles the INSERT after you return.
   IMPORTANT: tweets, permalink, engagement_style, source_summary in your JSON output are what get logged. Make source_summary rich.

6. Return the structured JSON output. Every field is required. permalink = URL of the first tweet if posted, null if aborted. tweets array must contain the EXACT text of each tweet posted (no markdown, no additions).

CRITICAL: NEVER use em dashes. Use commas, plain hyphens, or separate sentences.
CRITICAL: Each tweet <=280 chars. The schema enforces this; do not exceed.
CRITICAL: Use ONLY the browser tools listed in the BROWSER BACKEND block above (the Twitter-dedicated MCP for this run). NEVER use generic mcp__playwright-extension__*, mcp__isolated-browser__*, or mcp__macos-use__* tools.
CRITICAL: Close browser tabs after each navigation (browser_tabs action 'close').
CRITICAL: If a browser call times out, wait 30s and retry up to 3 times." > "$CLAUDE_TMP" 2>&1
CLAUDE_RC=$?
set -e
CLAUDE_OUTPUT=$(cat "$CLAUDE_TMP")
rm -f "$CLAUDE_TMP"

echo "$CLAUDE_OUTPUT" | tee -a "$LOG_FILE"
if [ "$CLAUDE_RC" -ne 0 ]; then
  echo "RUN_CLAUDE_NONZERO_EXIT rc=$CLAUDE_RC (output above is full stderr+stdout)" | tee -a "$LOG_FILE"
fi

# Extract structured_output. claude -p --output-format json wraps results.
PARSED=$(/usr/bin/python3 -c "
import json,sys
try:
    raw = sys.stdin.read()
    d, _ = json.JSONDecoder().raw_decode(raw)
    so = d.get('structured_output') or d
    print(json.dumps(so))
except Exception as e:
    print(json.dumps({'_parse_error': str(e)}))
" <<< "$CLAUDE_OUTPUT" 2>/dev/null)

PERMALINK=$(/usr/bin/python3 -c "import json,sys; r=json.loads(sys.stdin.read()); print(r.get('permalink') or 'null')" <<< "$PARSED" 2>/dev/null)
ABORT_REASON=$(/usr/bin/python3 -c "import json,sys; r=json.loads(sys.stdin.read()); print(r.get('abort_reason') or '')" <<< "$PARSED" 2>/dev/null)

# Step compliance summary
/usr/bin/python3 -c "
import json,sys
r = json.loads(sys.stdin.read())
if '_parse_error' in r:
    print(f'Step compliance: PARSE ERROR ({r[\"_parse_error\"]})')
else:
    files = r.get('research_files_read', [])
    tweets = r.get('tweets', [])
    style = r.get('engagement_style', '?')
    over = [i for i,t in enumerate(tweets) if len(t) > 280]
    print(f'Step compliance: research={len(files)} files, tweets={len(tweets)}, style={style}, over_280={over or \"none\"}')
" <<< "$PARSED" 2>/dev/null | tee -a "$LOG_FILE"

if [ "$PERMALINK" != "null" ] && [ "$PERMALINK" != "" ] && [ "$PERMALINK" != "PARSE_ERROR" ]; then
  echo "POSTED: $PERMALINK" | tee -a "$LOG_FILE"

  # Authoritative DB INSERT. Same pattern as reddit threads runner.
  PARSED="$PARSED" \
  CLAUDE_SESSION_ID="$CLAUDE_SESSION_ID" \
  PROJECT_ENV="$PROJECT" \
  POST_ACCOUNT="$POST_ACCOUNT" \
  REPO_DIR="$REPO_DIR" \
  CAMPAIGN_IDS="$CAMPAIGN_IDS" \
  CAMPAIGN_SUFFIX="$CAMPAIGN_SUFFIX" \
  LINK_URL="$LINK_URL" \
  LINK_SOURCE="$LINK_SOURCE" \
  /usr/bin/python3 <<'PYEOF' 2>&1 | tee -a "$LOG_FILE" || true
import json, os, subprocess, sys
sys.path.insert(0, os.path.join(os.environ["REPO_DIR"], "scripts"))
from http_api import api_post

parsed = json.loads(os.environ.get("PARSED") or "{}")
permalink = parsed.get("permalink") or ""
tweets    = parsed.get("tweets") or []
summary   = parsed.get("source_summary", "")
style     = parsed.get("engagement_style", "") or None
session   = os.environ.get("CLAUDE_SESSION_ID") or None
project   = os.environ.get("PROJECT_ENV", "")
account   = os.environ.get("POST_ACCOUNT", "")
link_url    = (os.environ.get("LINK_URL") or "").strip()
link_source = (os.environ.get("LINK_SOURCE") or "").strip() or None

if not permalink or not tweets:
    print("[db-insert] SKIP - empty permalink or tweets in structured_output")
    sys.exit(0)

# Verify post-flight that tweet 1 ends with the resolved link. The cycle
# pipeline gets this for free (twitter_browser.py concatenates the URL); for
# threads the LLM does the appending, so we audit and downgrade link_source
# when the model dropped the link.
if link_url:
    first_tweet = (tweets[0] or "").rstrip()
    if first_tweet.endswith(link_url):
        print(f"[link-verify] OK first tweet ends with {link_url!r}")
    else:
        print(f"[link-verify] MISS expected suffix {link_url!r} not at end of tweet1; "
              f"tail={first_tweet[-80:]!r}")
        link_source = "link_missing"

# Stitch tweets into our_content with double-newline separators so downstream
# stats/refresh queries treat the whole thread as one row.
body = "\n\n".join(t.strip() for t in tweets if t and t.strip())
# Twitter doesn't have a separate title; use the first tweet's first 100 chars
# so dashboard listings have something readable.
title = (tweets[0] or "")[:100]

# HTTP-only lane (2026-06-01): the authoritative post log goes through the
# s4l.ai API (POST /api/v1/posts). No DATABASE_URL, no db.get_conn(). The
# endpoint dedups on (platform, thread_url) server-side and returns 409 with
# existing_post_id, replacing the old SELECT idempotency guard (thread_url ==
# our_url == permalink here, so dedup is equivalent to the old our_url check).
resp = api_post("/api/v1/posts", {
    "platform": "twitter",
    "thread_url": permalink,
    "thread_author": account,
    "thread_title": title,
    "thread_content": body,
    "our_url": permalink,
    "our_content": body,
    "our_account": account,
    "source_summary": summary,
    "project": project,
    "engagement_style": style,
    "status": "active",
    "claude_session_id": session,
    "link_source": link_source,
}, ok_on_conflict=True)

if not resp.get("ok", True) and (resp.get("error") or {}).get("code") == "duplicate_thread":
    existing_id = ((resp.get("error") or {}).get("details") or {}).get("existing_post_id")
    print(f"[db-insert] SKIP — post {permalink} already in DB as id={existing_id}")
    sys.exit(0)

post_id = (resp.get("data") or {}).get("post", {}).get("id")
print(f"[db-insert] OK — inserted posts.id={post_id} for {permalink}")

# Campaign verification gate. Twitter has no edit API so we cannot fix the
# tweet after posting; instead we verify the model honored the suffix
# instruction and bump the counter only if it did. Mirrors the verified-edit
# semantics in skill/run-reddit-threads.sh.
campaign_ids_csv = (os.environ.get("CAMPAIGN_IDS") or "").strip()
campaign_suffix  = os.environ.get("CAMPAIGN_SUFFIX") or ""
if campaign_ids_csv and campaign_suffix:
    last_tweet = (tweets[-1] or "").rstrip()
    expected   = campaign_suffix.rstrip()
    if last_tweet.endswith(expected):
        bump = os.path.join(os.environ["REPO_DIR"], "scripts", "campaign_bump.py")
        for cid in [c for c in campaign_ids_csv.split(",") if c.strip()]:
            try:
                subprocess.run(
                    ["python3", bump,
                     "--table", "posts", "--id", str(post_id),
                     "--campaign-id", cid.strip()],
                    capture_output=True, text=True, timeout=15,
                )
            except Exception as e:
                print(f"[campaign-twitter-thread] WARNING: campaign_bump failed (id={post_id} c={cid}): {e}")
        print(f"[campaign-twitter-thread] OK — last tweet ends with suffix, campaigns {campaign_ids_csv} bumped on post {post_id}")
    else:
        # Verification failed: model did not add the suffix as instructed.
        # Leave the post untagged. campaign_id stays NULL, so the row joins
        # the control bucket for A/B purposes (parallels reddit-threads'
        # degraded path on edit-thread failure).
        print(f"[campaign-twitter-thread] WARNING: last tweet does not end with expected suffix; post {post_id} stays untagged. last_tail={last_tweet[-80:]!r} expected={expected!r}")
PYEOF

elif [ -n "$ABORT_REASON" ] && [ "$ABORT_REASON" != "PARSE_ERROR" ]; then
  echo "ABORTED: $ABORT_REASON" | tee -a "$LOG_FILE"
else
  echo "UNKNOWN OUTCOME (check JSON output above)" | tee -a "$LOG_FILE"
fi

# Surface this run in the dashboard's Job History under "Post Threads · Twitter".
# Script name `thread_twitter` is what bin/server.js classifyScript() matches.
ELAPSED=$(( $(date +%s) - RUN_START_EPOCH ))
if [ "$PERMALINK" != "null" ] && [ "$PERMALINK" != "" ] && [ "$PERMALINK" != "PARSE_ERROR" ]; then
  POSTED_CT=1; SKIPPED_CT=0; FAILED_CT=0
elif [ -n "$ABORT_REASON" ] && [ "$ABORT_REASON" != "PARSE_ERROR" ]; then
  POSTED_CT=0; SKIPPED_CT=1; FAILED_CT=0
else
  POSTED_CT=0; SKIPPED_CT=0; FAILED_CT=1
fi
_COST=$(/usr/bin/python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START_EPOCH" --scripts "run-twitter-threads" 2>/dev/null || echo "0.0000")
/usr/bin/python3 "$REPO_DIR/scripts/log_run.py" --script "thread_twitter" --posted "$POSTED_CT" --skipped "$SKIPPED_CT" --failed "$FAILED_CT" --cost "$_COST" --elapsed "$ELAPSED" || true

echo "=== Run complete: $(date) ===" | tee -a "$LOG_FILE"
find "$LOG_DIR" -name "run-twitter-threads-*.log" -mtime +14 -delete 2>/dev/null || true
