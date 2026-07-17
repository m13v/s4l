#!/usr/bin/env bash
# stats.sh — Full stats pipeline:
#   Step 1: Reddit profile scrape (headless Playwright, views + upvotes + comments_count)
#   Step 2: API stats (deletion/removal detection + stats fallback) via Python
#   Step 3: X/Twitter stats via Claude + Playwright (browser required)
#   Step 4: LinkedIn stats (delegates to skill/stats-linkedin.sh, CDP-attach
#           to the linkedin-harness Chrome; NOT Claude-driven, no browser
#           tool calls -- the "via Claude + Playwright" description here was
#           stale, describing the pre-2026-05-11 implementation. The
#           scheduled LinkedIn stats job (com.m13v.social-stats-linkedin.plist)
#           calls stats-linkedin.sh directly and never reaches this file at
#           all; this Step 4 path only runs on a manual `stats.sh --platform
#           linkedin` or no-flag invocation, both still correct, just unused
#           by the current schedule)
# Called by launchd every 6 hours.
#
# Args (any order):
#   --platform <reddit|twitter|linkedin|moltbook>  Run only the steps for one platform.
#   --quiet                                        Minimal Python output.
# If --platform is omitted, all steps run (backward-compatible default).

set -uo pipefail

# Portable platform helpers (defines gtimeout shim for Linux). This is sourced
# early so the `gtimeout` function is available. Note: platform.sh exports a
# variable also named PLATFORM (darwin/linux), which stats.sh's arg parser
# immediately overwrites with the social-platform name below; that is fine
# because stats.sh never calls stat_mtime/platform_notify after arg parsing.
# shellcheck source=/dev/null
source "$(dirname "${BASH_SOURCE[0]}")/lib/platform.sh"

REPO_DIR="$HOME/social-autoposter"
SKILL_FILE="$REPO_DIR/SKILL.md"
LOG_DIR="$REPO_DIR/skill/logs"

# shellcheck source=/dev/null
source "$(dirname "${BASH_SOURCE[0]}")/lock.sh"

# Parse args (support --platform <name> and --quiet in any order).
QUIET=""
PLATFORM=""
while [ $# -gt 0 ]; do
    case "$1" in
        --platform)
            PLATFORM="${2:-}"
            shift 2
            ;;
        --platform=*)
            PLATFORM="${1#--platform=}"
            shift
            ;;
        --quiet)
            QUIET="--quiet"
            shift
            ;;
        *)
            # Unknown arg: ignore (keeps backward compatibility with callers).
            shift
            ;;
    esac
done

# Validate --platform if provided.
case "$PLATFORM" in
    ""|reddit|twitter|linkedin|moltbook)
        ;;
    *)
        echo "stats.sh: invalid --platform '$PLATFORM' (expected reddit, twitter, linkedin, or moltbook)" >&2
        exit 2
        ;;
esac

# Decide which steps to run.
# Variable naming: RUN_STEP1 = Reddit profile scrape, RUN_STEP2 = API stats.
# No --platform means "all" (legacy behavior, kept for manual invocations).
if [ -z "$PLATFORM" ]; then
    RUN_STEP1=1; RUN_STEP2=1; RUN_STEP3=1; RUN_STEP4=1
else
    # Per-platform mode: default everything off, then enable per platform.
    RUN_STEP1=0; RUN_STEP2=0; RUN_STEP3=0; RUN_STEP4=0
    case "$PLATFORM" in
        reddit)   RUN_STEP1=1; RUN_STEP2=1 ;;  # scrape then API.
        twitter)  RUN_STEP3=1 ;;                # Step 3 handles Twitter API directly.
        linkedin) RUN_STEP4=1 ;;                # LinkedIn has no cheap API leg.
        moltbook) RUN_STEP2=1 ;;                # API-only, covered by Step 2.
    esac
fi

# Load secrets (MOLTBOOK_API_KEY, DATABASE_URL, etc.)
# shellcheck source=/dev/null
[ -f "$REPO_DIR/.env" ] && source "$REPO_DIR/.env"

