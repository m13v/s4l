#!/bin/bash
# Stand-in mirror of run-github.sh's trap, for SIGTERM smoke test.
# Replaces post_github.py with a 60s sleep and replaces log_run.py with
# an `echo` that writes to a marker file so we can confirm the trap fired.

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
sleep 60  # stand-in for post_github.py
echo "=== SUMMARY: elapsed=60s posted=1 failed=0 ===" | tee -a "$LOG_FILE"
