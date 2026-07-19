#!/usr/bin/env bash
# sync-blocked-authors.sh — periodic reconciliation of a live
# blocked_by_author detection (twitter_browser._probe_author_block) into the
# durable author_blocklist table. See scripts/sync_blocked_authors.py for why
# this exists: the detection alone only suppresses the one tweet, never the
# author.
#
# Wired by launchd/com.m13v.social-twitter-blocklist-sync.plist. Local
# operator script only (uses db_direct for reads) -- not part of the shipped
# npm package.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"

LOG_FILE="$LOG_DIR/sync-blocked-authors-$(date +%Y-%m-%d_%H%M%S).log"

if [ -f "$REPO_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO_DIR/.env"
  set +a
fi

cd "$REPO_DIR" || exit 1

{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) sync-blocked-authors ==="
  /usr/bin/env python3 scripts/sync_blocked_authors.py
  RC=$?
  echo "=== exit_code=$RC ==="
  exit "$RC"
} >> "$LOG_FILE" 2>&1
