#!/bin/bash
# Social Autoposter - Reddit comment posting via search API + CDP browser
#
# Two-lane single-pass cycle (post 2026-05-07 refactor):
#
#   SALVAGE LANE (already-vetted retries, skip ripen):
#     Phase 0 → Salvage pull → Salvage draft → Salvage post
#
#   DISCOVER LANE (fresh threads, full ripen gate):
#     Discover → Ripen (30-min delta gate, floor>=1) → Discover draft → Discover post
#
# Both lanes run every cycle. Salvage rows skip ripen because they were
# already ripened in a prior cycle (either CDP-failed mid-post or already
# delta-validated); re-ripening burns 10 min of wall-clock for no signal.
# Salvage posts FIRST so the browser lock releases before the 10-min ripen
# sleep, letting peer agents use the browser during the wait.
#
# Browser lock is held PER ROW inside post_reddit.py's `_post_iteration`
# (acquire just before `post_via_cdp`, release in finally right after). The
# pre-flight at the top of this script does a one-shot ensure_browser_healthy
# (orphan-Chrome sweep, Singleton-lock clear) under a brief 30s lease so the
# rest of the cycle can rely on a clean profile. Migrated 2026-05-13 from the
# previous design that held the lease around the whole `--phase post` call —
# that monopolised the browser for the full batch (~30 min for 10 rows) while
# peer reddit pipelines sat blocked through every 3-min between-post sleep.
#
# Called by launchd every 15 minutes via run-reddit-search-launchd.sh.

set -euo pipefail

# SAPS_->S4L_ env mirror (brand rename 2026-07-03): old plists/tasks still
# export SAPS_*; new code reads S4L_*. Copy names, never values via eval.
while IFS='=' read -r _k _; do
  case "$_k" in SAPS_*) _n="S4L_${_k#SAPS_}"; eval "[ -n \"\${$_n+x}\" ] || export $_n=\"\${$_k}\"";; esac
done <<EOF_ENV
$(env | grep '^SAPS_' | cut -d= -f1 | sed 's/$/=/')
EOF_ENV

# Honor S4L_REPO_DIR (customer boxes run the materialized package copy, not
# ~/social-autoposter; the MCP-managed launchd plist bakes S4L_REPO_DIR in).
# Fallback keeps the operator Mac's hand-built plist working unchanged.
REPO_DIR="${S4L_REPO_DIR:-$HOME/social-autoposter}"

[ -f "$REPO_DIR/.env" ] && source "$REPO_DIR/.env"

LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run-reddit-search-$(date +%Y-%m-%d_%H%M%S).log"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

log "=== Reddit Search Post Run: $(date) ==="

source "$REPO_DIR/skill/lock.sh"

# Reddit-harness backend (2026-05-29 migration): Reddit started 403ing urllib/
# curl on *.json from residential IPs, so the whole Reddit pipeline now rides a
# dedicated browser-harness Chrome on port 9557 (profile reddit-harness),
# mirroring twitter-harness. Sourcing this exports REDDIT_CDP_URL so every child
# Python proc (reddit_tools.py discovery fetch, reddit_browser.py posting) attaches
# directly to the harness instead of ps-scanning for the reddit-agent MCP Chrome.
source "$REPO_DIR/skill/lib/reddit-backend.sh"

LIMIT=10
EXCLUDE=""
TOTAL_POSTED=0
TOTAL_FAILED=0
TOTAL_SKIPPED=0
TOTAL_SALVAGED=0  # actual salvaged decisions (rows pulled + drafted/posted) this cycle
TOTAL_CANDIDATES=0  # total reddit_candidates rows touched (discovered + salvaged)
RUN_START=$(date +%s)
FAILURE_REASONS=""

# Helper: parse `posted=N failed=M` from post-phase stdout. Returns "posted failed"
# on stdout. Caller MUST do the TOTAL_* accumulation in the parent shell;
# previously this function tried to mutate TOTAL_POSTED/TOTAL_FAILED itself,
# but bash's $() captures spawn a subshell where mutations to the parent's
# variables are silently lost. The 21:01 salvage lane really posted 4 to DB
# but run_monitor.log showed posted=0 because of this bug. (Fixed 2026-05-07.)
_parse_post_results() {
    local out="$1"
    local rc="$2"
    if [ "$rc" = "0" ]; then
        local posted failed
        posted=$(echo "$out" | grep -oE 'posted=[0-9]+' | tail -1 | cut -d= -f2 || echo 0)
        failed=$(echo "$out" | grep -oE 'failed=[0-9]+' | tail -1 | cut -d= -f2 || echo 0)
        echo "${posted:-0} ${failed:-0}"
    else
        # CRITICAL: write directly to LOG_FILE, NEVER to stdout. This function's
        # stdout is captured by $(...) in the caller's `read SALVAGE_POSTED ...`,
        # so any stray timestamp here corrupts arithmetic at TOTAL_POSTED += $X.
        # Bug observed 2026-05-08: a leaked "[14:57:04] Post phase: ..." line
        # produced "TOTAL_POSTED + [14:57:04]: syntax error" and `set -e` aborted
        # the script BEFORE the discover lane ran.
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Post phase: exit code $rc; counting as failed." >> "$LOG_FILE"
        echo "0 1"
    fi
}

