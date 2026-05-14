#!/usr/bin/env bash
# engage-twitter.sh — X/Twitter engagement loop
# Phase A: Discover replies/mentions via Twitter API (no browser needed)
# Phase B: Respond to pending Twitter replies via browser (API can't reply to most tweets)
# Called by launchd every 3 hours.


set -euo pipefail

# Bootstrap log paths early so the singleton-cleanup output below gets captured
# in the same log file the rest of the run uses.
LOG_DIR="$HOME/social-autoposter/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/engage-twitter-$(date +%Y-%m-%d_%H%M%S).log"

# Per-cycle batch id stamped onto every claude_sessions row spawned by this
# engagement run (via SA_CYCLE_ID env -> log_claude_session.py). 2026-05-10
# cycle_id rollout.
BATCH_ID="entw-$(date +%Y%m%d-%H%M%S)-$$"
export SA_CYCLE_ID="$BATCH_ID"

# 2026-05-13 backend selector — switch between twitter-agent (Playwright MCP,
# default) and twitter-harness (browser-harness MCP, CDP-driven real Chrome).
# Both drive the SAME logical Twitter session via shared auth_token cookies,
# so lock.sh's defer_if_foreign_browser_mcp_active treats them as mutually
# exclusive. To pilot the harness path: TWITTER_BACKEND=harness bash engage-twitter.sh
TWITTER_BACKEND="${TWITTER_BACKEND:-agent}"
case "$TWITTER_BACKEND" in
    agent)
        MCP_CONFIG_FILE="$HOME/.claude/browser-agent-configs/twitter-agent-mcp.json"
        BROWSER_INSTRUCTIONS=$(cat <<'BROWSER_AGENT_EOF'
BROWSER BACKEND: twitter-agent (Playwright MCP, headed Chromium at ~/.claude/browser-profiles/twitter).
Tools available: mcp__twitter-agent__browser_navigate, browser_snapshot, browser_run_code,
browser_click, browser_type, browser_take_screenshot, browser_wait_for, browser_press_key,
browser_resize, browser_console_messages, browser_network_requests. The MCP holds the
browser open across calls; tool calls are session-stateful.
BROWSER_AGENT_EOF
        )
        ;;
    harness)
        MCP_CONFIG_FILE="$HOME/.claude/browser-agent-configs/twitter-harness-mcp.json"
        # Route twitter_browser.py (Phase A) at the harness Chrome instead of the
        # twitter-agent profile. twitter_browser.py:get_browser_and_page reads
        # TWITTER_CDP_URL as a hard override (skips ps-based discovery).
        export TWITTER_CDP_URL="http://127.0.0.1:9555"
        BROWSER_INSTRUCTIONS=$(cat <<'BROWSER_HARNESS_EOF'
BROWSER BACKEND: twitter-harness (browser-harness MCP, CDP-driven REAL Google Chrome on
port 9555, profile ~/.claude/browser-profiles/browser-harness). The Chrome is already
logged in as m13v_; cookies persist on disk.

You have ONE tool: mcp__twitter-harness__bh_run(script). It runs arbitrary Python with
these helpers pre-imported:
  new_tab(url), goto_url(url), wait_for_load(), page_info(),
  capture_screenshot(),                     # returns path to PNG; Read it to see the page
  click_at_xy(x, y),                        # coordinate click (viewport pixels)
  js(expression),                           # page.evaluate-style; returns the result
  type_text(text),                          # types into currently-focused element
  press_key(key),                           # e.g. "Enter", "Tab", "Escape"
  scroll(direction, amount), cdp(method, **params)

TRANSLATION TABLE — wherever this prompt mentions a Playwright-style tool, do the
following with bh_run instead:

  browser_navigate(url)           ->  bh_run('new_tab("URL")') or bh_run('goto_url("URL"); wait_for_load()')
  browser_snapshot                ->  bh_run('print(js("""..."""))') to read DOM as structured data,
                                       OR bh_run('print(capture_screenshot())') + Read the PNG
  browser_run_code(js)            ->  bh_run('print(js("""<the JS expression>"""))')
  browser_click(ref=...)          ->  Find the element via selector, compute center coords from
                                       getBoundingClientRect, then bh_run('click_at_xy(X, Y)')
  browser_type(ref=..., text=...) ->  Click the textbox first (click_at_xy), then bh_run('type_text("TEXT")')
  browser_take_screenshot         ->  bh_run('print(capture_screenshot())') then Read the path
  browser_press_key("Enter")      ->  bh_run('press_key("Enter")')

EXAMPLE — click the reply submit button:
  bh_run('''
  pt = js("""
    const el = document.querySelector('[data-testid="tweetButtonInline"]');
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {x: r.x + r.width/2, y: r.y + r.height/2};
  """)
  print(pt)
  ''')
  # Then in a follow-up call (substituting the x/y from above):
  bh_run('click_at_xy(123, 456)')

VERIFY AFTER EVERY MUTATION by capturing a screenshot and reading the PNG — coordinate
clicks can miss; visual verification is the only reliable confirmation that the action took.
BROWSER_HARNESS_EOF
        )
        ;;
    *)
        echo "ERROR: unknown TWITTER_BACKEND='$TWITTER_BACKEND' (expected: agent, harness)" >&2
        exit 2
        ;;
