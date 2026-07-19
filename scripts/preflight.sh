#!/bin/bash
# preflight.sh — sourced helper for launchd-fired run-*.sh wrappers.
#
# Four checks, each emits a `[skipped: <reason>]` stderr line and exits 0
# (so launchd treats the slot as cleanly consumed and fires the next one
# on schedule, rather than thinking the job is broken):
#
#   1. preflight_skip_if_jetsam_pressure
#        Reads kern.memorystatus_vm_pressure_level (1=normal, 2=warn,
#        4=urgent, 8=critical). Skips when >= 2. Background: 2026-05-01
#        a JetsamEvent at 19:26 swallowed two consecutive launchd fires
#        of run-twitter-cycle (19:38, 19:53) — wrappers fired but the
#        grandchild bash never produced output, presumably jetsam-killed
#        or starved during the system's crash-cleanup spike. Skipping
#        cleanly when pressure is already elevated avoids stacking more
#        Chrome+Claude+Python work onto an already-thrashing system.
#
#   2. preflight_skip_if_claude_blocked
#        Reads /tmp/sa-claude-blocked.json. If `blocked_until > now`,
#        skips. Stamp is written by scripts/run_claude.sh when claude
#        emits a recognized fatal-quota error (monthly cap, daily cap,
#        org budget, context-window exceeded, credit balance, persistent
#        429). Default block window: 600s; once expired, the next fire
#        proceeds normally and either (a) succeeds, in which case the
#        stamp is auto-cleared, or (b) hits the same error and refreshes
#        the stamp for another 600s. This prevents launchd from burning
#        a fire every cadence-tick during a multi-hour outage while
#        still recovering automatically within 10 min of the underlying
#        cap being lifted.
#
#   3. preflight_skip_if_paused
#        Reads <state>/paused.flag (S4L Pause: written by mcp/src/index.ts
#        pauseS4L and the menubar's pause button). Pause boots the pipeline
#        launchd jobs out, but that only covers jobs loaded at pause time —
#        this guard is the shared runtime backstop every pipeline wrapper
#        calls, so a job that was mid-flight, re-loaded by a Claude boot,
#        or added later can never draft/post while paused. One guard, all
#        pipelines: add a new run-*.sh wrapper and call this, done.
#
#   4. preflight_acquire_slot_or_skip <pool_name> [max_slots=4]
#        Slot-pool admission control via mkdir on
#        /tmp/sa-${pool_name}-slot-{1..max_slots}.lock. If all slots are
#        held by live PIDs, skips. Stale slots (PID dead) are GC'd before
#        the acquire pass. Used by run-twitter-cycle.sh to cap concurrent
#        cycles at 4 (post 2026-04-30 the launchd wrapper double-forks
#        and no longer suppresses overlapping fires, so this is the only
#        guardrail against ramp-up under sustained pressure).
#
# Sourcing requirements:
#   - Source AFTER skill/lock.sh if you want both. preflight.sh chains
#     its slot cleanup with lock.sh's _sa_release_locks via a combined
#     EXIT trap (replaces lock.sh's trap; calls _sa_release_locks if
#     defined, then releases preflight slots).
#   - Source BEFORE the script-specific cleanup trap if any (the script
#     can install its own trap that calls _preflight_release_slots
#     itself; see run-twitter-cycle.sh for that pattern).

# Slot-pool array — initialised once per shell so multiple acquire calls
# in the same script stack cleanly.
if [ -z "${_SA_PREFLIGHT_SLOTS+x}" ]; then
    declare -a _SA_PREFLIGHT_SLOTS=()
fi

_preflight_release_slots() {
    local d
    for d in ${_SA_PREFLIGHT_SLOTS[@]+"${_SA_PREFLIGHT_SLOTS[@]}"}; do
        rm -rf "$d" 2>/dev/null || true
    done
}

# Combined exit handler: clean preflight slots AND chain lock.sh cleanup
# if it's been sourced. Installed unconditionally on first source so
# slot leaks never outlive the script even if the caller forgets to
# install its own trap.
_preflight_combined_exit() {
    _preflight_release_slots
    if command -v _sa_release_locks >/dev/null 2>&1; then
        _sa_release_locks
    fi
}
trap _preflight_combined_exit EXIT INT TERM HUP

# ---------------------------------------------------------------------------
# 1. Memory-pressure preflight
# ---------------------------------------------------------------------------
preflight_skip_if_jetsam_pressure() {
    # DISABLED (2026-06-04, by user request): this guard kept false-positive-
    # skipping healthy cycles because kern.memorystatus_vm_pressure_level is
    # sticky (latches at 2 for hours after a transient spike while RAM is
    # actually fine). Now a no-op so it never blocks a cycle. Kept as a
    # named function because run-twitter-cycle.sh (locked) still calls it.
    return 0
}