mkdir -p "$LOG_DIR"
# Include platform in log filename so the dashboard can distinguish per-platform runs.
LOG_TAG="${PLATFORM:-all}"
LOGFILE="$LOG_DIR/stats-${LOG_TAG}-$(date +%Y-%m-%d_%H%M%S).log"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$LOGFILE"; echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

RUN_START=$(date +%s)
STEP1_EXIT=0; STEP2_EXIT=0; STEP3_EXIT=0; STEP4_EXIT=0

log "=== Stats Pipeline Run: $(date) ==="
if [ -n "$PLATFORM" ]; then
    log "Platform filter: $PLATFORM (step1=$RUN_STEP1 step2=$RUN_STEP2 step3=$RUN_STEP3 step4=$RUN_STEP4)"
else
    log "Platform filter: (none, running all steps)"
fi

# ═══════════════════════════════════════════════════════
# STEP 1: Reddit profile scrape (headless Playwright, no Claude session).
# Runs BEFORE Step 2 so thread + comment rows get views/upvotes/comments_count
# in a single no-API pass. Step 2 then skips rows refreshed within the last 4h
# and spends the API budget only on deletion detection + unmatched rows.
# ═══════════════════════════════════════════════════════
if [ "$RUN_STEP1" -eq 1 ]; then
log "Step 1: Reddit profile scrape (headless Playwright)"

# Serialize with other reddit-agent consumers (post_reddit, run-reddit-threads,
# engage-dm-replies, audit-reddit*). Unified Python lease (2026-05-10) —
# TTL-aware, MCP-proxy heartbeated during reddit-agent calls.
python3 "$REPO_DIR/scripts/reddit_browser_lock.py" acquire --timeout 3600 --ttl 90 2>&1 || \
    log "WARNING: reddit_browser_lock.py acquire failed; proceeding without lease."

REDDIT_USERNAME=$(python3 -c "import json; print(json.load(open('$REPO_DIR/config.json'))['accounts']['reddit']['username'])" 2>/dev/null || echo "")

if [ -n "$REDDIT_USERNAME" ]; then
    SCRAPE_OUT=$(mktemp)
    gtimeout 900 python3 "$REPO_DIR/scripts/reddit_browser.py" scrape-views "$REDDIT_USERNAME" 300 > "$SCRAPE_OUT" 2>> "$LOGFILE"
    STEP1_EXIT=$?
    if [ "$STEP1_EXIT" -eq 124 ]; then
        log "Step 1: TIMEOUT (15 min limit reached)"
        rm -f "$SCRAPE_OUT"
    elif [ "$STEP1_EXIT" -ne 0 ]; then
        log "Step 1: FAILED scrape-views (exit $STEP1_EXIT)"
        head -c 500 "$SCRAPE_OUT" >> "$LOGFILE" 2>/dev/null || true
        rm -f "$SCRAPE_OUT"
    else
        # Extract the .results array into the format scrape_reddit_views.py expects.
        python3 -c "
import json, sys
with open('$SCRAPE_OUT') as f:
    data = json.load(f)
if not data.get('ok'):
    print('scrape_views returned ok=false:', data.get('error', 'unknown'), file=sys.stderr)
    sys.exit(2)
with open('/tmp/reddit_views.json', 'w') as f:
    json.dump(data.get('results', []), f)
