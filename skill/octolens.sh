#!/usr/bin/env bash
# Octolens mention engagement - find mentions via Octolens and engage.
#
# Usage:
#   octolens.sh                    Legacy: cross-platform pool, Claude picks best 5-10.
#   octolens.sh --platform reddit  Only Reddit mentions, engaged via reddit-agent.
#   octolens.sh --platform twitter
#   octolens.sh --platform linkedin

set -euo pipefail

# Cycle ID for cross-cycle cost accounting (matches the pattern in
# run-reddit-search.sh, engage-reddit.sh, dm-outreach-*.sh, link-edit-*.sh,
# etc.). Every claude session spawned in this script inherits SA_CYCLE_ID via
# env so log_claude_session.py stamps claude_sessions.cycle_id. Lets
# get_run_cost.py --cycle-id report THIS run's spend instead of bleeding into
# overlapping engagement cycles.
BATCH_ID="${BATCH_ID:-octolens-$(date +%Y%m%d-%H%M%S)}"
export BATCH_ID
export SA_CYCLE_ID="$BATCH_ID"

# Parse args.
PLATFORM=""
while [ $# -gt 0 ]; do
    case "$1" in
        --platform)    PLATFORM="${2:-}"; shift 2 ;;
        --platform=*)  PLATFORM="${1#--platform=}"; shift ;;
        *)             shift ;;
    esac
done

case "$PLATFORM" in
    ""|reddit|twitter|linkedin) ;;
    *)
        echo "octolens.sh: invalid --platform '$PLATFORM' (expected reddit, twitter, or linkedin)" >&2
        exit 2
        ;;
esac

# Per-platform lock name so all three can run in parallel without stepping on
# each other, but repeat invocations of the same platform queue up.
LOCK_NAME="octolens${PLATFORM:+-$PLATFORM}"

# Browser-profile lock first (shared across pipelines that use the same browser),
# then the pipeline-specific lock. Alphabetical for multi-platform runs.
#
# Reddit uses the unified Python lease (2026-05-10) — TTL-aware, auto-decays
# during Claude reasoning gaps so peer pipelines can use the profile. The
# MCP proxy heartbeats expires_at on every reddit-agent call, so the lease
# stays held during real activity. LinkedIn/Twitter still use the bash lock
# (no MCP-proxy heartbeat wiring for those agents yet).
source "$(dirname "$0")/lock.sh"
REPO_DIR_FOR_LOCK="$HOME/social-autoposter"
_release_reddit_lease() {
    timeout 3 python3 "$REPO_DIR_FOR_LOCK/scripts/reddit_browser_lock.py" release 2>/dev/null || true
}
case "${PLATFORM:-all}" in
    linkedin) acquire_lock "linkedin-browser" 3600 ;;
    reddit)
        python3 "$REPO_DIR_FOR_LOCK/scripts/reddit_browser_lock.py" acquire --timeout 3600 --ttl 90 2>&1 || \
            echo "WARNING: reddit_browser_lock.py acquire failed; proceeding without lease."
        trap '_release_reddit_lease; _sa_release_locks' EXIT INT TERM HUP
        ;;
    twitter|x) acquire_lock "twitter-browser" 3600 ;;
    all)
        acquire_lock "linkedin-browser" 3600
        python3 "$REPO_DIR_FOR_LOCK/scripts/reddit_browser_lock.py" acquire --timeout 3600 --ttl 90 2>&1 || \
            echo "WARNING: reddit_browser_lock.py acquire failed; proceeding without lease."
        trap '_release_reddit_lease; _sa_release_locks' EXIT INT TERM HUP
        acquire_lock "twitter-browser" 3600
        ;;
esac
acquire_lock "$LOCK_NAME" 3600

cd ~/social-autoposter

# Load env
set -a; source .env 2>/dev/null || true; set +a

LOG_DIR="skill/logs"
mkdir -p "$LOG_DIR"
LOG_TAG="${PLATFORM:-all}"
LOG_FILE="$LOG_DIR/octolens-${LOG_TAG}-$(date +%Y-%m-%d_%H%M%S).log"

RUN_START=$(date +%s)
REPO_DIR="$HOME/social-autoposter"
echo "=== Octolens Engagement Run (${LOG_TAG}): $(date) ===" | tee "$LOG_FILE"

# Find candidates from Octolens API, narrowed to this platform if requested.
FETCH_ARGS=(--from-api --limit 20)
[ -n "$PLATFORM" ] && FETCH_ARGS+=(--platform "$PLATFORM")

echo "Fetching Octolens mentions (${LOG_TAG})..." | tee -a "$LOG_FILE"
CANDIDATES=$(python3 scripts/octolens_threads.py "${FETCH_ARGS[@]}" 2>>"$LOG_FILE")
echo "$CANDIDATES" >> "$LOG_FILE"

CANDIDATE_COUNT=$(echo "$CANDIDATES" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('candidates',[])))" 2>/dev/null || echo "0")
echo "Found $CANDIDATE_COUNT candidates" | tee -a "$LOG_FILE"

