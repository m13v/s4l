#!/usr/bin/env bash
# octolens-linkedin.sh — Octolens LinkedIn mentions only

# LinkedIn killswitch (2026-05-27): refuse to run if a prior fire detected
# session compromise (http_999, authwall, throttle, li_at cleared).
# State: ~/.claude/social-autoposter/linkedin.killswitch
# Clear: python3 ~/social-autoposter/scripts/linkedin_killswitch.py clear
if [ -f "$HOME/.claude/social-autoposter/linkedin.killswitch" ]; then
    echo "[$(date +%H:%M:%S)] LINKEDIN_KILLSWITCH active. Aborting LinkedIn pipeline."
    echo "  Re-auth LinkedIn in harness Chrome, then: python3 ~/social-autoposter/scripts/linkedin_killswitch.py clear"
    exit 0
fi

exec "$(dirname "$0")/octolens.sh" --platform linkedin