# Helper: parse CDP failure reasons from post-phase stdout and accumulate
# into FAILURE_REASONS (Twitter pipeline schema). Mirrored across both lanes.
_accumulate_cdp_reasons() {
    local out="$1"
    while IFS= read -r line; do
        local cdp_key
        cdp_key=$(echo "$line" | grep -oE '\[post_reddit\] CDP FAILED: [a-z_]+' | awk '{print $NF}')
        case "$cdp_key" in
            thread_locked)          add_reason reddit_locked ;;
            thread_archived)        add_reason reddit_archived ;;
            thread_not_found)       add_reason reddit_deleted ;;
            account_blocked_in_sub) add_reason account_blocked ;;
            not_logged_in)          add_reason reddit_logged_out ;;
            all_attempts_failed)    add_reason cdp_no_response ;;
            comment_box_not_found)  add_reason comment_box_missing ;;
            "")                     : ;;
            *)                      add_reason "cdp_${cdp_key}" ;;
        esac
    done <<< "$out"
}

# Idempotent run_monitor.log emitter wired to EXIT/INT/TERM/HUP. Without this,
# a SIGTERM landing between the post phase (where post_reddit.py has already
# committed to the `posts` table via log_post) and the historical inline
# log_run.py call at the bottom of the script silently drops the run from
# run_monitor.log. The dashboard reads run_monitor.log, so the operator-
# visible "last post_reddit cycle" stays stuck on a stale entry while real
# posts continue landing in the DB unrecorded. Concretely: in one observed
# 4-cycle window, three of four 15-min cycles SIGTERMed mid-post and the
# dashboard surfaced none of the two posts (r/aiToolForBusiness,
# r/SideProject) that DID land in `posts`.
#
# Mechanism:
#   - The function reads the cycle's accumulator globals (TOTAL_*,
#     FAILURE_REASONS, RUN_START) and shells out to scripts/log_run.py with
#     the same arg shape the historical inline call used.
#   - _SA_RUN_SUMMARY_EMITTED guards against double-write: the happy path
#     calls the function explicitly once at the bottom (so cost can be
#     computed without the trap's 10s timeout), and the trap fires on EXIT
#     to catch SIGTERM/error paths. The flag makes either order a no-op
#     after first emission.
#   - On SIGTERM the get_run_cost.py call is wrapped in `timeout 10` so a
#     hung Postgres query doesn't wedge the trap; cost falls back to 0.0000.
#
# Trap chaining: lock.sh sourced above already installed `_sa_release_locks`
# on EXIT INT TERM HUP. Bash trap REPLACES, not appends, so we re-set with
# both handlers explicitly. Order matters: emit summary first (it shells
# out, harmless if locks are still held), then release locks. _sa_release_locks
# is defined by lock.sh and stays in scope after sourcing.
_SA_RUN_SUMMARY_EMITTED=0
_SA_PRECOMPUTED_COST=""
_sa_emit_run_summary_oneshot() {
    [ "${_SA_RUN_SUMMARY_EMITTED:-0}" = "1" ] && return 0
    _SA_RUN_SUMMARY_EMITTED=1
    local elapsed cost
    elapsed=$(( $(date +%s) - ${RUN_START:-$(date +%s)} ))
    if [ -n "${_SA_PRECOMPUTED_COST:-}" ]; then
        cost="$_SA_PRECOMPUTED_COST"
    else
        # Prefer cycle_id when BATCH_ID is set (after Phase 0). Falls back to
        # the legacy since+scripts query if the trap fires before BATCH_ID was
        # initialised (very early SIGTERM, e.g. from a stale .env source).
        if [ -n "${BATCH_ID:-}" ]; then
            cost=$(timeout 10 python3 "$REPO_DIR/scripts/get_run_cost.py" \
                        --cycle-id "$BATCH_ID" 2>/dev/null || echo "0.0000")
        else
            cost=$(timeout 10 python3 "$REPO_DIR/scripts/get_run_cost.py" \
                        --since "${RUN_START:-0}" \
                        --scripts "post_reddit" 2>/dev/null || echo "0.0000")
        fi
    fi
    # Rescue Anthropic-side failures the per-phase add_reason cascade didn't
    # catch (stream_idle_timeout, monthly_limit, api_overloaded,
    # context_overflow, credit_balance, generic api_error). Scans the cycle
    # log only when TOTAL_FAILED>0 AND FAILURE_REASONS is still empty — so
    # the historical per-phase keys (reddit_locked, account_blocked, etc.)
    # stay authoritative when they fired, and the classifier fills in the
    # gap when Claude died before any phase emitted a reason.
    if [ "${TOTAL_FAILED:-0}" -gt 0 ] && [ -z "${FAILURE_REASONS:-}" ] \
        && [ -n "${LOG_FILE:-}" ] && [ -f "${LOG_FILE:-}" ]; then
        local api_reason
        api_reason=$(python3 "$REPO_DIR/scripts/classify_run_error.py" "$LOG_FILE" 2>/dev/null)
        [ -n "$api_reason" ] && FAILURE_REASONS="${api_reason}:1"
    fi
    local args
    args=(--script "post_reddit" \
          --posted "${TOTAL_POSTED:-0}" \
          --skipped "${TOTAL_SKIPPED:-0}" \
          --failed "${TOTAL_FAILED:-0}" \
          --cost "$cost" \
          --elapsed "$elapsed")
    [ "${TOTAL_SALVAGED:-0}" -gt 0 ] && args+=(--salvaged "$TOTAL_SALVAGED")
    [ "${TOTAL_CANDIDATES:-0}" -gt 0 ] && args+=(--candidates "$TOTAL_CANDIDATES")
    [ -n "${FAILURE_REASONS:-}" ] && args+=(--failure-reasons "$FAILURE_REASONS")
    python3 "$REPO_DIR/scripts/log_run.py" "${args[@]}" 2>/dev/null || true
}
_sa_release_lease_oneshot() {
    # Belt-and-suspenders for SIGTERM/crash paths: free the reddit-browser
    # lease in case post_reddit.py died mid-post and didn't get to the explicit
    # release. Idempotent (NOT_HELD is fine). Safe to call even if we never
    # acquired the lease this run. Bounded 3s so a hung helper can't stall the
    # trap. Without this, a SIGTERM mid-post would leave the lease alive for
    # ~90s before peers could steal it; with this, peers proceed within seconds.
    timeout 3 python3 "$REPO_DIR/scripts/reddit_browser_lock.py" release 2>/dev/null || true
}
trap '_sa_emit_run_summary_oneshot; _sa_release_lease_oneshot; _sa_release_locks' EXIT INT TERM HUP

