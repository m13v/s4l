#!/bin/bash
# run-twitter-cycle.sh — Combined Twitter scan + post cycle.
#
# Phase 1 (t=0):
#   - weighted-sample 5 projects from config.json
#   - LLM drafts one search query per project (style from past top queries)
#   - scrape tweets via twitter-agent, enrich via fxtwitter -> T0 snapshot
#   - store all candidates with batch_id and search_topic
#
# Sleep 300s.
#
# Phase 2 (t=5m):
#   - re-fetch the same candidates via fxtwitter -> T1 snapshot + delta_score
#   - SQL gate: only candidates with delta_score >= 1 (skip zero-momentum duds)
#   - Claude reads top 15 by delta, drops unsuitable, posts top N where N is
#     adaptive: 4 if ≥3 candidates cleared Δ≥10 (strong momentum), else 1
#   - keep remaining pending rows: salvaged into the next cycle, hard-expired
#     by Phase 0 once tweet age crosses FRESHNESS_HOURS
#
# Launchd cadence: every 20 minutes. One combined job, one browser lock.

set -uo pipefail

REPO_DIR="$HOME/social-autoposter"
SKILL_FILE="$REPO_DIR/SKILL.md"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"

BATCH_ID="twcycle-$(date +%Y%m%d-%H%M%S)"
LOG_FILE="$LOG_DIR/twitter-cycle-$(date +%Y-%m-%d_%H%M%S).log"
RAW_FILE="/tmp/twitter_cycle_raw_$(date +%s).json"
QUERIES_FILE="/tmp/twitter_cycle_queries_$(date +%s).json"
RUN_START=$(date +%s)
# Tweets older than this are no longer worth replying to. Pending rows older
# than this are hard-expired by Phase 0; younger pending rows are salvaged
# from prior cycles into this batch.
FRESHNESS_HOURS=6

# `set -a` auto-exports every variable assigned by `source .env`, so DATABASE_URL
# and friends propagate to subprocess env (python3 scripts use os.environ at
# import time and would otherwise see empty strings — silently breaking
# update_candidate_posted in twitter_post_plan.py and creating duplicate posts
# under parallel cycles, observed 2026-05-01 batches 02-08).
if [ -f "$REPO_DIR/.env" ]; then
    set -a
    source "$REPO_DIR/.env"
    set +a
fi

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

log "=== Twitter Cycle (batch=$BATCH_ID): $(date) ==="

# --- Preflight (added 2026-05-02) -----------------------------------------
# Three early-exit gates BEFORE we open the DB, set up traps, or touch the
# browser. Each gate emits a `[skipped: <reason>]` stderr line and exits 0
# so launchd treats the slot as cleanly consumed and fires the next one
# on schedule.
#
# 1. Memory pressure: 2026-05-01 a JetsamEvent at 19:26 swallowed two
#    consecutive launchd fires (19:38, 19:53). The wrappers fired, but the
#    grandchild bash never produced output — most likely jetsam-killed or
#    starved during the system's crash-cleanup spike. Skipping when
#    pressure_level >= 2 (warn) avoids piling more Chrome+Claude+Python
#    work onto an already-thrashing system.
#
# 2. Claude quota stamp: prior run_claude.sh invocation hit a fatal cap
#    (monthly cap, daily cap, context-window, credit balance, persistent
#    429). Skip until the stamp expires (default 10 min). When the cap
#    lifts and the next post-expiry fire succeeds, run_claude.sh clears
#    the stamp automatically.
#
# 3. Parallel-cycle cap: max 4 concurrent run-twitter-cycle.sh. Post
#    2026-04-30 the launchd wrapper double-forks and no longer suppresses
#    overlapping fires; without this cap, sustained back-to-back cycles
#    that each take 25-45 min wall-clock can stack 5-6 deep and trigger
#    the same memory pressure that caused the 19:26 jetsam.
#
# preflight.sh exposes a small set of helpers; we call them in order
# (cheapest first) so a fast-path skip (already-blocked) doesn't even
# spend the sysctl read for the next check.
source "$REPO_DIR/scripts/preflight.sh"
SA_PREFLIGHT_SCRIPT="run-twitter-cycle"
preflight_skip_if_claude_blocked
preflight_skip_if_jetsam_pressure
preflight_acquire_slot_or_skip "twitter-cycle" 4

# Source lock helpers (functions only, no lock acquired here). Phase 0 + the
# project/queries setup below run lock-free against DB and config files;
# the twitter-browser lock is acquired later, immediately before the Phase 1
# Claude scan that actually drives the browser (line ~177). Pre-2026-05-01
# this acquire was here at script start and held the lock through Phase 0
# (~3-10s of pure DB/Python work that doesn't touch the browser), starving
# peer cycles' Phase 2b-post under parallel-cycle contention.
source "$REPO_DIR/skill/lock.sh"

# --- Phase tracking: start the twitter_batches row + chain into lock.sh trap -
# Per-cycle phase row (twitter_batches.current_phase + phase_started_at) is
# read by peer cycles' Phase 0 to decide salvage timing per-phase instead of
# the old flat 20-min wall-clock cutoff. Phase 2b-gen (SEO landing-page build)
# legitimately runs 10-40 min and was being salvaged out from under live
# owners under the old rule. See migration 2026-05-01_twitter_batches.sql and
# scripts/twitter_batch_phase.py.
#
# Trap design: lock.sh installs `_sa_release_locks` on EXIT/INT/TERM/HUP. We
# wrap that into `_sa_combined_exit` so a clean exit ALSO deletes our
# twitter_batches row. SIGKILL / OOM / hard crash bypasses traps and
# intentionally leaves the row stale — that's the salvage recovery path.
_sa_cleanup_batch_row() {
    if [ -n "${BATCH_ID:-}" ] && [ -n "${DATABASE_URL:-}" ]; then
        python3 "$REPO_DIR/scripts/twitter_batch_phase.py" end "$BATCH_ID" 2>/dev/null || true
    fi
}
_sa_combined_exit() {
    # Emit run_monitor.log summary FIRST, before any cleanup. Without this,
    # SIGTERM landing between Phase 2b-post (where twitter_post_plan.py has
    # already committed to the `posts` table) and the historical inline
    # summary write at the bottom of the script silently drops the run from
    # run_monitor.log. Mirrors the same fix shipped to run-reddit-search.sh.
    # Idempotent: a flag-guarded one-shot, so the happy-path explicit call at
    # the bottom and the trap firing on EXIT do not double-write.
    _sa_emit_run_summary_oneshot
    _sa_cleanup_batch_row
    # Release the parallel-cycle slot acquired by preflight.sh. Without this,
    # this trap (which OVERWRITES the preflight trap installed at source-time)
    # would leak the slot until the next launchd fire's GC pass — capping
    # effective throughput at 1/cycle even though the slot pool is 4 wide.
    if command -v _preflight_release_slots >/dev/null 2>&1; then
        _preflight_release_slots
    fi
    _sa_release_locks
}

