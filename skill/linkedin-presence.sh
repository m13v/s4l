#!/usr/bin/env bash
# linkedin-presence.sh - read-only LinkedIn session presence pass.
#
# Purpose:
#   Run a bounded, auditable browsing session in the real linkedin-harness
#   Chrome. It random-walks first-party LinkedIn surfaces like a person:
#   scrolls, dwells, clicks read-only links (top nav tabs, profiles from the
#   feed, company pages, LinkedIn News stories), and navigates back. Clicks
#   are restricted to an href allowlist of read-only linkedin.com pages.
#   It does not like, follow, connect, message, comment, or touch any action
#   button.
#
# The shell wrapper is the pipeline: scheduling, killswitch, locks, harness
# bootstrap, and run_monitor logging. The browser action itself is deterministic
# Python CDP so a presence pass does not spend a Claude session just to scroll.

set -euo pipefail

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/linkedin-presence-$(date +%Y-%m-%d_%H%M%S).log"
RUN_START=$(date +%s)
BATCH_ID="lipres-$(date +%Y%m%d_%H%M%S)-$$"
export SA_CYCLE_ID="$BATCH_ID"
export S4L_PIPELINE_NAME="linkedin-presence"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"; }

DRY_RUN="${LINKEDIN_PRESENCE_DRY_RUN:-0}"

if [ -f "$HOME/.claude/social-autoposter/linkedin.killswitch" ] && [ "$DRY_RUN" != "1" ]; then
    log "LINKEDIN_KILLSWITCH active. Skipping LinkedIn presence pass."
    exit 0
elif [ -f "$HOME/.claude/social-autoposter/linkedin.killswitch" ]; then
    log "DRY_RUN: ignoring active LINKEDIN_KILLSWITCH for validation."
fi

# Optional local kill switch for operators who want the plist loaded but dormant.
if [ "${LINKEDIN_PRESENCE_ENABLED:-1}" = "0" ]; then
    log "LINKEDIN_PRESENCE_ENABLED=0. Skipping."
    exit 0
fi

# The launchd timer is fixed; vary each actual pass inside the script. Skipped
# passes do not write run_monitor rows, so the dashboard history only shows real
# browser activity.
RUN_PCT="${LINKEDIN_PRESENCE_RUN_PCT:-65}"
if [ "$DRY_RUN" != "1" ]; then
    ROLL=$(( RANDOM % 100 ))
    if [ "$ROLL" -ge "$RUN_PCT" ]; then
        log "Presence pass skipped by schedule jitter (roll=$ROLL threshold=$RUN_PCT)."
        exit 0
    fi
    JITTER_MAX="${LINKEDIN_PRESENCE_JITTER_MAX_SEC:-900}"
    if [ "$JITTER_MAX" -gt 0 ]; then
        JITTER=$(( RANDOM % (JITTER_MAX + 1) ))
        log "Sleeping ${JITTER}s before presence pass."
        sleep "$JITTER"
    fi
fi

# The browse session itself (start surface, action mix, scrolls, clicks,
# dwells) is randomized inside scripts/linkedin_presence.py.
log "=== LinkedIn Presence Run: $(date) (batch=$BATCH_ID) ==="

# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

source "$REPO_DIR/skill/lock.sh"
source "$REPO_DIR/skill/lib/linkedin-backend.sh"