# Cycle-level batch_id, mirrors the Twitter cycle's twcycle-* convention.
# Used by --phase phase0 / --phase salvage / --phase discover to attribute
# rows in reddit_candidates and to drive the persistent retry queue.
BATCH_ID="rdcycle-$(date +%Y%m%d-%H%M%S)"
log "Cycle batch_id=$BATCH_ID"

# --- Draft-only mode (2026-07-14, mirrors run-draft-and-publish.sh) ---------
# The ONE mode flag (scripts/s4l_mode.py draft-only, stdout 1/0) now governs
# Reddit exactly like the X cycle: when ON, both lanes stop after the draft
# phase and merge their drafted decisions into the menu-bar review cards
# (merge_review_queue.py --reddit-plan); posting then happens one approved
# card at a time via the plugin's post_drafts tool, which reconstructs a
# one-decision plan and reuses `post_reddit.py --phase post` unchanged. When
# OFF (operator opt-out), the lanes post autonomously below as always.
# Fail-safe default is 1 (draft-only) — same posture as the X wrapper.
DRAFT_ONLY_FLAG="$(python3 "$REPO_DIR/scripts/s4l_mode.py" draft-only 2>/dev/null || echo 1)"
log "Draft-only flag: $DRAFT_ONLY_FLAG"

# Posting-active defer (2026-07-14, mirrors run-draft-and-publish.sh): an
# MCP-approval poster mid-drain owns the ONE shared harness tab; running the
# cycle's discovery/prefetch now would navigate the tab out from under it and
# false-positive the poster's comment-form check. The backend-level defer
# hook can't cover THIS script (its ensure failure falls back to
# ensure_browser_healthy and proceeds), so check explicitly and skip the
# whole fire; launchd re-fires in 15 minutes. Stale flags (heartbeat older
# than 120s) never block.
_S4L_PA_FILE="${S4L_STATE_DIR:-$HOME/.social-autoposter-mcp}/reddit-posting-active.json"
if [ -f "$_S4L_PA_FILE" ]; then
    _S4L_PA_AGE=$(( $(date +%s) - $(stat -f %m "$_S4L_PA_FILE" 2>/dev/null || echo 0) ))
    if [ "$_S4L_PA_AGE" -lt 120 ]; then
        log "Reddit posting-active (age ${_S4L_PA_AGE}s); skipping this cycle fire."
        exit 0
    fi
