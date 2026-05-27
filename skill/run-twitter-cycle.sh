#!/bin/bash
# run-twitter-cycle.sh — Combined Twitter scan + post cycle.
#
# Phase 1 (t=0):
#   - select 8 projects via the shared inverse-recent-share picker
#     (scripts/pick_project.py, same logic as github/reddit)
#   - LLM drafts one search query per project (style from past top queries)
#   - scrape tweets via twitter-harness, enrich via fxtwitter -> T0 snapshot
#   - store all candidates with batch_id and search_topic
#
# Sleep 300s.
#
# Phase 2 (t=5m):
#   - re-fetch the same candidates via fxtwitter -> T1 snapshot + delta_score
#   - SQL gate: floor lowered to delta_score >= 0 so zero-momentum but on-theme
#     product-discussion tweets (asking for a tool, venting a pain point) can
#     compete; ranking still favors growth via hybrid sort
#   - Hybrid sort: ORDER BY (delta_score + product_intent_boost) where
#     product_intent_boost is +5 when tweet text matches an intent-signal regex
#     (wish/need/looking for/recommend/alternative/frustrated/etc); raw growth
#     remains the dominant signal, but a slow-burn "anyone know a tool for..."
#     tweet now ranks alongside fast-growing news/drama
#   - Claude reads top 25 (raised from 15 so the long tail reaches the model),
#     drops unsuitable, posts every candidate it judges genuinely on-brand
#     (no per-cycle post cap, no per-project quota)
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
# Exported so twitter_post_plan.py (Phase 2b-post child process) can re-stamp
# the executing cycle's batch_id onto candidates at post time. Without this
# export, peer cycles' Phase 0 salvage can rewrite our candidates' batch_id
# mid-flight (documented edge case 2026-05-15); the re-stamp at post time is
# the structural fix so attribution always lands on the cycle that fired the
# browser, regardless of salvage timing.
export BATCH_ID
# Export the same id as SA_CYCLE_ID so every Claude session spawned by this
# cycle (via run_claude.sh -> log_claude_session.py) stamps its claude_sessions
# row with cycle_id=$BATCH_ID. Enables exact per-cycle cost accounting via
# get_run_cost.py --cycle-id, instead of the legacy script+since query which
# bleeds costs across concurrent stacked cycles. See 2026-05-10 cycle_id
# rollout (started on reddit, extended here).
export SA_CYCLE_ID="$BATCH_ID"
LOG_FILE="$LOG_DIR/twitter-cycle-$(date +%Y-%m-%d_%H%M%S).log"
RAW_FILE="/tmp/twitter_cycle_raw_$(date +%s).json"
QUERIES_FILE="/tmp/twitter_cycle_queries_$(date +%s).json"
# log_twitter_search_attempts.py writes [{query, project, attempt_id}, ...]
# here so score_twitter_candidates.py can stamp the exact discovering
# attempt_id onto each twitter_candidates row (2026-05-21).
ATTEMPTS_FILE="/tmp/twitter_cycle_attempts_$(date +%s).json"
RUN_START=$(date +%s)

# ----------------------------------------------------------------------------
# Browser: CDP-driven real Google Chrome on port 9555 via the twitter-harness
# MCP. Profile lives at ~/.claude/browser-profiles/browser-harness.
# TW_MCP_CONFIG / TW_ENGINE_PREFIX are placeholders, the real values get set
# below when lib/twitter-backend.sh is sourced (overwriting both).
# ----------------------------------------------------------------------------
TW_MCP_CONFIG=""
TW_ENGINE_PREFIX=""
# Tweets older than this are no longer worth replying to. Pending rows older
# than this are hard-expired by Phase 0; younger pending rows are salvaged
# from prior cycles into this batch.
FRESHNESS_HOURS=6

# ----------------------------------------------------------------------------
# A/B/C/D variant assignment. Deterministic per BATCH_ID so that every step
# of one cycle (search, scoring, drafting, posting, salvage) sees the same
# variant. Variants:
#   A (control, 2026-05-22)      = current logic: 20-min ripen wait + 6h
#                                  freshness window.
#   B (no-ripen, 2026-05-22)     = skip the sleep 1200 + fetch_twitter_t1
#                                  step (saves ~20 min thread->post latency).
#                                  6h freshness unchanged.
#   C (no-ripen+1h, 2026-05-22)  = skip ripening AND tighten the Phase 1
#                                  freshness window to 1h (since_time epoch
#                                  passed to scan model + hook).
#   D (C + 2k view cap, 2026-05-25) = same as C, plus score_twitter_candidates
#                                  drops any tweet with views > 2000 at
#                                  discovery T0. Tests the hypothesis that
#                                  high-view threads starve our reply of
#                                  audience: bucket data shows view-share
#                                  collapses from 4% on <2k threads to 0.1%
#                                  on >10k threads. Filter is enforced in
#                                  scripts/score_twitter_candidates.py by
#                                  reading TWITTER_CYCLE_VARIANT from env.
# Phase 0 hard-expire continues to use FRESHNESS_HOURS=6 (the union ceiling)
# so peer cycles don't accidentally expire each other's still-pending rows.
# Only FRESHNESS_HOURS_DISCOVER (Phase 1 prompt + since-rewrite hook) varies.
TWITTER_CYCLE_VARIANT=$(python3 -c "import hashlib, sys; h=int(hashlib.sha1(sys.argv[1].encode()).hexdigest(),16)%4; print('ABCD'[h])" "$BATCH_ID" 2>/dev/null || echo "A")
FRESHNESS_HOURS_DISCOVER=$FRESHNESS_HOURS
if [ "$TWITTER_CYCLE_VARIANT" = "C" ] || [ "$TWITTER_CYCLE_VARIANT" = "D" ]; then
    FRESHNESS_HOURS_DISCOVER=1
fi
export TWITTER_CYCLE_VARIANT FRESHNESS_HOURS_DISCOVER
# Hook env: ~/.claude/hooks/twitter-search-since-rewrite.py reads this and
# uses it in place of its hardcoded 6h default when present.
export FRESHNESS_HOURS_OVERRIDE=$FRESHNESS_HOURS_DISCOVER

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
log "Variant=$TWITTER_CYCLE_VARIANT (A=ripen+6h, B=no-ripen+6h, C=no-ripen+1h, D=no-ripen+1h+2k_view_cap); discover_freshness=${FRESHNESS_HOURS_DISCOVER}h"

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

# Harness-only browser bootstrap (twitter-agent path fully removed 2026-05-19).
# Sets MCP_CONFIG_FILE, BROWSER_INSTRUCTIONS, exports TWITTER_CDP_URL=9555.
# Phase 2b-post's twitter_post_plan.py shells out to twitter_browser.py, which
# honors TWITTER_CDP_URL exported by this lib.
source "$REPO_DIR/skill/lib/twitter-backend.sh"
TW_MCP_CONFIG="$MCP_CONFIG_FILE"
TW_ENGINE_PREFIX="${BROWSER_INSTRUCTIONS}"$'\n\n' # inject backend + translation table at top of every prompt

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
# so a Postgres hang during shutdown can't wedge the trap.
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
    # Prefer the in-memory counters captured from twitter_post_plan.py's JSON
    # summary (EXEC_POSTED / EXEC_SKIPPED). Those are the ground truth for what
    # THIS cycle did. The fallback SQL count is needed when SIGTERM hits before
    # Phase 2b-post records a count, but it's UNRELIABLE during normal exit:
    # peer cycles' Phase 0 may have salvaged this batch's candidates into a new
    # batch_id mid-Phase-2b (documented edge case, mitigated by the phase2b-*
    # advance stamps but not 100% eliminated under heavy parallel load), in
    # which case the WHERE batch_id='$BATCH_ID' query returns 0 even though we
    # successfully posted N replies. That false-zero is what historically
    # synthesized phase2b_silent failure_reasons against successful runs.
    if [ -n "${EXEC_POSTED:-}" ] || [ -n "${EXEC_SKIPPED:-}" ]; then
        posted_ct="${EXEC_POSTED:-0}"
        skipped_ct="${EXEC_SKIPPED:-0}"
    elif [ -n "${BATCH_ID:-}" ]; then
        # /api/v1/twitter-candidates/counts-by-batch returns posted +
        # skipped_or_expired in one roundtrip; helper prints them space-
        # separated so this stays a single $() capture.
        _SC=$(timeout 10 python3 "$REPO_DIR/scripts/twitter_cycle_helper.py" \
                status-counts --batch-id "$BATCH_ID" 2>/dev/null || echo "0 0")
        posted_ct=$(echo "$_SC" | awk '{print $1}')
        skipped_ct=$(echo "$_SC" | awk '{print $2}')
        : "${posted_ct:=0}"
        : "${skipped_ct:=0}"
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
        # Run the shared API-error classifier first — catches monthly_limit,
        # stream_idle_timeout, api_overloaded, context_overflow, credit_balance,
        # etc. uniformly so the dashboard pill reads with the actual error
        # class instead of falling through to the generic phase2b_silent.
        local classifier_reason
        classifier_reason=$(echo "$phase2b_log" | python3 "$REPO_DIR/scripts/classify_run_error.py" 2>/dev/null)
        if [ -n "$classifier_reason" ]; then
            failure_reasons="${failure_reasons:+$failure_reasons,}${classifier_reason}:1"
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
    [ -n "${EXEC_SKIP_REASONS:-}" ] && args+=(--skip-reasons "$EXEC_SKIP_REASONS")
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
#        phase2b-prep  -> 45 min  (Claude reads threads + drafts; bumped 2026-05-15
#                                  15 -> 30 after 17:15 cycle was wrongly salvaged
#                                  while queued behind 17:30's 42-min lock-hold;
#                                  bumped 2026-05-22 30 -> 45 to leave more
#                                  headroom for big-batch Claude reads after the
#                                  Variant A wake re-stamp fix)
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
LEGACY_SALVAGE_CUTOFF_BATCH_ID="twcycle-$(python3 -c "import datetime; print((datetime.datetime.now() - datetime.timedelta(minutes=${LEGACY_SALVAGE_CUTOFF_MIN})).strftime('%Y%m%d-%H%M%S'))")"
# Single-transaction Phase 0 salvage now lives server-side at
# /api/v1/twitter-candidates/phase0-salvage. Same advisory lock (7472346),
# same expire + salvage CTE, same phase-aware budget table. The helper
# prints "<expired_count>|<salvaged_count>" so the legacy cut/cut shape
# downstream still works.
PHASE0_RESULT=$(python3 "$REPO_DIR/scripts/twitter_cycle_helper.py" \
    phase0-salvage \
    --batch-id "$BATCH_ID" \
    --freshness-hours "$FRESHNESS_HOURS" \
    --legacy-cutoff "$LEGACY_SALVAGE_CUTOFF_BATCH_ID" \
    2>/dev/null | tail -1 | tr -d ' ')
