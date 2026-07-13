#!/usr/bin/env bash
# amplitude-24h-signups.sh — launchd wrapper for scripts/amplitude_24h_signups.py.
#
# Fires every 5 min from com.m13v.social-amplitude-24h.plist.
# Writes ~/social-autoposter/skill/cache/amplitude_24h_signups.json.
#
# The script itself uses a real-time PostHog count for the headline number
# (cheap, ~1s) and refreshes the eventually-consistent Amplitude export
# only every ~25 min (heavy, ~30s + ~150 MB).
#
# Read by project_stats_json.py:_amplitude_signups when days==1.

set -uo pipefail

REPO_DIR="$HOME/social-autoposter"

# shellcheck source=/dev/null
[ -f "$REPO_DIR/.env" ] && source "$REPO_DIR/.env"

# Inject Amplitude + PostHog creds from keychain so the export half can run
# without env vars being baked into the launchd plist.
export AMPLITUDE_STUDYLY_API_KEY="${AMPLITUDE_STUDYLY_API_KEY:-$(security find-generic-password -s amplitude-studyly-api-key -w 2>/dev/null)}"
export AMPLITUDE_STUDYLY_SECRET_KEY="${AMPLITUDE_STUDYLY_SECRET_KEY:-$(security find-generic-password -s amplitude-studyly-secret-key -w 2>/dev/null)}"
export POSTHOG_PERSONAL_API_KEY="${POSTHOG_PERSONAL_API_KEY:-$(security find-generic-password -s PostHog-Personal-API-Key-m13v -w 2>/dev/null)}"

cd "$REPO_DIR" || exit 2

# shellcheck source=lock.sh
source "$REPO_DIR/skill/lock.sh"
acquire_lock amplitude-24h-signups 5

RUN_START=$(date +%s)
/opt/homebrew/bin/python3.11 "$REPO_DIR/scripts/amplitude_24h_signups.py"
EXIT_CODE=$?
RUN_ELAPSED=$(( $(date +%s) - RUN_START ))

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] === done in ${RUN_ELAPSED}s (exit=${EXIT_CODE}) ==="
exit "$EXIT_CODE"