fi

# Merge a drafted plan into the review cards (draft-only lanes). $1 = plan
# file, $2 = lane label. merge_review_queue consumes (deletes) the plan file.
_merge_reddit_drafts_to_cards() {
    local _plan_file="$1" _lane="$2"
    log "$_lane lane: draft-only ON; merging drafted decision(s) into review cards (no autonomous post)."
    python3 "$REPO_DIR/scripts/merge_review_queue.py" --reddit-plan "$_plan_file" 2>&1 | tee -a "$LOG_FILE" || true
}

# Export the same id as SA_CYCLE_ID so every Claude session spawned downstream
# (post_reddit.py → run_claude(), run_claude.sh → claude -p, log_claude_session.py)
# stamps claude_sessions.cycle_id with this cycle. Without this, concurrent
# overlapping cycles (double-fork wrapper added 2026-04-30 lets cycles stack)
# all share the same script tag 'post_reddit' and get_run_cost.py was summing
# costs across every cycle in the time window, producing absurd $150+ per-cycle
# reports (observed 2026-05-10: 11:00 cycle reported $166 when its own work
# was ~$32; the rest belonged to the 11:15/11:30/11:45 cycles that started
# during the same window). cycle_id makes per-cycle cost attribution accurate.
export SA_CYCLE_ID="$BATCH_ID"

# --- Pre-flight: orphan-Chrome sweep + singleton-lock clear, ONCE per cycle ---
# Lock strategy (migrated 2026-05-13): per-post acquire/release happens INSIDE
# post_reddit.py's `_post_iteration` for loop (around each `post_via_cdp` call),
# not here. Holding the lease around the whole `--phase post` invocation meant
# a 10-row salvage batch monopolised the browser for ~30 min (10 × ~45s post +
# 9 × 180s between-post sleep) while peer reddit pipelines (link-edit-reddit,
# dm-outreach-reddit, engage-reddit, engage-dm-replies-reddit) sat blocked.
# Mirrors the link-edit-reddit.sh / dm-outreach-reddit.sh pattern shipped
# 2026-05-08 → 2026-05-10.
#
# The brief acquire+ensure_browser_healthy+release below runs ONCE so
# ensure_browser_healthy's CDP probe / wait-for-orphan-exit / Singleton-lock
# clear happens before the cycle starts. ensure_browser_healthy is bash so we
# can't easily call it from Python; orphan-Chrome sweep ALSO runs inside the
# Python lock helper's acquire path (sweep_orphan_browser_processes), so the
# per-post lease still gets that protection. Pre-flight is best-effort: if
# acquire is BUSY (peer pipeline mid-post), warn and proceed; Python per-row
# acquire will retry inside the for loop.
log "Pre-flight: brief reddit-browser acquire + harness bootstrap + release..."
python3 "$REPO_DIR/scripts/reddit_browser_lock.py" acquire --timeout 60 --ttl 30 2>&1 | tee -a "$LOG_FILE" || \
    log "WARNING: pre-flight acquire BUSY; harness bootstrap will run anyway; per-row acquires inside post_reddit.py will retry."
# reddit-harness bootstrap: probe + launch the dedicated harness Chrome on port
# 9557 (profile reddit-harness) if down, then clean leftover tabs. Replaces the
# old ensure_browser_healthy "reddit" ps-scan-for-MCP-Chrome path; the harness is
# the single browser the whole Reddit pipeline now rides (REDDIT_CDP_URL points
# every child Python proc at it). Falls back to ensure_browser_healthy on failure.
ensure_reddit_browser_for_backend 2>&1 | tee -a "$LOG_FILE" || true
_ensure_rc="${PIPESTATUS[0]}"
if [ "$_ensure_rc" = "78" ]; then
    # Reserved skip code (harness-common.sh:182-185): the pre-launch hook
    # deliberately deferred because a poster is mid-drain on the ONE shared
    # tab. This is NOT a bootstrap failure — falling back to
    # ensure_browser_healthy here (as this used to, treating ANY nonzero rc
    # as broken) is wrong on two counts: it misreports a clean defer as a
    # warning, and ensure_browser_healthy checks the pre-harness
    # browser-profiles/reddit path (dead since the 2026-05-29 migration), so
    # it is pure wasted work every time cycles overlap around an active post
    # (routine — see the "double-fork wrapper" note above BATCH_ID).
    log "reddit-harness bootstrap deferred (rc=78, poster active elsewhere); skipping this cycle so the poster keeps the tab."
    python3 "$REPO_DIR/scripts/reddit_browser_lock.py" release 2>/dev/null || true
    exit 0
