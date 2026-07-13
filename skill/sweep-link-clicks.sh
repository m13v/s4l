#!/usr/bin/env bash
# sweep-link-clicks.sh — launchd wrapper for scripts/sweep_post_link_clicks.py.
#
# Fires every 30 min from com.m13v.social-sweep-link-clicks.plist.
#
# Re-classifies post_link_clicks rows as is_bot=true based on behavioral
# patterns the per-hit UA regex can't detect:
#
#   R1  same ip_hash + same code + >=3 hits   (repeat-tap on a single link)
#   R2  clicks > views * platform_ctr_ceiling (impossible CTR)
#   R3  ip_hash hits >=5 different codes      (crawler sweep)
#   R4  no referrer + dirty-IP companion      (suspicious naked GET)
#   R5  >=4 codes within 60s from one ip_hash (burst fan-out)
#
# Records the rule in post_link_clicks.bot_reason and rebuilds
# post_links.clicks (humans only) for affected codes.
#
# Single-flight: takes the project lock so a slow run can't stack with
# the next launchd fire.

set -uo pipefail

REPO_DIR="$HOME/social-autoposter"

# shellcheck source=/dev/null
[ -f "$REPO_DIR/.env" ] && source "$REPO_DIR/.env"

cd "$REPO_DIR" || exit 2

# shellcheck source=lock.sh
source "$REPO_DIR/skill/lock.sh"
acquire_lock sweep-link-clicks 5

RUN_START=$(date +%s)
/opt/homebrew/bin/python3.11 "$REPO_DIR/scripts/sweep_post_link_clicks.py" --cron
EXIT_CODE=$?
RUN_ELAPSED=$(( $(date +%s) - RUN_START ))

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] === done in ${RUN_ELAPSED}s (exit=${EXIT_CODE}) ==="
exit "$EXIT_CODE"