# Idempotent run_monitor.log emitter wired into _sa_combined_exit (which is
# trap'd to EXIT INT TERM HUP). On the happy path the bottom of the script
# calls this directly; on SIGTERM the trap calls it. Either order is a no-op
# after first emission via _SA_RUN_SUMMARY_EMITTED.
#
# Reads counters from globals the cycle has been accumulating (BATCH_ID,
# RUN_START, EXEC_FAILED, EXEC_REASONS, EXEC_SKIPPED, CANDIDATE_COUNT,
# SALVAGED, QUERIES_TOTAL, DUDS_TOTAL, TWEETS_PULLED, BATCH_COUNT,
# HIGH_DELTA_COUNT). Re-derives POSTED_CT/SKIPPED_CT from the
# twitter_candidates table directly so a SIGTERM mid-Phase-2b still gets
# accurate counts (the row was committed inside twitter_post_plan.py before
# the kill). All psql / get_run_cost.py calls are wrapped in `timeout 10`
# so a Neon hang during shutdown can't wedge the trap.
#
# Early-exit failure paths (Phase 1 abort, empty batch, etc.) write their
# own dedicated log_run.py line with custom failure_reasons and then set
# _SA_RUN_SUMMARY_EMITTED=1 to short-circuit this function — they keep
# their tailored error reason, this fallback skips.
_SA_RUN_SUMMARY_EMITTED=0
_sa_emit_run_summary_oneshot() {
    [ "${_SA_RUN_SUMMARY_EMITTED:-0}" = "1" ] && return 0
    _SA_RUN_SUMMARY_EMITTED=1

    local posted_ct=0 skipped_ct=0 cost="0.0000" failed_ct failure_reasons
    if [ -n "${BATCH_ID:-}" ] && [ -n "${DATABASE_URL:-}" ]; then
        posted_ct=$(timeout 10 psql "$DATABASE_URL" -t -A -c \
            "SELECT COUNT(*) FROM twitter_candidates WHERE batch_id='$BATCH_ID' AND status='posted'" \
            2>/dev/null || echo 0)
        skipped_ct=$(timeout 10 psql "$DATABASE_URL" -t -A -c \
            "SELECT COUNT(*) FROM twitter_candidates WHERE batch_id='$BATCH_ID' AND status IN ('skipped','expired')" \
            2>/dev/null || echo 0)
    fi
    cost=$(timeout 10 python3 "$REPO_DIR/scripts/get_run_cost.py" \
                --since "${RUN_START:-0}" \
                --scripts "run-twitter-cycle-scan" "run-twitter-cycle-prep" "run-twitter-cycle-post" \
                2>/dev/null || echo "0.0000")

    failed_ct="${EXEC_FAILED:-0}"
    failure_reasons="${EXEC_REASONS:-}"
    # Reproduce the failure-reason synthesis block so SIGTERM cycles still
    # get a useful reason instead of a silent "—". Same conditions as the
    # historical inline block: cycle ended with zero progress despite having
    # candidates pending.
    if [ "${posted_ct:-0}" = "0" ] \
        && [ "${failed_ct:-0}" = "0" ] \
        && [ "${EXEC_SKIPPED:-0}" = "0" ] \
        && [ -z "$failure_reasons" ] \
        && [ "${CANDIDATE_COUNT:-0}" -gt 0 ]; then
        local phase2b_log
        phase2b_log=$(awk '/Phase 1: drafting queries|Phase 2b-prep: Claude reading|Phase 2b-post:/,EOF' "$LOG_FILE" 2>/dev/null || echo "")
        # Inline reason-add: bash doesn't support `local` on function decls,
        # and a free-standing nested function would leak into the outer
        # scope, so we just expand the assignments at each call site.
        if echo "$phase2b_log" | grep -qiE '"api_error_status":429|hit your limit|monthly usage limit'; then
            failure_reasons="${failure_reasons:+$failure_reasons,}monthly_limit:1"
            failed_ct=$(( failed_ct + 1 ))
        fi
        if echo "$phase2b_log" | grep -qiE 'auth redirect|re-authenticat|browser profile.*auth|profile.*needs.*re-auth'; then
            failure_reasons="${failure_reasons:+$failure_reasons,}auth_redirect:1"
            failed_ct=$(( failed_ct + 1 ))
        fi
        if echo "$phase2b_log" | grep -qiE '"error":"rate_limited"|RATE_LIMITED_TWITTER'; then
            failure_reasons="${failure_reasons:+$failure_reasons,}rate_limited:1"
            failed_ct=$(( failed_ct + 1 ))
        fi
        if echo "$phase2b_log" | grep -qiE 'page.load.timeout|navigation timeout|timed out|Timeout exceeded'; then
            failure_reasons="${failure_reasons:+$failure_reasons,}timeout:1"
            failed_ct=$(( failed_ct + 1 ))
        fi
        if echo "$phase2b_log" | grep -qiE 'reply_box_not_found|tweet_not_found'; then
            failure_reasons="${failure_reasons:+$failure_reasons,}posting_blocked:1"
            failed_ct=$(( failed_ct + 1 ))
        fi
        if [ -z "$failure_reasons" ]; then
            failure_reasons="phase2b_silent:1"
            failed_ct=$(( failed_ct + 1 ))
        fi
    fi

    local args
    args=(--script "post_twitter" \
          --posted "${posted_ct:-0}" \
          --skipped "${skipped_ct:-0}" \
          --failed "$failed_ct" \
          --salvaged "${SALVAGED:-0}" \
          --queries "${QUERIES_TOTAL:-0}" --duds "${DUDS_TOTAL:-0}" \
          --tweets-pulled "${TWEETS_PULLED:-0}" \
          --candidates "${BATCH_COUNT:-0}" --above-floor "${HIGH_DELTA_COUNT:-0}" \
          --cost "$cost" \
          --elapsed $(( $(date +%s) - ${RUN_START:-$(date +%s)} )))
    [ -n "$failure_reasons" ] && args+=(--failure-reasons "$failure_reasons")
    python3 "$REPO_DIR/scripts/log_run.py" "${args[@]}" 2>/dev/null || true
}

trap _sa_combined_exit EXIT INT TERM HUP

python3 "$REPO_DIR/scripts/twitter_batch_phase.py" start "$BATCH_ID" --phase phase0 2>&1 | tee -a "$LOG_FILE" || true