elif [ "$_ensure_rc" != "0" ]; then
    log "WARNING: reddit-harness bootstrap failed (rc=$_ensure_rc); falling back to ensure_browser_healthy reddit"
    ensure_browser_healthy "reddit"
fi
python3 "$REPO_DIR/scripts/reddit_browser_lock.py" release 2>/dev/null || true

# --- Phase 0: hard-expire stale pending rows + salvage truly-orphaned rows ---
# Pending rows from prior cycles fall into two buckets:
#   - discovered_at older than FRESHNESS_HOURS (24h) -> hard-expire
#   - still-fresh AND attempt_count < MAX_ATTEMPTS (3) AND last_attempt_at
#     older than RETRY_BACKOFF (30m) -> re-assign to this batch so the loop
#     below can pull them via --phase salvage.
#
# Mirrors run-twitter-cycle.sh's Phase 0 in shape, but with Reddit-tuned
# windows (24h FRESHNESS vs Twitter 6h, since Reddit threads stay actionable
# longer). All the SQL lives in post_reddit.py:_db_phase0_salvage() under a
# pg_advisory_xact_lock so two concurrent Reddit cycles can't double-salvage.
#
# Output is `expired=N salvaged=M` on a single line; we parse it inline.
PHASE0_OUT=$(python3 "$REPO_DIR/scripts/post_reddit.py" --phase phase0 --batch-id "$BATCH_ID" 2>&1 | tee -a "$LOG_FILE" | tail -1)
PHASE0_EXPIRED=$(echo "$PHASE0_OUT" | grep -oE 'expired=[0-9]+' | cut -d= -f2 || echo 0)
PHASE0_SALVAGED=$(echo "$PHASE0_OUT" | grep -oE 'salvaged=[0-9]+' | cut -d= -f2 || echo 0)
[ "${PHASE0_EXPIRED:-0}" -gt 0 ] && log "Phase 0: hard-expired $PHASE0_EXPIRED pending rows older than 24h"
[ "${PHASE0_SALVAGED:-0}" -gt 0 ] && log "Phase 0: salvaged $PHASE0_SALVAGED orphaned pending rows into $BATCH_ID"

# Add a reason:count pair to FAILURE_REASONS (same schema as Twitter pipeline).
# Accumulates counts for duplicate keys (e.g. two thread_locked failures).
add_reason() {
    local key="$1" count="${2:-1}"
    # Extract existing count for this key and add to it
    local existing
    existing=$(echo "$FAILURE_REASONS" | tr ',' '\n' | grep "^${key}:" | cut -d: -f2 | head -1)
    if [ -n "$existing" ]; then
        local new_count=$(( existing + count ))
        FAILURE_REASONS=$(echo "$FAILURE_REASONS" | tr ',' '\n' | grep -v "^${key}:" | tr '\n' ',' | sed 's/,$//;s/^,//')
        FAILURE_REASONS="${FAILURE_REASONS:+$FAILURE_REASONS,}${key}:${new_count}"
    else
        FAILURE_REASONS="${FAILURE_REASONS:+$FAILURE_REASONS,}${key}:${count}"
    fi
}

# =============================================================================
# SALVAGE LANE — already-vetted retries, skip ripen, post early
# =============================================================================
# Salvage rows were ripened (and survived, or CDP-failed mid-post) in a prior
# cycle. Re-ripening them now would burn 10 min of wall-clock for stale signal.
# Pull up to LIMIT rows from one project, draft any that lack a fresh persisted
# draft, then post. Lock is held briefly here so peers can use the browser
# during the discover lane's 10-min ripen sleep below.
SALVAGE_FILE=$(mktemp -t post_reddit_salvage.XXXXXX.json)
SALVAGE_DRAFT_FILE=$(mktemp -t post_reddit_salvage_draft.XXXXXX.json)
HAS_SALVAGE=0
SALVAGE_COUNT=0

set +e
python3 "$REPO_DIR/scripts/post_reddit.py" \
    --phase salvage \
    --batch-id "$BATCH_ID" \
    --limit "$LIMIT" \
    --out "$SALVAGE_FILE" 2>&1 | tee -a "$LOG_FILE"
SALVAGE_RC=${PIPESTATUS[0]}
set -e

