#!/bin/bash
# v2: spawn the long child as a separate process so SIGTERM to the wrapping
# shell forwards via signal-group to the child, mirroring how launchd kills
# the bash leader and python child together.

set -euo pipefail

LOG_FILE="/tmp/sa_smoke_github_log.txt"
MARKER="/tmp/sa_smoke_github_marker.txt"
rm -f "$LOG_FILE" "$MARKER"

RUN_START=$(date +%s)

_SA_RUN_SUMMARY_EMITTED=0
_sa_emit_run_summary_oneshot() {
    [ "${_SA_RUN_SUMMARY_EMITTED:-0}" = "1" ] && return 0
    _SA_RUN_SUMMARY_EMITTED=1
    if [ -n "${LOG_FILE:-}" ] && [ -f "${LOG_FILE:-}" ] \
        && grep -q "=== SUMMARY: elapsed=" "$LOG_FILE" 2>/dev/null; then
        echo "PYTHON_OWNED_SUMMARY" > "$MARKER"
        return 0
    fi
    local elapsed
    elapsed=$(( $(date +%s) - ${RUN_START:-$(date +%s)} ))
    echo "TRAP_EMITTED elapsed=$elapsed reasons=sigterm:1" > "$MARKER"
}
trap _sa_emit_run_summary_oneshot EXIT INT TERM HUP

echo "=== GitHub Issues Run: $(date) ===" | tee "$LOG_FILE"
# Mirror real script: stand-in for `python3 post_github.py 2>&1 | tee -a $LOG_FILE`
# The pipeline puts sleep in a subshell; SIGTERM to the bash leader doesn't
# get deferred behind sleep because the leader is `tee` reading from sleep's
# stdout and waiting on the pipeline.
sleep 60 | tee -a "$LOG_FILE"
echo "=== SUMMARY: elapsed=60s posted=1 failed=0 ===" | tee -a "$LOG_FILE"
