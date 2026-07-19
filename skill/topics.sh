#!/bin/bash
# Shared search-topic picker helper. Mirrors skill/styles.sh exactly.
# Usage:
#   source topics.sh
#   ASSIGN_FILE=$(mktemp -t s4l_topic_assign_XXXXXX.json)
#   ASSIGNMENT=$(s4l_pick_topic studyly twitter "$ASSIGN_FILE")
#   PICKED_TOPIC=$(echo "$ASSIGNMENT" | python3 -c "import json,sys; print((json.load(sys.stdin).get('search_topic') or ''))")
#   TOPIC_BLOCK=$(s4l_render_topic_block "$ASSIGN_FILE")
# Requires REPO_DIR to be set before sourcing.
#
# Architecture (2026-05-26 picker rollout):
#   - s4l_pick_topic: per-project programmatic search_topic picker. Emits the
#     assignment JSON to stdout AND writes it to the optional outfile path so
#     a sibling shell var can keep the path around and re-read it later.
#     Replaces the legacy "show all search_topics[], let the model improvise"
#     pattern, which produced inconsistent per-tweet topic stamps and made
#     end-to-end attribution noisy.
#   - s4l_render_topic_block: turns an assignment JSON file into the compact
#     prompt block (one assigned topic + trusted top-N reference context).
#   - Mirrors styles.sh's s4l_pick_style / s4l_render_style_block.

s4l_pick_topic() {
  local project="$1"
  local platform="${2:-twitter}"
  local outfile="${3:-}"
  python3 -c "
import json, sys
sys.path.insert(0, '$REPO_DIR/scripts')
from pick_search_topic import pick_topic_for_project
assignment = pick_topic_for_project('$project', platform='$platform')
if assignment is None:
    sys.exit(2)
out = '$outfile'
if out:
    with open(out, 'w') as f:
        json.dump(assignment, f)
print(json.dumps(assignment))
" 2>/dev/null || echo '{"mode":"cold_start","search_topic":null,"project":"'"$project"'","platform":"'"$platform"'","reference_topics":[],"universe_size":0,"trusted_n":0,"cold_n":0}'
}

s4l_render_topic_block() {
  local assign_file="$1"
  python3 -c "
import json, sys
sys.path.insert(0, '$REPO_DIR/scripts')
from pick_search_topic import get_assigned_topic_prompt
with open('$assign_file', 'r') as f:
    assignment = json.load(f)
print(get_assigned_topic_prompt(assignment))
" 2>/dev/null || echo "(topic picker unavailable)"
}