esac

# Browser-profile lock first (shared with other twitter pipelines), then pipeline lock.
source "$(dirname "$0")/lock.sh"

# Skip cleanly if an interactive twitter-agent MCP session (Fazm Dev / IDE /
# another cron) is alive on the same profile. Racing the foreign Chrome
# triggers the "chromium profile locked by another process; waited 45s"
# SIGTRAP cascade — observed live 2026-05-13 14:29 with the user's IDE
# holding the profile via codex-acp.
#
# 2026-05-13: For TWITTER_BACKEND=harness we skip this check — the harness
# Chrome is on a separate profile (~/.claude/browser-profiles/browser-harness)
# and CDP supports multiple concurrent clients on the same Chrome, so foreign
# harness MCP servers do NOT cause SingletonLock contention. We still rely on
# the bash `twitter-browser` lock below for serializing Twitter operations
# across pipelines.
if [ "$TWITTER_BACKEND" = "agent" ]; then
    if defer_if_foreign_browser_mcp_active "twitter" "$LOG_FILE"; then
        exit 0
    fi
fi

acquire_lock "twitter-browser" 3600

if [ "$TWITTER_BACKEND" = "agent" ]; then
    # Drop stale Chrome singleton symlinks before launch. Background ungraceful-
    # exits (SIGKILL, jetsam, force quit) leave Singleton{Lock,Cookie,Socket}
    # pointing at dead PIDs / vanished sockets; without this, Chrome pops "Something
    # went wrong when opening your profile" 7x and the pipeline hangs. See
    # scripts/clean_stale_singleton.sh — refuses to clean if PID is alive.
    bash "$HOME/social-autoposter/scripts/clean_stale_singleton.sh" "$HOME/.claude/browser-profiles/twitter" 2>&1 | tee -a "$LOG_FILE" || true
    ensure_browser_healthy "twitter"