print(f\"scraped {data.get('total', 0)} urls, {data.get('with_views', 0)} with views, {data.get('with_score', 0)} with score, {data.get('with_comments_count', 0)} with comments_count\")
" >> "$LOGFILE" 2>&1
        EXTRACT_EXIT=$?
        rm -f "$SCRAPE_OUT"
        if [ "$EXTRACT_EXIT" -ne 0 ]; then
            log "Step 1: FAILED extract (exit $EXTRACT_EXIT)"
        else
            python3 "$REPO_DIR/scripts/scrape_reddit_views.py" --from-json /tmp/reddit_views.json $QUIET >> "$LOGFILE" 2>&1
            UPDATE_EXIT=$?
            if [ "$UPDATE_EXIT" -ne 0 ]; then
                log "Step 1: FAILED DB update (exit $UPDATE_EXIT)"
            else
                log "Step 1: Done"
            fi
        fi
    fi
else
    log "Step 1: SKIPPED, no Reddit username in config.json"
fi
# Release the reddit-browser lock NOW. Step 2 (stats.py --reddit-only)
# is pure unauthenticated HTTPS to old.reddit.com/api/info.json: no Playwright,
# no logged-in session, different rate-limit bucket from reddit-agent. Holding
# the lock through Step 2's paced API loop (~100 req / 10 min) starves the
# post_reddit + run-reddit-search + dm-replies queue for 5-15 min every cycle.
# Releasing here keeps Step 1's serialization guarantee for the actual browser
# work and frees the queue immediately.
python3 "$REPO_DIR/scripts/reddit_browser_lock.py" release 2>/dev/null || true
log "Step 1: released reddit-browser lease (Step 2 is HTTP-only)"
else
    log "Step 1: SKIPPED (platform=$PLATFORM)"
fi

# ═══════════════════════════════════════════════════════
# STEP 2: API stats — deletion/removal detection and stats fallback for any
# row Step 1 couldn't cover. Rows refreshed by Step 1 within the last 4h
# are skipped via the engagement_updated_at freshness window.
# ═══════════════════════════════════════════════════════
# Sidecar JSON written by stats.py --reply-summary so we can forward
# the per-platform reply-refresh count to log_run.py at the end of the run.
# The Python side writes {reddit, twitter, github} integers (zeros if a
# platform's reply pass didn't run).
REPLY_SUMMARY_FILE=$(mktemp -t fazm-reply-summary.XXXXXX)
# Sidecar JSON written by scrape_linkedin_stats.py --summary so we can forward
# LinkedIn-specific counters (refreshed/removed/unavailable/not_found) into
# log_run.py. Step 4's Claude-driven prompt invokes the Python script with
# --summary "$LINKEDIN_SUMMARY_FILE", so the file is populated only if Step 4
# ran end-to-end. Empty file means LinkedIn contributed 0 to every counter.
LINKEDIN_SUMMARY_FILE=$(mktemp -t fazm-linkedin-summary.XXXXXX)
# Chain lock cleanup. A plain `trap '...' EXIT` would REPLACE lock.sh's
# `trap _sa_release_locks EXIT INT TERM HUP`, orphaning the platform-browser
# lock across runs. Cover all four signals so watchdog SIGTERM also frees it.
trap 'rm -f "$REPLY_SUMMARY_FILE" "$LINKEDIN_SUMMARY_FILE"; _sa_release_locks' EXIT INT TERM HUP

if [ "$RUN_STEP2" -eq 1 ]; then
    # Narrow the Python call per platform. Without --platform we run the
    # default all-platforms pass (kept for manual invocations only).
    STEP2_ARGS=()
    [ "$QUIET" = "--quiet" ] && STEP2_ARGS+=("--quiet")
    STEP2_ARGS+=("--reply-summary" "$REPLY_SUMMARY_FILE")
    case "$PLATFORM" in
        reddit)   STEP2_ARGS+=("--reddit-only") ;;
        moltbook) STEP2_ARGS+=("--moltbook-only") ;;
        twitter)  STEP2_ARGS+=("--twitter-only") ;;
    esac

    log "Step 2: API stats (Python) ${STEP2_ARGS[*]:-}"
    python3 "$REPO_DIR/scripts/stats.py" "${STEP2_ARGS[@]}" >> "$LOGFILE" 2>&1
    STEP2_EXIT=$?
    if [ "$STEP2_EXIT" -ne 0 ]; then
        log "Step 2: FAILED (exit $STEP2_EXIT), continuing to next step"
    else
        log "Step 2: Done"
    fi
else
    log "Step 2: SKIPPED (platform=$PLATFORM)"
fi

