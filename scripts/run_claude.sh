#!/bin/bash
# Wrapper around `claude` that pre-assigns a session UUID, exports it for
# downstream loggers (log_post.py, reply_db.py, dm_conversation.py read
# CLAUDE_SESSION_ID from env), and after the session exits records token
# usage + computed cost into the claude_sessions table.
#
# Usage:
#   scripts/run_claude.sh <script_tag> -p "PROMPT" [other claude flags...]
#
# Runner migration pattern:
#   OLD: claude -p "PROMPT" 2>&1 | tee -a "$LOG_FILE"
#   NEW: scripts/run_claude.sh "run-linkedin" -p "PROMPT" 2>&1 | tee -a "$LOG_FILE"
#
# The wrapper passes everything after the script_tag verbatim to `claude`,
# so all flags (--output-format, --json-schema, --model, etc.) work unchanged.
# stdout is streamed straight from claude — no buffering — so existing pipes
# and parsers see identical output.

set -uo pipefail

if [ $# -lt 2 ]; then
    echo "Usage: run_claude.sh <script_tag> <claude args...>" >&2
    exit 2
fi

SCRIPT_TAG="$1"; shift

# If the caller pre-set CLAUDE_SESSION_ID, honor it. This lets calling
# scripts inject the same UUID into their prompt (e.g. for SQL inserts that
# need to stamp claude_session_id) before invoking the wrapper.
SESSION_ID="${CLAUDE_SESSION_ID:-$(uuidgen | tr 'A-Z' 'a-z')}"
export CLAUDE_SESSION_ID="$SESSION_ID"

# ---------------------------------------------------------------------------
# Queue provider seam (added 2026-06-23) — customer boxes have no `claude` CLI.
#
# On a .mcpb install the runtime provisions Python only; there is no `claude`
# binary to exec. When SAPS_CLAUDE_PROVIDER=queue, route this call through the
# job queue instead: scripts/claude_job.py enqueues the prompt + --json-schema,
# BLOCKS until a Claude Desktop scheduled task ("saps-phase1-query" /
# "saps-phase2b-draft") processes it, and prints a claude `--output-format json`
# -shaped envelope to stdout so the pipeline's existing parsers are unchanged.
#
# Provider unset (your own machines, every launchd plist) => fall straight
# through to the real `claude -p` below, byte-for-byte unchanged. The seam lives
# here, in the single chokepoint every claude call funnels through, so NO
# pipeline script needs to know whether it's on a customer box or yours.
#
# The prompt reaches us either as a trailing positional arg (Phase 1 queries) or
# piped on stdin (Phase 2b prep); claude_job.py handles both. `exec` inherits
# stdin so the piped form passes through, and propagates the helper's exit code
# (0 = result, 79 = timed-out/blocked like a quota skip, 1 = not queue-eligible).
# ---------------------------------------------------------------------------
if [ "${SAPS_CLAUDE_PROVIDER:-}" = "queue" ]; then
    SAPS_QUEUE_REPO="${SAPS_REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
    SAPS_QUEUE_PY="${SAPS_PYTHON:-python3}"
    exec "$SAPS_QUEUE_PY" "$SAPS_QUEUE_REPO/scripts/claude_job.py" \
        provider --tag "$SCRIPT_TAG" -- "$@"
fi

# ---------------------------------------------------------------------------
# Quota preflight + post-hoc detection (added 2026-05-02).
#
# Background: if claude is hitting an org-level cap (monthly usage cap,
# daily token cap, context-window exceeded on every prompt, credit balance
# zero, persistent 429), retrying every cadence-tick burns nothing useful —
# it just guarantees an empty envelope back to the pipeline, which then
# fails noisily downstream (cf. 2026-05-01 19:23 twitter-cycle that died
# in Phase 1 on the org monthly limit and produced 0 tweets, then 2 more
# cycles fired into the same wall).
#
# Mechanism (see scripts/preflight.sh for full design):
#   - At start: check /tmp/sa-claude-blocked.json. If `blocked_until > now`,
#     exit 79 immediately. The wrapper exits visibly (stderr `[skipped]`
#     line) and the calling pipeline can either (a) abort gracefully on
#     non-zero, or (b) check `$? == 79` and treat as "skip cycle" rather
#     than "real failure".
#   - After claude exits: scan SIDE_LOG for known fatal-quota patterns. On
#     match, write a fresh stamp (10 min default) and force exit 79.
#   - On a clean claude run, if a stamp is present, clear it — the cap has
#     lifted and we shouldn't gate the next cycle.
#
# Block window = 10 min. The next launchd fire after expiry will retry
# claude for real. If the underlying cap is still in place, we re-stamp
# and skip again. This recovers automatically within 10 min of the cap
# being lifted, without piling up backlog or burning cycles in the gap.
# ---------------------------------------------------------------------------
SA_PREFLIGHT="$(cd "$(dirname "$0")" && pwd)/preflight.sh"
SA_QUOTA_PREFLIGHT_OK=0
if [ -f "$SA_PREFLIGHT" ]; then
    # shellcheck source=/dev/null
    source "$SA_PREFLIGHT"
    SA_QUOTA_PREFLIGHT_OK=1
    # If a prior run stamped a still-valid block, exit 79 with a [skipped:]
    # log line. We don't reuse preflight_skip_if_claude_blocked verbatim
    # because that helper exits 0 (suitable for launchd wrappers); here we
    # want exit 79 so the *caller pipeline* can tell "claude blocked" from
    # "claude ran cleanly but returned no candidates".
    if [ -f "$SA_CLAUDE_BLOCK_STAMP" ]; then
        SA_BLOCK_REMAINING=$(/usr/bin/python3 - "$SA_CLAUDE_BLOCK_STAMP" <<'PY'
import json, sys
from datetime import datetime, timezone
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
    bu = d.get("blocked_until", "")
    if not bu:
        print(0); sys.exit(0)
    until = datetime.fromisoformat(bu.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    print(int(max(0, (until - now).total_seconds())))
except Exception:
    print(0)
PY
)
        if [ "${SA_BLOCK_REMAINING:-0}" -gt 0 ]; then
            SA_BLOCK_REASON=$(/usr/bin/python3 -c "import json; print(json.load(open('$SA_CLAUDE_BLOCK_STAMP')).get('reason','unknown'))" 2>/dev/null)
            echo "[run_claude] skipped: claude_blocked reason=$SA_BLOCK_REASON expires_in=${SA_BLOCK_REMAINING}s script=$SCRIPT_TAG; exit 79" >&2
            exit 79
        fi
    fi
fi

# Auto-detect the platform agent from --mcp-config and signal the PreToolUse
# hooks (~/.claude/hooks/<platform>-agent-lock.sh) to bypass the cross-session
# block check. Rationale: every caller of run_claude.sh inside this repo is a
# launchd-managed pipeline that has already acquired the shell-level
# <platform>-browser lock via skill/lock.sh BEFORE invoking us. The shell
# lock is the authoritative serializer; the hook lock used to layer a second
# block on top, which produced false positives like the 2026-05-01 14:33
# LinkedIn run that paid $8.91 for an empty envelope because the *prior*
# LinkedIn cycle's JSONL was 57s stale (under the hook's 60s threshold)
# even though the shell lock had cleanly released.
#
# When SA_PIPELINE_LOCKED=1 is set, the hook trusts the shell layer and
# skips the cross-session check entirely.
for arg in "$@"; do
    case "$arg" in
        *linkedin-agent-mcp.json)  export SA_PIPELINE_PLATFORM="linkedin"; export SA_PIPELINE_LOCKED=1 ;;
        *twitter-harness-mcp.json) export SA_PIPELINE_PLATFORM="twitter";  export SA_PIPELINE_LOCKED=1 ;;
        *reddit-agent-mcp.json)    export SA_PIPELINE_PLATFORM="reddit";   export SA_PIPELINE_LOCKED=1 ;;
    esac
