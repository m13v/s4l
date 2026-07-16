#!/bin/bash
# Social Autoposter - Original Reddit thread poster (generalized)
#
# Picks one (project, subreddit) target via pick_thread_target.py,
# which enforces per-sub floor and banned-subreddit filtering, then spawns a
# Claude session with reddit-agent to research, draft, and post ONE original
# thread.
#
# Called by launchd every 6 hours. See com.m13v.social-reddit-threads.plist.

set -euo pipefail

[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

# Cycle ID for cross-cycle cost accounting (see run-reddit-search.sh / engage.sh
# for the same pattern). Stamps claude_sessions.cycle_id so get_run_cost.py
# --cycle-id reports per-cycle spend instead of bleeding across concurrent runs.
BATCH_ID="${BATCH_ID:-rdthr-$(date +%Y%m%d-%H%M%S)}"
export BATCH_ID
export SA_CYCLE_ID="$BATCH_ID"

REPO_DIR="$HOME/social-autoposter"
CONFIG_FILE="$REPO_DIR/config.json"
SKILL_FILE="$REPO_DIR/SKILL.md"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run-reddit-threads-$(date +%Y-%m-%d_%H%M%S).log"

echo "=== Reddit Threads Run: $(date) ===" | tee "$LOG_FILE"
RUN_START_EPOCH=$(date +%s)

# Match run-reddit-search.sh:38 / engage.sh:46. Without this, the 4 `log "..."`
# calls added in 5e41d96 (2026-05-10) fall through to macOS /usr/bin/log,
# which barfs `Unknown subcommand` and ERR-traps the run at exit=64 before
# the browser lease is ever acquired (silent kill since 2026-05-10).
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

# Diagnostic: log the failing line and command before set -e kills the script.
# Without this, silent deaths (e.g., Claude exits non-zero inside the $() below)
# leave only the context block in the log with no clue what killed the run.
trap 'rc=$?; echo "SCRIPT DIED line=$LINENO cmd=\"$BASH_COMMAND\" exit=$rc" | tee -a "$LOG_FILE" >&2' ERR

# Pipeline lock at top. The reddit-browser lock is acquired later, just
# before the Claude/MCP step that drives the browser, so peers can use the
# profile during our pre-Claude research + prompt build.
source "$REPO_DIR/skill/lock.sh"
# reddit-harness backend (2026-05-29). Sets MCP_CONFIG_FILE, BROWSER_INSTRUCTIONS,
# exports REDDIT_CDP_URL=:9557, provides ensure_reddit_browser_for_backend.
source "$REPO_DIR/skill/lib/reddit-backend.sh"
acquire_lock "reddit-threads" 600

# Load engagement styles.
# 2026-05-25: switched from generate_styles_block (which throws away the
# picker assignment) to the explicit s4l_pick_style + s4l_render_style_block
# pair. PICKED_STYLE/PICK_MODE flow into the DB-insert heredoc below as env
# vars so validate_or_register can coerce USE-mode drift back to the assigned
# style, matching post_reddit.py / twitter_post_plan.py / post_github.py
# semantics. Without this, the prompt's "pick from the list" instruction is
# unenforced and any drifted/invented name silently lands in posts.engagement_style.
source "$REPO_DIR/skill/styles.sh"
STYLE_ASSIGN_FILE=$(mktemp -t s4l_reddit_threads_style_XXXXXX.json)
s4l_pick_style reddit posting "$STYLE_ASSIGN_FILE" >/dev/null 2>>"$LOG_FILE" || true
PICKED_STYLE=$(/usr/bin/python3 -c "import json; print(json.load(open('$STYLE_ASSIGN_FILE')).get('style') or '')" 2>/dev/null || echo "")
PICK_MODE=$(/usr/bin/python3 -c "import json; print(json.load(open('$STYLE_ASSIGN_FILE')).get('mode','')) " 2>/dev/null || echo "")
STYLES_BLOCK=$(s4l_render_style_block "$STYLE_ASSIGN_FILE" reddit posting)
echo "engagement_style: picked='${PICKED_STYLE}' mode='${PICK_MODE}'" | tee -a "$LOG_FILE"

# RETRY-FROM-PENDING (added 2026-05-01 after r/AutoHotkey MCP-crash incident):
# Before paying for fresh research+drafting, check if a previously-aborted draft
# is sitting in pending_threads waiting for a retry. If so, pick it up and skip
# the research/drafting phase entirely. This reuses sunk Claude cost on prior runs.
RETRY_PAYLOAD=$(/usr/bin/python3 <<'PYEOF' 2>/dev/null
import sys, os, json
sys.path.insert(0, os.path.expanduser("~/social-autoposter/scripts"))
import pending_threads as pt
rows = pt.list_pending(project=None)
# Cap at 3 attempts before abandoning, so a perpetually-broken draft doesn't
# blackhole every run.
rows = [r for r in rows if (r.get("attempts") or 0) < 3]
if not rows:
    print("")
else:
    # Oldest first.
    r = rows[0]
    print(json.dumps(r))
PYEOF
)

if [ -n "$RETRY_PAYLOAD" ]; then
  RETRY_MODE=1
  PENDING_ID=$(echo "$RETRY_PAYLOAD" | /usr/bin/python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
  PROJECT=$(echo "$RETRY_PAYLOAD" | /usr/bin/python3 -c "import sys,json; print(json.load(sys.stdin)['project_name'])")
  SUBREDDIT=$(echo "$RETRY_PAYLOAD" | /usr/bin/python3 -c "import sys,json; print(json.load(sys.stdin)['subreddit'])")
  # Pull title, body, flair from the saved draft via the helper
  PENDING_FULL=$(/usr/bin/python3 "$REPO_DIR/scripts/pending_threads.py" get --id "$PENDING_ID")
  PENDING_TITLE=$(echo "$PENDING_FULL" | /usr/bin/python3 -c "import sys,json; print(json.load(sys.stdin).get('title',''))")
  PENDING_BODY=$(echo "$PENDING_FULL" | /usr/bin/python3 -c "import sys,json; print(json.load(sys.stdin).get('body',''))")
  PENDING_FLAIR=$(echo "$PENDING_FULL" | /usr/bin/python3 -c "import sys,json; print(json.load(sys.stdin).get('flair_target') or '')")
  PENDING_STYLE=$(echo "$PENDING_FULL" | /usr/bin/python3 -c "import sys,json; print(json.load(sys.stdin).get('engagement_style') or '')")
  PENDING_TOPIC=$(echo "$PENDING_FULL" | /usr/bin/python3 -c "import sys,json; print(json.load(sys.stdin).get('topic_angle') or '')")
  PENDING_SOURCE=$(echo "$PENDING_FULL" | /usr/bin/python3 -c "import sys,json; print(json.load(sys.stdin).get('source_summary') or '')")
  IS_OWN="False"  # default; not critical for retry
  echo "RETRY_MODE: pending_id=$PENDING_ID project=$PROJECT subreddit=$SUBREDDIT" | tee -a "$LOG_FILE"
else
  RETRY_MODE=0
  # Pick target
  TARGET_JSON=$(/usr/bin/python3 "$REPO_DIR/scripts/pick_thread_target.py" --json 2>&1) || {
    echo "NO_ELIGIBLE_TARGET: every eligible subreddit is inside its floor window. Stopping." | tee -a "$LOG_FILE"
    exit 0
  }

  PROJECT=$(echo "$TARGET_JSON" | /usr/bin/python3 -c "import sys,json; print(json.load(sys.stdin)['project']['name'])")
  SUBREDDIT=$(echo "$TARGET_JSON" | /usr/bin/python3 -c "import sys,json; print(json.load(sys.stdin)['subreddit'])")
  IS_OWN=$(echo "$TARGET_JSON" | /usr/bin/python3 -c "import sys,json; print(json.load(sys.stdin)['is_own_community'])")

  echo "Target: project=$PROJECT subreddit=$SUBREDDIT own_community=$IS_OWN" | tee -a "$LOG_FILE"
fi
SUB_SLUG=$(echo "$SUBREDDIT" | sed 's|^r/||I')

# Posting account (hardcoded for now; the only configured reddit account)
POST_ACCOUNT=$(/usr/bin/python3 -c "
import json
c = json.load(open('$CONFIG_FILE'))
print(c.get('accounts',{}).get('reddit',{}).get('username','Deep_Ad1959'))
")

# Build full per-project context block (JSON-driven so prompt stays compact)
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

t = proj.get('threads') or {}
lp = proj.get('landing_pages') or {}

out = []
out.append(f"Project: {proj['name']}")
out.append(f"Description: {proj.get('description','').strip()}")
if proj.get('website'): out.append(f"Website: {proj['website']}")
if lp.get('base_url'): out.append(f"Base URL: {lp['base_url']}")
if proj.get('content_angle'):
    out.append(f"\nContent angle: {proj['content_angle']}")

voice = proj.get('voice')
if voice:
    out.append(f"\nVoice tone: {voice.get('tone','')}")
    if voice.get('never'):
        out.append("Voice never: " + "; ".join(voice['never']))

# Dynamic day counter
dc = t.get('dynamic_context') or {}
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

# Topic angles
angles = t.get('topic_angles') or []
if angles:
    out.append("\nTopic angles to choose from:")
    for a in angles:
        out.append(f"- {a}")

# Source paths (SEO pipeline pattern)
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

# Threads content_sources
cs = t.get('content_sources') or {}
if cs.get('guide_dir'):
    gd = os.path.expanduser(cs['guide_dir'])
    out.append(f"\nGuide dir (read page.tsx files here for specific detail): {gd}")
if cs.get('link_base'):
    out.append(f"Link base for any URL you include: {cs['link_base']}")
if cs.get('read_instructions'):
    out.append(cs['read_instructions'])

print("\n".join(out))
PYEOF
)

echo "--- Context block ---" | tee -a "$LOG_FILE"
echo "$CONTEXT_BLOCK" | tee -a "$LOG_FILE"
echo "---------------------" | tee -a "$LOG_FILE"

# HTTP-only lane (2026-06-01): the four prompt-context reads go through the
# s4l.ai API via scripts/reddit_threads_helper.py. No DATABASE_URL, no psql,
# no fallback. Each subcommand prints exactly what the psql -t -A call printed
# (one row per line, `|`-delimited) so the prompt context is byte-identical.
RT_HELPER="$REPO_DIR/scripts/reddit_threads_helper.py"

# Recent posts in THIS sub (avoid repeats - include endings for closer variation)
RECENT_POSTS_SUB=$(python3 "$RT_HELPER" recent-posts-sub --sub "$SUB_SLUG" --limit 10 2>/dev/null || echo "(api error)")

# Recent posts project-wide (cross-sub dedup - include endings)
RECENT_POSTS_PROJECT=$(python3 "$RT_HELPER" recent-posts-project --project "$PROJECT" --days 14 --limit 15 2>/dev/null || echo "(api error)")

# Recent engagement styles for this project on THIS platform (avoid repeating).
# Scoped to platform='reddit' because cross-platform history conflated tiers —
# a Moltbook post yesterday was blocking a Reddit style today for no reason.
RECENT_STYLES=$(python3 "$RT_HELPER" recent-styles --project "$PROJECT" --limit 5 2>/dev/null || echo "(api error)")

# Top performers (tone calibration)
TOP_POSTS=$(python3 "$RT_HELPER" top-posts --project "$PROJECT" --min-score 5 --limit 10 2>/dev/null || echo "(api error)")

if [ "$IS_OWN" = "True" ]; then
  CADENCE_NOTE="This is our OWNED subreddit. Daily cadence (1-day floor). Be yourself, no product pitches."
else
  CADENCE_NOTE="This is an EXTERNAL subreddit (3-day floor). The thread must pass the sub's self-promo bar. No product links unless genuinely relevant (max 1)."
fi

# JSON schema: forces the model to return structured output with all required fields.
# This is how we enforce step compliance programmatically.
RESULT_SCHEMA='{"type":"object","properties":{"research_files_read":{"type":"array","items":{"type":"string"},"description":"Absolute paths of source files actually read during research step"},"subreddit_browsed":{"type":"boolean","description":"Whether you navigated to the subreddit hot page and read threads"},"hot_threads_seen":{"type":"array","items":{"type":"string"},"description":"Titles of 3-5 hot threads you read on the subreddit"},"topic_angle":{"type":"string","description":"The topic angle chosen from the list"},"engagement_style":{"type":"string","description":"The engagement style chosen"},"title":{"type":"string","description":"The exact post title submitted"},"body":{"type":"string","description":"The exact post body submitted"},"permalink":{"type":["string","null"],"description":"The Reddit permalink after successful submission, or null if aborted"},"rules_checked":{"type":"boolean","description":"Whether you checked subreddit rules"},"flair_applied":{"type":["string","null"],"description":"Flair text applied, or null if none"},"abort_reason":{"type":["string","null"],"description":"Reason for aborting, or null if posted successfully"},"permanent_block":{"type":"boolean","description":"Set TRUE only if this subreddit will reject EVERY future post from this account: account-banned, link-only sub, mod rule banning our entire category (e.g. all software/website posts), approved-submitters-only, or any standing rule that makes future thread posts impossible. Set FALSE for one-off issues (this specific topic violates a rule, repetition, transient errors). When TRUE, the sub is added to thread_blocked permanently and never picked again. Default FALSE."},"source_summary":{"type":"string","description":"Rich source summary: (a) topic angle and why, (b) source files read, (c) specific details used"}},"required":["research_files_read","subreddit_browsed","hot_threads_seen","topic_angle","engagement_style","title","body","permalink","rules_checked","flair_applied","abort_reason","permanent_block","source_summary"]}'

# Pre-generate session id so the prompt's inline INSERT can stamp it.
export CLAUDE_SESSION_ID=$(uuidgen | tr 'A-Z' 'a-z')

# Acquire the browser lock now, immediately before the Claude/MCP step.
#
# Lock strategy (changed 2026-05-10): switched from 3600s bash lock held for
# the entire Claude session to a 90s Python lease lock. The reddit-agent MCP
# proxy (scripts/mcp_lock_proxy.py) heartbeats expires_at on every browser
# tool call, so the lease stays held during real activity but auto-decays
# within 90s of idleness (Claude reasoning gaps, research file reads, etc.).
# Peer pipelines (run-reddit-search post phase, engage-reddit, dm-replies-reddit,
# link-edit-reddit) can use the profile during our long Claude thinking phases.
#
# Unified lock (2026-05-10): only the Python lease. The bash pre-flight was
# removed because lock.sh did not honor expires_at and would block on
# TTL-stale-but-PID-alive holders. Python acquire performs the orphan-Chrome
# sweep internally (ported from lock.sh:175-198); ensure_browser_healthy then
# runs under the exclusive Python lease that follows.
log "Acquiring reddit-browser lease (TTL 90s, MCP-proxy heartbeated)..."
python3 "$REPO_DIR/scripts/reddit_browser_lock.py" acquire --timeout 600 --ttl 90 2>&1 | tee -a "$LOG_FILE" || \
    log "WARNING: reddit_browser_lock.py acquire failed; proceeding without lease (peer pipelines may collide)."
ensure_reddit_browser_for_backend 2>&1 | tee -a "$LOG_FILE" || true
_ensure_rc="${PIPESTATUS[0]}"
hc_exit_if_deferred "$_ensure_rc" "reddit-harness"
if [ "$_ensure_rc" != "0" ]; then
    log "WARNING: reddit-harness bootstrap failed (rc=$_ensure_rc); falling back to ensure_browser_healthy reddit"
    ensure_browser_healthy "reddit"
fi

# NOTE 2026-05-07: removed broken pre-flight Chrome health check (commit
# 971844d, 2026-05-04). Intent was to short-circuit before Claude drafted for
# $4-5 in a session where reddit-agent MCP tools wouldn't load. But the check
# probed for an already-running Chrome with a CDP port; Chrome only launches
# INSIDE the Claude session via launch_persistent_context, so the probe always
# saw "no port" and aborted every fire. 22 launchd fires, 0 posts from
# 2026-05-04 12:13 through 2026-05-07. Aligned with run-reddit-search.sh and
# engage-reddit.sh, which call ensure_browser_healthy and then trust the
# Claude/MCP step to launch Chrome — they kept posting comments fine the whole
# time. Re-introducing a real "MCP loaded?" probe is Phase 2 (separate cheap
# claude -p tool-list call before drafting); for now we accept the same $5
# tail-risk per fire that every other reddit pipeline already accepts.

# Capture Claude output to a temp file so a non-zero exit doesn't swallow stderr
# before we get a chance to log it. Without this, run_claude.sh failures look
# like "SCRIPT DIED line=283 exit=1" with zero context.
CLAUDE_TMP=$(mktemp)
set +e
if [ "$RETRY_MODE" = "1" ]; then
"$REPO_DIR/scripts/run_claude.sh" "run-reddit-threads" --strict-mcp-config --mcp-config "$MCP_CONFIG_FILE" -p --output-format json --json-schema "$RESULT_SCHEMA" "You are RETRYING a previously-aborted thread for the ${PROJECT} project as u/${POST_ACCOUNT}.

$BROWSER_INSTRUCTIONS

## CRITICAL: This is a RETRY. The title and body are PRE-WRITTEN and FINAL.
DO NOT redraft. DO NOT research. DO NOT browse the subreddit. DO NOT pick a topic.
You are ONLY driving the browser through the submit flow with the exact strings below.

## Saved draft (USE EXACTLY AS-IS)
Subreddit: ${SUBREDDIT}
Title: ${PENDING_TITLE}

Body:
${PENDING_BODY}

Topic angle (for log purposes only): ${PENDING_TOPIC}
Engagement style (for log purposes only): ${PENDING_STYLE}
Source summary (for log purposes only): ${PENDING_SOURCE}
Flair target (if known from prior attempt): ${PENDING_FLAIR}

## Workflow

1. Navigate to https://old.reddit.com/${SUBREDDIT}/submit?selftext=true (bh_run: goto_url + wait_for_load).

2. Fill the title and body using the exact saved strings above. Use js() to set the values
   directly (most reliable on old.reddit's md-container wrapper):
     document.querySelector('textarea[name=\"title\"]').value = TITLE;
     document.querySelector('textarea[name=\"title\"]').dispatchEvent(new Event('input',{bubbles:true}));
     document.querySelector('textarea[name=\"text\"]').value = BODY;
     document.querySelector('textarea[name=\"text\"]').dispatchEvent(new Event('input',{bubbles:true}));

3. FLAIR HELPER (if flair required, old.reddit verified 2026-05-01 on r/AutoHotkey).
   OLD.REDDIT SELECTORS ARE STALE in the wild: it is NOT '.flairselector-button',
   NOT '.flairoption', and the confirm button is NOT 'Save'. Do not rely on those.
   Use the visible text/structure described below.
   a. Look for a group labeled around 'choose a flair'. Inside it is a button whose
      visible text is exactly 'select' (lowercase). Click that button.
   b. A modal opens. Header is 'select flair'. Body is a <ul> of <li> rows; each <li>
      is a clickable flair option. Click the <li> matching the saved flair_target above
      (or pick a sensible match if blank, e.g. 'Meta / Discussion', 'Question', 'Help').
   c. Confirm by clicking 'apply' (lowercase, NOT 'Save').
   d. Verify the chosen flair name appears next to the title (replacing '(none)').
   If the 'select' button doesn't open a modal, OR no <li> matches, OR 'apply' is
   missing: ABORT with abort_reason='flair_ui_unexpected' or 'no_suitable_flair'.
   Do NOT loop or retry the click more than twice.

4. Click the submit button (visible text 'submit', lowercase on old.reddit). Wait 3
   seconds. Capture the permalink (document.location.href after submission). Close the tab.

5. Return structured JSON. Use the saved title, body, topic_angle, engagement_style, and
   source_summary as-is for the log fields. Fill permalink with the actual URL if posted,
   or null if aborted. Set rules_checked=true (they were checked in the original attempt).
   Set subreddit_browsed=false and hot_threads_seen=[] (we're not re-doing browse work).
   Set research_files_read=[] (we're reusing prior research). Set permanent_block=false
   unless the submit step itself returned a forbidden/403 error.

CRITICAL: NEVER use em dashes.
CRITICAL: Use ONLY the browser tool described in the BROWSER BACKEND block above (mcp__reddit-harness__bh_run).
CRITICAL: If mcp__reddit-harness__bh_run is NOT available in this session (you cannot find it as deferred or callable), return JSON immediately with permalink=null and abort_reason='mcp_browser_unavailable'. DO NOT spawn Python, shell, headless or headed Playwright as a fallback. DO NOT use mcp__reddit-agent__*, mcp__playwright-extension__*, or any other browser MCP. The pipeline depends on the reddit-harness backend specifically.
CRITICAL: Reuse the SAME tab via goto_url for sequential navigation (the harness keeps one real tab); do NOT open a fresh tab per step.
CRITICAL: If a browser call times out, wait 30s and retry up to 3 times.
CRITICAL: This is a RETRY of a \$4-24 sunk-cost draft. Do NOT redraft, do NOT research." > "$CLAUDE_TMP" 2>&1
CLAUDE_RC=$?
else
"$REPO_DIR/scripts/run_claude.sh" "run-reddit-threads" --strict-mcp-config --mcp-config "$MCP_CONFIG_FILE" -p --output-format json --json-schema "$RESULT_SCHEMA" "You are posting an ORIGINAL thread to ${SUBREDDIT} for the ${PROJECT} project as u/${POST_ACCOUNT}.

$BROWSER_INSTRUCTIONS

## Config & Rules
Read $SKILL_FILE for content rules and anti-AI-detection checklist.
You may also open $CONFIG_FILE for the full project block if you need anything not summarized below.

## Target
Project: ${PROJECT}
Subreddit: ${SUBREDDIT}
Own community: ${IS_OWN}
${CADENCE_NOTE}

## Project context (live-assembled)
${CONTEXT_BLOCK}

${STYLES_BLOCK}

## Recent posts by us in ${SUBREDDIT} (DO NOT repeat topics OR closers)
Each entry shows: title |ENDING| last 200 chars of post body. Study the endings to vary your closer.
${RECENT_POSTS_SUB}

## Recent posts by us for ${PROJECT} across all subs (last 14d, don't recycle angle OR closing style)
${RECENT_POSTS_PROJECT}

## Recent engagement styles for ${PROJECT} (avoid repeating the same style back-to-back)
${RECENT_STYLES}

## Top performing ${PROJECT} posts (match tone/style)
${TOP_POSTS}

## Workflow

1. RESEARCH (required): Read the product source paths listed in the context block. Specifically:
   - README.md at the repo root
   - Any files under src/ or docs/ that relate to your chosen topic angle
   - For Vipassana: read relevant page.tsx under the guide dir
   Pull 1-2 concrete, specific details from the source code or docs to anchor the post. Generic posts get ignored.

2. BROWSE THE SUBREDDIT: Navigate to https://old.reddit.com/${SUBREDDIT}/hot (bh_run: goto_url + wait_for_load).
   - Read 3-5 recent thread titles and their top comments to absorb community tone, vocabulary, and what topics are getting engagement right now.
   - Note any recurring themes or hot-button issues the community cares about today.
   This shapes your post to sound like it belongs in the current conversation, not like a scheduled drop.

3. Pick a topic from the threads.topic_angles list (in the context block above) that:
   - Has NOT been posted recently in this subreddit (see above)
   - Is not a recycled angle from other subs (see project-wide list)
   - Fits this subreddit's community and rules
   - Invites genuine discussion (end with a question or open thread)
   - Pick an engagement_style from the styles list above that:
     (a) fits the topic and subreddit culture
     (b) is NOT one of the last 3 styles used for this project (see recent styles above)

4. Draft the post. RULES:
   - No em dashes anywhere. Commas, periods, or plain '-' only.
   - No markdown formatting (no ##, no **bold**, no bullet lists).
   - 2-4 short paragraphs, casual tone. Narrator voice follows the VOICE RELATIONSHIP block in the styles section.
   - Include at least one imperfection (sentence fragment, aside, lowercase start).
   - Title: lowercase, no clickbait patterns, no emojis.
   - Ground in a specific detail from the product source you read in step 1.
   - Follow the voice guidance from the project context. Read it out loud; if it sounds like a blog post, rewrite.
   - VARY YOUR CLOSERS: check how recent posts ended (shown after |ENDING| above). Use a DIFFERENT ending pattern. Banned closers: 'curious if anyone', 'anyone else', 'thoughts?', 'has anyone'. Sometimes end with a statement, sometimes mid-thought, sometimes a specific (not generic) question.
   - VARY CAPITALIZATION: do NOT lowercase every sentence start. Mix it naturally: some sentences capitalized, some not. Uniform all-lowercase is a known AI tell.

5. SUBREDDIT RULES CHECK (bh_run: goto_url to https://old.reddit.com/${SUBREDDIT}/about/rules + wait_for_load)
   - If strict no-self-promo and our post would read promotional, ABORT. Set abort_reason and permalink=null.
   - Note whether flair is required.

   PERMANENT_BLOCK DECISION (always set this field):
   - permanent_block = TRUE if the sub has a STANDING rule that rejects every post we could ever make from this account: bans all software/website/AI posts (mod-pinned), link-only sub, approved-submitters-only, account is banned from this sub, no-self-promo with zero exceptions for our category. ALSO set TRUE on submit-time forbidden / 403.
   - permanent_block = FALSE if the issue is specific to THIS post (recent topic was already covered, this title is too promotional, you chose to abort to be safe but the sub itself does accept posts of this type, transient browser/network error, repetition concern).
   - When in doubt, FALSE. False positives are cheap (we just retry the sub later); false negatives waste a Claude run cost (\$1.50-3.50 USD) every time we re-pick the same dead-end sub.

6. POST (bh_run):
   - Navigate to https://old.reddit.com/${SUBREDDIT}/submit?selftext=true (goto_url + wait_for_load)
   - Fill title and body. Set the values directly via js() (most reliable on the md-container wrapper):
       document.querySelector('textarea[name=\"title\"]').value = TITLE;
       document.querySelector('textarea[name=\"title\"]').dispatchEvent(new Event('input',{bubbles:true}));
       document.querySelector('textarea[name=\"text\"]').value = BODY;
       document.querySelector('textarea[name=\"text\"]').dispatchEvent(new Event('input',{bubbles:true}));
   - FLAIR HELPER (if flair required, old.reddit verified 2026-05-01 on r/AutoHotkey).
     OLD.REDDIT SELECTORS ARE STALE in the wild: it is NOT '.flairselector-button',
     NOT '.flairoption', and the confirm button is NOT 'Save'. Do not rely on those.
     Use the visible text/structure described below.
     a. After the post body is typed, look for a group labeled around 'choose a flair'.
        Inside it is a button whose visible text is exactly 'select' (lowercase).
        Compute its center coords from getBoundingClientRect via js(), then click_at_xy(x, y).
     b. A modal opens. Header is 'select flair'. Body is a <ul> of <li> rows; each <li>
        is a clickable flair option (cursor: pointer). There is no .flairoption class.
        Find the <li> whose text matches the right flair (e.g. 'Meta / Discussion',
        'Question', 'Help', 'Showcase'). Click that <li>.
     c. The confirm button is labeled 'apply' (lowercase). Click 'apply'. Do NOT look
        for 'Save', 'OK', 'Confirm', or 'Submit' inside the modal — they don't exist.
     d. Verify success by re-reading the snapshot: the '(none)' placeholder next to
        the title should be replaced by the chosen flair name (typically rendered green).
     If the 'select' button doesn't open a modal, OR no <li> matches a sensible flair,
     OR the 'apply' button is missing: ABORT with abort_reason='flair_ui_unexpected'
     or 'no_suitable_flair'. Do NOT loop or retry the click more than twice — repeated
     clicks have crashed the chrome MCP child in past runs (2026-05-01 r/AutoHotkey).
   - Click the submit button (visible text 'submit', lowercase on old.reddit).
     Wait 3 seconds. Capture the permalink (document.location.href after submission).
   - Close the tab.

7. DO NOT touch the database. The shell wrapper handles the INSERT after you return.
   IMPORTANT: source_summary, title, body, permalink, engagement_style in your
   JSON output ARE what get logged. Make source_summary rich and grounded in
   the specific files/details you read in step 1.
   ABORT-SAFE: if you abort AFTER drafting (e.g. flair UI broke, MCP child died,
   submit button vanished), still return the title + body you drafted in your
   JSON. The shell wrapper will persist them to pending_threads so the next
   pipeline run can retry the post WITHOUT re-paying for research/drafting.

8. Return your structured JSON output. Every field in the schema is required. Fill permalink with the actual URL if posted, or null if aborted.

CRITICAL: NEVER use em dashes.
CRITICAL: Use ONLY the browser tool described in the BROWSER BACKEND block above (mcp__reddit-harness__bh_run).
CRITICAL: If mcp__reddit-harness__bh_run is NOT available in this session (you cannot find it as deferred or callable), return JSON immediately with permalink=null and abort_reason='mcp_browser_unavailable'. DO NOT spawn Python, shell, headless or headed Playwright as a fallback. DO NOT use mcp__reddit-agent__*, mcp__playwright-extension__*, or any other browser MCP. The pipeline depends on the reddit-harness backend specifically. The shell wrapper persists your draft to pending_threads on abort, so a clean ABORT-SAFE return preserves the work.
CRITICAL: Reuse the SAME tab via goto_url for sequential navigation (the harness keeps one real tab); do NOT open a fresh tab per step.
CRITICAL: If a browser call times out, wait 30s and retry up to 3 times." > "$CLAUDE_TMP" 2>&1
CLAUDE_RC=$?
fi  # end RETRY_MODE branch
set -e
CLAUDE_OUTPUT=$(cat "$CLAUDE_TMP")
rm -f "$CLAUDE_TMP"

# Belt-and-suspenders: free the reddit-browser lease if it's still held.
# Idempotent — release prints OK / NOT_HELD / HELD_BY_OTHER. Mirrors
# link-edit-reddit.sh:185 and engage-dm-replies.sh:1439. If Claude crashed
# mid-post the lease auto-decays in 90s, but explicit release frees peers
# immediately so they can use the profile during the rest of this run's
# DB writes / logging.
timeout 3 python3 "$REPO_DIR/scripts/reddit_browser_lock.py" release 2>/dev/null || true

# Parse structured output and log results
echo "$CLAUDE_OUTPUT" | tee -a "$LOG_FILE"
if [ "$CLAUDE_RC" -ne 0 ]; then
  echo "RUN_CLAUDE_NONZERO_EXIT rc=$CLAUDE_RC (output above is full stderr+stdout)" | tee -a "$LOG_FILE"
fi

# Extract structured_output from the JSON envelope.
# claude -p --output-format json wraps results as: {"structured_output": {...}, "result": "...", ...}
PARSED=$(/usr/bin/python3 -c "
import json,sys
try:
    raw = sys.stdin.read()
    # run_claude.sh appends a log line to stderr but 2>&1 captures it here too,
    # giving us two concatenated JSON objects. raw_decode stops after the first.
    d, _ = json.JSONDecoder().raw_decode(raw)
    so = d.get('structured_output') or d
    print(json.dumps(so))
except Exception as e:
    print(json.dumps({'_parse_error': str(e)}))
" <<< "$CLAUDE_OUTPUT" 2>/dev/null)

PERMALINK=$(/usr/bin/python3 -c "import json,sys; r=json.loads(sys.stdin.read()); print(r.get('permalink') or 'null')" <<< "$PARSED" 2>/dev/null)
TITLE=$(/usr/bin/python3 -c "import json,sys; r=json.loads(sys.stdin.read()); print(r.get('title',''))" <<< "$PARSED" 2>/dev/null)
ABORT_REASON=$(/usr/bin/python3 -c "import json,sys; r=json.loads(sys.stdin.read()); print(r.get('abort_reason') or '')" <<< "$PARSED" 2>/dev/null)
# Explicit permanent-block signal from the model. Trusted when present;
# regex fallback in mark_thread_blocked still runs if Claude omits it.
PERMANENT_BLOCK=$(/usr/bin/python3 -c "import json,sys; r=json.loads(sys.stdin.read()); print('1' if r.get('permanent_block') is True else '0')" <<< "$PARSED" 2>/dev/null)

# Log step compliance summary
/usr/bin/python3 -c "
import json,sys
r = json.loads(sys.stdin.read())
if '_parse_error' in r:
    print(f'Step compliance: PARSE ERROR ({r[\"_parse_error\"]})')
else:
    files = r.get('research_files_read', [])
    browsed = r.get('subreddit_browsed', False)
    hot = r.get('hot_threads_seen', [])
    rules = r.get('rules_checked', False)
    style = r.get('engagement_style', '?')
    print(f'Step compliance: research={len(files)} files, browsed={browsed}, hot_threads={len(hot)}, rules_checked={rules}, style={style}')
" <<< "$PARSED" 2>/dev/null | tee -a "$LOG_FILE"

if [ "$PERMALINK" != "null" ] && [ "$PERMALINK" != "PARSE_ERROR" ]; then
  echo "POSTED: $PERMALINK | $TITLE" | tee -a "$LOG_FILE"

  # Authoritative DB INSERT.
  # Historical bug: step 7 of the prompt asked Claude to run psql via Bash to
  # log the post. Claude sometimes did, sometimes didn't (e.g. mk0r run id
  # 21486 on 2026-04-29 was orphaned and had to be backfilled by hand).  The
  # shell already has every required value parsed out of structured_output, so
  # do the INSERT here and stop trusting the model with a database step.
  PARSED="$PARSED" \
  CLAUDE_SESSION_ID="$CLAUDE_SESSION_ID" \
  PROJECT_ENV="$PROJECT" \
  POST_ACCOUNT="$POST_ACCOUNT" \
  REPO_DIR="$REPO_DIR" \
  PENDING_ID_ENV="${PENDING_ID:-}" \
  PICKED_STYLE_ENV="${PICKED_STYLE:-}" \
  PICK_MODE_ENV="${PICK_MODE:-}" \
  /usr/bin/python3 <<'PYEOF' 2>&1 | tee -a "$LOG_FILE" || true
import json, os, sys
sys.path.insert(0, os.path.join(os.environ["REPO_DIR"], "scripts"))
from http_api import api_post
import pending_threads as pt

parsed = json.loads(os.environ.get("PARSED") or "{}")
permalink = parsed.get("permalink") or ""
title     = parsed.get("title", "")
body      = parsed.get("body", "")
summary   = parsed.get("source_summary", "")
raw_style = (parsed.get("engagement_style") or "").strip()
session   = os.environ.get("CLAUDE_SESSION_ID") or None
project   = os.environ.get("PROJECT_ENV", "")
account   = os.environ.get("POST_ACCOUNT", "")
pending_id_str = os.environ.get("PENDING_ID_ENV", "")

# 2026-05-25: validate_or_register pass. The picker assignment (PICKED_STYLE +
# PICK_MODE) is sourced from the shell-level s4l_pick_style call near the top
# of this script. USE mode coerces drift back to the assigned style; INVENT
# mode registers the new_style block (if the model shipped one) into
# engagement_styles_registry via /api/v1/engagement-styles/registry. Without
# this gate, run-reddit-threads.sh was the lone Reddit pipeline writing raw
# model output straight to posts.engagement_style, which let names like
# 'sensory_contrast' leak in without ever hitting the registry (caught
# 2026-05-25 during the engagement-style audit).
style = raw_style or None
try:
    from engagement_styles import validate_or_register
    picked_style = (os.environ.get("PICKED_STYLE_ENV") or "").strip() or None
    pick_mode    = (os.environ.get("PICK_MODE_ENV") or "").strip() or None
    if raw_style:
        new_style_block = parsed.get("new_style") if isinstance(parsed.get("new_style"), dict) else None
        decision = {
            "engagement_style": raw_style,
            **({"new_style": new_style_block} if new_style_block else {}),
        }
        coerced_style, action = validate_or_register(
            decision,
            source_post={
                "platform": "reddit",
                "post_url": permalink or None,
                "post_id": None,
                "model": None,
            },
            assigned_style=picked_style,
            assigned_mode=pick_mode,
        )
        if action == "coerced" and coerced_style and coerced_style != raw_style:
            print(f"[engagement_style] coerced {raw_style!r} -> {coerced_style!r} "
                  f"(assigned={picked_style!r} mode={pick_mode!r})")
        elif action == "registered":
            print(f"[engagement_style] registered new style {coerced_style!r} "
                  f"into engagement_styles_registry")
        elif action == "rejected":
            print(f"[engagement_style] rejected {raw_style!r} (assigned={picked_style!r}); "
                  f"falling back to assigned")
            coerced_style = picked_style
        style = (coerced_style or picked_style or raw_style) or None
except Exception as e:
    # Never block a posted thread on registry plumbing. Log and fall back
    # to the raw model output.
    print(f"[engagement_style] validate_or_register raised {e!r}; "
          f"falling back to raw={raw_style!r}")
    style = raw_style or None

if not permalink or not title:
    print("[db-insert] SKIP — empty permalink or title in structured_output")
    sys.exit(0)

# HTTP-only lane (2026-06-01): the authoritative post log goes through the
# s4l.ai API (POST /api/v1/posts). No DATABASE_URL, no psql, no db.get_conn().
# The endpoint dedups on (platform, thread_url) server-side and returns 409 with
# existing_post_id, which replaces the old SELECT idempotency guard. For an
# own-thread post, thread_url == our_url == permalink, so the (platform,
# thread_url) dedup is equivalent to the old (platform, our_url) check.
resp = api_post("/api/v1/posts", {
    "platform": "reddit",
    "thread_url": permalink,
    "thread_author": account,
    "thread_title": title,
    "our_url": permalink,
    "our_content": body,
    "our_account": account,
    "source_summary": summary,
    "project": project,
    "engagement_style": style,
    "status": "active",
    "claude_session_id": session,
}, ok_on_conflict=True)

if not resp.get("ok", True) and (resp.get("error") or {}).get("code") == "duplicate_thread":
    existing_id = ((resp.get("error") or {}).get("details") or {}).get("existing_post_id")
    print(f"[db-insert] SKIP — post {permalink} already in DB as id={existing_id}")
    sys.exit(0)

post_id = (resp.get("data") or {}).get("post", {}).get("id")
print(f"[db-insert] OK — inserted posts.id={post_id} for {permalink}")

# If this run came from a pending_threads retry, mark the pending row posted
# so it stops being picked up by future retry cycles.
if pending_id_str:
    try:
        pt.mark_posted(pending_id=int(pending_id_str), post_id=post_id, permalink=permalink)
        print(f"[pending] OK — pending_threads.id={pending_id_str} marked posted")
    except Exception as e:
        print(f"[pending] WARNING — mark_posted failed for id={pending_id_str}: {e}")

# Campaign wiring: post-submit edit pattern.
# Threads can't apply the suffix at submit time (Claude drives the browser
# directly via MCP), so we load active campaigns AFTER insert, roll the dice,
# and use reddit_browser.py edit-thread to append the suffix on the live post.
# Bumps the campaign counter only on a verified live edit (parallels
# post_reddit / engage_reddit / send_dm semantics).
import random, subprocess
from post_reddit import load_active_reddit_campaigns

active_campaigns = load_active_reddit_campaigns()
applied_campaign_ids = []
new_body = body
for camp in active_campaigns:
    if random.random() < camp["sample_rate"]:
        new_body = new_body + camp["suffix"]
        applied_campaign_ids.append(camp["id"])

if applied_campaign_ids:
    print(f"[campaign-thread] applying {applied_campaign_ids} (suffix to be appended via edit)")
    rb = os.path.join(os.environ["REPO_DIR"], "scripts", "reddit_browser.py")
    edit_proc = subprocess.run(
        ["python3", rb, "edit-thread", permalink, new_body],
        capture_output=True, text=True, timeout=120,
    )
    edit_ok = False
    edit_payload = {}
    try:
        # reddit_browser.py edit-thread prints multi-line JSON via
        # json.dumps(result, indent=2). Use raw_decode on the full stdout so
        # we get the whole document, not just the final '}' line.
        stdout_str = (edit_proc.stdout or "").strip()
        edit_payload, _ = json.JSONDecoder().raw_decode(stdout_str)
        edit_ok = edit_payload.get("ok") is True
    except Exception as e:
        edit_payload = {"_parse_error": f"{type(e).__name__}: {e}",
                        "_stdout_tail": (edit_proc.stdout or "")[-200:]}
    if edit_ok:
        from http_api import api_patch
        api_patch(f"/api/v1/posts/{post_id}", {"our_content": new_body})
        bump = os.path.join(os.environ["REPO_DIR"], "scripts", "campaign_bump.py")
        for cid in applied_campaign_ids:
            try:
                subprocess.run(
                    ["python3", bump,
                     "--table", "posts", "--id", str(post_id),
                     "--campaign-id", str(cid)],
                    capture_output=True, text=True, timeout=15,
                )
            except Exception as e:
                print(f"[campaign-thread] WARNING: campaign_bump failed (id={post_id} c={cid}): {e}")
        print(f"[campaign-thread] OK — edit verified={edit_payload.get('verified')}, our_content updated, counters bumped")
    else:
        # Edit failed — leave the post untagged. The post is already live, so
        # this is a degraded but not data-corrupting outcome. campaign_id stays
        # NULL, so the row joins the control bucket for A/B purposes.
        err = edit_payload.get("error") or edit_payload.get("_parse_error") or "unknown"
        print(f"[campaign-thread] WARNING: edit-thread failed ({err}); post stays untagged. stderr={(edit_proc.stderr or '')[-200:]}")
else:
    print("[campaign-thread] no active campaigns fired (or none active)")
PYEOF

elif [ -n "$ABORT_REASON" ] && [ "$ABORT_REASON" != "PARSE_ERROR" ]; then
  echo "ABORTED: $ABORT_REASON" | tee -a "$LOG_FILE"
  echo "PERMANENT_BLOCK signal from model: $PERMANENT_BLOCK" | tee -a "$LOG_FILE"

  # Persist the abandoned draft to pending_threads so we don't lose the
  # research/drafting work (the original 2026-05-01 r/AutoHotkey crash burned
  # ~$24 because a fully-drafted post evaporated when the chrome MCP child
  # died at the flair-click step). Skip persistence on permanent_block — that
  # sub will never accept this post anyway.
  if [ "$PERMANENT_BLOCK" != "1" ]; then
    PARSED="$PARSED" \
    CLAUDE_SESSION_ID="$CLAUDE_SESSION_ID" \
    PROJECT_ENV="$PROJECT" \
    SUBREDDIT_ENV="$SUBREDDIT" \
    POST_ACCOUNT="$POST_ACCOUNT" \
    ABORT_REASON_ENV="$ABORT_REASON" \
    PENDING_ID_ENV="${PENDING_ID:-}" \
    RETRY_MODE_ENV="${RETRY_MODE:-0}" \
    REPO_DIR="$REPO_DIR" \
    /usr/bin/python3 <<'PYEOF' 2>&1 | tee -a "$LOG_FILE" || true
import json, os, sys
sys.path.insert(0, os.path.join(os.environ["REPO_DIR"], "scripts"))
import pending_threads as pt

parsed = json.loads(os.environ.get("PARSED") or "{}")
title = (parsed.get("title") or "").strip()
body  = (parsed.get("body") or "").strip()
abort_reason = os.environ.get("ABORT_REASON_ENV", "")
retry_mode = os.environ.get("RETRY_MODE_ENV", "0") == "1"
existing_pid = os.environ.get("PENDING_ID_ENV") or ""

if retry_mode and existing_pid:
    # We were retrying an existing pending row. Bump its attempts; if it just
    # crossed the abandon threshold (3), mark it abandoned so the retry loop
    # stops picking it up.
    pid = int(existing_pid)
    pt.mark_aborted(
        pending_id=pid,
        abort_reason=abort_reason,
        abort_stage="retry_attempt",
    )
    rec = pt.get(pid) or {}
    if (rec.get("attempts") or 0) >= 3:
        pt.abandon(pending_id=pid, reason=f"max_retries_exceeded ({abort_reason})")
        print(f"[pending] ABANDON — id={pid} attempts={rec.get('attempts')} >= 3, no further retries")
    else:
        print(f"[pending] BUMP — id={pid} attempts={rec.get('attempts')} (will retry next cycle)")
elif not title or not body:
    print("[pending] SKIP — no title/body in structured_output, nothing to persist")
else:
    # Fresh-draft abort: save it for retry on next cycle.
    pid = pt.create(
        project=os.environ["PROJECT_ENV"],
        subreddit=os.environ["SUBREDDIT_ENV"],
        account=os.environ["POST_ACCOUNT"],
        title=title,
        body=body,
        flair_target=parsed.get("flair_applied"),
        engagement_style=parsed.get("engagement_style"),
        topic_angle=parsed.get("topic_angle"),
        source_summary=parsed.get("source_summary"),
        claude_session_id=os.environ.get("CLAUDE_SESSION_ID") or None,
    )
    pt.mark_aborted(
        pending_id=pid,
        abort_reason=abort_reason,
        abort_stage="post_attempt_1",
    )
    print(f"[pending] OK — saved draft id={pid} for retry on next thread cycle")
PYEOF
  else
    echo "[pending] SKIP — permanent_block=true, draft not retryable on this sub" | tee -a "$LOG_FILE"
    # If we were retrying a pending row and the sub got permanently blocked,
    # abandon the pending row so it stops blocking the queue.
    if [ "$RETRY_MODE" = "1" ] && [ -n "${PENDING_ID:-}" ]; then
      PENDING_ID_ENV="$PENDING_ID" \
      ABORT_REASON_ENV="$ABORT_REASON" \
      REPO_DIR="$REPO_DIR" \
      /usr/bin/python3 -c "
import os, sys
sys.path.insert(0, os.path.join(os.environ['REPO_DIR'], 'scripts'))
import pending_threads as pt
pt.abandon(pending_id=int(os.environ['PENDING_ID_ENV']), reason='permanent_block: '+os.environ.get('ABORT_REASON_ENV',''))
print(f'[pending] ABANDON — id={os.environ[\"PENDING_ID_ENV\"]} due to permanent_block on retry')
" 2>&1 | tee -a "$LOG_FILE" || true
    fi
  fi
  # Auto-block path:
  #   1. PRIMARY: trust the model's permanent_block boolean from structured_output
  #      (added 2026-04-29). If true, add to thread_blocked unconditionally.
  #   2. FALLBACK: regex match against abort_reason via _abort_is_permanent_block.
  #      Catches cases where the model forgot the field or is on an old prompt.
  SUB_SLUG_ENV="$SUB_SLUG" \
  ABORT_REASON_ENV="$ABORT_REASON" \
  PERMANENT_BLOCK_ENV="$PERMANENT_BLOCK" \
  PROJECT_NAME_ENV="${PROJECT_ENV:-}" \
  REPO_DIR="$REPO_DIR" \
  /usr/bin/python3 <<'PYEOF' 2>&1 | tee -a "$LOG_FILE" || true
import os, sys
sys.path.insert(0, os.path.join(os.environ["REPO_DIR"], "scripts"))
from post_reddit import mark_thread_blocked, _abort_is_permanent_block

sub = os.environ.get("SUB_SLUG_ENV", "")
reason = os.environ.get("ABORT_REASON_ENV", "")
project = os.environ.get("PROJECT_NAME_ENV") or None
explicit = os.environ.get("PERMANENT_BLOCK_ENV", "0") == "1"

if explicit:
    # Model declared the block explicitly. Bypass the regex gate via force=True
    # so the audit record still captures the model's verbatim abort_reason
    # even when the text doesn't match _THREAD_BLOCK_PATTERNS.
    mark_thread_blocked(sub, reason or "permanent_block=true", project=project, force=True)
    print(f"[auto-block] r/{sub} added via explicit permanent_block=true from model (project={project!r})")
elif _abort_is_permanent_block(reason):
    mark_thread_blocked(sub, reason, project=project)
    print(f"[auto-block] r/{sub} added via regex fallback on abort_reason (project={project!r})")
else:
    print(f"[auto-block] r/{sub} NOT auto-blocked (permanent_block=false, abort reason looks transient)")
PYEOF
else
  echo "UNKNOWN OUTCOME (check JSON output above)" | tee -a "$LOG_FILE"
fi

# Surface this run in the dashboard's Job History under "Post Threads · Reddit".
# Script name `thread_reddit` is what bin/server.js classifyScript() matches.
ELAPSED=$(( $(date +%s) - RUN_START_EPOCH ))
if [ "$PERMALINK" != "null" ] && [ "$PERMALINK" != "" ] && [ "$PERMALINK" != "PARSE_ERROR" ]; then
  POSTED_CT=1; SKIPPED_CT=0; FAILED_CT=0
elif [ -n "$ABORT_REASON" ] && [ "$ABORT_REASON" != "PARSE_ERROR" ]; then
  POSTED_CT=0; SKIPPED_CT=1; FAILED_CT=0
else
  POSTED_CT=0; SKIPPED_CT=0; FAILED_CT=1
fi
_COST=$(/usr/bin/python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START_EPOCH" --scripts "run-reddit-threads" 2>/dev/null || echo "0.0000")
/usr/bin/python3 "$REPO_DIR/scripts/log_run.py" --script "thread_reddit" --posted "$POSTED_CT" --skipped "$SKIPPED_CT" --failed "$FAILED_CT" --cost "$_COST" --elapsed "$ELAPSED" || true

echo "=== Run complete: $(date) ===" | tee -a "$LOG_FILE"
find "$LOG_DIR" -name "run-reddit-threads-*.log" -mtime +14 -delete 2>/dev/null || true