# ═══════════════════════════════════════════════════════
# STEP 3: X/Twitter stats (API via fxtwitter, no browser needed)
# ═══════════════════════════════════════════════════════
if [ "$RUN_STEP3" -eq 1 ]; then
    log "Step 3: X/Twitter stats (API via fxtwitter)"
    STEP3_ARGS=("--twitter-only" "--reply-summary" "$REPLY_SUMMARY_FILE")
    [ "$QUIET" = "--quiet" ] && STEP3_ARGS+=("--quiet")
    python3 "$REPO_DIR/scripts/stats.py" "${STEP3_ARGS[@]}" >> "$LOGFILE" 2>&1
    STEP3_EXIT=$?
    if [ "$STEP3_EXIT" -ne 0 ]; then
        log "Step 3: FAILED (exit $STEP3_EXIT)"
    else
        log "Step 3: Done"
    fi
else
    log "Step 3: SKIPPED (platform=$PLATFORM)"
fi

# ═══════════════════════════════════════════════════════
# STEP 4: LinkedIn stats (Python CDP-attach to linkedin-agent MCP)
#
# Cutover 2026-05-04: replaced the Claude-driven `run_claude.sh stats-step4`
# heredoc-prompt path with a direct Python script that CDP-attaches to the
# already-running linkedin-agent MCP, scrapes per-comment reactions, and
# applies the same DB write-path (scrape_linkedin_stats.update_linkedin_stats).
# Same data, $0 cost instead of $1-3 per run, 3-5 min instead of 5-10 min.
# get_run_cost.py --scripts stats-step4 will return $0 going forward; that
# is correct, not a missed run.
#
# Prereqs: linkedin-agent MCP must be alive (Chrome with --remote-debugging-port
# already running). The post pipeline fires every 15min and primes the browser,
# so in steady state DevToolsActivePort is always live. If MCP is cold the
# script returns mcp_not_running / exit 1; stats.sh logs the leg as failed.
#
# Lock policy: acquire the bash linkedin-browser lock for 1800s so we
# serialize against run-linkedin.sh / engage-linkedin.sh /
# dm-outreach-linkedin.sh / engage-dm-replies.sh (all of which acquire the
# same lock for 3600s). The earlier Claude-driven Step 4 did NOT acquire
# this lock, which let it race the post pipeline; the cutover closes that
# gap. The lock's own ppid==1 orphan-Chrome sweep handles dead Chromes;
# ensure_browser_healthy is intentionally NOT called here (see inline note
# at the call site for the --remote-debugging-port=0 incompatibility).
# ═══════════════════════════════════════════════════════
if [ "$RUN_STEP4" -eq 1 ]; then
log "Step 4: LinkedIn stats (Python CDP-attach to linkedin-agent)"

# PATH hardening: launchd / nohup / cron environments don't inherit the
# interactive shell PATH, so `gtimeout` and `python3` may not resolve.
# Pin to absolute Homebrew + system paths. /usr/bin/python3 is the only
# python on this Mac with playwright installed in user-site (see CLAUDE.md
# "Programmatic Gmail Access" + engage-dm-replies.sh:1314 for the same
# convention). 2026-05-05 cutover bug: bare `python3` in nohup shells
# resolved to /opt/homebrew/bin/python3 which has psycopg2 but NOT
# playwright, causing ModuleNotFoundError on every Step 4 fire.
GTIMEOUT_BIN="/opt/homebrew/bin/gtimeout"
PY_BIN="/usr/bin/python3"

# HTTP-only lane (2026-06-01): the LinkedIn refresh-eligibility count goes
# through the s4l.ai API via scripts/stats_helper.py. No DATABASE_URL, no
# psql, no fallback. Prints the same integer the old psql COUNT(*) did.
LINKEDIN_POSTS=$(python3 "$REPO_DIR/scripts/stats_helper.py" linkedin-refresh-count 2>/dev/null || echo "0")