done

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
START=$(date -u +%Y-%m-%dT%H:%M:%S.000Z)

# Allow one-off model override without touching locked scripts.
MODEL_ARGS=()
if [ -n "${MODEL_OVERRIDE:-}" ]; then
    MODEL_ARGS=(--model "$MODEL_OVERRIDE")
fi

# Tee claude's stdout to a side file so we can extract the native SDK cost
# (streamRes.total_cost_usd) emitted in the final result event of stream-json /
# json output. Stdout still flows unchanged to whoever piped this wrapper, so
# downstream parsers see identical bytes. PIPESTATUS[0] preserves claude's
# exit code through the tee.
SIDE_LOG="$(mktemp -t sa_run_claude_stdout.XXXXXX)"

# Active-session sidecar. Lets the dashboard surface a live JSONL-tail link
# for the in-flight `claude` invocation while the phase is running, and lets
# investigators find the right transcript when run_claude.sh gets killed by
# the watchdog before log_claude_session.py can archive it. The file name is
# the wrapper PID so /api/claude-active can GC stale entries by checking
# whether the owning process is still alive.
ACTIVE_DIR="/tmp/sa-active-claude"
mkdir -p "$ACTIVE_DIR" 2>/dev/null || true
ACTIVE_FILE="$ACTIVE_DIR/$$.json"

