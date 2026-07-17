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
# No ripen wait (variant D won the A/B/C/D test 2026-05-31): the cycle goes
# straight from discovery to drafting. There is NO 5-min sleep and NO fxtwitter
# T1 re-poll anywhere in the Twitter pipeline. delta_score stays at its T0
# value and is no longer a gate. Do not re-introduce a ripen/sleep step here.
#
# Phase 2 (immediately after Phase 1):
#   - sort candidates by virality_score DESC (composite predictor stamped at
#     discovery by score_twitter_candidates.py); no delta floor, no T1 re-poll
#   - Claude reads top 25 (raised from 15 so the long tail reaches the model),
#     drops unsuitable, posts every candidate it judges genuinely on-brand
#     (no per-cycle post cap, no per-project quota)
#   - keep remaining pending rows: salvaged into the next cycle, hard-expired
#     by Phase 0 once tweet age crosses FRESHNESS_HOURS
#
# Launchd cadence: every 20 minutes. One combined job, one browser lock.

set -uo pipefail

# SAPS_->S4L_ env mirror (brand rename 2026-07-03): old plists/tasks still
# export SAPS_*; new code reads S4L_*. Copy names, never values via eval.
while IFS='=' read -r _k _; do
  case "$_k" in SAPS_*) _n="S4L_${_k#SAPS_}"; eval "[ -n \"\${$_n+x}\" ] || export $_n=\"\${$_k}\"";; esac
done <<EOF_ENV
$(env | grep '^SAPS_' | cut -d= -f1 | sed 's/$/=/')
EOF_ENV

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

# Honor S4L_REPO_DIR (set by the MCP wrapper + launchd plists) so a .mcpb
# install that materializes the repo under ~/.social-autoposter-mcp/repo/package
# resolves correctly. Falls back to the legacy ~/social-autoposter path for
# npm/git installs and direct invocations. Cascades to every $REPO_DIR/... ref
# below (sourced libs + child scripts inherit it), so this one line fixes the
# whole cycle's repo resolution on a bare .mcpb install.
REPO_DIR="${S4L_REPO_DIR:-$HOME/social-autoposter}"
export REPO_DIR
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

# LENGTH A/B CONCLUDED 2026-06-04: control won the configured primary metric
# (avg_clicks) and the enforcement branch is retired. New cycles no longer
# export LENGTH_ARM, so engagement_styles.py renders the legacy "keep it tight"
# prompt and twitter_post_plan.py does not stamp posts.length_arm. Historical
# arm stats stay preserved as a frozen shipped card in /api/experiments.

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
#
# 2026-07-06 (per user request): there is exactly ONE freshness path and ONE
# exception, with NO env-var knobs. Every cycle, draft or autopilot, runs the
# experiment-concluded variant D windows: 2h expire ceiling here plus 1h
# discovery. The single exception is first-run setup: the MCP server drops
# first-run-boost.json into the state dir when it installs the kicker
# (mcp/src/index.ts), and while that marker is live a draft cycle widens both
# knobs to a hardcoded 48h so the user's FIRST review batch surfaces real
# candidates. run-draft-and-publish.sh owns the marker lifecycle: consumed
# the moment a merge delivers cards, removed after 24h without any. We read
# the marker file directly; the old S4L_DRAFT_FRESHNESS_HOURS and
# S4L_FIRST_RUN_FRESHNESS_HOURS env overrides are retired.
#
# 2026-07-15 (per user request, thread-age-vs-engagement investigation): both
# knobs raised 2h/1h -> 6h. Isolating variant D's own posting history (no
# cross-variant confound) showed engagement flat through ~3h and only
# cratering past 6h (18-22 avg views vs 51-77 in the 0-3h plateau); 3-6h is a
# real but under-sampled gray zone precisely because the old 2h cap prevented
# candidates from ever reaching it. 6h is the evidence-backed outer bound, not
# a measured optimum inside 3-6h. Discovery (FRESHNESS_HOURS_DISCOVER) moves to
# 6h too, on explicit request — this reopens, rather than confirms, the
# concluded A/B/C/D result below, which found a *tighter* 1h discovery window
# winning.
#
# Also fixed here: the expire-stale gate has always compared against
# discovered_at (discovery age), not tweet_posted_at (thread age) — harmless
# while discovery was capped at 1h (the two stayed within ~1h of each other),
# but wrong now that discovery can itself be up to 6h stale at the moment we
# find a candidate: a discovered_at-based gate would then let real thread age
# reach ~12h before expiring. score_twitter_candidates.py now passes
# basis="tweet_posted_at" on its expire-stale calls so the gate measures
# thread age directly, not how long the row sat in our own pipeline. The
# route (~/social-autoposter-website twitter-candidates/expire-stale) treats
# basis as opt-in, defaulting to discovered_at, so other installs are
# unaffected.
FIRST_RUN_BOOST_MARKER="${S4L_STATE_DIR:-$HOME/.social-autoposter-mcp}/first-run-boost.json"
if [ "${DRAFT_ONLY:-0}" = "1" ] && [ -f "$FIRST_RUN_BOOST_MARKER" ]; then
    FRESHNESS_HOURS=48
    FRESHNESS_HOURS_DISCOVER=48
else
    FRESHNESS_HOURS=6
    FRESHNESS_HOURS_DISCOVER=6
fi

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
# 2026-07-15: FRESHNESS_HOURS_DISCOVER raised 1h -> 6h (see the block above) —
# this reopens the exact question this experiment settled in D's favor. D's
# own no-ripen + 2k-view-cap logic is untouched; only the discovery window
# widens.
#
# Phase 0 hard-expire uses FRESHNESS_HOURS (the union ceiling, now 6h and keyed
# on thread age via the basis param — see above) so peer cycles don't
# accidentally expire each other's still-pending rows.
TWITTER_CYCLE_VARIANT=D
# FRESHNESS_HOURS_DISCOVER is set together with FRESHNESS_HOURS in the
# first-run-boost block above: 6h everywhere (2026-07-15), 48h only while the
# setup marker is live. The lean Phase 1 CDP scraper reads it directly and
# honors any value.
# Export FRESHNESS_HOURS too so score_twitter_candidates.py inherits it and
# drives the expire-stale gate from the same knob.
export TWITTER_CYCLE_VARIANT FRESHNESS_HOURS_DISCOVER FRESHNESS_HOURS
# Hook env: ~/.claude/hooks/twitter-search-since-rewrite.py reads this and
# uses it in place of its hardcoded 6h default when present. The hook accepts
# only a 1-24h range and silently falls back to its 6h default on anything
# bigger, so cap the exported value at 24: during the 48h first-run boost the
# CDP scraper still gets the full window via FRESHNESS_HOURS_DISCOVER, while
# any hook-rewritten query keeps the widest value the hook can honor.
if [ "$FRESHNESS_HOURS_DISCOVER" -gt 24 ] 2>/dev/null; then
    export FRESHNESS_HOURS_OVERRIDE=24
else
    export FRESHNESS_HOURS_OVERRIDE=$FRESHNESS_HOURS_DISCOVER
fi

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

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

log "=== Twitter Cycle (batch=$BATCH_ID): $(date) ==="
log "Logic=D (no-ripen + 2k_view_cap; experiment concluded 2026-05-31, discovery window widened 2026-07-15); discover_freshness=${FRESHNESS_HOURS_DISCOVER}h"
log "Length-control experiment concluded 2026-06-04; winner=control; LENGTH_ARM retired"

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
if [ "${SCAN_ONLY:-0}" = "1" ]; then
    # SCAN_ONLY (the Desktop-session autopilot's scan step) gets its OWN slot pool
    # so it is NOT blocked by a live launchd autopilot cycle; the two then serialize
    # on the twitter-browser lock (acquired in Phase 1) instead of being mutually
    # exclusive. It also SKIPS the claude-blocked gate: SCAN_ONLY drives no
    # `claude -p`, so a prior claude cap must not suppress a pure scan.
    SA_PREFLIGHT_SCRIPT="run-twitter-cycle-scan"
    preflight_skip_if_jetsam_pressure
    preflight_acquire_slot_or_skip "twitter-scan" 1
elif [ -n "${S4L_SANDBOX_CANDIDATES_FILE:-}" ]; then
    # Same reasoning as the SCAN_ONLY branch above: a sandbox run does none of
    # the discovery/scraping the "twitter-cycle" pool exists to rate-limit
    # (Phase 0/1 are skipped entirely), so it gets its OWN pool rather than
    # queueing behind -- or blocking -- a live production cycle. Real
    # serialization still happens correctly at the resources that actually
    # matter: the twitter-browser lock (FIFO queue, acquire_lock waits its
    # turn rather than bailing) and the claude_job.py queue (the s4l-worker
    # drains one job at a time regardless of how many callers enqueued).
    # Still capped at 1 so two sandbox runs of my own don't overlap each
    # other. Still respects the claude-quota and jetsam-pressure gates below.
    preflight_skip_if_claude_blocked
    preflight_skip_if_jetsam_pressure
    preflight_acquire_slot_or_skip "twitter-cycle-sandbox" 1
else
    preflight_skip_if_claude_blocked
    preflight_skip_if_jetsam_pressure
    preflight_acquire_slot_or_skip "twitter-cycle" 1
fi

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
# 2026-06-26: the model-facing steps (Phase 1 query draft, Phase 2b prep draft) are
# TOOL-FREE. All browser work is done deterministically by the shell's CDP scan
# (browser-harness over port 9555) + Phase 2b-post's twitter_browser.py, NOT by the
# model. The old BROWSER BACKEND / bh_run "translation table" block is no longer
# injected: prep drafts purely from the inlined candidate context (Text: $ctext per
# candidate) + MEDIA_BLOCK, which is exactly what the model's rare bh_run fallback
# used to re-fetch (1 call/week vs ~18.5k/wk deterministic CDP scans). The 9555 Chrome
# is still launched by twitter-backend.sh above for the shell scan + post step; only
# the model's browser fallback is removed. This also drops the hardcoded "logged in as
# m13v_" identity that the block carried, so prompts are no longer single-tenant.
TW_ENGINE_PREFIX=""