if [ "$LINKEDIN_POSTS" -gt 0 ]; then
    acquire_lock "linkedin-browser" 1800
    # Deliberately do NOT call ensure_browser_healthy here. That helper
    # reads --remote-debugging-port from the Chrome cmdline, but the
    # linkedin-agent MCP launches Chrome with `--remote-debugging-port=0`
    # (let Chrome pick a random port; actual port written to
    # DevToolsActivePort). Result: ensure_browser_healthy reads `0`, probes
    # http://localhost:0, fails, then KILLS the perfectly healthy Chrome —
    # which is the opposite of what we want. The bash lock's orphan-Chrome
    # sweep (ppid==1 filter) already handles the truly-dead case, and our
    # Python script CDP-attaches via DevToolsActivePort so it discovers the
    # real port without needing the cmdline value. If MCP is genuinely cold,
    # the script falls back to launch_persistent_context on the SAME
    # ~/.claude/browser-profiles/linkedin profile the MCP uses (verified
    # against linkedin_browser.PROFILE_DIR), so cookies + fingerprint match
    # the post pipeline regardless of which lifecycle mode is active.

    # 2026-05-11: scrape_linkedin_stats_browser.py was deprecated 2026-05-05
    # (the per-permalink scrape loop pattern triggered LinkedIn's anti-bot on
    # 2026-05-05). It still exits 2. We now call skill/stats-linkedin.sh, the
    # unified orchestrator: one CDP-attached scrape of /in/me/recent-activity/
    # comments/, two DB writers (replies + posts tables sharing one feed).
    # The orchestrator manages its OWN linkedin-browser lock acquire/release
    # internally, so we release ours first to avoid a self-deadlock.
    release_lock "linkedin-browser"
    "$GTIMEOUT_BIN" 1800 bash "$REPO_DIR/skill/stats-linkedin.sh" \
        >> "$LOGFILE" 2>&1
    STEP4_EXIT=$?
    # Bridge the unified-orchestrator's summary into the legacy
    # $LINKEDIN_SUMMARY_FILE shape that the dashboard parser downstream still
    # expects. The orchestrator writes its posts-table summary internally
    # then deletes it; for the dashboard, surface the combined refresh count
    # via a fresh sidecar populated from the orchestrator's per-fire log.
    LAST_STATS_LOG=$(ls -t "$REPO_DIR/skill/logs/stats-linkedin-"*.log 2>/dev/null | head -1)
    if [ -n "$LAST_STATS_LOG" ] && [ -f "$LAST_STATS_LOG" ]; then
        REFRESHED_TOTAL=$(grep -oE 'total=[0-9]+' "$LAST_STATS_LOG" | tail -1 | sed 's/total=//' || echo 0)
        NOT_FOUND_TOTAL=$(grep -oE 'unmatched=[0-9]+' "$LAST_STATS_LOG" | tail -1 | sed 's/unmatched=//' || echo 0)
        printf '{"refreshed":%s,"removed":0,"unavailable":0,"not_found":%s}\n' \
            "${REFRESHED_TOTAL:-0}" "${NOT_FOUND_TOTAL:-0}" \
            > "$LINKEDIN_SUMMARY_FILE" 2>/dev/null || true
    fi

    if [ "$STEP4_EXIT" -eq 124 ]; then
        log "Step 4: TIMEOUT (30 min limit reached)"
    elif [ "$STEP4_EXIT" -ne 0 ]; then
        log "Step 4: FAILED (exit $STEP4_EXIT)"
    else
        log "Step 4: Done"
    fi
else
    log "Step 4: SKIPPED, no LinkedIn posts need stats update ($LINKEDIN_POSTS found)"
fi
else
    log "Step 4: SKIPPED (platform=$PLATFORM)"
fi

log "=== Stats Pipeline complete: $(date) ==="

# Log run to persistent monitor (matches audit.sh pattern so run_monitor.log
# covers every launchd job). SCRIPT_TAG uses underscores so the dashboard
# regex in bin/server.js (^stats_(\w+)$) classifies the row correctly.
RUN_ELAPSED=$(( $(date +%s) - RUN_START ))
STATS_FAILED=$(( (STEP1_EXIT != 0 ? 1 : 0) + (STEP2_EXIT != 0 ? 1 : 0) + (STEP3_EXIT != 0 ? 1 : 0) + (STEP4_EXIT != 0 ? 1 : 0) ))
SCRIPT_TAG="stats${PLATFORM:+_$PLATFORM}"

