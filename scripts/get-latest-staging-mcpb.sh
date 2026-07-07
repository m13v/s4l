#!/bin/bash
# Get the permanent download URL for the latest S4L staging release (.mcpb).
# Usage:
#   bash scripts/get-latest-staging-mcpb.sh [--download] [--channel] [--path]
#
# Options:
#   --download    Download the .mcpb to current directory (instead of just printing URL)
#   --channel     Also set the local channel to staging after download
#   --path        Print the download path instead of the URL (for scripts)

set -euo pipefail

DO_DOWNLOAD=0
DO_CHANNEL=0
DO_PATH=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --download) DO_DOWNLOAD=1; shift ;;
    --channel) DO_CHANNEL=1; shift ;;
    --path) DO_PATH=1; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# Fetch the latest pre-release from GitHub API (first page, per_page=1 for speed)
# Response is a JSON array; use python to extract the first object
RELEASE_DATA=$(curl -fsS "https://api.github.com/repos/m13v/s4l/releases?per_page=1&pre_release=true" 2>/dev/null | python3 -c "import sys, json; data=json.load(sys.stdin); r=data[0] if data else {}; print(json.dumps(r))")

# Extract version and download URL using python for reliable JSON parsing
LATEST_TAG=$(echo "$RELEASE_DATA" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('tag_name', ''))" 2>/dev/null)
MCPB_URL=$(echo "$RELEASE_DATA" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for asset in data.get('assets', []):
    if 'social-autoposter.mcpb' in asset.get('name', ''):
        print(asset.get('browser_download_url', ''))
        break
" 2>/dev/null)

if [ -z "$LATEST_TAG" ] || [ -z "$MCPB_URL" ]; then
  echo "Error: Could not find latest staging release" >&2
  exit 1
fi

if [ "$DO_PATH" -eq 1 ]; then
  # Return the tag only (for scripting)
  echo "$LATEST_TAG"
  exit 0
fi

if [ "$DO_DOWNLOAD" -eq 1 ]; then
  FILENAME="social-autoposter-${LATEST_TAG}.mcpb"
  echo "Downloading $FILENAME from $MCPB_URL" >&2
  curl -fL "$MCPB_URL" -o "$FILENAME"
  echo "$FILENAME"

  if [ "$DO_CHANNEL" -eq 1 ]; then
    STATE_DIR="${S4L_STATE_DIR:-$HOME/.social-autoposter-mcp}"
    mkdir -p "$STATE_DIR"
    echo '{"channel":"staging"}' > "$STATE_DIR/channel.json"
    echo "✓ Channel set to staging" >&2
  fi
else
  # Just print the URL
  echo "$MCPB_URL"
fi