EXPIRED_STALE=$(echo "$PHASE0_RESULT" | cut -d'|' -f1)
SALVAGED=$(echo "$PHASE0_RESULT" | cut -d'|' -f2)
[ "${EXPIRED_STALE:-0}" -gt 0 ] && log "Phase 0: hard-expired $EXPIRED_STALE pending rows older than ${FRESHNESS_HOURS}h"
[ "${SALVAGED:-0}" -gt 0 ] && log "Phase 0: salvaged $SALVAGED orphaned pending rows (phase-aware budget) into $BATCH_ID"

# Advance our own batch row from phase0 -> phase1 now that the salvage SQL
# committed. Subsequent phase transitions are stamped right before the work
# they cover begins.
python3 "$REPO_DIR/scripts/twitter_batch_phase.py" advance "$BATCH_ID" --phase phase1 2>&1 | tee -a "$LOG_FILE" || true

# --- Shared project selection (inverse-recent-share) -------------------------
# Project selection is shared across twitter/github/reddit via
# scripts/pick_project.py (pick_projects): inverse-recent-share weighting,
# weight / (1 + posts in the last 7d), sampled without replacement. This
# replaced the old inline weighted sample on 2026-05-15 so all platforms
# pick projects the same way.
# Each chosen project is then enriched here with an `excludes_for_search`
# array sourced from project_search_excludes (only terms past the
# 2-distinct-batch activation gate). The Phase 1 scanner is required to
# mechanically append these as `-term` operators to whatever query it drafts
# for the project. See scripts/project_excludes.py for proposal/activation/
# decay rules.
PROJECTS_JSON=$(python3 - <<'PY'
import json, os, subprocess, sys
REPO = os.path.expanduser('~/social-autoposter')
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import project_excludes as pe

res = subprocess.run(
    ['python3', os.path.join(REPO, 'scripts', 'pick_project.py'),
     '--platform', 'twitter', '--count', '1', '--json'],
    capture_output=True, text=True, timeout=30,
)
picked = []
if res.returncode == 0 and res.stdout.strip():
    try:
        picked = json.loads(res.stdout)
    except Exception:
        picked = []

# pick_project.py returns a single dict when --count=1, a list when --count>1.
# Normalize to a list so the rest of the heredoc works either way.
if isinstance(picked, dict):
    picked = [picked]

try:
    from pick_search_topic import pick_topic_for_project
except Exception:
    pick_topic_for_project = None

chosen = []
for p in picked:
    try:
        excludes = pe.active_excludes('twitter', p.get('name'))
    except Exception:
        excludes = []
    # 2026-05-26: force-pick ONE search_topic per project via the Python
    # picker so end-to-end attribution (topic -> query -> candidate ->
    # post -> click) is clean. Mirrors the engagement_styles flow.
    # Two branches:
    #   - use (~90%): picker returns search_topic=<string>, weighted-random
    #     over the FULL universe with log-smoothed weights (top ~20-30%,
    #     cold ~0.5-1%). Claude must use the assigned topic verbatim.
    #   - explore_invent (~10%): picker returns search_topic=None, Claude
    #     INVENTS a new topic for this cycle given the per-project pool
    #     stats (reference_topics). The invention is captured in
    #     twitter_candidates.search_topic for analytics; promotion into
    #     config.json is a manual human-review step.
    # See scripts/pick_search_topic.py.
    topic_pick = None
    if pick_topic_for_project is not None:
        try:
            topic_pick = pick_topic_for_project(p.get('name'), platform='twitter')
        except Exception:
            topic_pick = None
    if topic_pick is not None:
        picked_topic = topic_pick.get('search_topic')  # may be None on explore_invent
        picked_mode = topic_pick.get('mode', 'use')
        reference_topics = topic_pick.get('reference_topics') or []
        picked_weight_pct = topic_pick.get('picked_weight_pct')
    else:
        # Picker unavailable or project has no search_topics[] in
        # config.json. Fall back to the first legacy entry so the
        # pipeline degrades gracefully instead of erroring.
        legacy = p.get('search_topics') or []
        picked_topic = legacy[0] if legacy else ''
        picked_mode = 'fallback'
        reference_topics = []
        picked_weight_pct = None
    chosen.append({
        'name': p.get('name'),
        'description': p.get('description', ''),
        # Force-picked single topic (2026-05-26). Replaces the legacy
        # `search_topics: [...]` array. Claude draws its query from THIS
        # topic and must echo it verbatim on every tweet object via the
        # bh_run scrape script's `search_topic` Python variable.
        # NULL when topic_picked_mode == 'explore_invent' — Claude must
        # invent a new topic given the reference_topics stats below.
        'search_topic': picked_topic,
        'topic_picked_mode': picked_mode,
        'picked_weight_pct': picked_weight_pct,
        # Per-project pool stats (top by composite_score). Surfaced so
        # Claude can see what's working / saturated when inventing on
        # the explore_invent branch, and as context in use mode.
        'reference_topics': reference_topics,
        # Self-improving exclusion list (2026-05-09): MUST be appended
        # as `-term` to every query drafted for this project.
        'excludes_for_search': excludes,
    })
print(json.dumps(chosen, indent=2))
PY
)