if [ "$SALVAGE_RC" = "0" ]; then
    SALVAGE_COUNT=$(python3 -c "import json;print(len(json.load(open('$SALVAGE_FILE')).get('decisions',[])))" 2>/dev/null || echo 1)
    HAS_SALVAGE=1
    TOTAL_SALVAGED=$((TOTAL_SALVAGED + ${SALVAGE_COUNT:-1}))
    TOTAL_CANDIDATES=$((TOTAL_CANDIDATES + ${SALVAGE_COUNT:-1}))
    log "Salvage lane: pulled $SALVAGE_COUNT candidate(s); skipping ripen."
else
    log "Salvage lane: nothing to salvage this cycle (rc=$SALVAGE_RC)."
fi

# --- Salvage draft (no browser; skips rows with fresh persisted draft_text) ---
if [ "$HAS_SALVAGE" = "1" ]; then
    log "Salvage lane: drafting $SALVAGE_COUNT candidate(s)..."
    set +e
    python3 "$REPO_DIR/scripts/post_reddit.py" \
        --phase draft \
        --in "$SALVAGE_FILE" \
        --out "$SALVAGE_DRAFT_FILE" 2>&1 | tee -a "$LOG_FILE"
    SALVAGE_DRAFT_RC=${PIPESTATUS[0]}
    set -e

    case "$SALVAGE_DRAFT_RC" in
        0) : ;;
        5) log "Salvage draft: Claude failed; skipping salvage post."; HAS_SALVAGE=0; TOTAL_FAILED=$((TOTAL_FAILED + ${SALVAGE_COUNT:-1})) ;;
        6) log "Salvage draft: no drafted decisions; skipping salvage post."; HAS_SALVAGE=0; TOTAL_SKIPPED=$((TOTAL_SKIPPED + ${SALVAGE_COUNT:-1})) ;;
        *) log "Salvage draft: rc=$SALVAGE_DRAFT_RC; skipping salvage post."; HAS_SALVAGE=0; TOTAL_FAILED=$((TOTAL_FAILED + ${SALVAGE_COUNT:-1})) ;;
    esac
fi

# --- Salvage post (per-row lease handled inside post_reddit.py) ---
# Lock strategy (migrated 2026-05-13): the reddit-browser lease is now
# acquired/released PER ROW inside post_reddit.py's `_post_iteration` for
# loop, around each `post_via_cdp` call. We no longer hold the lease around
# the whole `--phase post` invocation — that monopolised the browser for the
# entire batch (~30 min for 10 rows) while peers sat blocked. The pre-flight
# at the top of this script already did the one-shot ensure_browser_healthy
# work; per-row acquires inside Python handle the rest.
if [ "$HAS_SALVAGE" = "1" ] && [ "$DRAFT_ONLY_FLAG" = "1" ]; then
    _merge_reddit_drafts_to_cards "$SALVAGE_DRAFT_FILE" "Salvage"
    HAS_SALVAGE=0
fi
if [ "$HAS_SALVAGE" = "1" ]; then
    log "Salvage lane: posting $SALVAGE_COUNT candidate(s) (per-row reddit-browser lease)..."

    # set +e covers the entire post + cleanup block. Discover lane MUST run
    # every cycle (per design comment at line 263), so any failure in salvage
    # cleanup (_parse_post_results contamination, arithmetic over malformed
    # reads, _accumulate_cdp_reasons) must NOT abort the script.
    # 2026-05-08 bug: cycle 16:38 finished salvage (posted=2) then died on a
    # set -e trap mid-cleanup; discover lane never ran for the rest of the day.
    set +e
    SALVAGE_POST_OUT=$(python3 "$REPO_DIR/scripts/post_reddit.py" --phase post --in "$SALVAGE_DRAFT_FILE" 2>&1 | tee -a "$LOG_FILE")
    SALVAGE_POST_RC=${PIPESTATUS[0]}

    # Parse + accumulate in parent shell. $() spawns a subshell, so we must
    # do the TOTAL_* increments AFTER capturing the helper's stdout.
    read -r SALVAGE_POSTED SALVAGE_FAILED <<< "$(_parse_post_results "$SALVAGE_POST_OUT" "$SALVAGE_POST_RC")"
    TOTAL_POSTED=$((TOTAL_POSTED + ${SALVAGE_POSTED:-0}))
    TOTAL_FAILED=$((TOTAL_FAILED + ${SALVAGE_FAILED:-0}))
    log "Salvage lane: posted=$SALVAGE_POSTED failed=$SALVAGE_FAILED"
    _accumulate_cdp_reasons "$SALVAGE_POST_OUT"
    set -e
fi

