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

# Browser-profile lock first (shared with other twitter pipelines), then pipeline lock.
source "$(dirname "$0")/lock.sh"
# Harness-only browser bootstrap (twitter-agent path fully removed 2026-05-19).
# Sets MCP_CONFIG_FILE, BROWSER_INSTRUCTIONS, exports TWITTER_CDP_URL=9555.
source "$(dirname "$0")/lib/twitter-backend.sh"

echo "[$(date +%H:%M:%S)] Acquiring twitter-browser lock (pid=$$)..." | tee -a "$LOG_FILE"
acquire_lock "twitter-browser" 3600 2>>"$LOG_FILE"
echo "[$(date +%H:%M:%S)] twitter-browser lock held (pid=$$)" | tee -a "$LOG_FILE"
# Probe + launch harness Chrome on port 9555 if needed, then sweep leftover tabs.
ensure_twitter_browser_for_backend 2>&1 | tee -a "$LOG_FILE"
echo "[$(date +%H:%M:%S)] Acquiring twitter (pipeline) lock (pid=$$)..." | tee -a "$LOG_FILE"
acquire_lock "twitter" 3600 2>>"$LOG_FILE"

# Load secrets
# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
SKILL_FILE="$REPO_DIR/SKILL.md"
BATCH_SIZE=500

# All Twitter engage DB I/O routes through scripts/engage_twitter_helper.py
# (HTTP API at /api/v1/*) since 2026-05-18. DATABASE_URL is no longer
# required for this script and is left for downstream tooling only.
ENGAGE_TWITTER_HELPER="$REPO_DIR/scripts/engage_twitter_helper.py"
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

# Reset any 'processing' items older than 2 hours back to 'pending'.
# Server-side WHERE in /api/v1/replies/reset-stuck mirrors the old SQL.
RESET_COUNT=$(python3 "$ENGAGE_TWITTER_HELPER" reset-stuck-replies)
[ "$RESET_COUNT" -gt 0 ] && log "Phase B: Reset $RESET_COUNT stuck 'processing' Twitter items back to pending"

PENDING_COUNT=$(python3 "$ENGAGE_TWITTER_HELPER" pending-count)

if [ "$PENDING_COUNT" -eq 0 ]; then
    log "Phase B: No pending Twitter replies. Done!"
else
    log "Phase B: $PENDING_COUNT pending Twitter replies to process"

    # /api/v1/replies/next-pending returns the SAME join (replies + posts)
    # with the SAME priority ordering (our_thread first, then discovered_at
    # ASC) the previous json_agg() build emitted; the helper just reshapes
    # the response into the legacy field set the prompt expects.
    PENDING_DATA=$(python3 "$ENGAGE_TWITTER_HELPER" pending-data --batch-size "$BATCH_SIZE")

    # JOIN-aware emptiness guard (2026-05-26). /api/v1/replies/counts returns
    # the raw pending count (no JOIN), but /api/v1/replies/next-pending INNER
    # JOINs posts; orphan replies whose post_id no longer exists make these
    # two disagree. Without this guard, Phase B burns the full gtimeout
    # holding the twitter-browser lock while Claude finds nothing to do,
    # starving dm-outreach-twitter and dm-replies-twitter in the lock queue
    # for 30+ min. Skip Phase B and release the browser lock early when
    # /next-pending returns 0 rows.
    PENDING_REAL_COUNT=$(echo "$PENDING_DATA" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read() or '{}')
    if isinstance(d, dict):
        rows = d.get('replies', [])
    elif isinstance(d, list):
        rows = d
    else:
        rows = []
    print(len(rows))
except Exception:
    print(0)
