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

# 2026-05-28: launchd inherits a default open-files limit of 256 on macOS,
# which is below the threshold the claude binary needs when it loads MCP
# servers from ~/.claude.json (50+ servers, each opening a stdio pipe pair).
# Without this bump, `claude -p` exits with code 1 and ZERO bytes of output
# (no stdout, no stderr, no archive) because the fd-exhaustion crash happens
# inside Node.js startup before any handler can run. The lean Phase 1 path
# (no --strict-mcp-config) was the first thing in the cycle to hit it.
# 4096 is well above what claude + uv + helpers need; soft-fail to original
# if the kernel/account caps below this.
ulimit -n 4096 2>/dev/null || true

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

# LENGTH A/B (2026-06-01): split each cycle ~50/50 into a length-control
# experiment arm and hand it to both the draft render (engagement_styles.py
# get_assigned_style_prompt) and the post-time gate (twitter_post_plan.py) via
# LENGTH_ARM. One style block is rendered per cycle for the whole batch, so the
# arm is a per-cycle decision (per BATCH_ID), not per-post.
#   treatment = per-style target line in the prompt + post-time truncation gate
#   control   = generic "keep it tight" guidance, no per-style number, no gate
#               (reproduces pre-length-project length behavior WITHOUT reverting
#                the bug fixes shipped alongside the length project)
# Assignment is deterministic from BATCH_ID's trailing timestamp digit (parity)
# so a re-run of the same cycle id lands the same arm. Set LENGTH_AB_ENABLED=0
# to disable the experiment: every cycle then runs treatment (full length
# control behavior).
#
# DO NOT switch this back to md5/md5sum. Those binaries live in /sbin on macOS
# (and /usr/bin/md5sum-only on Linux), and /sbin is NOT on the launchd PATH
# (node/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin). On 2026-06-02 an
# md5-based split silently produced an EMPTY hash under cron, fell through the
# case to control, and stamped 173/173 control in 12h (a dead experiment that
# looked alive). tr + tail live in /usr/bin (on the launchd PATH, and present
# on both macOS and the Linux VMs), so the digit split is PATH- and platform-
# safe. Empty/garbage fails OPEN to treatment (full length behavior), never
# silently to one arm.
LENGTH_AB_ENABLED="${LENGTH_AB_ENABLED:-1}"
if [ "$LENGTH_AB_ENABLED" = "1" ]; then
  _len_digits=$(printf '%s' "$BATCH_ID" | tr -cd '0-9')
  _len_last=$(printf '%s' "$_len_digits" | tail -c 1)
  case "$_len_last" in
    [13579]) LENGTH_ARM="treatment" ;;
    [02468]) LENGTH_ARM="control" ;;
    *)       LENGTH_ARM="treatment" ;;
  esac
else
  LENGTH_ARM="treatment"
  _len_last="off"
fi
export LENGTH_ARM

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
# 2026-06-01: tightened 6h -> 2h. The pending pool had bloated to 636 rows,
# 523 of them >6h old (median virality 0.44, far below the ~5.8 posted median),
# because the salvage loop kept re-carrying stale low-virality junk. A 2h
# ceiling drops that carry runway so aged-out junk expires instead of riding
# ~80 cycles. Discovery is already capped at 1h (FRESHNESS_HOURS_DISCOVER).
FRESHNESS_HOURS=2

