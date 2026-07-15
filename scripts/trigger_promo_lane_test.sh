#!/bin/bash
# One-shot promotion-lane test trigger for the LOCAL S4L install (operator tool).
#
# Purpose: verify the promotion-lane prep prompt (size + structure) without
# hand-running the twitter cycle. It flips mode.json to promotion-only via
# s4l_mode.py (byte-for-byte what the menubar toggle writes), lets the
# PRODUCTION launchd kicker start the next cycle on its own, restores the
# original mode.json the moment that promotion cycle has started (the driver
# reads mode.json once, at cycle start), then waits for the cycle's
# [prep_prompt_snapshot] line and prints the size plus content verification.
#
# Guarantees:
#   - never runs the cycle itself (launchd stays the only driver)
#   - never touches the installed package; only mode.json state
#   - aborts if draft-only is OFF (a promo test must not auto-post)
#   - restores mode.json on ANY exit (trap), exposure window = one cycle start
set -euo pipefail

STATE="${S4L_STATE_DIR:-$HOME/.social-autoposter-mcp}"
PKG="$STATE/repo/package"
PY="$STATE/runtime/.venv/bin/python3"
LOGS="$PKG/skill/logs"
MODE="$STATE/mode.json"

BACKUP=$(mktemp -t s4l_mode_backup_XXXXXX)
cp "$MODE" "$BACKUP"
RESTORED=0
restore() {
    if [ "$RESTORED" = "0" ] && [ -f "$BACKUP" ]; then
        cp "$BACKUP" "$MODE"
        RESTORED=1
        echo "[trigger] mode.json restored to original"
    fi
    rm -f "$BACKUP"
}
trap restore EXIT

PERSONA=$(S4L_STATE_DIR="$STATE" "$PY" "$PKG/scripts/s4l_mode.py" persona-name 2>/dev/null || echo "")
if [ "$(S4L_STATE_DIR="$STATE" "$PY" "$PKG/scripts/s4l_mode.py" draft-only)" != "1" ]; then
    echo "[trigger] ABORT: draft-only is OFF; a promotion test cycle could auto-post"
    exit 1
fi

MARKER=$(mktemp -t s4l_promo_marker_XXXXXX)
echo "[trigger] saved mode.json; flipping to promotion-only (persona project: ${PERSONA:-none})"
S4L_STATE_DIR="$STATE" "$PY" "$PKG/scripts/s4l_mode.py" set promotion >/dev/null

DEADLINE=$(( $(date +%s) + 2400 ))
PROMO_LOG=""
echo "[trigger] waiting for the launchd kicker to start a promotion cycle (up to 40 min)..."
while [ -z "$PROMO_LOG" ]; do
    if [ "$(date +%s)" -gt "$DEADLINE" ]; then
        echo "[trigger] TIMEOUT: no promotion cycle started"
        exit 1
    fi
    for f in $(find "$LOGS" -name 'twitter-cycle-*.log' -newer "$MARKER" 2>/dev/null); do
        sel=$(grep -m1 'Selected projects:' "$f" 2>/dev/null | sed 's/.*Selected projects: //') || true
        [ -z "$sel" ] && continue
        if [ -n "$PERSONA" ] && [ "$sel" = "$PERSONA" ]; then
            continue   # persona cycle that raced the flip; keep waiting
        fi
        PROMO_LOG="$f"
        break
    done
    sleep 10
done
echo "[trigger] promotion cycle started: $(basename "$PROMO_LOG") (Selected projects: $sel)"
restore   # the running cycle already read the mode; put the toggle back NOW

until grep -q 'prep_prompt_snapshot' "$PROMO_LOG" 2>/dev/null; do
    if [ "$(date +%s)" -gt "$DEADLINE" ]; then
        echo "[trigger] TIMEOUT waiting for prep_prompt_snapshot; cycle tail:"
        tail -5 "$PROMO_LOG"
        exit 1
    fi
    sleep 10
done

LINE=$(grep -m1 'prep_prompt_snapshot' "$PROMO_LOG")
echo "[trigger] $LINE"
SNAP=$(echo "$LINE" | sed 's/.*path=//')
if [ -f "$SNAP" ]; then
    echo "[trigger] verification of $SNAP:"
    echo "  bytes                 = $(wc -c < "$SNAP" | tr -d ' ')"
    echo "  history occurrences   = $(grep -c '"history"' "$SNAP" || true)"
    echo "  prefs blocks          = $(grep -c '"draft_style_notes"' "$SNAP" || true) (must be 1)"
    echo "  global prefs line     = $(grep -c 'Global learned preferences' "$SNAP" || true) (must be 1)"
    echo "  projects in routing   = $(grep -c '"voice_relationship"' "$SNAP" || true)"
    echo "  ops keys leaked       = $(grep -cE '"posthog"|"onboarded_at"|"web_chat"|"seo_author"' "$SNAP" || true) (must be 0)"
fi
echo "[trigger] done"
