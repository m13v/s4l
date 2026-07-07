#!/bin/bash
# Set the S4L release channel (stable or staging).
# Usage:
#   bash scripts/set-channel.sh stable   # track stable releases only (default)
#   bash scripts/set-channel.sh staging  # track staging releases (pre-releases)

set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: bash scripts/set-channel.sh <stable|staging>" >&2
  exit 1
fi

CHANNEL="$1"
if [ "$CHANNEL" != "stable" ] && [ "$CHANNEL" != "staging" ]; then
  echo "Error: channel must be 'stable' or 'staging'" >&2
  exit 1
fi

STATE_DIR="${S4L_STATE_DIR:-$HOME/.social-autoposter-mcp}"
mkdir -p "$STATE_DIR"
echo "{\"channel\":\"$CHANNEL\"}" > "$STATE_DIR/channel.json"

echo "✓ Release channel set to: $CHANNEL"
if [ "$CHANNEL" = "staging" ]; then
  echo "  Will now track pre-releases (rc.N builds). Next update check pulls the latest staging release."
else
  echo "  Will now track stable releases only. Next update check pulls the latest stable release."
fi
