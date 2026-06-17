#!/usr/bin/env bash
# reply-risk-digest.sh — daily operator email summarizing risky/insightful
# inbound replies to our replies.
#
# Wired by launchd/com.m13v.social-reply-risk-digest.plist. The Python script
# does the DB read, optional Claude synthesis, and Gmail send.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"

LOG_FILE="$LOG_DIR/reply-risk-digest-$(date +%Y-%m-%d_%H%M%S).log"

if [ -f "$REPO_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO_DIR/.env"
  set +a
fi

cd "$REPO_DIR" || exit 1

{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) reply-risk-digest ==="
  /usr/bin/env python3 scripts/reply_risk_digest.py --hours 24 --platform x
  RC=$?
  echo "=== exit_code=$RC ==="
  exit "$RC"
} >> "$LOG_FILE" 2>&1