" 2>/dev/null || echo 0)

    if [ "$PENDING_REAL_COUNT" -eq 0 ]; then
        log "Phase B: counts says $PENDING_COUNT pending but JOIN returned 0 rows (likely orphan replies whose post_id is missing). Skipping Phase B."
        log "Releasing twitter + twitter-browser locks so other pipelines (dm-outreach-twitter, dm-replies-twitter) can run."
        release_lock "twitter" 2>>"$LOG_FILE" || true
        release_lock "twitter-browser" 2>>"$LOG_FILE" || true
        rm -f "$HOME/.claude/twitter-browser-lock.json" 2>/dev/null || true
        RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
        log "=== Twitter Engagement Run done (no real work): elapsed=${RUN_ELAPSED}s ==="
        python3 "$REPO_DIR/scripts/log_run.py" --script "engage_twitter" --posted 0 --skipped 0 --failed 0 --cost 0 --elapsed "$RUN_ELAPSED" 2>/dev/null || true
        exit 0
    fi

    log "Phase B: $PENDING_REAL_COUNT pending Twitter replies confirmed via JOIN (counts said $PENDING_COUNT)"

    # Per-project voice map (so each reply can be drafted in the matched project's voice)
    PROJECTS_VOICE_JSON=$(python3 -c "
import json
c = json.load(open('$REPO_DIR/config.json'))
print(json.dumps({p['name']: p.get('voice', {}) for p in c.get('projects', []) if p.get('voice')}, indent=2))
" 2>/dev/null || echo "{}")

    # Engagement-style picker (2026-05-19): pick ONE assigned style per
    # reply iteration. The picked style flows two places: (1) --style
    # filter for top_performers.py so the per-style exemplars match the
    # assignment, (2) saps_render_style_block so the prompt embeds the
    # same assignment. On invent mode picked_style is empty and
    # top_performers stays unfiltered.
    source "$REPO_DIR/skill/styles.sh"
    STYLE_ASSIGN_FILE=$(mktemp -t saps_twitter_eng_assign_XXXXXX.json)
    saps_pick_style twitter replying "$STYLE_ASSIGN_FILE" >/dev/null 2>&1 || true
    PICKED_STYLE=$(python3 -c "
import json
try:
    with open('$STYLE_ASSIGN_FILE') as f:
        d = json.load(f)
    print(d.get('style') or '')
except Exception:
    print('')
" 2>/dev/null)
    PICKED_MODE=$(python3 -c "
import json
try:
    with open('$STYLE_ASSIGN_FILE') as f:
        d = json.load(f)
    print(d.get('mode') or 'use')
except Exception:
    print('use')
" 2>/dev/null)
    STYLES_BLOCK=$(saps_render_style_block "$STYLE_ASSIGN_FILE" twitter replying)
    rm -f "$STYLE_ASSIGN_FILE" 2>/dev/null || true

    # Top performers feedback report — filtered to the picked style when
    # in 'use' mode so the few-shot exemplars match the assignment.
    if [ -n "$PICKED_STYLE" ]; then
        TOP_REPORT=$(python3 "$REPO_DIR/scripts/top_performers.py" --platform twitter --style "$PICKED_STYLE" 2>/dev/null || echo "(top performers report unavailable)")
    else
        TOP_REPORT=$(python3 "$REPO_DIR/scripts/top_performers.py" --platform twitter 2>/dev/null || echo "(top performers report unavailable)")
    fi

    # Precompute active Twitter campaign suffix + sample_rate + id for the
    # prompt to inline. Phase B replies go through the browser typing tool
    # (twitter_browser.py reply wedges against the same profile), so tool-level
    # injection is unavailable; the LLM has to flip a coin and append the
    # literal suffix by hand. When no active campaign exists, all three vars
    # resolve to empty strings and the prompt's "if empty, do nothing extra"
    # branch fires. Mirrors the Reddit MCP-fallback pattern in
    # engage-dm-replies.sh.
    # Three psql one-liners collapsed into one HTTP call via active-campaign.
    # Returns JSON {} when no active campaign matches, or
    # {id, suffix, sample_rate}. Same WHERE (status='active', platform
    # contains twitter, budget remaining, non-empty suffix) runs server-side.
    TWITTER_CAMPAIGN_JSON=$(python3 "$ENGAGE_TWITTER_HELPER" active-campaign 2>/dev/null || echo "{}")
    TWITTER_CAMPAIGN_SUFFIX_LITERAL=$(echo "$TWITTER_CAMPAIGN_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.stdout.write(d.get('suffix','') or '')" 2>/dev/null || echo "")
    TWITTER_CAMPAIGN_SAMPLE_RATE=$(echo "$TWITTER_CAMPAIGN_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.stdout.write(str(d.get('sample_rate','') or ''))" 2>/dev/null || echo "")
    TWITTER_CAMPAIGN_ID=$(echo "$TWITTER_CAMPAIGN_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.stdout.write(str(d.get('id','') or ''))" 2>/dev/null || echo "")

    PHASE_B_PROMPT=$(mktemp)
    cat > "$PHASE_B_PROMPT" <<PROMPT_EOF
You are the Social Autoposter Twitter/X engagement bot.

$BROWSER_INSTRUCTIONS

Read $SKILL_FILE for the full workflow, content rules, and platform details.

EXCLUSIONS - do NOT engage with these accounts (skip and mark as 'skipped' with reason 'excluded_author'):
- Excluded authors: $EXCLUDED_AUTHORS
- Excluded Twitter accounts: $EXCLUDED_TWITTER

### BOT / ENGAGEMENT-LOOP ESCAPE HATCH (use sparingly, but use it)
We maintain a universal author blocklist in Postgres (\`author_blocklist\`),
consulted at /api/v1/replies POST time. A single block recorded by ANY of
our accounts/installs applies to EVERY future engagement from EVERY of our
accounts — universal scope, by design. The velocity gate already covers
"this handle has gotten too many replies from us in 24h/7d"; this lane is
for the LLM-judgment cases velocity cannot catch.

When to add a block (your judgment, exercised CONSERVATIVELY):
- The handle is plainly an AI/bot account: templated phrasing, generic
  filler answers, name pattern like \`SomethingAI\` / \`Foo_GPT\`, bio reads
  "AI agent that replies to…"
- We are clearly stuck in a reciprocal engagement loop with this handle
  (they reply to every one of our posts, we reply to every one of theirs,
  no substance is exchanged)
- The handle is engagement farming (mass low-effort replies across the
  platform, not actually engaging with the topic)

DO NOT add a block for: someone we disagree with, a hostile-but-human
critic, a low-quality but human reply, or a single bad interaction.
Skip those (status='skipped') — blocking is permanent until manually
removed and applies to all our accounts.

How to add the block (run BEFORE marking the current reply skipped):
  python3 \$REPO_DIR/scripts/reply_db.py blocklist add x HANDLE \\
    --reason "<one-line judgment, e.g. 'AI-named account, templated replies>" \\
    --classification {bot|engagement_loop} \\
    --source-reply-id REPLY_ID

Then mark the current reply skipped with a clear reason:
  python3 \$REPO_DIR/scripts/reply_db.py skipped REPLY_ID "blocklist_added:HANDLE"

You can verify with:
  python3 \$REPO_DIR/scripts/reply_db.py blocklist check x HANDLE

CRITICAL - Reply posting: Use the SAME browser session you used in Step 2 (navigate), via the tools described in the BROWSER BACKEND block above. Do NOT call scripts/twitter_browser.py reply: that launches a second Chromium against the same profile dir, which wedges x.com on a Loading state and times out. NEVER use any other browser MCP (playwright-extension, isolated-browser, macos-use, etc.) for posting.
CRITICAL: If a click or type fails (stale ref, button not found, page not ready, page wedged on Loading), re-snapshot the page (per the BROWSER BACKEND block) and retry up to 2 times.
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
When you recommend a project in a reply (Tier 2 or Tier 3), set project_name on the reply via reply_db.py (which writes through /api/v1/replies/:id — DO NOT shell out to psql):
  python3 $REPO_DIR/scripts/reply_db.py set_project REPLY_ID "PROJECT_NAME"
This lets the DM pipeline know which project the conversation is about.

MANDATORY reply flow for every item:
  Step 1: python3 reply_db.py processing ID      <- mark BEFORE posting
  Step 2: NAVIGATE TO THE THREAD AND READ CONTEXT (mandatory, do NOT skip).
          Do NOT draft a reply from the notification snippet alone — the snippet
          is truncated and lacks the parent tweet content + sibling replies.
          a) Navigate to their_comment_url (use the navigate tool from the BROWSER BACKEND block above)
          b) Snapshot or query the DOM (per the BROWSER BACKEND block) to read:
             - the FULL parent tweet text (our original post if this is on our thread)
             - the immediate ancestors of their_comment_id (so you understand the
               conversational beat being replied to)
             - sibling replies (so you don't repeat what someone else already said)
          c) Extract the parent tweet ID (the long numeric string after \`/status/\`)
             from the URL chain or page DOM. Resolve it:
               python3 $REPO_DIR/scripts/lookup_post.py twitter PARENT_TWEET_ID
             If the response has a non-null \"project\", that's our post — OVERRIDE
             the reply row and use that project's voice for drafting:
               python3 $REPO_DIR/scripts/reply_db.py set_project REPLY_ID "RESOLVED_PROJECT"
             Use the returned \"our_content\" as the FULL text of the post being
             replied to (more accurate than the truncated our_content in PENDING_DATA).
             If \"project\" is null, we're a guest in someone else's thread; keep
             the existing project_name and follow global content rules.
  Step 3: Draft the reply using the resolved project's voice + chosen engagement
          style. One sentence or a tight fragment — aim for under 100 chars. NEVER em dashes. Match parent tweet language.
  Step 3a: ACTIVE CAMPAIGN SUFFIX (MCP fallback, mirrors Reddit's pattern).
          The phase-B reply path goes through the browser typing tool
          (twitter_browser.py reply wedges against the same profile), so
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
  Step 3b: SHORT-LINK ANY URL IN YOUR_REPLY_TEXT. Mandatory for Tier 2/3 replies
           that drop a brand URL (runner.now, agora.xyz, podlog.io, fazm.ai,
           usenightowl.com, etc.). The browser type path has NO Python
           wrap layer, so a bare URL would be posted as-is and we lose all
           per-post click attribution. Mint a real /r/<code> short link the
           SAME way the post/link-edit pipelines do, in bash:
             WRAP_RESULT=\$(python3 \$REPO_DIR/scripts/dm_short_links.py wrap-post-text \\
               --platform twitter \\
               --project RESOLVED_PROJECT_NAME \\
               --text "YOUR_REPLY_TEXT")
           RESOLVED_PROJECT_NAME must be the EXACT \`name\` field from config.json
           (case-sensitive; e.g. "fazm" lowercase, "Cyrano", "WhatsApp MCP").
           Parse the JSON output (\`{ok, text, minted_session, ...}\`):
             - Use \`text\` (every URL replaced with a /r/<code> short link on
               the project's own domain) as YOUR_REPLY_TEXT going forward; this
               is what you type in Step 4.
             - Save \`minted_session\` as MINTED_SESSION for the Step 5b backfill.
           If \`ok\` is false, log the error and SKIP this reply (leave it to be
           reset to 'pending' on the next run); do NOT type a bare URL. If
           YOUR_REPLY_TEXT contains zero URLs (Tier 1, default case),
           wrap-post-text is a no-op: it returns the text unchanged and
           minted_session is null (that's fine), carry on and skip Step 5b.

  Step 4: Post the reply via the SAME browser session from Step 2 (use the
          tools described in the BROWSER BACKEND block).
          a) Re-snapshot the page to refresh element state.
          b) Find the reply textbox: role="textbox" with name like "Post your reply"
             or "Post text". Click it.
          c) Type YOUR_REPLY_TEXT (post-Step-3b short-link-wrapped form, post-Step-3a
             suffix) into that textbox. Do NOT auto-submit; we click the Reply
             button explicitly in step e.
          d) Re-snapshot the page (refs can shift after typing).
          e) Find the submit button: role="button" with name="Reply", or selector
             [data-testid="tweetButtonInline"]. Click it.
             Do NOT match a generic "Reply" by accessible name without checking testid:
             every reply icon on the page also reads as "Reply" and you'll click the
             wrong one.
          f) Wait ~3s, then re-snapshot to confirm the textbox is empty
             (= post landed). If your draft text is still in the textbox after
             8s, treat as a failed click and retry per the rule above.
          g) Capture REPLY_URL:
             - Navigate to https://x.com/m13v_/with_replies
             - Snapshot the page
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
  Step 5b: BACKFILL SHORT-LINK ATTRIBUTION. If you minted a short link in Step 3b
          (MINTED_SESSION is non-empty AND not the string "null"), stamp it onto
          this reply row now that Step 5 succeeded. The reply id is the same ID
          you passed to reply_db.py in Step 5:
            python3 $REPO_DIR/scripts/dm_short_links.py backfill-reply --minted-session MINTED_SESSION --reply-id ID
          This sets post_links.reply_id so the /r/<code> clicks attribute to this
          engagement reply (same mechanism link-edit pipelines use via backfill-post).
          If Step 3b minted nothing (no URL in the reply, MINTED_SESSION null), skip this step.
If Step 5 fails, the item stays 'processing' and will be reset to 'pending' on the next run.
If the tweet has been deleted or is unavailable, mark as 'skipped' with reason 'tweet_not_found'.

After every 10 replies, run: python3 $REPO_DIR/scripts/reply_db.py status
PROMPT_EOF

    # Phase B Claude timeout: 30 min (was 5400=90 min). Real engage runs
    # complete in 5-15 min. The 90-min cap let a single broken-data run hold
    # the twitter-browser lock for 45+ min and starve DM lanes (see 2026-05-26
    # incident). 30 min is a generous ceiling for legitimate work; the
    # JOIN-aware guard above already cuts the no-op case to <3 s.
    gtimeout 1800 "$REPO_DIR/scripts/run_claude.sh" "engage-twitter-phaseB" --strict-mcp-config --mcp-config "$MCP_CONFIG_FILE" --output-format stream-json --verbose -p "$(cat "$PHASE_B_PROMPT")" 2>&1 | tee -a "$LOG_FILE" || log "WARNING: Phase B claude exited with code $?"
    rm -f "$PHASE_B_PROMPT"
fi

# Reset any items left in 'processing' after subprocess exit. The
# /api/v1/replies/reset-stuck route requires a positive
# older_than_hours; we use 1h here so a freshly-stuck row from this run's
# Claude subprocess gets reset on the next cycle's pre-Phase-B sweep
# instead of immediately (so we don't race a still-progressing Claude that
# JUST set processing_at = NOW()).
POST_RESET=$(python3 "$ENGAGE_TWITTER_HELPER" post-reset)
[ "$POST_RESET" -gt 0 ] && log "Post-run: Reset $POST_RESET 'processing' Twitter items back to pending"

# ═══════════════════════════════════════════════════════
# Cleanup
# ═══════════════════════════════════════════════════════
# One HTTP roundtrip for all three counts instead of three psql one-liners.
COUNTS_JSON=$(python3 "$ENGAGE_TWITTER_HELPER" reply-counts 2>/dev/null || echo '{"pending":0,"replied":0,"skipped":0}')
TOTAL_PENDING=$(echo "$COUNTS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('pending',0))" 2>/dev/null || echo "0")
TOTAL_REPLIED=$(echo "$COUNTS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('replied',0))" 2>/dev/null || echo "0")
TOTAL_SKIPPED=$(echo "$COUNTS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('skipped',0))" 2>/dev/null || echo "0")

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