# Process-group ID of the most recent claude invocation. Set by the run loop
# below (we run claude inside a `set -m` brace group + `&` so the forked
# subshell PID == its PGID, and claude + any grandchildren it spawns inherit
# that PGID). Used by _sa_cleanup to nuke orphan grandchildren that survive
# claude itself (e.g. a `find /` claude launched in the background and never
# waited on, which on 2026-05-01 burned CPU on PID 3187 long after the
# orchestrator exited because nothing was responsible for cleaning up after
# claude's kids).
CLAUDE_PG=""

# After-claude cleanup: explicitly remove the hook-layer lockfile for this
# session so the NEXT pipeline cycle doesn't see a stale lock from us. The
# unlock hook (PostToolUse) refreshes the lock timestamp to keep it alive
# across multi-tool sessions; without an explicit final cleanup, the lock
# survives session end and (per JSONL-mtime check) reads as "live" for up
# to 60s after we exit, causing the false-positive that produced the
# 2026-05-01 14:33 $8.91 empty-envelope run.
_sa_cleanup() {
    rm -f "$SIDE_LOG"
    rm -f "$ACTIVE_FILE"

    # Sweep orphan claude descendants. Process groups survive the parent's
    # death (kids reparented to launchd keep their PGID), so killing
    # `kill -- -PGID` reaches every grandchild, including ones reparented
    # to PID 1. Done before the lockfile cleanup so any orphan still
    # holding a browser lock dies first.
    if [ -n "$CLAUDE_PG" ]; then
        local survivors
        survivors=$(pgrep -g "$CLAUDE_PG" 2>/dev/null | grep -v "^$$\$" || true)
        if [ -n "$survivors" ]; then
            echo "[run_claude] sweeping orphan claude descendants in pg=$CLAUDE_PG: $(echo $survivors | tr '\n' ' ')" >&2
            kill -TERM -- -"$CLAUDE_PG" 2>/dev/null || true
            sleep 0.3
            kill -KILL -- -"$CLAUDE_PG" 2>/dev/null || true
        fi
    fi

    if [ -n "${SA_PIPELINE_PLATFORM:-}" ]; then
        local lockfile="$HOME/.claude/${SA_PIPELINE_PLATFORM}-agent-lock.json"
        if [ -f "$lockfile" ]; then
            # Only remove if WE hold it — defensive in case a peer raced in.
            local holder
            holder=$(jq -r '.session_id // empty' "$lockfile" 2>/dev/null || echo "")
            if [ "$holder" = "$SESSION_ID" ]; then
                rm -f "$lockfile"
            fi
        fi
    fi
}
# Cover EXIT (normal/return from script), INT (Ctrl-C from interactive),
# TERM (watchdog SIGTERM from scripts/watchdog_hung_runs.py), and HUP
# (controlling-tty death). SIGKILL is uncatchable; the active sidecar
# self-GCs on read in that case.
trap _sa_cleanup EXIT INT TERM HUP