# Log-run tag so the dashboard can tell platforms apart.
LOG_RUN_SCRIPT="octolens${PLATFORM:+-$PLATFORM}"

if [ "$CANDIDATE_COUNT" = "0" ]; then
    echo "No new candidates to engage with." | tee -a "$LOG_FILE"
    RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
    python3 "$REPO_DIR/scripts/log_run.py" --script "$LOG_RUN_SCRIPT" --posted 0 --skipped 0 --failed 0 --cost 0 --elapsed "$RUN_ELAPSED"
    find "$LOG_DIR" -name "octolens-*.log" -mtime +7 -delete 2>/dev/null || true
    exit 0
fi

# Build the platform-specific engagement instructions for Claude.
if [ -n "$PLATFORM" ]; then
    case "$PLATFORM" in
        reddit)   AGENT_HINT="Use ONLY mcp__reddit-agent__* tools for posting." ;;
        twitter)  AGENT_HINT="Use ONLY the mcp__twitter-harness__bh_run tool for posting (CDP-driven real Chrome on port 9555). See lib/twitter-backend.sh BROWSER_INSTRUCTIONS for the Playwright -> bh_run translation table." ;;
        linkedin) AGENT_HINT="Use ONLY the mcp__linkedin-harness__bh_run tool for posting (CDP-driven real Chrome on port 9556). See lib/linkedin-backend.sh BROWSER_INSTRUCTIONS for the Playwright -> bh_run translation table. The LinkedIn comment box is contenteditable: click_at_xy the computed center, type_text, then click the Post/Comment button (never press Enter)." ;;
    esac
    PLATFORM_GATE="IMPORTANT: This is the Octolens ${PLATFORM} engagement pipeline. All candidates are already filtered to ${PLATFORM}. ${AGENT_HINT} NEVER use any other browser agent."
else
    PLATFORM_GATE="IMPORTANT: This is the Octolens engagement pipeline. Use ONLY mcp__reddit-agent__* for Reddit, mcp__twitter-harness__bh_run for Twitter, mcp__linkedin-harness__bh_run for LinkedIn. NEVER use mcp__playwright-extension__*, mcp__isolated-browser__*, or mcp__macos-use__*."
fi

# Run Claude with the social-autoposter skill to engage
echo "Starting Claude engagement..." | tee -a "$LOG_FILE"
echo "$CANDIDATES" | "$REPO_DIR/scripts/run_claude.sh" "octolens" --strict-mcp-config --mcp-config "$HOME/.claude/browser-agent-configs/all-agents-mcp.json" -p "You are running the social-autoposter Octolens engagement workflow.

Here are the Octolens mention candidates (JSON):
$(echo "$CANDIDATES")

${PLATFORM_GATE}
- Max 10 Octolens-sourced posts per run
- Check: SELECT COUNT(*) FROM posts WHERE source_summary LIKE '%octolens%' AND posted_at >= NOW() - INTERVAL '24 hours'
- If >= 100 octolens posts in 24h, stop. Otherwise proceed.

Pick the BEST 5-10 candidates to engage with. Prioritize:
1. buy_intent or product_question tags (someone looking for a solution)
2. Negative competitor mentions on Reddit (opportunity to suggest alternative)
3. High-follower authors on Twitter/X
4. Reddit threads with active discussion
5. Skip tweets that are just replies to other tweets (low visibility)
6. Skip [removed] or empty content posts

For each picked candidate, follow the standard social-autoposter posting flow:
- Read the full thread/post via browser to understand context
- Draft a natural comment following content_angle from config.json
- Post via browser automation per the platform rule above
- Determine project_name by matching thread topic to config.json projects[].topics
- Log to the posts table with source_summary = 'octolens: [keyword]' (MUST include project_name)

Skip if nothing fits naturally. Config is at ~/social-autoposter/config.json" 2>&1 | tee -a "$LOG_FILE"

echo "=== Done (${LOG_TAG}): $(date) ===" | tee -a "$LOG_FILE"

# Log run to persistent monitor
RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
_COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START" --scripts "octolens" 2>/dev/null || echo "0.0000")
POSTED=$(grep -c "INSERT INTO posts" "$LOG_FILE" 2>/dev/null) || true
SKIPPED=$(grep -ci "skipped\|skip" "$LOG_FILE" 2>/dev/null) || true
FAILED=$(grep -ci "error\|failed\|FAILED" "$LOG_FILE" 2>/dev/null) || true
python3 "$REPO_DIR/scripts/log_run.py" --script "$LOG_RUN_SCRIPT" --posted "$POSTED" --skipped "$SKIPPED" --failed "$FAILED" --cost "$_COST" --elapsed "$RUN_ELAPSED"

# Clean up old logs (keep last 7 days)
find "$LOG_DIR" -name "octolens-*.log" -mtime +7 -delete 2>/dev/null || true
