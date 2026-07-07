#!/usr/bin/env bash
# sentry-digest.sh — critical (level:error/fatal) Sentry issue digest for the
# s4l Sentry project. Replaces the raw per-issue Sentry alert email; only
# emails when something is NEW or GROWING. Idempotent via
# scripts/state/sentry_digest_ledger.json. Wired by
# launchd/com.m13v.s4l-sentry-digest.plist (every 4 hours).

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/sentry-digest-$(date +%Y%m%d).log"

cd "$REPO_DIR" || exit 1

{
    echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) sentry-digest run ==="
    /usr/bin/python3 scripts/sentry_digest.py
    echo
} >> "$LOG_FILE" 2>&1