# Parse the per-run log to extract REAL counters for the dashboard. Before
# 2026-04-28 we logged `--posted "$ACTIVE"` (total active posts in the DB),
# which was meaningless and made every stats row read like "posted=18216".
# Now we extract the real per-run counters from the structured summary lines
# each step prints:
#
#   Step 1 (Reddit views leg):
#     Reddit Views: <N> had views, <M> DB posts updated, <U> unmatched
#   Step 2 (Reddit detail leg):
#     Reddit: <T> total, <S> skipped, <C> checked, <U> updated, <D> deleted, <R> removed, <E> errors [...]
#   Step 3 (Twitter):
#     Twitter: <T> total, <S> skipped, <C> checked, <U> updated, <D> deleted, <E> errors
#   Step 2 --moltbook-only:
#     Moltbook: <C> checked, <U> updated, <D> deleted, <E> errors
#   Step 4 (LinkedIn): no stdout summary; counters are read from the JSON
#     sidecar file written by scrape_linkedin_stats.py --summary.
#
# Missing platforms simply contribute 0 to each total. awk handles parsing
# robustly even when commas/brackets vary.
extract_field() {
    # Usage: extract_field <line> <field>
    # Pulls the integer that precedes <field> in a comma-separated counter
    # line such as "Reddit: 4346 total, 1696 skipped, ..."  Echoes 0 when the
    # field isn't present.
    #
    # Strips the leading "Platform:" prefix before splitting on commas so the
    # first segment ("Moltbook: 50 checked") doesn't break the leading-integer
    # match. Without this, fields living in the first comma-segment always
    # return 0 (the leading prefix is not numeric).
    local line="$1" field="$2"
    echo "$line" | awk -v f=" $field" '{
        sub(/^[A-Za-z][A-Za-z ]*:[[:space:]]*/, "", $0)
        n = split($0, parts, ",")
        for (i = 1; i <= n; i++) {
            if (index(parts[i], f) > 0) {
                # Strip leading whitespace, then the leading integer is the value.
                gsub(/^[[:space:]]+/, "", parts[i])
                if (match(parts[i], /^[0-9]+/)) {
                    print substr(parts[i], RSTART, RLENGTH)
                    exit
                }
            }
        }
        print 0
    }'
}

REDDIT_VIEWS_LINE=$(grep -E "^Reddit Views:" "$LOGFILE" 2>/dev/null | tail -1)
REDDIT_DETAIL_LINE=$(grep -E "^Reddit: [0-9]+ total" "$LOGFILE" 2>/dev/null | tail -1)
TWITTER_LINE=$(grep -E "^Twitter: [0-9]+ total" "$LOGFILE" 2>/dev/null | tail -1)
# Moltbook prints `Moltbook: N checked, N updated, N deleted, N errors` (no
# "total" prefix), so it gets its own grep. LinkedIn doesn't print a
# structured stdout line; its counters come from $LINKEDIN_SUMMARY_FILE.
MOLTBOOK_LINE=$(grep -E "^Moltbook: [0-9]+ checked" "$LOGFILE" 2>/dev/null | tail -1)

# Reddit views leg: "<M> DB posts updated" — only the "updated" leg matters here.
REDDIT_VIEWS_UPDATED=0
if [ -n "$REDDIT_VIEWS_LINE" ]; then
    REDDIT_VIEWS_UPDATED=$(echo "$REDDIT_VIEWS_LINE" | awk '{
        for (i = 1; i <= NF; i++) {
            if ($i == "DB" && $(i+1) == "posts" && $(i+2) == "updated,") {
                print $(i-1); exit
            }
        }
        print 0
    }')
fi