log "Selected projects: $(echo "$PROJECTS_JSON" | python3 -c 'import json,sys; print(", ".join(p["name"] for p in json.load(sys.stdin)))')"
EXCLUDES_TOTAL=$(echo "$PROJECTS_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(len(p.get("excludes_for_search") or []) for p in d))')
[ "${EXCLUDES_TOTAL:-0}" -gt 0 ] && log "Active project-wide excludes loaded across selected projects: $EXCLUDES_TOTAL"

# --- Top past queries for style inspiration (PER-PROJECT, 2026-05-26) -------
# Now scored by composite (clicks×100 + likes + views×0.001) AND filtered by
# project so the model sees ITS OWN historical winners, not a global pool.
# Each query carries the full conversion funnel: tweets_found_avg, posted_n,
# skipped_n, post_rate, views, likes, clicks. Clicks are the ultimate signal;
# composite weights them ×100 so one click outvalues 100 likes of vibes.
#
# Why per-project (vs the previous global TOP_QUERIES_JSON): the global list
# let a thin niche (paperback-expert) cross-mimic a stronger project's
# min_faves tier from the gold list, even when paperback-expert had ZERO
# historical rows of its own. Per-project routing isolates the signal so
# each project's prompt sees only queries it ran itself.
#
# Cold-start projects (zero historical rows): no cross-project fallback. They
# get an empty project_queries array and rely on PER-PROJECT SUPPLY SIGNAL
# (for min_faves) + their config.json description (for keyword phrasing). A
# cross-project "structural inspiration" fallback contradicts the whole point
# of the per-project routing; explicitly removed 2026-05-26.
TOP_QUERIES_PER_PROJECT_JSON=$(echo "$PROJECTS_JSON" | python3 -c "
import json, sys, subprocess
projects = json.load(sys.stdin)
repo_dir = '$REPO_DIR'

def run_q(args):
    try:
        r = subprocess.run(['python3', f'{repo_dir}/scripts/top_twitter_queries.py'] + args,
                           capture_output=True, text=True, timeout=30)
        return json.loads((r.stdout or '[]').strip() or '[]') if r.returncode == 0 else []
    except Exception:
        return []

out = {}
for p in projects:
    name = (p.get('name') or '').strip()
    if not name:
        continue
    rows = run_q(['--limit', '20', '--window-days', '14', '--project', name])
    out[name] = {'project_queries': rows}
print(json.dumps(out))
" 2>/dev/null || echo "{}")
TOP_QUERIES_SUMMARY=$(echo "$TOP_QUERIES_PER_PROJECT_JSON" | python3 -c '
import json, sys
d = json.load(sys.stdin)
parts = []
cold = 0
for name, entry in d.items():
    n = len(entry.get("project_queries") or [])
    parts.append(f"{name}={n}")
    if n == 0:
        cold += 1
print(", ".join(parts) + f" (cold_start_projects={cold})")
')
log "Per-project top queries loaded: $TOP_QUERIES_SUMMARY"

# --- Top performing search topics (topic-universe evolution, 2026-05-25) ----
# Sibling signal to TOP_QUERIES_JSON, one level up the funnel: where queries
# are the literal X search strings, search_topics are the conceptual seeds
# they were drafted from (e.g. "MCP client desktop", "AI agent that takes
# actions"). top_search_topics.py reads twitter_candidates (sidesteps
# posts.search_topic which was 0% covered for Twitter until this cycle) and
# returns, per topic: posted vs skipped count, avg virality posted vs
# skipped, total clicks/likes/views, composite_score. The model uses this to
# evolve the TOPIC UNIVERSE itself (drop topics with high skipped/posted
# ratio, mimic topics with non-zero clicks, invent variants of winning
# topics) rather than just rephrasing within the same fixed set of topics.
TOP_TOPICS_JSON=$(python3 "$REPO_DIR/scripts/top_search_topics.py" --platform twitter --limit 20 --window-days 14 --json 2>/dev/null || echo "[]")
TOP_TOPICS_COUNT=$(echo "$TOP_TOPICS_JSON" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
log "Top search topics loaded: $TOP_TOPICS_COUNT (Twitter, 14d window)"

# --- Dud queries: phrasings that returned 0 tweets in the last 48h ----------
# Fed into the prompt as a negative-signal anti-list so the LLM stops
# redrafting the same flat queries every 20-min cycle. Source is
# twitter_search_attempts, populated below from this run's queries_used.
# Now also surfaces the parsed `min_faves` value per dud so the model can
# spot patterns like "every studyly dud last 48h used min_faves:20 — drop
# the floor for that project".
DUD_QUERIES_JSON=$(python3 "$REPO_DIR/scripts/top_dud_twitter_queries.py" --limit 30 --window-hours 48 2>/dev/null || echo "[]")
DUD_COUNT=$(echo "$DUD_QUERIES_JSON" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
log "Dud queries loaded: $DUD_COUNT (last 48h, 0-result, with min_faves parsed)"

# --- Dud topics: CONCEPT SEEDS that find viral but off-fit candidates -------
# One level up from dud queries. DUD_QUERIES_JSON says "this exact phrasing
# returns 0 tweets, do not reuse it"; DUD_TOPICS_JSON says "this CONCEPT
# SEED finds viral tweets but Phase 2b keeps skipping them — the seed is
# mismatched to your buyers; reword the queries narrower or drop the seed".
# Surfaces sample_skip_reasons so the model can see WHY (audience mismatch,
# competitor launch, spam-flagged author, etc.) rather than just numeric
# skip counts. 7d window so we accumulate enough skips for action thresholds
# without dragging in stale topics.
DUD_TOPICS_JSON=$(python3 "$REPO_DIR/scripts/top_dud_twitter_topics.py" --limit 12 --window-hours 168 --min-skips 5 2>/dev/null || echo "[]")
DUD_TOPICS_COUNT=$(echo "$DUD_TOPICS_JSON" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
log "Dud topics loaded: $DUD_TOPICS_COUNT (7d window, min_skips=5)"

# --- Per-project supply signal: what min_faves tier returns tweets? ----------
# Replaces the old flat "broad=50 / narrow=20" rule. For each project the
# model is currently drafting for, this table shows the median tweets_found
# at each min_faves tier we've ever tried, plus zero-result %. The model
# is instructed to pick the LOWEST min_faves tier that historically yields
# >=3 median tweets for that project (or step down one tier if every tier
# is >=3 — supply signal trumps the flat rule). For studyly this auto-
# selects min_faves:15; for mk0r it stays at 30-50.
SUPPLY_SIGNAL_JSON=$(python3 "$REPO_DIR/scripts/twitter_supply_signal.py" --window-days 14 2>/dev/null || echo "[]")
SUPPLY_COUNT=$(echo "$SUPPLY_SIGNAL_JSON" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
log "Per-project supply signal loaded: $SUPPLY_COUNT projects"

# --- Recently-engaged tweet IDs: scanner skips tweets we already replied to -
# The scanner re-searches stable hot topics every cycle, so the same fresh
# tweets resurface. Once we've replied to one it's a dead candidate
# (score_twitter_candidates.py dedups it downstream). Injecting the last 48h
# of engaged status IDs into the scan prompt lets the model skip them while
# scraping instead of spending tokens evaluating tweets it can't post to.
# 48h is ample: the 6h freshness wall means any dup is necessarily a recent
# reply. Scoring remains the backstop; this is purely a token cleanup.
ENGAGED_TWEET_IDS=$(python3 "$REPO_DIR/scripts/twitter_cycle_helper.py" engaged-tweet-ids --window-hours 48 2>/dev/null || echo "[]")
[ -z "$ENGAGED_TWEET_IDS" ] && ENGAGED_TWEET_IDS="[]"
ENGAGED_COUNT=$(echo "$ENGAGED_TWEET_IDS" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
log "Recently-engaged tweet IDs loaded: $ENGAGED_COUNT (last 48h; scanner will skip them)"

# --- Hook-notice block (harness is the only backend now) --------------------
# The PreToolUse hook ~/.claude/hooks/twitter-search-since-rewrite.py matches
# `mcp__twitter-harness__bh_run` and enforces a strict 6-hour freshness window.
# Single-quoted assignment because the body contains literal backticks the
# model needs to see verbatim.
HOOK_NOTICE='HOOK NOTICE — date operators in your search queries are auto-rewritten by a PreToolUse hook on every `mcp__twitter-harness__bh_run` call to enforce a strict 6-hour freshness window. The hook does three things BEFORE your script executes: (a) every `since:YYYY-MM-DD` you write is rewritten to `since_time:<6h-ago-epoch>`, (b) every stale `since_time:<old-epoch>` is re-stamped to <6h-ago-epoch>, and (c) any search-query string containing `-filter:replies` but NO date operator gets `since_time:<6h-ago-epoch>` injected. This is INTENTIONAL — downstream scoring drops every tweet older than 6h, so older results are wasted budget. The rewrite is OUR pipeline, NOT X misbehavior. Do NOT try to work around it (closer dates, dropping the operator, URL-encoding to bypass, retrying with a different draft, or interpreting the smaller result set as a bug) — your queries WILL be filtered to the last 6 hours regardless. Accept the smaller fresh result set; if a query honestly returns 0, drop the min_faves floor per the zero-result rule below and re-run that one query.'

# --- Anti-debug rule --------------------------------------------------------
# Phase 1 is a STRAIGHT-LINE scrape, not an exploration. The 17:15 cycle on
# 2026-05-15 ran 42 minutes with num_turns=37 because Claude saw a 0-result
# query, assumed X was misbehaving, and spent 30 minutes reverse-engineering
# our own since: hook + X's scroll virtualization. That's all wasted budget:
# the hook is documented (see HOOK_NOTICE), scroll behavior doesn't matter
# (the scrape only reads the initial viewport's 8 tweets), and a 0-result
# query honestly means there is no fresh supply. Encode "stop debugging,
# finish the job" as an explicit rule the model can quote back to itself.
ANTI_DEBUG_RULE='ANTI-DEBUG RULE — Phase 1 is a STRAIGHT-LINE scrape with a HARD 15-minute total budget. You have AT MOST 2 bh_run calls per project (1 for the search, 1 only if the first crashed with a Python error — NOT for retries on empty results). You MUST NOT: investigate X search-operator behavior even if results look surprising; investigate why your `since:` operators got rewritten (see HOOK NOTICE above); try alternate URL forms (top vs latest, with/without -filter:replies, &src= variants, etc.); scroll the page, click anything, or take screenshots to "verify" state; "improve" or rewrite the bh_run script body; re-try a query that returned 0 tweets. If your bh_run returns an empty list, that means no fresh supply exists in the 6h window — that is the CORRECT, EXPECTED outcome for thin queries. Log `tweets_found:0` in queries_used and move on to the next project. Finishing all projects in 8 minutes with several 0-result entries is the SUCCESS state. Spending 30 minutes "debugging" one project is a FAILURE — empty results are the diagnostic, not a bug to fix.'

# --- Step 2 block (harness, hardcoded bh_run script) ------------------------
# Give Claude the LITERAL script to run, not a Playwright template + a
# translation table. The mental-translation step costs turns and invites
# detours: every cycle the model would have to re-derive the bh_run idiom
# (new_tab vs goto_url, js() triple-quote, tab hygiene) from the translation
# table. Hardcoding the script collapses that to zero turns of decision-making.
#
# `read -r -d ''` rather than `$(cat <<EOF)` because macOS bash 3.2 has a
# parsing bug: inside `$(...)` it tries to balance parens/quotes in the
# heredoc body, even with a single-quoted delimiter. The JS arrow-function
# syntax `(() => {...})()` and apostrophes in prose body would trigger
# spurious "unexpected EOF" errors. `read -d ''` reads the heredoc directly
# into the variable with no command substitution, sidestepping the bug.
# `|| true` because read returns 1 when it hits EOF without the delimiter.
IFS='' read -r -d '' STEP2_INSTRUCTIONS <<'HARNESS_STEP2_EOF' || true
## Step 2: Search and extract — RUN THIS EXACT SCRIPT, NO IMPROVEMENTS

For EACH project query you drafted, make ONE call to `mcp__twitter-harness__bh_run` with the Python body below. Substitute ONLY the values of `query`, `search_topic`, and `matched_project`; leave every other byte identical. Use `new_tab(url)` on the very FIRST bh_run call of the cycle, and `goto_url(url)` for every subsequent call (reuse the tab — opening a new tab per query leaks tabs and exhausts Chrome).

CRITICAL — `search_topic` is the assigned topic from the project's JSON entry under the `search_topic` field, NOT the query string. The picker has already chosen one canonical topic per project; you MUST paste that exact string verbatim into the `search_topic` Python variable. End-to-end attribution (topic → query → candidate → click) joins on this string match, so any drift breaks the analytics. Do NOT set `search_topic = query`, do NOT paraphrase the topic, do NOT lowercase or strip operators from it.

```python
import json, urllib.parse, time
query = "YOUR DRAFTED QUERY HERE WITH OPERATORS"
search_topic = "PASTE THE PROJECT'S ASSIGNED search_topic FIELD VERBATIM"  # NOT the query string
matched_project = "PROJECT_NAME"                     # e.g. studyly, Podlog, S4L
url = "https://x.com/search?q=" + urllib.parse.quote(query) + "&f=live"
goto_url(url)  # FIRST call of the cycle only: replace with new_tab(url)
wait_for_load()
time.sleep(4)
tweets = js("""
(() => {
  const SNOWFLAKE = /\/status\/(\d{15,19})(?:[\/?#]|$)/;
  const FAKE_TAIL = /0{6,}$/;
  const results = [];
  for (const article of [...document.querySelectorAll('article[data-testid="tweet"]')].slice(0, 8)) {
    try {
      let handle = '';
      for (const link of article.querySelectorAll('a[role="link"]')) {
        const href = link.getAttribute('href');
        if (href && href.startsWith('/') && !href.includes('/status/') && !href.includes('/search') && href.length > 1 && href.split('/').length === 2) {
          handle = href.replace('/', ''); break;
        }
      }
      const tweetText = article.querySelector('[data-testid="tweetText"]');
      const text = tweetText ? tweetText.textContent : '';
      const timeEl = article.querySelector('time');
      const timeParent = timeEl ? timeEl.closest('a') : null;
      const tweetUrl = timeParent ? 'https://x.com' + timeParent.getAttribute('href') : '';
      const datetime = timeEl ? timeEl.getAttribute('datetime') : '';
      // Defense-in-depth: drop any card whose snowflake suffix looks fabricated
      // (no status ID, length wrong, or 6+ trailing zeros — the model's
      // template signature observed 2026-05-16). The scorer also rejects these,
      // but stripping them at scrape time keeps the structured_output clean.
      const sm = tweetUrl.match(SNOWFLAKE);
      if (!sm || FAKE_TAIL.test(sm[1])) continue;
      // Drop cards with no readable timestamp (real <time> elements always
      // carry an ISO datetime attribute).
      if (!datetime || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/.test(datetime)) continue;
      let replies=0, retweets=0, likes=0, views=0, bookmarks=0;
      for (const btn of article.querySelectorAll('[role="group"] button')) {
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
})()
""")
# 2026-05-27: HARD AGE GATE at the scrape boundary. Drops stale tweets BEFORE
# they are baked with metadata and printed between the ###TWEETS_*### sentinels,
# so the scan model literally cannot see anything older than the variant's
# freshness window in its tool_result. This closes every bypass at once:
#   - X "Latest" tab leaking older tweets past since_time: (X-side filter is
#     advisory, not authoritative)
#   - bare-keyword queries that route around the operator-anchored hook
#   - prompt drift where the model swaps since_time: back to since:
# Reads FRESHNESS_HOURS_DISCOVER from the cycle env (set by run-twitter-cycle.sh
# per variant: A/B=6h, C/D=1h). Falls back to 6h if unset. Any tweet whose
# `datetime` is malformed or missing is dropped (we cannot prove it's fresh).
import os as _os_age, datetime as _dt_age
_AGE_CAP_S = int(_os_age.environ.get('FRESHNESS_HOURS_DISCOVER') or '6') * 3600
_NOW_AGE = _dt_age.datetime.now(_dt_age.timezone.utc).timestamp()
def _is_fresh(_t):
    _ds = _t.get('datetime') or ''
    if not _ds:
        return False
    try:
        _ts = _dt_age.datetime.fromisoformat(_ds.replace('Z', '+00:00')).timestamp()
    except Exception:
        return False
    return (_NOW_AGE - _ts) <= _AGE_CAP_S
_pre_age_count = len(tweets)
tweets = [_t for _t in tweets if _is_fresh(_t)]
_age_dropped = _pre_age_count - len(tweets)
# Unconditional log so every bh_run leaves positive evidence the gate ran,
# not just the cycles where it had stale tweets to drop. Operators (and the
# dashboard scraper) can grep this marker to confirm the harness-level gate
# is loaded in the running script body.
print(f'[harness_age_gate] dropped={_age_dropped} kept={len(tweets)} pre={_pre_age_count} cap_h={_AGE_CAP_S//3600}', flush=True)

# Bake project/topic/query into each tweet object IN PYTHON, before printing —
# so the model has zero degrees of freedom on these fields. The model only
# concatenates the per-project arrays; it does not regenerate any value.
# `query` is the LITERAL X advanced-search string this bh_run call used; the
# scorer joins on it against twitter_search_attempts so each candidate gets
# stamped with the exact discovering attempt_id (2026-05-21 bug fix: dashboard
# was crediting dud queries with posts from sibling queries in the same batch).
for t in tweets:
    t['search_topic'] = search_topic
    t['matched_project'] = matched_project
    t['query'] = query
print('###TWEETS_BEGIN###')
print(json.dumps(tweets))
print('###TWEETS_END###')
```

Output rules — READ CAREFULLY, this part has historically been buggy.

1. The print() emits a tagged JSON array between `###TWEETS_BEGIN###` and `###TWEETS_END###` sentinels. THE JSON BETWEEN THE SENTINELS IS GROUND TRUTH — every field (`handle`, `text`, `tweetUrl`, `datetime`, `replies`, `retweets`, `likes`, `views`, `bookmarks`, `search_topic`, `matched_project`, `query`) is already correct.

2. When you assemble the final structured `tweets` array, you MUST copy each tweet object byte-for-byte from the JSON output. You MUST NOT:
   - Regenerate, beautify, summarize, infer, round, or otherwise modify any field.
   - "Fix" what looks like a truncated tweet URL (e.g. an ID ending in many zeros). If a card has a malformed snowflake the JS already dropped it; nothing reaches you that needs fixing.
   - "Fill in" a datetime that looks weird (round-hour stamps, missing seconds). Real Twitter datetimes look like `2026-05-16T15:42:17.000Z`. If you find yourself emitting `2026-05-16T10:00:00.000Z`, `09:00:00.000Z`, `08:00:00.000Z` etc. in 1-hour decrements, STOP — you are hallucinating, not copying. Re-read the bh_run stdout and copy the actual datetime strings.
   - Invent any tweet that did not appear in the JSON output. The tweet count you report in `queries_used.tweets_found` must equal the array length printed between the sentinels for that bh_run call.

3. After all projects: return the full `tweets` array AND a `queries_used` array (one entry per project, with `query`, `project`, `tweets_found`, and `search_topic`). The `search_topic` field is the project's ASSIGNED topic (the `search_topic` value from PROJECTS_JSON in `use` mode, or the topic you INVENTED for that project in `explore_invent` mode) pasted verbatim — same string you stamped on every tweet's `search_topic` field. Emit zero-result entries — they are logged to `twitter_search_attempts` so future cycles avoid dud phrasings and so dud-cycle topics still get attributed end-to-end.

4. NEVER make more than one bh_run call per project under normal operation. The only exception: a bh_run that returned a Python traceback (not an empty list) may be retried ONCE with the IDENTICAL script body.
HARNESS_STEP2_EOF

# --- Phase 1: Claude drafts queries, scrapes tweets -------------------------
# JSON schema forces structured output. Eliminates the prose-drift failure mode
# where the scanner summarized instead of dumping the JSON array.
SCAN_SCHEMA='{"type":"object","properties":{"tweets":{"type":"array","items":{"type":"object","properties":{"handle":{"type":"string"},"text":{"type":"string"},"tweetUrl":{"type":"string"},"datetime":{"type":"string"},"replies":{"type":"integer"},"retweets":{"type":"integer"},"likes":{"type":"integer"},"views":{"type":"integer"},"bookmarks":{"type":"integer"},"search_topic":{"type":"string"},"matched_project":{"type":"string"},"query":{"type":"string"}},"required":["handle","text","tweetUrl","datetime","replies","retweets","likes","views","bookmarks","search_topic","matched_project","query"]}},"queries_used":{"type":"array","items":{"type":"object","properties":{"query":{"type":"string"},"project":{"type":"string"},"tweets_found":{"type":"integer"},"search_topic":{"type":"string"}},"required":["query","project","tweets_found","search_topic"]}}},"required":["tweets","queries_used"]}'

log "Acquiring twitter-browser lock for Phase 1 Claude scan..."
acquire_lock "twitter-browser" 3600 2>>"$LOG_FILE"
log "twitter-browser lock held (pid=$$) Phase 1"
# Drop stale Chrome singleton symlinks before launch. Background ungraceful-
# exits (SIGKILL, jetsam, force quit) leave Singleton{Lock,Cookie,Socket}
# pointing at dead PIDs / vanished sockets; without this, Chrome pops "Something
# went wrong when opening your profile" 7x and the pipeline hangs. Helper
# refuses to clean if the lock PID is alive.
ensure_twitter_browser_for_backend 2>&1 | tee -a "$LOG_FILE"

log "Phase 1: drafting queries and scraping tweets..."

SCAN_OUTPUT=$("$REPO_DIR/scripts/run_claude.sh" "run-twitter-cycle-scan" --strict-mcp-config --mcp-config "$TW_MCP_CONFIG" -p --output-format json --json-schema "$SCAN_SCHEMA" "${TW_ENGINE_PREFIX}You are a Twitter hot-tweet scanner. Your ONLY job is to find high-engagement tweets happening RIGHT NOW that are relevant to one of our projects. Do NOT post anything.

## Step 1: Draft one search query per project

You have $(echo "$PROJECTS_JSON" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))') projects. Draft exactly ONE Twitter search query for each, tailored to that project's ASSIGNED search_topic.

Each project entry carries TWO fields that drive your behavior: \`topic_picked_mode\` (either \`use\` or \`explore_invent\`) and \`search_topic\` (a string in \`use\` mode, NULL in \`explore_invent\` mode).

USE mode (~90% of cycles, indicated by \`topic_picked_mode: "use"\` and a non-null \`search_topic\`):
The Python picker has already chosen this project's search_topic by weighted-random sampling over the FULL universe in config.json. Weights are log-smoothed so the top performer takes about 20-30% and every cold topic still has a small but real chance (around 0.5-1%). The chosen topic's share of the weight is in \`picked_weight_pct\` for transparency. Your job is to translate that ASSIGNED topic into the best Twitter advanced-search query that will surface fresh, on-topic tweets discussing it. Do NOT substitute a different topic; do NOT paraphrase the topic into an adjacent angle even if a sibling topic looks more attractive. The picker has already made the selection and end-to-end attribution joins on this exact string — drift breaks the analytics.

EXPLORE_INVENT mode (~10% of cycles, indicated by \`topic_picked_mode: "explore_invent"\` and \`search_topic: null\`):
The picker is asking you to INVENT a brand-new search_topic for this project. Look at the project's own \`reference_topics\` array (per-project pool stats with weight, score, posts, clicks, posted_n, skipped_n) to see what's already working, what's saturated, and where the gaps are. Propose ONE new topic concept that does NOT appear in reference_topics and is NOT a paraphrase of anything in reference_topics. The invention should be in the project's domain (see \`description\`) but probe a gap the current list does not cover — adjacent verticals, longer-tail phrasings, fresh angles on the value prop. It must be plausible as something real users tweet about. Use your invented topic for the query AND stamp it on every tweet's \`search_topic\` field in the bh_run output (one consistent string for every tweet you return from this project's query).

Each project's \`reference_topics\` is the project-scoped pool the picker drew from (or that you're inventing around). Use it to ground your query phrasing in either mode. The global TOP_TOPICS_JSON / DUD_TOPICS_JSON blocks below are CONTEXT for operator selection only, NOT a menu to pick a different topic from.

Projects:
$PROJECTS_JSON

Top past queries FOR THE PROJECT YOU'RE DRAFTING FOR (per-project, sorted by clicks DESC first, then composite-scored: clicks×100 + likes + views×0.001). CLICKS ARE THE PRIORITY SIGNAL. Any query with \`clicks_total > 0\` is GOLD TIER for THAT project — clicks are the only metric that proves our reply drove someone to actually visit the project's link. Likes and views are vanity. Optimize the entire pipeline for clicks; everything else is leading indicators.

ROLE BOUNDARY (strict): this list is for STRUCTURE inspiration only — operators, keyword density, query length, phrasing patterns. The canonical source for \`min_faves:N\` selection is the PER-PROJECT SUPPLY SIGNAL block further down. Do NOT pull min_faves tiers from this list even when a gold-tier row uses a specific tier; the supply table reflects what X actually serves for the project's audience today. Mimic the STRUCTURE of YOUR project's gold-tier queries; let the supply table set the min_faves floor.

JSON shape: a dict keyed by project name. For each project: \`project_queries\` is the array of that project's top historical queries (may be empty). Cold-start projects (empty array) have no historical signal yet — rely on the PER-PROJECT SUPPLY SIGNAL block below for min_faves and your project's \`description\` + assigned \`search_topic\` for keyword phrasing. There is no cross-project fallback by design.

Each entry exposes the FULL conversion funnel AND a posted-vs-skipped split so you can diagnose query failure modes:

  Funnel signals (downstream, only meaningful for posted candidates):
  - \`tweets_found_avg\`: X's supply for that query (how many tweets it returned per search attempt)
  - \`posted_n\`: replies we actually shipped from this query (status='posted')
  - \`skipped_n\`: candidates discovered but NOT posted (status='skipped' or 'expired')
  - \`post_rate\`: posted_n / (posted_n + skipped_n); the draft-gate acceptance ratio. A high \`posted_n\` with low \`post_rate\` means the query is loud (surfaces lots of candidates) but Claude keeps rejecting them at the draft gate; reword for tighter fit rather than copying it as-is.
  - \`views_total\`, \`likes_total\`: engagement on OUR replies
  - \`clicks_total\`: link clicks attributed to our replies (CTA tracking).
    THIS IS THE ULTIMATE SIGNAL, clicks on Twitter are extremely sparse, so a single click is a strong endorsement of the query+reply combo. Composite score weights clicks ×100 deliberately.

  Source-thread split (read these together, they tell you WHY the query did or did not produce posts):
  - \`avg_virality_posted\`: avg source-thread virality_score for the threads we DID post to. High = the query surfaces threads that are both viral AND on-topic enough that Claude judged them post-worthy.
  - \`avg_virality_skipped\`: avg source-thread virality_score for the threads we did NOT post to. Diagnostic: if \`avg_virality_skipped\` is high but \`posted_n\` is low, the query is finding viral NOISE (loud but off-topic threads, the keyword cluster is mismatched even though the engagement floor is fine). Reword the query in that case rather than dropping it. If both \`avg_virality_skipped\` and \`avg_virality_posted\` are low, the query is just dead supply, drop the keyword cluster.

Use these as STYLE inspiration for phrasing and operators within YOUR project's row. Do NOT copy keywords literally; adapt them to each project's current assigned search_topic.
$TOP_QUERIES_PER_PROJECT_JSON

TOP-PERFORMING SEARCH TOPICS (conceptual seeds, 14d window) — one level above queries. Where queries are the literal X strings, topics are the conceptual seeds they were drafted from. The pipeline writes \`search_topic\` onto every twitter_candidates row at discovery, so this list aggregates per topic across all the different queries that surfaced it. Use this to evolve the TOPIC UNIVERSE itself, not just to reword queries:
  - \`posts\` / \`skipped_n\`: how often a topic surfaces threads we post to versus skip. A topic with high skipped vs low posted is ON-TOPIC but the queries that surface it find off-fit threads — REWORD the queries for that topic, do not drop the topic.
  - \`clicks_total\`: the ultimate signal. Topics with non-zero clicks have proven they convert; bias new query drafts toward them and INVENT close-variant topics (e.g. "AI agent that takes actions" winning -> try "agent that completes tasks", "autonomous agent loop").
  - \`avg_virality_posted\` vs \`avg_virality_skipped\`: if posted virality is high AND clicks are non-zero, this topic is a winner — keep drafting fresh queries for it. If both viralities are low, the topic is dead supply; let it cycle out by not drafting for it this run.
  - \`composite_score\`: clicks×100 + likes + views×0.001, descending. Top of the list is where the marginal effort pays off.
Treat the topic list as CONTEXT for query phrasing and operator selection, NOT a menu to pick from. As of 2026-05-26 the picker chooses ONE topic per project before this prompt fires; you draft a query for THAT topic only. The TOP_TOPICS list shows you which topics have converted historically so you can model the operator/keyword density of winning queries — it does NOT authorize you to swap the assigned topic for a higher-ranked one. New-topic invention is now an offline concern handled by the picker's explore branch (5% of cycles), not an inline decision.
$TOP_TOPICS_JSON

DUD QUERIES — DO NOT REUSE these phrasings or close variants. They returned ZERO tweets in the last 48h, so redrafting them wastes the budget. \`attempts\` is how many cycles already wasted on each one; \`last_ran_h_ago\` is hours since the most recent attempt; \`min_faves\` is the floor that produced zero supply (look for patterns: if EVERY dud for a project uses the same min_faves tier, the floor is too high for that project's audience and you should DROP it). Pick a different angle, different operators, or different keyword cluster:
$DUD_QUERIES_JSON

DUD CONCEPT SEEDS — one level up from DUD QUERIES. These search_topic seeds pulled in tweets that Phase 2b's draft gate kept skipping (or that expired un-drafted) over the last 7d. They are off-fit at the CONCEPT level, not the query level. Per entry: \`posted_n\` is candidates from this seed that actually got replied to, \`skipped_n\` is candidates rejected at the draft gate or expired, \`omit_rate\` = skipped_n / (posted_n + skipped_n), \`avg_virality_skipped\` is how viral the OFF-FIT tweets were (high values mean the seed finds noise, not buyers), and \`sample_skip_reasons\` is the top-5 most-common reject reasons from Phase 2b. Action rules:
  - If omit_rate >= 0.6 AND skipped_n >= 5: REWORD the queries narrower for that project (cite the dud topic you are replacing in the queries_used note), OR drop the seed this cycle and pick a different config.json seed for that project.
  - Read sample_skip_reasons BEFORE rewriting. If the pattern is audience mismatch ("Arabic-language students" for a dev tool, "TV news" for a podcast tool), add domain anchors to the query. If it is "competitor launch" or "vendor squabble" or "author threatened spam report", drop the seed for this cycle.
  - A seed appearing here does NOT mean delete from config.json; it means THIS cycle's queries are pulling the wrong slice. Future cycles can revisit the seed with sharper queries once the dud signal cools.
$DUD_TOPICS_JSON

PER-PROJECT SUPPLY SIGNAL — for each project, the historical median tweets_found at each \`min_faves:N\` tier you've drafted for them in the last 14d. This REPLACES the old flat "broad=50 / narrow=20" rule. Pick the LOWEST min_faves tier where \`median_tweets_found\` >= 3 for the project you're drafting for; if every tier is below 3, drop one tier lower than the lowest you've tried. Niche audiences (med students, meditators) cluster at min_faves:5–15; tech audiences (devs, AI) cluster at min_faves:20–50. Trust this table over your priors:
$SUPPLY_SIGNAL_JSON

ALREADY-ENGAGED TWEET IDS — we have already posted a reply to each of these tweets within the last 48h, so they are dead candidates. Do NOT return any tweet whose status ID (the digits in \`/status/<ID>\`) appears in this list; skip it while scraping so it never reaches your \`tweets\` array. Returning one only wastes tokens — it is dropped downstream regardless.
$ENGAGED_TWEET_IDS

$HOOK_NOTICE

$ANTI_DEBUG_RULE

Query guidelines:
- MANDATORY: every query MUST end with the operator \`since_time:$(( $(date +%s) - FRESHNESS_HOURS_DISCOVER * 3600 ))\` copied EXACTLY as written. It is a pre-computed Unix-epoch timestamp; do NOT recalculate, reformat, or round it, just paste it verbatim into every single query. It restricts X to tweets posted in the last ${FRESHNESS_HOURS_DISCOVER} hours, which is the cycle's freshness wall: tweets older than that are dropped at scoring and the whole search is wasted. Do NOT use the day-granular \`since:YYYY-MM-DD\` operator: it admits tweets up to ~45h old that the scorer then discards. Even if some past top-performing queries shown below still use \`since:\`, you MUST use \`since_time:\` instead; those examples predate this rule.
- MANDATORY EVEN IF YOUR QUERY KEYWORDS DO NOT NAME THE EXCLUDED TOPIC. If a project's \`excludes_for_search\` array is non-empty, append \`-term\` for EVERY listed term to that project's query, verbatim, with NO EXCEPTIONS. The exclusion is project-wide and persistent — it is a safety rail against ALL future false positives for that project, not a query-keyword-conditional rule. Do NOT reason "my search keywords are about meditation/local AI/vibe coding so the cricket/crypto/Bolt excludes are unnecessary" — that reasoning defeats the rail. The terms passed the >=2-batch activation gate; they have ALREADY survived a one-off filter. Your job here is purely mechanical concatenation, not editorial judgment. Concrete examples of what MUST happen: if Vipassana has \`excludes_for_search: ["cricket","ipl","kohli","lsg","pant","csk","inglis","suvendu","tmc","bjp"]\`, your Vipassana query MUST end with \` -cricket -ipl -kohli -lsg -pant -csk -inglis -suvendu -tmc -bjp\` even when the query searches for "meditation" or "vipassana" with no mention of Goenka. If fazm has \`excludes_for_search: ["memecoin","okx","onchain"]\`, every fazm query gets \` -memecoin -okx -onchain\` appended whether searching "local AI", "RPA", or anything else. If mk0r has \`excludes_for_search: ["usain"]\`, every mk0r query gets \` -usain\`. Skipping these in any query is a bug.
- MANDATORY: pick \`min_faves:N\` per the PER-PROJECT SUPPLY SIGNAL above. If the project has no entry in the supply table (new project or first cycle), use min_faves:20 as a starting point; the next cycle will see your attempt and self-tune. Never hardcode min_faves:50 for a project whose supply table shows zero results at that tier.
- Favor discussions/opinions (people sharing experience, asking questions), not news/promos/giveaways
- Pick a query likely to surface tweets RELEVANT to that project's actual domain
- Mix it up each run, don't always use the same query for the same project
- Use the project's ASSIGNED \`search_topic\` plus its \`description\` as grounding for query phrasing (the topic is a shared concept seed across platforms — some phrases are tuned for Reddit or GitHub, so rephrase into natural Twitter search terms with hashtag-adjacent vernacular). The assigned topic is the SUBJECT of the query; the description is the project's voice. Do not invent off-topic queries.
- In the bh_run scrape script you run for each project, the \`search_topic\` Python variable MUST be the project's assigned \`search_topic\` field pasted verbatim (NOT the query string, NOT a paraphrase). The scoring pipeline stamps \`twitter_candidates.search_topic\` from this value and joins it against the picker's assignment for end-to-end attribution.

$STEP2_INSTRUCTIONS" 2>&1)

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
        --attempts-out "$ATTEMPTS_FILE" \
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
    # Classify the Anthropic-side cause of the empty-tweet outcome so the
    # dashboard surfaces the *actual* error (stream_idle_timeout,
    # monthly_limit, api_overloaded, context_overflow, etc.) instead of
    # collapsing every Claude failure to a generic phase1_no_tweets pill.
    # Bit us on 2026-05-15 16:45 cycle: $10.77 burned to a "Stream idle
    # timeout - partial response received" that read as a silent
    # phase1_no_tweets in run_monitor.log. Falls back to phase1_no_tweets
    # when Claude returned successfully but with zero usable tweets (the
    # historical "Claude tried, found nothing relevant" case).
    PHASE1_REASON=$(echo "$SCAN_OUTPUT" | python3 "$REPO_DIR/scripts/classify_run_error.py" 2>/dev/null)
    [ -z "$PHASE1_REASON" ] && PHASE1_REASON="phase1_no_tweets"
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
        ${ATTEMPTS_FILE:+--attempts "$ATTEMPTS_FILE"} \
    2>&1 | tee -a "$LOG_FILE"
rm -f "$RAW_FILE" "$ATTEMPTS_FILE"

BATCH_COUNT=$(python3 "$REPO_DIR/scripts/twitter_cycle_helper.py" batch-count --batch-id "$BATCH_ID" 2>/dev/null || echo 0)
log "Phase 1 complete. Batch has $BATCH_COUNT candidates with T0 snapshot."

# Stamp the A/B/C variant onto every candidate in this batch so downstream
# analytics (post-rate, thread-age-at-discover, lag-after-thread, top-reply
# ratio) can split by variant. Idempotent: same value would be written if the
# batch is salvaged into a peer cycle (variant follows the discovering cycle).
python3 -c "
import os, psycopg2 as psycopg
with psycopg.connect(os.environ['DATABASE_URL']) as conn:
    with conn.cursor() as cur:
        cur.execute('UPDATE twitter_candidates SET cycle_variant=%s WHERE batch_id=%s AND cycle_variant IS NULL', (os.environ['TWITTER_CYCLE_VARIANT'], os.environ['BATCH_ID']))
        conn.commit()
" 2>>"$LOG_FILE" || log "Phase 1: cycle_variant stamp failed (non-fatal)"

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
release_lock "twitter-browser" 2>>"$LOG_FILE"
# Defense-in-depth: clear the twitter_browser.py process lockfile so the next
# cycle's writer never sees a stale entry from us. run_claude.sh's exit trap
# already does this; explicit repeat covers SIGKILL of the wrapper.
rm -f "$HOME/.claude/twitter-browser-lock.json"

# --- Sleep 20 min before T1 measurement -------------------------------------
# Variants B, C, and D skip ripening entirely (2026-05-22 A/B/C test,
# extended 2026-05-25 with D = C + 2k view cap). The 20-min wait + t1
# re-fetch was originally a velocity gate; the gate floor was removed
# 2026-05-15 so the wait only feeds delta_score into the LLM prompt now.
# Variants B/C/D test whether eliminating that ~20 min thread->post lag
# meaningfully improves engagement vs. the marginal informational value of
# delta_score in the draft prompt.
if [ "$TWITTER_CYCLE_VARIANT" = "A" ]; then
    log "Variant A: sleeping 1200s before T1 re-measurement..."
    sleep 1200

    # Re-stamp phase2a the instant we wake. The 20-min phase2a budget was set
    # BEFORE the 1200s sleep, so it has now expired; any peer cycle's Phase 0
    # salvage that fires between wake and the phase2b-prep stamp below will
    # steal this batch's pending rows. Re-stamping resets the budget clock so
    # the short T1 poll + Phase 2b candidates query are protected.
    # Incident reference: batch twcycle-20260522-113005 woke at 12:00:01, the
    # 12:00 peer's Phase 0 salvaged 150 rows at 12:00:05, and this batch's
    # pending query at 12:00:06 returned 0, triggering "No candidates with
    # delta scores. Marking batch expired." even though 4 candidates had
    # passed the Δ>=10 floor (jain_harshit Δ=35.3, sawyerhood Δ=33.6,
    # laurasideral Δ=24.2, alessandro_a0 Δ=20.3).
    python3 "$REPO_DIR/scripts/twitter_batch_phase.py" advance "$BATCH_ID" --phase phase2a 2>&1 | tee -a "$LOG_FILE" || true

    # --- Phase 2a: re-fetch T1 engagement -----------------------------------
    log "Phase 2a: re-polling fxtwitter for T1 engagement..."
    python3 "$REPO_DIR/scripts/fetch_twitter_t1.py" --batch-id "$BATCH_ID" 2>&1 | tee -a "$LOG_FILE"
else
    log "Variant $TWITTER_CYCLE_VARIANT: skipping ripening wait and T1 fetch (no sleep, delta_score stays at T0 value)"
fi

# --- Phase 2b: top 25 by virality_score, no post cap ---------------------
# Sort key (2026-05-27): virality_score DESC. This is the composite predictor
# stamped at discovery by score_twitter_candidates.py:
#   virality_score = velocity * reach_mult * age_decay * rt_bonus
#                    * (1 + reply_bonus) * (1 + discussion_bonus)
# It folds in engagement velocity, author reach (follower-tier multiplier),
# age decay (6h half-life), retweet ratio, reply count, and discussion
# quality (reply:like ratio). Cohort analysis on 30d posted data: the
# [10k+) virality bucket gets ~36x the median reply views of the [0-10)
# bucket, which is much steeper than what raw 5-min delta predicts.
# Replaces the prior `delta + flat-5 intent-regex boost` sort: the intent
# regex was a crutch for delta_score (a raw growth count that ignored
# reach + decay); the model reads tweet text directly in the prep prompt
# and detects intent itself, so the lexical layer is redundant.
# 2026-05-15: ripening floor removed entirely (was `delta_score >= 0`).
# The model already sees per-candidate Virality + Delta in CANDIDATE_BLOCK
# below and can weigh velocity against topical fit itself. Letting
# negative-delta tweets through means a thoughtful comment can still ride
# an on-theme but cooling thread to the right audience. LIMIT 25 stays as
# a draft-budget cap, not a ripening gate.
# Candidate list comes through /api/v1/twitter-candidates (route returns
# all pending rows for the batch); the helper applies the virality_score
# sort + 25-row cap client-side and emits the SAME pipe-separated columns
# the legacy psql -F '|' query produced. Pipe shape is documented in
# scripts/twitter_cycle_helper.py:cmd_candidates.
CANDIDATES=$(python3 "$REPO_DIR/scripts/twitter_cycle_helper.py" candidates --batch-id "$BATCH_ID" 2>/dev/null || echo "")

if [ -z "$CANDIDATES" ]; then
    log "No candidates with delta scores. Marking batch expired."
    # /api/v1/twitter-candidates/expire-batch performs the same status-flip
    # UPDATE atomically and prints the resulting expired_count integer that
    # the EXPIRED_BATCH variable previously got from a second COUNT(*) query.
    EXPIRED_BATCH=$(python3 "$REPO_DIR/scripts/twitter_cycle_helper.py" expire-batch --batch-id "$BATCH_ID" 2>/dev/null || echo 0)
    _COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START" --scripts "run-twitter-cycle-scan" "run-twitter-cycle-post" 2>/dev/null || echo "0.0000")
    # Not a hard error — batch had candidates but none remained 'pending' after
    # Phase 2a (typically: every row already flipped to posted/skipped/expired
    # by an earlier salvage pass). With the ripening floor removed (2026-05-15),
    # this no longer fires on low-delta rows; only on empty/exhausted batches.
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
log "Top $CANDIDATE_COUNT candidates by virality_score selected for post review."

# No post cap: Phase 2b-prep posts every candidate it judges genuinely
# on-brand. HIGH_DELTA_COUNT is still computed, but ONLY as a dashboard
# diagnostic (the "Δ≥10 N" stat, fed to log_run.py --above-floor). It no
# longer gates how many replies the cycle is allowed to post.
HIGH_DELTA_COUNT=$(printf '%s\n' "$CANDIDATES" | awk -F'|' '$1 ~ /^[0-9]+$/ && $6+0 >= 10 {n++} END {print n+0}')
log "Candidates with Δ≥10 (momentum diagnostic only, not a cap): $HIGH_DELTA_COUNT"

CANDIDATE_BLOCK=""
while IFS='|' read -r cid curl cauthor ctext cscore cdelta cproject ctopic clikes crts creplies cviews cfollowers cage cdraft cdraftstyle cdraftage; do
    DRAFT_LINE=""
    if [ -n "$cdraft" ] && [ "$cdraftage" != "-1" ]; then
        # Round draft age to whole minutes for the prompt.
        DRAFT_MIN=$(printf '%.0f' "$cdraftage")
        DRAFT_LINE="
EXISTING DRAFT (style=$cdraftstyle, age=${DRAFT_MIN}m): $cdraft"
    fi
    # Per-candidate prior-interaction context: surface our last 5 comments to
    # this author in the past 30 days (soft context only — vary angle, don't
    # repeat phrasing). Empty when we have no history. Failure is silent.
    AUTHOR_HISTORY_LINE=""
    if [ -n "$cauthor" ]; then
        _AH=$(python3 "$REPO_DIR/scripts/author_history_block.py" --platform twitter --author "$cauthor" --days 30 --limit 5 2>>"$LOG_FILE" || true)
        if [ -n "$_AH" ]; then
            AUTHOR_HISTORY_LINE="
$_AH"
        fi
    fi
    CANDIDATE_BLOCK="${CANDIDATE_BLOCK}
---
Candidate ID: $cid
URL: $curl
Author: @$cauthor (${cfollowers} followers)
Text: $ctext
Virality: $cscore | Delta (5min): $cdelta | Likes: $clikes | RTs: $crts | Replies: $creplies | Views: $cviews | Age: ${cage}h
Search query: $ctopic
Project match: $cproject${DRAFT_LINE}${AUTHOR_HISTORY_LINE}
"
done <<< "$CANDIDATES"

ALL_PROJECTS_JSON=$(python3 -c "
import json, os
config = json.load(open(os.path.expanduser('~/social-autoposter/config.json')))
print(json.dumps({p['name']: p for p in config.get('projects', [])}, indent=2))
" 2>/dev/null || echo "{}")

# Engagement-style picker (2026-05-19): pick ONE assigned style for this
# cycle. The picked style flows two places: (1) --style filter for
# top_performers.py so the per-style exemplars section shows only posts
# matching the assigned style, (2) saps_render_style_block (below) so the
# prompt block embeds the same assignment. On invent mode picked_style is
# empty and top_performers stays unfiltered (model sees full landscape).
source "$REPO_DIR/skill/styles.sh"
STYLE_ASSIGN_FILE=$(mktemp -t saps_twitter_assign_XXXXXX.json)
saps_pick_style twitter posting "$STYLE_ASSIGN_FILE" >/dev/null 2>&1 || true
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
log "Engagement style assigned: mode=$PICKED_MODE style=${PICKED_STYLE:-(invent)}"

if [ -n "$PICKED_STYLE" ]; then
    TOP_REPORT=$(python3 "$REPO_DIR/scripts/top_performers.py" --platform twitter --style "$PICKED_STYLE" 2>/dev/null || echo "(top performers report unavailable)")
else
    TOP_REPORT=$(python3 "$REPO_DIR/scripts/top_performers.py" --platform twitter 2>/dev/null || echo "(top performers report unavailable)")
fi

# --- Generation trace -------------------------------------------------------
# Snapshot the few-shot context this cycle will feed to Claude — top_performers
# report, top_queries from Phase 1, supply signal, dud queries — and write to a
# tempfile. Path travels via env var to twitter_post_plan.py (Phase 2b-post),
# which forwards it as --generation-trace to log_post.py so every post landed
# this cycle gets posts.generation_trace JSONB pointing to the same snapshot.
# This is what answers "which examples produced post #N" later. See
# migrations/2026-05-12_generation_trace.sql for the shape contract.
#
# Failure is non-fatal: empty string means downstream skips --generation-trace
# and the row gets NULL trace. We never block the cycle on the audit row.
TRACE_INPUT=$(python3 -c "
import json, sys
payload = {
    'platform': 'twitter',
    'project_name': 'all',
    'prompt_chars': len(sys.argv[1]) + len(sys.argv[2]) + len(sys.argv[3]) + len(sys.argv[4]) + len(sys.argv[7]),
    'top_performers_text': sys.argv[1],
    'top_search_topics_text': sys.argv[7],
    'recent_comment_ids': [],
    'extras': {
        'top_queries_per_project': json.loads(sys.argv[2] or '{}'),
        'supply_signal': json.loads(sys.argv[3] or '[]'),
        'dud_queries': json.loads(sys.argv[4] or '[]'),
        'auto_picked_style': sys.argv[5] or None,
        'auto_picked_mode': sys.argv[6] or 'use',
        'top_search_topics': json.loads(sys.argv[7] or '[]'),
    },
    'min_score_floor': 5,
}
print(json.dumps(payload))
" "$TOP_REPORT" "$TOP_QUERIES_PER_PROJECT_JSON" "$SUPPLY_SIGNAL_JSON" "$DUD_QUERIES_JSON" "$PICKED_STYLE" "$PICKED_MODE" "$TOP_TOPICS_JSON" 2>/dev/null || echo '{}')
SAPS_TWITTER_GEN_TRACE_PATH=$(printf '%s' "$TRACE_INPUT" | python3 "$REPO_DIR/scripts/write_generation_trace.py" --prefix twitter_gen_trace_ 2>/dev/null || echo "")
export SAPS_TWITTER_GEN_TRACE_PATH
if [ -n "$SAPS_TWITTER_GEN_TRACE_PATH" ] && [ -f "$SAPS_TWITTER_GEN_TRACE_PATH" ]; then
    log "Generation trace: $SAPS_TWITTER_GEN_TRACE_PATH ($(wc -c < "$SAPS_TWITTER_GEN_TRACE_PATH") bytes)"
else
    log "WARN: generation_trace build returned empty path; posts this cycle will have NULL trace"
fi

STYLES_BLOCK=$(saps_render_style_block "$STYLE_ASSIGN_FILE" twitter posting)
# Style assignment file is the same one we picked above; styles.sh already sourced.
# Cleanup at cycle end (best effort).
trap 'rm -f "$STYLE_ASSIGN_FILE" 2>/dev/null || true' EXIT

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
# Stamp phase2b-prep BEFORE the long-running Claude read/draft so peer cycles'
# Phase 0 salvage SQL sees current_phase='phase2b-prep' (45-min budget) instead
# of stale phase2a (20-min budget). Without this stamp, mid-Phase-2b runs get
# wrongly salvaged once 20 min elapse past phase2a's start, creating false
# phase2b_silent run-monitor rows even when posts succeeded.
python3 "$REPO_DIR/scripts/twitter_batch_phase.py" advance "$BATCH_ID" --phase phase2b-prep 2>&1 | tee -a "$LOG_FILE" || true
log "Re-acquiring twitter-browser lock for Phase 2b-prep (read+draft only)..."
acquire_lock "twitter-browser" 3600 2>>"$LOG_FILE"
log "twitter-browser lock held (pid=$$) Phase 2b-prep"
# Drop stale singleton locks (see clean_stale_singleton.sh, also called in Phase 1).
ensure_twitter_browser_for_backend 2>&1 | tee -a "$LOG_FILE"

log "Phase 2b-prep: Claude reading threads and drafting replies (no post cap)..."

# Pre-assign the prep session UUID in the parent shell so it survives the
# command-substitution subshell run_claude.sh runs in. We write it into the
# plan JSON below so Phase 2b-post can re-export it for log_post.py, which
# stamps posts.claude_session_id and lets the dashboard activity feed join
# to claude_sessions for cost. Without this, twitter posts get NULL session
# ids and blank cost cells.
CLAUDE_SESSION_ID="$(uuidgen | tr 'A-Z' 'a-z')"
export CLAUDE_SESSION_ID

# PREP_SCHEMA — strict JSON schema for the prep envelope. Includes
# optional `new_style` per candidate (an inner object) that the model
# MUST populate when it chooses a brand-new engagement style name (i.e.
# the picker set mode=invent and the model invented a snake_case name).
# Fields mirror engagement_styles.py::_REQUIRED_NEW_STYLE_FIELDS so the
# downstream validate_or_register call accepts the block without a
# second schema layer.
PREP_SCHEMA='{"type":"object","properties":{"candidates":{"type":"array","items":{"type":"object","properties":{"candidate_id":{"type":"integer"},"candidate_url":{"type":"string"},"thread_author":{"type":"string"},"thread_text":{"type":"string"},"matched_project":{"type":"string"},"reply_text":{"type":"string"},"engagement_style":{"type":"string"},"new_style":{"type":["object","null"],"properties":{"description":{"type":"string"},"example":{"type":"string"},"why_existing_didnt_fit":{"type":"string"},"note":{"type":"string"}}},"language":{"type":"string"},"has_landing_pages":{"type":"boolean"},"link_keyword":{"type":"string"},"link_slug":{"type":"string"},"search_topic":{"type":"string"}},"required":["candidate_id","candidate_url","matched_project","reply_text","engagement_style","language","has_landing_pages","search_topic"]}},"rejected":{"type":"array","items":{"type":"object","properties":{"candidate_id":{"type":"integer"},"reason":{"type":"string"},"proposed_excludes":{"type":"array","items":{"type":"string"}}},"required":["candidate_id","reason"]}}},"required":["candidates","rejected"]}'

PREP_PROMPT="${TW_ENGINE_PREFIX}You are the Social Autoposter prep step.

Your ONLY job in THIS session:
  1. Read each thread you decide to reply to (browser tools from the BROWSER BACKEND block above, READ-ONLY).
  2. Draft a reply for each.
  3. Persist each fresh draft via log_draft.py.
  4. Emit a structured plan describing the chosen candidates, the reply text, and (when applicable) the SEO link keyword + slug.

You will NOT post anything. You will NOT generate landing pages. You will NOT call log_post.py. The shell handles all of that AFTER your session ends, with the browser lock released for the long landing-page build.

Read $SKILL_FILE for content rules and voice context.
Read $REPO_DIR/config.json for project metadata.

## PRE-SCORED CANDIDATES (sorted by Virality DESC, highest first)
Virality is a composite predictor of how big this thread will get AFTER you reply: it combines engagement velocity (eng/hour), author reach (follower tier), age decay (6h half-life), retweet ratio, reply count, and discussion quality (reply:like ratio). On historical posted data the highest-Virality cohort (score >= 10000) received ~36x the median reply views of the lowest cohort (score < 10), so prioritize on-brand candidates with HIGH Virality. Rule of thumb: Virality >= 100 = strong thread on a real growth curve, your reply is likely to land 10-100x more eyeballs than a low-Virality thread. Delta (5min) is the raw T1-T0 engagement count and is shown for context only; do not re-rank on Delta.
$CANDIDATE_BLOCK

## PROJECT ROUTING (per-candidate)
Each candidate has a 'Project match' field. Use that project unless the thread content clearly better fits another project.
All project configs: $ALL_PROJECTS_JSON

## FEEDBACK FROM PAST PERFORMANCE:
$TOP_REPORT

$STYLES_BLOCK

## WORKFLOW
There is NO cap on how many candidates you may pick this cycle. Pick EVERY candidate whose thread is genuinely on-brand and worth a substantive reply. Skip a candidate ONLY when its thread is off-topic for the matched project, toxic / hateful, low-quality / spam, an audience mismatch, or a near-duplicate of something already replied to. Do NOT cap, quota, or balance picks by project: if the strongest candidates this cycle all belong to one project, pick all of them. Project routing matters; project diversification does not. Never force a weak entry just to add volume, and never drop a strong on-brand entry just to limit volume.

For each chosen candidate:
1. Navigate to CANDIDATE_URL using the navigate tool from the BROWSER BACKEND block above (READ-ONLY).
2. Read the thread to understand context.
3. DRAFT HANDLING (existing vs fresh):
   - If the candidate block shows an EXISTING DRAFT line AND draft age < 30 minutes, REUSE the draft text verbatim. Set engagement_style to the existing style. Do NOT call log_draft.py; do NOT redraft. Reason: prior cycle paid the LLM cost.
   - Otherwise: draft a reply using the best engagement style. 1-2 sentences. NEVER em dashes. Apply the matched project's \`voice\` block from ALL_PROJECTS_JSON: follow voice.tone, never violate voice.never, mirror voice.examples / voice.examples_good when present.
3a. PERSIST FRESH DRAFTS (skip for reused drafts):
     python3 $REPO_DIR/scripts/log_draft.py --candidate-id CANDIDATE_ID --text 'YOUR_REPLY_TEXT' --style STYLE --assigned-style '$PICKED_STYLE' --assigned-mode '$PICKED_MODE'
   The --assigned-style / --assigned-mode flags carry the orchestrator's picker output (this cycle: mode=$PICKED_MODE style='${PICKED_STYLE:-(invent)}') into the candidate row so the post pipeline can coerce drift and register invented styles. Pass them VERBATIM as shown.
   If you are inventing a brand-new style this cycle (i.e. \$PICKED_MODE=invent and your STYLE is a new snake_case name not in the style block above), ALSO pass:
     --new-style '{\"description\":\"...\",\"example\":\"...\",\"why_existing_didnt_fit\":\"...\"}'
   with the same description/example/why_existing_didnt_fit you put in the 'new_style' field of your output JSON for this candidate.
   Failure here is non-fatal, log a warning and continue.
4. EMIT one entry in the structured 'candidates' array with these fields:
   - candidate_id (int): from the candidate block
   - candidate_url (string): the parent tweet URL
   - thread_author (string): the @handle (no leading @)
   - thread_text (string): the parent tweet's text, condensed to <=500 chars if needed
   - matched_project (string): the project name to attribute this post to
   - reply_text (string): the FINAL reply text WITHOUT any URL appended (the shell appends the URL later). Keep <=250 chars so a 23-char t.co link fits inside the 280-char Twitter cap.
   - engagement_style (string): style name applied (or 'reused' for an unchanged stale draft). In USE mode ($PICKED_MODE=use) this MUST be the assigned style name '${PICKED_STYLE}' verbatim; the orchestrator silently coerces drift back. In INVENT mode ($PICKED_MODE=invent) this MUST be a NEW snake_case style name not in the curated style block.
   - new_style (object, REQUIRED iff INVENT mode produced a new name; OMIT or set null otherwise): {description (string), example (string), why_existing_didnt_fit (string), note (string, optional)}. Same shape you passed to --new-style in step 3a. The post pipeline reads this and POSTs to /api/v1/engagement-styles/registry so the new style lands in engagement_styles_registry alongside Reddit/GitHub/Moltbook inventions.
   - language (string): ISO 639-1 code (en, ja, zh, es, ...)
   - has_landing_pages (bool): true iff the matched project has BOTH landing_pages.repo AND landing_pages.base_url set in config.json. Otherwise false.
   - link_keyword (string, REQUIRED when has_landing_pages=true; OMIT otherwise): a SHORT 3-6 word phrase that captures the ESSENCE OF YOUR REPLY (not just the thread topic). Think: what would a reader search to find a useful page about what you just said?
   - link_slug (string, REQUIRED when has_landing_pages=true; OMIT otherwise): kebab-case, alphanumeric+hyphens only, max 50 chars.
   - search_topic (string, REQUIRED): the EXACT 'Search query' value from this candidate's block above. Copy verbatim. Do not paraphrase, normalise, or trim. The shell stamps this onto posts.search_topic so the next cycle's Phase 1 can rank which topics convert (clicks per post) and evolve the universe accordingly.

5. ACCOUNT FOR EVERY PRE-SCORED CANDIDATE: every Candidate ID listed in the PRE-SCORED CANDIDATES section above MUST appear in EXACTLY ONE of the two output arrays this cycle:
   - 'candidates' (every on-brand pick, no cap) per step 4 above, OR
   - 'rejected' with a SHORT one-line reason explaining why this thread is not worth replying to (off-topic for the matched project, toxic / hateful, low-quality / spam, audience mismatch, near-duplicate of something we already replied to, etc.). Reason must be <=200 chars, plain text, no quotes.
   It is fine for 'candidates' to be empty if no thread is on-brand; in that case every candidate id goes into 'rejected'. The reverse (every id in 'candidates', none in 'rejected') is also allowed when every thread is genuinely on-brand.
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
- Browser tools (from the BROWSER BACKEND block) are READ-ONLY in this step.
- NEVER use em dashes. Use commas, periods, or regular dashes (-).
- Reply in the SAME LANGUAGE as the parent tweet."

# Pipe the prep prompt via stdin instead of passing as a shell argument.
# On Linux ARG_MAX is 2MB; the assembled prompt (config.json + top_report +
# styles + schema + candidates) busts that on the VM, dying with E2BIG
# "Argument list too long". stdin has no such cap.
PREP_OUTPUT=$(printf '%s' "$PREP_PROMPT" | "$REPO_DIR/scripts/run_claude.sh" "run-twitter-cycle-prep" --strict-mcp-config --mcp-config "$TW_MCP_CONFIG" -p --output-format json --json-schema "$PREP_SCHEMA" 2>&1)

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
# The picker assignment travels through the plan envelope so
# twitter_post_plan.py can call validate_or_register(...) with the
# original (assigned_style, assigned_mode) and coerce USE-mode drift
# back to the picker's choice (or accept the INVENT-mode invention +
# POST it to /api/v1/engagement-styles/registry). Without this, the
# post pipeline can't tell which style the picker actually assigned
# vs. what the model picked. Empty string means INVENT mode (NULL
# assigned_style in the registry-coercion contract).
json.dump({'candidates': candidates,
           'session_id': '$CLAUDE_SESSION_ID',
           'assigned_style': '$PICKED_STYLE' or None,
           'assigned_mode': '$PICKED_MODE' or 'use'}, open('$PLAN_FILE', 'w'), indent=2)
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

# Classify Anthropic-side error in the prep envelope so the dashboard
# surfaces a specific reason (monthly_limit, stream_idle_timeout, api_overloaded,
# context_overflow, etc.) rather than a silent failure when prep returns no
# plan. Empty plan with NO classified API error falls through to the historical
# "empty plan, no failure logged" branch below (salvage retries next cycle).
PREP_REASON=$(echo "$PREP_OUTPUT" | python3 "$REPO_DIR/scripts/classify_run_error.py" 2>/dev/null)

PLAN_COUNT=0
if [ "$PREP_PARSE_EXIT" -eq 0 ] && [ -f "$PLAN_FILE" ]; then
    PLAN_COUNT=$(python3 -c "import json; print(len(json.load(open('$PLAN_FILE')).get('candidates') or []))" 2>/dev/null || echo 0)
fi
log "Phase 2b-prep complete. plan_count=$PLAN_COUNT"

# Determine if Phase 2b-gen will be a no-op. When TWITTER_PAGE_GEN_RATE=0
# globally, scripts/twitter_gen_links.py rewrites the plan with plain URLs in
# <1s. In that case the release-now + re-acquire-after-gen dance is pure waste:
# under cycle overlap the re-acquire can sit in the FIFO ticket queue for
# 30-90s behind the very `engage-twitter` / next `run-twitter-cycle` we just
# handed the lock to. We keep the lock through 2b-gen instead and skip the
# dance entirely.
GEN_RATE_RAW="${TWITTER_PAGE_GEN_RATE:-0.30}"
GEN_IS_NOOP=false
case "$GEN_RATE_RAW" in
  0|0.0|0.00|0.000|"") GEN_IS_NOOP=true ;;
esac

# Release the lock unless (a) plan is non-empty AND (b) gen is a no-op. The
# empty-plan early-exit below still needs the release for a clean handoff, so
# we cannot just skip when GEN_IS_NOOP=true unconditionally.
if [ "${PLAN_COUNT:-0}" = "0" ] || ! $GEN_IS_NOOP; then
    log "Releasing twitter-browser lock (gen step is lock-free)..."
    release_lock "twitter-browser" 2>>"$LOG_FILE"
    # Defense-in-depth: clear the twitter_browser.py process lockfile; see Phase 1 note.
    rm -f "$HOME/.claude/twitter-browser-lock.json"
else
    log "Keeping twitter-browser lock through Phase 2b-gen (TWITTER_PAGE_GEN_RATE=$GEN_RATE_RAW, gen is a no-op; skipping release/re-acquire dance)"
fi

if [ "${PLAN_COUNT:-0}" = "0" ]; then
    log "Empty plan from prep step. Exiting cycle without posting (pending rows salvaged next cycle)."
    rm -f "$PLAN_FILE"
    _COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START" --scripts "run-twitter-cycle-scan" "run-twitter-cycle-prep" "run-twitter-cycle-post" 2>/dev/null || echo "0.0000")
    # If the classifier identified a real Anthropic error (any non-empty reason
    # key), log as failed=1 with that reason so the dashboard pill reads
    # "failed: stream_idle_timeout" / "failed: monthly_limit" / etc. Otherwise
    # keep the historical failed=0 behaviour for "empty plan, no API error"
    # (salvage retries the candidates next cycle, nothing to surface).
    if [ -n "$PREP_REASON" ]; then
        python3 "$REPO_DIR/scripts/log_run.py" --script "post_twitter" --posted 0 --skipped "${CANDIDATE_COUNT:-0}" --failed 1 --salvaged "${SALVAGED:-0}" \
            --queries "${QUERIES_TOTAL:-0}" --duds "${DUDS_TOTAL:-0}" \
            --tweets-pulled "${TWEETS_PULLED:-0}" --candidates "${BATCH_COUNT:-0}" --above-floor "${HIGH_DELTA_COUNT:-0}" \
            --failure-reasons "${PREP_REASON}:1" --cost "$_COST" --elapsed $(( $(date +%s) - RUN_START ))
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
# phase2b-gen has the longest budget (60 min) because the SEO landing-page
# build can legitimately run 10-40 min. Stamping it here is what protects
# this cycle from being salvaged out from under itself.
python3 "$REPO_DIR/scripts/twitter_batch_phase.py" advance "$BATCH_ID" --phase phase2b-gen 2>&1 | tee -a "$LOG_FILE" || true
log "Phase 2b-gen: generating SEO pages for $PLAN_COUNT candidate(s) without holding the browser lock..."
python3 "$REPO_DIR/scripts/twitter_gen_links.py" --plan "$PLAN_FILE" 2>&1 | tee -a "$LOG_FILE"
GEN_EXIT=${PIPESTATUS[0]:-1}
if [ "$GEN_EXIT" -ne 0 ]; then
    log "WARN: twitter_gen_links.py exited $GEN_EXIT, continuing with whatever links it set (per-candidate fallback to plain project URL on gen failure)."
fi

# --- Phase 2b-post: re-acquire browser lock and post ------------------------
# Stamp phase2b-post (15-min budget) before the browser-side reply loop. After
# 2b-gen's potentially long run, peer cycles' 20-min phase2a fallback would
# already be tripping if we left the row at phase2a.
python3 "$REPO_DIR/scripts/twitter_batch_phase.py" advance "$BATCH_ID" --phase phase2b-post 2>&1 | tee -a "$LOG_FILE" || true
# Re-acquire only if we actually released for gen (see GEN_IS_NOOP above).
# When the lock was kept through 2b-gen there's nothing to re-acquire.
if ! $GEN_IS_NOOP; then
    log "Re-acquiring twitter-browser lock for Phase 2b-post..."
    acquire_lock "twitter-browser" 3600 2>>"$LOG_FILE"
fi
log "twitter-browser lock held (pid=$$) Phase 2b-post"
# Drop stale singleton locks (see clean_stale_singleton.sh, also called in Phase 1 / 2b-prep).
ensure_twitter_browser_for_backend 2>&1 | tee -a "$LOG_FILE"

log "Phase 2b-post: posting $PLAN_COUNT candidate(s)..."
POST_OUTPUT=$(python3 "$REPO_DIR/scripts/twitter_post_plan.py" --plan "$PLAN_FILE" 2>&1)
echo "$POST_OUTPUT" >> "$LOG_FILE"

# The post helper prints a JSON summary on its last stdout line.
POST_SUMMARY=$(printf '%s\n' "$POST_OUTPUT" | tail -n 1)
EXEC_POSTED=$(python3 -c "import json,sys; d=json.loads(sys.argv[1] or '{}'); print(d.get('posted', 0))" "$POST_SUMMARY" 2>/dev/null || echo 0)
EXEC_SKIPPED=$(python3 -c "import json,sys; d=json.loads(sys.argv[1] or '{}'); print(d.get('skipped', 0))" "$POST_SUMMARY" 2>/dev/null || echo 0)
EXEC_FAILED=$(python3 -c "import json,sys; d=json.loads(sys.argv[1] or '{}'); print(d.get('failed', 0))" "$POST_SUMMARY" 2>/dev/null || echo 0)
EXEC_REASONS=$(python3 -c "import json,sys; d=json.loads(sys.argv[1] or '{}'); print(d.get('failure_reasons', ''))" "$POST_SUMMARY" 2>/dev/null || echo "")
EXEC_SKIP_REASONS=$(python3 -c "import json,sys; d=json.loads(sys.argv[1] or '{}'); print(d.get('skip_reasons', ''))" "$POST_SUMMARY" 2>/dev/null || echo "")
log "Phase 2b-post summary: posted=$EXEC_POSTED skipped=$EXEC_SKIPPED failed=$EXEC_FAILED reasons=$EXEC_REASONS skip_reasons=$EXEC_SKIP_REASONS"

rm -f "$PLAN_FILE"

# Generation trace tempfile cleanup. By now every post in this cycle that
# made it to log_post.py has the trace persisted to posts.generation_trace
# JSONB, so the on-disk JSON is redundant. Best-effort delete.
if [ -n "$SAPS_TWITTER_GEN_TRACE_PATH" ] && [ -f "$SAPS_TWITTER_GEN_TRACE_PATH" ]; then
    rm -f "$SAPS_TWITTER_GEN_TRACE_PATH"
fi

# --- No end-of-cycle expire ------------------------------------------------
# Pending rows are intentionally left alone. They are either:
#   - candidates Phase 2b never reached (e.g., org quota, browser crash, or
#     a phase budget elapsing before the long tail was reviewed), and the
#     next cycle's Phase 0 will salvage them while still fresh
#   - hard-expired by the next cycle's Phase 0 once they cross FRESHNESS_HOURS
# This avoids losing work to transient infra failures.

# --- Summary ---------------------------------------------------------------
# Per-run-log human readout. The persistent run_monitor.log row is written
# by _sa_emit_run_summary_oneshot (defined near the top of this script) so
# SIGTERM during the summary block still produces a dashboard-visible row.
# Summary now comes from /api/v1/twitter-candidates/counts-by-batch via
# the helper, formatted as "status|count\nstatus|count..." to match the
# legacy psql -F '|' shape this log line consumed.
SUMMARY=$(python3 "$REPO_DIR/scripts/twitter_cycle_helper.py" batch-summary --batch-id "$BATCH_ID" 2>/dev/null)
log "Batch summary: $SUMMARY"

_sa_emit_run_summary_oneshot

log "=== Cycle complete: $(date) ==="
find "$LOG_DIR" -name "twitter-cycle-*.log" -mtime +7 -delete 2>/dev/null || true