# AUP-refusal retry loop. The Claude API safety filter occasionally refuses
# Phase A / SERP-driven prompts non-deterministically (the same prompt that
# refused at 18:13 succeeded at 17:58 today, 2026-05-01). Refusal output
# format: "API Error: Claude Code is unable to respond to this request,
# which appears to violate our Usage Policy". Retry up to 2 more times with
# 30s / 60s backoff and a fresh session UUID each retry (the prior session
# may have been flagged backend-side). Other RC failures pass through.
: > "$SIDE_LOG"
MAX_AUP_RETRIES=2
AUP_BACKOFF=(30 60)
# Transient-failure retry. The `claude` CLI gets reinstalled
# periodically (npm/curl installer), and the install briefly removes
# the old binary before writing the new one. Any invocation in that
# window gets exit 127 (command not found). Retry up to 3 times with
# 5s/10s/20s backoff before giving up. These retries do NOT count
# against the AUP-refusal budget, since the binary never actually ran.
# Caused two failed runs on 2026-05-01: 19:33 (engage-dm-replies, mid
# v2.1.126 install) and again on a follow-up cycle.
MAX_TRANSIENT_RETRIES=3
TRANSIENT_BACKOFF=(5 10 20)
RC=0
attempt=0
while :; do
    attempt=$((attempt + 1))
    transient_attempt=0
    while :; do
        : > "$SIDE_LOG"

        # Refresh active-session sidecar each attempt — SESSION_ID rotates on
        # AUP retries (line ~140), so the dashboard always points at the live
        # transcript, not the abandoned one.
        cat > "$ACTIVE_FILE" <<EOF
{
  "session_id": "$SESSION_ID",
  "script_tag": "$SCRIPT_TAG",
  "wrapper_pid": $$,
  "started_at": "$START",
  "attempt": $attempt,
  "platform": "${SA_PIPELINE_PLATFORM:-}"
}
EOF

        # Run claude in its own process group so we can kill orphans on exit.
        # `set -m` makes background jobs each get their own PGID == job PID;
        # the brace-group pipeline runs in a forked subshell whose PID is the
        # PGID, and claude inherits that PGID along with any descendants it
        # spawns. PIPESTATUS is captured INSIDE the brace group so the
        # subshell's exit code IS claude's exit code (not tee's), giving us
        # the same exit semantics callers had before.
        #
        # BASH-VERSION CAVEAT: this whole pattern depends on bash putting each
        # backgrounded job in its own process group when `set -m` is on, AND
        # on `$!` returning the brace-group subshell's PID (which equals the
        # new PGID). Verified on macOS bash 3.2 (the system default, 2026-05).
        # If the system bash is ever upgraded (bash 5.x, ble.sh wrappers,
        # `shopt -s lastpipe`, `set -o pipefail` toggles, the `inherit_errexit`
        # option, or running under zsh-as-bash compatibility mode), re-verify
        # the orphan sweep with /tmp/sa_pg_test.sh before assuming PGIDs still
        # line up. Specifically check:
        #   1. `pgrep -g $CLAUDE_PG` finds claude + grandchildren mid-run.
        #   2. A `nohup ... &` grandchild that survives claude's exit still
        #      shows up in pgrep -g $CLAUDE_PG until the EXIT trap fires.
        #   3. `kill -- -$CLAUDE_PG` actually reaches reparented (PPID=1)
        #      orphans — some shells silently strip job-control and put
        #      everything in the parent shell's PG, which would make the
        #      cleanup nuke the WRONG PG and either kill our own shell or
        #      no-op while the orphan keeps running.
        # The 2026-05-01 PID 3187 incident was the symptom we built this
        # against; if it ever returns, this assumption breaking is the
        # first thing to suspect.
        set -m
        { claude --session-id "$SESSION_ID" ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"} "$@" | tee -a "$SIDE_LOG"; exit "${PIPESTATUS[0]}"; } &
        CLAUDE_PG=$!
        set +m
        wait "$CLAUDE_PG"
        RC=$?
        if [ "$RC" -ne 127 ]; then
            break
        fi
        if [ "$transient_attempt" -ge "$MAX_TRANSIENT_RETRIES" ]; then
            echo "[run_claude] claude binary still missing after $MAX_TRANSIENT_RETRIES retries; giving up with exit 127" >&2
            break
        fi
        sleep_secs="${TRANSIENT_BACKOFF[$transient_attempt]:-20}"
        transient_attempt=$((transient_attempt + 1))
        echo "[run_claude] claude not found (exit 127, likely mid-reinstall); retrying in ${sleep_secs}s ($transient_attempt/$MAX_TRANSIENT_RETRIES)" >&2
        sleep "$sleep_secs"
    done
    if grep -qE "(API Error|Error).*Usage Policy|appears to violate our Usage Policy" "$SIDE_LOG"; then
        if [ "$attempt" -le "$MAX_AUP_RETRIES" ]; then
            sleep_secs="${AUP_BACKOFF[$((attempt - 1))]:-60}"
            echo "[run_claude] AUP refusal on attempt $attempt/$((MAX_AUP_RETRIES + 1)); retrying in ${sleep_secs}s with new session" >&2
            sleep "$sleep_secs"
            SESSION_ID="$(uuidgen | tr 'A-Z' 'a-z')"
            export CLAUDE_SESSION_ID="$SESSION_ID"
            # SIDE_LOG reset is handled at the top of the inner transient loop.
            continue
        fi
        echo "[run_claude] AUP refusal on final attempt $attempt; giving up" >&2
    fi
    break
done

END=$(date -u +%Y-%m-%dT%H:%M:%S.000Z)

# ---------------------------------------------------------------------------
# Post-hoc quota-error detection (added 2026-05-02).
#
# Scan claude's stdout for known fatal-quota signals. On match: stamp the
# shared block file (so subsequent pipelines skip cleanly) and force exit 79.
# On no match AND a successful claude run, clear any stale stamp — the cap
# has lifted.
#
# Why post-hoc and not via streaming watchdog: claude already wrote the
# bytes by the time we'd notice, and the orchestrator turn is one shot per
# wrapper invocation. Stamping at exit + skipping the NEXT fire is cheaper
# than racing to interrupt the current one. The current run paid for the
# error already; we just protect the next 10 min of cadence-ticks.
# ---------------------------------------------------------------------------
if [ "$SA_QUOTA_PREFLIGHT_OK" = "1" ]; then
    # Skip the regex-based quota classifier when Claude emitted a successful
    # SDK result event. Background: 2026-05-07 the Anthropic x SpaceX news
    # cycle produced tweets containing the literal phrase "5 hour rate limit".
    # Phase 1 of run-twitter-cycle.sh dumps every scraped tweet's full text
    # into stdout as structured_output, the classifier matched the phrase
    # inside tweet bodies, stamped /tmp/sa-claude-blocked.json, and the
    # subsequent Phase 2b-prep skipped with `claude_blocked` even though
    # Claude itself ran cleanly. The shared stamp file then blocked ~12 runs
    # across twitter-cycle, engage-twitter, and run-moltbook before clearing.
    #
    # The result event is a single-line JSON object emitted last, and only
    # on a clean orchestrator turn:
    #   {"type":"result","subtype":"success","is_error":false,...}
    # When that line is present AND claude exited 0, the run was clean by
    # construction — there is no quota error to classify. Anchoring on
    # `"subtype":"success"` (not just `is_error:false`) is intentional:
    # tweet bodies may contain `is_error` strings, but `"subtype":"success"`
    # is unique to the final result event.
    SA_RESULT_OK=0
    if [ "$RC" = "0" ] && grep -qE '"type":"result"[^}]*"subtype":"success"' "$SIDE_LOG" 2>/dev/null; then
        SA_RESULT_OK=1
    fi

    SA_QUOTA_REASON=""
    if [ "$SA_RESULT_OK" = "0" ]; then
        SA_QUOTA_REASON="$(preflight_classify_claude_error "$SIDE_LOG" 2>/dev/null | head -1 | tr -d '[:space:]')"
    fi

    if [ -n "$SA_QUOTA_REASON" ]; then
        # Stamp + force exit 79. Block window 600s (10 min). If the underlying
        # cap is real, the next 10 min of fires skip cleanly. After 600s a
        # fresh fire retries; success clears the stamp, repeat-failure
        # refreshes it.
        preflight_stamp_claude_blocked "$SA_QUOTA_REASON" 600 "$SCRIPT_TAG" "$SESSION_ID"
        echo "[run_claude] quota error detected reason=$SA_QUOTA_REASON; skipping next 10 min of fires (exit 79)" >&2
        # Still log the session for cost accounting before exiting.
        ORCH_COST="$(grep -oE '"total_cost_usd"[[:space:]]*:[[:space:]]*[0-9]+(\.[0-9]+)?' "$SIDE_LOG" 2>/dev/null \
            | tail -1 \
            | sed -E 's/.*:[[:space:]]*//')"
        ORCH_ARGS=()
        if [ -n "$ORCH_COST" ]; then
            ORCH_ARGS=(--orchestrator-cost-usd "$ORCH_COST")
        fi
        /usr/bin/python3 "$REPO_DIR/scripts/log_claude_session.py" \
            --session-id "$SESSION_ID" \
            --script "$SCRIPT_TAG" \
            --started-at "$START" \
            --ended-at "$END" \
            ${ORCH_ARGS[@]+"${ORCH_ARGS[@]}"} >&2 || true
        exit 79
    fi
    # Clean run AND no quota signal — clear any stale stamp (cap has lifted).
    if [ "$RC" = "0" ] && [ -f "$SA_CLAUDE_BLOCK_STAMP" ]; then
        preflight_clear_claude_block
    fi
fi

# Pull the LAST total_cost_usd in the stdout (the result event is emitted last
# in both stream-json and json modes). Tolerant to spaces and floats; defaults
# to empty when the format doesn't expose a result event (e.g. interactive runs
# that crash before the result line) so log_claude_session.py just leaves the
# DB column NULL.
ORCH_COST="$(grep -oE '"total_cost_usd"[[:space:]]*:[[:space:]]*[0-9]+(\.[0-9]+)?' "$SIDE_LOG" 2>/dev/null \
    | tail -1 \
    | sed -E 's/.*:[[:space:]]*//')"

# Best-effort cost logging. Never let logging failures mask the wrapped
# command's exit code.
ORCH_ARGS=()
if [ -n "$ORCH_COST" ]; then
    ORCH_ARGS=(--orchestrator-cost-usd "$ORCH_COST")
fi
python3 "$REPO_DIR/scripts/log_claude_session.py" \
    --session-id "$SESSION_ID" \
    --script "$SCRIPT_TAG" \
    --started-at "$START" \
    --ended-at "$END" \
    ${ORCH_ARGS[@]+"${ORCH_ARGS[@]}"} >&2 || true

exit $RC