# 2026-05-18 relabel pass. stats.py's structured stdout lines now
# emit five cleanly-separated fields per platform: total / skipped /
# checked / changed / errors. Map them to the new dashboard pills:
#   REDDIT_SCANNED          -> 'scanned' pill (total considered)
#   REDDIT_SKIPPED          -> 'skipped' pill (stable + fresh combined)
#   REDDIT_CHECKED          -> 'checked' pill (rows actually hit the API)
#   REDDIT_CHANGED          -> 'changed' pill (metric-moved subset)
#   REDDIT_VIEWS_UPDATED    -> 'views' pill (Step 1 scrape leg, separate)
# `updated` is the legacy field name; if stats.py is mid-deploy and
# the new `changed` field is missing on the line, fall back to `updated`.
REDDIT_SCANNED=$(extract_field "$REDDIT_DETAIL_LINE" "total")
REDDIT_CHECKED=$(extract_field "$REDDIT_DETAIL_LINE" "checked")
REDDIT_CHANGED=$(extract_field "$REDDIT_DETAIL_LINE" "changed")
if [ "$REDDIT_CHANGED" = "0" ]; then
    # Back-compat: pre-relabel lines used `updated` for the same value.
    REDDIT_CHANGED=$(extract_field "$REDDIT_DETAIL_LINE" "updated")
fi
REDDIT_DETAIL_UPDATED="$REDDIT_CHANGED"  # legacy alias
REDDIT_DELETED=$(extract_field "$REDDIT_DETAIL_LINE" "deleted")
REDDIT_REMOVED_FIELD=$(extract_field "$REDDIT_DETAIL_LINE" "removed")
REDDIT_SKIPPED=$(extract_field "$REDDIT_DETAIL_LINE" "skipped")
REDDIT_ERRORS=$(extract_field "$REDDIT_DETAIL_LINE" "errors")

TWITTER_SCANNED=$(extract_field "$TWITTER_LINE" "total")
TWITTER_CHECKED=$(extract_field "$TWITTER_LINE" "checked")
TWITTER_CHANGED=$(extract_field "$TWITTER_LINE" "changed")
if [ "$TWITTER_CHANGED" = "0" ]; then
    TWITTER_CHANGED=$(extract_field "$TWITTER_LINE" "updated")
fi
TWITTER_UPDATED="$TWITTER_CHANGED"  # legacy alias
TWITTER_DELETED=$(extract_field "$TWITTER_LINE" "deleted")
TWITTER_SKIPPED=$(extract_field "$TWITTER_LINE" "skipped")
TWITTER_ERRORS=$(extract_field "$TWITTER_LINE" "errors")

MOLTBOOK_CHECKED=$(extract_field "$MOLTBOOK_LINE" "checked")
MOLTBOOK_UPDATED=$(extract_field "$MOLTBOOK_LINE" "updated")
MOLTBOOK_DELETED=$(extract_field "$MOLTBOOK_LINE" "deleted")
MOLTBOOK_ERRORS=$(extract_field "$MOLTBOOK_LINE" "errors")

# LinkedIn counters live in a JSON sidecar (no structured stdout line). The
# file is written by scrape_linkedin_stats.py --summary; absent or empty
# means the LinkedIn leg didn't run or wrote nothing, so all counters are 0.
LINKEDIN_REFRESHED=0
LINKEDIN_REMOVED=0
LINKEDIN_UNAVAILABLE=0
LINKEDIN_NOT_FOUND=0
if [ -s "$LINKEDIN_SUMMARY_FILE" ]; then
    LINKEDIN_REFRESHED=$(python3 -c "import json,sys; d=json.load(open('$LINKEDIN_SUMMARY_FILE')); print(int(d.get('refreshed', 0) or 0))" 2>/dev/null || echo 0)
    LINKEDIN_REMOVED=$(python3 -c "import json,sys; d=json.load(open('$LINKEDIN_SUMMARY_FILE')); print(int(d.get('removed', 0) or 0))" 2>/dev/null || echo 0)
    LINKEDIN_UNAVAILABLE=$(python3 -c "import json,sys; d=json.load(open('$LINKEDIN_SUMMARY_FILE')); print(int(d.get('unavailable', 0) or 0))" 2>/dev/null || echo 0)
    LINKEDIN_NOT_FOUND=$(python3 -c "import json,sys; d=json.load(open('$LINKEDIN_SUMMARY_FILE')); print(int(d.get('not_found', 0) or 0))" 2>/dev/null || echo 0)
