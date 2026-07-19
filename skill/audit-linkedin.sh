#!/usr/bin/env bash
# audit-linkedin.sh — LinkedIn-only audit (Python CDP + summary)

# LinkedIn killswitch (2026-05-27): refuse to run if a prior fire detected
# session compromise (http_999, authwall, throttle, li_at cleared).
# State: ~/.claude/social-autoposter/linkedin.killswitch
# Clear: python3 ~/social-autoposter/scripts/linkedin_killswitch.py clear
if [ -f "$HOME/.claude/social-autoposter/linkedin.killswitch" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] LINKEDIN_KILLSWITCH active. Aborting LinkedIn pipeline."
    echo "  Re-auth LinkedIn in harness Chrome, then: python3 ~/social-autoposter/scripts/linkedin_killswitch.py clear"
    exit 0
fi

exec "$(dirname "$0")/audit.sh" --platform linkedin