else
    # Harness path: ensure the harness Chrome is alive on port 9555 before
    # Phase A's twitter_browser.py call. The browser-harness MCP launches it
    # lazily on first bh_run, but Phase A doesn't go through MCP — it connects
    # via CDP directly. Cold start = ~3s. Idempotent: skips if already up.
    if ! curl -sf --max-time 2 -o /dev/null http://127.0.0.1:9555/json/version 2>/dev/null; then
        log "Harness Chrome down on port 9555 — launching..."
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"             --remote-debugging-port=9555             --user-data-dir="$HOME/.claude/browser-profiles/browser-harness"             --no-first-run --no-default-browser-check             --disable-features=ChromeWhatsNewUI             about:blank >>"$LOG_FILE" 2>&1 &
        disown
        # Wait up to 12s for CDP to be ready.
        for _i in 1 2 3 4 5 6 7 8 9 10 11 12; do
            curl -sf --max-time 2 -o /dev/null http://127.0.0.1:9555/json/version 2>/dev/null && break
            sleep 1
        done
        if ! curl -sf --max-time 2 -o /dev/null http://127.0.0.1:9555/json/version 2>/dev/null; then
            log "ERROR: harness Chrome failed to start within 12s; aborting"
            exit 1
        fi
        log "Harness Chrome up on port 9555"
    else
        log "Harness Chrome already alive on port 9555"
    fi
fi
acquire_lock "twitter" 3600

# Load secrets
# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
SKILL_FILE="$REPO_DIR/SKILL.md"
BATCH_SIZE=500

if [ -z "${DATABASE_URL:-}" ]; then
    echo "ERROR: DATABASE_URL not set in ~/social-autoposter/.env"
    exit 1
fi
# (LOG_DIR/LOG_FILE bootstrapped at top of script.)

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

RUN_START=$(date +%s)
log "=== Twitter Engagement Run: $(date) ==="

# Load exclusions from config
EXCLUDED_AUTHORS=$(python3 -c "import json; c=json.load(open('$REPO_DIR/config.json')); print(', '.join(c.get('exclusions',{}).get('authors',[])))" 2>/dev/null || echo "")
EXCLUDED_TWITTER=$(python3 -c "import json; c=json.load(open('$REPO_DIR/config.json')); print(', '.join(c.get('exclusions',{}).get('twitter_accounts',[])))" 2>/dev/null || echo "")

# ═══════════════════════════════════════════════════════
# PHASE A: Discover new replies/mentions from Twitter notifications
# ═══════════════════════════════════════════════════════
log "Phase A: Scanning Twitter mentions via browser (no API cost)..."
NOTIFS_JSON=$(mktemp -t twitter_notifs.XXXXXX.json)
python3 "$REPO_DIR/scripts/twitter_browser.py" notifications 8 > "$NOTIFS_JSON" 2>>"$LOG_FILE" \
    || log "WARNING: twitter_browser.py notifications failed"
python3 "$REPO_DIR/scripts/scan_twitter_mentions_browser.py" --json-file "$NOTIFS_JSON" 2>&1 \
    | tee -a "$LOG_FILE" \
    || log "WARNING: Phase A scan_twitter_mentions_browser.py exited with code $?"
rm -f "$NOTIFS_JSON"

# ═══════════════════════════════════════════════════════
# PHASE B: Respond to pending Twitter replies
# ═══════════════════════════════════════════════════════