fi

CHECKED=$(( REDDIT_CHECKED + TWITTER_CHECKED + MOLTBOOK_CHECKED + LINKEDIN_REFRESHED ))
# 2026-05-18 relabel: the legacy `UPDATED` summed Reddit's Step 1 view-scrape
# leg into the same pill as Step 2's "metric actually changed" leg, which
# silently inflated the number. Keep `UPDATED` wired for back-compat (the
# log line still emits `updated=N`), but it is now the same value as
# `CHANGED` so old dashboards behave sanely. New dashboards read the
# explicit `changed=` and `views_refreshed=` fields instead.
CHANGED=$(( REDDIT_CHANGED + TWITTER_CHANGED + MOLTBOOK_UPDATED + LINKEDIN_REFRESHED ))
VIEWS_REFRESHED=$REDDIT_VIEWS_UPDATED
UPDATED=$CHANGED
# `SCANNED` is the total rows the run considered, across all platforms.
# Moltbook has no skip class so its "scanned" == "checked"; LinkedIn ditto.
SCANNED=$(( REDDIT_SCANNED + TWITTER_SCANNED + MOLTBOOK_CHECKED + LINKEDIN_REFRESHED ))
REMOVED=$(( REDDIT_DELETED + REDDIT_REMOVED_FIELD + TWITTER_DELETED + MOLTBOOK_DELETED + LINKEDIN_REMOVED ))
SKIPPED_REAL=$(( REDDIT_SKIPPED + TWITTER_SKIPPED ))
UNAVAILABLE=$LINKEDIN_UNAVAILABLE
NOT_FOUND=$LINKEDIN_NOT_FOUND
# API errors are surfaced via a per-platform counter but are folded into the
# "failed" pill alongside step-exit counts. Stays bounded since API errors
# cap at a few hundred and step exits are 0-4.
FAILED_REAL=$(( STATS_FAILED + REDDIT_ERRORS + TWITTER_ERRORS + MOLTBOOK_ERRORS ))

# Pull the reply-refresh count for this platform out of the sidecar JSON.
# Defaults to 0 if the file is missing or the platform's pass didn't run.
REPLIES_REFRESHED=0
if [ -s "$REPLY_SUMMARY_FILE" ]; then
    KEY="${PLATFORM:-reddit}"  # all-platforms run reports reddit + twitter + github separately;
                                # without --platform we just total them.
    if [ -n "$PLATFORM" ]; then
        REPLIES_REFRESHED=$(python3 -c "import json,sys; d=json.load(open('$REPLY_SUMMARY_FILE')); print(d.get('$KEY', 0))" 2>/dev/null || echo 0)
    else
        REPLIES_REFRESHED=$(python3 -c "import json,sys; d=json.load(open('$REPLY_SUMMARY_FILE')); print(sum(d.values()))" 2>/dev/null || echo 0)
    fi
fi

_COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START" --scripts "stats-step4" 2>/dev/null || echo "0.0000")
python3 "$REPO_DIR/scripts/log_run.py" \
    --script "$SCRIPT_TAG" \
    --posted 0 \
    --skipped "$SKIPPED_REAL" \
    --failed "$FAILED_REAL" \
    --replies-refreshed "$REPLIES_REFRESHED" \
    --checked "$CHECKED" \
    --updated "$UPDATED" \
    --removed "$REMOVED" \
    --unavailable "$UNAVAILABLE" \
    --not-found "$NOT_FOUND" \
    --scanned "$SCANNED" \
    --changed "$CHANGED" \
    --views-refreshed "$VIEWS_REFRESHED" \
    --cost "$_COST" \
    --elapsed "$RUN_ELAPSED"

# Clean up old logs (keep last 7 days). Covers both new `stats-<platform>-*`
# and any legacy `stats-YYYY-*` filenames.
find "$LOG_DIR" -name "stats-*.log" -mtime +7 -delete 2>/dev/null || true