# --- Prompt-sandbox short-circuit (2026-07-15) -------------------------------
# When S4L_SANDBOX_CANDIDATES_FILE is set, skip live batch tracking + Phase 0
# salvage + Phase 1 discovery/scraping entirely and jump straight to Phase
# 2b-prep drafting against a pre-selected set of historical twitter_candidates
# rows (scripts/twitter_prompt_sandbox.py writes the file in the exact
# pipe-separated shape twitter_cycle_helper.py's cmd_candidates produces).
# Lets a prompt variant (S4L_DRAFT_PROMPT_VARIANT / S4L_EXP_*) be tried
# against real past threads through the real drafting pipeline without
# spending live search/scrape quota or writing a twitter_batches telemetry
# row. The historical rows' OWN draft/status history is never written to:
# the loader mints synthetic ids far outside the real serial range so any
# accidental by-id write-back (log_draft.py, media set_media) 404s
# harmlessly instead of corrupting the real historical candidate — mirrors
# the "rd-" candidate_id prefix merge_review_queue.py already uses for
# reddit cards for the identical reason. The body of the "live" branch below
# (Phase tracking through the Phase 1 candidate fetch) is intentionally left
# at its original indentation to keep this a pure wrap, not a reformat.
if [ -z "${S4L_SANDBOX_CANDIDATES_FILE:-}" ]; then

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
#        phase2a       -> 20 min  (browser-lock handoff window; no ripen wait since 2026-05-31)
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
_PJ_ERR="$(mktemp)"
PROJECTS_JSON=$(python3 - 2>"$_PJ_ERR" <<'PY'
import json, os, subprocess, sys
REPO = os.path.expanduser(os.environ.get('S4L_REPO_DIR') or os.environ.get('REPO_DIR') or '~/social-autoposter')
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import project_excludes as pe

_pp_args = ['python3', os.path.join(REPO, 'scripts', 'pick_project.py'),
            '--platform', 'twitter', '--count', '1', '--json']
# Manual-mode (MCP draft_cycle) single-project scoping: when S4L_FORCE_PROJECT
# is set, force that exact project instead of the weighted-random autopilot
# pick, so a customer's interactive cycle only ever touches their own project.
_force_project = os.environ.get('S4L_FORCE_PROJECT')
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
_PJ_RC=$?
# Fail loud when the project/topic universe can't be built. The heredoc above
# exits non-zero (PROJECTS_JSON empty) when pick_topic_for_project finds zero
# active rows in project_search_topics for the selected project, or the topics
# API is unreachable; it also yields "[]" when no project is eligible. Without
# this guard the empty PROJECTS_JSON silently falls through to "0 queries -> 0
# tweets -> batch expired -> zero", which reads to the user as "nothing to post"
# when the real cause is "this project was never seeded with search topics".
# Seeding now happens in the MCP setup tool; this is the defense-in-depth net
# so a missing universe is surfaced, never swallowed. (2026-06-02)
if [ "$_PJ_RC" -ne 0 ] || ! printf '%s' "$PROJECTS_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if isinstance(d,list) and d else 1)' 2>/dev/null; then
    _PJ_REASON="project_selection_failed"
    if grep -q "no active search topics" "$_PJ_ERR" 2>/dev/null; then
        _PJ_REASON="no_search_topics"
    elif grep -qiE "project-search-topics API|API unreachable" "$_PJ_ERR" 2>/dev/null; then
        _PJ_REASON="topics_api_unreachable"
    fi
    log "Project/topic universe build FAILED (reason=$_PJ_REASON); stopping cycle before scan. Last error lines:"
    tail -15 "$_PJ_ERR" 2>/dev/null | sed 's/^/    /' | tee -a "$LOG_FILE"
    rm -f "$_PJ_ERR"
    # Surface the reason to the MCP draft_cycle wrapper (stdout marker) in manual mode.
    if [ "${DRAFT_ONLY:-0}" = "1" ]; then
        echo "DRAFT_ONLY_BLOCKED=$_PJ_REASON"
    fi
    # Record a dashboard-visible failure row (best-effort) and exit cleanly.
    python3 "$REPO_DIR/scripts/log_run.py" --script "post_twitter" --posted 0 --skipped 0 --failed 1 \
        --failure-reasons "${_PJ_REASON}:1" --cost "0.0000" --elapsed $(( $(date +%s) - RUN_START )) 2>/dev/null || true
    _SA_RUN_SUMMARY_EMITTED=1
    exit 0
fi
rm -f "$_PJ_ERR"

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


# --- Phase 1: deterministic query bank, scrapes tweets ----------------------
# No Claude call (removed 2026-07-08; see the query-bank comment below). The
# Python pipeline runs each banked query via headless Chrome and writes the
# tweets directly to SCAN_TWEETS_FILE for the shell.

log "Acquiring twitter-browser lock for Phase 1 scan..."
acquire_lock "twitter-browser" 3600 2>>"$LOG_FILE"
log "twitter-browser lock held (pid=$$) Phase 1"
# Drop stale Chrome singleton symlinks before launch. Background ungraceful-
# exits (SIGKILL, jetsam, force quit) leave Singleton{Lock,Cookie,Socket}
# pointing at dead PIDs / vanished sockets; without this, Chrome pops "Something
# went wrong when opening your profile" 7x and the pipeline hangs. Helper
# refuses to clean if the lock PID is alive.
ensure_twitter_browser_for_backend 2>&1 | tee -a "$LOG_FILE" || true
_ensure_rc="${PIPESTATUS[0]}"
[ "$_ensure_rc" != "0" ] && log "WARNING: twitter-harness bootstrap failed (rc=$_ensure_rc); the access probe below will catch a genuinely broken browser"

# --- Pre-flight: live X session probe (added 2026-06-02) --------------------
# Before drafting/scraping anything, confirm the harness Chrome actually has a
# valid x.com session. One CDP Network.getCookies call (<1s) catches the
# "import never ran, evaporated after a hard restart, or auth_token expired"
# cases that previously surfaced as "Phase 1 returned 0 tweets" mysteries.
# Failing fast here turns a wasted ~7-minute scan + Claude bill into a clear
# "reconnect X" message in the log.
# Probe the harness Chrome for a live x.com auth_token. Echoes a single
# PREFLIGHT_OK / PREFLIGHT_FAIL / PREFLIGHT_CDP_ERROR line. Used pre-cycle and
# again after an auto-restore from the local cookie mirror.
_xsession_probe() {
    BU_NAME=twitter-harness BU_CDP_URL=http://127.0.0.1:9555 \
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
}

log "Pre-flight: probing harness Chrome for a live x.com auth_token..."
_PREFLIGHT_OUT=$(_xsession_probe)
if ! printf '%s\n' "$_PREFLIGHT_OUT" | grep -q '^PREFLIGHT_OK'; then
    # Gap B auto-recovery: the harness Chrome lost its x.com session — its cookie
    # store was wiped on a hard restart or a macOS keychain re-lock, or never
    # persisted to disk. Re-inject from the durable 0600 local cookie mirror
    # (written on every connect, keychain-independent) via CDP, then re-probe
    # before giving up. This is what makes the session survive app/Chrome
    # restarts without a manual reconnect.
    log "  Pre-flight FAILED ($(printf '%s\n' "$_PREFLIGHT_OUT" | tail -1)); auto-restoring from local cookie mirror..."
    _RESTORE_OUT=$(TWITTER_CDP_URL="${TWITTER_CDP_URL:-http://127.0.0.1:9555}" \
        python3 "$REPO_DIR/scripts/restore_twitter_session.py" 2>&1)
    log "  Restore: $(printf '%s\n' "$_RESTORE_OUT" | tail -2 | tr '\n' '|')"
    _PREFLIGHT_OUT=$(_xsession_probe)
fi
if printf '%s\n' "$_PREFLIGHT_OUT" | grep -q '^PREFLIGHT_OK'; then
    log "  Pre-flight OK: $(printf '%s\n' "$_PREFLIGHT_OUT" | grep '^PREFLIGHT_OK' | head -1)"
else
    log "  Pre-flight FAILED. The harness Chrome has no live X session (auto-restore from the local cookie mirror did not recover it)."
    log "  Details: $(printf '%s\n' "$_PREFLIGHT_OUT" | tail -3 | tr '\n' '|')"
    log "  Action: run \`python3 scripts/setup_twitter_auth.py connect\` (or call the connect_x MCP tool) to import a fresh X session from your everyday browser, then re-run the cycle. If the import fails with 'access denied', unlock the macOS keychain first: \`security unlock-keychain ~/Library/Keychains/login.keychain-db\`."
    echo "twitter_batches: ended $BATCH_ID"
    release_lock "twitter-browser" 2>/dev/null || true
    exit 1
fi

# --- Pre-flight 2: live access-gate probe + backoff (added 2026-06-29) -------
# The cookie probe above only proves an auth_token EXISTS. X can still gate a
# perfectly valid session: from a datacenter IP (e.g. the MacStadium box) it
# commonly 302s authenticated routes to /account/access ("verify it's you") or
# fronts them with a Cloudflare "security verification" interstitial. A gated
# session renders real, public tweets as "this page doesn't exist", so the scan
# silently returns 0-few candidates and we'd draft/post against phantom
# emptiness (this is one root of the old "Phase 1 returned 0 tweets" mysteries
# that the cookie probe alone never caught). Navigate ONE authenticated route
# and STOP the cycle if X is gating us. Fails OPEN: a probe error or an
# ok/unknown render never blocks, so a transient hydration miss can't halt
# posting — only a positively-detected gate (gated:true) stops the cycle.
#
# BACKOFF: this launchd job fires every 5 min, and a gated cycle exits in ~2s,
# so without backoff we'd hit Cloudflare /account/access ~12x/hr (~288/day),
# which only deepens the datacenter-IP trust penalty. A state marker records the
# gate and an exponential cooldown (15m -> 30m -> 60m -> cap 120m). While the
# cooldown is live we skip the cycle WITHOUT navigating (no flagged traffic);
# once it elapses we re-probe; an 'ok' probe clears the marker and resumes. Only
# gated:true ever writes the marker, so fail-open is preserved.
_S4L_STATE_DIR="${S4L_STATE_DIR:-$HOME/.social-autoposter-mcp}"
_GATE_FILE="$_S4L_STATE_DIR/x-access-gate.json"
_NOW=$(date +%s)

# Backoff short-circuit: still inside a cooldown window -> skip without probing.
if [ -f "$_GATE_FILE" ]; then
    _CD_UNTIL=$(python3 -c 'import json,sys
try: print(int(json.load(open(sys.argv[1])).get("cooldown_until",0)))
except Exception: print(0)' "$_GATE_FILE" 2>/dev/null || echo 0)
    if [ "${_CD_UNTIL:-0}" -gt "$_NOW" ]; then
        _MINS=$(( (_CD_UNTIL - _NOW + 59) / 60 ))
        log "Pre-flight: X access-gate backoff active (~${_MINS}m left); skipping cycle without re-probing to avoid adding flagged Cloudflare traffic."
        echo "twitter_batches: ended $BATCH_ID"
        release_lock "twitter-browser" 2>/dev/null || true
        exit 0
    fi
fi

log "Pre-flight: probing for an X access gate (/account/access, Cloudflare)..."
_ACCESS_OUT=$(TWITTER_CDP_URL="${TWITTER_CDP_URL:-http://127.0.0.1:9555}" \
    python3 "$REPO_DIR/scripts/twitter_access_check.py" --session-probe --wait-ms 12000 2>/dev/null)
if printf '%s' "$_ACCESS_OUT" | grep -q '"gated": *true'; then
    # Write/refresh the backoff marker with an exponential cooldown. Python
    # prints "<next_mins> <consecutive> <cooldown_secs> <gate_age_secs>".
    _GATE_FIELDS=$(python3 -c 'import json,sys
gf, now = sys.argv[1], int(sys.argv[2])
base, cap, factor = 900, 7200, 2
try: prev = json.load(open(gf))
except Exception: prev = {}
cd = prev.get("cooldown_secs")
cd = base if not cd else min(int(cd)*factor, cap)
fs = int(prev.get("first_seen", now))
cons = int(prev.get("consecutive", 0)) + 1
out = {"first_seen": fs, "last_seen": now, "reason": "access_gated",
       "consecutive": cons, "cooldown_secs": cd, "cooldown_until": now+cd}
json.dump(out, open(gf, "w"))
print(cd//60, cons, cd, max(0, now-fs))' "$_GATE_FILE" "$_NOW" 2>/dev/null || echo "15 1 900 0")
    read -r _NEXT_MINS _CONS _CD_SECS _AGE_SECS <<< "$_GATE_FIELDS"
    log "  Pre-flight FAILED: X is gating this session (access gate detected)."
    log "  Probe: $(printf '%s' "$_ACCESS_OUT" | tr '\n' ' ' | tr -s ' ' | sed 's/^ *//')"
    log "  X redirected an authenticated route to /account/access or served a Cloudflare verification page. This is usually datacenter-IP trust degradation: the session cookie is still valid but X hides content from it, so a scan would return phantom 'doesn't exist' results."
    log "  Backoff engaged: next access re-probe in ~${_NEXT_MINS}m (intervening 5-min firings skip without touching Cloudflare)."
    log "  Action: open the harness Chrome (CDP :9555) and complete the verification at https://x.com/account/access once, or route the box through a residential/clean IP. The cycle auto-resumes within one cooldown of the gate lifting."
    # Machine-greppable marker (additive; mirrors the stderr-marker convention
    # bin/server.js parses). Pairs with twitter_access_gate:recovered below.
    echo "twitter_access_gate: gated consecutive=${_CONS} age_s=${_AGE_SECS} next_reprobe_s=${_CD_SECS}" >&2
    echo "twitter_batches: ended $BATCH_ID"
    release_lock "twitter-browser" 2>/dev/null || true
    exit 1
fi
# Probe-error guard (2026-07-12, S4L-4H): output lacking a "gated" verdict
# means the probe CRASHED (typically the CDP attach failed or timed out on a
# wedged Chrome) rather than returning a clean bill. The old code logged
# 'Pre-flight access OK: {"success": false, ...}' and marched a wedged browser
# into the scan. Heal once (ensure_twitter_browser_for_backend now does a real
# CDP readiness check and restarts a wedged harness Chrome), re-probe, and end
# the cycle if there is still no verdict.
if [ -z "$_ACCESS_OUT" ] || ! printf '%s' "$_ACCESS_OUT" | grep -q '"gated"'; then
    log "  Pre-flight access probe FAILED (no verdict; browser not driveable): $(printf '%s' "$_ACCESS_OUT" | tr '\n' ' ' | tr -s ' ' | sed 's/^ *//')"
    echo "twitter_cdp_wedge: access_probe_no_verdict" >&2
    ensure_twitter_browser_for_backend || true
    _ACCESS_OUT=$(TWITTER_CDP_URL="${TWITTER_CDP_URL:-http://127.0.0.1:9555}" \
        python3 "$REPO_DIR/scripts/twitter_access_check.py" --session-probe --wait-ms 12000 2>/dev/null)
    if [ -z "$_ACCESS_OUT" ] || ! printf '%s' "$_ACCESS_OUT" | grep -q '"gated"'; then
        log "  Pre-flight access probe still has no verdict after harness heal; ending cycle."
        echo "twitter_batches: ended $BATCH_ID"
        release_lock "twitter-browser" 2>/dev/null || true
        exit 1
    fi
    if printf '%s' "$_ACCESS_OUT" | grep -q '"gated": *true'; then
        log "  Recovered probe reports an access gate; ending cycle (backoff engages on the next firing)."
        echo "twitter_batches: ended $BATCH_ID"
        release_lock "twitter-browser" 2>/dev/null || true
        exit 1
    fi
    log "  Access probe recovered after harness heal."
fi
# Probe came back clean. If a backoff marker exists we were gated: record the
# recovery (how long the gate lasted, since first_seen) BEFORE deleting it, so
# the lift event + duration survive in the log even though the marker is gone.
if [ -f "$_GATE_FILE" ]; then
    _REC=$(python3 -c 'import json,sys
try: d = json.load(open(sys.argv[1]))
except Exception: d = {}
now = int(sys.argv[2]); fs = int(d.get("first_seen", now)); cons = int(d.get("consecutive", 0))
dur = max(0, now-fs)
print(dur, dur//60, cons)' "$_GATE_FILE" "$_NOW" 2>/dev/null || echo "0 0 0")
    read -r _DUR_S _DUR_M _RCONS <<< "$_REC"
    rm -f "$_GATE_FILE"
    echo "twitter_access_gate: recovered_after_s=${_DUR_S} consecutive=${_RCONS}" >&2
    log "  X access gate lifted after ~${_DUR_M}m (${_RCONS} consecutive gated probes); cleared backoff marker and resuming normal cycle."
fi
log "  Pre-flight access OK: $(printf '%s' "$_ACCESS_OUT" | tr '\n' ' ' | tr -s ' ' | sed 's/^ *//')"

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
# Phase 1 is the deterministic qualified-query bank (no Claude): the bank
# replays every historically qualified query for the picked project in a
# single pass, so there is nothing to "retry-draft" and one attempt is enough.
# (The old LLM-draft path used a 5-attempt retry loop, because LLM-drafted
# queries frequently returned empty and needed re-drafting; removed 2026-07-08
# — unused since the bank became the default 2026-05-28, and the queue-routed
# claude call it made held the twitter-browser lock for no reason, the same
# class of bug fixed in Phase 2b-prep the same day.)
MAX_SCAN_ATTEMPTS=1
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
REPO = os.path.expanduser(os.environ.get('S4L_REPO_DIR') or os.environ.get('REPO_DIR') or '~/social-autoposter')
sys.path.insert(0, os.path.join(REPO, 'scripts'))
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
# === DETERMINISTIC QUALIFIED-QUERY-BANK PATH (default since 2026-05-28;
# only path since 2026-07-08 removal of the LLM query-draft alternative) =====
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
REPO = os.path.expanduser(os.environ.get('S4L_REPO_DIR') or os.environ.get('REPO_DIR') or '~/social-autoposter')
sys.path.insert(0, os.path.join(REPO, 'scripts'))
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

# Stamp phase2a before releasing the lock so the salvage budget covers the
# browser-lock handoff window (phase2a budget = 20 min).
python3 "$REPO_DIR/scripts/twitter_batch_phase.py" advance "$BATCH_ID" --phase phase2a 2>&1 | tee -a "$LOG_FILE" || true

# Release the twitter-browser lock between Phase 1 scrape and Phase 2b posting.
# Other pipelines (engage-twitter, dm-outreach-twitter, link-edit-twitter,
# stats.sh) can run their browser steps in this window instead of waiting for us
# to finish. We re-acquire just before Phase 2b posts, blocking up to the
# acquire_lock timeout if another pipeline is mid-run.
log "Releasing twitter-browser lock between Phase 1 scrape and Phase 2b posting..."
release_lock "twitter-browser" 2>>"$LOG_FILE"
# (2026-06-16) NO `rm -f twitter-browser-lock.json` here. The blind rm was
# ownership-unaware and ran AFTER release_lock, so under a pipeline handoff it
# deleted a LIVE peer's session mutex (defect b) -> two browser ops on one X
# tab. Dead python:PID holders are now reclaimed by _acquire_browser_lock in
# scripts/twitter_browser.py (os.kill liveness), so the workaround is obsolete
# AND unsafe. Do NOT re-add it. See docs/twitter_browser_lock.md.

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

else
    log "SANDBOX MODE: loading historical candidates from $S4L_SANDBOX_CANDIDATES_FILE (Phase 0/Phase 1 discovery skipped, no twitter_batches row created)."
    if [ ! -s "$S4L_SANDBOX_CANDIDATES_FILE" ]; then
        log "Sandbox candidates file missing or empty: $S4L_SANDBOX_CANDIDATES_FILE"
        _SA_RUN_SUMMARY_EMITTED=1
        exit 1
    fi
    BATCH_ID="sandbox-$(date +%Y%m%d-%H%M%S)"
    CANDIDATES=$(cat "$S4L_SANDBOX_CANDIDATES_FILE")
    SALVAGED=0
    QUERIES_TOTAL=0
    DUDS_TOTAL=0
    TWEETS_PULLED=0
    BATCH_COUNT=$(printf '%s\n' "$CANDIDATES" | grep -c '^[0-9]')
    # Phase 1 normally sets this (query-bank routing signal); skipped here, so
    # default it under `set -u` (line 31) -- otherwise Phase 2b-gen's payload
    # build below hits "TOP_QUERIES_PER_PROJECT_JSON: unbound variable" (found
    # live 2026-07-15; non-fatal thanks to that call's own `|| echo '{}'`
    # fallback, but noisy and worth defaulting properly).
    TOP_QUERIES_PER_PROJECT_JSON='{}'
    # Same reasoning as TOP_QUERIES_PER_PROJECT_JSON above: all three are
    # Phase-1-only outputs (query-bank routing / dud-query / supply-signal
    # reports), referenced later in the same Phase 2b-gen payload build.
    # Discovered incrementally across two live sandbox runs -- `set -u`
    # only reports the FIRST unbound reference it hits per expansion, so
    # fixing one revealed the next; defaulting all three here at once
    # instead of one more round-trip each.
    SUPPLY_SIGNAL_JSON='[]'
    DUD_QUERIES_JSON='[]'
    TOP_TOPICS_JSON='[]'
    # Hard force, NOT just a default (2026-07-15): a sandbox run must NEVER
    # reach Phase 2b-post, full stop. A prior test run without an explicit
    # DRAFT_ONLY=1 raced straight through Phase 2b-prep/2b-gen to Phase
    # 2b-post and actually attempted a real post -- it was only skipped
    # because that specific historical candidate happened to already be
    # posted-to in reality (an incidental dedup hit, not a real safety net).
    # scripts/twitter_post_plan.py's post_one() now also hard-refuses any
    # experiments.sandbox candidate regardless of this, but that is
    # defense-in-depth, not a substitute for stopping before Phase 2b-post
    # even runs. This also happens to be the only way a sandbox run ever
    # produces a reviewable draft instead of skipping straight to a post
    # attempt.
    export DRAFT_ONLY=1
fi

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

# --- SCAN_ONLY gate: stop after scoring, emit candidates, skip drafting -------
# When SCAN_ONLY=1 the cycle runs scan -> score -> top-N select, writes the chosen
# candidates as JSON, and STOPS before the claude drafting step. The MCP
# scan_candidates tool reads this so a Claude Desktop scheduled-task session can do
# the drafting ITSELF (on the user's plan, no `claude -p`) and hand the drafts back
# via submit_drafts. Candidates stay 'pending' (drafted+posted via submit_drafts ->
# post_drafts, or salvaged by a later cycle). The browser lock was already released
# at the Phase 1 handoff, so this exits clean via the EXIT trap. NO current caller
# sets SCAN_ONLY, so the autopilot/draft_cycle paths are byte-for-byte unchanged.
if [ "${SCAN_ONLY:-0}" = "1" ]; then
    SCAN_FILE="/tmp/s4l_scan_candidates_${BATCH_ID}.json"
    # $CANDIDATES is the same pipe-separated top-N the drafting step consumes (cols
    # documented in twitter_cycle_helper.py:cmd_candidates; tweet_text/draft fields
    # are pipe+newline sanitized there, so a field split is safe). Batch id + out
    # path travel via env so the single-quoted python needs no shell interpolation.
    printf '%s\n' "$CANDIDATES" | S4L_SCAN_FILE="$SCAN_FILE" S4L_SCAN_BATCH="$BATCH_ID" python3 -c '
import json, os, sys
def _i(x):
    try:
        return int(float(x or 0))
    except Exception:
        return 0
def _f(x):
    try:
        return float(x or 0)
    except Exception:
        return 0.0
out = []
for line in sys.stdin:
    line = line.rstrip("\n")
    if not line.strip():
        continue
    p = line.split("|")
    if len(p) < 14 or not p[0].isdigit():
        continue
    out.append({
        "id": int(p[0]), "tweet_url": p[1], "author_handle": p[2], "tweet_text": p[3],
        "virality_score": _f(p[4]), "delta_score": _f(p[5]), "matched_project": p[6],
        "search_topic": p[7], "likes": _i(p[8]), "retweets": _i(p[9]), "replies": _i(p[10]),
        "views": _i(p[11]), "author_followers": _i(p[12]), "age_hours": _f(p[13]),
        "existing_draft": p[14] if len(p) > 14 else "", "existing_draft_style": p[15] if len(p) > 15 else "",
    })
json.dump({"batch_id": os.environ["S4L_SCAN_BATCH"], "candidates": out}, open(os.environ["S4L_SCAN_FILE"], "w"))
' 2>/dev/null || printf '{"batch_id": "%s", "candidates": []}' "$BATCH_ID" > "$SCAN_FILE"
    SCAN_N=$(python3 -c "import json; print(len(json.load(open('$SCAN_FILE')).get('candidates') or []))" 2>/dev/null || echo 0)
    log "SCAN_ONLY=1: $SCAN_N candidate(s) scored and written to $SCAN_FILE. Stopping before drafting (agent drafts next)."
    _SA_RUN_SUMMARY_EMITTED=1
    echo "SCAN_ONLY_RESULT=$SCAN_FILE"
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
# Thread-media capture (2026-06-03): collect each candidate's id|url so that,
# AFTER the browser lock is acquired, we can deterministically pre-fetch the
# media (images/videos/GIFs/link-cards) of every thread the model is about to
# draft against and feed it into the prep prompt. Gated by
# S4L_TWITTER_CAPTURE_MEDIA so it stays a no-op until the website API (with the
# set_media action + thread_media column) deploys. Populated in the loop below.
MEDIA_URLS_FILE=$(mktemp -t s4l_twitter_media_urls_XXXXXX.tsv)
while IFS='|' read -r cid curl cauthor ctext cscore cdelta cproject ctopic clikes crts creplies cviews cfollowers cage cdraft cdraftstyle cdraftage; do
    if [ -n "$cid" ] && [ -n "$curl" ]; then
        printf '%s\t%s\n' "$cid" "$curl" >> "$MEDIA_URLS_FILE"
    fi
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
import json, os, sys
repo_dir = os.path.expanduser(os.environ.get('S4L_REPO_DIR') or os.environ.get('REPO_DIR') or '~/social-autoposter')
sys.path.insert(0, os.path.join(repo_dir, 'scripts'))
# Prompt-sandbox config override (2026-07-15): a sandbox run testing another
# install's real voice/persona (scripts/admin_fetch_install_config.py
# materializes it) points this at a fetched copy instead of the operator's
# own config.json. Checked BEFORE the real path since the repo-root
# config.json is a symlink to the state dir -- the override must never
# follow that symlink back to the operator's own config.
_sandbox_dir = os.environ.get('S4L_SANDBOX_CONFIG_DIR')
_config_path = os.path.join(_sandbox_dir, 'config.json') if _sandbox_dir else os.path.join(repo_dir, 'config.json')
config = json.load(open(_config_path))
projects = config.get('projects', [])
# learned_preferences is a SINGLE install-wide block since 2026-07-08 (see
# scripts/learned_preferences.py). Since 2026-07-14 it is NO LONGER stamped
# into each project entry: the prep prompt embeds it exactly ONCE via
# GLOBAL_LEARNED_PREFS_JSON (the 'Global learned preferences' line right
# after this JSON). The old per-project stamping quintupled the block in
# promotion prompts (~100KB of a ~283KB prompt was 5 identical copies).
lane = os.environ.get('S4L_ACTIVE_LANE', '')
if lane == 'personal_brand':
    # Personal-brand lane is pure organic growth: the drafter must NOT see any
    # product config at all (no website, links, booking_link, get_started_link,
    # features, pricing, CTAs). We emit ONLY the persona project, and ONLY the
    # drafting-relevant fields, so there is literally no product context in the
    # prompt to accidentally pitch, quote, or link. This also kills cross-routing
    # (no 'other project' exists to route a candidate to). Whitelist, not
    # denylist: any field added to the persona entry later stays out unless
    # explicitly allowed here. (learned_preferences left the whitelist
    # 2026-07-14: it now travels once, globally, not inside the entry.)
    ALLOWED = {
        'name', 'description', 'content_angle', 'voice',
        'voice_relationship', 'content_guardrails',
    }
    persona = next((p for p in projects if p.get('persona') is True), None)
    out = {}
    if persona:
        out[persona['name']] = {k: v for k, v in persona.items() if k in ALLOWED}
    print(json.dumps(out, indent=2))
else:
    # Promotion lane: full product config MINUS ops-only plumbing the drafter
    # can never use (analytics ids, contact/SEO bylines, link-infra flags,
    # onboarding metadata). Denylist, not whitelist, on purpose: an unknown
    # future MARKETING field should reach the drafter by default; add a key
    # here only when it is provably non-drafting plumbing. landing_pages must
    # stay (the plan schema's has_landing_pages is defined against it).
    OPS_KEYS = {
        'posthog', 'contact', 'seo_author', 'seo_roundup', 'web_chat',
        'short_links_host', 'short_links_live', 'external_short_links',
        'force_utm_only', 'booking_link_auto_share', 'onboarded_at',
        'client', 'client_engagement', 'geo_focus', 'engagement_start',
        'weight', 'enabled', 'demo_video', 'platforms_disabled',
        'brand_domain', 'learned_preferences',
    }
    out = {}
    for p in projects:
        out[p['name']] = {k: v for k, v in p.items() if k not in OPS_KEYS}
    print(json.dumps(out, indent=2))
" 2>/dev/null || echo "{}")

# The install-wide learned_preferences block, embedded ONCE in the prep prompt
# (see the 'Global learned preferences' line under PROJECT ROUTING). history is
# the feedback loop's audit changelog: it lives in config.json for humans and
# has NO reader in any prompt (the digest's own prompt embeds only the four
# managed lists), so it is stripped here; it was ~15KB per copy and rode into
# every drafting prompt as dead weight before 2026-07-14.
GLOBAL_LEARNED_PREFS_JSON=$(python3 -c "
import json, os, sys
repo_dir = os.path.expanduser(os.environ.get('S4L_REPO_DIR') or os.environ.get('REPO_DIR') or '~/social-autoposter')
sys.path.insert(0, os.path.join(repo_dir, 'scripts'))
import learned_preferences as lp
# Same sandbox config override as ALL_PROJECTS_JSON above (see that comment).
_sandbox_dir = os.environ.get('S4L_SANDBOX_CONFIG_DIR')
_config_path = os.path.join(_sandbox_dir, 'config.json') if _sandbox_dir else os.path.join(repo_dir, 'config.json')
config = json.load(open(_config_path))
block = lp.get_global_block(config)
block.pop('history', None)
print(json.dumps(block, indent=2))
" 2>/dev/null || echo "{}")

# Engagement-style picker (2026-05-19): pick ONE assigned style for this
# cycle. The picked style flows two places: (1) --style filter for
# top_performers.py so the per-style exemplars section shows only posts
# matching the assigned style, (2) s4l_render_style_block (below) so the
# prompt block embeds the same assignment. On invent mode picked_style is
# empty and top_performers stays unfiltered (model sees full landscape).
source "$REPO_DIR/skill/styles.sh"
STYLE_ASSIGN_FILE=$(mktemp -t s4l_twitter_assign_XXXXXX.json)
s4l_pick_style twitter posting "$STYLE_ASSIGN_FILE" >/dev/null 2>&1 || true
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

# --- Second engagement style for dual-draft review cards (2026-07-07) -------
# Two-draft cards: pick a SECOND style ("Style B") this cycle, independent of
# the first ("Style A" = PICKED_STYLE/PICKED_MODE above). The prep step drafts
# every fresh candidate once under EACH style and the reviewer picks which
# posts; see PREP_SCHEMA/PREP_PROMPT below. Bounded retry so the two styles
# are actually different (a same-name pair defeats the whole point); INVENT
# mode on either side is accepted immediately since an invented name is
# definitionally distinct from a pinned one. This is orthogonal to the
# treatment_v4/control_v4 draft-prompt A/B below (that varies WORDING of the
# directive for the whole cycle; this varies STYLE per draft slot), so neither
# experiment disturbs the other.
STYLE_ASSIGN_FILE_B=$(mktemp -t s4l_twitter_assign_b_XXXXXX.json)
# --- Draft-B exploration source (2026-07-11) ---------------------------------
# Style B is now the EXPLORE slot: it trials the newest human_derived styles
# and post-2026-07-10 inventions (least-used first) instead of drawing a
# second scored pick from the same proven pool as Style A. This is the
# distribution channel for the standalone invent_styles.py job: the card
# pick + the posted draft's engagement write a new style's first real score,
# and winners graduate into the Draft-A pool via the normal sampler. NOTHING
# is invented here (pick_exploration_style never returns mode=invent). On an
# empty pool or API failure we fall back to the legacy second scored pick so
# dual-draft cards never break. The source tag rides the S4L_EXP_ convention:
# active_experiments.collect() auto-stamps it onto every plan candidate and
# the review card's details-eye renders it with zero card-side code.
DRAFT_B_SOURCE=$(python3 -c "
import json, sys
sys.path.insert(0, '$REPO_DIR/scripts')
from engagement_styles import pick_exploration_style
a = pick_exploration_style('twitter', context='posting', exclude={'$PICKED_STYLE'})
if a and a.get('style'):
    with open('$STYLE_ASSIGN_FILE_B', 'w') as f:
        json.dump(a, f)
    print(a.get('source') or '')
" 2>/dev/null || echo "")
if [ -n "$DRAFT_B_SOURCE" ]; then
    PICKED_STYLE_B=$(python3 -c "
import json
try:
    with open('$STYLE_ASSIGN_FILE_B') as f:
        d = json.load(f)
    print(d.get('style') or '')
except Exception:
    print('')
" 2>/dev/null)
    PICKED_MODE_B="use"
fi
if [ -z "${PICKED_STYLE_B:-}" ]; then
    DRAFT_B_SOURCE="scored_fallback"
    for _style_b_attempt in 1 2 3; do
        s4l_pick_style twitter posting "$STYLE_ASSIGN_FILE_B" >/dev/null 2>&1 || true
        PICKED_STYLE_B=$(python3 -c "
import json
try:
    with open('$STYLE_ASSIGN_FILE_B') as f:
        d = json.load(f)
    print(d.get('style') or '')
except Exception:
    print('')
" 2>/dev/null)
        PICKED_MODE_B=$(python3 -c "
import json
try:
    with open('$STYLE_ASSIGN_FILE_B') as f:
        d = json.load(f)
    print(d.get('mode') or 'use')
except Exception:
    print('use')
" 2>/dev/null)
        if [ "$PICKED_MODE" = "invent" ] || [ "$PICKED_MODE_B" = "invent" ] || [ "$PICKED_STYLE_B" != "$PICKED_STYLE" ]; then
            break
        fi
    done
fi
export S4L_EXP_DRAFT_B_SOURCE="$DRAFT_B_SOURCE"
log "Engagement style B assigned: mode=$PICKED_MODE_B style=${PICKED_STYLE_B:-(invent)} source=$DRAFT_B_SOURCE"

# --- Draft-prompt A/B: voice-first (v4, 2026-07-15) --------------------------
# Per-CYCLE arm (the prep session drafts the whole batch from ONE prompt, so
# assignment is at cycle granularity, not per post; the whole batch shares it).
#   control_v4   = the plain draft directive (v3's control text verbatim).
#   treatment_v4 = voice-first package: real, human-verified voice signal (the
#                  account's own corpus + voice.examples + learned_preferences)
#                  is made the DOMINANT signal, elevated above and prioritized
#                  over the assigned engagement style, which is demoted from a
#                  binding FORM (v3's treatment) to an optional idea, rendered
#                  arm-aware by engagement_styles.get_assigned_style_prompt,
#                  which reads S4L_DRAFT_PROMPT_VARIANT at render time IN this
#                  cycle process. Also skips the per-style top_performers
#                  exemplar report entirely (TOP_REPORT/TOP_REPORT_B below) so
#                  no cross-account "what wins in this style" signal competes
#                  with the account's own real voice. Applied GLOBALLY: both
#                  the promotion-lane and personal_brand-lane directives are
#                  arm-aware, not just personal_brand (learned_preferences is
#                  install-wide and voice.examples is per-project already).
#   History: v1 (2026-06-29) decoupled the product pivot; the model kept the
#   skeleton (30% ~= 28% on 857 replies). v2 (2026-07-06) banned the
#   concede-then-reverse STRUCTURE. v3 (2026-07-10) made the style block the
#   binding FORM of the draft, testing whether structural specificity beat a
#   uniform length clamp. v4 (2026-07-15) replaces v3's treatment wholesale:
#   rather than testing HOW MUCH the style should structure the draft, it
#   tests whether the account's own real voice (corpus + examples +
#   learned_preferences) should dominate over the style and other generic
#   exemplars entirely. Retiring v3 rather than running v4 alongside it as an
#   independent axis: v4 directly manipulates the same style-block-bindingness
#   variable v3 already owned, and running both independently would produce
#   literal contradictions in the same prompt in one of the four combinations
#   (v3-treatment saying "commit to this style's form", v4-treatment saying
#   "the style is optional"), not just a statistical interaction to control for.
# The arm is stamped onto every post this cycle via S4L_DRAFT_PROMPT_VARIANT
# (read by twitter_post_plan.py -> log_post.py -> posts.draft_prompt_variant),
# mirroring the tail_link_variant plumbing. Split tunable via
# TWITTER_DRAFT_PROMPT_AB_RATE = fraction of cycles assigned to 'treatment'.
# CODE DEFAULT 0.5 = 50/50 EVERYWHERE (2026-07-06): every install runs a real
# holdback so treatment can always be measured against the plain control
# prompt. The old default of 1 (100% treatment) was changed because it
# silently dropped the control arm whenever the .env pin did not propagate to the
# running env (the installed-package driver reads its OWN .env, not the source
# tree's), leaving no control data. Robustly defaulting to 0.5 in code, not via an
# .env override, prevents that. The dashboard reads the SAME var with the SAME
# default (bin/server.js), so display and routing never diverge.
DRAFT_PROMPT_AB_RATE="${TWITTER_DRAFT_PROMPT_AB_RATE:-0.5}"
# Arm VALUE versioned to '..._v4' on 2026-07-15 to RESET the experiment. The old
# v1/v2/v3 rows stay in the DB under their old labels but the dashboard now
# counts only the '_v4' arms, so the voice-first experiment starts fresh from
# zero. Bump this suffix again on any future reset (keep bin/server.js
# DRAFT_PROMPT_VARIANT_DEFS, scripts/active_experiments.py DESCRIPTIONS, and the
# treatment_v4 gate in scripts/engagement_styles.py get_assigned_style_prompt in
# sync).
# Explicit override (2026-07-16): if a caller (the sandbox tooling, or
# anyone else) already exported S4L_DRAFT_PROMPT_VARIANT, respect it instead
# of overwriting it with a fresh coin flip -- lets a sandbox run force
# treatment_v4 or control_v4 deliberately instead of leaving it to a random
# 50/50, which is the whole point of an A/B comparison tool. No real caller
# ever pre-sets this var today, so live cycles are unaffected.
if [ -n "${S4L_DRAFT_PROMPT_VARIANT:-}" ]; then
    log "Draft-prompt A/B arm: $S4L_DRAFT_PROMPT_VARIANT (forced via env, no coin flip)"
else
    # Server-side per-install pin (2026-07-16): lets the operator remotely pin
    # ONE specific install to always draft under one arm (e.g. Nhat's real
    # account -> treatment_v4), via POST /api/v1/installations/draft-prompt-variant.
    # Mirrors the posting_mode precedent (2026-07-13) exactly: NULL/unset is
    # the default for every install, so the 50/50 A/B holdback (2026-07-06:
    # every install runs a real split so treatment can always be measured
    # against control) stays intact unless explicitly overridden here. This
    # pins DRAFTING ONLY; it does not change discovery, posting, or anything
    # else. Fetch failure fails OPEN to the random coin flip below, same
    # fail-open contract as the VIRALITY_THRESHOLD fetch elsewhere in this
    # phase -- a down/slow API must never block or bias drafting.
    S4L_DRAFT_PROMPT_VARIANT_PIN=$(python3 -c "
import os, sys
_repo = os.path.expanduser(os.environ.get('S4L_REPO_DIR') or os.environ.get('REPO_DIR') or '~/social-autoposter')
sys.path.insert(0, os.path.join(_repo, 'scripts'))
from http_api import api_get
try:
    r = api_get('/api/v1/installations/draft-prompt-variant')
    v = (r or {}).get('data', {}).get('variant')
    if v:
        print(v)
except BaseException:
    pass
" 2>/dev/null || echo "")
    if [ -n "${S4L_DRAFT_PROMPT_VARIANT_PIN:-}" ]; then
        S4L_DRAFT_PROMPT_VARIANT="$S4L_DRAFT_PROMPT_VARIANT_PIN"
        log "Draft-prompt A/B arm: $S4L_DRAFT_PROMPT_VARIANT (server-side pin for this install, no coin flip)"
    else
        S4L_DRAFT_PROMPT_VARIANT=$(python3 -c "
import random
try:
    rate = float('$DRAFT_PROMPT_AB_RATE')
except Exception:
    rate = 0.5
rate = min(1.0, max(0.0, rate))
print('treatment_v4' if random.random() < rate else 'control_v4')
" 2>/dev/null || echo treatment_v4)
        log "Draft-prompt A/B arm: $S4L_DRAFT_PROMPT_VARIANT (rate=$DRAFT_PROMPT_AB_RATE)"
    fi
fi
export S4L_DRAFT_PROMPT_VARIANT
# v4 RESET (2026-07-15, replaces v3 wholesale): v3 tested whether the
# assigned engagement style should be a binding structural FORM. v4 tests a
# different, bigger claim: that real, measured voice signal (the account's
# own corpus + voice.examples + learned_preferences, all human-verified)
# should dominate the draft over EVERY other competing signal — including the
# style block's structure, which is now demoted to an optional idea
# (scripts/engagement_styles.py), and the per-style top-performers exemplar
# report, which treatment_v4 skips computing entirely (see TOP_REPORT below).
# Applied GLOBALLY (both lanes), not personal_brand-only: learned_preferences
# has been install-wide since 2026-07-08 and voice.examples exists per-project
# for every project, persona or product. v1-v3 arm strings are retired; the
# dashboard (bin/server.js DRAFT_PROMPT_VARIANT_DEFS) and
# scripts/active_experiments.py DESCRIPTIONS now count only v4.
if [ "$S4L_DRAFT_PROMPT_VARIANT" = "treatment_v4" ]; then
    DRAFT_DIRECTIVE="Otherwise: draft a direct, natural reply that stands on its own as a useful contribution to the thread. Mention the matched project only when it is genuinely the most relevant thing to say, and state it plainly in one clause; most replies will not need it. PRIORITY ORDER for how you write this, highest first: (1) learned_preferences.draft_style_notes and edit_examples under PROJECT ROUTING are the strongest signal available, real human corrections to this account's own past drafts, and are MANDATORY, not advisory. (2) The ACCOUNT VOICE CORPUS block and the matched project's voice.examples are VERBATIM GROUND TRUTH for how this account actually writes: capitalization, punctuation, contractions, sentence length, terseness. Match those mechanics exactly. (3) voice.tone is a supporting description only, never ground truth: if it ever conflicts with what the corpus/examples actually show, follow the examples, not the tone description. (4) The assigned engagement style is an optional idea for angle and length, not a required structure; draw on it only when it fits naturally, never at the cost of (1)-(3). Do NOT use the concede-then-reverse skeleton in ANY form. Banned openings include: 'X is the easy part/half/win, the hard part is Y'; 'X was never the [thing], it's Y'; 'X isn't the [problem], it's Y'; 'the real/actual/harder part is Y'; 'what actually breaks/ships/matters is Y'; 'the part nobody says/shows is Y'; 'X is solved, Y is what breaks'. Lead with substance from ONE entry point and vary the entry point across replies: a concrete first-hand specific or number; a direct answer to the exact question asked; one sharp opinion with no hedge; a genuine question that moves the thread forward; or a relevant pointer. No warm-up framing sentence before the substance. Length is governed by the per-style LENGTH LIMIT in the style block above. NEVER em dashes. Never violate voice.never. Treat learned_preferences.audience_avoid / thread_avoid matches as strong reasons to skip the candidate. Never violate content_guardrails.do_not."
else
    DRAFT_DIRECTIVE="Otherwise: draft a reply using the best engagement style. Length is governed ENTIRELY by the per-style LENGTH LIMIT in the style block above; obey that target and ceiling, do not apply any other length rule here. NEVER em dashes. Apply the matched project's \`voice\` block from ALL_PROJECTS_JSON: follow voice.tone, never violate voice.never, mirror voice.examples / voice.examples_good when present. The global learned_preferences block under PROJECT ROUTING is distilled human review feedback and is MANDATORY, not advisory: follow every learned_preferences.draft_style_notes entry when writing (it overrides the engagement style's structural template on conflict), and treat learned_preferences.audience_avoid / thread_avoid matches as strong reasons to skip the candidate. Never violate content_guardrails.do_not."
fi
# Draft A/B divergence note (2026-07-16, treatment_v4 only): style assignment
# is otherwise the ONLY instructed reason draft_a_text and draft_b_text ever
# differ (see the fixed "they must diverge... because they follow different
# style templates" line below) -- confirmed live on Nhat's PersonalBrand
# project: Draft B's engagement style is picked in INVENT mode as often as
# not (a fresh model-invented style, not a vetted registry entry), and that
# invention kept landing on the exact formulaic "'X' is the Y" reframe opener
# her learned_preferences.draft_style_notes explicitly bans, regardless of
# the invented style's name. Style alone isn't a reliable enough divergence
# mechanism when the style itself is freshly improvised. This does NOT touch
# style assignment (both slots still get a picked/assigned style exactly as
# before); it adds genuine distinctness as its OWN explicit requirement,
# independent of style, so the two drafts stay real alternatives even when
# style differences alone fail to force them apart. control_v4 is unaffected.
if [ "$S4L_DRAFT_PROMPT_VARIANT" = "treatment_v4" ]; then
    DRAFT_B_DIVERGENCE_NOTE=" Beyond following different style templates: draft_a_text and draft_b_text must be genuinely distinct takes on this thread -- think of them as variant 1a and variant 1b: different entry point, different specific detail seized on, different rhetorical move -- not two phrasings of the same underlying observation dressed in different style language. If 1a and 1b would say essentially the same thing, pick a different angle entirely for 1b rather than just rewording 1a."
else
    DRAFT_B_DIVERGENCE_NOTE=""
fi
# Personal-brand lane (S4L_ACTIVE_LANE=personal_brand, set by s4l_mode.py):
# replace the product-framed directive entirely. This lane is pure organic
# growth: no product, no link, no CTA. The reply must add real value grounded in
# the persona's first-hand material (the ACCOUNT VOICE CORPUS block + the
# persona voice block), not concede-and-agree filler. Replaces the
# product-framed promotion directives above, but is itself arm-aware (see
# below): treatment_v4 adds the skeleton ban AND the voice/preferences
# priority order; control_v4 keeps the plain persona directive, so the A/B
# runs in this lane too.
if [ "${S4L_ACTIVE_LANE:-}" = "personal_brand" ]; then
    # Arm-aware persona lane (v4 RESET, 2026-07-15): treatment_v4 adds the
    # concede-then-reverse ban clause AND a PRIORITY ORDER clause making the
    # ACCOUNT VOICE CORPUS / voice.examples and learned_preferences outrank
    # the freeform voice.tone description and the assigned engagement style
    # whenever they disagree. voice.tone is synthesized LLM prose written at
    # onboarding and can drift from what the corpus actually shows (seen in
    # the wild: a persona's tone said "lowercase is fine" while every scanned
    # example was properly capitalized) — the old directive never told the
    # model which one wins on conflict; v4 does. control_v4 keeps the plain
    # persona directive. The style block itself (STYLES_BLOCK, rendered in
    # this process) is also arm-aware: it renders as an optional idea, not a
    # binding FORM, on treatment_v4 (scripts/engagement_styles.py).
    if [ "$S4L_DRAFT_PROMPT_VARIANT" = "treatment_v4" ]; then
        PERSONA_SKELETON_BAN=" Also do NOT use the concede-then-reverse skeleton in ANY form: banned openings include 'X is the easy part/half/win, the hard part is Y', 'X was never the [thing], it's Y', 'X isn't the [problem], it's Y', 'the real/actual/harder part is Y', 'what actually breaks/ships/matters is Y', 'the part nobody says/shows is Y', and 'X is solved, Y is what breaks'; if a draft has that pivot, rewrite it from one of the entry points above."
        PERSONA_VOICE_PRIORITY_CLAUSE=" PRIORITY ORDER for how you write this, highest first: (1) learned_preferences.draft_style_notes and edit_examples are the strongest signal available, real human corrections to this account's own past drafts, and are MANDATORY. (2) The ACCOUNT VOICE CORPUS block and voice.examples above are VERBATIM GROUND TRUTH for how they actually write: capitalization, punctuation, contractions, sentence length, terseness. Match those mechanics exactly. (3) voice.tone is a supporting description only, never ground truth: if it ever conflicts with what the corpus/examples actually show (for example tone says lowercase is fine but every real example is properly capitalized), follow the examples. (4) The assigned engagement style is an optional idea for angle and length, not a required structure."
        PERSONA_PREFS_RELATION="(per the PRIORITY ORDER above: learned_preferences and the account's real voice outrank the engagement style whenever they disagree)"
    else
        PERSONA_SKELETON_BAN=""
        PERSONA_VOICE_PRIORITY_CLAUSE=""
        PERSONA_PREFS_RELATION="(it overrides the engagement style's structural template on conflict)"
    fi
    DRAFT_DIRECTIVE="Otherwise: draft a reply that stands on its own as a genuinely useful contribution to THIS thread. Ground it in the persona's real, first-hand experience from the ACCOUNT VOICE CORPUS block below (specific projects, real numbers, sharp opinions, actual failures) and in the persona's \`voice\` block from ALL_PROJECTS_JSON. Add exactly ONE of: a concrete specific from that lived experience, a sharp non-obvious opinion, a useful pointer, or a question that genuinely moves the thread forward. NEVER generic agreement ('makes sense', 'this is spot on', 'great point', 'the nuance here is').${PERSONA_SKELETON_BAN} This is a personal account, not a brand: sound like a real person in the thread. If web search is available and the thread hinges on a current fact, verify it before drafting rather than guessing. Length is governed by the per-style LENGTH LIMIT in the style block above. NEVER em dashes. Follow voice.tone, never violate voice.never, mirror voice.examples / voice.examples_good when present.${PERSONA_VOICE_PRIORITY_CLAUSE} The global learned_preferences block under PROJECT ROUTING is distilled human review feedback and is MANDATORY, not advisory: follow every learned_preferences.draft_style_notes entry when writing ${PERSONA_PREFS_RELATION}, and treat learned_preferences.audience_avoid / thread_avoid matches as strong reasons to skip the candidate. Never violate content_guardrails.do_not."
fi

# 2026-07-10 anti-sameness: --no-project-sections strips the multi-project
# winner corpus (~400 lines of "Top Posts by Project" + summary table) that
# used to ride along in EVERY draft prompt and homogenize drafts. The prep
# session now queries per-project winners on demand AFTER routing a
# candidate (see PROJECT TOP PERFORMERS section in PREP_PROMPT below).
# 2026-07-15 (draft_prompt v4): treatment_v4 skips even the per-style
# exemplar report below. voice.examples + the account voice corpus +
# learned_preferences are meant to be the dominant signal on treatment; a
# report of what wins in this style ACROSS OTHER ACCOUNTS/PROJECTS is direct
# competing signal against one specific account's own real voice, and is cut
# entirely rather than reduced. control_v4 is unaffected (unchanged from v3).
if [ "$S4L_DRAFT_PROMPT_VARIANT" = "treatment_v4" ]; then
    TOP_REPORT=""
    TOP_REPORT_B=""
else
    if [ -n "$PICKED_STYLE" ]; then
        TOP_REPORT=$(python3 "$REPO_DIR/scripts/top_performers.py" --platform twitter --style "$PICKED_STYLE" --no-project-sections 2>/dev/null || echo "(top performers report unavailable)")
    else
        TOP_REPORT=$(python3 "$REPO_DIR/scripts/top_performers.py" --platform twitter --no-project-sections 2>/dev/null || echo "(top performers report unavailable)")
    fi
    if [ -n "$PICKED_STYLE_B" ]; then
        TOP_REPORT_B=$(python3 "$REPO_DIR/scripts/top_performers.py" --platform twitter --style "$PICKED_STYLE_B" --no-project-sections 2>/dev/null || echo "(top performers report unavailable)")
    else
        TOP_REPORT_B=$(python3 "$REPO_DIR/scripts/top_performers.py" --platform twitter --no-project-sections 2>/dev/null || echo "(top performers report unavailable)")
    fi
fi

# --- Cross-cycle self-memory (anti-repetition) ------------------------------
# 2026-07-10: the prep session never used to see this account's own recent
# replies across threads (author_history_block is per-author only), so it
# recycled the same openers and sentence skeletons cycle after cycle. This
# block is NEGATIVE context, rendered with explicit do-not-imitate rules by
# recent_self_posts.py. Empty string on any failure; never blocks the cycle.
RECENT_SELF_BLOCK=$(python3 "$REPO_DIR/scripts/recent_self_posts.py" --platform twitter --limit 20 2>/dev/null || echo "")

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
S4L_TWITTER_GEN_TRACE_PATH=$(printf '%s' "$TRACE_INPUT" | python3 "$REPO_DIR/scripts/write_generation_trace.py" --prefix twitter_gen_trace_ 2>/dev/null || echo "")
export S4L_TWITTER_GEN_TRACE_PATH
if [ -n "$S4L_TWITTER_GEN_TRACE_PATH" ] && [ -f "$S4L_TWITTER_GEN_TRACE_PATH" ]; then
    log "Generation trace: $S4L_TWITTER_GEN_TRACE_PATH ($(wc -c < "$S4L_TWITTER_GEN_TRACE_PATH") bytes)"
else
    log "WARN: generation_trace build returned empty path; posts this cycle will have NULL trace"
fi

STYLES_BLOCK=$(s4l_render_style_block "$STYLE_ASSIGN_FILE" twitter posting)
# Style B block: the assigned-style portion only (description/example/note/
# length/grounding rule), NOT the full s4l_render_style_block — content rules,
# anti-patterns, and the voice-relationship rule are platform/project-wide, not
# per-style, and are already embedded once via STYLES_BLOCK above. Calling
# get_assigned_style_prompt() directly here avoids repeating those shared
# sections a second time and keeps skill/styles.sh untouched.
STYLES_BLOCK_B=$(python3 -c "
import json, sys
sys.path.insert(0, '$REPO_DIR/scripts')
from engagement_styles import get_assigned_style_prompt
try:
    with open('$STYLE_ASSIGN_FILE_B', 'r') as f:
        assignment = json.load(f)
    print(get_assigned_style_prompt('twitter', assignment, context='posting'))
except Exception:
    print('(style module unavailable)')
" 2>/dev/null || echo "(style module unavailable)")
# Style assignment files are the ones we picked above; styles.sh already sourced.
# Cleanup at cycle end (best effort).
trap 'rm -f "$STYLE_ASSIGN_FILE" "$STYLE_ASSIGN_FILE_B" 2>/dev/null || true' EXIT

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
# Sandbox short-circuit: skip so a sandbox run never auto-creates a
# twitter_batches row (advance's own "auto-creates the row if start was
# missed" fallback would otherwise silently do it, since sandbox mode never
# calls 'start' — found live 2026-07-15, a stray sandbox-* row landed in
# production twitter_batches from this exact call).
if [ -z "${S4L_SANDBOX_CANDIDATES_FILE:-}" ]; then
    python3 "$REPO_DIR/scripts/twitter_batch_phase.py" advance "$BATCH_ID" --phase phase2b-prep 2>&1 | tee -a "$LOG_FILE" || true
fi
# Thread-media capture (2026-06-03, gated by S4L_TWITTER_CAPTURE_MEDIA, default
# OFF). When enabled, does ONE cheap deterministic pass over every candidate
# thread to pull its media (images/videos/GIFs/link-cards), persist each into
# twitter_candidates.thread_media, and build a MEDIA CONTEXT block injected into
# the prep prompt so the reply-writer can react to what the tweet visually shows
# instead of replying text-blind. Must be deterministic (Python pre-fetch) because
# the prep prompt forbids the model from calling twitter_browser.py. Entirely
# best-effort: any failure leaves MEDIA_BLOCK empty and the cycle proceeds.
#
# The twitter-browser lock exists SOLELY to guard this capture step, so it is
# now gated by the same flag (2026-07-16): acquiring it unconditionally meant
# every cycle -- sandbox or live -- contended for the browser lock even when
# nothing in this phase touches the browser, which is the default state (media
# capture off). Found live: a sandbox test run waited ~6 minutes in the FIFO
# lock queue behind a real cycle for a lock it never ended up needing.
MEDIA_BLOCK=""
if [ "${S4L_TWITTER_CAPTURE_MEDIA:-0}" = "1" ] || [ "${S4L_TWITTER_CAPTURE_MEDIA:-}" = "true" ]; then
    log "Re-acquiring twitter-browser lock for Phase 2b-prep (read+draft only)..."
    acquire_lock "twitter-browser" 3600 2>>"$LOG_FILE"
    log "twitter-browser lock held (pid=$$) Phase 2b-prep"
    # Drop stale singleton locks (see clean_stale_singleton.sh, also called in Phase 1).
    ensure_twitter_browser_for_backend 2>&1 | tee -a "$LOG_FILE" || true
    _ensure_rc="${PIPESTATUS[0]}"
    [ "$_ensure_rc" != "0" ] && log "WARNING: twitter-harness bootstrap failed (rc=$_ensure_rc); continuing anyway, downstream browser calls may fail"

    if [ -s "$MEDIA_URLS_FILE" ]; then
        log "Phase 2b-prep: capturing thread media for $(wc -l < "$MEDIA_URLS_FILE" | tr -d ' ') candidate(s)..."
        MEDIA_BLOCK=$(python3 "$REPO_DIR/scripts/capture_thread_media.py" --urls-file "$MEDIA_URLS_FILE" --scroll 1 2>>"$LOG_FILE" || true)
        if [ -n "$MEDIA_BLOCK" ]; then
            log "Phase 2b-prep: media context captured ($(printf '%s' "$MEDIA_BLOCK" | grep -c '^Candidate ') thread(s) with media)."
        else
            log "Phase 2b-prep: no media captured (none found or capture skipped)."
        fi
    fi
    rm -f "$MEDIA_URLS_FILE" 2>/dev/null || true

    # Release the twitter-browser lock now. Thread-media capture above was the
    # ONLY browser-touching step in Phase 2b-prep; the Claude drafting call below
    # is architecturally browser-free (--strict-mcp-config omits the
    # twitter-harness MCP, so the model can never reach the CDP Chrome even if it
    # tried) and, since the 2026-06-23 queue migration, "run-twitter-cycle-prep"
    # routes through claude_job.py to an independent worker process that can
    # block for up to S4L_CLAUDE_QUEUE_TIMEOUT (1800s default). Holding the
    # browser lock across that wait made this process a preemption target for
    # any post that needed the browser: the post-vs-scan hijack fix SIGKILLs
    # whoever holds the lock (see docs/twitter_browser_lock.md), which killed
    # this process mid-wait while the worker kept drafting in the background,
    # stranding the finished result until the salvage reconciler's 35-min window
    # (scripts/salvage_orphaned_prep_results.py) picked it up. Releasing here
    # removes this whole phase from being a SIGKILL target for the rest of the
    # drafting wait. Phase 2b-post re-acquires unconditionally below.
    log "Releasing twitter-browser lock before Claude drafting (drafting never touches the browser)..."
    release_lock "twitter-browser" 2>>"$LOG_FILE"
else
    log "Phase 2b-prep: thread-media capture disabled (S4L_TWITTER_CAPTURE_MEDIA not set); skipping the twitter-browser lock entirely."
    rm -f "$MEDIA_URLS_FILE" 2>/dev/null || true
fi

# --- ACCOUNT VOICE CORPUS injection (both lanes, 2026-07-15) -----------------
# build_persona.py apply / voice_exemplars.py write a raw first-hand corpus
# sidecar (persona_corpus.txt) next to config.json, scanned from the
# account's OWN real posts/replies. It reflects the ACCOUNT HOLDER, not a
# specific project, so it is equally valid grounding whether this cycle is
# drafting a personal_brand reply or a promotion-lane reply for a product
# project: same human, same fingers, same account. Previously gated to the
# personal_brand lane only, which withheld real grounding data from every
# promotion-lane draft for no reason tied to the data itself; ungated so
# every lane gets it whenever the file exists. This is NOT part of the
# draft_prompt A/B — both v4 arms (and any legacy card mid-flight) see it;
# withholding real examples from control would confound "does showing real
# examples help" with "does emphasizing them differently help."
CORPUS_BLOCK=""
# Same sandbox override as the config.json reads above: persona_corpus.txt is
# a flat repo-root file (not symlinked), so it needs its own override branch
# rather than following $REPO_DIR.
_PERSONA_CORPUS_PATH="${S4L_SANDBOX_CONFIG_DIR:+$S4L_SANDBOX_CONFIG_DIR/persona_corpus.txt}"
_PERSONA_CORPUS_PATH="${_PERSONA_CORPUS_PATH:-$REPO_DIR/persona_corpus.txt}"
if [ -f "$_PERSONA_CORPUS_PATH" ]; then
    CORPUS_BLOCK="## ACCOUNT VOICE CORPUS (raw first-hand material — ground your reply in THIS)
This is the account holder's own public writing and work, verbatim. Quote and draw real specifics from it: actual projects, real numbers, sharp opinions, real failures. Do NOT invent anything not supported here or in the project's voice block. Use it to make the reply concrete and unmistakably human, and as ground truth for HOW they write (capitalization, punctuation, contractions, length) regardless of which project this reply is for.
$(cat "$_PERSONA_CORPUS_PATH")
"
    log "Phase 2b-prep: injected account voice corpus ($(wc -c < "$_PERSONA_CORPUS_PATH" | tr -d ' ') bytes)."
fi

log "Phase 2b-prep: Claude reading threads and drafting replies (no post cap)..."

# Pre-assign the prep session UUID in the parent shell so it survives the
# command-substitution subshell run_claude.sh runs in. We write it into the
# plan JSON below so Phase 2b-post can re-export it for log_post.py, which
# stamps posts.claude_session_id and lets the dashboard activity feed join
# to claude_sessions for cost. Without this, twitter posts get NULL session
# ids and blank cost cells.
CLAUDE_SESSION_ID="$(uuidgen | tr 'A-Z' 'a-z')"
export CLAUDE_SESSION_ID

# PREP_SCHEMA — strict JSON schema for the prep envelope. Two-draft cards
# (2026-07-07, no-recommendation redesign 2026-07-08): each candidate
# carries draft_a_* / draft_b_* (one per assigned style) instead of a
# single reply_text/engagement_style, plus is_reused_draft. The model does
# NOT pick a favorite; Draft A is always the default shown/selected on the
# review card, the reviewer's own click chooses otherwise. Includes optional
# `draft_a_new_style` / `draft_b_new_style` per candidate (inner objects) that
# the model MUST populate when that slot's picker set mode=invent and it
# invented a snake_case name. Fields mirror
# engagement_styles.py::_REQUIRED_NEW_STYLE_FIELDS so the downstream
# validate_or_register call accepts the block without a second schema layer.
PREP_SCHEMA='{"type":"object","properties":{"candidates":{"type":"array","items":{"type":"object","properties":{"candidate_id":{"type":"integer"},"candidate_url":{"type":"string"},"thread_author":{"type":"string"},"thread_text":{"type":"string"},"matched_project":{"type":"string"},"is_reused_draft":{"type":"boolean"},"draft_a_text":{"type":"string"},"draft_a_style":{"type":"string"},"draft_a_new_style":{"type":["object","null"],"properties":{"description":{"type":"string"},"example":{"type":"string"},"why_existing_didnt_fit":{"type":"string"},"note":{"type":"string"},"target_chars":{"type":"integer"}}},"draft_a_text_en":{"type":["string","null"]},"draft_b_text":{"type":["string","null"]},"draft_b_style":{"type":["string","null"]},"draft_b_new_style":{"type":["object","null"],"properties":{"description":{"type":"string"},"example":{"type":"string"},"why_existing_didnt_fit":{"type":"string"},"note":{"type":"string"},"target_chars":{"type":"integer"}}},"draft_b_text_en":{"type":["string","null"]},"language":{"type":"string"},"thread_text_en":{"type":"string"},"has_landing_pages":{"type":"boolean"},"link_keyword":{"type":"string"},"link_slug":{"type":"string"},"search_topic":{"type":"string"}},"required":["candidate_id","candidate_url","matched_project","is_reused_draft","draft_a_text","draft_a_style","draft_b_text","draft_b_style","language","has_landing_pages","search_topic"]}},"rejected":{"type":"array","items":{"type":"object","properties":{"candidate_id":{"type":"integer"},"reason":{"type":"string"},"proposed_excludes":{"type":"array","items":{"type":"string"}}},"required":["candidate_id","reason"]}}},"required":["candidates","rejected"]}'

PREP_PROMPT="${TW_ENGINE_PREFIX}You are the Social Autoposter prep step.

Your ONLY job in THIS session:
  1. Read each candidate's thread context from the PRE-SCORED CANDIDATES block below (each entry's 'Text:' field is the parent tweet). You have WebSearch and WebFetch available: use them ONLY when a thread hinges on a current fact, a name, a release, or a claim you are not sure about, so your reply is specific and correct instead of vague. You do NOT have the Twitter/X browser this session — never fetch, navigate, or open a tweet/x.com URL, and never try to load the thread itself; the thread text you need is already inlined below. Most replies need no search at all; reach for it only when it materially improves the reply.
  2. Draft TWO independent replies for each fresh candidate, one under Draft A's assigned style and one under Draft B's assigned style below. Do not judge or rank them, the reviewer reads both and picks.
  3. Persist the recommended fresh draft via log_draft.py.
  4. Emit a structured plan describing the chosen candidates, both draft texts, and (when applicable) the SEO link keyword + slug.

You will NOT post anything. You will NOT generate landing pages. You will NOT call log_post.py. The shell handles all of that AFTER your session ends, with the browser lock released for the long landing-page build.

Read $SKILL_FILE for content rules and voice context.
Read $REPO_DIR/config.json for project metadata.

## PRE-SCORED CANDIDATES (sorted by Virality DESC, highest first)
Virality is a composite predictor of how big this thread will get AFTER you reply: it combines engagement velocity (eng/hour), author reach (follower tier), age decay (6h half-life), retweet ratio, reply count, and discussion quality (reply:like ratio). On historical posted data the highest-Virality cohort (score >= 10000) received ~36x the median reply views of the lowest cohort (score < 10), so prioritize on-brand candidates with HIGH Virality. Rule of thumb: Virality >= 100 = strong thread on a real growth curve, your reply is likely to land 10-100x more eyeballs than a low-Virality thread. Delta (5min) is the raw T1-T0 engagement count and is shown for context only; do not re-rank on Delta.
$CANDIDATE_BLOCK
$MEDIA_BLOCK
$CORPUS_BLOCK

## PROJECT ROUTING (per-candidate)
Each candidate has a 'Project match' field. Use that project unless the thread content clearly better fits another project.
All project configs: $ALL_PROJECTS_JSON
Global learned preferences (ONE install-wide block; it applies to EVERY project. Any directive in this prompt that references a project's learned_preferences block means THIS block): $GLOBAL_LEARNED_PREFS_JSON

## PROJECT TOP PERFORMERS (query on demand, do NOT skip routing first)
The feedback reports below carry a per-style exemplar only; project winners are no longer bulk-injected. AFTER you have decided which project a candidate's draft is for, you MAY pull that project's own recent winners (last 30 days, ranked by real click rate) when you are unsure how this product converts in replies:
   python3 $REPO_DIR/scripts/top_performers.py --platform twitter --project 'PROJECT_NAME' --top 3 --brief --invoked-by '$BATCH_ID'
(PROJECT_NAME exactly as it appears in the candidate's 'Project match' / config.json.) Treat the results as evidence of which CLAIMS and ANGLES landed for that product, never as structural templates: do not copy their sentence shape, opener, or pivot wording. One call per project at most; skip the call entirely for projects you already queried this session.

$RECENT_SELF_BLOCK

## DRAFT A: assigned style + feedback from past performance
$TOP_REPORT

$STYLES_BLOCK

## DRAFT B: assigned style + feedback from past performance
$TOP_REPORT_B

$STYLES_BLOCK_B

## WORKFLOW
There is NO cap on how many candidates you may pick this cycle. Pick EVERY candidate whose thread is genuinely on-brand and worth a substantive reply. Skip a candidate ONLY when its thread is off-topic for the matched project, toxic / hateful, low-quality / spam, an audience mismatch, or a near-duplicate of something already replied to. Do NOT cap, quota, or balance picks by project: if the strongest candidates this cycle all belong to one project, pick all of them. Project routing matters; project diversification does not. Never force a weak entry just to add volume, and never drop a strong on-brand entry just to limit volume.

For each chosen candidate:
1. Read the candidate's parent tweet from its 'Text:' field in the PRE-SCORED CANDIDATES block above.
2. Understand the context from that inlined text (the thread text is already in this prompt; you do NOT have the Twitter browser, but you MAY use WebSearch/WebFetch for external facts when a thread needs them to be answered well).
3. DRAFT HANDLING (existing vs fresh):
   - If the candidate block shows an EXISTING DRAFT line AND draft age < 30 minutes, REUSE the draft text verbatim as draft_a_text/draft_a_style (set is_reused_draft=true, draft_b_text=null, draft_b_style=null). Do NOT call log_draft.py; do NOT redraft; do NOT write a second variant, prior cycle already paid the LLM cost for the one draft you have.
   - Otherwise (fresh candidate, is_reused_draft=false): write TWO independent drafts. Do NOT judge, rank, or pick a favorite between them, both are shown to the reviewer, who decides.
     - draft_a_text: follow the DRAFT A style block above (its own description/example/note/length limit).
     - draft_b_text: follow the DRAFT B style block above, written INDEPENDENTLY from scratch as if draft_a_text did not exist. Do NOT lightly reword draft_a_text into draft_b_text, they must diverge in length and rhetorical move because they follow different style templates, not just differ in phrasing. If you notice draft_b_text ending up as a paraphrase of draft_a_text, stop and rewrite it from Style B's own example instead.$DRAFT_B_DIVERGENCE_NOTE
   - $DRAFT_DIRECTIVE (applies to both drafts on fresh candidates; each still obeys its OWN style's length limit, not a shared one).
3a. PERSIST DRAFT A (skip entirely for reused drafts):
     python3 $REPO_DIR/scripts/log_draft.py --candidate-id CANDIDATE_ID --text 'DRAFT_A_TEXT' --style DRAFT_A_STYLE --assigned-style '$PICKED_STYLE' --assigned-mode '$PICKED_MODE'
   Always persist draft_a_text/draft_a_style here (Draft A is the single-draft representative used if a near-immediate next cycle reuses this candidate's draft verbatim per step 3 above); never draft_b.
   The --assigned-style / --assigned-mode flags carry the orchestrator's picker output (this cycle: mode=$PICKED_MODE style='${PICKED_STYLE:-(invent)}') into the candidate row so the post pipeline can coerce drift and register invented styles. Pass them VERBATIM as shown.
   If Draft A used an invented style (i.e. mode is invent and its STYLE is a new snake_case name not in the Draft A style block), ALSO pass:
     --new-style '{\"description\":\"...\",\"example\":\"...\",\"why_existing_didnt_fit\":\"...\"}'
   with the same description/example/why_existing_didnt_fit you put in draft_a_new_style in your output JSON for this candidate.
   Failure here is non-fatal, log a warning and continue.
4. EMIT one entry in the structured 'candidates' array with these fields:
   - candidate_id (int): from the candidate block
   - candidate_url (string): the parent tweet URL
   - thread_author (string): the @handle (no leading @)
   - thread_text (string): the parent tweet's text, condensed to <=500 chars if needed
   - matched_project (string): the project name to attribute this post to
   - is_reused_draft (bool, REQUIRED): true iff you reused an existing draft verbatim per step 3 above, false for a freshly-drafted candidate.
   - draft_a_text (string, REQUIRED): the FINAL Draft A reply text WITHOUT any URL appended (the shell appends the URL later). 250 chars is the hard ceiling (leaves room for a 23-char t.co link inside the 280-char cap) — stay well under it, not up to it. On a reused candidate this IS the reused text.
   - draft_a_style (string, REQUIRED): style name applied to draft_a_text (or the reused candidate's existing style). In USE mode ($PICKED_MODE=use) this MUST be the Draft A assigned style name '${PICKED_STYLE}' verbatim; the orchestrator silently coerces drift back. In INVENT mode ($PICKED_MODE=invent) this MUST be a NEW snake_case style name not in the Draft A style block.
   - draft_a_new_style (object, REQUIRED iff Draft A's INVENT mode produced a new name; OMIT or set null otherwise): {description (string), example (string), why_existing_didnt_fit (string), note (string, optional), target_chars (integer, REQUIRED)}. target_chars is the comment length THIS new style wins at, in characters; the example you write must be EXACTLY that length, write the example first, count its characters, then set target_chars to that count. Bias SHORT: one-liner style ~45, story-arc style up to ~180, never above 220.
   - draft_a_text_en (string, REQUIRED when language != 'en'; null when language == 'en'): a faithful English translation of draft_a_text. Display-only: the review card shows it so the operator can understand a non-English draft; it is NEVER posted. Translate meaning, not word-by-word; no added commentary.
   - draft_b_text (string, REQUIRED when is_reused_draft=false; null when is_reused_draft=true): the FINAL Draft B reply text, same rules as draft_a_text, written under the DRAFT B style block instead.
   - draft_b_style (string, REQUIRED when is_reused_draft=false; null when is_reused_draft=true): style name applied to draft_b_text, same rules as draft_a_style but against the Draft B assignment (USE mode style '${PICKED_STYLE_B}', mode $PICKED_MODE_B).
   - draft_b_new_style (object, REQUIRED iff Draft B's INVENT mode produced a new name; OMIT or set null otherwise): same shape as draft_a_new_style.
   - draft_b_text_en (string, REQUIRED when is_reused_draft=false AND language != 'en'; null otherwise): faithful English translation of draft_b_text, same display-only rules as draft_a_text_en.
   - language (string): ISO 639-1 code (en, ja, zh, es, ...)
   - thread_text_en (string, REQUIRED when language != 'en'; OMIT when language == 'en'): a faithful English translation of thread_text (same <=500 char condensation). Display-only, never posted.
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
- You do NOT have the Twitter/X browser this session: never navigate, fetch, or open a tweet/x.com URL, and never try to reload the thread. WebSearch/WebFetch ARE available for external fact-checking only; use them sparingly and never to open the tweet itself.
- NEVER use em dashes. Use commas, periods, or regular dashes (-).
- Reply in the SAME LANGUAGE as the parent tweet."

# Pipe the prep prompt via stdin instead of passing as a shell argument.
# On Linux ARG_MAX is 2MB; the assembled prompt (config.json + top_report +
# styles + schema + candidates) busts that on the VM, dying with E2BIG
# "Argument list too long". stdin has no such cap.
# --allowedTools: restore external fact-checking to the prep drafter (removed
# 2026-06-26). --strict-mcp-config stays so the twitter-harness browser MCP is NOT
# loaded: the model can search the web but can never touch the CDP Chrome that
# Phase 2b-post drives (that would break the two-lock). The tools are passed as a
# SINGLE comma-separated token on purpose: claude_job.py's queue parser (box
# installs) treats --allowedTools as a one-value flag, so a space-separated second
# tool would leak in as the prompt. On the box these flags ride through
# claude_job.py; Desktop's own web search + the reworded prompt enable it there.
# --- Prep-prompt snapshot (2026-07-11) ---------------------------------------
# Persist the exact rendered PREP_PROMPT per batch so prompt-block presence is
# verifiable after any release (grep the file), instead of reverse-engineering
# it from package scripts. The queue's prompt-*.md files are transient work
# files deleted on completion, and the generation trace deliberately carries
# only the exemplar context, so this is the ONLY durable full-prompt record.
# Local-only, newest 50 kept (file cleanup, not candidate-row retention; the
# no-retention rule covers DB *_candidates rows). Never blocks the run.
PREP_PROMPT_DIR="${S4L_STATE_DIR:-$HOME/.social-autoposter-mcp}/prep-prompts"
if mkdir -p "$PREP_PROMPT_DIR" 2>/dev/null; then
    _PP_FILE="$PREP_PROMPT_DIR/prep-prompt-$BATCH_ID.md"
    if printf '%s' "$PREP_PROMPT" > "$_PP_FILE" 2>/dev/null; then
        ls -t "$PREP_PROMPT_DIR"/prep-prompt-*.md 2>/dev/null | tail -n +51 | while IFS= read -r _pp_old; do
            rm -f "$_pp_old"
        done
        log "[prep_prompt_snapshot] batch=$BATCH_ID bytes=$(wc -c < "$_PP_FILE" | tr -d ' ') path=$_PP_FILE"
    else
        log "WARN: prep-prompt snapshot write failed for batch=$BATCH_ID (non-fatal)"
    fi
fi

PREP_OUTPUT=$(printf '%s' "$PREP_PROMPT" | "$REPO_DIR/scripts/run_claude.sh" "run-twitter-cycle-prep" --strict-mcp-config --mcp-config "$TW_MCP_CONFIG" --allowedTools WebSearch,WebFetch -p --output-format json --json-schema "$PREP_SCHEMA" 2>&1)

echo "$PREP_OUTPUT" >> "$LOG_FILE"

# --- TOP-N POST CAP (2026-06-29) -------------------------------------------
# The prep model still drafts EVERY on-brand candidate, but autopilot now posts
# only the single highest-Virality one per cycle. This caps per-account reply
# volume (the May-June ~10x ramp that collapsed per-post reach ~15x) while
# keeping the strongest thread. Deferred picks are dropped from the plan so they
# stay status='pending' (NOT 'skipped'); Phase 0 salvage re-judges them next
# cycle and reuses their fresh drafts. (2026-06-30) The cap is now the SINGLE
# standard for BOTH lanes: autopilot direct-post AND DRAFT_ONLY manual MCP review.
# The old DRAFT_ONLY=1 -> POST_TOP_N=0 special-case was removed on purpose, so the
# human reviews the exact same one highest-Virality draft the autopilot would post.
# Override with S4L_TWITTER_POST_TOP_N (default 1; 0 = no cap, env opt-out only).
# Sandbox default is 0 (no cap): the top-1 rule above exists so a human
# reviewer sees exactly what autopilot would post, which is meaningless when
# nothing is ever posted -- a sandbox test wants every model-approved draft
# visible for comparison, not just the single highest-virality one. Still
# overridable with S4L_TWITTER_POST_TOP_N if someone wants the production cap
# reproduced in sandbox mode too.
if [ -n "${S4L_SANDBOX_CANDIDATES_FILE:-}" ]; then
    POST_TOP_N="${S4L_TWITTER_POST_TOP_N:-0}"
else
    POST_TOP_N="${S4L_TWITTER_POST_TOP_N:-1}"
fi

# --- ROLLING VIRALITY BAR (2026-07-02) --------------------------------------
# Fetch THIS install's trailing-24h virality percentile so the parse step posts
# the top-1 ONLY if it clears the bar. This holds the post rate near the target
# (~10 / day as of 2026-07-13 reach-recovery: p0.99 replayed against the prior
# week's picks yields ~12/day vs ~91/day at the old p0.90) with NO hard cap:
# the bar is the Nth percentile of the install's
# OWN recent candidate pool (via /api/v1/twitter-candidates/virality-threshold),
# so it self-calibrates to cadence and niche instead of being a fixed number.
# The bar applies to BOTH lanes (2026-07-03, per user instruction): the
# autopilot lane drops below-bar picks before POSTING, and the DRAFT_ONLY lane
# drops them before they become review cards, so human review time is never
# spent on bottom-of-pool drafts. Dropped picks stay status='pending' (never
# 'rejected'); Phase 0 salvages and re-judges them next cycle.
# The bar is OFF (empty threshold) when:
#   - Cold start: sample_count < min, so a fresh pool posts ungated until it fills.
#     (This is also what keeps brand-new installs seeing every draft card.)
#   - Fetch failure: fail-open, never silence posting on a transient API blip.
# Virality percentile is HARDCODED to 0.99 here (raised from 0.90 on 2026-07-13,
# reach-recovery: quality over volume): single source of truth, no env
# var, no fallback, one path (every install behaves identically regardless of how
# its plist was generated). Sample floor S4L_TWITTER_VIRALITY_MIN_SAMPLE default 200.
if [ -n "${S4L_SANDBOX_CANDIDATES_FILE:-}" ]; then
    # The fetch below reads /api/v1/twitter-candidates/virality-threshold under
    # THIS machine's own identity (http_api.py's X-Installation), so it would
    # reflect the OPERATOR's trailing-24h candidate pool -- the wrong baseline
    # entirely when replaying another install's (e.g. Karol's, Nhat's)
    # candidates. A sandbox test also wants every model-approved draft
    # visible, not gated to only the top-1 that would clear a live posting
    # cadence bar which, again, doesn't apply since nothing is ever posted.
    VIRALITY_THRESHOLD=""
    log "Virality bar OFF for sandbox mode (would reflect the operator's own pool, not the replayed install's; every model-approved draft stays visible)."
else
    VIRALITY_THRESHOLD=$(S4L_VPCTILE="0.99" \
        S4L_VMIN="${S4L_TWITTER_VIRALITY_MIN_SAMPLE:-200}" \
        S4L_SCRIPTS_DIR="$REPO_DIR/scripts" \
        python3 -c "
import os, sys
_repo = os.path.expanduser(os.environ.get('S4L_REPO_DIR') or os.environ.get('REPO_DIR') or '~/social-autoposter')
sys.path.insert(0, os.environ.get('S4L_SCRIPTS_DIR') or os.path.join(_repo, 'scripts'))
from http_api import api_get
try:
    r = api_get('/api/v1/twitter-candidates/virality-threshold',
                {'pctile': os.environ['S4L_VPCTILE'], 'hours': 24})
    d = (r or {}).get('data') or {}
    thr = d.get('threshold')
    n = int(d.get('sample_count') or 0)
    mn = int(os.environ['S4L_VMIN'])
    if thr is not None and n >= mn:
        print(f'{float(thr):.4f}')
except BaseException as e:
    sys.stderr.write(f'virality-bar fetch failed (bar OFF this cycle): {e}\n')
" 2>>"$LOG_FILE" || echo "")
    if [ -n "$VIRALITY_THRESHOLD" ]; then
        log "Virality bar ACTIVE: p0.99 = $VIRALITY_THRESHOLD (this install, trailing 24h); top-1 kept only if it clears the bar."
    else
        log "Virality bar OFF this cycle (cold-start/thin pool or fetch failed); top-1 kept ungated."
    fi
fi

# Parse the prep envelope and write the plan to \$PLAN_FILE; also extract the
# 'rejected' array into \$SKIP_FILE so log_twitter_skips.py can persist a
# reason against every twitter_candidates row Claude reviewed but didn't pick.
S4L_CAND_VIR="$CANDIDATES" S4L_POST_TOP_N="$POST_TOP_N" VIRALITY_THRESHOLD="$VIRALITY_THRESHOLD" python3 -c "
import json, sys, os
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
# Build candidate_id -> virality_score from the pre-scored CANDIDATES block
# (pipe cols: id|url|author|text|virality|delta|...). Shared by the top-N cap
# and the rolling virality bar below.
_vir = {}
for _ln in (os.environ.get('S4L_CAND_VIR', '') or '').splitlines():
    _p = _ln.split('|')
    if len(_p) >= 5 and _p[0].isdigit():
        try: _vir[int(_p[0])] = float(_p[4] or 0)
        except Exception: pass
# TOP-N POST CAP (2026-06-29): keep only the highest-Virality on-brand pick(s).
# S4L_POST_TOP_N=0 disables the cap (env opt-out only; the cap applies to both
# autopilot and DRAFT_ONLY lanes as of 2026-06-30). Truncated picks are dropped
# from the plan, so they stay status='pending' (NOT 'rejected'); Phase 0 salvages.
_top_n = int(os.environ.get('S4L_POST_TOP_N', '1') or '1')
_deferred = 0
if _top_n > 0 and len(candidates) > _top_n:
    candidates.sort(key=lambda c: _vir.get(c.get('candidate_id'), 0.0), reverse=True)
    _deferred = len(candidates) - _top_n
    candidates = candidates[:_top_n]
# ROLLING VIRALITY BAR (2026-07-02): drop kept pick(s) below the trailing-24h
# percentile of THIS install's candidate pool (VIRALITY_THRESHOLD, from /api/v1).
# Empty env = bar OFF: DRAFT_ONLY (new users see every draft), cold start (thin
# pool), or fetch failure. Below-bar picks are dropped like deferrals -> stay
# 'pending', never 'rejected', so Phase 0 re-judges them next cycle.
_bar = (os.environ.get('VIRALITY_THRESHOLD', '') or '').strip()
_below_bar = 0
if _bar and candidates:
    try:
        _thr = float(_bar)
        _kept = [c for c in candidates if _vir.get(c.get('candidate_id'), 0.0) >= _thr]
        _below_bar = len(candidates) - len(_kept)
        candidates = _kept
    except Exception:
        pass
# The picker assignment travels through the plan envelope so
# twitter_post_plan.py can call validate_or_register(...) with the
# original (assigned_style, assigned_mode) and coerce USE-mode drift
# back to the picker's choice (or accept the INVENT-mode invention +
# POST it to /api/v1/engagement-styles/registry). Without this, the
# post pipeline can't tell which style the picker actually assigned
# vs. what the model picked. Empty string means INVENT mode (NULL
# assigned_style in the registry-coercion contract).
# Experiment/scenario arms (2026-07-07): stamped at the SOURCE. The arm
# assignment lives in THIS cycle process (S4L_DRAFT_PROMPT_VARIANT above,
# S4L_CYCLE_LANE from the wrapper, any S4L_EXP_*), so persist it onto every
# candidate as the plan is written. This per-candidate record is the ONLY
# source downstream: review cards render it and twitter_post_plan.py stamps
# posts.draft_prompt_variant from it (env is NOT read at post time, so the
# queue-review lane stamps identically to autopilot).
sys.path.insert(0, os.path.join('$REPO_DIR', 'scripts'))
try:
    from active_experiments import collect as _collect_exps
    _exps = _collect_exps()
except Exception as _e:
    _exps = {}
    print(f'prep: experiments stamp failed: {_e}', file=sys.stderr)
for _c in candidates:
    _c['experiments'] = dict(_exps)
    # Prompt-sandbox marker (2026-07-15): lets the review card + the menu bar
    # approve flow tell a real live draft apart from a sandbox replay of a
    # historical thread, so the latter can be flagged and blocked from ever
    # triggering a real post. See the sandbox short-circuit near the top of
    # this script for how S4L_SANDBOX_CANDIDATES_FILE gets here.
    if os.environ.get('S4L_SANDBOX_CANDIDATES_FILE'):
        _c['experiments']['sandbox'] = 'true'
    # Two-draft cards (2026-07-07; no-recommendation redesign 2026-07-08):
    # mirror Draft A onto the canonical single-draft fields (reply_text/
    # engagement_style/new_style/reply_text_en/assigned_style/assigned_mode)
    # so every existing downstream consumer (tail-link baking, dedup,
    # translations, posting, drift coercion) keeps working unchanged by
    # default. Draft A is always the default, the model is never asked to
    # pick a favorite, only the reviewer's own click on the card overrides
    # it. Additionally attach a 'drafts' array for the review card to offer
    # a switch; reused/stale candidates (single draft, no draft_b) don't get
    # a drafts array, so the card falls back to today's single-draft UI.
    _rec = 'a'
    _is_reused = bool(_c.get('is_reused_draft'))
    _rec_text = _c.get(f'draft_{_rec}_text') or ''
    _rec_style = _c.get(f'draft_{_rec}_style') or ''
    _rec_new_style = _c.get(f'draft_{_rec}_new_style')
    _rec_text_en = _c.get(f'draft_{_rec}_text_en')
    _c['reply_text'] = _rec_text
    _c['engagement_style'] = _rec_style
    _c['new_style'] = _rec_new_style
    if _rec_text_en:
        _c['reply_text_en'] = _rec_text_en
    _c['assigned_style'] = ('$PICKED_STYLE' or None) if _rec == 'a' else ('$PICKED_STYLE_B' or None)
    _c['assigned_mode'] = ('$PICKED_MODE' or 'use') if _rec == 'a' else ('$PICKED_MODE_B' or 'use')
    _draft_b_text = _c.get('draft_b_text')
    if not _is_reused and _draft_b_text:
        _c['drafts'] = [
            {
                'variant': 'a', 'text': _c.get('draft_a_text') or '',
                'style': _c.get('draft_a_style') or '',
                'text_en': _c.get('draft_a_text_en'),
                'assigned_style': '$PICKED_STYLE' or None,
                'assigned_mode': '$PICKED_MODE' or 'use',
            },
            {
                'variant': 'b', 'text': _draft_b_text,
                'style': _c.get('draft_b_style') or '',
                'text_en': _c.get('draft_b_text_en'),
                'assigned_style': '$PICKED_STYLE_B' or None,
                'assigned_mode': '$PICKED_MODE_B' or 'use',
            },
        ]
json.dump({'candidates': candidates,
           'session_id': '$CLAUDE_SESSION_ID',
           'assigned_style': '$PICKED_STYLE' or None,
           'assigned_mode': '$PICKED_MODE' or 'use'}, open('$PLAN_FILE', 'w'), indent=2)
json.dump({'skips': rejected}, open('$SKIP_FILE', 'w'), indent=2)
print(f'prep: wrote {len(candidates)} candidate(s) (deferred {_deferred} lower-virality, {_below_bar} below bar) and {len(rejected)} skips to $PLAN_FILE / $SKIP_FILE', file=sys.stderr)
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

# On-demand project-winners usage marker (2026-07-10). top_performers.py
# appends a JSON line to the state-dir ledger on every --project call (the
# draft prompt tells the model to pass --invoked-by "$BATCH_ID"). Count this
# batch's lines and log a greppable marker so "did the drafting session
# actually use the per-project query" is answerable from the cycle log alone.
# Stderr-marker convention: format is load-bearing elsewhere; keep it stable.
TP_ONDEMAND=$(python3 -c "
import json, os, sys
path = os.path.join(os.environ.get('S4L_STATE_DIR', os.path.expanduser('~/.social-autoposter-mcp')), 'top-performers-invocations.jsonl')
n, projects = 0, []
try:
    for line in open(path):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get('invoked_by') == '$BATCH_ID':
            n += 1
            p = r.get('project')
            if p and p not in projects:
                projects.append(p)
except OSError:
    pass
print(f'{n} projects={projects}')
" 2>/dev/null || echo "0 projects=[]")
log "[project_top_performers] batch=$BATCH_ID on_demand_invocations=$TP_ONDEMAND"

# twitter-browser lock was already released right after thread-media capture
# (before the Claude drafting call above), since nothing from there through
# Phase 2b-gen touches the browser. Phase 2b-post re-acquires unconditionally
# below. (2026-06-16) session-lock rm removed (defect b); dead holders
# self-reclaim in twitter_browser.py now. Do NOT re-add. See Phase 1 note +
# docs/twitter_browser_lock.md.

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
    # In DRAFT_ONLY (MCP draft_cycle) mode, a non-empty PREP_REASON means the
    # prep step FAILED for a real reason (e.g. claude_not_logged_in) rather than
    # genuinely finding nothing on-brand. Surface it on stdout so the MCP wrapper
    # can tell the user the actual problem (e.g. "run claude /login") instead of
    # mis-reporting it as "all threads already engaged".
    if [ "${DRAFT_ONLY:-0}" = "1" ] && [ -n "$PREP_REASON" ]; then
        echo "DRAFT_ONLY_BLOCKED=$PREP_REASON"
    fi
    _SA_RUN_SUMMARY_EMITTED=1
    exit 0
fi

# --- Phase 2b-gen: SEO landing pages (no browser lock) ----------------------
# phase2b-gen has the longest budget (60 min) because the SEO landing-page
# build can legitimately run 10-40 min. Stamping it here is what protects
# this cycle from being salvaged out from under itself.
# Same sandbox short-circuit as the phase2b-prep advance above.
if [ -z "${S4L_SANDBOX_CANDIDATES_FILE:-}" ]; then
    python3 "$REPO_DIR/scripts/twitter_batch_phase.py" advance "$BATCH_ID" --phase phase2b-gen 2>&1 | tee -a "$LOG_FILE" || true
fi
log "Phase 2b-gen: generating SEO pages for $PLAN_COUNT candidate(s) without holding the browser lock..."
python3 "$REPO_DIR/scripts/twitter_gen_links.py" --plan "$PLAN_FILE" 2>&1 | tee -a "$LOG_FILE"
GEN_EXIT=${PIPESTATUS[0]:-1}
if [ "$GEN_EXIT" -ne 0 ]; then
    log "WARN: twitter_gen_links.py exited $GEN_EXIT, continuing with whatever links it set (per-candidate fallback to plain project URL on gen failure)."
fi

# Two-draft cards: twitter_gen_links.py (above, locked/untouched) bakes the
# tail link into reply_text in place for Draft A only (the canonical default),
# it never looks at the 'drafts' array. Sync the same suffix onto Draft B so
# switching to it on the review card still posts with the link.
# link_tail.py's apply_tail_link is documented APPEND-ONLY (concatenates the
# bridge sentence onto the unmodified reply_text, never rewrites it), so
# diffing the appended suffix and re-applying it verbatim is safe.
python3 -c "
import json
plan_path = '$PLAN_FILE'
try:
    with open(plan_path) as f:
        plan = json.load(f)
except Exception as e:
    print(f'[gen] drafts-link-sync: plan read failed: {e}')
    raise SystemExit(0)
changed = 0
for c in plan.get('candidates') or []:
    drafts = c.get('drafts')
    if not isinstance(drafts, list) or len(drafts) != 2:
        continue
    rec_idx = 0  # Draft A is always the canonical default reply_text mirrors
    other_idx = 1
    rec_draft = drafts[rec_idx]
    other_draft = drafts[other_idx]
    baked = (c.get('reply_text') or '')
    original = (rec_draft.get('text') or '')
    if baked and original and baked != original and baked.startswith(original):
        suffix = baked[len(original):]
        rec_draft['text'] = baked
        other_draft['text'] = (other_draft.get('text') or '') + suffix
        changed += 1
if changed:
    with open(plan_path, 'w') as f:
        json.dump(plan, f, indent=2)
print(f'[gen] drafts-link-sync: synced tail link onto the non-recommended draft for {changed} candidate(s)')
" 2>&1 | tee -a "$LOG_FILE"

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
    # (2026-06-16) session-lock rm removed (defect b); dead holders self-reclaim
    # in twitter_browser.py now. Do NOT re-add. See Phase 1 note + docs/twitter_browser_lock.md.
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
    # Sandbox runs have no MCP wrapper watching stdout for this marker to hand
    # the plan off to merge_review_queue.py (that handoff normally happens
    # outside this script), so a sandbox plan would otherwise just rot on
    # disk -- found live 2026-07-15, had to hand-reconstruct one after the
    # fact. $PLAN_FILE already carries the fully-transformed candidates
    # (reply_text/drafts/experiments.sandbox stamp, same shape any real draft
    # gets), so this is a straight, no-transform call into the same merge
    # step everything else uses.
    if [ -n "${S4L_SANDBOX_CANDIDATES_FILE:-}" ]; then
        python3 "$REPO_DIR/scripts/merge_review_queue.py" --plan "$PLAN_FILE" 2>&1 | tee -a "$LOG_FILE"
    fi
    # IMPORTANT: do NOT rm -f "$PLAN_FILE" here; the reviewer needs it. Print a
    # machine-readable marker so the MCP wrapper can locate the plan deterministically.
    echo "DRAFT_ONLY_PLAN=$PLAN_FILE"
    exit 0
fi

# --- Phase 2b-post: re-acquire browser lock and post ------------------------
# Stamp phase2b-post (15-min budget) before the browser-side reply loop. After
# 2b-gen's potentially long run, peer cycles' 20-min phase2a fallback would
# already be tripping if we left the row at phase2a.
# Same sandbox short-circuit as the phase2b-prep advance above.
if [ -z "${S4L_SANDBOX_CANDIDATES_FILE:-}" ]; then
    python3 "$REPO_DIR/scripts/twitter_batch_phase.py" advance "$BATCH_ID" --phase phase2b-post 2>&1 | tee -a "$LOG_FILE" || true
fi
# Always re-acquire: the lock was released right after thread-media capture
# (before Claude drafting), well before 2b-gen, so it is never still held here.
log "Re-acquiring twitter-browser lock for Phase 2b-post..."
acquire_lock "twitter-browser" 3600 2>>"$LOG_FILE"
log "twitter-browser lock held (pid=$$) Phase 2b-post"
# Drop stale singleton locks (see clean_stale_singleton.sh, also called in Phase 1 / 2b-prep).
ensure_twitter_browser_for_backend 2>&1 | tee -a "$LOG_FILE" || true
_ensure_rc="${PIPESTATUS[0]}"
[ "$_ensure_rc" != "0" ] && log "WARNING: twitter-harness bootstrap failed (rc=$_ensure_rc); continuing anyway, downstream posting may fail"

log "Phase 2b-post: posting $PLAN_COUNT candidate(s)..."
POST_OUTPUT=$("${S4L_PYTHON:-python3}" "$REPO_DIR/scripts/twitter_post_plan.py" --plan "$PLAN_FILE" 2>&1)
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
if [ -n "$S4L_TWITTER_GEN_TRACE_PATH" ] && [ -f "$S4L_TWITTER_GEN_TRACE_PATH" ]; then
    rm -f "$S4L_TWITTER_GEN_TRACE_PATH"
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
