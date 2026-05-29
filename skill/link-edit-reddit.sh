#!/usr/bin/env bash
# link-edit-reddit.sh — Edit high-performing Reddit comments to append a project link.
# Splits out from the legacy engage.sh Phase D so a single platform failure
# (e.g. LinkedIn hang) no longer blocks Reddit.
# Called by launchd (com.m13v.social-link-edit-reddit) every 6 hours.

set -euo pipefail

# Cycle ID for cross-cycle cost accounting (see run-reddit-search.sh / engage-reddit.sh
# for the same pattern). Stamps claude_sessions.cycle_id via env inheritance.
BATCH_ID="${BATCH_ID:-lerd-$(date +%Y%m%d-%H%M%S)}"
export BATCH_ID
export SA_CYCLE_ID="$BATCH_ID"

# Pipeline lock at top. We DO NOT acquire reddit-browser at the bash level
# anymore — claude itself acquires it per-post via
# scripts/reddit_browser_lock.py, only around the actual MCP browser
# operations (~15-60s per post). This unblocks peer reddit pipelines
# (engage-reddit, dm-replies-reddit, post-reddit) during the long
# generate_page.py / WebFetch / DB phases of each post.
source "$(dirname "$0")/lock.sh"
# reddit-harness backend (2026-05-29). Sets MCP_CONFIG_FILE, BROWSER_INSTRUCTIONS,
# exports REDDIT_CDP_URL=:9557, provides ensure_reddit_browser_for_backend.
source "$(dirname "$0")/lib/reddit-backend.sh"
acquire_lock "link-edit-reddit" 5400

# Load secrets
# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
SKILL_FILE="$REPO_DIR/SKILL.md"
LOG_DIR="$REPO_DIR/skill/logs"

if [ -z "${DATABASE_URL:-}" ]; then
    echo "ERROR: DATABASE_URL not set in ~/social-autoposter/.env"
    exit 1
fi

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/link-edit-reddit-$(date +%Y-%m-%d_%H%M%S).log"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

RUN_START=$(date +%s)
log "=== Reddit Link Edit Run: $(date) ==="

# A/B gate: per-post deterministic coin flip for the page-gen lane. Mirrors
# scripts/twitter_gen_links.py's TWITTER_PAGE_GEN_RATE behavior. 0.30 means
# ~30% of eligible posts hit the full seo/generate_page.py pipeline; the
# other ~70% fall through to the project's homepage with
# link_source='plain_url_ab_skip'. Per-post hash via Postgres hashtext() so
# the same post stays in the same lane across cron retries — without that
# we'd risk shipping two different lanes for the same post on consecutive
# runs and burn budget. Tunable via env var so cadence sweeps don't need
# code changes. 0.0 disables page-gen; 1.0 restores 100% page-gen.
LINK_EDIT_REDDIT_PAGE_GEN_RATE="${LINK_EDIT_REDDIT_PAGE_GEN_RATE:-0.30}"
PAGE_GEN_RATE_PCT=$(python3 -c "v=float('$LINK_EDIT_REDDIT_PAGE_GEN_RATE'); v=max(0.0,min(1.0,v)); print(int(round(v*100)))")
log "A/B gate: LINK_EDIT_REDDIT_PAGE_GEN_RATE=$LINK_EDIT_REDDIT_PAGE_GEN_RATE (page_gen_lane='page_gen' on ~${PAGE_GEN_RATE_PCT}% of eligible posts; rest go to plain_url_ab_skip)"

