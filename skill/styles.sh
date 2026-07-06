#!/bin/bash
# Shared engagement styles helper.
# Usage:
#   source styles.sh
#   ASSIGN_FILE=$(mktemp -t s4l_style_assign_XXXXXX.json)
#   ASSIGNMENT=$(s4l_pick_style twitter posting "$ASSIGN_FILE")
#   PICKED_STYLE=$(echo "$ASSIGNMENT" | python3 -c "import json,sys; print((json.load(sys.stdin).get('style') or ''))")
#   STYLES_BLOCK=$(s4l_render_style_block "$ASSIGN_FILE" twitter posting)
# Requires REPO_DIR to be set before sourcing.
#
# Architecture (2026-05-19 picker rollout):
#   - s4l_pick_style: programmatic style picker. Emits the assignment JSON to
#     stdout AND writes it to the optional outfile path so a sibling shell var
#     can keep the path around and re-read it later. Replaces the legacy
#     "show all styles, let the model pick" pattern.
#   - s4l_render_style_block: turns an assignment JSON file into the compact
#     prompt block (one assigned style + description + example + note, or the
#     invent block with top-N references) plus content rules + anti-patterns
#     + grounding rule.
#   - generate_styles_block: legacy wrapper (pick + render in one go). Retained
#     for shell callers that don't need the picked style downstream (rare; most
#     now want it to filter top_performers and to log drift).

# Pick a style and emit the assignment as JSON to stdout. Optionally also
# writes the JSON to $3 (an outfile path).
s4l_pick_style() {
  local platform="$1"
  local context="${2:-posting}"
  local outfile="${3:-}"
  python3 -c "
import json, sys
sys.path.insert(0, '$REPO_DIR/scripts')
from engagement_styles import pick_style_for_post
assignment = pick_style_for_post('$platform', context='$context')
out = '$outfile'
if out:
    with open(out, 'w') as f:
        json.dump(assignment, f)
print(json.dumps(assignment))
" 2>/dev/null || echo '{"mode":"use","style":null,"description":null,"example":null,"note":null,"reference_styles":[],"distribution_snapshot":[]}'
}

# Render the compact prompt block from an assignment JSON file.
# Includes the styles block + content rules + anti-patterns + grounding rule
# (the grounding rule is bundled inside get_assigned_style_prompt) +
# voice relationship rule (introduced 2026-05-27 so the model knows whether
# to speak AS the matched project's maker or as an outside observer; per
# project the rule reads voice_relationship in config.json).
s4l_render_style_block() {
  local assign_file="$1"
  local platform="$2"
  local context="${3:-posting}"
  python3 -c "
import json, sys
sys.path.insert(0, '$REPO_DIR/scripts')
from engagement_styles import (
    get_assigned_style_prompt, get_content_rules, get_anti_patterns,
    get_voice_relationship_rule,
)
with open('$assign_file', 'r') as f:
    assignment = json.load(f)
print(get_assigned_style_prompt('$platform', assignment, context='$context'))
print()
print(get_voice_relationship_rule())
print()
print('## Content rules')
print(get_content_rules('$platform'))
print()
print(get_anti_patterns())
" 2>/dev/null || echo "(style module unavailable)"
}

# Legacy: pick + render in one call, no assignment exposed to the caller.
# Equivalent to the pre-2026-05-19 behavior except the prompt now assigns one
# style instead of listing all of them.
generate_styles_block() {
  local platform="$1"
  local context="${2:-posting}"
  local tmpfile
  tmpfile=$(mktemp -t s4l_style_assign_XXXXXX.json) || {
    echo "(style module unavailable: mktemp failed)"
    return
  }
  s4l_pick_style "$platform" "$context" "$tmpfile" >/dev/null
  s4l_render_style_block "$tmpfile" "$platform" "$context"
  rm -f "$tmpfile"
}
