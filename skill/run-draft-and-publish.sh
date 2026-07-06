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

# Salvage twitter-prep drafts orphaned when a producer cycle died after its worker
# wrote the result but before consuming it (a consumed result is deleted, so a
# surviving one in claude-queue/result/ = never consumed). Best-effort, dedup-safe
# merge into the review queue; the default age gate only touches results past the
# queue timeout, so no live producer is racing. Runs every tick so a dead
# producer's drafts land within a cycle instead of being lost. Never blocks.
"$PY" "$REPO_DIR/scripts/salvage_orphaned_prep_results.py" >/dev/null 2>&1 || true

# Posting-defer gate (2026-07-06). Posting and scanning share ONE harness Chrome,
# and the MCP poster SIGKILLs any scan holding the twitter-browser lock (it cannot
# wait: an approved card must land while the user watches, and scans trap SIGTERM).
# The poster always wins, so a cycle launched into an active posting window only
# gets killed 1-5 min in (rc=137, no plan, wasted scan) — 6 of 9 cycles died this
# way on 2026-07-06 while a 17-card approval batch drained. Don't start a cycle
# that is born to die: defer this tick and let launchd refire in 60s. Two signals,
# either one defers:
#   1. posting-active.json fresh (expires_at in the future) — the cross-instance
#      flag the poster heartbeats through the whole batch (mcp 1.6.94).
#   2. a post-*.log younger than 90s — every posted card writes one at completion,
#      so this covers per-card gaps and installed MCPs that predate the flag. 90s
#      outlives the poster's 60s lock grace-hold by design.
_posting_reason=""
_flag="${S4L_STATE_DIR:-$HOME/.social-autoposter-mcp}/posting-active.json"
if [ -f "$_flag" ] && "$PY" -c '
import json, sys, time
try:
    j = json.load(open(sys.argv[1]))
    sys.exit(0 if j.get("expires_at", 0) > time.time() * 1000 else 1)
except Exception:
    sys.exit(1)
' "$_flag" 2>/dev/null; then
    _posting_reason="posting-active.json fresh"
elif [ -n "$(find "$REPO_DIR/skill/logs" -maxdepth 1 -name 'post-*.log' -mtime -90s 2>/dev/null | head -1)" ]; then
    _posting_reason="post-*.log younger than 90s"
fi
if [ -n "$_posting_reason" ]; then
    echo "[run-draft-and-publish] posting active ($_posting_reason); deferring this cycle — launchd retries in 60s" >&2
    exit 0
fi

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

# First-run onboarding boost (2026-07-02; env plumbing removed 2026-07-06).
# The MCP server drops first-run-boost.json into the state dir when it
# installs the kicker for the very first time. run-twitter-cycle.sh reads the
# marker file DIRECTLY and widens its freshness windows to a hardcoded 48h
# while it exists; the S4L_DRAFT_FRESHNESS_HOURS and
# S4L_FIRST_RUN_FRESHNESS_HOURS env vars are retired. This wrapper only owns
# the marker lifecycle: expire it after 24h without cards, consume it the
# moment a merge delivers cards, so the widened window applies to exactly one
# successful first batch.
BOOST_MARKER="${S4L_STATE_DIR:-$HOME/.social-autoposter-mcp}/first-run-boost.json"
BOOST_ACTIVE=0
if [ -f "$BOOST_MARKER" ]; then
    if [ -n "$(find "$BOOST_MARKER" -mmin +1440 2>/dev/null)" ]; then
        rm -f "$BOOST_MARKER"
        echo "[run-draft-and-publish] first-run boost expired (>24h, no cards produced); removed" >&2
    else
        BOOST_ACTIVE=1
        echo "[run-draft-and-publish] first-run boost active: cycle reads the marker and widens discovery to 48h" >&2
    fi
fi

# Top-N cap REMOVED (per user, 2026-07-06): every draft the worker produces goes
# through — review cards in DRAFT_ONLY, bar-gated posts in the autopilot lane —
# instead of "top-1 kept, rest deferred". 0 is the locked cycle's documented env
# opt-out (run-twitter-cycle.sh POST_TOP_N: "0 = no cap, env opt-out only"). The
# rolling p0.90 virality bar stays as the volume/quality valve. An operator can
# re-cap by exporting S4L_TWITTER_POST_TOP_N=<n> in the environment. This also
# supersedes the first-run boost's old top_n=5 export: no cap ≥ boost.
export S4L_TWITTER_POST_TOP_N="${S4L_TWITTER_POST_TOP_N:-0}"

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