# =============================================================================
# DISCOVER LANE — fresh threads, full ripen gate
# =============================================================================
# Discover always runs every cycle (independent of salvage). Picks one project
# via select_project.py, fans out search topics, and emits all matching threads
# as candidates for ripen. The 10-min ripen sleep happens here; salvage
# already finished posting above so this sleep doesn't block any output.
#
# Project-scoped subreddit excludes (added 2026-05-11): post_reddit.py's
# discover phase logs `[project_excludes] platform=reddit project=...
# active_subs=N active_keywords=N subs=[...] keywords=[...]` for visibility.
# Enforcement happens server-side inside reddit_tools._load_comment_blocked_
# subs via the S4L_REDDIT_PROJECT env var that post_reddit.py exports below.
# Claude's draft prompt can propose new subreddit:<slug> excludes when it
# rejects a thread; they accumulate in project_search_excludes and go live
# after >=2 distinct batch_ids propose them (activation gate). See
# scripts/project_excludes.py for the full spec.
DISCOVER_FILE=$(mktemp -t post_reddit_discover.XXXXXX.json)
RIPEN_FILE=$(mktemp -t post_reddit_ripened.XXXXXX.json)
DISCOVER_DRAFT_FILE=$(mktemp -t post_reddit_discover_draft.XXXXXX.json)
HAS_DISCOVER=0

set +e
python3 "$REPO_DIR/scripts/post_reddit.py" \
    --phase discover \
    --batch-id "$BATCH_ID" \
    --out "$DISCOVER_FILE" \
    --exclude "$EXCLUDE" \
    --limit "$LIMIT" 2>&1 | tee -a "$LOG_FILE"
DISCOVER_RC=${PIPESTATUS[0]}
set -e

case "$DISCOVER_RC" in
    0)
        DISCOVER_COUNT=$(python3 -c "import json;print(len(json.load(open('$DISCOVER_FILE')).get('decisions',[])))" 2>/dev/null || echo 0)
        TOTAL_CANDIDATES=$((TOTAL_CANDIDATES + DISCOVER_COUNT))
        HAS_DISCOVER=1
        log "Discover lane: found $DISCOVER_COUNT candidate(s)."
        ;;
    3) log "Discover lane: rate-limited; skipping discover this cycle." ;;
    4) log "Discover lane: no eligible project; skipping discover this cycle." ;;
    5) log "Discover lane: Claude failed; counting as failed."; TOTAL_FAILED=$((TOTAL_FAILED + 1)) ;;
    6) log "Discover lane: no candidates found; counting as skipped."; TOTAL_SKIPPED=$((TOTAL_SKIPPED + 1)) ;;
    *) log "Discover lane: unexpected rc=$DISCOVER_RC; counting as failed."; TOTAL_FAILED=$((TOTAL_FAILED + 1)) ;;
esac

# --- Rank + cap discover output (ripen stage RETIRED 2026-06-01) ---
# The 30-min ripen momentum gate (ripen_reddit_plan.py: T0 capture → sleep 1800s
# → T1 repoll → composite Δup+4·Δcomments floor) was removed to align with the
# Twitter pipeline, which dropped its own inter-phase momentum sleep on
# 2026-05-31 (variant D won: no wait, just expire→discover+score→draft→post).
# Two failure modes the ripen stage caused, both fixed by removing it:
#   1. repoll() had a hard 120s subprocess timeout on T1 re-fetch of the WHOLE
#      candidate set; at ~75+ candidates it timed out → returned {} → every
#      candidate dropped → zero posts (S4L 08:15, mk0r 08:30 on 2026-06-01).
#   2. mature long-tail threads that are genuinely on-topic but not gaining
#      fresh upvotes in a 30-min window were momentum-starved and dropped
#      (Podlog 08:00), even though they were the RIGHT threads to comment on.
# Ranking + capping now happens inside post_reddit.py --phase discover
# (_discover_iteration): candidates are scored by topical overlap (query vs.
# thread title+selftext) to fight the sort=relevance leak, then capped to the
# top S4L_REDDIT_DISCOVER_CAP (default 25). The final post cap is still
# enforced by _post_iteration (S4L_REDDIT_MAX_POSTS_PER_CYCLE, default 10).
# RIPEN_FILE is kept as a passthrough alias so the draft/cleanup paths below
# stay unchanged.
if [ "$HAS_DISCOVER" = "1" ]; then
    cp "$DISCOVER_FILE" "$RIPEN_FILE"
    SURVIVORS=$(python3 -c "import json;print(len(json.load(open('$RIPEN_FILE')).get('decisions',[])))" 2>/dev/null || echo 0)
    if [ "$SURVIVORS" = "0" ]; then
        log "Discover lane: 0 candidates after rank+cap; skipping discover draft+post."
        TOTAL_SKIPPED=$((TOTAL_SKIPPED + 1))
        HAS_DISCOVER=0
    else
        log "Discover lane: $SURVIVORS candidate(s) ranked + capped (no ripen wait)."
    fi
