#!/usr/bin/env bash
# Regression test for the 2026-09-18 orphan leak: an S4L MCP server whose client
# dies (stdin EOF) must exit on its own. Before v1.7.12-rc.6 it never did — the
# SDK never surfaces stdin EOF and the eager panel HTTP listener pinned the event
# loop, so every dead client left a full server behind (16 orphans / 33 GB found).
#
# Run manually from the repo:  bash mcp/scripts/test-stdin-eof.sh
# Exits 0 when the server exits within GRACE seconds of stdin closing.
set -u
cd "$(dirname "$0")/.."

GRACE=8 # seconds allowed between stdin EOF and process exit
LOG="$(mktemp -t s4l-eof-test)"

INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"eof-test","version":"0"}}}'

# stdin carries a real initialize, then closes after 2s — exactly what a client
# clean-exit (or SIGKILL, which just closes the pipe) looks like to the server.
( printf '%s\n' "$INIT"; sleep 2 ) | node dist/index.js >"$LOG" 2>&1 &
PID=$!

deadline=$(( $(date +%s) + 2 + GRACE ))
while kill -0 "$PID" 2>/dev/null; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "FAIL: server (pid $PID) still alive ${GRACE}s after stdin EOF" >&2
    echo "--- server log tail ---" >&2
    tail -20 "$LOG" >&2
    kill -9 "$PID" 2>/dev/null
    exit 1
  fi
  sleep 0.5
done

if grep -q "client gone" "$LOG"; then
  echo "PASS: server exited on stdin EOF ($(grep -o 'client gone ([^)]*)' "$LOG" | head -1))"
else
  echo "PASS: server exited after stdin EOF (no 'client gone' log line — check $LOG if unexpected)"
fi
exit 0
