#!/usr/bin/env bash
# Continuous heartbeat to /api/v1/installations/heartbeat.
#
# Independent of any reply traffic: even when github-engage is quiet, this
# proves the install lane (identity.py + Vercel + Postgres) is round-tripping.
# A gap in installations.last_seen_at on the server is a leading signal of
# Vercel outage / DNS / cert / migration drift before any user-facing
# pipeline notices.
#
# Schedule: every 15 minutes via launchd.
# Logs:     ~/social-autoposter/skill/logs/heartbeat-*.log

set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/social-autoposter}"
BASE_URL="${AUTOPOSTER_API_BASE:-https://s4l.ai}"
LOG_DIR="$REPO_DIR/skill/logs"
LOG_FILE="$LOG_DIR/heartbeat.log"

mkdir -p "$LOG_DIR"

ts() { date -u "+%Y-%m-%dT%H:%M:%SZ"; }
log() { printf '[%s] %s\n' "$(ts)" "$1" >> "$LOG_FILE"; }

PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
HDR=$("$PYTHON_BIN" "$REPO_DIR/scripts/identity.py" header 2>>"$LOG_FILE") || {
  log "FAIL identity.py header non-zero"
  exit 1
}

# Attach the S4L autopilot scheduled-task folder state (parity with the .mcpb
# heartbeat) so the server can tell centrally whether the queue-worker tasks are
# running from ~/.s4l-worker or are still mislocated. Best-effort: any failure
# falls back to an empty body so the heartbeat itself never depends on it.
BODY='{}'
if ST=$("$PYTHON_BIN" "$REPO_DIR/scripts/scheduled_tasks_snapshot.py" --summary 2>>"$LOG_FILE"); then
  if [ -n "$ST" ]; then
    BODY="{\"scheduled_tasks\":$ST}"
  fi
fi

# POST so the server can refresh the volatile fields (last_ip, last_seen_at).
RESP=$(curl -fsS -m 20 \
  -X POST \
  -H "X-Installation: $HDR" \
  -H "content-type: application/json" \
  -d "$BODY" \
  -w "\n__HTTP__%{http_code}__%{time_total}s" \
  "$BASE_URL/api/v1/installations/heartbeat" 2>>"$LOG_FILE") || {
  log "FAIL curl exit=$?"
  exit 1
}

CODE=$(echo "$RESP" | sed -n 's/.*__HTTP__\([0-9]*\)__.*/\1/p')
DUR=$(echo "$RESP"  | sed -n 's/.*__HTTP__[0-9]*__\(.*\)/\1/p')

if [ "$CODE" = "200" ]; then
  log "ok 200 ${DUR}"
else
  log "FAIL http=$CODE dur=$DUR"
  exit 1
fi