# ---------------------------------------------------------------------------
# 2. Claude-quota stamp preflight
# ---------------------------------------------------------------------------
# Stamp file is JSON:
#   {
#     "reason": "monthly_limit|daily_limit|context_window|credit_balance|...",
#     "stamped_at": "2026-05-02T18:00:00Z",
#     "blocked_until": "2026-05-02T18:10:00Z",
#     "stamped_by_session": "<uuid>",
#     "stamped_by_script": "run-twitter-cycle"
#   }
# Single source of truth across all pipelines. Written by run_claude.sh,
# read by every launchd wrapper. Blocking is per-machine, not per-pipeline,
# because every pipeline shares the same Anthropic org quota.
SA_CLAUDE_BLOCK_STAMP="${SA_CLAUDE_BLOCK_STAMP:-/tmp/sa-claude-blocked.json}"

preflight_skip_if_claude_blocked() {
    [ -f "$SA_CLAUDE_BLOCK_STAMP" ] || return 0

    # Pull blocked_until + reason in one python invocation. Falls through
    # to "not blocked" on any parse failure (corrupt stamp -> recover).
    local payload
    payload=$(/usr/bin/python3 - <<'PY' "$SA_CLAUDE_BLOCK_STAMP" 2>/dev/null
import json, sys, os
from datetime import datetime, timezone
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
    bu = d.get("blocked_until", "")
    if not bu:
        sys.exit(0)
    # Tolerate trailing Z or +00:00.
    bu_norm = bu.replace("Z", "+00:00")
    until = datetime.fromisoformat(bu_norm)
    now = datetime.now(timezone.utc)
    remaining = int((until - now).total_seconds())
    print(f"{remaining}|{d.get('reason','unknown')}|{d.get('stamped_by_script','?')}|{bu}")
except Exception:
    pass
PY
)
    [ -z "$payload" ] && return 0

    local remaining reason stamped_by stamped_until
    IFS='|' read -r remaining reason stamped_by stamped_until <<< "$payload"

    if [ -z "$remaining" ]; then
        return 0
    fi

    if [ "$remaining" -gt 0 ]; then
        local script_tag="${SA_PREFLIGHT_SCRIPT:-${SCRIPT_TAG:-$(basename "$0")}}"
        echo "[skipped: claude_blocked reason=$reason expires_in=${remaining}s stamped_by=$stamped_by until=$stamped_until script=$script_tag] $(date)" >&2
        exit 0
    fi

    # Stamp expired. Best-effort cleanup so the next pipeline doesn't
    # repeat the parse. If a parallel script is mid-write of a fresh
    # stamp we lose the race harmlessly — they'll just re-write below.
    rm -f "$SA_CLAUDE_BLOCK_STAMP" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# 3. S4L Pause preflight
# ---------------------------------------------------------------------------
preflight_skip_if_paused() {
    local flag="${S4L_STATE_DIR:-$HOME/.social-autoposter-mcp}/paused.flag"
    [ -e "$flag" ] || return 0
    local script_tag="${SA_PREFLIGHT_SCRIPT:-${SCRIPT_TAG:-$(basename "$0")}}"
    echo "[skipped: paused flag=$flag script=$script_tag] $(date)" >&2
    exit 0
}

# ---------------------------------------------------------------------------
# 4. Slot-pool admission (parallel-cycle cap)
# ---------------------------------------------------------------------------
preflight_acquire_slot_or_skip() {
    local pool_name="$1"
    local max_slots="${2:-4}"
    local pass i pid slot_dir

    if [ -z "$pool_name" ]; then
        echo "preflight_acquire_slot_or_skip: pool_name required" >&2
        return 1
    fi

    # Two passes:
    #   pass=1: GC slots whose holder PID is dead (clean SIGKILL / OOM).
    #   pass=2: try to claim the first free slot.
    for pass in 1 2; do
        for i in $(seq 1 "$max_slots"); do
            slot_dir="/tmp/sa-${pool_name}-slot-${i}.lock"
            if [ "$pass" = "1" ]; then
                if [ -d "$slot_dir" ]; then
                    pid=$(cat "$slot_dir/pid" 2>/dev/null || echo "")
                    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
                        rm -rf "$slot_dir" 2>/dev/null || true
                    fi
                fi
            else
                if mkdir "$slot_dir" 2>/dev/null; then
                    echo $$ > "$slot_dir/pid"
                    _SA_PREFLIGHT_SLOTS+=("$slot_dir")
                    return 0
                fi
            fi
        done
    done

    # All slots taken — count + report and skip.
    local active=0
    for i in $(seq 1 "$max_slots"); do
        [ -d "/tmp/sa-${pool_name}-slot-${i}.lock" ] && active=$((active + 1))
    done
    local script_tag="${SA_PREFLIGHT_SCRIPT:-${SCRIPT_TAG:-$(basename "$0")}}"
    echo "[skipped: too_many_inflight pool=$pool_name max=$max_slots active=$active script=$script_tag] $(date)" >&2
    exit 0
}

# ---------------------------------------------------------------------------
# Stamp helpers (used by run_claude.sh after claude exits with quota error).
# Exposed here so any caller can also stamp manually if it detects a quota
# signal outside of run_claude.sh (e.g. python script directly hitting the
# Anthropic API).
# ---------------------------------------------------------------------------

# Write/refresh the block stamp.
#   $1 = reason            (monthly_limit | daily_limit | context_window | credit_balance | rate_limit_persistent | unknown)
#   $2 = duration_seconds  (default 600)
#   $3 = optional script tag
#   $4 = optional session id
preflight_stamp_claude_blocked() {
    local reason="${1:-unknown}"
    local duration="${2:-600}"
    local script_tag="${3:-${SA_PREFLIGHT_SCRIPT:-${SCRIPT_TAG:-unknown}}}"
    local session="${4:-${CLAUDE_SESSION_ID:-unknown}}"

    /usr/bin/python3 - "$SA_CLAUDE_BLOCK_STAMP" "$reason" "$duration" "$script_tag" "$session" <<'PY'
import json, sys, os, tempfile
from datetime import datetime, timezone, timedelta
path, reason, duration, script_tag, session = sys.argv[1:6]
duration = int(duration)
now = datetime.now(timezone.utc)
until = now + timedelta(seconds=duration)
payload = {
    "reason": reason,
    "stamped_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "blocked_until": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "stamped_by_session": session,
    "stamped_by_script": script_tag,
    "duration_seconds": duration,
}
# If a stamp already exists with later expiry, keep the later one.
try:
    with open(path) as f:
        existing = json.load(f)
    eu = existing.get("blocked_until", "")
    if eu:
        e_until = datetime.fromisoformat(eu.replace("Z", "+00:00"))
        if e_until > until:
            # Existing stamp blocks longer; preserve it but bump reason.
            existing["reason"] = reason
            payload = existing
except Exception:
    pass
# Atomic write.
tmp = tempfile.NamedTemporaryFile("w", dir=os.path.dirname(path) or "/tmp",
                                   delete=False, prefix=".sa-claude-blocked.", suffix=".tmp")
json.dump(payload, tmp)
tmp.close()
os.replace(tmp.name, path)
print(f"[claude_quota] stamped reason={reason} until={payload['blocked_until']} duration={duration}s", file=sys.stderr)
PY
}

# Clear the stamp (called when a fresh claude run succeeds, signalling
# the underlying cap has lifted).
preflight_clear_claude_block() {
    [ -f "$SA_CLAUDE_BLOCK_STAMP" ] || return 0
    rm -f "$SA_CLAUDE_BLOCK_STAMP" 2>/dev/null || true
    echo "[claude_quota] cleared block stamp (claude run succeeded)" >&2
}

# Inspect a claude transcript / log for known fatal-quota error patterns.
# Reads from path argument or stdin. Echoes the matched reason on stdout
# (one of: monthly_limit | daily_limit | context_window | credit_balance |
# rate_limit_persistent | empty if no match). Exit 0 always.
#
# Patterns are intentionally broad — false positives stamp a 10-min skip
# which self-clears on next try. False negatives let the caller burn an
# entire cycle's budget on a doomed run, which is the worse failure.
preflight_classify_claude_error() {
    local source_file="${1:-/dev/stdin}"
    /usr/bin/python3 - "$source_file" <<'PY'
import sys, re, os
path = sys.argv[1]
try:
    if path == "/dev/stdin":
        text = sys.stdin.read()
    else:
        with open(path, "r", errors="replace") as f:
            text = f.read()
except Exception:
    sys.exit(0)

low = text.lower()

# Order matters — most-specific first.
patterns = [
    ("monthly_limit",         [r"monthly\s+usage\s+limit", r"hit your org's monthly", r"monthly\s+limit\s+reached", r"month'?s\s+(?:usage|allowance|cap)"]),
    ("daily_limit",           [r"daily\s+rate\s+limit", r"daily\s+usage\s+limit", r"daily\s+limit\s+reached", r"day'?s\s+(?:usage|allowance|cap)"]),
    ("credit_balance",        [r"credit\s+balance\s+is\s+too\s+low", r"insufficient\s+credit", r"out\s+of\s+credits"]),
    ("context_window",        [r"context[\s_-]?length\s+(?:exceeded|too\s+long)", r"context[\s_-]?window\s+(?:exceeded|too\s+long)", r"prompt\s+is\s+too\s+long", r"max(?:imum)?\s+context\s+(?:length|window)"]),
    ("rate_limit_persistent", [r"5[\s-]?hour\s+(?:rate\s+)?limit", r"rate_limit_5h", r"per[\s-]?5h\s+(?:rate\s+)?limit"]),
]
for reason, regexes in patterns:
    for rx in regexes:
        if re.search(rx, low):
            print(reason)
            sys.exit(0)
sys.exit(0)
PY
}
