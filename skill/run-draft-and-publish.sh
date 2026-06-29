#!/bin/bash
# run-draft-and-publish.sh — the launchd kicker entrypoint for the queue-backed
# draft autopilot (2026-06-24). It is the ONLY way cards are produced on a
# customer box: there is no host-draft scenario.
#
# Runs the REAL pipeline in DRAFT_ONLY mode (inheriting the kicker plist's
# DRAFT_ONLY=1 / SAPS_CLAUDE_PROVIDER=queue env, so Phase 2b drafting routes
# through the job queue and is drafted by the scheduled-task worker), then MERGES
# the drafts it produced into the review-queue cards the menu bar shows. Without
# this merge the cycle's plan would sit in a /tmp batch file nobody reads.
set -uo pipefail

REPO_DIR="${SAPS_REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
PY="${SAPS_PYTHON:-python3}"

OUT="$(mktemp -t saps_draft_publish.XXXXXX)"
HB_PID=""  # scan-phase heartbeat (started below); torn down by the EXIT trap
# Clear the menu-bar activity signal on ANY exit so a crash/early-exit mid-cycle
# never leaves a stuck "scanning/drafting" label, and stop the heartbeat so it
# can't outlive the cycle. Best-effort; the || true keeps the trap from changing
# the cycle's exit code.
trap 'kill "$HB_PID" 2>/dev/null || true; rm -f "$OUT"; "$PY" "$REPO_DIR/scripts/saps_activity.py" clear 2>/dev/null || true' EXIT

# Narrate the scan phase, GRANULARLY. The CDP scan runs inside the (locked)
# run-twitter-cycle.sh which has no activity writer; this covers that window until
# the queue provider flips the label to "finding threads"/"drafting replies".
# Instead of a frozen "scanning X for threads" for the whole multi-minute scan,
# each heartbeat recomputes elapsed and scrapes THIS cycle's own stdout ($OUT, the
# tee target below) for live progress — queries run, and candidates found once
# Phase 1 reports them — so the menu bar actually moves. Reads $OUT only; never
# touches the locked cycle. heartbeat() re-stamps ONLY while the state is still
# "scanning", so once the provider advances the phase it goes quiet (no flicker).
"$PY" "$REPO_DIR/scripts/saps_activity.py" write scanning "scan: starting" 2>/dev/null || true
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
    if [ -n "$_total" ]; then _qpart="${_q}/${_total} queries"; else _qpart="${_q} queries"; fi
    _found=$(grep -oE "Batch has [0-9]+" "$OUT" 2>/dev/null | tail -1 | grep -oE "[0-9]+" | tail -1 || true)
    if [ -n "$_found" ]; then
      _lbl="scan: ${_dur} · ${_qpart}, ${_found} found"
    else
      _lbl="scan: ${_dur} · ${_qpart}"
    fi
    "$PY" "$REPO_DIR/scripts/saps_activity.py" heartbeat scanning "$_lbl" 2>/dev/null || true
  done
) &
HB_PID=$!

# Engagement mode (2026-06-26). The menu-bar toggle writes mode.json; this reads
# it and, in personal_brand mode, exports SAPS_FORCE_PROJECT=<persona project> and
# TWITTER_TAIL_LINK_RATE=0 so the (locked) cycle below drafts link-free organic
# replies for the persona instead of the normal weighted product pick. In the
# default promotion mode it exports nothing and the cycle runs exactly as before.
# Read at cycle runtime (NOT baked into the plist) so flipping the toggle takes
# effect on the very next cycle with no launchd reload. Best-effort: any failure
# leaves the env untouched and the promotion pipeline runs.
eval "$("$PY" "$REPO_DIR/scripts/saps_mode.py" env 2>/dev/null || true)"
if [ -n "${SAPS_FORCE_PROJECT:-}" ]; then
    echo "[run-draft-and-publish] personal_brand mode: forcing project '$SAPS_FORCE_PROJECT' (link-free)" >&2
fi

# Run the cycle; tee stdout so we can scan it for the DRAFT_ONLY_PLAN marker.
# Phase 2b blocks on the queue until the worker drafts it, so this can take a
# few minutes — that is expected.
bash "$REPO_DIR/skill/run-twitter-cycle.sh" 2>&1 | tee "$OUT"
RC=${PIPESTATUS[0]}

# Deliver the cycle's drafts into the cards.
MARKER="$(grep -oE 'DRAFT_ONLY_PLAN=\S+\.json' "$OUT" | tail -1)"
if [ -n "$MARKER" ]; then
    "$PY" "$REPO_DIR/scripts/merge_review_queue.py" --plan-from-marker "$MARKER" || true
else
    echo "[run-draft-and-publish] no DRAFT_ONLY_PLAN marker (cycle rc=$RC); nothing to merge" >&2
fi

exit "$RC"
