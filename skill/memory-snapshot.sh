#!/usr/bin/env bash
# Append one social-autoposter memory/process snapshot.
#
# This wrapper is intentionally tiny and does not source .env: command lines can
# already contain enough context for diagnostics, and the Python sampler redacts
# likely secrets before writing its JSONL log.

set -uo pipefail

# SAPS_->S4L_ env mirror (brand rename 2026-07-03): old plists/tasks still
# export SAPS_*; new code reads S4L_*. Copy names, never values via eval.
while IFS='=' read -r _k _; do
  case "$_k" in SAPS_*) _n="S4L_${_k#SAPS_}"; eval "[ -n \"\${$_n+x}\" ] || export $_n=\"\${$_k}\"";; esac
done <<EOF_ENV
$(env | grep '^SAPS_' | cut -d= -f1 | sed 's/$/=/')
EOF_ENV

REPO_DIR="${REPO_DIR:-$HOME/social-autoposter}"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"

cd "$REPO_DIR" || exit 2

PID_FILE="/tmp/social-autoposter-memory-snapshot.pid"
if [ -f "$PID_FILE" ]; then
  prev=$(cat "$PID_FILE" 2>/dev/null || true)
  if [ -n "$prev" ] && kill -0 "$prev" 2>/dev/null; then
    echo "[memory-snapshot] previous sampler still active pid=$prev; skipping"
    exit 0
  fi
fi
echo "$$" > "$PID_FILE"
trap 'rm -f "$PID_FILE"' EXIT INT TERM

PYTHON_BIN="${S4L_PYTHON:-python3}"
"$PYTHON_BIN" "$REPO_DIR/scripts/memory_snapshot.py" \
  --output "${S4L_MEMORY_SNAPSHOT_LOG:-$LOG_DIR/memory-snapshots.jsonl}" \
  --top "${S4L_MEMORY_TOP_N:-30}" \
  --max-bytes "${S4L_MEMORY_MAX_BYTES:-104857600}"