fi

# --- Discover draft ---
if [ "$HAS_DISCOVER" = "1" ]; then
    log "Discover lane: drafting $SURVIVORS candidate(s)..."
    set +e
    python3 "$REPO_DIR/scripts/post_reddit.py" \
        --phase draft \
        --in "$RIPEN_FILE" \
        --out "$DISCOVER_DRAFT_FILE" 2>&1 | tee -a "$LOG_FILE"
    DRAFT_RC=${PIPESTATUS[0]}
    set -e

    case "$DRAFT_RC" in
        0) : ;;
        5) log "Discover draft: Claude failed."; TOTAL_FAILED=$((TOTAL_FAILED + 1)); HAS_DISCOVER=0 ;;
        6) log "Discover draft: no drafted decisions."; TOTAL_SKIPPED=$((TOTAL_SKIPPED + 1)); HAS_DISCOVER=0 ;;
        *) log "Discover draft: rc=$DRAFT_RC."; TOTAL_FAILED=$((TOTAL_FAILED + 1)); HAS_DISCOVER=0 ;;
    esac
fi

# --- Discover post (per-row lease handled inside post_reddit.py) ---
# Same per-row lease pattern as the salvage block above (see comment there for
# rationale). The lease is acquired/released around each post_via_cdp call
# inside `_post_iteration`, NOT around the whole --phase post invocation.
if [ "$HAS_DISCOVER" = "1" ] && [ "$DRAFT_ONLY_FLAG" = "1" ]; then
    _merge_reddit_drafts_to_cards "$DISCOVER_DRAFT_FILE" "Discover"
    HAS_DISCOVER=0
fi
if [ "$HAS_DISCOVER" = "1" ]; then
    log "Discover lane: posting $SURVIVORS survivor(s) (per-row reddit-browser lease)..."

    # set +e covers the entire post + cleanup block. The script must reach
    # the trap-installed cost emitter at the bottom even if discover cleanup
    # errors (mirrors the salvage block above).
    set +e
    DISCOVER_POST_OUT=$(python3 "$REPO_DIR/scripts/post_reddit.py" --phase post --in "$DISCOVER_DRAFT_FILE" 2>&1 | tee -a "$LOG_FILE")
    DISCOVER_POST_RC=${PIPESTATUS[0]}

    read -r DISCOVER_POSTED DISCOVER_FAILED <<< "$(_parse_post_results "$DISCOVER_POST_OUT" "$DISCOVER_POST_RC")"
    TOTAL_POSTED=$((TOTAL_POSTED + ${DISCOVER_POSTED:-0}))
    TOTAL_FAILED=$((TOTAL_FAILED + ${DISCOVER_FAILED:-0}))
    log "Discover lane: posted=$DISCOVER_POSTED failed=$DISCOVER_FAILED"
    _accumulate_cdp_reasons "$DISCOVER_POST_OUT"
    set -e
fi

rm -f "$SALVAGE_FILE" "$SALVAGE_DRAFT_FILE" "$DISCOVER_FILE" "$RIPEN_FILE" "$DISCOVER_DRAFT_FILE"

ELAPSED=$(( $(date +%s) - RUN_START ))
# Sum claude_sessions.total_cost_usd for every post_reddit session started
# during this cycle. Mirrors run-twitter-cycle.sh / run-linkedin.sh; the
# script value here is the same tag passed to log_claude_session.py inside
# scripts/post_reddit.py (~line 1141). Falls back to 0.0000 if the DB is
# unreachable so the dashboard never shows blank.
_COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --cycle-id "$BATCH_ID" 2>/dev/null || echo "0.0000")
log "=== Run summary: posted=$TOTAL_POSTED failed=$TOTAL_FAILED skipped=$TOTAL_SKIPPED salvaged=$TOTAL_SALVAGED candidates=$TOTAL_CANDIDATES projects=[$EXCLUDE] cost=\$$_COST elapsed=${ELAPSED}s ==="

# Hand the precomputed cost to the trap-installed emitter so the happy path
# pays the (slow) Postgres query once, without the 10s clamp the SIGTERM path
# uses. _sa_emit_run_summary_oneshot is idempotent; the EXIT trap will
# no-op after this call.
_SA_PRECOMPUTED_COST="$_COST"
_sa_emit_run_summary_oneshot

log "=== Done: $(date) ==="
