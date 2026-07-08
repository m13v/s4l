#!/bin/bash
# Print (or download) the permanent download link for the latest S4L staging
# release (.mcpb).
#
# Usage:
#   bash scripts/get-latest-staging-mcpb.sh [--download] [--channel]
#
# Options:
#   --download    Download the .mcpb to the current directory (instead of
#                 just printing the URL)
#   --channel     After downloading, opt this machine into the staging
#                 channel (delegates to scripts/s4l_channel.py, the single
#                 source of truth for the channel marker)
#
# The link below is a FIXED URL, not a live GitHub API lookup: every
# `bash scripts/release-mcpb.sh --staging` run mirrors its .mcpb onto a
# separate, non-version-tagged release ("staging-latest") so this exact URL
# always serves whatever's newest — see scripts/release-mcpb.sh step 7b. That
# means this script needs zero network calls to resolve anything; it only
# hits the network when actually downloading.

set -euo pipefail

STAGING_MCPB_URL="https://github.com/m13v/s4l/releases/download/staging-latest/social-autoposter.mcpb"

DO_DOWNLOAD=0
DO_CHANNEL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --download) DO_DOWNLOAD=1; shift ;;
    --channel) DO_CHANNEL=1; shift ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [ "$DO_DOWNLOAD" -eq 0 ]; then
  echo "$STAGING_MCPB_URL"
  exit 0
fi

FILENAME="social-autoposter-staging.mcpb"
echo "Downloading $FILENAME from $STAGING_MCPB_URL" >&2
curl -fL "$STAGING_MCPB_URL" -o "$FILENAME"
echo "$FILENAME"

if [ "$DO_CHANNEL" -eq 1 ]; then
  REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  python3 "$REPO_ROOT/scripts/s4l_channel.py" set staging >&2
fi
