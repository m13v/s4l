#!/usr/bin/env bash
# ingest-web-chat-replies.sh — Poll Gmail for [WEB-CHAT #N] replies and forward
# them to visitors via Resend. Called by launchd every 5 minutes.
#
# Mirror of ~/social-autoposter/skill/dm-replies-ingest pattern (the
# ingest_human_dm_replies.py launchd rail), specialised for the web-chat thread.

set -euo pipefail

source "$(dirname "$0")/lock.sh"
acquire_lock "ingest-web-chat-replies" 60

# DB access is HTTP-only via scripts/http_api.py -> s4l.ai /api/v1/web-chat/*.
# No DATABASE_URL needed here any more.

ANALYTICS_ENV="$HOME/analytics/.env.production.local"
if [ -f "$ANALYTICS_ENV" ]; then
    export RESEND_API_KEY=$(grep '^RESEND_API_KEY=' "$ANALYTICS_ENV" | sed 's/^RESEND_API_KEY=//' | tr -d '"' | tr -d '\\n')
fi
export NODE_PATH="$HOME/analytics/node_modules"

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"

PYTHON_BIN="${PYTHON_BIN:-/opt/homebrew/bin/python3.11}"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="/usr/bin/python3"

LOG_FILE="$LOG_DIR/web-chat-ingest.log"
echo "[$(date)] starting ingest" >> "$LOG_FILE"

"$PYTHON_BIN" "$REPO_DIR/scripts/ingest_web_chat_replies.py" >> "$LOG_FILE" 2>&1 || \
    echo "[$(date)] ERROR: ingest_web_chat_replies.py failed" >> "$LOG_FILE"

# Trim log.
if [ -f "$LOG_FILE" ]; then
    tail -2000 "$LOG_FILE" > "$LOG_FILE.tmp" 2>/dev/null && mv "$LOG_FILE.tmp" "$LOG_FILE" || true
fi