# ----------------------------------------------------------------------------
# EXPERIMENT CONCLUDED 2026-05-31: variant D won the ripen+freshness A/B/C/D
# test (shipped 2026-05-22, D added 2026-05-25). D = no ripen wait + 1h Phase 1
# freshness window + drop parent threads with T0 views > 2000. Over the 60-day
# window D cut thread-age-at-discover p50 to 21 min (vs 173-277 for A/B/C) and
# led on avg views (91) and avg clicks (0.45), trading post-rate for fresher,
# higher-converting replies. A/B/C logic has been ripped out; D is now the
# permanent, hardcoded behavior. The cycle_variant column is still stamped 'D'
# below so historical analytics keep a consistent label.
#
# Phase 0 hard-expire uses FRESHNESS_HOURS (the union ceiling, tightened to 2h
# on 2026-06-01, see above) so peer cycles don't accidentally expire each
# other's still-pending rows. FRESHNESS_HOURS_DISCOVER (Phase 1 prompt +
# since-rewrite hook) stays tightened to 1h, the winning D setting.
TWITTER_CYCLE_VARIANT=D
FRESHNESS_HOURS_DISCOVER=1
# Export FRESHNESS_HOURS too so score_twitter_candidates.py inherits it and
# drives the expire-stale gate from the same knob (was hardcoded 18h there).
export TWITTER_CYCLE_VARIANT FRESHNESS_HOURS_DISCOVER FRESHNESS_HOURS
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
log "Logic=D (no-ripen + 1h freshness + 2k_view_cap; experiment concluded 2026-05-31); discover_freshness=${FRESHNESS_HOURS_DISCOVER}h"
log "[length-ab] enabled=$LENGTH_AB_ENABLED arm=$LENGTH_ARM (batch=$BATCH_ID, last_digit=${_len_last:-none})"

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
# 3. Single-cycle gate: exactly 1 concurrent run-twitter-cycle.sh, enforced
#    HERE in the script itself so EVERY launch path is covered — the launchd
#    singleton wrapper's snapshot copy, the MCP draft_cycle tool's direct
#    `bash skill/run-twitter-cycle.sh`, and any manual/agent invocation all
#    run this preflight and compete for the same /tmp/sa-twitter-cycle-slot-1
#    mkdir lock. History: 2026-05-03 introduced this as a max-4 cap; on
#    2026-05-22 we added run-twitter-cycle-singleton.sh to enforce one-at-a-
#    time, but that wrapper only governs the launchd path and never kills
#    (per user instruction), so out-of-band literal launches (MCP/manual)
#    sailed past it while this cap still permitted 4. On 2026-06-01 a launchd
#    studyly cycle + an out-of-band fazm cycle overlapped, fought over the
#    twitter-browser lock, and the fazm one got watchdog-killed (logged as
#    phase2b_silent). Cutting the cap to 1 unifies the gate across all paths.
#    The slot's dead-holder GC (kill -0 in preflight.sh) still reclaims slots
#    orphaned by SIGKILL/OOM, so a crashed cycle never wedges the gate.
#
# preflight.sh exposes a small set of helpers; we call them in order
# (cheapest first) so a fast-path skip (already-blocked) doesn't even
# spend the sysctl read for the next check.
source "$REPO_DIR/scripts/preflight.sh"
SA_PREFLIGHT_SCRIPT="run-twitter-cycle"
preflight_skip_if_claude_blocked
preflight_skip_if_jetsam_pressure
preflight_acquire_slot_or_skip "twitter-cycle" 1

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
    if [ -n "${BATCH_ID:-}" ]; then
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
                --cycle-id "${BATCH_ID}" \
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

_pp_args = ['python3', os.path.join(REPO, 'scripts', 'pick_project.py'),
            '--platform', 'twitter', '--count', '1', '--json']
# Manual-mode (MCP draft_cycle) single-project scoping: when SAPS_FORCE_PROJECT
# is set, force that exact project instead of the weighted-random autopilot
# pick, so a customer's interactive cycle only ever touches their own project.
_force_project = os.environ.get('SAPS_FORCE_PROJECT')
if _force_project:
    _pp_args += ['--project', _force_project]
