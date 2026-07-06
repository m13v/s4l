#!/bin/bash
# run-draft-and-publish.sh — the launchd kicker entrypoint for the queue-backed
# twitter cycle (2026-06-24; draft-only flag 2026-07-06). It is the ONLY way
# cards are produced on a customer box: there is no host-draft scenario.
#
# Runs the REAL pipeline with Phase 2b drafting routed through the job queue
# (S4L_CLAUDE_PROVIDER=queue, drafted by the scheduled-task worker). The
# per-cycle DRAFT_ONLY value is decided BELOW from mode.json, not by the plist:
#   - draft-only ON (the DEFAULT): stop before posting, MERGE the plan into the
#     review-queue cards the menu bar shows. Without this merge the cycle's
#     plan would sit in a /tmp batch file nobody reads.
#   - draft-only OFF (operator opt-out via `s4l_mode.py draft-only off`,
#     promotion-lane cycles only): DRAFT_ONLY=0 — the cycle posts its top-1
#     pick autonomously, gated by the rolling virality bar. Persona cycles
#     keep making cards either way.
set -uo pipefail

# SAPS_->S4L_ env mirror (brand rename 2026-07-03): old plists/tasks still
# export SAPS_*; new code reads S4L_*. Copy names, never values via eval.
while IFS='=' read -r _k _; do
  case "$_k" in SAPS_*) _n="S4L_${_k#SAPS_}"; eval "[ -n \"\${$_n+x}\" ] || export $_n=\"\${$_k}\"";; esac
done <<EOF_ENV
$(env | grep '^SAPS_' | cut -d= -f1 | sed 's/$/=/')
EOF_ENV

REPO_DIR="${S4L_REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
PY="${S4L_PYTHON:-python3}"

OUT="$(mktemp -t s4l_draft_publish.XXXXXX)"
HB_PID=""  # scan-phase heartbeat (started below); torn down by the EXIT trap
# Clear the menu-bar activity signal on ANY exit so a crash/early-exit mid-cycle
# never leaves a stuck "scanning/drafting" label, and stop the heartbeat so it
# can't outlive the cycle. Best-effort; the || true keeps the trap from changing
# the cycle's exit code.
trap 'kill "$HB_PID" 2>/dev/null || true; rm -f "$OUT"; "$PY" "$REPO_DIR/scripts/s4l_activity.py" clear 2>/dev/null || true' EXIT

# Narrate the scan phase, GRANULARLY. The CDP scan runs inside the (locked)
# run-twitter-cycle.sh which has no activity writer; this covers that window until
# the queue provider flips the label to "finding threads"/"drafting replies".
# Instead of a frozen "scanning X for threads" for the whole multi-minute scan,
# each heartbeat recomputes elapsed and scrapes THIS cycle's own stdout ($OUT, the
# tee target below) for live progress — queries run, and candidates found once
# Phase 1 reports them — so the menu bar actually moves. Reads $OUT only; never
# touches the locked cycle. heartbeat() re-stamps ONLY while the state is still
# "scanning", so once the provider advances the phase it goes quiet (no flicker).
"$PY" "$REPO_DIR/scripts/s4l_activity.py" write scanning "scan: starting" 2>/dev/null || true
SCAN_T0=$(date +%s)
(
  while true; do
    sleep 20
    _el=$(( $(date +%s) - SCAN_T0 ))
    if [ "$_el" -lt 60 ]; then _dur="${_el}s"; else _dur="$(( _el / 60 ))m"; fi
    _q=$(grep -c "kept=" "$OUT" 2>/dev/null || true); _q=${_q:-0}
    # Total planned queries IS announced upfront by Phase 1:
    #   "Lean Phase 1: executing 118 queries via browser-harness CDP"
    # so show K/total once that line lands (it precedes the per-query "kept=" lines).
    _total=$(grep -oE "executing [0-9]+ queries" "$OUT" 2>/dev/null | tail -1 | grep -oE "[0-9]+" | head -1 || true)
    if [ -n "$_total" ]; then _qpart="${_q}/${_total}"; else _qpart="${_q}"; fi
    _found=$(grep -oE "Batch has [0-9]+" "$OUT" 2>/dev/null | tail -1 | grep -oE "[0-9]+" | tail -1 || true)
    if [ -n "$_found" ]; then
      _lbl="scan: ${_dur} · ${_qpart}, ${_found} found"
    else
      _lbl="scan: ${_dur} · ${_qpart}"
    fi
    "$PY" "$REPO_DIR/scripts/s4l_activity.py" heartbeat scanning "$_lbl" 2>/dev/null || true
  done
) &
HB_PID=$!

# Engagement mode (2026-06-26). The menu-bar toggle writes mode.json; this reads
# it and, in personal_brand mode, exports S4L_FORCE_PROJECT=<persona project> and
# TWITTER_TAIL_LINK_RATE=0 so the (locked) cycle below drafts link-free organic
# replies for the persona instead of the normal weighted product pick. In the
# default promotion mode it exports nothing and the cycle runs exactly as before.
# Read at cycle runtime (NOT baked into the plist) so flipping the toggle takes
# effect on the very next cycle with no launchd reload. Best-effort: any failure
# leaves the env untouched and the promotion pipeline runs.
eval "$("$PY" "$REPO_DIR/scripts/s4l_mode.py" env 2>/dev/null || true)"
if [ -n "${S4L_FORCE_PROJECT:-}" ]; then
    echo "[run-draft-and-publish] personal_brand mode: forcing project '$S4L_FORCE_PROJECT' (link-free)" >&2
