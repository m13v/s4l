#!/bin/bash
# Social Autoposter - Reddit comment posting via search API + CDP browser
#
# 4-phase pipeline per iteration (mirrors Twitter's discover/T1/post split):
#   1. Discover  - Claude searches and selects threads only (no drafting, no browser)
#   2. Ripen     - T0 snapshot, 5-min sleep, T1 re-poll, composite delta gate
#   3. Draft     - Claude writes comments ONLY for ripen-survivors (no browser)
#   4. Post      - CDP browser posts survivors with drafted text
#
# Browser lock is held ONLY around post phase. All other phases run unlocked
# so peers (dm-outreach, link-edit, engage-dm-replies, audit) can use the
# browser during our HTTP/Claude work.
#
# Called by launchd every 15 minutes via run-reddit-search-launchd.sh.

set -euo pipefail

[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run-reddit-search-$(date +%Y-%m-%d_%H%M%S).log"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

log "=== Reddit Search Post Run: $(date) ==="

source "$REPO_DIR/skill/lock.sh"

ITERATIONS=5
LIMIT=1
EXCLUDE=""
TOTAL_POSTED=0
TOTAL_FAILED=0
TOTAL_SKIPPED=0
TOTAL_SALVAGED=0  # how many iterations this cycle ran a salvaged candidate
TOTAL_CANDIDATES=0  # total reddit_candidates rows touched (discovered + salvaged)
RUN_START=$(date +%s)
FAILURE_REASONS=""

# Cycle-level batch_id, mirrors the Twitter cycle's twcycle-* convention.
# Used by --phase phase0 / --phase salvage / --phase discover to attribute
# rows in reddit_candidates and to drive the persistent retry queue.
BATCH_ID="rdcycle-$(date +%Y%m%d-%H%M%S)"
log "Cycle batch_id=$BATCH_ID"

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

for i in $(seq 1 "$ITERATIONS"); do
    log "--- Iteration $i/$ITERATIONS ---"
    DISCOVER_FILE=$(mktemp -t post_reddit_discover.XXXXXX.json)
    ITER_SALVAGED=0  # 1 = this iteration is replaying a salvaged candidate

    # Salvage-first: try to pull a pending row that Phase 0 re-assigned to
    # this batch BEFORE paying the discover cost. Salvaged rows skip the
    # Claude discover spend entirely; ripen re-measures fresh deltas, draft
    # reuses any persisted text (<60min old), and post retries the CDP step.
    set +e
    python3 "$REPO_DIR/scripts/post_reddit.py" \
        --phase salvage \
        --batch-id "$BATCH_ID" \
        --out "$DISCOVER_FILE" 2>&1 | tee -a "$LOG_FILE"
    SALVAGE_RC=${PIPESTATUS[0]}
    set -e

    if [ "$SALVAGE_RC" = "0" ]; then
        ITER_SALVAGED=1
        TOTAL_SALVAGED=$((TOTAL_SALVAGED + 1))
        TOTAL_CANDIDATES=$((TOTAL_CANDIDATES + 1))
        # Salvaged iterations bypass the project-exclude mechanism: we're
        # retrying a specific row, not picking a fresh project.
        log "Iteration $i: replaying salvaged candidate."
    else
        # Phase 1: Discover — search and select threads. No browser, no drafting.
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
                : # discover succeeded with candidates
                ;;
            3)
                log "Discover phase: rate-limited; ending run."
                rm -f "$DISCOVER_FILE"
                break
                ;;
            4)
                log "Discover phase: no eligible project left; ending run."
                rm -f "$DISCOVER_FILE"
                break
                ;;
            5)
                log "Discover phase: Claude failed; counting as failed and continuing."
                TOTAL_FAILED=$((TOTAL_FAILED + 1))
                rm -f "$DISCOVER_FILE"
                continue
                ;;
            6)
                log "Discover phase: no candidates found; counting as skipped and continuing."
                TOTAL_SKIPPED=$((TOTAL_SKIPPED + 1))
                PICKED=$(python3 -c "import json,sys;print(json.load(open('$DISCOVER_FILE')).get('project_name',''))" 2>/dev/null || echo "")
                [ -n "$PICKED" ] && EXCLUDE="${EXCLUDE:+$EXCLUDE,}$PICKED"
                rm -f "$DISCOVER_FILE"
                continue
                ;;
            *)
                log "Discover phase: unexpected exit code $DISCOVER_RC; counting as failed."
                TOTAL_FAILED=$((TOTAL_FAILED + 1))
                rm -f "$DISCOVER_FILE"
                continue
                ;;
        esac

        # Count freshly-discovered candidates so the dashboard's queue
        # tooltip distinguishes new finds from salvaged retries.
        DISCOVER_COUNT=$(python3 -c "import json;print(len(json.load(open('$DISCOVER_FILE')).get('decisions',[])))" 2>/dev/null || echo 0)
        TOTAL_CANDIDATES=$((TOTAL_CANDIDATES + DISCOVER_COUNT))

        PICKED=$(python3 -c "import json,sys;print(json.load(open('$DISCOVER_FILE')).get('project_name',''))" 2>/dev/null || echo "")
        [ -n "$PICKED" ] && EXCLUDE="${EXCLUDE:+$EXCLUDE,}$PICKED"
    fi

    # Phase 2: Ripen — T0 snapshot, 5-min sleep, T1 re-poll, composite delta gate.
    # composite = Δup + 4*Δcomments, floor>=1. Runs without browser lock.
    # Floor flipped from strict > to >= 2026-05-06: a +1 upvote in 5min is
    # enough signal that the thread is still alive — strict > rejected those
    # exact-floor cases as wholesale losses.
    # --top-k LIMIT (post 2026-05-06 refactor): the discover phase now emits
    # ALL search results (no LLM selection), so ripen typically receives
    # 20-50 candidates per iteration. Sort survivors by composite DESC and
    # keep only the top LIMIT (default 1) so the draft phase only pays LLM
    # cost for the most-momentum thread. Mirrors twitter_post_plan.py's
    # `LIMIT 15` SQL cap.
    RIPEN_FILE=$(mktemp -t post_reddit_ripened.XXXXXX.json)
    log "Ripening candidates (5-min delta gate, floor>=1, top-k=$LIMIT, w_comments=4)..."
    set +e
    python3 "$REPO_DIR/scripts/ripen_reddit_plan.py" \
        --in "$DISCOVER_FILE" \
        --out "$RIPEN_FILE" \
        --top-k "$LIMIT" 2>&1 | tee -a "$LOG_FILE"
    RIPEN_RC=${PIPESTATUS[0]}
    set -e

    if [ "$RIPEN_RC" != "0" ]; then
        log "Ripen phase: exit code $RIPEN_RC; falling back to unfiltered discover output."
        cp "$DISCOVER_FILE" "$RIPEN_FILE"
    fi

    SURVIVORS=$(python3 -c "import json;print(len(json.load(open('$RIPEN_FILE')).get('decisions',[])))" 2>/dev/null || echo 0)
    if [ "$SURVIVORS" = "0" ]; then
        log "Ripen phase: 0 survivors; skipping draft and post for this iteration."
        TOTAL_SKIPPED=$((TOTAL_SKIPPED + 1))
        rm -f "$DISCOVER_FILE" "$RIPEN_FILE"
        continue
    fi
    log "Ripen phase: $SURVIVORS candidate(s) passed delta gate."

    # Phase 3: Draft — Claude writes comments for survivors only. No browser.
    DRAFT_FILE=$(mktemp -t post_reddit_draft.XXXXXX.json)
    log "Drafting comments for $SURVIVORS survivor(s)..."
    set +e
    python3 "$REPO_DIR/scripts/post_reddit.py" \
        --phase draft \
        --in "$RIPEN_FILE" \
        --out "$DRAFT_FILE" 2>&1 | tee -a "$LOG_FILE"
    DRAFT_RC=${PIPESTATUS[0]}
    set -e

    case "$DRAFT_RC" in
        0)
            : # draft succeeded
            ;;
        5)
            log "Draft phase: Claude failed; counting as failed and continuing."
            TOTAL_FAILED=$((TOTAL_FAILED + 1))
            rm -f "$DISCOVER_FILE" "$RIPEN_FILE" "$DRAFT_FILE"
            continue
            ;;
        6)
            log "Draft phase: no drafted decisions; counting as skipped."
            TOTAL_SKIPPED=$((TOTAL_SKIPPED + 1))
            rm -f "$DISCOVER_FILE" "$RIPEN_FILE" "$DRAFT_FILE"
            continue
            ;;
        *)
            log "Draft phase: unexpected exit code $DRAFT_RC; counting as failed."
            TOTAL_FAILED=$((TOTAL_FAILED + 1))
            rm -f "$DISCOVER_FILE" "$RIPEN_FILE" "$DRAFT_FILE"
            continue
            ;;
    esac

    # Phase 4: Post — needs browser. Acquire lock, post, release immediately.
    log "Acquiring reddit-browser lock for post phase..."
    acquire_lock "reddit-browser" 3600
    ensure_browser_healthy "reddit"

    set +e
    POST_OUT=$(python3 "$REPO_DIR/scripts/post_reddit.py" --phase post --in "$DRAFT_FILE" 2>&1 | tee -a "$LOG_FILE")
    POST_RC=${PIPESTATUS[0]}
    set -e

    release_lock "reddit-browser"

    if [ "$POST_RC" = "0" ]; then
        ITER_POSTED=$(echo "$POST_OUT" | grep -oE 'posted=[0-9]+' | tail -1 | cut -d= -f2 || echo 0)
        ITER_FAILED=$(echo "$POST_OUT" | grep -oE 'failed=[0-9]+' | tail -1 | cut -d= -f2 || echo 0)
        TOTAL_POSTED=$((TOTAL_POSTED + ${ITER_POSTED:-0}))
        TOTAL_FAILED=$((TOTAL_FAILED + ${ITER_FAILED:-0}))
    else
        log "Post phase: exit code $POST_RC; counting as failed."
        TOTAL_FAILED=$((TOTAL_FAILED + 1))
    fi

    # Parse CDP failure reasons from post output and accumulate into FAILURE_REASONS.
    # Mirrors Twitter's EXEC_REASONS pattern so the dashboard pill shows the breakdown.
    while IFS= read -r line; do
        cdp_key=$(echo "$line" | grep -oE '\[post_reddit\] CDP FAILED: [a-z_]+' | awk '{print $NF}')
        case "$cdp_key" in
            thread_locked)          add_reason reddit_locked ;;
            thread_archived)        add_reason reddit_archived ;;
            thread_not_found)       add_reason reddit_deleted ;;
            account_blocked_in_sub) add_reason account_blocked ;;
            not_logged_in)          add_reason reddit_logged_out ;;
            all_attempts_failed)    add_reason cdp_no_response ;;
            comment_box_not_found)  add_reason comment_box_missing ;;
            "")                     : ;; # no match on this line
            *)                      add_reason "cdp_${cdp_key}" ;;
        esac
    done <<< "$POST_OUT"

    rm -f "$DISCOVER_FILE" "$RIPEN_FILE" "$DRAFT_FILE"