res = subprocess.run(
    _pp_args,
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
    rows = run_q(['--limit', '20', '--window-days', '7', '--project', name])
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


# --- Phase 1: Claude drafts queries, scrapes tweets -------------------------
# JSON schema forces structured output. Eliminates the prose-drift failure mode
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

# --- Pre-flight: live X session probe (added 2026-06-02) --------------------
# Before drafting/scraping anything, confirm the harness Chrome actually has a
# valid x.com session. One CDP Network.getCookies call (<1s) catches the
# "import never ran, evaporated after a hard restart, or auth_token expired"
# cases that previously surfaced as "Phase 1 returned 0 tweets" mysteries.
# Failing fast here turns a wasted ~7-minute scan + Claude bill into a clear
# "reconnect X" message in the log.
log "Pre-flight: probing harness Chrome for a live x.com auth_token..."
_PREFLIGHT_OUT=$(BU_NAME=twitter-harness BU_CDP_URL=http://127.0.0.1:9555 \
    "$HOME/.local/bin/browser-harness" <<'PY' 2>&1
import sys, time
try:
    raw = cdp('Network.getCookies', urls=['https://x.com/', 'https://twitter.com/'])
except Exception as e:
    print('PREFLIGHT_CDP_ERROR ' + type(e).__name__ + ': ' + str(e))
    sys.exit(0)
ck = raw.get('cookies', [])
auth = [c for c in ck if c.get('name') == 'auth_token']
if not auth:
    print('PREFLIGHT_FAIL no_auth_token cookies_total=' + str(len(ck)))
    sys.exit(0)
exp = auth[0].get('expires')
domain = auth[0].get('domain', '?')
if exp in (None, -1, 0):
    print('PREFLIGHT_OK session domain=' + domain)
else:
    now = time.time()
    if exp < now:
        print('PREFLIGHT_FAIL auth_token_expired exp=' + str(int(exp)) + ' now=' + str(int(now)))
        sys.exit(0)
    print('PREFLIGHT_OK exp=' + str(int(exp)) + ' domain=' + domain)
PY
)
if printf '%s\n' "$_PREFLIGHT_OUT" | grep -q '^PREFLIGHT_OK'; then
    log "  Pre-flight OK: $(printf '%s\n' "$_PREFLIGHT_OUT" | grep '^PREFLIGHT_OK' | head -1)"
else
    log "  Pre-flight FAILED. The harness Chrome has no live X session."
    log "  Details: $(printf '%s\n' "$_PREFLIGHT_OUT" | tail -3 | tr '\n' '|')"
    log "  Action: run \`python3 scripts/setup_twitter_auth.py connect\` (or call the connect_x MCP tool) to import a fresh X session from your everyday browser, then re-run the cycle. If the import fails with 'access denied', unlock the macOS keychain first: \`security unlock-keychain ~/Library/Keychains/login.keychain-db\`."
    echo "twitter_batches: ended $BATCH_ID"
    release_lock "twitter-browser" 2>/dev/null || true
    exit 1
fi

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
# DEFAULT Phase 1 is the deterministic qualified-query bank (no Claude): the
# bank replays every historically qualified query for the picked project in a
# single pass, so there is nothing to "retry-draft" and one attempt is enough.
# The legacy LLM-draft path (TWITTER_PHASE1_LLM_DRAFT=1) keeps the 5-attempt
# retry loop, because LLM queries frequently return empty and need re-drafting.
if [ "${TWITTER_PHASE1_LLM_DRAFT:-0}" = "1" ]; then
    MAX_SCAN_ATTEMPTS=5
else
    MAX_SCAN_ATTEMPTS=1
fi
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
# Snapshot the pre-attempt batch size so the verdict step below can compute
# kept_after_skip as a delta after the scorer finishes this attempt (2026-05-28
# retry-feedback: turns TRIED_QUERIES_JSON from bare phrasings into per-query
# verdicts the drafter can use to choose broaden vs narrow vs new-topic).
BATCH_COUNT_BEFORE_ATTEMPT="${BATCH_COUNT:-0}"

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
if [ "${TWITTER_PHASE1_LLM_DRAFT:-0}" = "1" ]; then
# === LLM QUERY-DRAFT PATH (legacy, behind TWITTER_PHASE1_LLM_DRAFT=1) ========
log "Lean Phase 1: drafting queries (no browser tools)..."

QUERIES_OUTPUT=$("$REPO_DIR/scripts/run_claude.sh" "run-twitter-cycle-queries" --strict-mcp-config --mcp-config "$TW_MCP_CONFIG" -p --output-format json --json-schema "$SCAN_SCHEMA_LEAN" "${TW_ENGINE_PREFIX}You are a Twitter query drafter. Your ONLY job is to draft fresh X advanced-search queries that surface tweets relevant to our projects. You do NOT post, you do NOT call any tools, you do NOT scrape. A separate Python pipeline runs your queries over the same CDP-driven Chrome and applies a strict freshness gate; you only return the query strings.

## Step 1: Draft one search query per project

You have $(echo "$PROJECTS_JSON" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))') projects. Draft exactly ONE Twitter search query for each, tailored to that project's ASSIGNED search_topic.

Each project entry carries TWO fields that drive your behavior: \`topic_picked_mode\` (either \`use\` or \`explore_invent\`) and \`search_topic\` (a string in \`use\` mode, NULL in \`explore_invent\` mode).

USE mode (~90% of cycles, indicated by \`topic_picked_mode: \"use\"\` and a non-null \`search_topic\`):
The Python picker has already chosen this project's search_topic by weighted-random sampling over the FULL universe in config.json. Your job is to translate that ASSIGNED topic into the best Twitter advanced-search query that will surface fresh, on-topic tweets. Do NOT substitute a different topic; do NOT paraphrase the topic. End-to-end attribution joins on the exact string.

EXPLORE_INVENT mode (~10% of cycles, indicated by \`topic_picked_mode: \"explore_invent\"\` and \`search_topic: null\`):
The picker is asking you to INVENT a brand-new search_topic. Look at the project's own \`reference_topics\` array and propose ONE new topic concept that does NOT appear there and is NOT a paraphrase of anything in it. Use your invented topic as the query's \`search_topic\` AND drive the keyword phrasing from it (one consistent string per project).

Projects:
$PROJECTS_JSON

Top past queries FOR THE PROJECT YOU'RE DRAFTING FOR (7-day window, per-project, sorted by clicks DESC first, then composite-scored: clicks×100 + likes + views×0.001). CLICKS ARE THE PRIORITY SIGNAL. Each row carries THREE labels that tell you what to do with it as a reference:

  - \`supply_bucket\`: low (<1 tweet/attempt), medium (1-5), high (>5). Raw supply X returned for this phrasing.
  - \`conversion_bucket\`: low (<0.2 post_rate), medium (0.2-0.6), high (>=0.6). How often a found tweet survived the draft gate.
  - \`guidance\`: one of MIMIC, KEEP_STYLE, NARROW, BROADEN — the action to take when drawing from this query.
  - \`posts_per_attempt\`: posts produced per Phase 1 search invocation; <0.1 means most attempts produce zero survivors.

How to act on \`guidance\`:
  - MIMIC      — gold tier. Reuse the operator skeleton verbatim, swap only the topic keyword for the picker-assigned topic.
  - KEEP_STYLE — solid. Use the operator pattern as inspiration; small phrasing tweaks OK.
  - NARROW     — high supply, low conversion (noisy pond). If you draw from it, ADD specificity: more OR alternates, stricter min_faves, extra -term excludes.
  - BROADEN    — low supply (query dying or topic running dry). The OPERATORS are dead weight. Shorten to 1-2 keywords, drop OR groups, step min_faves down a tier. Do NOT inherit operators from a BROADEN-tagged row.

The canonical source for \`min_faves:N\` selection is the PER-PROJECT SUPPLY SIGNAL block below.
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

THIS-CYCLE QUERIES ALREADY TRIED with per-query outcomes (attempt $SCAN_ATTEMPT/$MAX_SCAN_ATTEMPTS, target=$RETRY_TARGET candidates after filters). Do NOT repeat any of these phrasings or close variants. Read each entry's \`verdict\` field and respond directionally (do NOT default to generic "broaden"):
- \`dead_supply\` (raw_tweets=0): the phrasing returned ZERO tweets from X. The query was too narrow for X's index. HARD RULE: attempt N+1 MUST execute at least ONE of these THREE concrete broadening moves, NOT a topic rotation. Pick exactly one and apply it visibly: (a) lower \`min_faves\` by ONE FULL TIER (e.g. 20→5, 5→1, 1→0); (b) reduce the OR alternates inside any parenthesized group to AT MOST 2 terms (e.g. \`(A OR B OR C OR D)\` → \`(A OR B)\`); (c) drop ALL \`-term\` excludes EXCEPT those listed in this project's \`excludes_for_search\` (which remain mandatory). The PER-PROJECT SUPPLY SIGNAL block is OVERRIDDEN by \`dead_supply\` THIS CYCLE — do not appeal to historical min_faves when the current attempt returned 0. Swapping the topic noun while keeping the same operator skeleton is NOT broadening and is FORBIDDEN as a response to \`dead_supply\`.
- \`all_aged_out\` (raw>0, kept_after_age=0): topic is supply-limited at the current freshness window; every tweet was older than the cap. Pick a structurally adjacent topic; do NOT rephrase the same one (it will just hit the cap again).
- \`all_engaged_or_skipped\` (kept_after_age>0, kept_after_skip=0): query phrasing is fine, but the surviving tweets were already engaged on prior cycles. Pick a DIFFERENT topic, not a rephrase.
- \`found_some\` (kept_after_skip>0 but below target): query is on-target. Raise min_faves one tier OR add a semantic constraint to lift quality. Do NOT broaden.
$TRIED_QUERIES_JSON

Query guidelines:
- MANDATORY: do NOT add any date or time-window operator to your query (no \`since:\`, \`until:\`, \`since_time:\`, \`until_time:\`). The Python scraper enforces the freshness window at the URL level after you return; any time operator you include is stripped and overwritten. Including raw bash arithmetic, format strings, or placeholder text in place of a real epoch will be sent to X as a literal keyword and produce zero results.
- MANDATORY EVEN IF YOUR QUERY KEYWORDS DO NOT NAME THE EXCLUDED TOPIC: if a project's \`excludes_for_search\` array is non-empty, append \`-term\` for EVERY listed term to that project's query, verbatim, no exceptions.
- MANDATORY: pick \`min_faves:N\` per the PER-PROJECT SUPPLY SIGNAL above. If a project has no entry there (new / first cycle), start at min_faves:20.
- Favor discussions/opinions (people sharing experience, asking questions), not news/promos/giveaways.
- Pick a query likely to surface tweets RELEVANT to that project's actual domain.
- Mix it up each run; don't always use the same query for the same project.
- Use the project's ASSIGNED \`search_topic\` plus its \`description\` as grounding for query phrasing.
- The \`search_topic\` you emit in the output JSON MUST be the project's assigned \`search_topic\` field pasted VERBATIM (NOT the query string, NOT a paraphrase). The scoring pipeline stamps \`twitter_candidates.search_topic\` from this for end-to-end attribution.

## Output

Return ONLY the structured_output JSON with this shape:
{\"queries\": [{\"project\": \"PROJECT_NAME\", \"query\": \"X advanced search string with operators\", \"search_topic\": \"assigned or invented topic, verbatim\"}, ...]}

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

else
# === DETERMINISTIC QUALIFIED-QUERY-BANK PATH (default, 2026-05-28) ==========
# No Claude call. Replay every historically qualified query for the picked
# project(s): every distinct query that ever produced a posted reply with
# >=1 like OR >=1 non-bot link click, regardless of the per-cycle
# search_topic. This makes Phase 1 fully deterministic; the only remaining
# Claude session in the cycle is Phase 2b (reply drafting). The bank is
# exhaustive on attempt 1, so MAX_SCAN_ATTEMPTS is forced to 1 above; the
# attempt>1 guard here is belt-and-suspenders for the legacy retry loop.
QUERIES_TMP="/tmp/twcycle-${BATCH_ID}-attempt-${SCAN_ATTEMPT}-queries.json"
if [ "$SCAN_ATTEMPT" -gt 1 ]; then
    echo "[]" > "$QUERIES_TMP"
    log "Phase 1 (bank): attempt $SCAN_ATTEMPT no-op (full bank already run on attempt 1)"
else
    log "Phase 1 (bank): building qualified query bank from PROJECTS_JSON (deterministic, no Claude)..."
    echo "$PROJECTS_JSON" | python3 "$REPO_DIR/scripts/qualified_query_bank.py" --from-projects-json > "$QUERIES_TMP" 2>>"$LOG_FILE"
fi
fi

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
    # browser-harness upstream main reads the script from STDIN (the `-c` flag was
    # removed). Feed the body via a quoted heredoc and pass $REPO_DIR / $QUERIES_TMP
    # through the environment so the Python reads them from os.environ (no shell
    # expansion inside the heredoc). Keep the local CLI in sync with upstream main:
    # `uv tool install -e ~/Developer/browser-harness --force` after a git pull.
    BU_NAME=twitter-harness BU_CDP_URL=http://127.0.0.1:9555 \
    SCAN_TWEETS_FILE="$SCAN_TWEETS_FILE" \
    BATCH_ID="$BATCH_ID" \
    TWITTER_CYCLE_VARIANT="$TWITTER_CYCLE_VARIANT" \
    FRESHNESS_HOURS_DISCOVER="$FRESHNESS_HOURS_DISCOVER" \
    ENGAGED_TWEET_IDS="$ENGAGED_TWEET_IDS" \
    REPO_DIR="$REPO_DIR" \
    QUERIES_TMP="$QUERIES_TMP" \
        "$HOME/.local/bin/browser-harness" <<'PY' 2>&1 | tee -a "$LOG_FILE"
import sys, json, os, time
sys.path.insert(0, os.environ['REPO_DIR'] + '/scripts')
from twitter_scan import scan
queries = json.load(open(os.environ['QUERIES_TMP']))
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
PY
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

# Snapshot this iteration's queries WITH per-query verdicts into
# TRIED_QUERIES_JSON BEFORE log_twitter_search_attempts.py deletes QUERIES_FILE.
# Verdicts come from joining QUERIES_FILE (drafted queries) with SCAN_TWEETS_FILE
# (raw scrape per query record) and the BATCH_COUNT delta this attempt
# (kept_after_skip approximation). kept_after_age comes from the optional
# scorer sidecar at /tmp/twcycle-${BATCH_ID}-attempt-${SCAN_ATTEMPT}-scored.json
# (written by score_twitter_candidates.py --scored-sidecar); when the sidecar
# is missing we assume kept_after_age == raw_tweets, which collapses the
# all_aged_out branch into dead_supply / found_some — still useful, just less
# directional. Output entry shape:
#   {query, project, search_topic, raw_tweets, kept_after_age,
#    kept_after_skip, verdict}
# verdict ∈ {dead_supply, all_aged_out, all_engaged_or_skipped, found_some}.
if [ -f "$QUERIES_FILE" ]; then
    TRIED_QUERIES_JSON=$(python3 - \
        "$TRIED_QUERIES_JSON" "$QUERIES_FILE" "$SCAN_TWEETS_FILE" \
        "/tmp/twcycle-${BATCH_ID}-attempt-${SCAN_ATTEMPT}-scored.json" \
        "$BATCH_COUNT_BEFORE_ATTEMPT" "$BATCH_COUNT" <<'PY' 2>/dev/null || echo "$TRIED_QUERIES_JSON"
import json, os, sys
from collections import Counter

cur = json.loads(sys.argv[1] or '[]')
queries_path = sys.argv[2]
scan_path = sys.argv[3]
scored_path = sys.argv[4]
try:
    pre = int(sys.argv[5] or 0)
except Exception:
    pre = 0
try:
    post = int(sys.argv[6] or 0)
except Exception:
    post = 0

try:
    new = json.load(open(queries_path))
    if not isinstance(new, list):
        new = []
except Exception:
    new = []

# raw_tweets per query from SCAN_TWEETS_FILE (one JSONL record per scan call).
# Multiple records can share a query if the harness retried; we sum.
raw_by_query = Counter()
if scan_path and os.path.exists(scan_path):
    with open(scan_path) as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            q = (rec.get('query') or '').strip()
            n = len(rec.get('tweets') or [])
            if q:
                raw_by_query[q] += n

# kept_after_age per query from the scorer sidecar (optional). Falls back to
# raw_tweets when the sidecar is absent.
age_by_query = {}
if scored_path and os.path.exists(scored_path):
    try:
        scored = json.load(open(scored_path))
        for q, counts in (scored or {}).items():
            age_by_query[(q or '').strip()] = int(counts.get('kept_after_age') or 0)
    except Exception:
        pass

# kept_after_skip is the cycle-level delta this attempt. The scorer doesn't
# tag the per-tweet survivor with its source query upstream, so we split the
# delta evenly across queries that actually returned raw tweets. We mostly
# care about zero vs nonzero per query, not the exact split.
delta = max(0, post - pre)
queries_with_raw = [e for e in new if raw_by_query.get((e.get('query') or '').strip(), 0) > 0]
share = (delta / max(1, len(queries_with_raw))) if queries_with_raw else 0

for entry in new:
    q = (entry.get('query') or '').strip()
    raw = raw_by_query.get(q, 0)
    # When sidecar present, trust it; else assume freshness gate passed all raw.
    kept_age = age_by_query[q] if q in age_by_query else raw
    kept_skip = int(round(share)) if raw > 0 else 0
    if raw == 0:
        verdict = 'dead_supply'
    elif kept_age == 0:
        verdict = 'all_aged_out'
    elif kept_skip == 0:
        verdict = 'all_engaged_or_skipped'
    else:
        verdict = 'found_some'
    entry['raw_tweets'] = raw
    entry['kept_after_age'] = kept_age
    entry['kept_after_skip'] = kept_skip
    entry['verdict'] = verdict

cur.extend(new)
print(json.dumps(cur))
PY
)
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
    # SCAN_OUTPUT was a stale leftover from the pre-lean design (when the scan's
    # stdout was captured into a shell var); the lean Phase 1 loop now tees its
    # output to $LOG_FILE instead, so an empty-scan attempt hit `set -u` and
    # aborted the whole cycle here. Feed the classifier the recent log tail (the
    # actual scan output, where harness/Anthropic error signatures land) so we
    # still distinguish a real error from "found nothing relevant".
    SCAN_OUTPUT=$(tail -n 80 "$LOG_FILE" 2>/dev/null || true)
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
            --scored-sidecar "/tmp/twcycle-${BATCH_ID}-attempt-${SCAN_ATTEMPT}-scored.json" \
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
# Stamp cycle_variant='D' onto every candidate in this batch. The A/B/C/D
# experiment concluded 2026-05-31 (D won); this is now a constant label kept so
# downstream analytics (post-rate, thread-age-at-discover, lag-after-thread,
# top-reply ratio) stay continuous with the historical experiment rows.
# Idempotent: same value would be written if the batch is salvaged into a peer
# cycle.
# HTTP-only (2026-06-01): the cycle_variant stamp routes through
# /api/v1/twitter-candidates/stamp-cycle-variant via twitter_cycle_helper.py.
# No DATABASE_URL, no psycopg, no fallback. Idempotent: only NULL rows touched.
python3 "$REPO_DIR/scripts/twitter_cycle_helper.py" stamp-cycle-variant \
    --batch-id "$BATCH_ID" --variant "$TWITTER_CYCLE_VARIANT" \
    >/dev/null 2>>"$LOG_FILE" || log "Phase 1: cycle_variant stamp failed (non-fatal)"

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
    _COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --cycle-id "$BATCH_ID" 2>/dev/null || echo "0.0000")
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

# --- No ripen wait (winning variant D) --------------------------------------
# The 20-min ripen sleep + fetch_twitter_t1 re-measurement was removed when
# variant D won the A/B/C/D test (2026-05-31). The wait was originally a
# velocity gate; the gate floor was removed 2026-05-15 so it only fed
# delta_score into the LLM prompt, and the experiment showed eliminating that
# ~20 min thread->post lag improves engagement more than delta_score helps the
# draft. We go straight from candidate discovery to Phase 2b; delta_score stays
# at its T0 value.
log "No ripen wait (logic D): skipping sleep + T1 fetch, delta_score stays at T0 value"

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
    _COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --cycle-id "$BATCH_ID" 2>/dev/null || echo "0.0000")
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
PREP_SCHEMA='{"type":"object","properties":{"candidates":{"type":"array","items":{"type":"object","properties":{"candidate_id":{"type":"integer"},"candidate_url":{"type":"string"},"thread_author":{"type":"string"},"thread_text":{"type":"string"},"matched_project":{"type":"string"},"reply_text":{"type":"string"},"engagement_style":{"type":"string"},"new_style":{"type":["object","null"],"properties":{"description":{"type":"string"},"example":{"type":"string"},"why_existing_didnt_fit":{"type":"string"},"note":{"type":"string"},"target_chars":{"type":"integer"}}},"language":{"type":"string"},"has_landing_pages":{"type":"boolean"},"link_keyword":{"type":"string"},"link_slug":{"type":"string"},"search_topic":{"type":"string"}},"required":["candidate_id","candidate_url","matched_project","reply_text","engagement_style","language","has_landing_pages","search_topic"]}},"rejected":{"type":"array","items":{"type":"object","properties":{"candidate_id":{"type":"integer"},"reason":{"type":"string"},"proposed_excludes":{"type":"array","items":{"type":"string"}}},"required":["candidate_id","reason"]}}},"required":["candidates","rejected"]}'

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
   - Otherwise: draft a reply using the best engagement style. Length is governed ENTIRELY by the per-style LENGTH LIMIT in the style block above; obey that target and ceiling, do not apply any other length rule here. NEVER em dashes. Apply the matched project's \`voice\` block from ALL_PROJECTS_JSON: follow voice.tone, never violate voice.never, mirror voice.examples / voice.examples_good when present.
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
   - reply_text (string): the FINAL reply text WITHOUT any URL appended (the shell appends the URL later). 250 chars is the hard ceiling (leaves room for a 23-char t.co link inside the 280-char cap) — stay well under it, not up to it.
   - engagement_style (string): style name applied (or 'reused' for an unchanged stale draft). In USE mode ($PICKED_MODE=use) this MUST be the assigned style name '${PICKED_STYLE}' verbatim; the orchestrator silently coerces drift back. In INVENT mode ($PICKED_MODE=invent) this MUST be a NEW snake_case style name not in the curated style block.
   - new_style (object, REQUIRED iff INVENT mode produced a new name; OMIT or set null otherwise): {description (string), example (string), why_existing_didnt_fit (string), note (string, optional), target_chars (integer, REQUIRED)}. Same shape you passed to --new-style in step 3a. The post pipeline reads this and POSTs to /api/v1/engagement-styles/registry so the new style lands in engagement_styles_registry alongside Reddit/GitHub/Moltbook inventions. target_chars is the comment length THIS new style wins at, in characters. IMPORTANT: the example you write must be EXACTLY that length — the example IS the canonical length reference, and the hard ceiling is target_chars × 1.5. Write the example first, count its characters, then set target_chars to that count. Bias SHORT: one-liner style ~45, story-arc style up to ~180, never above 220.
   - language (string): ISO 639-1 code (en, ja, zh, es, ...)
   - has_landing_pages (bool): true iff the matched project has BOTH landing_pages.repo AND landing_pages.base_url set in config.json. Otherwise false.
   - link_keyword (string, REQUIRED when has_landing_pages=true; OMIT otherwise): a SHORT 3-6 word phrase that captures the ESSENCE OF YOUR REPLY (not just the thread topic). Think: what would a reader search to find a useful page about what you just said?
   - link_slug (string, REQUIRED when has_landing_pages=true; OMIT otherwise): kebab-case, alphanumeric+hyphens only, max 50 chars.
   - search_topic (string, REQUIRED): normally the EXACT 'Search query' value from this candidate's block above, copied verbatim (do not paraphrase, normalise, or trim). EXCEPTION (cross-route): if the matched_project you chose for this candidate is DIFFERENT from the candidate's 'Project match' field (i.e. you re-routed the thread to a better-fitting project), set search_topic to an empty string \"\" instead. The origin query's topic belongs to the project that ISSUED that query, not the one you routed to; copying it onto the new project's post miscredits the new project's topic ranking and the issuing project's query bank. When matched_project equals the 'Project match' field, copy the topic verbatim as before. The shell stamps this onto posts.search_topic so the next cycle's Phase 1 can rank which topics convert (clicks per post) and evolve the universe accordingly.

5. CLASSIFY EVERY PRE-SCORED CANDIDATE into ONE of THREE outcomes. There is NO post cap and NO per-project quota: post EVERY thread you judge genuinely on-brand.
   (a) 'candidates' — an on-brand pick you are replying to this cycle (step 4 above). No cap.
   (b) 'rejected' — ONLY for a PERMANENT, thread-intrinsic reason this thread should NEVER be replied to for the matched project: off-topic for the project, toxic / hateful, low-quality / spam / promo / shill, audience or ICP mismatch, our own account, or stale. Reason must be <=200 chars, plain text, no quotes. CRITICAL: the shell marks every 'rejected' entry status='skipped', and a skipped (thread, project) is filtered out of ALL future scans for this account PERMANENTLY. Only reject things that will never be a good fit.
   (c) OMIT from BOTH arrays — for a TIMING-ONLY reason where the thread itself is fine but you are simply not posting to it right NOW. Omitting keeps it 'pending' so a later cycle can re-judge it. ALWAYS omit (NEVER reject) when your only reason is one of:
       - you preferred a stronger candidate this cycle (there is no cap, so ideally just post this one too; if you still defer, omit it),
       - it is a near-duplicate of another thread you are already picking THIS cycle,
       - you already engaged this author / a similar thread this cycle and want to avoid back-to-back over-engagement.
       These are DEFERRALS, not rejections. Putting any of them in 'rejected' would permanently blacklist a thread that is actually fine. Do NOT do that.
   It is fine for 'candidates' to be empty (nothing on-brand) and fine for 'rejected' to be empty (nothing permanently unsuitable).
   Do NOT update twitter_candidates yourself; the shell will mark every entry of 'rejected' as status='skipped' with the reason, and Phase 0 will salvage anything you omit or forget.

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
GEN_RATE_RAW="${TWITTER_PAGE_GEN_RATE:-0.0}"
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
    _COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --cycle-id "$BATCH_ID" 2>/dev/null || echo "0.0000")
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

# --- DRAFT_ONLY gate: stop after drafting for human review (MCP manual mode) -
# When DRAFT_ONLY=1 the cycle runs scan -> score -> draft -> link-gen, leaves the
# fully-baked plan (links already stamped into reply_text) at $PLAN_FILE, and
# STOPS before posting. The social-autoposter MCP draft_cycle tool reads that
# plan, walks the human through approve/skip per draft, then posts the approved
# subset via twitter_post_plan.py. Nothing is posted from this script in that
# mode. The gate sits AFTER 2b-gen on purpose: twitter_post_plan.py does not run
# link-gen itself, so the plan must already carry baked links before we hand it
# off. Run with TWITTER_PAGE_GEN_RATE=0 (the default) so gen is a sub-second
# plain-URL rewrite, not a 10-40 min SEO build, in the interactive path.
if [ "${DRAFT_ONLY:-0}" = "1" ]; then
    # Not posting, so the browser lock isn't needed; release it if still held.
    release_lock "twitter-browser" 2>>"$LOG_FILE" || true
    rm -f "$HOME/.claude/twitter-browser-lock.json"
    log "DRAFT_ONLY=1: $PLAN_COUNT draft(s) ready for review at $PLAN_FILE. Stopping before post."
    # Emit a clean posted=0 run row and suppress the EXIT-trap summary oneshot, so
    # a draft-only run is NOT mis-synthesized as a phase2b_silent failure (the
    # trap's fallback would do that for posted=0 with candidates pending).
    _COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --cycle-id "$BATCH_ID" 2>/dev/null || echo "0.0000")
    python3 "$REPO_DIR/scripts/log_run.py" --script "post_twitter" --posted 0 --skipped 0 --failed 0 --salvaged "${SALVAGED:-0}" \
        --queries "${QUERIES_TOTAL:-0}" --duds "${DUDS_TOTAL:-0}" \
        --tweets-pulled "${TWEETS_PULLED:-0}" --candidates "${BATCH_COUNT:-0}" --above-floor "${HIGH_DELTA_COUNT:-0}" \
        --cost "$_COST" --elapsed $(( $(date +%s) - RUN_START )) 2>/dev/null || true
    _SA_RUN_SUMMARY_EMITTED=1
    # IMPORTANT: do NOT rm -f "$PLAN_FILE" here; the reviewer needs it. Print a
    # machine-readable marker so the MCP wrapper can locate the plan deterministically.
    echo "DRAFT_ONLY_PLAN=$PLAN_FILE"
    exit 0
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
