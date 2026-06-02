#!/usr/bin/env bash
# audit-dm-staleness.sh — Age out no_response DMs after 14 days of silence
# and downgrade stale not_our_prospect escalations.
#
# Runs daily via launchd com.m13v.social-audit-dm-staleness.plist.

set -uo pipefail

# shellcheck source=/dev/null
[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/audit-dm-staleness-$(date +%Y-%m-%d_%H%M%S).log"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

# HTTP-only lane (2026-06-01): both staleness UPDATEs run server-side via the
# s4l.ai API (POST /api/v1/dms/staleness-sweep). No DATABASE_URL, no psql.
AUDIT_HELPER="$REPO_DIR/scripts/audit_helper.py"

RUN_START=$(date +%s)
log "=== DM staleness audit: $(date) ==="

# Both sweeps run in one POST and return {aged, downgraded}:
#   1. Ghosted outreach: no_response + active + older than 14 days -> stale.
#   2. Reverse-pitchers the reply bot escalated (needs_human + not_our_prospect)
#      -> active; next inbound re-evaluates via classifier and re-escalates if needed.
SWEEP_JSON=$(python3 "$AUDIT_HELPER" dm-staleness-sweep 2>/dev/null || echo '{"aged":0,"downgraded":0}')
AGED=$(echo "$SWEEP_JSON" | python3 -c "import json,sys; print(int(json.load(sys.stdin).get('aged') or 0))" 2>/dev/null || echo "0")
DOWNGRADED=$(echo "$SWEEP_JSON" | python3 -c "import json,sys; print(int(json.load(sys.stdin).get('downgraded') or 0))" 2>/dev/null || echo "0")
log "Aged ghosted outreach to stale: $AGED"
log "Downgraded not_our_prospect escalations: $DOWNGRADED"

RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
python3 "$REPO_DIR/scripts/log_run.py" --script "audit-dm-staleness" \
    --posted "$AGED" --skipped "$DOWNGRADED" --failed 0 --cost 0 --elapsed "$RUN_ELAPSED" 2>/dev/null || true

log "=== Done in ${RUN_ELAPSED}s ==="

find "$LOG_DIR" -name "audit-dm-staleness-*.log" -mtime +30 -delete 2>/dev/null || true
