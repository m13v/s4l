#!/bin/bash
# Get the permanent download URL for the latest S4L staging release (.mcpb).
# Usage:
#   bash scripts/get-latest-staging-mcpb.sh [--download] [--channel] [--path]
#
# Options:
#   --download    Download the .mcpb to current directory (instead of just printing URL)
#   --channel     Also set the local channel to staging after download
#   --path        Print the download path instead of the URL (for scripts)
#
# Implementation note: the GitHub release JSON is parsed in ONE python3 call
# that reads straight from urllib, never round-tripped through a bash variable
# + `echo`. `echo` (zsh's builtin especially, but also bash under some shopts)
# can interpret backslash escapes, so a `\n` that json.dumps() correctly
# escaped inside the release notes text gets turned into a REAL newline byte
# before the second parse, which then throws "Invalid control character".
# This bit in practice on 2026-07-07; do not reintroduce the echo-pipe pattern.

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

RELEASE_INFO=$(python3 -c "
import json, urllib.request, sys

req = urllib.request.Request(
    'https://api.github.com/repos/m13v/s4l/releases?per_page=1&pre_release=true',
    headers={'Accept': 'application/vnd.github+json'},
)
with urllib.request.urlopen(req, timeout=15) as r:
    releases = json.loads(r.read().decode('utf-8'))

if not releases:
    sys.exit(1)

rel = releases[0]
tag = rel['tag_name']
url = next(
    (a['browser_download_url'] for a in rel.get('assets', [])
     if a.get('name', '').endswith('social-autoposter.mcpb')),
    None,
)
if not url:
    sys.exit(1)
print(tag)
print(url)
")

LATEST_TAG=$(echo "$RELEASE_INFO" | sed -n '1p')
MCPB_URL=$(echo "$RELEASE_INFO" | sed -n '2p')

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
