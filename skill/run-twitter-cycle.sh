#!/bin/bash
# run-twitter-cycle.sh — Combined Twitter scan + post cycle.
#
# Phase 1 (t=0):
#   - select 8 projects via the shared inverse-recent-share picker
#     (scripts/pick_project.py, same logic as github/reddit)
#   - LLM drafts a search query per project (style from past top queries);
#     if Phase 1 yields <RETRY_TARGET candidates that pass all filters
#     (harness age gate + scorer dedupe + already-posted), the scan is
#     re-invoked with the previously-tried queries injected as "do NOT
#     repeat" — up to MAX_SCAN_ATTEMPTS total per cycle, same batch_id.
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

from pick_search_topic import pick_topic_for_project, PickerError

chosen = []
for p in picked:
    try:
        excludes = pe.active_excludes('twitter', p.get('name'))
    except Exception:
        excludes = []
    # 2026-05-26: force-pick ONE search_topic per project via the Python
    # picker so end-to-end attribution (topic -> query -> candidate ->
    # post -> click) is clean. Mirrors the engagement_styles flow.
    #
    # Single mode (post-2026-05-28): picker returns search_topic=<string>,
    # weighted-random over the FULL universe with log-smoothed weights
    # (top ~20-30%, cold ~0.5-1%). Claude must use the assigned topic
    # verbatim. EXPLORE_INVENT was removed in favor of the standalone
    # invent_topics.py job that writes new topics directly into
    # project_search_topics outside the cycle.
    #
    # 2026-05-27: NO fallback. The DB is the only source of truth for
    # the universe. If pick_topic_for_project raises (DB unreachable or
    # zero active topics for this project), let the heredoc crash so
    # PROJECTS_JSON is empty, the bash trap fires, and launchd records
    # a hard failure. Silent fallback to config.json or to the first
    # legacy search_topics[] entry would post against a stale seed list
    # and corrupt attribution; the rule is "stop the pipeline".
    topic_pick = pick_topic_for_project(p.get('name'), platform='twitter')
    picked_topic = topic_pick.get('search_topic')
    reference_topics = topic_pick.get('reference_topics') or []
    picked_weight_pct = topic_pick.get('picked_weight_pct')
    chosen.append({
        'name': p.get('name'),
        'description': p.get('description', ''),
        # Force-picked single topic (2026-05-26). Replaces the legacy
        # `search_topics: [...]` array. Claude draws its query from THIS
        # topic and must echo it verbatim on every tweet object via the
        # bh_run scrape script's `search_topic` Python variable.
        'search_topic': picked_topic,
        'picked_weight_pct': picked_weight_pct,
        # Per-project pool stats (top by composite_score). Surfaced as
        # context to help Claude understand the topic's history.
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
HOOK_NOTICE='STUB ENFORCEMENT — your `mcp__twitter-harness__bh_run` script is hard-pinned by a PreToolUse hook (TWITTER_SCAN_ENFORCE=1) to the canonical `twitter_scan.scan(...)` stub shown in Step 2. The hook REJECTS or REWRITES any deviation: any `goto_url`, `js()`, `new_tab`, URL construction, or scrape logic you try to add gets replaced with the canonical stub before it runs. Freshness is enforced inside scan() itself — it strips any since/until/since_time/until_time from your query, then force-appends `since_time:<freshness_hours-ago-epoch>` to the URL on the Latest tab. Do NOT add date operators to your query (they will be stripped). Do NOT try to scrape outside scan() (the script will be denied). Do NOT investigate empty results — they mean no fresh supply exists in the freshness window; drop the min_faves floor per the zero-result rule below and re-run that one query.'

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
## Step 2: Search and extract, RUN EXACTLY THIS STUB, NO IMPROVEMENTS

For EACH project query you drafted, make ONE call to `mcp__twitter-harness__bh_run` with the Python body below. The body is a thin stub that calls the operator-owned `twitter_scan.scan` function. DO NOT write `goto_url`, `js(...)`, `new_tab`, URL construction, scrape loops, or age-gate logic yourself; the operator owns ALL of that inside scan(). The PreToolUse hook is in enforcement mode and WILL REJECT or REWRITE any bh_run script that is not this stub.

Your ONLY freedom is the three quoted keyword arguments. Substitute:
- `query`: the query string you drafted for this project (with operators).
- `project`: the project name (e.g. studyly, Podlog, S4L).
- `search_topic`: the project's ASSIGNED `search_topic` field, pasted VERBATIM. End-to-end attribution joins on this string; do NOT set it equal to the query, do NOT paraphrase the topic, do NOT lowercase or strip operators from it.

`freshness_hours` and `skip_ids` are pre-filled with cycle-correct values; leave them as-is.

```python
import sys
sys.path.insert(0, "/Users/matthewdi/social-autoposter/scripts")
from twitter_scan import scan
scan(
    query="YOUR DRAFTED QUERY HERE WITH OPERATORS",
    project="PROJECT_NAME",
    search_topic="PASTE THE PROJECT'S ASSIGNED search_topic VERBATIM",
    freshness_hours=___FRESHNESS_HOURS___,
    skip_ids=___ENGAGED_IDS___,
)
```

What scan() does for you (so you understand the contract; you do not write any of this):
- Builds the URL with `&f=live` (Latest tab forced; you cannot pick Top).
- Strips any since/until/since_time/until_time from your query string.
- Force-appends `since_time:<now - freshness_hours*3600>` to the URL.
- Navigates the harness Chrome (reuses an existing real tab or opens one).
- Scrapes up to 8 article cards with the same JS the legacy template used.
- Drops tweets older than `freshness_hours` and any tweet ID in `skip_ids`.
- Stamps `search_topic`, `matched_project`, `query` on every kept tweet.
- Prints the kept tweets as JSON between `###TWEETS_BEGIN###` and `###TWEETS_END###` sentinels.

Output rules:

1. The cycle shell reads scan() output directly from a sidecar file (env `SCAN_TWEETS_FILE`); you do NOT need to read or parse the sentinel-framed JSON scan() prints to stdout. Each bh_run call appends one JSONL record there; the shell aggregates them across all calls in this attempt.

2. Your final `structured_output` only needs to satisfy the schema. Emit `tweets: []` and `queries_used: []` as empty placeholders. The shell uses the sidecar file as ground truth and ignores your arrays unless the sidecar is empty (e.g. every bh_run was denied by the stub-enforcement hook).

3. NEVER make more than one bh_run call per project under normal operation. The only exception: a bh_run that returned a Python traceback (not an empty list) may be retried ONCE with the IDENTICAL script body.
HARNESS_STEP2_EOF

# Substitute cycle-resolved values for the stub's freshness/skip placeholders.
# Single-quoted heredoc above prevents shell expansion, so we do it here against
# the captured variable. Bash 3.2 supports ${var//search/replace}.
STEP2_INSTRUCTIONS="${STEP2_INSTRUCTIONS//___FRESHNESS_HOURS___/$FRESHNESS_HOURS_DISCOVER}"
STEP2_INSTRUCTIONS="${STEP2_INSTRUCTIONS//___ENGAGED_IDS___/$ENGAGED_TWEET_IDS}"


# --- Phase 1: Claude drafts queries, scrapes tweets -------------------------
# JSON schema forces structured output. Eliminates the prose-drift failure mode
# where the scanner summarized instead of dumping the JSON array.
SCAN_SCHEMA='{"type":"object","properties":{"tweets":{"type":"array","items":{"type":"object","properties":{"handle":{"type":"string"},"text":{"type":"string"},"tweetUrl":{"type":"string"},"datetime":{"type":"string"},"replies":{"type":"integer"},"retweets":{"type":"integer"},"likes":{"type":"integer"},"views":{"type":"integer"},"bookmarks":{"type":"integer"},"search_topic":{"type":"string"},"matched_project":{"type":"string"},"query":{"type":"string"}},"required":["handle","text","tweetUrl","datetime","replies","retweets","likes","views","bookmarks","search_topic","matched_project","query"]}},"queries_used":{"type":"array","items":{"type":"object","properties":{"query":{"type":"string"},"project":{"type":"string"},"tweets_found":{"type":"integer"},"search_topic":{"type":"string"}},"required":["query","project","tweets_found","search_topic"]}}},"required":["tweets","queries_used"]}'

# Lean Phase 1 schema (2026-05-28): the scan session no longer scrapes,
# it only drafts queries. The Python pipeline runs each query via headless
# Chrome and writes the tweets directly to SCAN_TWEETS_FILE for the shell.
SCAN_SCHEMA_LEAN='{"type":"object","properties":{"queries":{"type":"array","items":{"type":"object","properties":{"project":{"type":"string"},"query":{"type":"string"},"search_topic":{"type":"string"}},"required":["project","query","search_topic"]}}},"required":["queries"]}'

log "Acquiring twitter-browser lock for Phase 1 Claude scan..."
acquire_lock "twitter-browser" 3600 2>>"$LOG_FILE"
log "twitter-browser lock held (pid=$$) Phase 1"
# Drop stale Chrome singleton symlinks before launch. Background ungraceful-
# exits (SIGKILL, jetsam, force quit) leave Singleton{Lock,Cookie,Socket}
# pointing at dead PIDs / vanished sockets; without this, Chrome pops "Something
# went wrong when opening your profile" 7x and the pipeline hangs. Helper
# refuses to clean if the lock PID is alive.
ensure_twitter_browser_for_backend 2>&1 | tee -a "$LOG_FILE"

# --- Phase 1 retry loop (2026-05-27) ----------------------------------------
# When a single scan produces fewer than RETRY_TARGET candidates that survive
# all Phase 1 filters (harness age gate, scorer stale_age cutoff, already-
# posted dedupe, fabricated_id check), re-invoke the Claude scan with the
# queries already tried this cycle injected as a "do NOT repeat" block.
# Each iteration upserts into the SAME batch_id so survivors accumulate.
# Cap at MAX_SCAN_ATTEMPTS to stay inside the 20-min Phase 1 budget; if the
# cap is hit before target, proceed with whatever we have (even 1 candidate
# is better than 0). When BATCH_COUNT is still 0 after the loop, the
# post-loop empty_batch branch fires.
MAX_SCAN_ATTEMPTS=5
RETRY_TARGET=5
SCAN_ATTEMPT=0
BATCH_COUNT=0
# Cumulative counters across iterations — feed log_run.py once at end so the
# dashboard shows the total work the cycle did, not just the last attempt.
CUMULATIVE_QUERIES=0
CUMULATIVE_DUDS=0
CUMULATIVE_TWEETS_PULLED=0
# Running list of queries the model has already tried THIS cycle, injected
# into each retry's prompt as "do NOT repeat these phrasings". Extended after
# every scan from QUERIES_FILE before log_twitter_search_attempts deletes it.
TRIED_QUERIES_JSON='[]'
# Running list of SEARCH TOPICS already tried this cycle. Each retry calls
# pick_topic_for_project again with this list as exclude_topics so the model
# isn't pinned to one assigned topic for all 5 attempts. When the filtered
# universe empties (small-project case), the picker raises
# UniverseExhaustedError and the retry loop breaks — no invent fallback
# (invention is the standalone invent_topics.py job's responsibility).
TRIED_TOPICS_JSON='[]'
# Latest Anthropic-side error classification for the post-loop log_run when
# every attempt returned zero tweets (stream_idle_timeout vs phase1_no_tweets
# vs api_overloaded, etc.). Falls back to phase1_no_tweets when unset.
LAST_PHASE1_REASON=""
# Set to 1 by the in-loop repick when pick_search_topic raises
# UniverseExhaustedError (the project ran out of un-tried active topics).
# Used by the post-loop empty-batch branch to emit `universe_exhausted:1`
# instead of `empty_batch:1` so the dashboard shows the right cause.
UNIVERSE_EXHAUSTED=0

while [ "$SCAN_ATTEMPT" -lt "$MAX_SCAN_ATTEMPTS" ]; do
SCAN_ATTEMPT=$((SCAN_ATTEMPT + 1))

# --- Per-attempt topic (re)pick (2026-05-27) ---------------------------------
# Attempt 1 keeps the pre-loop topic that PROJECTS_JSON already carries.
# Attempts 2+ call pick_topic_for_project again with TRIED_TOPICS_JSON as
# exclude_topics, then rewrite PROJECTS_JSON in place with the new topic and
# its reference_topics. This makes the retry genuinely end-to-end programmatic:
# new topic -> new query -> new tweets, not just "model rephrases the same
# assigned topic 5 times". When the project's filtered universe empties
# (small project, all topics tried this cycle), the picker raises
# UniverseExhaustedError and the shell breaks the retry loop cleanly
# (post-2026-05-28: no invent fallback; invention lives in invent_topics.py).
if [ "$SCAN_ATTEMPT" -gt 1 ]; then
    log "Phase 1 attempt $SCAN_ATTEMPT: re-picking search_topic via pick_topic_for_project (exclude=$(echo "$TRIED_TOPICS_JSON" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))' 2>/dev/null || echo 0) tried)..."
    # The exhaustion marker file is the cross-boundary signal back to
    # bash: when pick_topic_for_project raises UniverseExhaustedError for
    # ANY selected project, the Python writes this file and the shell
    # breaks the retry loop after the heredoc returns. No invent fallback
    # (2026-05-28 architecture: invention is the standalone
    # invent_topics.py job's responsibility, not the cycle's).
    UNIVERSE_EXHAUSTED_MARKER="/tmp/twitter_cycle_universe_exhausted_${BATCH_ID}"
    rm -f "$UNIVERSE_EXHAUSTED_MARKER"
    PROJECTS_JSON=$(python3 - "$PROJECTS_JSON" "$TRIED_TOPICS_JSON" "$UNIVERSE_EXHAUSTED_MARKER" <<'PY' 2>>"$LOG_FILE"
import json, os, sys
sys.path.insert(0, os.path.expanduser('~/social-autoposter/scripts'))
from pick_search_topic import pick_topic_for_project, PickerError, UniverseExhaustedError

projects = json.loads(sys.argv[1] or '[]')
excluded = json.loads(sys.argv[2] or '[]')
marker_path = sys.argv[3]
exhausted_for = []
for p in projects:
    name = p.get('name')
    if not name:
        continue
    try:
        new_pick = pick_topic_for_project(
            name, platform='twitter', exclude_topics=excluded,
        )
    except UniverseExhaustedError as exc:
        # All active topics for this project already tried this cycle.
        # Stamp the marker file so the shell breaks the retry loop and
        # logs `universe_exhausted:1` as the failure reason. Leave the
        # project entry as-is (the loop will exit before scanning anyway).
        sys.stderr.write(f"repick_universe_exhausted project={name!r} error={exc}\n")
        exhausted_for.append(name)
        continue
    except PickerError as exc:
        # On repick PickerError (DB unreachable, etc) keep the previous
        # topic; the scan will still run. Strictly better than aborting.
        sys.stderr.write(f"repick_failed project={name!r} error={exc}\n")
        continue
    p['search_topic'] = new_pick.get('search_topic')
    p['picked_weight_pct'] = new_pick.get('picked_weight_pct')
    p['reference_topics'] = new_pick.get('reference_topics') or []
if exhausted_for:
    with open(marker_path, 'w') as fh:
        fh.write(','.join(exhausted_for) + '\n')
print(json.dumps(projects))
PY
)
if [ -f "$UNIVERSE_EXHAUSTED_MARKER" ]; then
    UNIVERSE_EXHAUSTED=1
    _EXH_PROJECTS=$(cat "$UNIVERSE_EXHAUSTED_MARKER" 2>/dev/null | tr -d '\n')
    log "  Universe exhausted for project(s)=$_EXH_PROJECTS after $((SCAN_ATTEMPT - 1)) prior attempt(s); breaking retry loop"
    rm -f "$UNIVERSE_EXHAUSTED_MARKER"
    break
fi
fi

# Snapshot this attempt's topic(s) into TRIED_TOPICS_JSON so the NEXT
# iteration's repick excludes them. Runs every attempt (incl. attempt 1)
# so the initial pre-loop topic also goes into the exclude list before
# attempt 2's repick. Idempotent: same topic added twice is a no-op.
TRIED_TOPICS_JSON=$(python3 - "$TRIED_TOPICS_JSON" "$PROJECTS_JSON" <<'PY' 2>>"$LOG_FILE"
import json, sys
cur = json.loads(sys.argv[1] or '[]')
projects = json.loads(sys.argv[2] or '[]')
seen = {(t or '').strip().lower() for t in cur if t}
for p in projects:
    t = (p.get('search_topic') or '').strip()
    if t and t.lower() not in seen:
        cur.append(t)
        seen.add(t.lower())
print(json.dumps(cur))
PY
)

_CURRENT_TOPICS=$(echo "$PROJECTS_JSON" | python3 -c 'import json,sys; ps=json.load(sys.stdin); print(", ".join((p.get("search_topic") or "?") for p in ps))' 2>/dev/null || echo "")
log "Phase 1 scan attempt $SCAN_ATTEMPT/$MAX_SCAN_ATTEMPTS (batch=$BATCH_ID, candidates so far=$BATCH_COUNT/$RETRY_TARGET, topic(s)=$_CURRENT_TOPICS)"

log "Phase 1: drafting queries and scraping tweets..."

# Shell-side data path. scripts/twitter_scan.scan() appends one JSONL record
# per call to this file. After the claude scan session ends we parse it
# directly into $RAW_FILE and $QUERIES_FILE, bypassing the model's
# structured_output relay so the model no longer pays per-tweet copy tokens.
# One file per Phase 1 attempt so retry iterations do not share state. The
# rm -f makes each attempt's accumulation start clean. Falls back to the
# structured_output parse below when the file is empty (e.g. every bh_run was
# denied by the stub-enforcement hook so scan() never executed).
SCAN_TWEETS_FILE="/tmp/twcycle-${BATCH_ID}-attempt-${SCAN_ATTEMPT}-tweets.jsonl"
rm -f "$SCAN_TWEETS_FILE"
export SCAN_TWEETS_FILE

# === LEAN PHASE 1 (2026-05-28) =============================================
# Replaces the model-driven Twitter scrape with: small Claude call that only
# DRAFTS queries (no tools, no MCP, no browser), then a Python loop that runs
# each query via the operator-owned twitter_scan.scan() function over the same
# CDP daemon. Cuts per-cycle scan cost roughly 10x by removing:
#   - MCP bh_run tool roundtrips
#   - structured_output tweet relay (was hundreds of tweet objects)
#   - draft-deny-retry churn (model used to try inline scrapes and bounce off
#     the PreToolUse stub-enforcement hook every session)
# Output downstream is identical: $RAW_FILE + $QUERIES_FILE feed the scorer
# and twitter_search_attempts logger the same way as before.
#
# Dead under this flow (still in the file as scaffolding for rollback):
#   - $STEP2_INSTRUCTIONS (bh_run stub instruction)
#   - $HOOK_NOTICE / $ANTI_DEBUG_RULE (no browser session for the model)
#   - $SCAN_SCHEMA (the old tweets+queries_used schema)
#   - TWITTER_SCAN_ENFORCE=1 env (no bh_run from this rail)
#   - The PreToolUse stub-enforcement code in twitter-search-since-rewrite.py
#     (still wired but never fires for the scan session)

log "Lean Phase 1: drafting queries (no browser tools)..."

QUERIES_OUTPUT=$("$REPO_DIR/scripts/run_claude.sh" "run-twitter-cycle-queries" -p --output-format json --json-schema "$SCAN_SCHEMA_LEAN" "${TW_ENGINE_PREFIX}You are a Twitter query drafter. Your ONLY job is to draft fresh X advanced-search queries that surface tweets relevant to our projects. You do NOT post, you do NOT call any tools, you do NOT scrape. A separate Python pipeline runs your queries over the same CDP-driven Chrome and applies a strict freshness gate; you only return the query strings.

## Step 1: Draft one search query per project

You have $(echo "$PROJECTS_JSON" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))') projects. Draft exactly ONE Twitter search query for each, tailored to that project's ASSIGNED search_topic.

Each project entry carries TWO fields that drive your behavior: \`topic_picked_mode\` (either \`use\` or \`explore_invent\`) and \`search_topic\` (a string in \`use\` mode, NULL in \`explore_invent\` mode).

USE mode (~90% of cycles, indicated by \`topic_picked_mode: "use"\` and a non-null \`search_topic\`):
The Python picker has already chosen this project's search_topic by weighted-random sampling over the FULL universe in config.json. Your job is to translate that ASSIGNED topic into the best Twitter advanced-search query that will surface fresh, on-topic tweets. Do NOT substitute a different topic; do NOT paraphrase the topic. End-to-end attribution joins on the exact string.

EXPLORE_INVENT mode (~10% of cycles, indicated by \`topic_picked_mode: "explore_invent"\` and \`search_topic: null\`):
The picker is asking you to INVENT a brand-new search_topic. Look at the project's own \`reference_topics\` array and propose ONE new topic concept that does NOT appear there and is NOT a paraphrase of anything in it. Use your invented topic as the query's \`search_topic\` AND drive the keyword phrasing from it (one consistent string per project).

Projects:
$PROJECTS_JSON

Top past queries FOR THE PROJECT YOU'RE DRAFTING FOR (per-project, sorted by clicks DESC first, then composite-scored: clicks×100 + likes + views×0.001). CLICKS ARE THE PRIORITY SIGNAL. Use the structure of YOUR project's gold-tier queries (operators, keyword density, query length) as inspiration; the canonical source for \`min_faves:N\` selection is the PER-PROJECT SUPPLY SIGNAL block below.
$TOP_QUERIES_PER_PROJECT_JSON

TOP-PERFORMING SEARCH TOPICS (conceptual seeds, 14d window) — context for query phrasing only; you draft a query for the picker-assigned topic, you do NOT swap topics here:
$TOP_TOPICS_JSON

DUD QUERIES — DO NOT REUSE these phrasings or close variants. They returned ZERO tweets in the last 48h:
$DUD_QUERIES_JSON

DUD CONCEPT SEEDS — these search_topic seeds pulled in tweets that Phase 2b's draft gate kept skipping over the last 7d. Per entry: \`omit_rate\` = skipped_n / (posted_n + skipped_n), \`sample_skip_reasons\` are the top reject reasons. If \`omit_rate >= 0.6\` AND \`skipped_n >= 5\`, REWORD the query narrower or drop the seed and pick a different config.json seed for that project:
$DUD_TOPICS_JSON

PER-PROJECT SUPPLY SIGNAL — for each project, the historical median tweets_found at each \`min_faves:N\` tier you've drafted in the last 14d. Pick the LOWEST tier where \`median_tweets_found >= 3\`; if every tier is below 3, drop one tier lower than the lowest you've tried. Trust this table over priors:
$SUPPLY_SIGNAL_JSON

ALREADY-ENGAGED TWEET IDS (last 48h) — the Python scraper skips these regardless, but knowing them helps you avoid drafting a query that would predominantly surface dead candidates:
$ENGAGED_TWEET_IDS

THIS-CYCLE QUERIES ALREADY TRIED (attempt $SCAN_ATTEMPT/$MAX_SCAN_ATTEMPTS, target=$RETRY_TARGET candidates after filters) — do NOT repeat any of these phrasings or close variants. If non-empty, prior attempts returned too few survivors after the freshness gate + scorer dedupe; broaden, switch operator set, or drop a tier of min_faves:
$TRIED_QUERIES_JSON

Query guidelines:
- MANDATORY: every query MUST end with the operator \`since_time:\$(( \$(date +%s) - FRESHNESS_HOURS_DISCOVER * 3600 ))\` copied EXACTLY as written. It is a pre-computed Unix-epoch timestamp; do NOT recalculate or reformat. The Python scraper additionally strips and re-injects this operator at the URL level (so even if you drop it, freshness is enforced); but matching it here keeps the query strings honest in the dashboard.
- MANDATORY EVEN IF YOUR QUERY KEYWORDS DO NOT NAME THE EXCLUDED TOPIC: if a project's \`excludes_for_search\` array is non-empty, append \`-term\` for EVERY listed term to that project's query, verbatim, no exceptions.
- MANDATORY: pick \`min_faves:N\` per the PER-PROJECT SUPPLY SIGNAL above. If a project has no entry there (new / first cycle), start at min_faves:20.
- Favor discussions/opinions (people sharing experience, asking questions), not news/promos/giveaways.
- Pick a query likely to surface tweets RELEVANT to that project's actual domain.
- Mix it up each run; don't always use the same query for the same project.
- Use the project's ASSIGNED \`search_topic\` plus its \`description\` as grounding for query phrasing.
- The \`search_topic\` you emit in the output JSON MUST be the project's assigned \`search_topic\` field pasted VERBATIM (NOT the query string, NOT a paraphrase). The scoring pipeline stamps \`twitter_candidates.search_topic\` from this for end-to-end attribution.

## Output

Return ONLY the structured_output JSON:
{"queries": [{"project": "<project_name>", "query": "<X advanced-search string>", "search_topic": "<assigned or invented topic, verbatim>"}, ...]}

One entry per project. Do NOT include tweets, do NOT include tweets_found, do NOT call any tool, do NOT scrape. The shell pipeline runs each query via headless Chrome with a strict freshness gate after you return." 2>&1)

# Dump the captured envelope to the cycle log for offline inspection.
echo "$QUERIES_OUTPUT" >> "$LOG_FILE"

# Extract the drafted queries to a temp file.
QUERIES_TMP="/tmp/twcycle-${BATCH_ID}-attempt-${SCAN_ATTEMPT}-queries.json"
python3 -c "
import json, sys
text = sys.stdin.read().strip()
try:
    env, _ = json.JSONDecoder().raw_decode(text)
except Exception as e:
    print(f'lean phase 1: envelope parse error: {e}', file=sys.stderr)
    json.dump([], open('$QUERIES_TMP', 'w'))
    sys.exit(0)
so = env.get('structured_output')
if so is None:
    so = env.get('result')
if isinstance(so, str):
    try: so = json.loads(so)
    except Exception: pass
qs = so.get('queries', []) if isinstance(so, dict) else []
json.dump(qs, open('$QUERIES_TMP', 'w'))
print(f'lean phase 1: drafted {len(qs)} queries', flush=True)
" <<< "$QUERIES_OUTPUT" 2>&1 | tee -a "$LOG_FILE"

QUERIES_COUNT=$(python3 -c "
import json
try: print(len(json.load(open('$QUERIES_TMP'))))
except Exception: print(0)
" 2>/dev/null || echo 0)

# Loop: for each drafted query, run scan() over the same browser-harness daemon
# the cycle already keeps alive (port 9555, BU_NAME=twitter-harness). One
# browser-harness invocation handles the full loop so we don't pay the CLI
# startup cost N times. Each scan() call appends one JSONL record to
# $SCAN_TWEETS_FILE, which the existing shell-side parse below consumes.
if [ "$QUERIES_COUNT" -gt 0 ]; then
    log "Lean Phase 1: executing $QUERIES_COUNT queries via browser-harness CDP"
    BU_NAME=twitter-harness BU_CDP_URL=http://127.0.0.1:9555 \
    SCAN_TWEETS_FILE="$SCAN_TWEETS_FILE" \
    BATCH_ID="$BATCH_ID" \
    TWITTER_CYCLE_VARIANT="$TWITTER_CYCLE_VARIANT" \
    FRESHNESS_HOURS_DISCOVER="$FRESHNESS_HOURS_DISCOVER" \
    ENGAGED_TWEET_IDS="$ENGAGED_TWEET_IDS" \
        "$HOME/.local/bin/browser-harness" -c "
import sys, json, os, time
sys.path.insert(0, '/Users/matthewdi/social-autoposter/scripts')
from twitter_scan import scan
queries = json.load(open('$QUERIES_TMP'))
freshness = int(os.environ.get('FRESHNESS_HOURS_DISCOVER', '6'))
skip_ids = json.loads(os.environ.get('ENGAGED_TWEET_IDS', '[]'))
for q in queries:
    project = q.get('project', '')
    query = q.get('query', '')
    topic = q.get('search_topic', '')
    t0 = time.time()
    try:
        kept = scan(
            query=query,
            project=project,
            search_topic=topic,
            freshness_hours=freshness,
            skip_ids=skip_ids,
        )
        dt = time.time() - t0
        print(f'  ok  project={project!r}  q={query[:50]!r}  kept={len(kept)}  in {dt:.1f}s', flush=True)
    except Exception as e:
        dt = time.time() - t0
        print(f'  err project={project!r}  q={query[:50]!r}  in {dt:.1f}s  {type(e).__name__}: {e}', flush=True)
" 2>&1 | tee -a "$LOG_FILE"
fi
rm -f "$QUERIES_TMP"

# Shell-side parse of $SCAN_TWEETS_FILE -> $RAW_FILE + $QUERIES_FILE. Identical
# to the prior shell-side branch; the structured_output fallback is no longer
# wired because the lean flow always produces SCAN_TWEETS_FILE (scan() writes
# even on zero-tweet calls). If SCAN_TWEETS_FILE is missing entirely (e.g. the
# Claude call returned no queries), write empty arrays so downstream scoring
# treats this attempt as a zero-result Phase 1 and the retry loop fires.
if [ -s "$SCAN_TWEETS_FILE" ]; then
    log "Parsing tweets from $SCAN_TWEETS_FILE"
    python3 -c "
import json, sys
recs = []
for ln in open('$SCAN_TWEETS_FILE'):
    ln = ln.strip()
    if not ln:
        continue
    try:
        recs.append(json.loads(ln))
    except json.JSONDecodeError:
        print(f'shell-side: skipping bad JSONL line', file=sys.stderr)
tweets = []
queries_used = []
for r in recs:
    ts = r.get('tweets') or []
    tweets.extend(ts)
    queries_used.append({
        'query': r.get('query', ''),
        'project': r.get('project', ''),
        'tweets_found': len(ts),
        'search_topic': r.get('search_topic', ''),
    })
json.dump(queries_used, open('$QUERIES_FILE', 'w'))
json.dump(tweets, open('$RAW_FILE', 'w'))
print(f'shell-side parse: {len(tweets)} tweets, {len(queries_used)} attempts from SCAN_TWEETS_FILE', flush=True)
sys.exit(0 if tweets else 1)
" 2>&1 | tee -a "$LOG_FILE"
    EXTRACT_EXIT=${PIPESTATUS[0]:-1}
else
    log "no SCAN_TWEETS_FILE this attempt (0 queries drafted or every scrape errored)"
    : > "$QUERIES_FILE"
    : > "$RAW_FILE"
    EXTRACT_EXIT=1
fi
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

# Accumulate per-iteration counts into cycle-level totals for the post-loop
# log_run.py call (otherwise the dashboard would show only the last attempt's
# queries/duds/tweets-pulled, hiding the retry work).
CUMULATIVE_QUERIES=$((CUMULATIVE_QUERIES + QUERIES_TOTAL))
CUMULATIVE_DUDS=$((CUMULATIVE_DUDS + DUDS_TOTAL))
CUMULATIVE_TWEETS_PULLED=$((CUMULATIVE_TWEETS_PULLED + TWEETS_PULLED))

# Snapshot this iteration's queries into TRIED_QUERIES_JSON BEFORE
# log_twitter_search_attempts.py deletes QUERIES_FILE. Used in the next
# iteration's prompt to tell the model which phrasings it has already burned.
if [ -f "$QUERIES_FILE" ]; then
    TRIED_QUERIES_JSON=$(python3 -c "
import json, sys
cur = json.loads(sys.argv[1] or '[]')
try:
    new = json.load(open(sys.argv[2]))
    if not isinstance(new, list):
        new = []
except Exception:
    new = []
cur.extend(new)
print(json.dumps(cur))
" "$TRIED_QUERIES_JSON" "$QUERIES_FILE" 2>/dev/null || echo "$TRIED_QUERIES_JSON")
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
    # Claude returned no usable tweet array this attempt. Could be a real
    # Anthropic error (stream_idle_timeout, api_overloaded, monthly_limit,
    # context_overflow) or just "model found nothing relevant". Classify
    # the failure for the post-loop log_run summary; the loop control below
    # decides whether to retry or give up.
    PHASE1_REASON_LATEST=$(echo "$SCAN_OUTPUT" | python3 "$REPO_DIR/scripts/classify_run_error.py" 2>/dev/null)
    [ -z "$PHASE1_REASON_LATEST" ] && PHASE1_REASON_LATEST="phase1_no_tweets"
    LAST_PHASE1_REASON="$PHASE1_REASON_LATEST"
    log "  Phase 1 attempt $SCAN_ATTEMPT returned no tweets (reason=$PHASE1_REASON_LATEST); falling through to loop control"
else
    # --- Phase 1 finalize: enrich + score with T0 + batch_id ----------------
    log "Enriching via fxtwitter + scoring with T0 snapshot (batch=$BATCH_ID, attempt=$SCAN_ATTEMPT)..."
    cat "$RAW_FILE" \
        | python3 "$REPO_DIR/scripts/enrich_twitter_candidates.py" \
        | python3 "$REPO_DIR/scripts/score_twitter_candidates.py" --batch-id "$BATCH_ID" \
            ${ATTEMPTS_FILE:+--attempts "$ATTEMPTS_FILE"} \
        2>&1 | tee -a "$LOG_FILE"
    rm -f "$RAW_FILE" "$ATTEMPTS_FILE"
fi

BATCH_COUNT=$(python3 "$REPO_DIR/scripts/twitter_cycle_helper.py" batch-count --batch-id "$BATCH_ID" 2>/dev/null || echo 0)
log "Phase 1 attempt $SCAN_ATTEMPT complete. Batch has $BATCH_COUNT/$RETRY_TARGET candidates with T0 snapshot."

# --- Retry-loop control ------------------------------------------------------
# Break out if we hit the target; else either retry or give up at the cap.
if [ "$BATCH_COUNT" -ge "$RETRY_TARGET" ]; then
    log "  Reached target ($BATCH_COUNT >= $RETRY_TARGET) after $SCAN_ATTEMPT scan(s); proceeding to Phase 2"
    break
fi
if [ "$SCAN_ATTEMPT" -ge "$MAX_SCAN_ATTEMPTS" ]; then
    log "  Hit scan cap ($MAX_SCAN_ATTEMPTS); proceeding with $BATCH_COUNT candidate(s)"
    break
fi
_TRIED_N=$(echo "$TRIED_QUERIES_JSON" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))' 2>/dev/null || echo 0)
log "  Below target ($BATCH_COUNT/$RETRY_TARGET); $_TRIED_N queries tried so far this cycle; looping for attempt $((SCAN_ATTEMPT + 1))..."
done

# --- Post-loop bookkeeping ---------------------------------------------------
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

# Promote cumulative totals onto the per-iteration names so every downstream
# log_run.py / trap handler picks up the cycle-level work (not just the last
# attempt's counts). Keeps all the existing "${QUERIES_TOTAL:-0}" etc. call
# sites correct without touching them individually.
QUERIES_TOTAL="$CUMULATIVE_QUERIES"
DUDS_TOTAL="$CUMULATIVE_DUDS"
TWEETS_PULLED="$CUMULATIVE_TWEETS_PULLED"

log "Phase 1 complete after $SCAN_ATTEMPT scan attempt(s). Final batch has $BATCH_COUNT candidates with T0 snapshot."

if [ "$BATCH_COUNT" = "0" ]; then
    # Distinguish "Claude returned no tweets at all" from "Claude returned
    # tweets but enrichment dropped them all" from "we exhausted the topic
    # universe mid-retry" so the dashboard can surface the right failure
    # mode. Priority order: universe_exhausted (the picker said stop) >
    # Anthropic-side classified error > generic empty_batch.
    if [ "${UNIVERSE_EXHAUSTED:-0}" = "1" ]; then
        _FAILURE_REASON="universe_exhausted:1"
    elif [ -n "$LAST_PHASE1_REASON" ] && [ "$CUMULATIVE_TWEETS_PULLED" = "0" ]; then
        _FAILURE_REASON="${LAST_PHASE1_REASON}:1"
    else
        _FAILURE_REASON="empty_batch:1"
    fi
    log "Empty batch after $SCAN_ATTEMPT attempt(s) (reason=$_FAILURE_REASON). Nothing to re-score. Exiting."
    _COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START" --scripts "run-twitter-cycle-scan" "run-twitter-cycle-post" 2>/dev/null || echo "0.0000")
    python3 "$REPO_DIR/scripts/log_run.py" --script "post_twitter" --posted 0 --skipped 0 --failed 1 \
        --salvaged "${SALVAGED:-0}" \
        --queries "${CUMULATIVE_QUERIES:-0}" --duds "${CUMULATIVE_DUDS:-0}" \
        --tweets-pulled "${CUMULATIVE_TWEETS_PULLED:-0}" \
        --failure-reasons "$_FAILURE_REASON" \
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
