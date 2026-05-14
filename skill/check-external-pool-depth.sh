#!/usr/bin/env bash
# Wraps scripts/check_external_pool_depth.py for launchd. Fires at most one
# email per (project, platform, severity) per 24h via DB-side cooldown.
set -eu
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"
exec /opt/homebrew/bin/python3.11 "$REPO_DIR/scripts/check_external_pool_depth.py"