done

ELAPSED=$(( $(date +%s) - RUN_START ))
# Sum claude_sessions.total_cost_usd for every post_reddit session started
# during this cycle. Mirrors run-twitter-cycle.sh / run-linkedin.sh; the
# script value here is the same tag passed to log_claude_session.py inside
# scripts/post_reddit.py (~line 1141). Falls back to 0.0000 if the DB is
# unreachable so the dashboard never shows blank.
_COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START" --scripts "post_reddit" 2>/dev/null || echo "0.0000")
log "=== Run summary: posted=$TOTAL_POSTED failed=$TOTAL_FAILED skipped=$TOTAL_SKIPPED salvaged=$TOTAL_SALVAGED candidates=$TOTAL_CANDIDATES projects=[$EXCLUDE] cost=\$$_COST elapsed=${ELAPSED}s ==="

LOG_ARGS=(--script "post_reddit" --posted "$TOTAL_POSTED" --skipped "$TOTAL_SKIPPED" --failed "$TOTAL_FAILED" --cost "$_COST" --elapsed "$ELAPSED")
# Queue counters surface in the dashboard Result column:
#   --salvaged   how many iterations replayed a row from a prior cycle
#                (parsed by RUN_LINE_RE and rendered as a "salvaged: N" pill,
#                same key as Twitter's run-twitter-cycle.sh)
#   --candidates total reddit_candidates rows the cycle TOUCHED across discover
#                + salvage iterations. Lets an operator see "discover hit 4
#                candidates, queue replayed 2" at a glance.
[ "${TOTAL_SALVAGED:-0}" -gt 0 ] && LOG_ARGS+=(--salvaged "$TOTAL_SALVAGED")
[ "${TOTAL_CANDIDATES:-0}" -gt 0 ] && LOG_ARGS+=(--candidates "$TOTAL_CANDIDATES")
[ -n "$FAILURE_REASONS" ] && LOG_ARGS+=(--failure-reasons "$FAILURE_REASONS")
python3 "$REPO_DIR/scripts/log_run.py" "${LOG_ARGS[@]}" || true

log "=== Done: $(date) ==="