# --- Phase 0: hard-expire stale pending + salvage truly-orphaned rows --------
# Pending rows from prior cycles fall into two buckets:
#   - tweet_posted_at older than FRESHNESS_HOURS  -> hard-expire (lost the
#     replying window, no value in retrying)
#   - still-fresh AND owning batch is dead        -> re-assign to this batch
#     so Phase 2a re-measures T1 and Phase 2b reconsiders them. This is the
#     recovery path for cycles whose Phase 2b died on Anthropic org quota,
#     X rate limit, browser crash, or any other infra failure.
#
# Two safety guards make this safe under parallel cycles (post 2026-04-30
# detach refactor: launchd no longer suppresses overlapping fires, so 2-3
# run-twitter-cycle.sh can be in Phase 0/1/2 simultaneously):
#
#   1. pg_advisory_xact_lock(7472346) serializes Phase 0 transactions, so
#      two cycles can't race on the salvage UPDATE.
#
#   2. PHASE-AWARE BUDGET (post 2026-05-01): salvage timing is per-phase,
#      read from the owner's twitter_batches row:
#        phase0        ->  5 min  (just the salvage SQL)
#        phase1        -> 20 min  (Claude scan + scrape)
#        phase2a       -> 20 min  (5 min sleep + HTTP T1 poll)
#        phase2b-prep  -> 15 min  (Claude reads threads + drafts)
#        phase2b-gen   -> 60 min  (SEO landing-page build, the slow phase)
#        phase2b-post  -> 15 min  (browser reply + log)
#      Pre-2026-05-01 the rule was a flat 20-min wall-clock cutoff against
#      batch_id, which salvaged live cycles whose Phase 2b-gen step (10-40
#      min in normal operation) hadn't finished. Observed 2026-05-01: cycle
#      16:23's candidate 7994 was salvaged into 16:53 while 16:23 was still
#      generating the SEO page; both cycles raced on the post and the
#      late-arriving owner logged failed=1.
#
#   3. LEGACY FALLBACK: rows whose batch has no twitter_batches entry (any
#      cycle that ran before this migration, OR a cycle whose start helper
#      failed) fall back to the original flat 20-min batch_id heuristic.
#      Self-cleans within FRESHNESS_HOURS of migration.
#
# batch_id format is `twcycle-YYYYMMDD-HHMMSS` (assigned at script start
# from `date +%Y%m%d-%H%M%S`, local time). Since the format is fixed-width
# and lexicographically sortable, we compute the cutoff in the shell
# (same TZ as batch_id) and do a string comparison in SQL — sidesteps the
# Postgres session-TZ trap that would otherwise mis-interpret batch_id.
LEGACY_SALVAGE_CUTOFF_MIN=20
LEGACY_SALVAGE_CUTOFF_BATCH_ID="twcycle-$(date -v-${LEGACY_SALVAGE_CUTOFF_MIN}M +%Y%m%d-%H%M%S)"
PHASE0_RESULT=$(psql "$DATABASE_URL" --single-transaction -t -A -c "
SELECT pg_advisory_xact_lock(7472346);
WITH expired AS (
    UPDATE twitter_candidates
    SET status='expired'
    WHERE status='pending' AND tweet_posted_at < NOW() - INTERVAL '$FRESHNESS_HOURS hours'
    RETURNING id
), salvaged AS (
    UPDATE twitter_candidates tc
    SET batch_id='$BATCH_ID'
    WHERE tc.status='pending'
      AND tc.batch_id != '$BATCH_ID'
      AND tc.batch_id LIKE 'twcycle-%'
      AND tc.tweet_posted_at >= NOW() - INTERVAL '$FRESHNESS_HOURS hours'
      -- Skip threads we already posted to. score_twitter_candidates.py applies
      -- this filter on FRESH scrapes; salvage must repeat it because Bug 1
      -- (DATABASE_URL not exported, fixed 2026-05-01) left successful posts'
      -- candidate rows stuck at status='pending', and salvage was re-claiming
      -- already-posted threads, double-firing browser replies (4 observed
      -- duplicates on m13v_ timeline 2026-05-01). Belt-and-suspenders.
      AND tc.tweet_url NOT IN (
          SELECT thread_url FROM posts
          WHERE platform='twitter' AND thread_url IS NOT NULL
      )
      AND (
          -- Phase-aware path: owner has a twitter_batches row, use the
          -- per-phase budget. Owner's phase_started_at is reset by every
          -- twitter_batch_phase.py advance call.
          EXISTS (
              SELECT 1 FROM twitter_batches tb
              WHERE tb.batch_id = tc.batch_id
                AND tb.phase_started_at < NOW() - CASE tb.current_phase
                    WHEN 'phase0'        THEN INTERVAL '5 minutes'
                    WHEN 'phase1'        THEN INTERVAL '20 minutes'
                    WHEN 'phase2a'       THEN INTERVAL '20 minutes'
                    WHEN 'phase2b-prep'  THEN INTERVAL '15 minutes'
                    WHEN 'phase2b-gen'   THEN INTERVAL '60 minutes'
                    WHEN 'phase2b-post'  THEN INTERVAL '15 minutes'
                    ELSE INTERVAL '20 minutes'
                END
          )
          -- Legacy fallback: no batches row, use the old 20-min batch_id
          -- string-cutoff heuristic. Self-cleans within FRESHNESS_HOURS of
          -- migration deploy.
          OR (
              NOT EXISTS (SELECT 1 FROM twitter_batches tb WHERE tb.batch_id = tc.batch_id)
              AND tc.batch_id < '$LEGACY_SALVAGE_CUTOFF_BATCH_ID'
          )
      )
    RETURNING id
)
SELECT (SELECT COUNT(*) FROM expired) || '|' || (SELECT COUNT(*) FROM salvaged);
" 2>/dev/null | tail -1 | tr -d ' ')
EXPIRED_STALE=$(echo "$PHASE0_RESULT" | cut -d'|' -f1)
SALVAGED=$(echo "$PHASE0_RESULT" | cut -d'|' -f2)
[ "${EXPIRED_STALE:-0}" -gt 0 ] && log "Phase 0: hard-expired $EXPIRED_STALE pending rows older than ${FRESHNESS_HOURS}h"
[ "${SALVAGED:-0}" -gt 0 ] && log "Phase 0: salvaged $SALVAGED orphaned pending rows (phase-aware budget) into $BATCH_ID"

# Advance our own batch row from phase0 -> phase1 now that the salvage SQL
# committed. Subsequent phase transitions are stamped right before the work
# they cover begins.
python3 "$REPO_DIR/scripts/twitter_batch_phase.py" advance "$BATCH_ID" --phase phase1 2>&1 | tee -a "$LOG_FILE" || true

# --- Weighted project sample -------------------------------------------------
# Each chosen project is enriched with an `excludes_for_search` array sourced
# from project_search_excludes (only terms past the 2-distinct-batch activation
# gate). The Phase 1 scanner is required to mechanically append these as
# `-term` operators to whatever query it drafts for the project. See
# scripts/project_excludes.py for proposal/activation/decay rules.
PROJECTS_JSON=$(python3 - <<'PY'
import json, os, random, sys
sys.path.insert(0, os.path.expanduser('~/social-autoposter/scripts'))
import project_excludes as pe
c = json.load(open(os.path.expanduser('~/social-autoposter/config.json')))
projects = [p for p in c.get('projects', []) if p.get('weight', 0) > 0]
weights = [(p, p.get('weight', 0)) for p in projects]
k = 8
chosen = []
remaining = list(weights)
for _ in range(min(k, len(remaining))):
    total = sum(w for _, w in remaining)
    r = random.uniform(0, total)
    acc = 0
    for i, (p, w) in enumerate(remaining):
        acc += w
        if acc >= r:
            try:
                excludes = pe.active_excludes('twitter', p.get('name'))
            except Exception:
                excludes = []
            chosen.append({
                'name': p.get('name'),
                'description': p.get('description', ''),
                # Unified search_topics (post 2026-04-30 legacy field cleanup).
                'search_topics': p.get('search_topics') or [],
                # Self-improving exclusion list (2026-05-09): MUST be appended
                # as `-term` to every query drafted for this project.
                'excludes_for_search': excludes,
            })
            remaining.pop(i)
            break
print(json.dumps(chosen, indent=2))
PY
)

log "Selected projects: $(echo "$PROJECTS_JSON" | python3 -c 'import json,sys; print(", ".join(p["name"] for p in json.load(sys.stdin)))')"
EXCLUDES_TOTAL=$(echo "$PROJECTS_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(len(p.get("excludes_for_search") or []) for p in d))')
[ "${EXCLUDES_TOTAL:-0}" -gt 0 ] && log "Active project-wide excludes loaded across selected projects: $EXCLUDES_TOTAL"

# --- Top past queries for style inspiration ---------------------------------
TOP_QUERIES_JSON=$(python3 "$REPO_DIR/scripts/top_twitter_queries.py" --limit 20 2>/dev/null || echo "[]")
TOP_COUNT=$(echo "$TOP_QUERIES_JSON" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
log "Top past queries loaded: $TOP_COUNT"

# --- Dud queries: phrasings that returned 0 tweets in the last 48h ----------
# Fed into the prompt as a negative-signal anti-list so the LLM stops
# redrafting the same flat queries every 20-min cycle. Source is
# twitter_search_attempts, populated below from this run's queries_used.
DUD_QUERIES_JSON=$(python3 "$REPO_DIR/scripts/top_dud_twitter_queries.py" --limit 30 --window-hours 48 2>/dev/null || echo "[]")
DUD_COUNT=$(echo "$DUD_QUERIES_JSON" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
log "Dud queries loaded: $DUD_COUNT (last 48h, 0-result)"

# --- Phase 1: Claude drafts queries, scrapes tweets -------------------------
# JSON schema forces structured output. Eliminates the prose-drift failure mode
# where the scanner summarized instead of dumping the JSON array.
SCAN_SCHEMA='{"type":"object","properties":{"tweets":{"type":"array","items":{"type":"object","properties":{"handle":{"type":"string"},"text":{"type":"string"},"tweetUrl":{"type":"string"},"datetime":{"type":"string"},"replies":{"type":"integer"},"retweets":{"type":"integer"},"likes":{"type":"integer"},"views":{"type":"integer"},"bookmarks":{"type":"integer"},"search_topic":{"type":"string"},"matched_project":{"type":"string"}},"required":["handle","text","tweetUrl","datetime","replies","retweets","likes","views","bookmarks","search_topic","matched_project"]}},"queries_used":{"type":"array","items":{"type":"object","properties":{"query":{"type":"string"},"project":{"type":"string"},"tweets_found":{"type":"integer"}},"required":["query","project","tweets_found"]}}},"required":["tweets","queries_used"]}'

log "Acquiring twitter-browser lock for Phase 1 Claude scan..."
acquire_lock "twitter-browser" 3600
# Drop stale Chrome singleton symlinks before launch. Background ungraceful-
# exits (SIGKILL, jetsam, force quit) leave Singleton{Lock,Cookie,Socket}
# pointing at dead PIDs / vanished sockets; without this, Chrome pops "Something
# went wrong when opening your profile" 7x and the pipeline hangs. Helper
# refuses to clean if the lock PID is alive.
bash "$REPO_DIR/scripts/clean_stale_singleton.sh" "$HOME/.claude/browser-profiles/twitter" 2>&1 | tee -a "$LOG_FILE" || true
ensure_browser_healthy "twitter"

log "Phase 1: drafting queries and scraping tweets..."

SCAN_OUTPUT=$("$REPO_DIR/scripts/run_claude.sh" "run-twitter-cycle-scan" --strict-mcp-config --mcp-config "$HOME/.claude/browser-agent-configs/twitter-agent-mcp.json" -p --output-format json --json-schema "$SCAN_SCHEMA" "You are a Twitter hot-tweet scanner. Your ONLY job is to find high-engagement tweets happening RIGHT NOW that are relevant to one of our projects. Do NOT post anything.

## Step 1: Draft one search query per project

You have $(echo "$PROJECTS_JSON" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))') projects. Draft exactly ONE Twitter search query for each, tailored to that project's topic space.

Projects:
$PROJECTS_JSON

Past top-performing query STYLES (use these only as inspiration for phrasing, operators, and specificity level; do NOT copy keywords blindly, adapt them to each project):
$TOP_QUERIES_JSON

DUD QUERIES — DO NOT REUSE these phrasings or close variants. They returned ZERO tweets in the last 48h, so redrafting them wastes the budget. \`attempts\` is how many cycles already wasted on each one; \`last_ran_h_ago\` is hours since the most recent attempt. Pick a different angle, different operators, or different keyword cluster:
$DUD_QUERIES_JSON

Query guidelines:
- MANDATORY: every query MUST include the operator \`since:$(date -u -v-1d +%Y-%m-%d)\` so X returns only tweets from the last ~24h. Evergreen tweets waste budget — we want momentum, not history.
- MANDATORY: if a project's \`excludes_for_search\` array is non-empty, append \`-term\` for EACH listed term to that project's query, verbatim. This is mechanical, not optional. These terms were learned from prior false-positive rejections and are the ONLY upstream block against recurring noise (e.g. for Vipassana, terms like \`-cricket -kohli -ipl\` block IPL/Sanjiv-Goenka tweets that collide with the meditation teacher S.N. Goenka). DO NOT second-guess or omit them; the activation gate (>=2 distinct batches) already filtered out one-off proposals.
- Favor high engagement: include 'min_faves:50' for broad terms, 'min_faves:20' for narrower ones
- Favor discussions/opinions (people sharing experience, asking questions), not news/promos/giveaways
- Pick a query likely to surface tweets RELEVANT to that project's actual domain
- Mix it up each run, don't always use the same query for the same project
- Use the projects' search_topics/description as grounding (search_topics is a shared concept seed list across platforms — some phrases are tuned for Reddit or GitHub, so rephrase into natural Twitter search terms with hashtag-adjacent vernacular)

## Step 2: Search and extract

For EACH project's query you drafted:
1. Navigate to: https://x.com/search?q={your_query} -filter:replies&f=live
   Use mcp__twitter-agent__browser_navigate
2. Wait 4 seconds, then run this JavaScript via mcp__twitter-agent__browser_run_code to extract tweets:

async (page) => {
  await page.waitForTimeout(3000);
  const tweets = await page.evaluate(() => {
    const results = [];
    for (const article of [...document.querySelectorAll('article[data-testid=\"tweet\"]')].slice(0, 8)) {
      try {
        let handle = '';
        for (const link of article.querySelectorAll('a[role=\"link\"]')) {
          const href = link.getAttribute('href');
          if (href && href.startsWith('/') && !href.includes('/status/') && !href.includes('/search') && href.length > 1 && href.split('/').length === 2) {
            handle = href.replace('/', ''); break;
          }
        }
        const tweetText = article.querySelector('[data-testid=\"tweetText\"]');
        const text = tweetText ? tweetText.textContent : '';
        const timeEl = article.querySelector('time');
        const timeParent = timeEl ? timeEl.closest('a') : null;
        const tweetUrl = timeParent ? 'https://x.com' + timeParent.getAttribute('href') : '';
        const datetime = timeEl ? timeEl.getAttribute('datetime') : '';
        let replies=0, retweets=0, likes=0, views=0, bookmarks=0;
        for (const btn of article.querySelectorAll('[role=\"group\"] button')) {
          const al = btn.getAttribute('aria-label') || '';
          let m;
          if (m=al.match(/([\d,]+)\s*repl/i)) replies=parseInt(m[1].replace(/,/g,''));
          if (m=al.match(/([\d,]+)\s*repost/i)) retweets=parseInt(m[1].replace(/,/g,''));
          if (m=al.match(/([\d,]+)\s*like/i)) likes=parseInt(m[1].replace(/,/g,''));
          if (m=al.match(/([\d,]+)\s*view/i)) views=parseInt(m[1].replace(/,/g,''));
          if (m=al.match(/([\d,]+)\s*bookmark/i)) bookmarks=parseInt(m[1].replace(/,/g,''));
        }
        results.push({handle, text, tweetUrl, datetime, replies, retweets, likes, views, bookmarks});
      } catch(e) {}
    }
    return results;
  });
  return JSON.stringify(tweets);
}

3. After scanning all projects, return EVERY extracted tweet via the structured 'tweets' field. Each tweet object MUST include 'search_topic' (the query that found it) and 'matched_project' (the project name whose query found it).

4. ALSO return the structured 'queries_used' array with ONE entry per project (length must equal the number of projects), each with:
   - 'query': the exact final query string you searched on x.com (without the leading 'q=' or url-encoding)
   - 'project': the project name
   - 'tweets_found': integer count of tweets you extracted for that query (0 if X showed 'No results' or the page was empty)
   This list is logged to twitter_search_attempts so future cycles can avoid redrafting dead phrasings. Emit it even when tweets_found is 0 — the zero rows are the whole point of this list.

CRITICAL RULES:
- Use ONLY mcp__twitter-agent__* tools for scraping
- Do NOT post, reply, like, or interact with any tweet
- Do NOT generate any reply content
- If a search fails or times out, skip it and continue to the next (still emit a queries_used entry with tweets_found:0 for that project)" 2>&1)

# Dump the captured envelope to the cycle log for offline inspection.
echo "$SCAN_OUTPUT" >> "$LOG_FILE"

# Parse the structured-output envelope and write the tweets array to $RAW_FILE.
# claude -p --output-format json wraps results as {"structured_output": {...}, ...}.
# Also extract queries_used (the LLM's drafted query list with per-query
# tweets_found counts) to $QUERIES_FILE so we can log every attempt to
# twitter_search_attempts — including the ZERO-result ones, which are the
# whole point of this telemetry. We MUST write $QUERIES_FILE even on the
# no-tweets exit path; otherwise duds never get logged and the negative
# anti-list stays empty.
python3 -c "
import json, sys
text = sys.stdin.read().strip()
# raw_decode reads the first complete JSON object and stops, so the trailing
# run_claude.sh cost-log JSON line on stdout/stderr does not cause 'Extra data'.
try:
    env, _ = json.JSONDecoder().raw_decode(text)
except Exception as e:
    print(f'No tweet data found in output (envelope parse error: {e})', file=sys.stderr); sys.exit(1)
so = env.get('structured_output')
if so is None:
    so = env.get('result')
if isinstance(so, str):
    try: so = json.loads(so)
    except Exception: pass

queries_used = so.get('queries_used', []) if isinstance(so, dict) else []
# Always write \$QUERIES_FILE even when empty so the shell's existence check
# is unambiguous; logger no-ops on empty list.
json.dump(queries_used, open('$QUERIES_FILE', 'w'))
print(f'Extracted {len(queries_used)} queries_used entries to $QUERIES_FILE', file=sys.stderr)

tweets = so.get('tweets', []) if isinstance(so, dict) else []
if not tweets:
    print('No tweets in structured_output.tweets', file=sys.stderr); sys.exit(1)
json.dump(tweets, open('$RAW_FILE', 'w'))
print(f'Extracted {len(tweets)} tweets to $RAW_FILE', file=sys.stderr)
" <<< "$SCAN_OUTPUT" 2>&1 | tee -a "$LOG_FILE"

EXTRACT_EXIT=${PIPESTATUS[0]:-1}

# --- Discovery-stage counters ------------------------------------------------
# Capture queries-run / duds / raw-tweets-pulled BEFORE any early-exit branch
# so every log_run.py call below can pass --queries/--duds/--tweets-pulled.
# QUERIES_FILE is the array Claude returned (one row per drafted query incl.
# zero-result ones); RAW_FILE is the deduped tweet array. Use python3 inline so
# we get the exact in-memory counts the rest of the pipeline operates on.
QUERIES_TOTAL=0
DUDS_TOTAL=0
TWEETS_PULLED=0
if [ -f "$QUERIES_FILE" ]; then
    QUERIES_TOTAL=$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(len(d) if isinstance(d, list) else 0)
except Exception:
    print(0)
" "$QUERIES_FILE" 2>/dev/null || echo 0)
    DUDS_TOTAL=$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    n = sum(1 for q in (d if isinstance(d, list) else []) if (q.get('tweets_found') or 0) == 0)
    print(n)
except Exception:
    print(0)
" "$QUERIES_FILE" 2>/dev/null || echo 0)
fi
if [ -f "$RAW_FILE" ]; then
    TWEETS_PULLED=$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(len(d) if isinstance(d, list) else 0)
except Exception:
    print(0)
" "$RAW_FILE" 2>/dev/null || echo 0)
fi

# Log every drafted query (incl. zero-result ones) to twitter_search_attempts
# BEFORE any early-exit branches. Runs even when the tweets array is empty
# so dud queries actually accumulate in the negative-signal table.
if [ -f "$QUERIES_FILE" ]; then
    python3 "$REPO_DIR/scripts/log_twitter_search_attempts.py" --batch-id "$BATCH_ID" \
        < "$QUERIES_FILE" 2>&1 | tee -a "$LOG_FILE"
    rm -f "$QUERIES_FILE"
fi

# Stamp last_used_at on every active project-wide exclude we surfaced to
# Claude this cycle. These are the terms Claude was REQUIRED to append as
# `-term` to its drafted queries, so even if Claude omits one, the term is
# still considered "in use" for decay purposes — drafter compliance is its
# own problem, not a reason to prune a learned exclude. Done after the
# search_attempts log so a Phase 1 abort still leaves the marks behind.
python3 - "$PROJECTS_JSON" <<'PY' 2>&1 | tee -a "$LOG_FILE" || true
import json, os, sys
sys.path.insert(0, os.path.expanduser('~/social-autoposter/scripts'))
import project_excludes as pe
projects = json.loads(sys.argv[1] or '[]')
total = 0
for p in projects:
    terms = p.get('excludes_for_search') or []
    if not terms:
        continue
    try:
        n = pe.mark_used('twitter', p.get('name'), terms)
    except Exception as exc:
        print(f"mark_used error for {p.get('name')}: {exc}", file=sys.stderr)
        continue
    total += n
if total:
    print(f"project_excludes: marked {total} term(s) used across selected projects")
PY
if [ "$EXTRACT_EXIT" -ne 0 ] || [ ! -f "$RAW_FILE" ]; then
    log "No tweets extracted in Phase 1. Aborting cycle."
    _COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START" --scripts "run-twitter-cycle-scan" "run-twitter-cycle-post" 2>/dev/null || echo "0.0000")
    # Detect Anthropic usage-limit hits in the scan envelope so the dashboard
    # surfaces "failed: monthly_limit" instead of a silent failed=1 row. The
    # 429 marker comes from the JSON envelope ("api_error_status":429), the
    # plain-text fallback covers Anthropic's ratelimit prose ("You've hit your
    # limit"). Reason key is consistent with engage_reddit.py for unified
    # rendering.
    PHASE1_REASON="phase1_no_tweets"
    if echo "$SCAN_OUTPUT" | grep -qiE '"api_error_status":429|"hit your limit"|usage limit'; then
        PHASE1_REASON="monthly_limit"
    fi
    python3 "$REPO_DIR/scripts/log_run.py" --script "post_twitter" --posted 0 --skipped 0 --failed 1 \
        --salvaged "${SALVAGED:-0}" \
        --queries "${QUERIES_TOTAL:-0}" --duds "${DUDS_TOTAL:-0}" \
        --tweets-pulled "${TWEETS_PULLED:-0}" \
        --failure-reasons "${PHASE1_REASON}:1" \
        --cost "$_COST" --elapsed $(( $(date +%s) - RUN_START ))
    # Tell the EXIT trap's _sa_emit_run_summary_oneshot to skip; this branch
    # already wrote its tailored failure-reasons line above.
    _SA_RUN_SUMMARY_EMITTED=1
    exit 0
fi

# --- Phase 1 finalize: enrich + score with T0 + batch_id --------------------
log "Enriching via fxtwitter + scoring with T0 snapshot (batch=$BATCH_ID)..."
cat "$RAW_FILE" \
    | python3 "$REPO_DIR/scripts/enrich_twitter_candidates.py" \
    | python3 "$REPO_DIR/scripts/score_twitter_candidates.py" --batch-id "$BATCH_ID" \
    2>&1 | tee -a "$LOG_FILE"
rm -f "$RAW_FILE"

BATCH_COUNT=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM twitter_candidates WHERE batch_id='$BATCH_ID'" 2>/dev/null || echo 0)
log "Phase 1 complete. Batch has $BATCH_COUNT candidates with T0 snapshot."

if [ "$BATCH_COUNT" = "0" ]; then
    log "Empty batch. Nothing to re-score. Exiting."
    _COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START" --scripts "run-twitter-cycle-scan" "run-twitter-cycle-post" 2>/dev/null || echo "0.0000")
    # Surface as failed=1 with reason so the dashboard doesn't render this as a
    # silent "—". Distinct reason from phase1_no_tweets so the operator can tell
    # "Claude returned tweets but enrichment dropped them all" from "Claude
    # returned no tweets at all".
    python3 "$REPO_DIR/scripts/log_run.py" --script "post_twitter" --posted 0 --skipped 0 --failed 1 \
        --salvaged "${SALVAGED:-0}" \
        --queries "${QUERIES_TOTAL:-0}" --duds "${DUDS_TOTAL:-0}" \
        --tweets-pulled "${TWEETS_PULLED:-0}" \
        --failure-reasons "empty_batch:1" \
        --cost "$_COST" --elapsed $(( $(date +%s) - RUN_START ))
    _SA_RUN_SUMMARY_EMITTED=1
    exit 0
fi

# Stamp phase2a before releasing the lock so the budget covers the entire
# 5-min wait + HTTP poll window (phase2a budget = 20 min).
python3 "$REPO_DIR/scripts/twitter_batch_phase.py" advance "$BATCH_ID" --phase phase2a 2>&1 | tee -a "$LOG_FILE" || true

# Release the twitter-browser lock during the 5-min T1 wait + HTTP-only Phase 2a.
# Other pipelines (engage-twitter, dm-outreach-twitter, link-edit-twitter,
# stats.sh) can run their browser steps in this window instead of waiting for us
# to finish. We re-acquire just before Phase 2b posts, blocking up to the
# acquire_lock timeout if another pipeline is mid-run.
log "Releasing twitter-browser lock for the T1 wait window (5min sleep + HTTP fxtwitter poll)..."
release_lock "twitter-browser"
# Defense-in-depth: clear the hook-layer lockfile so the next cycle's
# PreToolUse never sees a stale entry from us. run_claude.sh's exit trap
# already does this; explicit repeat covers SIGKILL of the wrapper.
rm -f "$HOME/.claude/twitter-agent-lock.json"

# --- Sleep 5 min before T1 measurement --------------------------------------
log "Sleeping 300s before T1 re-measurement..."
sleep 300

# --- Phase 2a: re-fetch T1 engagement ---------------------------------------
log "Phase 2a: re-polling fxtwitter for T1 engagement..."
python3 "$REPO_DIR/scripts/fetch_twitter_t1.py" --batch-id "$BATCH_ID" 2>&1 | tee -a "$LOG_FILE"

# --- Phase 2b: top 15 by delta (Δ≥1 floor), adaptive post cap 1 or 4 --------
CANDIDATES=$(psql "$DATABASE_URL" -t -A -F '|' -c "
    SELECT id, tweet_url, author_handle,
           REPLACE(REPLACE(COALESCE(tweet_text, ''), E'\n', ' '), E'\r', ' '),
           virality_score,
           COALESCE(delta_score, 0), matched_project, search_topic,
           likes_t1, retweets_t1, replies_t1, views_t1, author_followers,
           EXTRACT(EPOCH FROM (NOW() - tweet_posted_at))/3600,
           REPLACE(REPLACE(COALESCE(draft_reply_text, ''), E'\n', ' '), E'\r', ' '),
           COALESCE(draft_engagement_style, ''),
           CASE WHEN drafted_at IS NULL THEN -1
                ELSE EXTRACT(EPOCH FROM (NOW() - drafted_at))/60
           END
    FROM twitter_candidates
    WHERE batch_id='$BATCH_ID' AND status='pending' AND delta_score >= 1
    ORDER BY delta_score DESC
    LIMIT 15;
" 2>/dev/null || echo "")

if [ -z "$CANDIDATES" ]; then
    log "No candidates with delta scores. Marking batch expired."
    psql "$DATABASE_URL" -c "UPDATE twitter_candidates SET status='expired' WHERE batch_id='$BATCH_ID' AND status='pending'" 2>&1 | tee -a "$LOG_FILE"
    _COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START" --scripts "run-twitter-cycle-scan" "run-twitter-cycle-post" 2>/dev/null || echo "0.0000")
    EXPIRED_BATCH=$(psql "$DATABASE_URL" -t -A -c "SELECT COUNT(*) FROM twitter_candidates WHERE batch_id='$BATCH_ID' AND status='expired'" 2>/dev/null || echo 0)
    # Not a hard error — batch had candidates but none cleared the Δ≥1 floor.
    # Report as skipped (not failed) so the row reads "skipped: N" rather than
    # the silent "—" we used to render. failure_reasons stays empty.
    python3 "$REPO_DIR/scripts/log_run.py" --script "post_twitter" --posted 0 --skipped "${EXPIRED_BATCH:-0}" --failed 0 \
        --salvaged "${SALVAGED:-0}" \
        --queries "${QUERIES_TOTAL:-0}" --duds "${DUDS_TOTAL:-0}" \
        --tweets-pulled "${TWEETS_PULLED:-0}" --candidates "${BATCH_COUNT:-0}" \
        --cost "$_COST" --elapsed $(( $(date +%s) - RUN_START ))
    _SA_RUN_SUMMARY_EMITTED=1
    exit 0
fi

CANDIDATE_COUNT=$(printf '%s\n' "$CANDIDATES" | grep -c '^[0-9]')
log "Top $CANDIDATE_COUNT candidates by delta selected for post review."

# Adaptive post cap: if ≥3 candidates cleared Δ≥10 (strong momentum), allow up to 4
# posts; otherwise cap at 1 so we don't burn reply budget on marginal cycles.
HIGH_DELTA_COUNT=$(printf '%s\n' "$CANDIDATES" | awk -F'|' '$1 ~ /^[0-9]+$/ && $6+0 >= 10 {n++} END {print n+0}')
if [ "$HIGH_DELTA_COUNT" -ge 3 ]; then
    POST_LIMIT=4
else
    POST_LIMIT=1
fi
log "Adaptive post cap: $HIGH_DELTA_COUNT candidates with Δ≥10 → POST_LIMIT=$POST_LIMIT"

CANDIDATE_BLOCK=""
while IFS='|' read -r cid curl cauthor ctext cscore cdelta cproject ctopic clikes crts creplies cviews cfollowers cage cdraft cdraftstyle cdraftage; do
    DRAFT_LINE=""
    if [ -n "$cdraft" ] && [ "$cdraftage" != "-1" ]; then
        # Round draft age to whole minutes for the prompt.
        DRAFT_MIN=$(printf '%.0f' "$cdraftage")
        DRAFT_LINE="
EXISTING DRAFT (style=$cdraftstyle, age=${DRAFT_MIN}m): $cdraft"
    fi
    CANDIDATE_BLOCK="${CANDIDATE_BLOCK}
---
Candidate ID: $cid
URL: $curl
Author: @$cauthor (${cfollowers} followers)
Text: $ctext
Score: $cscore | Delta (5min): $cdelta | Likes: $clikes | RTs: $crts | Replies: $creplies | Views: $cviews | Age: ${cage}h
Search query: $ctopic
Project match: $cproject${DRAFT_LINE}
"
done <<< "$CANDIDATES"

ALL_PROJECTS_JSON=$(python3 -c "
import json, os
config = json.load(open(os.path.expanduser('~/social-autoposter/config.json')))
print(json.dumps({p['name']: p for p in config.get('projects', [])}, indent=2))
" 2>/dev/null || echo "{}")

TOP_REPORT=$(python3 "$REPO_DIR/scripts/top_performers.py" --platform twitter 2>/dev/null || echo "(top performers report unavailable)")

source "$REPO_DIR/skill/styles.sh"
STYLES_BLOCK=$(generate_styles_block twitter posting)

# Phase 2b is split into three sub-phases so the twitter-browser lock is only
# held during actual browser work. The killer in the old single-session flow
# was generate_page.py running inside the Claude session: 10-40 minutes of
# Cloud Run deploy chain time, all under the browser lock, blocking every
# other twitter pipeline. The new flow:
#   2b-prep (lock held): Claude reads threads, drafts replies, saves drafts,
#                        emits a JSON plan listing chosen candidates.
#   <release lock>
#   2b-gen  (no lock):    twitter_gen_links.py runs generate_page.py per
#                        candidate; falls back to plain project URL on failure.
#   <re-acquire lock>
#   2b-post (lock held): twitter_post_plan.py calls twitter_browser.py reply,
#                        log_post.py, campaign_bump.py, marks link_edited_at.

PLAN_FILE="/tmp/twitter_cycle_plan_${BATCH_ID}.json"
SKIP_FILE="/tmp/twitter_cycle_skips_${BATCH_ID}.json"

# --- Phase 2b-prep: pick + draft + plan -------------------------------------
log "Re-acquiring twitter-browser lock for Phase 2b-prep (read+draft only)..."
acquire_lock "twitter-browser" 3600
# Drop stale singleton locks (see clean_stale_singleton.sh, also called in Phase 1).
bash "$REPO_DIR/scripts/clean_stale_singleton.sh" "$HOME/.claude/browser-profiles/twitter" 2>&1 | tee -a "$LOG_FILE" || true
ensure_browser_healthy "twitter"

log "Phase 2b-prep: Claude reading threads and drafting up to $POST_LIMIT replies..."

# Pre-assign the prep session UUID in the parent shell so it survives the
# command-substitution subshell run_claude.sh runs in. We write it into the
# plan JSON below so Phase 2b-post can re-export it for log_post.py, which
# stamps posts.claude_session_id and lets the dashboard activity feed join
# to claude_sessions for cost. Without this, twitter posts get NULL session
# ids and blank cost cells.
CLAUDE_SESSION_ID="$(uuidgen | tr 'A-Z' 'a-z')"
export CLAUDE_SESSION_ID

PREP_SCHEMA='{"type":"object","properties":{"candidates":{"type":"array","items":{"type":"object","properties":{"candidate_id":{"type":"integer"},"candidate_url":{"type":"string"},"thread_author":{"type":"string"},"thread_text":{"type":"string"},"matched_project":{"type":"string"},"reply_text":{"type":"string"},"engagement_style":{"type":"string"},"language":{"type":"string"},"has_landing_pages":{"type":"boolean"},"link_keyword":{"type":"string"},"link_slug":{"type":"string"}},"required":["candidate_id","candidate_url","matched_project","reply_text","engagement_style","language","has_landing_pages"]}},"rejected":{"type":"array","items":{"type":"object","properties":{"candidate_id":{"type":"integer"},"reason":{"type":"string"},"proposed_excludes":{"type":"array","items":{"type":"string"}}},"required":["candidate_id","reason"]}}},"required":["candidates","rejected"]}'

PREP_OUTPUT=$("$REPO_DIR/scripts/run_claude.sh" "run-twitter-cycle-prep" --strict-mcp-config --mcp-config "$HOME/.claude/browser-agent-configs/twitter-agent-mcp.json" -p --output-format json --json-schema "$PREP_SCHEMA" "You are the Social Autoposter prep step.

Your ONLY job in THIS session:
  1. Read each thread you decide to reply to (browser, mcp__twitter-agent__* read-only).
  2. Draft a reply for each.
  3. Persist each fresh draft via log_draft.py.
  4. Emit a structured plan describing the chosen candidates, the reply text, and (when applicable) the SEO link keyword + slug.

You will NOT post anything. You will NOT generate landing pages. You will NOT call log_post.py. The shell handles all of that AFTER your session ends, with the browser lock released for the long landing-page build.

Read $SKILL_FILE for content rules and voice context.
Read $REPO_DIR/config.json for project metadata.

## PRE-SCORED CANDIDATES (top by 5-min engagement velocity, best first)
$CANDIDATE_BLOCK

## PROJECT ROUTING (per-candidate)
Each candidate has a 'Project match' field. Use that project unless the thread content clearly better fits another project.
All project configs: $ALL_PROJECTS_JSON

## FEEDBACK FROM PAST PERFORMANCE:
$TOP_REPORT

$STYLES_BLOCK

## WORKFLOW
Pick AT MOST $POST_LIMIT candidate(s) this cycle. Skip any candidate whose thread is off-topic, toxic, or low-quality. If fewer than $POST_LIMIT candidates are truly on-brand, return fewer; never force entries.

For each chosen candidate:
1. Navigate to CANDIDATE_URL via mcp__twitter-agent__browser_navigate (READ-ONLY).
2. Read the thread to understand context.
3. DRAFT HANDLING (existing vs fresh):
   - If the candidate block shows an EXISTING DRAFT line AND draft age < 30 minutes, REUSE the draft text verbatim. Set engagement_style to the existing style. Do NOT call log_draft.py; do NOT redraft. Reason: prior cycle paid the LLM cost.
   - Otherwise: draft a reply using the best engagement style. 1-2 sentences. NEVER em dashes. Apply the matched project's \`voice\` block from ALL_PROJECTS_JSON: follow voice.tone, never violate voice.never, mirror voice.examples / voice.examples_good when present.
3a. PERSIST FRESH DRAFTS (skip for reused drafts):
     python3 $REPO_DIR/scripts/log_draft.py --candidate-id CANDIDATE_ID --text 'YOUR_REPLY_TEXT' --style STYLE
   Failure here is non-fatal, log a warning and continue.
4. EMIT one entry in the structured 'candidates' array with these fields:
   - candidate_id (int): from the candidate block
   - candidate_url (string): the parent tweet URL
   - thread_author (string): the @handle (no leading @)
   - thread_text (string): the parent tweet's text, condensed to <=500 chars if needed
   - matched_project (string): the project name to attribute this post to
   - reply_text (string): the FINAL reply text WITHOUT any URL appended (the shell appends the URL later). Keep <=250 chars so a 23-char t.co link fits inside the 280-char Twitter cap.
   - engagement_style (string): style name applied (or 'reused' for an unchanged stale draft)
   - language (string): ISO 639-1 code (en, ja, zh, es, ...)
   - has_landing_pages (bool): true iff the matched project has BOTH landing_pages.repo AND landing_pages.base_url set in config.json. Otherwise false.
   - link_keyword (string, REQUIRED when has_landing_pages=true; OMIT otherwise): a SHORT 3-6 word phrase that captures the ESSENCE OF YOUR REPLY (not just the thread topic). Think: what would a reader search to find a useful page about what you just said?
   - link_slug (string, REQUIRED when has_landing_pages=true; OMIT otherwise): kebab-case, alphanumeric+hyphens only, max 50 chars.

5. ACCOUNT FOR EVERY PRE-SCORED CANDIDATE: every Candidate ID listed in the PRE-SCORED CANDIDATES section above MUST appear in EXACTLY ONE of the two output arrays this cycle:
   - 'candidates' (chosen, capped at $POST_LIMIT) per step 4 above, OR
   - 'rejected' with a SHORT one-line reason explaining why this thread is not worth replying to (off-topic for the matched project, toxic / hateful, low-quality / spam, audience mismatch, near-duplicate of something we already replied to, etc.). Reason must be <=200 chars, plain text, no quotes.
   It is fine for 'candidates' to be empty if no thread is on-brand; in that case every candidate id goes into 'rejected'. The reverse (every id in 'candidates') is also allowed up to POST_LIMIT, with the rest in 'rejected'.
   Do NOT update twitter_candidates yourself; the shell will mark every entry of 'rejected' as status='skipped' with the reason, and Phase 0 will salvage anything you forgot.

5a. SELF-IMPROVING PROJECT-WIDE EXCLUSION LIST (optional, on rejected entries only):
    When you put a candidate in 'rejected' BECAUSE of a stable, recurring CLASS of false-positive (not a one-off bad tweet), you MAY include a 'proposed_excludes' array of 1-3 specific keywords. If you do, the pipeline will (after a 2-distinct-batch activation gate) automatically append \`-keyword\` to ALL future Twitter searches for the matched_project, project-wide and persistent. This is the ONLY upstream block against the entire class of false-positive that a tighter Phase 1 query alone cannot prevent.

    USE THIS POWER NARROWLY. False-negatives (legit tweets being filtered out) are far worse than the cost of seeing one more cricket tweet. Apply ALL of these rules:

    - DO emit when: the false-positive is caused by a SPECIFIC ambiguous proper noun, brand, or domain term that has a wholly unrelated meaning collisional with the project. Example for Vipassana: an IPL/cricket thread surfaced because the search query included \`Goenka\` (the meditation teacher S.N. Goenka shares a surname with Sanjiv Goenka, owner of an IPL team). Right proposed_excludes: ['cricket','kohli','ipl','lsg','rcb']. WRONG proposed_excludes: ['goenka'] (would mute legit S.N. Goenka tweets).

    - DO NOT emit when: the candidate is just personally low-quality (spam, low engagement, generic), the language is wrong, the author is bot-like, or the thread is just slightly off-topic. Those are one-offs, NOT classes. Use the 'reason' field instead.

    - Each proposed term must be:
      * a SINGLE token, lowercase, ascii letters/digits/hyphen only, no spaces, length 3-32. (e.g. 'cricket', 'kohli', 'ipl', 'lsg', 'rcb-fan', 'crypto', 'memecoin').
      * SPECIFIC and unambiguous in the project's domain. Proper nouns, brand names, narrow jargon, sport/team/franchise terms preferred. Generic words like 'practice', 'retreat', 'meditation', 'work', 'tips', 'app', 'tool', 'help' are FORBIDDEN — they will produce false-negatives.
      * NOT a core search topic of the matched_project (the validator rejects any term in the project's search_topics, so don't waste tokens proposing one).

    - Cap: at most 3 terms per rejected entry. If you need more, you're probably proposing too generically — narrow the list.

    - Activation gate: each term needs >=2 SEPARATE batches to propose it before it goes live, so a single false-rejection cannot mute a search. You don't need to think about this — propose if you'd be confident a future cycle's Claude would also propose it; if not, leave proposed_excludes off.

    - When in doubt, omit the field entirely. The default behavior (no proposed_excludes) is safe; over-proposing is not.

CRITICAL:
- DO NOT post anything. The shell handles posting.
- DO NOT call twitter_browser.py.
- DO NOT call generate_page.py (the shell runs it AFTER your session, outside the lock).
- DO NOT call log_post.py or campaign_bump.py.
- mcp__twitter-agent__* tools are READ-ONLY in this step.
- NEVER use em dashes. Use commas, periods, or regular dashes (-).
- Reply in the SAME LANGUAGE as the parent tweet." 2>&1)

echo "$PREP_OUTPUT" >> "$LOG_FILE"

# Parse the prep envelope and write the plan to \$PLAN_FILE; also extract the
# 'rejected' array into \$SKIP_FILE so log_twitter_skips.py can persist a
# reason against every twitter_candidates row Claude reviewed but didn't pick.
python3 -c "
import json, sys
text = sys.stdin.read().strip()
try:
    env, _ = json.JSONDecoder().raw_decode(text)
except Exception as e:
    print(f'prep: envelope parse error: {e}', file=sys.stderr); sys.exit(1)
so = env.get('structured_output')
if so is None:
    so = env.get('result')
if isinstance(so, str):
    try: so = json.loads(so)
    except Exception: pass
candidates = so.get('candidates', []) if isinstance(so, dict) else []
rejected   = so.get('rejected',   []) if isinstance(so, dict) else []
json.dump({'candidates': candidates, 'session_id': '$CLAUDE_SESSION_ID'}, open('$PLAN_FILE', 'w'), indent=2)
json.dump({'skips': rejected}, open('$SKIP_FILE', 'w'), indent=2)
print(f'prep: wrote {len(candidates)} candidates and {len(rejected)} skips to $PLAN_FILE / $SKIP_FILE', file=sys.stderr)
" <<< "$PREP_OUTPUT" 2>&1 | tee -a "$LOG_FILE"

PREP_PARSE_EXIT=${PIPESTATUS[0]:-1}

# Persist the rejected list to twitter_candidates (status='skipped' with reason)
# scoped to this batch so we never clobber rows from peer cycles. Non-fatal.
if [ -f "$SKIP_FILE" ]; then
    python3 "$REPO_DIR/scripts/log_twitter_skips.py" \
        --file "$SKIP_FILE" --require-batch-id "$BATCH_ID" 2>&1 | tee -a "$LOG_FILE" || true
    rm -f "$SKIP_FILE"
fi

# Detect Anthropic monthly cap so the dashboard surfaces a reason rather than
# a silent failure when prep returns no plan.
PREP_REASON="prep_failed"
if echo "$PREP_OUTPUT" | grep -qiE '"api_error_status":429|"hit your limit"|monthly usage limit'; then
    PREP_REASON="monthly_limit"
fi

PLAN_COUNT=0
if [ "$PREP_PARSE_EXIT" -eq 0 ] && [ -f "$PLAN_FILE" ]; then
    PLAN_COUNT=$(python3 -c "import json; print(len(json.load(open('$PLAN_FILE')).get('candidates') or []))" 2>/dev/null || echo 0)
fi
log "Phase 2b-prep complete. plan_count=$PLAN_COUNT"

# Always release the lock now: gen step is lock-free, and even on the empty
# path we don't want to hold the browser lock through the early-exit cleanup.
log "Releasing twitter-browser lock (gen step is lock-free)..."
release_lock "twitter-browser"
# Defense-in-depth: clear the hook-layer lockfile; see Phase 1 note.
rm -f "$HOME/.claude/twitter-agent-lock.json"

if [ "${PLAN_COUNT:-0}" = "0" ]; then
    log "Empty plan from prep step. Exiting cycle without posting (pending rows salvaged next cycle)."
    rm -f "$PLAN_FILE"
    _COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START" --scripts "run-twitter-cycle-scan" "run-twitter-cycle-prep" "run-twitter-cycle-post" 2>/dev/null || echo "0.0000")
    if [ "$PREP_REASON" = "monthly_limit" ]; then
        python3 "$REPO_DIR/scripts/log_run.py" --script "post_twitter" --posted 0 --skipped "${CANDIDATE_COUNT:-0}" --failed 1 --salvaged "${SALVAGED:-0}" \
            --queries "${QUERIES_TOTAL:-0}" --duds "${DUDS_TOTAL:-0}" \
            --tweets-pulled "${TWEETS_PULLED:-0}" --candidates "${BATCH_COUNT:-0}" --above-floor "${HIGH_DELTA_COUNT:-0}" \
            --failure-reasons "monthly_limit:1" --cost "$_COST" --elapsed $(( $(date +%s) - RUN_START ))
    else
        python3 "$REPO_DIR/scripts/log_run.py" --script "post_twitter" --posted 0 --skipped "${CANDIDATE_COUNT:-0}" --failed 0 --salvaged "${SALVAGED:-0}" \
            --queries "${QUERIES_TOTAL:-0}" --duds "${DUDS_TOTAL:-0}" \
            --tweets-pulled "${TWEETS_PULLED:-0}" --candidates "${BATCH_COUNT:-0}" --above-floor "${HIGH_DELTA_COUNT:-0}" \
            --cost "$_COST" --elapsed $(( $(date +%s) - RUN_START ))
    fi
    _SA_RUN_SUMMARY_EMITTED=1
    exit 0
fi

# --- Phase 2b-gen: SEO landing pages (no browser lock) ----------------------
log "Phase 2b-gen: generating SEO pages for $PLAN_COUNT candidate(s) without holding the browser lock..."
python3 "$REPO_DIR/scripts/twitter_gen_links.py" --plan "$PLAN_FILE" 2>&1 | tee -a "$LOG_FILE"
GEN_EXIT=${PIPESTATUS[0]:-1}
if [ "$GEN_EXIT" -ne 0 ]; then
    log "WARN: twitter_gen_links.py exited $GEN_EXIT, continuing with whatever links it set (per-candidate fallback to plain project URL on gen failure)."
fi

# --- Phase 2b-post: re-acquire browser lock and post ------------------------
log "Re-acquiring twitter-browser lock for Phase 2b-post..."
acquire_lock "twitter-browser" 3600
# Drop stale singleton locks (see clean_stale_singleton.sh, also called in Phase 1 / 2b-prep).
bash "$REPO_DIR/scripts/clean_stale_singleton.sh" "$HOME/.claude/browser-profiles/twitter" 2>&1 | tee -a "$LOG_FILE" || true
ensure_browser_healthy "twitter"

log "Phase 2b-post: posting $PLAN_COUNT candidate(s)..."
POST_OUTPUT=$(python3 "$REPO_DIR/scripts/twitter_post_plan.py" --plan "$PLAN_FILE" 2>&1)
echo "$POST_OUTPUT" >> "$LOG_FILE"

# The post helper prints a JSON summary on its last stdout line.
POST_SUMMARY=$(printf '%s\n' "$POST_OUTPUT" | tail -n 1)
EXEC_POSTED=$(python3 -c "import json,sys; d=json.loads(sys.argv[1] or '{}'); print(d.get('posted', 0))" "$POST_SUMMARY" 2>/dev/null || echo 0)
EXEC_SKIPPED=$(python3 -c "import json,sys; d=json.loads(sys.argv[1] or '{}'); print(d.get('skipped', 0))" "$POST_SUMMARY" 2>/dev/null || echo 0)
EXEC_FAILED=$(python3 -c "import json,sys; d=json.loads(sys.argv[1] or '{}'); print(d.get('failed', 0))" "$POST_SUMMARY" 2>/dev/null || echo 0)
EXEC_REASONS=$(python3 -c "import json,sys; d=json.loads(sys.argv[1] or '{}'); print(d.get('failure_reasons', ''))" "$POST_SUMMARY" 2>/dev/null || echo "")
log "Phase 2b-post summary: posted=$EXEC_POSTED skipped=$EXEC_SKIPPED failed=$EXEC_FAILED reasons=$EXEC_REASONS"

rm -f "$PLAN_FILE"

# --- No end-of-cycle expire ------------------------------------------------
# Pending rows are intentionally left alone. They are either:
#   - candidates Phase 2b never reached (e.g., org quota, browser crash, or
#     simply ran out of POST_LIMIT before reviewing the long tail), and the
#     next cycle's Phase 0 will salvage them while still fresh
#   - hard-expired by the next cycle's Phase 0 once they cross FRESHNESS_HOURS
# This avoids losing work to transient infra failures.

# --- Summary ---------------------------------------------------------------
# Per-run-log human readout. The persistent run_monitor.log row is written
# by _sa_emit_run_summary_oneshot (defined near the top of this script) so
# SIGTERM during the summary block still produces a dashboard-visible row.
SUMMARY=$(psql "$DATABASE_URL" -t -A -F '|' -c "
SELECT status, COUNT(*) FROM twitter_candidates WHERE batch_id='$BATCH_ID' GROUP BY status
" 2>/dev/null)
log "Batch summary: $SUMMARY"

_sa_emit_run_summary_oneshot

log "=== Cycle complete: $(date) ==="
find "$LOG_DIR" -name "twitter-cycle-*.log" -mtime +7 -delete 2>/dev/null || true