log_presence_run() {
    local failed="$1"
    local failure_reasons="$2"
    local rc="$3"
    local elapsed cost
    elapsed=$(( $(date +%s) - RUN_START ))
    cost=$(python3 "$REPO_DIR/scripts/get_run_cost.py" \
        --since "$RUN_START" --scripts "linkedin-presence" 2>/dev/null || echo "0.0000")

    # Pull the real session shape from the python summary line if present.
    local scan="pages=1,scrolls=0,clicks=0"
    local summary
    summary=$(grep -o "LINKEDIN_PRESENCE_SUMMARY: .*" "$LOG_FILE" 2>/dev/null | tail -1)
    if [ -n "$summary" ]; then
        local pages scrolls clicks
        pages=$(echo "$summary" | sed -n 's/.*pages=\([0-9]*\).*/\1/p')
        scrolls=$(echo "$summary" | sed -n 's/.*scrolls=\([0-9]*\).*/\1/p')
        clicks=$(echo "$summary" | sed -n 's/.*clicks=\([0-9]*\).*/\1/p')
        scan="pages=${pages:-1},scrolls=${scrolls:-0},clicks=${clicks:-0}"
    fi

    local args=(
        "$REPO_DIR/scripts/log_run.py" --script "presence_linkedin"
        --posted 0 --skipped 0 --failed "$failed"
        --cost "$cost" --elapsed "$elapsed"
        --scanned 1 --checked 1
        --scan "$scan"
    )
    if [ -n "$failure_reasons" ]; then
        args+=(--failure-reasons "$failure_reasons")
    fi
    python3 "${args[@]}" 2>/dev/null || true

    find "$LOG_DIR" -name "linkedin-presence-*.log" -mtime +14 -delete 2>/dev/null || true
    log "=== LinkedIn presence complete: $(date) rc=$rc failed=$failed ==="
}

cleanup() {
    rm -f "$HOME/.claude/linkedin-agent-lock.json" 2>/dev/null || true
    if declare -f _sa_release_locks >/dev/null 2>&1; then
        _sa_release_locks || true
    fi
}
trap cleanup EXIT INT TERM HUP

if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN: would run a randomized read-only browse session"
    exit 0
fi

acquire_lock "linkedin-browser" 1800
BOOTSTRAP_RC=0
set +e
ensure_linkedin_browser_for_backend 2>&1 | tee -a "$LOG_FILE"
BOOTSTRAP_RC=${PIPESTATUS[0]}
set -e
# rc=78 = linkedin-pipeline lock skip code (peer pipeline drives the 9556
# Chrome): a skip, not a failure, so don't record it as a bootstrap error.
if [ "$BOOTSTRAP_RC" -eq 78 ]; then
    release_lock "linkedin-browser"
    log "linkedin-pipeline lock: peer pipeline is driving the 9556 Chrome; skipping this fire"
    exit 0
fi
if [ "$BOOTSTRAP_RC" -ne 0 ]; then
    release_lock "linkedin-browser"
    if [ -f "$HOME/.claude/social-autoposter/linkedin.killswitch" ]; then
        log_presence_run 1 "session_invalid:1" "$BOOTSTRAP_RC"
    else
        log_presence_run 1 "browser_bootstrap_error:1" "$BOOTSTRAP_RC"
    fi
    exit 0
fi

PRESENCE_RC=0
PYTHON_BIN="${LINKEDIN_DISCOVER_PYTHON:-python3}"
TIMEOUT_BIN="$(command -v gtimeout || command -v timeout || true)"
PRESENCE_STEPS="${LINKEDIN_PRESENCE_STEPS:-0}"
set +e
if [ -n "$TIMEOUT_BIN" ]; then
    "$TIMEOUT_BIN" 300 "$PYTHON_BIN" "$REPO_DIR/scripts/linkedin_presence.py" \
        --steps "$PRESENCE_STEPS" --max-seconds 200 2>&1 | tee -a "$LOG_FILE"
else
    "$PYTHON_BIN" "$REPO_DIR/scripts/linkedin_presence.py" \
        --steps "$PRESENCE_STEPS" --max-seconds 200 2>&1 | tee -a "$LOG_FILE"
fi
PRESENCE_RC=${PIPESTATUS[0]}
set -e

release_lock "linkedin-browser"

FAILED=0
FAILURE_REASONS=""
if [ "$PRESENCE_RC" -eq 2 ]; then
    FAILED=1
    FAILURE_REASONS="session_invalid:1"
elif [ "$PRESENCE_RC" -ne 0 ]; then
    FAILED=1
    if [ "$PRESENCE_RC" = "124" ]; then
        FAILURE_REASONS="timeout:1"
    else
        FAILURE_REASONS="python_cdp_error:1"
    fi
elif ! grep -q "LINKEDIN_PRESENCE_SUMMARY: .* session=ok" "$LOG_FILE" 2>/dev/null; then
    FAILED=1
    FAILURE_REASONS="missing_summary:1"
fi

log_presence_run "$FAILED" "$FAILURE_REASONS" "$PRESENCE_RC"