fi

# Draft-only flag (2026-07-06). Per-cycle DRAFT_ONLY decision, overriding the
# plist's baked DRAFT_ONLY=1 baseline:
#   - draft-only ON (default), or persona cycle -> DRAFT_ONLY=1: draft stops
#     before posting and the plan merges into review cards, exactly as before.
#   - draft-only OFF + promotion cycle -> DRAFT_ONLY=0: the cycle POSTS its
#     top-1 pick autonomously; the rolling virality bar activates automatically
#     as the no-human quality gate.
# S4L_CYCLE_LANE comes from the s4l_mode env eval above; when absent (e.g. a
# personal_brand-only setup with no persona provisioned yet) we default to the
# safe card path.
DRAFT_ONLY_FLAG="$("$PY" "$REPO_DIR/scripts/s4l_mode.py" draft-only 2>/dev/null || echo 1)"
if [ "${S4L_CYCLE_LANE:-}" = "promotion" ] && [ "$DRAFT_ONLY_FLAG" = "0" ]; then
    export DRAFT_ONLY=0
    echo "[run-draft-and-publish] draft-only OFF: promotion cycle will POST autonomously (DRAFT_ONLY=0, virality bar active)" >&2
else
    export DRAFT_ONLY=1
fi

# First-run onboarding boost (2026-07-02). The MCP server drops
# first-run-boost.json into the state dir when it installs the kicker for the
# very first time. While the marker is live, widen the draft discovery window
# to 48h (vs the standard 24h draft window) and lift the top-1 card cap so the
# user's FIRST review batch surfaces several REAL drafts instead of one (or
# none). The marker is deleted the moment a merge actually delivers cards, or
# after 24h without any, so every later cycle runs the standard logic.
BOOST_MARKER="${S4L_STATE_DIR:-$HOME/.social-autoposter-mcp}/first-run-boost.json"
BOOST_ACTIVE=0
if [ -f "$BOOST_MARKER" ]; then
    if [ -n "$(find "$BOOST_MARKER" -mmin +1440 2>/dev/null)" ]; then
        rm -f "$BOOST_MARKER"
        echo "[run-draft-and-publish] first-run boost expired (>24h, no cards produced); removed" >&2
    else
        BOOST_ACTIVE=1
        export S4L_DRAFT_FRESHNESS_HOURS="${S4L_FIRST_RUN_FRESHNESS_HOURS:-48}"
        export S4L_TWITTER_POST_TOP_N="${S4L_FIRST_RUN_TOP_N:-5}"
        echo "[run-draft-and-publish] first-run boost active: freshness=${S4L_DRAFT_FRESHNESS_HOURS}h top_n=${S4L_TWITTER_POST_TOP_N}" >&2
    fi
fi

# Run the cycle; tee stdout so we can scan it for the DRAFT_ONLY_PLAN marker.
# Phase 2b blocks on the queue until the worker drafts it, so this can take a
# few minutes — that is expected.
bash "$REPO_DIR/skill/run-twitter-cycle.sh" 2>&1 | tee "$OUT"
RC=${PIPESTATUS[0]}

# Deliver the cycle's drafts into the cards.
MARKER="$(grep -oE 'DRAFT_ONLY_PLAN=\S+\.json' "$OUT" | tail -1)"
if [ -n "$MARKER" ]; then
    # merge_review_queue prints ONLY to stderr; capture and re-emit verbatim on
    # stderr (those [merge_review_queue] marker lines are load-bearing) so the
    # first-run boost can read the merged count.
    MERGE_OUT="$("$PY" "$REPO_DIR/scripts/merge_review_queue.py" --plan-from-marker "$MARKER" 2>&1 || true)"
    [ -n "$MERGE_OUT" ] && printf '%s\n' "$MERGE_OUT" >&2
    # Consume the first-run boost the moment a merge actually delivers cards, so
    # the widened window applies to exactly one successful first batch.
    if [ "$BOOST_ACTIVE" = "1" ] && printf '%s' "$MERGE_OUT" | grep -qE 'merged [1-9][0-9]* new draft'; then
        rm -f "$BOOST_MARKER"
        echo "[run-draft-and-publish] first-run boost consumed (cards delivered)" >&2
    fi
elif [ "${DRAFT_ONLY:-1}" = "0" ]; then
    # Draft-only OFF cycle: the cycle posted (or bar/relevance-gated) inline;
    # there is no plan left over to merge into cards by design.
    echo "[run-draft-and-publish] draft-only OFF cycle complete (rc=$RC); no card merge (posted inline)" >&2
else
    echo "[run-draft-and-publish] no DRAFT_ONLY_PLAN marker (cycle rc=$RC); nothing to merge" >&2
fi

exit "$RC"
