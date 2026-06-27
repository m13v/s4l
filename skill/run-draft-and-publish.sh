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
trap 'rm -f "$OUT"' EXIT

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
