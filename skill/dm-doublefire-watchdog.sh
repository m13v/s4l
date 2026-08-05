#!/usr/bin/env bash
# dm-doublefire-watchdog.sh - hourly sweep of recent claude-session logs for
# the DM double-fire signature (two sessions replying to the same inbound).
# Emails i@m13v.com once per incident. Idempotent via
# ~/.claude/social-autoposter/dm_double_fire_alerted.json.
# Wired by launchd/com.m13v.social-dm-doublefire-watchdog.plist (hourly).

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/dm-doublefire-watchdog-$(date +%Y%m%d).log"

cd "$REPO_DIR" || exit 1

{
    echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) dm-doublefire-watchdog sweep ==="
    /usr/bin/env python3 scripts/dm_double_fire_watchdog.py --days 2
    echo
} >> "$LOG_FILE" 2>&1