# Reset any 'processing' items older than 2 hours back to 'pending'
RESET_COUNT=$(psql "$DATABASE_URL" -t -A -c "
    WITH upd AS (
        UPDATE replies SET status='pending'
        WHERE platform='x' AND status='processing' AND processing_at < NOW() - INTERVAL '2 hours'
        RETURNING id
    ) SELECT COUNT(*) FROM upd;")
[ "$RESET_COUNT" -gt 0 ] && log "Phase B: Reset $RESET_COUNT stuck 'processing' Twitter items back to pending"

PENDING_COUNT=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM replies WHERE platform='x' AND status='pending';")

if [ "$PENDING_COUNT" -eq 0 ]; then
    log "Phase B: No pending Twitter replies. Done!"
else
    log "Phase B: $PENDING_COUNT pending Twitter replies to process"

    PENDING_DATA=$(psql "$DATABASE_URL" -t -A -c "
        SELECT json_agg(q) FROM (
            SELECT r.id, r.platform, r.their_author,
                   r.their_content as their_content,
                   r.their_comment_url, r.their_comment_id, r.depth,
                   p.thread_title as thread_title,
                   p.thread_url, p.our_content as our_content, p.our_url,
                   CASE WHEN p.thread_url = p.our_url THEN 1 ELSE 0 END as is_our_original_post,
                   p.project_name
            FROM replies r
            JOIN posts p ON r.post_id = p.id
            WHERE r.platform='x' AND r.status='pending'
            ORDER BY
                CASE WHEN p.thread_url = p.our_url THEN 0 ELSE 1 END,
                r.discovered_at ASC
            LIMIT $BATCH_SIZE
        ) q;")

    # Per-project voice map (so each reply can be drafted in the matched project's voice)
    PROJECTS_VOICE_JSON=$(python3 -c "
import json
c = json.load(open('$REPO_DIR/config.json'))
print(json.dumps({p['name']: p.get('voice', {}) for p in c.get('projects', []) if p.get('voice')}, indent=2))
" 2>/dev/null || echo "{}")

    # Generate engagement style and content rules from shared module
    source "$REPO_DIR/skill/styles.sh"
    STYLES_BLOCK=$(generate_styles_block twitter replying)

    # Top performers feedback report (platform-wide)
    TOP_REPORT=$(python3 "$REPO_DIR/scripts/top_performers.py" --platform twitter 2>/dev/null || echo "(top performers report unavailable)")

    # Precompute active Twitter campaign suffix + sample_rate + id for the
    # prompt to inline. Phase B replies go through the MCP browser_type path
    # (twitter_browser.py reply wedges against the MCP profile), so tool-level
    # injection is unavailable; the LLM has to flip a coin and append the
    # literal suffix by hand. When no active campaign exists, all three vars
    # resolve to empty strings and the prompt's "if empty, do nothing extra"
    # branch fires. Mirrors the Reddit MCP-fallback pattern in
    # engage-dm-replies.sh.
    TWITTER_CAMPAIGN_SUFFIX_LITERAL=$(psql "$DATABASE_URL" -t -A -c "
        SELECT suffix FROM campaigns
        WHERE status='active' AND (',' || platforms || ',') LIKE '%,twitter,%'
          AND max_posts_total IS NOT NULL AND posts_made < max_posts_total
          AND suffix IS NOT NULL AND suffix <> ''
        ORDER BY id LIMIT 1;" 2>/dev/null | tr -d '\n' || echo "")
    TWITTER_CAMPAIGN_SAMPLE_RATE=$(psql "$DATABASE_URL" -t -A -c "
        SELECT COALESCE(sample_rate, 1.000) FROM campaigns
        WHERE status='active' AND (',' || platforms || ',') LIKE '%,twitter,%'
          AND max_posts_total IS NOT NULL AND posts_made < max_posts_total
          AND suffix IS NOT NULL AND suffix <> ''
        ORDER BY id LIMIT 1;" 2>/dev/null | tr -d '\n' || echo "")
    TWITTER_CAMPAIGN_ID=$(psql "$DATABASE_URL" -t -A -c "
        SELECT id FROM campaigns
        WHERE status='active' AND (',' || platforms || ',') LIKE '%,twitter,%'
          AND max_posts_total IS NOT NULL AND posts_made < max_posts_total
          AND suffix IS NOT NULL AND suffix <> ''
        ORDER BY id LIMIT 1;" 2>/dev/null | tr -d '\n' || echo "")

    PHASE_B_PROMPT=$(mktemp)
    cat > "$PHASE_B_PROMPT" <<PROMPT_EOF
You are the Social Autoposter Twitter/X engagement bot.

$BROWSER_INSTRUCTIONS

Read $SKILL_FILE for the full workflow, content rules, and platform details.

EXCLUSIONS - do NOT engage with these accounts (skip and mark as 'skipped' with reason 'excluded_author'):
- Excluded authors: $EXCLUDED_AUTHORS
- Excluded Twitter accounts: $EXCLUDED_TWITTER

CRITICAL - Reply posting: Use the SAME mcp__twitter-agent__ browser session you used in Step 2 (navigate). Do NOT call scripts/twitter_browser.py reply: that launches a second Chromium against the same profile dir as the MCP, which wedges x.com on a Loading state and times out. NEVER use mcp__playwright-extension__*, mcp__isolated-browser__*, or mcp__macos-use__* browser tools for posting either.
CRITICAL: If a browser_click or browser_type fails (stale ref, button not found, page not ready, page wedged on Loading), take a fresh mcp__twitter-agent__browser_snapshot and retry up to 2 times.
CRITICAL: TECHNICAL FAILURES ARE NOT TERMINAL. If after retries the post still failed for any technical reason (browser, network, MCP, x.com unreachable, page rendering issue), DO NOT call reply_db.py skipped. Leave the row in 'processing' status (i.e., do nothing further with it) and move on to the next pending item. The post-run cleanup will reset 'processing' rows back to 'pending' so the next engage run retries automatically.
CRITICAL: ONLY call reply_db.py skipped for content/policy reasons (e.g., light_acknowledgment, drive_by_self_promo_link_drop, hostile_user, off_topic, troll, mod_removal, excluded_author). NEVER skip for technical browser/network failures: those must be retry-able.

## Respond to pending Twitter/X replies ($PENDING_COUNT total)

### Priority order:
1. **Replies on our original posts** (is_our_original_post=1) - highest priority
2. **Direct questions** ("what tool", "how do you", "can you share")
3. **Everything else** - general engagement

### Tiered link strategy:
- **Tier 1 (default):** No link. Genuine engagement, expand topic.
- **Tier 2 (natural mention):** Conversation touches a topic matching a project in config. Recommend it casually as a tool you've come across.
- **Tier 3 (direct ask):** They ask for link/tool/source. Give it immediately.

## FEEDBACK FROM PAST PERFORMANCE (use this to write better replies):
$TOP_REPORT

$STYLES_BLOCK

## Per-project voice map
For each reply you draft, look up the matched project's voice block below and apply it: follow \`voice.tone\`, never violate any item in \`voice.never\`, mirror \`voice.examples\` / \`voice.examples_good\` when present.
$PROJECTS_VOICE_JSON

## Resolving the parent post (replaces the old prompt-blob index)
Each pending row's \`project_name\` is a best-effort guess made at scan time. After navigating the thread (Step 2), extract the parent tweet ID from the page URL/DOM and resolve it via:
  python3 $REPO_DIR/scripts/lookup_post.py twitter PARENT_TWEET_ID
Returns JSON: {"project": "fazm", "our_content": "...full text...", "thread_url": "..."} or {"project": null} if it's not one of our posts.

Here are the replies to process:
$PENDING_DATA

CRITICAL: Reply in the SAME LANGUAGE as the message you are responding to. Match the language exactly.
CRITICAL: Process EVERY reply. For each: either post a response and mark as 'replied', OR mark as 'skipped' with a skip_reason.

CRITICAL: For ALL database operations, use the reply_db.py helper (NOT raw psql):
  python3 $REPO_DIR/scripts/reply_db.py processing ID          # BEFORE posting
  python3 $REPO_DIR/scripts/reply_db.py replied ID "reply text" [url] [engagement_style] [is_recommendation]   # AFTER posting. engagement_style is TONE (critic, storyteller, etc). is_recommendation is "1" ONLY when you casually mentioned a project (Tier 2/3); leave blank otherwise. Tone and intent are independent.
  python3 $REPO_DIR/scripts/reply_db.py skipped ID "reason"
  python3 $REPO_DIR/scripts/reply_db.py skip_batch '{"ids":[1,2,3],"reason":"..."}'
  python3 $REPO_DIR/scripts/reply_db.py status
NEVER use psql directly for reply status updates.

### Project tracking on replies
When you recommend a project in a reply (Tier 2 or Tier 3), set project_name on the reply:
  source ~/social-autoposter/.env
  psql "\$DATABASE_URL" -c "UPDATE replies SET project_name='PROJECT_NAME' WHERE id=REPLY_ID;"
This lets the DM pipeline know which project the conversation is about.

MANDATORY reply flow for every item:
  Step 1: python3 reply_db.py processing ID      <- mark BEFORE posting
  Step 2: NAVIGATE TO THE THREAD AND READ CONTEXT (mandatory, do NOT skip).
          Do NOT draft a reply from the notification snippet alone — the snippet
          is truncated and lacks the parent tweet content + sibling replies.
          a) mcp__twitter-agent__browser_navigate to their_comment_url
          b) mcp__twitter-agent__browser_snapshot (or browser_run_code) to read:
             - the FULL parent tweet text (our original post if this is on our thread)
             - the immediate ancestors of their_comment_id (so you understand the
               conversational beat being replied to)
             - sibling replies (so you don't repeat what someone else already said)
          c) Extract the parent tweet ID (the long numeric string after \`/status/\`)
             from the URL chain or page DOM. Resolve it:
               python3 $REPO_DIR/scripts/lookup_post.py twitter PARENT_TWEET_ID
             If the response has a non-null \"project\", that's our post — OVERRIDE
             the reply row and use that project's voice for drafting:
               source ~/social-autoposter/.env
               psql "\$DATABASE_URL" -c "UPDATE replies SET project_name='RESOLVED_PROJECT' WHERE id=REPLY_ID;"
             Use the returned \"our_content\" as the FULL text of the post being
             replied to (more accurate than the truncated our_content in PENDING_DATA).
             If \"project\" is null, we're a guest in someone else's thread; keep
             the existing project_name and follow global content rules.
  Step 3: Draft the reply using the resolved project's voice + chosen engagement
          style. 1-2 sentences. NEVER em dashes. Match parent tweet language.
  Step 3a: ACTIVE CAMPAIGN SUFFIX (MCP fallback, mirrors Reddit's pattern).
          The phase-B reply path goes through mcp__twitter-agent__browser_type
          (twitter_browser.py reply wedges against the same MCP profile), so
          the tool layer can NOT inject the campaign suffix — the LLM has to
          do it by hand. Active Twitter campaign (precomputed by the shell):
            TWITTER_CAMPAIGN_ID=$TWITTER_CAMPAIGN_ID
            TWITTER_CAMPAIGN_SUFFIX_LITERAL='$TWITTER_CAMPAIGN_SUFFIX_LITERAL'
            TWITTER_CAMPAIGN_SAMPLE_RATE=$TWITTER_CAMPAIGN_SAMPLE_RATE
          If TWITTER_CAMPAIGN_SUFFIX_LITERAL is empty, do nothing extra.
          Otherwise: flip a coin at TWITTER_CAMPAIGN_SAMPLE_RATE. On heads,
          append the literal suffix verbatim to YOUR_REPLY_TEXT and remember
          that the campaign fired (CAMPAIGN_FIRED=1 + CAMPAIGN_ID_TO_BUMP=$TWITTER_CAMPAIGN_ID).
          On tails, leave the text alone and CAMPAIGN_FIRED=0.
          Do this BEFORE Step 4, since the typed text in Step 4c must include
          the suffix. The literal text rule is the entire point: never paraphrase
          or reformat the suffix.
  Step 4: Post the reply via the SAME mcp__twitter-agent__ browser from Step 2.
          a) mcp__twitter-agent__browser_snapshot to refresh element refs.
          b) Find the reply textbox: role="textbox" with name like "Post your reply"
             or "Post text". Then mcp__twitter-agent__browser_click on its ref.
          c) mcp__twitter-agent__browser_type the YOUR_REPLY_TEXT into that textbox.
             Pass submit=false (we click the Reply button explicitly in step e).
          d) mcp__twitter-agent__browser_snapshot again (refs can shift after typing).
          e) Find the submit button: role="button" with name="Reply", or selector
             [data-testid="tweetButtonInline"]. mcp__twitter-agent__browser_click it.
             Do NOT match a generic "Reply" by accessible name without checking testid:
             every reply icon on the page also reads as "Reply" and you'll click the
             wrong one.
          f) Wait ~3s, then mcp__twitter-agent__browser_snapshot to confirm the
             textbox is empty (= post landed). If your draft text is still in the
             textbox after 8s, treat as a failed click and retry per the rule above.
          g) Capture REPLY_URL:
             - mcp__twitter-agent__browser_navigate to https://x.com/m13v_/with_replies
             - mcp__twitter-agent__browser_snapshot
             - Find the topmost link matching /m13v_/status/<digits>. That is REPLY_URL.
             If no fresh reply URL appears within 30s, leave REPLY_URL empty and
             continue to Step 5 (the reply IS posted; we just lack the URL link).
  Step 5: python3 reply_db.py replied ID "reply text" REPLY_URL ENGAGEMENT_STYLE [IS_RECOMMENDATION]   <- mark AFTER success. ENGAGEMENT_STYLE is TONE (e.g. critic, storyteller). Pass IS_RECOMMENDATION="1" ONLY when the reply casually recommends a project (Tier 2/3); leave unset otherwise. Tone and intent are independent. Use the FINAL TYPED TEXT (with any campaign suffix from Step 3a) as "reply text" so the stored content matches what was posted.
  Step 5a: If CAMPAIGN_FIRED=1 from Step 3a, attribute this reply to the
          campaign and advance the counter. The reply id is the ID you passed
          to reply_db.py in Step 5 (it returns the row id; or query
          \`SELECT id FROM replies ORDER BY id DESC LIMIT 1\` if you can't parse it):
            python3 $REPO_DIR/scripts/campaign_bump.py --table replies --id REPLY_ROW_ID --campaign-id CAMPAIGN_ID_TO_BUMP
          If CAMPAIGN_FIRED=0, skip this step entirely.
If Step 5 fails, the item stays 'processing' and will be reset to 'pending' on the next run.
If the tweet has been deleted or is unavailable, mark as 'skipped' with reason 'tweet_not_found'.

After every 10 replies, run: python3 $REPO_DIR/scripts/reply_db.py status
PROMPT_EOF

    gtimeout 5400 "$REPO_DIR/scripts/run_claude.sh" "engage-twitter-phaseB" --strict-mcp-config --mcp-config "$MCP_CONFIG_FILE" -p "$(cat "$PHASE_B_PROMPT")" 2>&1 | tee -a "$LOG_FILE" || log "WARNING: Phase B claude exited with code $?"
    rm -f "$PHASE_B_PROMPT"
fi

# Reset any items left in 'processing' after subprocess exit
POST_RESET=$(psql "$DATABASE_URL" -t -A -c "
    WITH upd AS (
        UPDATE replies SET status='pending'
        WHERE platform='x' AND status='processing'
        RETURNING id
    ) SELECT COUNT(*) FROM upd;")
[ "$POST_RESET" -gt 0 ] && log "Post-run: Reset $POST_RESET 'processing' Twitter items back to pending"

# ═══════════════════════════════════════════════════════
# Cleanup
# ═══════════════════════════════════════════════════════
TOTAL_PENDING=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM replies WHERE platform='x' AND status='pending';")
TOTAL_REPLIED=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM replies WHERE platform='x' AND status='replied';")
TOTAL_SKIPPED=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM replies WHERE platform='x' AND status='skipped';")

log "Twitter summary: pending=$TOTAL_PENDING replied=$TOTAL_REPLIED skipped=$TOTAL_SKIPPED"

# Log run to persistent monitor
RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
_COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START" --scripts "engage-twitter-phaseB" 2>/dev/null || echo "0.0000")
# Pull Phase A scan-stage counters out of the log so the dashboard Result
# column shows "scanned N / new N / excluded N" on engage runs. Phase A prints:
#   Processing N mentions...
#   Summary: N new, N already tracked, N excluded, N own account, N too short, N no_tweet_id
# We normalize to scanned/new/excluded/unmatched. Empty (Phase A failed before
# printing) -> no --scan arg, dashboard falls back to old rendering.
TW_SCAN_PROC_LINE=$(grep -m1 -E "^Processing [0-9]+ mentions\.\.\.$" "$LOG_FILE" 2>/dev/null || true)
TW_SCAN_SUMMARY_LINE=$(grep -m1 -E "^Summary: [0-9]+ new" "$LOG_FILE" 2>/dev/null || true)
TW_SCAN_ARG=""
if [ -n "$TW_SCAN_PROC_LINE" ] || [ -n "$TW_SCAN_SUMMARY_LINE" ]; then
  tw_scanned=$(echo "$TW_SCAN_PROC_LINE" | grep -oE "[0-9]+" | head -1)
  tw_new=$(echo "$TW_SCAN_SUMMARY_LINE" | grep -oE "[0-9]+ new" | grep -oE "[0-9]+" | head -1)
  tw_already=$(echo "$TW_SCAN_SUMMARY_LINE" | grep -oE "[0-9]+ already tracked" | grep -oE "[0-9]+" | head -1)
  tw_excluded=$(echo "$TW_SCAN_SUMMARY_LINE" | grep -oE "[0-9]+ excluded" | grep -oE "[0-9]+" | head -1)
  tw_own=$(echo "$TW_SCAN_SUMMARY_LINE" | grep -oE "[0-9]+ own account" | grep -oE "[0-9]+" | head -1)
  tw_short=$(echo "$TW_SCAN_SUMMARY_LINE" | grep -oE "[0-9]+ too short" | grep -oE "[0-9]+" | head -1)
  tw_noid=$(echo "$TW_SCAN_SUMMARY_LINE" | grep -oE "[0-9]+ no tweet_id" | grep -oE "[0-9]+" | head -1)
  # excluded pill = excluded + own_account; unmatched pill = too_short + no_tweet_id
  tw_excl_total=$(( ${tw_excluded:-0} + ${tw_own:-0} ))
  tw_unm_total=$(( ${tw_short:-0} + ${tw_noid:-0} ))
  parts=""
  [ -n "$tw_scanned" ] && parts="${parts}scanned=${tw_scanned},"
  [ -n "$tw_new" ]     && parts="${parts}new=${tw_new},"
  [ -n "$tw_already" ] && parts="${parts}already=${tw_already},"
  [ "$tw_excl_total" -gt 0 ] && parts="${parts}excluded=${tw_excl_total},"
  [ "$tw_unm_total"  -gt 0 ] && parts="${parts}unmatched=${tw_unm_total},"
  TW_SCAN_ARG="${parts%,}"
fi
if [ -n "$TW_SCAN_ARG" ]; then
  python3 "$REPO_DIR/scripts/log_run.py" --script "engage_twitter" --posted "$TOTAL_REPLIED" --skipped "$TOTAL_SKIPPED" --failed 0 --cost "$_COST" --elapsed "$RUN_ELAPSED" --scan "$TW_SCAN_ARG"
else
  python3 "$REPO_DIR/scripts/log_run.py" --script "engage_twitter" --posted "$TOTAL_REPLIED" --skipped "$TOTAL_SKIPPED" --failed 0 --cost "$_COST" --elapsed "$RUN_ELAPSED"
fi

# Delete old logs
find "$LOG_DIR" -name "engage-twitter-*.log" -mtime +7 -delete 2>/dev/null || true

log "=== Twitter engagement complete: $(date) ==="