EDITABLE=$(psql "$DATABASE_URL" -t -A -c "
    SELECT json_agg(q) FROM (
        SELECT id, platform, our_url, our_content, thread_title, upvotes, project_name,
               CASE WHEN ((hashtext(id::text) % 100) + 100) % 100 < ${PAGE_GEN_RATE_PCT}
                    THEN 'page_gen' ELSE 'ab_skip' END AS page_gen_lane
        FROM posts
        WHERE status='active'
          AND platform='reddit'
          AND posted_at < NOW() - INTERVAL '6 hours'
          AND link_edited_at IS NULL
          AND our_url IS NOT NULL
          AND upvotes > 1
        ORDER BY upvotes DESC NULLS LAST
    ) q;" 2>/dev/null || echo "")

if [ "$EDITABLE" = "null" ] || [ -z "$EDITABLE" ]; then
    log "No Reddit posts eligible for link edit"
    python3 "$REPO_DIR/scripts/log_run.py" --script "link_edit_reddit" --posted 0 --skipped 0 --failed 0 --cost 0 --elapsed $(( $(date +%s) - RUN_START ))
    exit 0
fi

EDITABLE_COUNT=$(echo "$EDITABLE" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "?")
log "Reddit: $EDITABLE_COUNT posts eligible for link edit"

PROMPT_FILE=$(mktemp)
cat > "$PROMPT_FILE" <<PROMPT_EOF
You are the Social Autoposter Reddit link-edit bot.

Read $SKILL_FILE for the full workflow. Execute the Reddit link-edit phase only.

$BROWSER_INSTRUCTIONS

CRITICAL - Browser agent rule: ONLY use the browser tool described in the BROWSER BACKEND block above (mcp__reddit-harness__bh_run). NEVER use generic mcp__playwright-extension__*, mcp__isolated-browser__*, mcp__reddit-agent__*, or mcp__macos-use__* tools. If a bh_run call is blocked or times out, wait 30s and retry (up to 3 times). If still blocked, skip that post.

CRITICAL: This is a single-shot run. NEVER call ScheduleWakeup, CronCreate, CronDelete, CronList, EnterPlanMode, EnterWorktree, or any deferred-execution / scheduling tool. You MUST complete or skip every post in this one run; do not defer work to "a future run". If you hit a hard block, mark the post SKIPPED via step 9 and move on to the next post.

EXECUTION MODEL — STRICT SEQUENTIAL, NO BATCHING (read this twice):
- Process posts ONE AT A TIME. Run the FULL chain (steps 1 → 8) end-to-end for post N before reading post N+1.
- NEVER batch generate_page.py calls in parallel. NEVER spawn page-gen subprocesses with double-fork (\`nohup ... &\`, \`disown\`, \`setsid\`, \`os.fork\`). NEVER write a polling loop that watches /tmp/seo_*.json. Each generate_page.py is a foreground call that you wait for synchronously.
- Step 8's DB UPDATE for post N MUST complete before you start step 1 for post N+1. This protects interim work: if the run is killed mid-stream, the first N-1 posts are fully shipped and only ONE post is in flight at most.
- The reddit-browser lock is NOT held by the parent shell. You acquire/release it explicitly per post (steps 6.5 and 7.5), so peer reddit pipelines can use the browser during your generate_page.py / WebFetch / DB phases.

Reddit posts eligible for editing:
$EDITABLE

Process ALL of them SEQUENTIALLY (one at a time, full chain per post). For each post:
1. Read ~/social-autoposter/config.json to get the projects list.
2. Pick the project whose topics are the CLOSEST match to thread_title + our_content.
   a. First check the project_name column. If it is set AND its topics/description fit the thread, use it.
   b. If project_name is set but CLEARLY does not fit the thread (e.g. Cyrano tagged to a law firm billing thread), treat it as a bad upstream tag and scan config.json for a project that DOES fit. If you find one, use that project instead and also run: psql "\$DATABASE_URL" -c "UPDATE posts SET project_name='BETTER_PROJECT' WHERE id=POST_ID" so the correction is persisted.
   c. If project_name is NOT set, match by topics. Be generous: if the thread touches agents, automation, desktop, memory, or anything related to the project descriptions, it's a match.
   d. ONLY if no project in config.json fits at all, mark it skipped (see step 9) and move on. Frame it as recommending a cool tool you've come across, NOT as something you built.
3. PAGE-GEN LANE GATE — read the post's \`page_gen_lane\` field (set deterministically by the pipeline; do NOT override).
   - If \`page_gen_lane == "ab_skip"\`: SKIP the full SEO page generation entirely. Set LINK_URL = the matched project's homepage from config.json (the \`website\` field) and LINK_SOURCE="plain_url_ab_skip". Continue to step 4. The /r/<code> short-link wrap in step 5 still mints attribution on the project's own domain, so we get click data for this lane to compare against seo_page lane CTR.
   - If \`page_gen_lane == "page_gen"\` AND the matched project has a landing_pages config: continue to step 3a below.
   - If \`page_gen_lane == "page_gen"\` BUT the matched project has NO landing_pages config: skip page-gen, set LINK_URL = project homepage, LINK_SOURCE="plain_url_no_lp", continue to step 4.

3a. If the matched project has a landing_pages config (with repo, base_url), generate a fresh SEO page for this thread by delegating to the unified generator:
   a. Decide a SHORT keyword phrase (3-6 words) that captures what page would help this thread's audience. Think SEO intent, not headline copy. Examples: "local ai agent", "macos accessibility automation", "self hosted llm inference".
   b. Derive a URL slug from the keyword: lowercase, kebab-case, alphanumeric and hyphens only, max 50 chars. Examples: "local-ai-agent", "macos-accessibility-automation".
   c. Run the unified SEO page generator (it loads the @m13v/seo-components palette, picks content type, builds the page, commits, pushes, verifies the live URL, and writes the seo_keywords row that surfaces in the dashboard activity feed). The generator's prompt has its own model-driven Step 0 reuse-or-redirect decision: if an existing page on the site already serves this keyword's intent, the generator will consolidate (308 redirect this slug to the existing page) instead of building a duplicate. Trust that decision; do not pre-filter for reuse here. Use the Bash tool:
        python3 ~/social-autoposter/seo/generate_page.py --product PROJECT_NAME --keyword "KEYWORD_PHRASE" --slug "url-slug" --trigger reddit
      This call can take 10-40 minutes per page (Cloud Run staging-then-tag deploys on mk0r are the slow end). The final stdout is a JSON object; parse it. On success it contains "success": true and "page_url": "https://...". On failure it contains "success": false and "error": "...".
   d. If success, set LINK_URL = the \`page_url\` from the JSON output and LINK_SOURCE="seo_page".
   e. If failure (success: false in the JSON), fall back GRACEFULLY (mirrors the Twitter pipeline behavior in scripts/twitter_gen_links.py): set LINK_URL = the project's homepage from config.json (the \`website\` field for the matched project) and set LINK_SOURCE="plain_url_fallback:<reason>" where <reason> is a SHORT snake_case tag derived from the JSON error string (preferred values: timeout, no_page_url, deploy_failed, build_failed, push_failed; otherwise pick a sensible 1-3 word snake_case summary). Do NOT skip the post; continue to step 4. The short-link wrap in step 5 will still mint a /r/<code> on the project's own domain, so click attribution works on the homepage URL too.
4. Write the link sentence as a CONTEXTUAL BRIDGE, not a citation or footnote. This is a structured 4-step sub-task; do NOT shortcut it.
   a. Re-read our_content (the comment we already left on this Reddit thread). Identify the SINGLE strongest claim, mechanism, or specific number we said in that comment (examples: "auto-rephrasing on revisit", "the 81 number on the rubric", "scoring distractor quality", "200ms p95 latency", "structured output across nested tool calls"). Pick ONE concrete thing, not a category.
   b. Read the landing page at LINK_URL (use WebFetch on LINK_URL). Identify the SPECIFIC mechanism on the page that delivers the thing from step 4a (one feature, one capability, named in the page's own language).
   c. Write 1-2 sentences (max ~35 words) that explicitly bridge: comment-claim → product mechanism → URL. The sentence MUST name at least ONE concrete capability inline (verb + noun, e.g. "scores against a rubric and rephrases the same fact on revisit"); NOT a category ("a tool for studying", "something for this"). The URL is the last token; nothing after it.
   d. Voice depends on the matched project's \`voice_relationship\` field in config.json (read it before drafting):
      - voice_relationship == "first_party": Frame as our own creation. Bridge form: "fwiw the [specific-thing-from-4a] is from [PROJECT_NAME], a thing I built that [specific-mechanism-from-4b], LINK_URL". Concrete first_party examples (do NOT copy verbatim; rewrite to match each post's actual claim):
        * "fwiw the 81 number is from a rubric we trained on med-school decks, scores factual correctness, distractor quality, and rephrases on revisit so you can't pattern-match the stem, https://studyly.io/t/anki-card-generator-for-medical-school"
        * "we shipped this for the rephrase-on-revisit problem specifically, it scores against a 4-axis rubric and re-asks the same fact in different stems, https://studyly.io/t/active-recall-question-generator"
      - voice_relationship == "third_party": You are an outside observer pointing at the project's mechanism. Bridge form: "fwiw [PROJECT_NAME] handles the [specific-thing-from-4a] via [specific-mechanism-from-4b], LINK_URL". Do NOT use "I built" / "we shipped" / "we made". Do NOT pretend you stumbled on it ("found this", "came across this") either; that drops engagement. Stay matter-of-fact and concrete.
   e. BANNED phrasing (rejects the bridge structure, costs CTR):
      - Bare citation with no mechanism: "fwiw the X number is from PROJECT, URL" with no named mechanism.
      - Generic verbs with no object: "a tool for exactly this", "does this" (no concrete mechanism named).
      - "Click here", "check it out", "give it a try" (Reddit downvotes pitchy CTAs).
      - For voice_relationship=third_party only: "I built" / "we shipped" / "we made" (ownership claims on a project you don't own).
      - For voice_relationship=first_party only: "I found this", "there's a tool", "came across this", "saw this manual", "found this guide" (pretends we're a neutral commenter pointing at someone else's project).
   f. SELF-REVIEW before posting: read your sentence aloud. Does it (1) reference the comment's specific claim, (2) name a concrete product mechanism, and (3) end on the URL? If any of the three is missing, rewrite. If after one rewrite it still doesn't pass all three checks, write a fresh sentence from scratch.
5. URL-WRAP THE LINK TEXT for click attribution. This MUST run for every LINK_SOURCE (seo_page, plain_url_fallback:*, plain_url_no_lp). The wrap helper accepts homepage URLs and mints a /r/<code> on the project's own domain. Run:
     python3 ~/social-autoposter/scripts/dm_short_links.py wrap-post-text \\
       --text "YOUR_LINK_SENTENCE_WITH_URL" \\
       --platform reddit \\
       --project PROJECT_NAME
   PROJECT_NAME must be the EXACT \`name\` field from config.json (case-sensitive; e.g. "fazm" lowercase, "Cyrano", "WhatsApp MCP"). Parse the JSON output. Use \`text\` (URL replaced with /r/<code>) as the FINAL LINK_TEXT for steps 6 and 7. Keep \`minted_session\` for step 8. If wrap returns ok=false, log the error and skip this post (do NOT post a raw URL).
6. Append the wrapped LINK_TEXT to our_content with a blank line separator.
6.5. ACQUIRE the reddit-browser lock NOW (just before any reddit-agent browser call). This is the ONLY moment you may touch the browser:
       LOCK_OUT=\$(python3 ~/social-autoposter/scripts/reddit_browser_lock.py acquire --timeout 600 2>&1)
     - If stdout starts with "OK", proceed to step 7.
     - If "BUSY", a peer reddit pipeline owns the browser and didn't release within 10 min. Treat as a TRANSIENT skip (step 10B): leave link_edited_at NULL, log the reason, move on to the NEXT post. Do NOT call step 7 without the lock — collisions on the same chrome profile crash both runs.
     - If "ERROR", same handling as BUSY: TRANSIENT skip, leave link_edited_at NULL, move on.
7. Navigate to old.reddit.com comment permalink via the reddit-agent browser. Click "edit", append the wrapped link text to the existing content, save, verify.
7.5. RELEASE the reddit-browser lock IMMEDIATELY after the edit confirms or fails. This is mandatory — failing to release it blocks every other reddit pipeline:
       python3 ~/social-autoposter/scripts/reddit_browser_lock.py release
     Run this even if step 7 raised, errored, or you're skipping the post. Wrap step 7 in a way that step 7.5 ALWAYS executes (mental try/finally). The release is idempotent and safe to call multiple times.
8. After each successful edit, update the DB (including link_source so we can A/B compare seo_page vs plain_url_fallback:* vs plain_url_no_lp click-through rates, same as Twitter does in scripts/twitter_gen_links.py) and backfill short-link attribution. THIS MUST RUN BEFORE YOU START THE NEXT POST:
   psql "\$DATABASE_URL" -c "UPDATE posts SET link_edited_at=NOW(), link_edit_content='LINK_TEXT', link_source='LINK_SOURCE' WHERE id=POST_ID"
   python3 ~/social-autoposter/scripts/dm_short_links.py backfill-post --minted-session MINTED_SESSION --post-id POST_ID
9. COMMITMENT GUARDRAILS (never violate these):
   - NEVER suggest, offer, or agree to calls, meetings, demos, or video chats.
   - NEVER promise to share links, files, or resources you don't have right now. Only share links from config.json projects (plus any new landing page you just deployed).
   - NEVER offer to DM or send anything outside the comment.
   - NEVER make time-bound promises.
10. SKIP HANDLING — two classes:
    A. PERMANENT skips (no project match, comment not found, removed by moderation, bad URL, post deleted, project has no landing_pages and no website at all): mark so it won't be retried.
       psql "\$DATABASE_URL" -c "UPDATE posts SET link_edited_at=NOW(), link_edit_content='SKIPPED: REASON' WHERE id=POST_ID"
    B. TRANSIENT skips (single_run_capacity_exceeded, batch budget exhausted, you ran out of time, reddit-agent locked by peer, anything that would resolve on retry): DO NOT stamp link_edited_at. Leave both link_edited_at and link_edit_content NULL so the next 6h cron picks it up again.
       Only annotate the reason in a comment / log line; never write to the DB for transient skips.
    If unsure which class a skip falls into, treat it as TRANSIENT (default to retry, not to swallow). Stamping link_edited_at is permanent — once set, the post is excluded from future eligibility queries forever.
PROMPT_EOF

# NOTE: We do NOT acquire reddit-browser at the bash level. Claude itself
# acquires/releases it per post via scripts/reddit_browser_lock.py
# (steps 6.5 and 7.5 in the prompt). This keeps the lock held only during
# the actual ~15-60s reddit-agent browser ops per post, not the full
# 90-min run. Peer pipelines (engage-reddit, dm-replies-reddit,
# post-reddit) can use the profile during our generate_page.py / WebFetch
# / DB phases.
#
# Pre-flight: ensure the profile isn't wedged by a prior crashed run.
# Unified lock (2026-05-10): brief Python acquire+release so the orphan-Chrome
# sweep happens once before claude starts. Python acquire honors expires_at,
# so a TTL-stale-but-PID-alive holder gets reclaimed automatically instead of
# blocking us for the full bash timeout.
log "Pre-flight: sweep orphan reddit-agent Chrome / playwright-mcp before handing off to claude..."
python3 "$REPO_DIR/scripts/reddit_browser_lock.py" acquire --timeout 60 --ttl 30 2>&1 | tee -a "$LOG_FILE" || \
    log "WARNING: reddit_browser_lock.py pre-flight acquire failed; proceeding (claude will retry per-post)."
if ! ensure_reddit_browser_for_backend 2>&1 | tee -a "$LOG_FILE"; then
    log "WARNING: reddit-harness bootstrap failed; falling back to ensure_browser_healthy reddit"
    ensure_browser_healthy "reddit"
fi
python3 "$REPO_DIR/scripts/reddit_browser_lock.py" release 2>/dev/null || true

gtimeout 5400 "$REPO_DIR/scripts/run_claude.sh" "link-edit-reddit" --strict-mcp-config --mcp-config "$MCP_CONFIG_FILE" --disallowed-tools "ScheduleWakeup,CronCreate,CronDelete,CronList,EnterPlanMode,EnterWorktree" --output-format stream-json --verbose -p "$(cat "$PROMPT_FILE")" 2>&1 | tee -a "$LOG_FILE" || log "WARNING: Reddit link-edit claude exited with code $?"
rm -f "$PROMPT_FILE"

# Belt-and-suspenders: if claude exited without releasing the lock (e.g.
# crashed mid-edit before reaching step 7.5), free it now so peer
# pipelines aren't stuck behind a phantom holder. release_lock checks
# the lock_dir and rm-rf's it; safe even if claude already released.
python3 "$REPO_DIR/scripts/reddit_browser_lock.py" release 2>/dev/null || true

EDITED=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM posts WHERE platform='reddit' AND link_edited_at IS NOT NULL;" 2>/dev/null || echo "0")
log "Reddit link-edit complete. Total reddit posts edited (all-time): $EDITED"

RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
_COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START" --scripts "link-edit-reddit" 2>/dev/null || echo "0.0000")
python3 "$REPO_DIR/scripts/log_run.py" --script "link_edit_reddit" --posted 0 --skipped 0 --failed 0 --cost "$_COST" --elapsed "$RUN_ELAPSED"

find "$LOG_DIR" -name "link-edit-reddit-*.log" -mtime +7 -delete 2>/dev/null || true

log "=== Reddit link-edit complete: $(date) ==="
