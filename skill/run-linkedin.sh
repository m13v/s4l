#!/bin/bash
# Social Autoposter - LinkedIn posting (Phase A discover+score, Phase B post)
#
# Phase A (discovery + scoring, ~$10-15 target): pick a project, consult
#   top/dud query history, draft 8 dynamic search queries, browse the
#   LinkedIn SERPs, extract engagement metrics (reactions/comments/reposts/
#   age/author) for every visible candidate, score serp quality, write a
#   structured JSON envelope to a tmp file, STOP. Bash then pipes the
#   envelope into:
#     - log_linkedin_search_attempts.py (records every query, including
#       zero-result and low-quality, so duds get blocked next cycle)
#     - score_linkedin_candidates.py    (computes velocity + virality, upserts
#       into linkedin_candidates, dedupes against engaged URN history)
#   Bash then SELECTs the top pending candidate by velocity_score.
#
# Phase B (compose + post + verify + log, ~$10-15 target): given Phase A's
#   chosen candidate (already in linkedin_candidates), navigate straight to
#   the URL, defensively re-check engaged-ids, draft using the project's
#   voice block + engagement styles + top performers report, post via
#   the linkedin-harness MCP (bh_run), verify (DOM + screenshot), log via
#   log_post.py, mark the candidate row 'posted' (or 'skipped'), STOP.
#
# Differences vs the pre-2026-04-29 shape:
#   - Phase A extracts ENGAGEMENT (not just URN); we don't fly blind anymore
#   - Phase A logs every search query (positive, zero, low-quality SERP) so
#     the LLM learns which phrasings work and which to retire
#   - Phase B reads its candidate from linkedin_candidates (DB-backed),
#     not from a file, so the same candidate isn't picked twice across runs

set -euo pipefail

# ===== Whole-run singleton guard (2026-05-30) =====
# launchd (com.m13v.social-linkedin) fires this script every 900s (15 min),
# but a full Phase A + Phase B run takes 20+ min. Without a run-level mutex,
# two fires overlap and BOTH drive the single linkedin-harness Chrome
# (port 9556) at once: one run searches SERPs (Phase A) while the other
# posts a comment (Phase B), yanking the same window back and forth. That is
# the "two LinkedIns running in parallel" symptom (proven via the browser
# activity log on 2026-05-30: pids 35789 Phase A + 59215 Phase B alive
# together, both on 9556). The per-phase locks do NOT prevent this because
# they release between phases. This guard makes the ENTIRE run a singleton:
# if a prior run-linkedin.sh is still alive, this fire exits immediately.
S4L_LI_RUN_LOCK="/tmp/s4l-run-linkedin.lock"
if mkdir "$S4L_LI_RUN_LOCK" 2>/dev/null; then
  echo $$ > "$S4L_LI_RUN_LOCK/pid"
else
  _li_holder="$(cat "$S4L_LI_RUN_LOCK/pid" 2>/dev/null || echo "")"
  if [ -n "$_li_holder" ] && kill -0 "$_li_holder" 2>/dev/null; then
    echo "[run-linkedin] singleton guard: prior full run (pid $_li_holder) still alive; exiting this fire to avoid two drivers on the 9556 Chrome"
    exit 0
  fi
  # holder is dead -> stale lock; reclaim it
  echo "[run-linkedin] singleton guard: reclaiming stale run lock (dead pid ${_li_holder:-unknown})"
  rm -rf "$S4L_LI_RUN_LOCK"
  mkdir "$S4L_LI_RUN_LOCK" && echo $$ > "$S4L_LI_RUN_LOCK/pid"
fi

# Transport backend selector (2026-05-28). Two interchangeable paths for the
# only two browser touchpoints (Phase A SERP search, Phase B comment-post):
#   browser (DEFAULT, ACTIVE) — headed-Chrome path via the linkedin-harness
#                       MCP (bh_run). This is what every real run uses.
#   unipile (DISABLED / OFF)  — UniPile REST API via scripts/linkedin_unipile.py.
#                       *** DO NOT ASSUME THIS PATH IS RUNNING. ***
#                       The UniPile-hosted LinkedIn session is dead (it logs
#                       itself out and returns 503 no_client_session), which
#                       silently zeroed out every discovery cycle. It is now
#                       gated OFF behind the default flip below and is only
#                       reachable by an explicit LINKEDIN_BACKEND=unipile
#                       override (which will still 503 until someone manually
#                       reconnects the UniPile account). All the unipile-branch
#                       code below (the `if [ "$LINKEDIN_BACKEND" = "unipile" ]`
#                       blocks, linkedin_unipile.py calls) is DORMANT, kept only
#                       so the path can be revived later. Seeing it in the file
#                       does NOT mean it is in use.
# Everything ELSE (project pick, query drafting, SERP-quality rating, dedup,
# velocity/virality scoring, voice composition, URL wrapping, log_post.py
# logging, candidate marking) is byte-for-byte identical across both paths.
# Override per-run (revives the dormant, currently-broken path):
#   LINKEDIN_BACKEND=unipile ~/social-autoposter/skill/run-linkedin.sh
LINKEDIN_BACKEND="${LINKEDIN_BACKEND:-browser}"

# LinkedIn killswitch (2026-05-27): refuse to run if a prior fire detected
# session compromise (http_999, authwall, throttle, li_at cleared).
# State: ~/.claude/social-autoposter/linkedin.killswitch
# Clear: python3 ~/social-autoposter/scripts/linkedin_killswitch.py clear
# Only gates the browser backend — UniPile has no headed session to compromise.
if [ "$LINKEDIN_BACKEND" = "browser" ] && [ -f "$HOME/.claude/social-autoposter/linkedin.killswitch" ]; then
    echo "[$(date +%H:%M:%S)] LINKEDIN_KILLSWITCH active. Aborting LinkedIn pipeline."
    echo "  Re-auth LinkedIn in harness Chrome, then: python3 ~/social-autoposter/scripts/linkedin_killswitch.py clear"
    exit 0
fi

[ -f "$HOME/social-autoposter/.env" ] && source "$HOME/social-autoposter/.env"

REPO_DIR="$HOME/social-autoposter"
SKILL_FILE="$REPO_DIR/SKILL.md"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run-linkedin-$(date +%Y-%m-%d_%H%M%S).log"
RUN_START_EPOCH=$(date +%s)
BATCH_ID="li-$(date +%Y%m%d_%H%M%S)-$$"
# Export as SA_CYCLE_ID so log_claude_session.py stamps cycle_id on every
# claude_sessions row spawned by this cycle. Enables per-cycle cost queries
# via get_run_cost.py --cycle-id. 2026-05-10 cycle_id rollout.
export SA_CYCLE_ID="$BATCH_ID"

echo "=== LinkedIn Post Run: $(date) (batch=$BATCH_ID) ===" | tee "$LOG_FILE"

# 2026-05-01: lock policy was changed from "hold for the entire run" to
# "hold only while a Claude phase is actively driving the browser". The old
# policy meant a single 25-45min cycle held linkedin-browser exclusively for
# its full duration, which (a) starved peer pipelines (dm-replies-linkedin,
# audit-linkedin, link-edit-linkedin) of any browser window and (b) defeated
# the launchd 15-min cadence: every fire of this job had to wait for the
# prior fire's full pipeline to finish. The browser is only actually used
# inside the two run_claude.sh invocations (Phase A discovery, Phase B
# post). All the work between them (envelope validate, DB ingest, candidate
# pick, project config, top performers, styles, etc.) is pure DB/CPU and
# does not need the lock. So we acquire just before each Claude phase and
# release immediately after.
source "$REPO_DIR/skill/lock.sh"
# Browser backend bootstrap (linkedin-harness). Sets MCP_CONFIG_FILE,
# BROWSER_INSTRUCTIONS, exports LINKEDIN_CDP_URL (so discover_linkedin_candidates.py
# CDP-attaches to the harness Chrome on 9556), and provides
# ensure_linkedin_browser_for_backend. Only the LINKEDIN_BACKEND=browser path
# uses these; the unipile (default) path has no browser. Migrated off the
# deprecated linkedin-agent MCP on 2026-05-29 (mirrors the Twitter migration).
source "$REPO_DIR/skill/lib/linkedin-backend.sh"

# Idempotent run_monitor.log emitter wired into a chained EXIT/INT/TERM/HUP
# trap. Without this, SIGTERM landing between Phase B post (where Claude has
# already submitted the comment via the LinkedIn API and the row is in the
# `posts` table) and the inline summary write at the bottom of the script
# silently drops the run from run_monitor.log. Mirrors the same fix shipped
# to run-reddit-search.sh and run-twitter-cycle.sh.
#
# Reads counters from globals the cycle accumulates (RUN_START_EPOCH,
# PB_RC, LOG_FILE) and re-derives POSTED/SKIPPED/FAILED the same way the
# inline block does. All shell-outs are wrapped in `timeout 10` so a Postgres
# hang during shutdown can't wedge the trap.
#
# Early-exit failure paths (Phase A no-candidates, etc.) write their own
# tailored log_run.py line and then set _SA_RUN_SUMMARY_EMITTED=1 to
# short-circuit this function — the trap fires, no-ops, and the dedicated
# error reason stays.
_SA_RUN_SUMMARY_EMITTED=0
_sa_emit_run_summary_oneshot() {
    [ "${_SA_RUN_SUMMARY_EMITTED:-0}" = "1" ] && return 0
    _SA_RUN_SUMMARY_EMITTED=1

    local elapsed window_sec posted skipped failed cost
    elapsed=$(( $(date +%s) - ${RUN_START_EPOCH:-$(date +%s)} ))
    window_sec=$(( elapsed + 60 ))
    posted=0
    posted=$(WINDOW_SEC="$window_sec" timeout 15 python3 - <<'PY' 2>/dev/null || true
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)) if "__file__" in dir() else os.getcwd(), "scripts"))
sys.path.insert(0, os.path.expanduser("~/social-autoposter/scripts"))
try:
    from http_api import api_get
    win = int(os.environ.get("WINDOW_SEC") or "0")
    resp = api_get("/api/v1/posts/count",
                   {"platform": "linkedin", "within_seconds": win})
    print(int((resp.get("data") or {}).get("count") or 0))
except Exception:
    print(0)
PY
)
    [ -z "$posted" ] && posted=0
    skipped=0
    if [ "$posted" = "0" ] && [ -n "${LOG_FILE:-}" ] && [ -f "${LOG_FILE:-}" ] \
        && grep -qE "PHASE_B_SKIP_POST_UNAVAILABLE|## Already engaged|## Comment soft-blocked" "$LOG_FILE" 2>/dev/null; then
        skipped=1
    fi
    failed=0
    if [ "${PB_RC:-1}" -ne 0 ] && [ "$posted" = "0" ] && [ "$skipped" = "0" ]; then
        failed=1
    fi
    cost=$(timeout 10 python3 "$REPO_DIR/scripts/get_run_cost.py" \
                --since "${RUN_START_EPOCH:-0}" \
                --scripts "run-linkedin-phaseA" "run-linkedin-phaseB" \
                2>/dev/null || echo "0.0000")
    # Surface Anthropic-side cause (stream_idle_timeout, monthly_limit,
    # api_overloaded, context_overflow, credit_balance) when failed>0 so
    # the dashboard pill carries the actual error class instead of just
    # showing a silent failed=1 row. Uses ${var:+...} conditional expansion
    # rather than an empty bash array to avoid the `set -u` empty-array
    # pitfall documented in CLAUDE.md (bash 3.2 trips on `"${empty[@]}"`).
    local lk_reason=""
    if [ "$failed" -gt 0 ] && [ -n "${LOG_FILE:-}" ] && [ -f "${LOG_FILE:-}" ]; then
        lk_reason=$(python3 "$REPO_DIR/scripts/classify_run_error.py" "$LOG_FILE" 2>/dev/null)
    fi
    python3 "$REPO_DIR/scripts/log_run.py" --script post_linkedin \
        --posted "$posted" --skipped "$skipped" --failed "$failed" \
        --cost "$cost" --elapsed "$elapsed" \
        ${lk_reason:+--failure-reasons "${lk_reason}:1"} 2>/dev/null || true
}

# Trap chain: lock.sh has already installed _sa_release_locks on
# EXIT INT TERM HUP. Replace with a chained handler so summary fires first,
# then locks release. _sa_release_locks remains in scope after sourcing.
trap '_sa_emit_run_summary_oneshot; _sa_release_locks; rm -rf "$S4L_LI_RUN_LOCK" 2>/dev/null || true' EXIT INT TERM HUP

# ===== Phase A: discovery + scoring =====
python3 "$REPO_DIR/scripts/linkedin_search_topic_schema.py" 2>>"$LOG_FILE" || true

PROJECT_DIST=$(python3 "$REPO_DIR/scripts/pick_project.py" --platform linkedin --distribution 2>/dev/null || echo "(distribution unavailable)")

# Mirror Twitter's ownership boundary: Python picks exactly one project and
# one project_search_topics row before Claude drafts literal LinkedIn queries.
set +e
PROJECT_PICK_JSON=$(REPO_DIR="$REPO_DIR" python3 - <<'PY' 2>>"$LOG_FILE"
import json
import os
import sys

repo = os.environ["REPO_DIR"]
sys.path.insert(0, os.path.join(repo, "scripts"))

from pick_project import load_config, pick_project
from pick_search_topic import pick_topic_for_project

project = pick_project(load_config(), platform="linkedin")
if not project:
    raise SystemExit("no LinkedIn-eligible project with active search_topics")

name = project.get("name") or ""
assignment = pick_topic_for_project(name, platform="linkedin")
topic = (assignment.get("search_topic") or "").strip()
if not topic:
    raise SystemExit(f"no search_topic picked for project={name!r}")

out = {
    "name": name,
    "description": project.get("description", ""),
    "qualification": project.get("qualification", ""),
    "search_topic": topic,
    "picked_weight_pct": assignment.get("picked_weight_pct"),
    "topic_assignment": assignment,
    "reference_topics": assignment.get("reference_topics") or [],
}
print(json.dumps(out, indent=2))
PY
)
PICK_RC=$?
set -e

if [ "$PICK_RC" -ne 0 ] || [ -z "${PROJECT_PICK_JSON:-}" ]; then
  echo "Phase A: project/search_topic picker failed. Skipping LinkedIn run." | tee -a "$LOG_FILE"
  ELAPSED=$(( $(date +%s) - RUN_START_EPOCH ))
  _COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START_EPOCH" --scripts "run-linkedin-phaseA" "run-linkedin-phaseB" 2>/dev/null || echo "0.0000")
  python3 "$REPO_DIR/scripts/log_run.py" --script post_linkedin --posted 0 --skipped 0 --failed 1 --cost "$_COST" --elapsed "$ELAPSED" || true
  _SA_RUN_SUMMARY_EMITTED=1
  echo "=== Run complete: $(date) ===" | tee -a "$LOG_FILE"
  exit 0
fi

LI_PROJECT_NAME=$(printf '%s' "$PROJECT_PICK_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('name',''))")
LI_SEARCH_TOPIC=$(printf '%s' "$PROJECT_PICK_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('search_topic',''))")

if [ -z "$LI_PROJECT_NAME" ] || [ -z "$LI_SEARCH_TOPIC" ]; then
  echo "Phase A: project/search_topic picker returned an incomplete assignment. Skipping LinkedIn run." | tee -a "$LOG_FILE"
  ELAPSED=$(( $(date +%s) - RUN_START_EPOCH ))
  _COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START_EPOCH" --scripts "run-linkedin-phaseA" "run-linkedin-phaseB" 2>/dev/null || echo "0.0000")
  python3 "$REPO_DIR/scripts/log_run.py" --script post_linkedin --posted 0 --skipped 0 --failed 1 --cost "$_COST" --elapsed "$ELAPSED" || true
  _SA_RUN_SUMMARY_EMITTED=1
  echo "=== Run complete: $(date) ===" | tee -a "$LOG_FILE"
  exit 0
fi

echo "Phase A: picked project=$LI_PROJECT_NAME search_topic='$LI_SEARCH_TOPIC'" | tee -a "$LOG_FILE"

# Top-performing historical queries for this exact project/topic
# (positive signal, last 30d).
TOP_QUERIES=$(python3 "$REPO_DIR/scripts/top_linkedin_queries.py" --project "$LI_PROJECT_NAME" --search-topic "$LI_SEARCH_TOPIC" --limit 15 --window-days 30 2>/dev/null || echo "[]")

# Dud queries to AVOID redrafting for this exact project/topic
# (zero-result OR low-SERP, last 7d).
DUD_QUERIES=$(python3 "$REPO_DIR/scripts/top_dud_linkedin_queries.py" --project "$LI_PROJECT_NAME" --search-topic "$LI_SEARCH_TOPIC" --limit 30 --window-days 7 2>/dev/null || echo "[]")

# BSD mktemp on macOS only substitutes XXXXXX at the end of the template.
PHASE_A_OUT=$(mktemp /tmp/sa-run-linkedin-phaseA-XXXXXX)
PHASE_A_PROMPT=$(mktemp /tmp/sa-run-linkedin-phaseA-prompt-XXXXXX)

# --- DORMANT unipile branch: OFF by default (see header). Reached ONLY with an
# --- explicit LINKEDIN_BACKEND=unipile override, which still 503s until the
# --- UniPile account is manually reconnected. Presence here != in use.
if [ "$LINKEDIN_BACKEND" = "unipile" ]; then
# ----- Phase A prompt: UniPile REST backend (no browser) -----
cat > "$PHASE_A_PROMPT" <<PROMPT_EOF
You are the Social Autoposter LinkedIn discovery + scoring scout (Phase A),
running on the UniPile REST backend (no browser, no headed Chrome).

Your job: use the pre-selected project and assigned search_topic, draft 8
DYNAMIC LinkedIn search queries from that one topic, run each through the
UniPile search CLI, extract engagement metrics, rate SERP quality, pick the
single best candidate, write a structured JSON envelope to $PHASE_A_OUT, and
STOP. Do NOT draft a comment. Do NOT post anything. Phase B handles drafting
+ posting using whatever you write to the candidates list.

## Pre-selected project and assigned topic
$PROJECT_PICK_JSON

Assigned project: $LI_PROJECT_NAME
Assigned search_topic: $LI_SEARCH_TOPIC

## Today's distribution (context only; the project is already picked)
$PROJECT_DIST

## Top-performing historical queries for this project/topic
STYLE inspiration only - do NOT reuse them verbatim. LinkedIn SERPs shift
daily, so reusing exact phrasing is wasteful. Mine them for the angle/keyword
combo that worked, then craft something new.
$TOP_QUERIES

## DUD queries to AVOID for this project/topic
Do NOT redraft any of these phrasings. They have been flat or audience-wrong
recently. 'zero_results' means LinkedIn rejected the keywords;
'low_serp_quality' means results came back but were influencer slop /
off-target audience.
$DUD_QUERIES

## Workflow

1. Use ONLY this assigned project and search_topic. Do NOT pick another
   project, do NOT switch topics, and do NOT iterate through the project list.

2. Draft 8 search queries for the assigned topic. Each query should:
   - Be 2-4 words (LinkedIn search hates long phrases)
   - Target practitioners, not influencers (no "expert tips", "thought
     leadership", or buzzwordy phrasing)
   - Be FRESH - different from the dud list, different angle from the
     top-performers list (steal the recipe, change the dish)
   - Map directly to the assigned search_topic
   - Cover DIFFERENT facets / pains / personas of the ICP - not 4 reskins
     of the same query. Wider net = higher chance of one ICP-fit hit.

   Run exactly 8 queries this run. More surface area beats narrow targeting:
   most queries return slop, so the 2-3 that survive should reach you with
   real candidates.

3. For EACH query, shell out via the Bash tool (ONE line, no browser):

       python3 $REPO_DIR/scripts/linkedin_unipile.py search --keywords "<query>" --date-posted past_week --sort-by date --with-followers --pipeline --limit 8

   This calls the UniPile REST API (a server-hosted LinkedIn session on the
   same account; there is NO local browser to prime or navigate) and prints a
   JSON envelope to stdout:

       {
         "ok": true,
         "query": "<query>",
         "result_count": N,
         "cursor": "...|null",
         "results": [
           {
             "post_url":           "https://www.linkedin.com/feed/update/urn:li:<ns>:<num>/",
             "activity_id":        "<num>",
             "all_urns":           ["<num>"],
             "social_id":          "urn:li:<ns>:<num>",
             "author_name":        "...",
             "author_headline":    "...|null",
             "author_profile_url": "https://www.linkedin.com/in/<slug>/|null",
             "author_followers":   <int|null>,
             "post_text":          "...",
             "age_hours":          <float|null>,
             "reactions":          <int>,
             "comments":           <int>,
             "reposts":            <int>,
             "is_repost":          <bool>
           }, ...
         ]
       }

   UniPile returns the post URN directly in social_id / post_url /
   activity_id, with the CORRECT namespace (activity / share / ugcPost). There
   is NO click-to-resolve step and NO URN-namespace guessing — copy these
   fields through verbatim.

   Failure handling: if a query prints "ok": false, or an object with an
   "error" / error "response" field (HTTP 401 / 429 / 5xx), treat it like a
   zero-result query — record it in queries_used with candidates_found=0 and
   serp_quality_score=null, then continue to the next query. If the VERY FIRST
   query returns an auth error (HTTP 401 missing_credentials), the UniPile
   session is dead: write the envelope with whatever queries_used you have and
   candidates: [], then STOP.

   3a. RATE THE SERP QUALITY 0-10 for THIS query, based on:
       - Practitioner ratio: judge from author_headline AND author_followers
         (low-follower / hands-on builders > influencer-tier accounts).
       - Topic fit: do the post_text excerpts actually match the project domain?
       - Freshness: median age_hours of results (lower = better).
       - 0-3 = useless slop, 4-5 = mixed, 6-8 = mostly relevant, 9-10 = goldmine.

   3b. SKIP candidates authored by Matthew Diakonov / linkedin.com/in/m13v/.

   3c. Dedup against engaged history. Gather the activity_id of every
       candidate across all queries into one comma-separated list, then run
       ONCE via Bash:
         python3 $REPO_DIR/scripts/linkedin_url.py --check-engaged-ids 'id1,id2,id3'
       Exit code 0 means at least one is already engaged; use the script's
       output to drop any candidate whose activity_id is already engaged.

4. PICK THE SINGLE BEST CANDIDATE across all queries.
   - The UniPile results are NOT pre-scored. Weigh engagement
     (reactions + 2*comments + 3*reposts) against age_hours yourself: a post
     with 40 reactions in 3 hours beats 60 reactions in 5 days. Favor recent
     posts with real, non-trivial engagement.

   - LEAN TOWARD POSTING. The bar is: "would commenting here be embarrassing
     or off-message for the project?" NOT "is this a perfect ICP fit?"
     A mediocre but on-topic comment costs around twenty cents; a missed real
     fit costs the entire cycle (roughly fifteen dollars). Favor the post.

   - HARD-REJECT (these are the only auto-disqualifiers):
       1. Direct competitor: the author or their company sells a product that
          competes with the project. Name the competing product in your
          rationale. Vague competitor vibes are NOT enough.
       2. Recruiter / job-ad post: body is "we're hiring", "open role", a job
          description, or a careers-page link.
       3. Off-topic content: politics, personal milestones, unrelated
          industry, news commentary not tied to the project's domain.
       4. Author is m13v / Matthew Diakonov. (Already filtered earlier.)

   - SOFT SIGNALS (do NOT auto-reject on these alone):
       * Author on a brand/company page (author_profile_url null but
         author_name present): engageable IF the post topic is on-message.
       * Adjacent persona / not the perfect ICP buyer: fine if the topic
         resonates with the project's wedge.
       * Lower follower count / "no-name" author: irrelevant to whether we
         should comment; practitioners with smaller audiences are often
         higher-quality targets than influencers.
       * Some buzzwords / hype framing: tolerable if the underlying post-topic
         is a real practitioner pain.

   - NAME THE VERDICT EXPLICITLY in your rationale: which hard-reject category
     fired (1/2/3/4), or "soft fit, posting." Do not write "ICP mismatch"
     without naming which category.

   - One winner. Not a ranked list. Not a top-3.

5. Write the envelope to $PHASE_A_OUT with the winner (and ONLY the winner —
   discard runners-up, they are noise that will not be reused) and STOP:

\`\`\`bash
cat > $PHASE_A_OUT <<JSON_EOF
{
  "project": "$LI_PROJECT_NAME",
  "search_topic": "$LI_SEARCH_TOPIC",
  "language": "en",
  "queries_used": [
    {"query": "ai agents production",   "search_topic": "$LI_SEARCH_TOPIC", "candidates_found": 4, "serp_quality_score": 7.5, "dropped_below_floor": 0},
    {"query": "macos automation tools", "search_topic": "$LI_SEARCH_TOPIC", "candidates_found": 0, "serp_quality_score": null, "dropped_below_floor": 0},
    {"query": "claude code workflow",   "search_topic": "$LI_SEARCH_TOPIC", "candidates_found": 6, "serp_quality_score": 5.0, "dropped_below_floor": 0}
  ],
  "candidates": [
    {
      "post_url": "https://www.linkedin.com/feed/update/urn:li:activity:NUMERIC/",
      "activity_id": "NUMERIC",
      "all_urns": ["NUMERIC"],
      "author_name": "First Last",
      "author_headline": "Headline | role | company (may be null)",
      "author_profile_url": "https://www.linkedin.com/in/SLUG/",
      "author_followers": 2124,
      "post_text": "post body, no newlines, no double quotes, no backticks",
      "age_hours": 6.5,
      "reactions": 42,
      "comments": 7,
      "reposts": 3,
      "search_topic": "$LI_SEARCH_TOPIC",
      "search_query": "ai agents production",
      "language": "en",
      "serp_quality_score": 7.5
    }
  ]
}
JSON_EOF
\`\`\`

   - queries_used MUST contain ONE row per query you ran (including
     zero-result ones — that is the whole point of the dud-learning).
   - project MUST equal "$LI_PROJECT_NAME" and every search_topic MUST equal
     "$LI_SEARCH_TOPIC". The search_query is the literal phrase you ran.
   - candidates_found is the count of usable candidates that query surfaced
     (after dropping self-authored / already-engaged). dropped_below_floor
     is always 0: neither path applies a virality floor (Twitter model).
   - candidates contains AT MOST one row (the winner from step 4). It can be
     empty if step 4 found nothing engageable. bash skips Phase B cleanly
     when empty.
   - The winner row MUST copy post_url, activity_id, and author_followers
     VERBATIM from the chosen search result. Do NOT rebuild or rewrite the URN
     namespace — UniPile already returned the correct one. Do NOT null out
     author_followers; it is a real number on this path and the scorer uses it.
   - candidates must NOT include posts you already engaged on or self-authored.
   - author_headline is optional on output; pass through whatever the search
     returned (may be null).
   - post_text must be safe to embed in a bash double-quoted string. Strip
     backticks, double quotes, and newlines before writing. Truncate to ~500
     chars before writing into the envelope.

Then say '## Phase A: envelope written' and STOP.

CRITICAL: Use ONLY the Bash tool plus the linkedin_unipile.py / linkedin_url.py
scripts. There is NO browser in this path — NEVER attempt any browser MCP
tools (none are loaded) and never try to navigate a webpage.
CRITICAL: Run exactly 8 search queries this run. Not 2, not 4, not 6. Eight.
CRITICAL: NEVER use em dashes anywhere.
PROMPT_EOF
else
# ----- Phase A prompt: headed-Chrome browser backend (linkedin-harness) -----
cat > "$PHASE_A_PROMPT" <<PROMPT_EOF
You are the Social Autoposter LinkedIn discovery + scoring scout (Phase A).

$BROWSER_INSTRUCTIONS

Your job: use the pre-selected project and assigned search_topic, draft 8
DYNAMIC LinkedIn search queries from that one topic, browse each query's
LinkedIn SERP, extract engagement metrics for every visible candidate post,
write a structured JSON envelope to $PHASE_A_OUT, and STOP. Do NOT draft a
comment. Do NOT post anything. Phase B handles drafting + posting using
whatever you write to the candidates list.

## Pre-selected project and assigned topic
$PROJECT_PICK_JSON

Assigned project: $LI_PROJECT_NAME
Assigned search_topic: $LI_SEARCH_TOPIC

## Today's distribution (context only; the project is already picked)
$PROJECT_DIST

## Top-performing historical queries for this project/topic
These are STYLE inspiration only - do NOT reuse them verbatim. LinkedIn
SERPs shift daily, so reusing the exact same phrasing is wasteful. Mine
them for the angle/keyword combo that worked, then craft something new.
$TOP_QUERIES

## DUD queries to AVOID for this project/topic
Do NOT redraft any of these phrasings. They have been flat or
audience-wrong recently. Note the 'reason' field - 'zero_results' means
LinkedIn rejected the keywords; 'low_serp_quality' means results came
back but were influencer slop / off-target audience.
$DUD_QUERIES

## Workflow

1. Use ONLY this assigned project and search_topic. Do NOT pick another
   project, do NOT switch topics, and do NOT iterate through the project list.

2. Draft 8 search queries for the assigned topic. Each query should:
   - Be 2-4 words (LinkedIn search hates long phrases)
   - Target practitioners, not influencers (no "expert tips", "thought
     leadership", or buzzwordy phrasing)
   - Be FRESH - different from the dud list, different angle from the
     top-performers list (steal the recipe, change the dish)
   - Map directly to the assigned search_topic
   - Cover DIFFERENT facets / pains / personas of the ICP - not 4 reskins
     of the same query. Wider net = higher chance of one ICP-fit hit.

   Run 8 queries this run. More surface area beats narrow targeting:
   most queries will return slop and get retired into the dud list, so the
   2-3 that survive should reach the LLM with real candidates. The
   LinkedIn rate budget (40/24h, 150/30d) accommodates this fine; rate
   caps are not the bottleneck, candidate quality is.

3. PRIME the harness browser ONCE before the per-query loop. This confirms
   the harness Chrome is up and the session is alive before the discover
   script CDP-attaches to it.
   3pre. Navigate (per the BROWSER BACKEND block) to https://www.linkedin.com/
         (one navigation), then take a screenshot and Read it.
   3pre-check. If the resulting URL contains /uas/login or /checkpoint/, or the
         screenshot shows a login / captcha / verify-you-are-human page, the
         persistent session is dead. Print SESSION_INVALID, write an empty
         envelope (no queries_used, no candidates) and STOP. The user must
         re-auth the harness LinkedIn Chrome interactively before the next run.

4. For EACH query, shell out via the Bash tool:

       SOCIAL_AUTOPOSTER_LINKEDIN_SEARCH=1 $LINKEDIN_DISCOVER_PYTHON \\
         $REPO_DIR/scripts/discover_linkedin_candidates.py content "<query>"

   The script CDP-attaches to the SAME harness Chrome (LINKEDIN_CDP_URL is
   already exported to the harness port; same cookies/session/fingerprint, no
   second browser), navigates the SERP, extracts every visible card, and prints
   a JSON envelope to stdout. Do NOT drive the browser yourself for discovery —
   the script handles navigation and extraction.

   Result shape on success:

       {
         "ok": true,
         "url": "https://www.linkedin.com/search/results/content/?keywords=...",
         "vertical": "content",
         "query": "<query>",
         "result_count": N,
         "dropped_below_virality_floor": 0,
         "virality_floor": null,
         "results": [   // SORTED by velocity_score DESC, top of list = highest score
           {
             "post_url":           "...|null",
             "activity_id":        "...|null",
             "all_urns":           [],
             "author_name":        "...",
             "author_headline":    "...|null",
             "author_profile_url": "...",
             "author_followers":   null,
             "post_text":          "...",
             "age_hours":          <float>,
             "age_text":           "5m",
             "reactions":          <int>,
             "comments":           <int>,
             "reposts":            <int>
           }, ...
         ],
         "rate_budget": {
           "daily_used":   N, "daily_cap":   40,
           "monthly_used": N, "monthly_cap": 150
         }
       }

   result_count is ALL cards the SERP returned (Twitter model: no virality
   floor, nothing is dropped on score). The cards are scored and sorted by
   velocity_score DESC so the strongest engagement signal sits at the top,
   but weak cards stay in the list as fallback. dropped_below_virality_floor
   is always 0 now; copy it into queries_used as dropped_below_floor=0. The
   dashboard reads raw SERP volume straight off candidates_found, so a query
   that returns 0 cards still reads as "SERP returned nothing".

   New SDUI caveat: post_url and activity_id are null for posts that don't
   embed a quoted/reposted share. That's expected — KEEP these in your
   working set, judge them on author/headline/post_text/age/engagement,
   and let step 5 below resolve the URN by clicking into the chosen winner.

   Failure handling (the JSON's "error" field):
     - "rate_limited"      → sleep retry_after_seconds, retry once. If still
                              rate-limited after retry, skip this query and
                              continue to the next.
     - "serp_redirected"   → log this query in queries_used with
                              candidates_found=0, serp_quality_score=0;
                              skip and move to next query.
     - "session_invalid"   → write empty envelope and STOP. Phase B will skip.
     - "mcp_not_running"   → same as session_invalid.
     - "navigation_failed" → skip this query, continue.
     - "db_unavailable"    → script already fails closed; treat like
                              "rate_limited" with no retry budget visible.
   On any non-ok, still append to queries_used so the run is auditable.

   4a. RATE THE SERP QUALITY 0-10 for THIS query, based on:
       - Practitioner ratio: judge from author_headline and post_text
         (low-follower / hands-on builders > influencer-tier accounts).
         author_followers is null on the new SDUI layout, so headline tone
         is your primary signal.
       - Topic fit: do the post excerpts actually match the project's domain?
       - Freshness: median age_hours of results (lower = better)
       - 0-3 = useless slop, 4-5 = mixed, 6-8 = mostly relevant, 9-10 = goldmine
       Write the score into the queries_used record (see envelope below).

   4b. SKIP candidates authored by Matthew Diakonov / linkedin.com/in/m13v/.

   4c. SKIP candidates that already have a known URN AND are already
       engaged. Run:
         python3 $REPO_DIR/scripts/linkedin_url.py --check-engaged-ids 'comma,sep,urns'
       For each candidate that HAS a non-null activity_id (the embedded-
       quoted-share case), check its all_urns set; if ANY URN already
       engaged, drop the candidate. Candidates with activity_id == null
       skip this check (their URN isn't known yet) — step 5 will resolve
       the URN before the engaged-id check runs again at Phase B.

5. PICK THE SINGLE BEST CANDIDATE across all queries.
   - Within each query's "results" array, candidates are PRE-SORTED by
     velocity_score DESCENDING (top of list = strongest engagement signal).
     Default to candidates near the top — the score already encodes
     reactions/comments/reposts/age, so the top of each list is a real
     prior. Walking past the top-3 of any query should require a clear
     ICP-fit reason. Do not skip a #1 just because #4 looks "interesting".

   - LEAN TOWARD POSTING. The bar is: "would commenting here be embarrassing
     or off-message for the project?" NOT "is this a perfect ICP fit?"
     A mediocre but on-topic comment costs around twenty cents; a missed
     real fit costs the entire cycle (roughly fifteen dollars). The cost is
     asymmetric, so favor the post.

   - HARD-REJECT (these are the only auto-disqualifiers):
       1. Direct competitor: the author or their company sells a product
          that competes with the project. Name the competing product in
          your rationale ("logistify.ai builds the same RPA-replacement
          agent Mediar does"). Vague competitor vibes are NOT enough.
       2. Recruiter / job-ad post: post body is "we're hiring", "open
          role", a job description, or a careers-page link. Engaging
          drops us into a recruiting funnel, off-message.
       3. Off-topic content: politics, personal milestones (weddings,
          baby announcements), unrelated industry, news commentary not
          tied to the project's domain.
       4. Author is m13v / Matthew Diakonov. (Already filtered earlier.)

   - SOFT SIGNALS (do NOT auto-reject on these alone):
       * Author is on a brand/company page (author_profile_url null but
         author_name present): engageable IF the post topic is on-message
         for the project. Brand-page comments still get seen.
       * Adjacent persona / not the perfect ICP buyer: a freelance dev
         posting about ops automation is adjacent to Mediar's enterprise-
         ops ICP, not on it. Adjacent is fine if the topic resonates with
         the project's wedge — adjacent personas often spread the message
         to actual buyers.
       * Lower follower count / "no-name" author: irrelevant to whether
         we should comment. Practitioners with smaller audiences are
         often higher-quality engagement targets than influencers.
       * Some buzzwords / hype framing: tolerable if the underlying
         post-topic is a real practitioner pain.

   - NAME THE VERDICT EXPLICITLY in your rationale: which hard-reject
     category fired (1/2/3/4), or "soft fit, posting." Do not write
     "ICP mismatch" without naming which category.

   - One winner. Not a ranked list. Not a top-3.
   - If the winner already has a non-null activity_id (rare: only the
     embedded-share case), skip step 5a/5b/5c — go straight to step 6.

   5a. The winner's SERP card has a clickable timestamp / "Feed post"
       title link that opens the canonical post detail. Click it ONCE
       (per the BROWSER BACKEND block: locate the matching card via
       getBoundingClientRect, then click_at_xy on its timestamp/title link).
       (Use the post_text first ~60 chars to disambiguate which card
       on the SERP is the winner.) Click on exactly one card per run.

   5b. After the navigation settles, read the resulting page URL via
       the BROWSER BACKEND block's run-code equivalent (bh_run js("""return location.href""")).
       Match /urn:li:(activity|share|ugcPost):(\\d{16,19})/ — capture
       BOTH the URN type (activity / share / ugcPost) and the numeric.

       CRITICAL: activity / share / ugcPost URNs are DIFFERENT namespaces.
       The same numeric ID resolves to different posts (or to nothing) in
       different namespaces. You MUST preserve the type when building the
       canonical URL — never collapse share/ugcPost to activity.

         post_url = https://www.linkedin.com/feed/update/urn:li:<TYPE>:<NUM>/
         activity_id = <NUM>            (bare numeric, for engaged-id check)

       If your click in 5a did NOT navigate (page still shows the SERP
       URL), fall back to the 3-dot menu → "Copy link to post" route
       (all clicks via click_at_xy per the BROWSER BACKEND block):
         - click the 3-dot control menu of the winner card
         - click the "Copy link to post" menu item
         - read the URL from clipboard via the run-code equivalent
           (bh_run js("""return await navigator.clipboard.readText()""")) (may fail
           with permission denied in headed Chrome — try Bash 'pbpaste' as a backup)
         - the slug encodes the URN type: parse /-(activity|share|ugcPost)-(\\d{16,19})/
           from the URL. Build canonical exactly as above using the captured TYPE.
         - Example: https://www.linkedin.com/posts/SLUG-share-7455...-pkG-...
           → urn_type = "share", activity_id = "7455...",
           post_url = https://www.linkedin.com/feed/update/urn:li:share:7455.../

   5c. If neither 5a nor the copy-link fallback yields a URN, drop this
       winner from your candidates list and pick the NEXT best one. Retry
       5a once on the second-best. If that also fails, write candidates: []
       and STOP — Phase B will skip cleanly. Do NOT loop through every
       candidate trying to resolve URNs.

   5d. Re-run the engaged-id check on the now-known numeric:
         python3 $REPO_DIR/scripts/linkedin_url.py --check-engaged-ids 'NUM'
       Exit 0 = already engaged, candidates: [], STOP.

6. Write the envelope to $PHASE_A_OUT with the winner (and ONLY the
   winner — discard runners-up, they're noise that won't be reused) and
   STOP:

\`\`\`bash
cat > $PHASE_A_OUT <<JSON_EOF
{
  "project": "$LI_PROJECT_NAME",
  "search_topic": "$LI_SEARCH_TOPIC",
  "language": "en",
  "queries_used": [
    {"query": "ai agents production",   "search_topic": "$LI_SEARCH_TOPIC", "candidates_found": 4, "serp_quality_score": 7.5, "dropped_below_floor": 2},
    {"query": "macos automation tools", "search_topic": "$LI_SEARCH_TOPIC", "candidates_found": 0, "serp_quality_score": null, "dropped_below_floor": 0},
    {"query": "claude code workflow",   "search_topic": "$LI_SEARCH_TOPIC", "candidates_found": 6, "serp_quality_score": 5.0, "dropped_below_floor": 9}
  ],
  "candidates": [
    {
      "post_url": "https://www.linkedin.com/feed/update/urn:li:activity:NUMERIC/",
      "activity_id": "NUMERIC",
      "all_urns": ["NUMERIC", "..."],
      "author_name": "First Last",
      "author_headline": "Headline | role | company (may be null)",
      "author_profile_url": "https://www.linkedin.com/in/SLUG/",
      "author_followers": null,
      "post_text": "post body, no newlines, no double quotes, no backticks",
      "age_hours": 6.5,
      "reactions": 42,
      "comments": 7,
      "reposts": 3,
      "search_topic": "$LI_SEARCH_TOPIC",
      "search_query": "ai agents production",
      "language": "en",
      "serp_quality_score": 7.5
    }
  ]
}
JSON_EOF
\`\`\`

   - queries_used MUST contain ONE row per query you ran (including
     zero-result ones — that is the whole point of the dud-learning).
   - project MUST equal "$LI_PROJECT_NAME" and every search_topic MUST
     equal "$LI_SEARCH_TOPIC". The search_topic is the assigned seed; the
     search_query is the literal phrase you ran on LinkedIn.
   - candidates_found is ALL cards the SERP returned, same as the discover
     script's result_count (Twitter model: no virality floor, nothing is
     dropped on score; cards are sorted by velocity_score DESC). Set
     dropped_below_floor to 0 for every query: the discover script no longer
     rejects cards, so its dropped_below_virality_floor is always 0. The
     dashboard reads raw SERP volume straight off candidates_found, so a
     query with candidates_found=0 still reads as "SERP returned nothing".
   - candidates contains AT MOST one row (the winner from step 5). It can
     be empty if step 5 found nothing engageable. bash will skip Phase B
     cleanly when empty.
   - The winner row MUST have non-null activity_id and post_url (resolved
     at step 5b). Do NOT write null URNs to candidates[] — Phase B no
     longer recovers them.
   - post_url MUST embed the correct URN namespace
     (urn:li:activity:NUM, urn:li:share:NUM, or urn:li:ugcPost:NUM) — NOT
     forcibly rewritten to activity. The shell trusts this URL verbatim.
   - candidates must NOT include posts you already engaged on or self-authored.
   - author_headline is optional on output; pass through whatever the
     discover script returned (may be null).
   - author_followers is null on the current LinkedIn layout; do not invent
     a value.
   - post_text must be safe to embed in a bash double-quoted string. Strip
     backticks, double quotes, and newlines before writing. Truncate to
     ~500 chars before writing into the envelope to keep Phase B's prompt
     compact (the full text is still available via the discover script log).

Then say '## Phase A: envelope written' and STOP.

CRITICAL: Use ONLY the browser tool described in the BROWSER BACKEND block
(mcp__linkedin-harness__bh_run). NEVER click the comment textbox. NEVER call
createComment. NEVER navigate to a post-compose flow. Phase B does all of that.
CRITICAL: Run exactly 8 search queries this run. Not 2, not 4, not 6. Eight.
Wider net = better odds of one ICP-fit hit. The rate budget can absorb it.
CRITICAL: NEVER use em dashes anywhere.
PROMPT_EOF
fi

# --- DORMANT unipile branch: OFF by default (see header). Reached ONLY with an
# --- explicit LINKEDIN_BACKEND=unipile override, which still 503s until the
# --- UniPile account is manually reconnected. Presence here != in use.
if [ "$LINKEDIN_BACKEND" = "unipile" ]; then
  # UniPile path: no headed browser, so no linkedin-browser lock, no
  # ensure_browser_healthy, no harness MCP, no PreToolUse hook lockfile.
  # --strict-mcp-config with NO --mcp-config loads zero MCP servers, leaving
  # the default Bash tool the agent uses to shell out to linkedin_unipile.py.
  set +e
  "$REPO_DIR/scripts/run_claude.sh" "run-linkedin-phaseA" --strict-mcp-config --output-format stream-json --verbose -p "$(cat "$PHASE_A_PROMPT")" 2>&1 | tee -a "$LOG_FILE"
  PA_RC=${PIPESTATUS[0]}
  set -e
  rm -f "$PHASE_A_PROMPT"
else
  # Acquire linkedin-browser ONLY for the Phase A Claude run. The shell lock
  # (skill/lock.sh) is FIFO-queued, so if a peer pipeline (dm-replies-linkedin,
  # audit-linkedin, link-edit-linkedin, or our own prior cycle's Phase B) is
  # mid-run, this BLOCKS and polls until release rather than skipping. That
  # matches the run-twitter-cycle.sh + run-reddit-search.sh behaviour.
  #
  # run_claude.sh auto-exports SA_PIPELINE_LOCKED=1 + SA_PIPELINE_PLATFORM,
  # which the PreToolUse hook (~/.claude/hooks/linkedin-agent-lock.sh) honors
  # to skip the cross-session block check. Without that bypass, the hook
  # previously rejected our Claude session if the prior cycle's JSONL was
  # <60s stale (tail-flush window), producing $8.91 empty-envelope runs.
  # 2026-05-01: false-positive hardened by env-var bypass + pgrep alive check.
  acquire_lock "linkedin-browser" 3600
  ensure_linkedin_browser_for_backend 2>&1 | tee -a "$LOG_FILE"

  set +e
  "$REPO_DIR/scripts/run_claude.sh" "run-linkedin-phaseA" --strict-mcp-config --mcp-config "$MCP_CONFIG_FILE" --output-format stream-json --verbose -p "$(cat "$PHASE_A_PROMPT")" 2>&1 | tee -a "$LOG_FILE"
  PA_RC=${PIPESTATUS[0]}
  set -e

  release_lock "linkedin-browser"
  # Defense-in-depth: explicitly clear the hook-layer lockfile so the next
  # pipeline cycle's PreToolUse never sees a stale entry from us. The
  # run_claude.sh exit trap already does this in the happy path; this
  # repeat is harmless and covers SIGKILL of run_claude.sh.
  rm -f "$HOME/.claude/linkedin-agent-lock.json"
  rm -f "$PHASE_A_PROMPT"
fi

# ===== Validate Phase A envelope + run Python ingest steps =====
if [ "$PA_RC" -ne 0 ] || [ ! -s "$PHASE_A_OUT" ]; then
  echo "Phase A: no envelope (rc=$PA_RC, $([ -s "$PHASE_A_OUT" ] && echo 'file non-empty' || echo 'file empty')). Skipping Phase B." | tee -a "$LOG_FILE"
  rm -f "$PHASE_A_OUT"
  ELAPSED=$(( $(date +%s) - RUN_START_EPOCH ))
  _COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START_EPOCH" --scripts "run-linkedin-phaseA" "run-linkedin-phaseB" 2>/dev/null || echo "0.0000")
  python3 "$REPO_DIR/scripts/log_run.py" --script post_linkedin --posted 0 --skipped 1 --failed 0 --cost "$_COST" --elapsed "$ELAPSED" || true
  _SA_RUN_SUMMARY_EMITTED=1  # short-circuit EXIT-trap emitter; this branch already wrote a tailored line
  echo "=== Run complete: $(date) ===" | tee -a "$LOG_FILE"
  find "$LOG_DIR" -name "run-linkedin-*.log" -mtime +7 -delete 2>/dev/null || true
  exit 0
fi

# Validate the envelope is well-formed JSON; if it isn't, ledger the run
# as failed and skip Phase B rather than crashing the ingest scripts.
if ! python3 -c "import json,sys; json.load(open('$PHASE_A_OUT'))" 2>/dev/null; then
  echo "Phase A: envelope is malformed JSON; skipping Phase B." | tee -a "$LOG_FILE"
  rm -f "$PHASE_A_OUT"
  ELAPSED=$(( $(date +%s) - RUN_START_EPOCH ))
  _COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START_EPOCH" --scripts "run-linkedin-phaseA" "run-linkedin-phaseB" 2>/dev/null || echo "0.0000")
  python3 "$REPO_DIR/scripts/log_run.py" --script post_linkedin --posted 0 --skipped 0 --failed 1 --cost "$_COST" --elapsed "$ELAPSED" || true
  _SA_RUN_SUMMARY_EMITTED=1
  echo "=== Run complete: $(date) ===" | tee -a "$LOG_FILE"
  exit 0
fi

PA_PROJECT=$(python3 -c "import json; print(json.load(open('$PHASE_A_OUT')).get('project',''))" 2>/dev/null || echo "")
PA_PROJECT="$LI_PROJECT_NAME"
PA_SEARCH_TOPIC=$(python3 -c "import json; print(json.load(open('$PHASE_A_OUT')).get('search_topic',''))" 2>/dev/null || echo "")
PA_SEARCH_TOPIC="$LI_SEARCH_TOPIC"

# Ingest queries_used into linkedin_search_attempts (one row per query, dud-aware).
LI_PROJECT_NAME="$LI_PROJECT_NAME" LI_SEARCH_TOPIC="$LI_SEARCH_TOPIC" python3 -c "
import os
import json
env = json.load(open('$PHASE_A_OUT'))
project = os.environ.get('LI_PROJECT_NAME') or env.get('project','')
search_topic = os.environ.get('LI_SEARCH_TOPIC') or env.get('search_topic','')
out = []
for q in env.get('queries_used') or []:
    out.append({
        'query': q.get('query',''),
        'project': project,
        'search_topic': search_topic,
        'candidates_found': q.get('candidates_found') or 0,
        'serp_quality_score': q.get('serp_quality_score'),
        'dropped_below_floor': q.get('dropped_below_floor') or 0,
    })
import sys; json.dump(out, sys.stdout)
" | python3 "$REPO_DIR/scripts/log_linkedin_search_attempts.py" --batch-id "$BATCH_ID" 2>&1 | tee -a "$LOG_FILE" || true

# Ingest candidates into linkedin_candidates (scored + deduped).
# Stamp serp_quality_score onto each candidate from its parent query so the
# scoring upsert has the per-row signal even though SERP quality is judged
# per-query.
LI_PROJECT_NAME="$LI_PROJECT_NAME" LI_SEARCH_TOPIC="$LI_SEARCH_TOPIC" python3 -c "
import os
import json
env = json.load(open('$PHASE_A_OUT'))
quality_by_query = {q.get('query',''): q.get('serp_quality_score') for q in env.get('queries_used') or []}
project = os.environ.get('LI_PROJECT_NAME') or env.get('project','')
search_topic = os.environ.get('LI_SEARCH_TOPIC') or env.get('search_topic','')
lang = env.get('language','en')
cands = []
for c in env.get('candidates') or []:
    if not isinstance(c, dict):
        continue
    c['matched_project'] = project
    c['search_topic'] = search_topic
    c.setdefault('language', lang)
    if c.get('serp_quality_score') is None:
        c['serp_quality_score'] = quality_by_query.get(c.get('search_query',''))
    cands.append(c)
import sys; json.dump(cands, sys.stdout)
" | python3 "$REPO_DIR/scripts/score_linkedin_candidates.py" --batch-id "$BATCH_ID" 2>&1 | tee -a "$LOG_FILE" || true

# ===== Pick top pending candidate from this batch (or fallback to global pending) =====
# We try the freshest batch first so a high-velocity post we just discovered
# wins over an older pending row that didn't get posted last cycle. If the
# fresh batch has zero usable rows (everything we saw was already engaged),
# fall back only inside the same pre-picked project/topic. A cycle should
# never post an older candidate from some other project just because the
# fresh search returned nothing.
PA_PICK=$(REPO_DIR="$REPO_DIR" BATCH_ID="$BATCH_ID" LI_PROJECT_NAME="$LI_PROJECT_NAME" LI_SEARCH_TOPIC="$LI_SEARCH_TOPIC" python3 - <<'PY' 2>/dev/null || echo "{}"
import json
import os
import sys

repo = os.environ["REPO_DIR"]
batch_id = os.environ["BATCH_ID"]
project = os.environ.get("LI_PROJECT_NAME", "")
search_topic = os.environ.get("LI_SEARCH_TOPIC", "")

sys.path.insert(0, os.path.join(repo, "scripts"))
from http_api import api_get

# Two-stage pending pick (freshest batch first, then same-project/topic
# fallback within a 96h window) runs server-side; see route.ts. The returned
# candidate shape matches the keys the PA_* extractors below expect exactly.
resp = api_get(
    "/api/v1/linkedin-candidates/next-pending",
    {
        "batch_id": batch_id,
        "project": project,
        "search_topic": search_topic,
        "max_age_hours": 96,
    },
)
cand = (resp.get("data") or {}).get("candidate")
if not cand:
    print(json.dumps({}))
else:
    out = {
        "post_url": cand.get("post_url") or "",
        "activity_id": cand.get("activity_id") or "",
        "all_urns": cand.get("all_urns") or "",
        "author_name": cand.get("author_name") or "",
        "author_profile_url": cand.get("author_profile_url") or "",
        "post_text": cand.get("post_text") or "",
        "language": cand.get("language") or "en",
        "project": cand.get("project") or project,
        "velocity_score": float(cand.get("velocity_score") or 0),
        "search_query": cand.get("search_query") or "",
        "search_topic": cand.get("search_topic") or search_topic,
    }
    print(json.dumps(out))
PY
)

PA_URL=$(echo "$PA_PICK" | python3 -c "import json,sys; print(json.load(sys.stdin).get('post_url',''))")
PA_ACTIVITY_ID=$(echo "$PA_PICK" | python3 -c "import json,sys; print(json.load(sys.stdin).get('activity_id',''))")
PA_ALL_URNS=$(echo "$PA_PICK" | python3 -c "import json,sys; print(json.load(sys.stdin).get('all_urns',''))")
PA_AUTHOR_NAME=$(echo "$PA_PICK" | python3 -c "import json,sys; print(json.load(sys.stdin).get('author_name',''))")
PA_AUTHOR_URL=$(echo "$PA_PICK" | python3 -c "import json,sys; print(json.load(sys.stdin).get('author_profile_url',''))")
PA_EXCERPT=$(echo "$PA_PICK" | python3 -c "import json,sys; print(json.load(sys.stdin).get('post_text',''))")
PA_LANG=$(echo "$PA_PICK" | python3 -c "import json,sys; print(json.load(sys.stdin).get('language','en'))")
PA_TITLE_HINT=$(echo "$PA_PICK" | python3 -c "import json,sys; v=json.load(sys.stdin).get('post_text',''); print((v or '').split('\\n')[0])")
PA_VELOCITY=$(echo "$PA_PICK" | python3 -c "import json,sys; print(json.load(sys.stdin).get('velocity_score',0))")
PA_QUERY=$(echo "$PA_PICK" | python3 -c "import json,sys; print(json.load(sys.stdin).get('search_query',''))")
PA_SEARCH_TOPIC=$(echo "$PA_PICK" | python3 -c "import json,sys; print(json.load(sys.stdin).get('search_topic',''))")
[ -z "$PA_SEARCH_TOPIC" ] && PA_SEARCH_TOPIC="$LI_SEARCH_TOPIC"
[ -z "${PA_PROJECT:-}" ] && PA_PROJECT=$(echo "$PA_PICK" | python3 -c "import json,sys; print(json.load(sys.stdin).get('project',''))")

# ===== If no candidate, exit cleanly =====
# Path D: Phase A's LLM is responsible for clicking-into-best to capture the
# URN, so every row reaching this gate must already have a numeric URN.
if [ -z "$PA_ACTIVITY_ID" ] || [ -z "$PA_URL" ]; then
  echo "Phase A: no postable candidate after scoring (project='$PA_PROJECT' topic='$PA_SEARCH_TOPIC'). Skipping Phase B." | tee -a "$LOG_FILE"
  rm -f "$PHASE_A_OUT"
  ELAPSED=$(( $(date +%s) - RUN_START_EPOCH ))
  _COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START_EPOCH" --scripts "run-linkedin-phaseA" "run-linkedin-phaseB" 2>/dev/null || echo "0.0000")
  python3 "$REPO_DIR/scripts/log_run.py" --script post_linkedin --posted 0 --skipped 1 --failed 0 --cost "$_COST" --elapsed "$ELAPSED" || true
  _SA_RUN_SUMMARY_EMITTED=1
  echo "=== Run complete: $(date) ===" | tee -a "$LOG_FILE"
  exit 0
fi

# activity_id must be 16-19 digit numeric.
case "$PA_ACTIVITY_ID" in
  ''|*[!0-9]*)
    echo "Phase A picked non-numeric activity_id '$PA_ACTIVITY_ID'. Skipping Phase B." | tee -a "$LOG_FILE"
    rm -f "$PHASE_A_OUT"
    ELAPSED=$(( $(date +%s) - RUN_START_EPOCH ))
    _COST=$(python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START_EPOCH" --scripts "run-linkedin-phaseA" "run-linkedin-phaseB" 2>/dev/null || echo "0.0000")
    python3 "$REPO_DIR/scripts/log_run.py" --script post_linkedin --posted 0 --skipped 0 --failed 1 --cost "$_COST" --elapsed "$ELAPSED" || true
    _SA_RUN_SUMMARY_EMITTED=1
    echo "=== Run complete: $(date) ===" | tee -a "$LOG_FILE"
    exit 0
    ;;
esac

# Build canonical URL. Trust the row's post_url if it's already a
# well-formed feed/update/urn:li:(activity|share|ugcPost):NUMERIC/ URL,
# because activity / share / ugcPost are DIFFERENT namespaces. Falling
# back to "always urn:li:activity:" caused "Post not found" 404s on
# share-namespace posts (Andreas Mautsch / Apple Container, 2026-05-01).
if [[ "$PA_URL" =~ ^https://www\.linkedin\.com/feed/update/urn:li:(activity|share|ugcPost):[0-9]{16,19}/?$ ]]; then
  # Already canonical with correct namespace — use it verbatim, just
  # ensure trailing slash.
  case "$PA_URL" in */) ;; *) PA_URL="$PA_URL/" ;; esac
else
  # No usable post_url on the row (legacy / malformed). Fall back to
  # building from activity_id; default namespace is 'activity' which is
  # correct for the historical majority. If the post is actually a
  # share/ugcPost, Phase B's URN-type fallback (below) will recover.
  PA_URL="https://www.linkedin.com/feed/update/urn:li:activity:${PA_ACTIVITY_ID}/"
fi

# The UniPile comment endpoint addresses a post by its social_id (the
# urn:li:<ns>:<num> embedded in the canonical URL), not the bare numeric.
# Extract it from PA_URL so Phase B's UniPile branch can POST to
# /posts/{social_id}/comments. Harmless/unused for the browser path.
_pa_url_tail="${PA_URL#*/feed/update/}"
PA_SOCIAL_ID="${_pa_url_tail%/}"

echo "Phase A: chose project=$PA_PROJECT topic='$PA_SEARCH_TOPIC' activity=$PA_ACTIVITY_ID velocity=$PA_VELOCITY query='$PA_QUERY'" | tee -a "$LOG_FILE"

# Look up the chosen project's full config (only this one).
PROJECT_FULL=$(python3 -c "
import json, os
c = json.load(open(os.path.expanduser('~/social-autoposter/config.json')))
p = next((p for p in c.get('projects',[]) if p['name']=='$PA_PROJECT'), {})
print(json.dumps(p, indent=2))
")

# Phase B inputs (only Phase B needs styles + top performers).
# Engagement-style picker (2026-05-31 LinkedIn alignment to Twitter): pick ONE
# assigned style for this cycle PROGRAMMATICALLY, then hand it to the Claude
# session instead of letting the post pipeline invent freely (the legacy
# generate_styles_block path). The picked style flows three places, identical
# to run-twitter-cycle.sh: (1) --style filter for top_performers.py so the
# exemplars section shows only posts matching the assigned style, (2)
# s4l_render_style_block so the prompt block embeds the same assignment, (3)
# --assigned-style/--assigned-mode flags on log_post.py so the post pipeline
# coerces USE-mode drift back to the assigned name and registers INVENT-mode
# inventions. On invent mode PICKED_STYLE is empty and top_performers stays
# unfiltered (model sees the full landscape to invent against).
source "$REPO_DIR/skill/styles.sh"
STYLE_ASSIGN_FILE=$(mktemp -t s4l_linkedin_assign_XXXXXX.json)
s4l_pick_style linkedin posting "$STYLE_ASSIGN_FILE" >/dev/null 2>&1 || true
PICKED_STYLE=$(python3 -c "
import json
try:
    with open('$STYLE_ASSIGN_FILE') as f:
        d = json.load(f)
    print(d.get('style') or '')
except Exception:
    print('')
" 2>/dev/null)
PICKED_MODE=$(python3 -c "
import json
try:
    with open('$STYLE_ASSIGN_FILE') as f:
        d = json.load(f)
    print(d.get('mode') or 'use')
except Exception:
    print('use')
" 2>/dev/null)
echo "Engagement style assigned: mode=$PICKED_MODE style=${PICKED_STYLE:-(invent)}" | tee -a "$LOG_FILE"

if [ -n "$PICKED_STYLE" ]; then
    TOP_REPORT=$(python3 "$REPO_DIR/scripts/top_performers.py" --platform linkedin --style "$PICKED_STYLE" 2>/dev/null || echo "(top performers report unavailable)")
else
    TOP_REPORT=$(python3 "$REPO_DIR/scripts/top_performers.py" --platform linkedin 2>/dev/null || echo "(top performers report unavailable)")
fi
STYLES_BLOCK=$(s4l_render_style_block "$STYLE_ASSIGN_FILE" linkedin posting)
# Best-effort cleanup of the assignment tempfile at wrapper exit.
trap 'rm -f "$STYLE_ASSIGN_FILE" 2>/dev/null || true' EXIT

# Prior-interactions context: surface our last 5 comments on threads by the
# same author in the past 30 days (soft context — vary angle, don't repeat).
# Empty when we have no history with this person. Failure is silent.
AUTHOR_HISTORY_BLOCK=""
if [ -n "${PA_AUTHOR_NAME:-}" ]; then
    AUTHOR_HISTORY_BLOCK=$(python3 "$REPO_DIR/scripts/author_history_block.py" --platform linkedin --author "$PA_AUTHOR_NAME" --days 30 --limit 5 2>>"$LOG_FILE" || true)
fi

PA_SEARCH_TOPIC_ARG=$(python3 -c "import shlex,sys; print(shlex.quote(sys.argv[1]))" "$PA_SEARCH_TOPIC")

# ===== Link-tail decision (Twitter-style) =====
# LinkedIn comments are engagement-only by default; the drafting prompt never
# emits a URL, so wrap-post-text (which only short-links URLs already present)
# is a no-op and our comments carry no link. Mirror the Twitter "link tail":
# resolve the project's clean landing URL, A/B-gate it, and when the arm is
# 'link' have Phase B append ONE CTA bridge sentence ending in that URL via
# link_tail.py (then wrap-post-text short-links it). Control arm posts no link.
LINK_URL=$(python3 -c "import json,sys; p=json.loads(sys.argv[1]); print((p.get('website') or p.get('url') or '').strip())" "$PROJECT_FULL")
LINKEDIN_TAIL_LINK_RATE="${LINKEDIN_TAIL_LINK_RATE:-0.5}"
TAIL_DECISION=$(python3 -c "import random,sys; url=sys.argv[1].strip(); rate=float(sys.argv[2]); print('link' if (url and random.random()<rate) else 'no_link')" "$LINK_URL" "$LINKEDIN_TAIL_LINK_RATE")
echo "[link-tail] project=$PA_PROJECT url=$LINK_URL rate=$LINKEDIN_TAIL_LINK_RATE decision=$TAIL_DECISION" | tee -a "$LOG_FILE"

# Allow Chrome's profile lockfile to release between phases.
sleep 3

# ===== Phase B: compose + post + verify + log =====
PHASE_B_PROMPT=$(mktemp /tmp/sa-run-linkedin-phaseB-prompt-XXXXXX)
# --- DORMANT unipile branch: OFF by default (see header). Reached ONLY with an
# --- explicit LINKEDIN_BACKEND=unipile override, which still 503s until the
# --- UniPile account is manually reconnected. Presence here != in use.
if [ "$LINKEDIN_BACKEND" = "unipile" ]; then
# ----- Phase B prompt: UniPile REST backend (no browser) -----
cat > "$PHASE_B_PROMPT" <<PROMPT_EOF
You are the Social Autoposter (Phase B), running on the UniPile REST backend
(no browser). Your job: post ONE comment on a pre-selected LinkedIn post
(already chosen + scored by Phase A) via the UniPile API, verify it landed by
reading the post's comments back, log it. STOP. Do NOT search for other
candidates.

Read $SKILL_FILE for tone and content rules.

## Pre-selected candidate (from Phase A — DO NOT rediscover)
- Project: **$PA_PROJECT**
- Thread URL: $PA_URL
- Post social_id (UniPile comment target): $PA_SOCIAL_ID
- Activity URN (numeric): $PA_ACTIVITY_ID
- All URNs already seen: $PA_ALL_URNS
- Author: $PA_AUTHOR_NAME ($PA_AUTHOR_URL)
- Post excerpt: $PA_EXCERPT
- Post title hint: $PA_TITLE_HINT
- Language: $PA_LANG
- Velocity score: $PA_VELOCITY (Phase A picked this as the top candidate)
- Search topic that guided discovery: '$PA_SEARCH_TOPIC'
- Search query that surfaced it: '$PA_QUERY'

$AUTHOR_HISTORY_BLOCK

## Project config
$PROJECT_FULL

## Top performers feedback (use to pick a comment angle)
$TOP_REPORT

$STYLES_BLOCK

## Workflow

1. Defensive engaged-id re-check. Run via Bash:
     python3 $REPO_DIR/scripts/linkedin_url.py --check-engaged-ids '$PA_ACTIVITY_ID'
   If exit code 0 (already engaged), mark the candidate skipped:
     python3 -c "import sys; sys.path.insert(0,'$REPO_DIR/scripts'); from http_api import api_patch; api_patch('/api/v1/linkedin-candidates', {'activity_id': '$PA_ACTIVITY_ID', 'status': 'skipped'}, ok_on_404=True)"
   then STOP with '## Already engaged (defensive catch in Phase B)'.

2. Draft the comment using the ASSIGNED engagement style (the style block above
   already assigns exactly one). This cycle: mode=$PICKED_MODE
   style='${PICKED_STYLE:-(invent)}'.
   - In USE mode ($PICKED_MODE=use) you MUST apply the assigned style
     '${PICKED_STYLE}' verbatim; do NOT pick a different style, do NOT invent a new
     name. (If your draft drifts, the orchestrator silently coerces it back to
     the assigned name at log time, so just use the assigned one.)
   - In INVENT mode ($PICKED_MODE=invent) you craft a NEW snake_case style name
     not in the curated block above, fitting the post + project. When you log
     (step 5 rejected / step 6 success), ALSO append this flag to the log_post.py
     command so the invention registers in engagement_styles_registry:
       --new-style '{\"description\":\"...\",\"example\":\"...\",\"why_existing_didnt_fit\":\"...\"}'
     (OMIT --new-style entirely in USE mode.)
   Apply the project's voice block (voice.tone, never violate voice.never,
   mirror voice.examples if present). Reply in $PA_LANG.
   The learned_preferences block in the Project config above (when present) is
   distilled human review feedback and is MANDATORY, not advisory: follow every
   learned_preferences.draft_style_notes entry when writing (it overrides the
   engagement style's structural template on conflict), and treat
   learned_preferences.audience_avoid / thread_avoid matches as strong reasons
   to skip. Never violate content_guardrails.do_not.
   NEVER use em dashes.

2a. LINK TAIL (A/B-gated, decided by the wrapper). The decision for THIS run is:
       TAIL_LINK_DECISION = '$TAIL_DECISION'
       LINK_URL           = '$LINK_URL'
    If TAIL_LINK_DECISION is 'link' AND LINK_URL is non-empty, append ONE short
    CTA bridge sentence ending in LINK_URL to your draft. Run via Bash:
       TAIL_RESULT=\$(python3 $REPO_DIR/scripts/link_tail.py \\
         --reply-text "YOUR_COMMENT_TEXT" \\
         --link-url '$LINK_URL' \\
         --thread-text "$PA_EXCERPT" \\
         --project '$PA_PROJECT' \\
         --platform linkedin)
       echo "\$TAIL_RESULT"
    Parse {ok, text}. If ok is true, REPLACE your draft with tail_result.text
    (it now ends in the URL); that becomes YOUR_COMMENT_TEXT for every step
    below. If ok is false, keep your original draft (no link this run).
    If TAIL_LINK_DECISION is 'no_link' OR LINK_URL is empty, SKIP this step and
    do NOT add any URL yourself (this is the control arm).

2b. Wrap any URLs in your draft before posting. Run:
     WRAP_RESULT=\$(python3 $REPO_DIR/scripts/dm_short_links.py wrap-post-text \\
       --text "YOUR_COMMENT_TEXT" --platform linkedin --project '$PA_PROJECT')
   If wrap_result.ok is true: use wrap_result.text as the final comment text
   and save wrap_result.minted_session as MINTED_SESSION. Otherwise use the
   original draft and set MINTED_SESSION to empty.

3. Post the comment via the UniPile API (use the possibly-wrapped text from 2b):
     COMMENT_RESULT=\$(python3 $REPO_DIR/scripts/linkedin_unipile.py comment --social-id '$PA_SOCIAL_ID' --text "YOUR_COMMENT_TEXT")
     echo "\$COMMENT_RESULT"
   The command prints JSON {ok, status, response, comment_urn, our_url} and
   exits 0 iff ok. A successful post is status 200 or 201 with
   response.object == "CommentSent" and (usually) a numeric response.comment_id.

4. POST-SUBMIT VERIFICATION (mandatory). Extract ok + comment_id:
     COMMENT_OK=\$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('ok'))" "\$COMMENT_RESULT" 2>/dev/null || echo "False")
     COMMENT_ID=\$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); r=d.get('response') or {}; print(r.get('comment_id') or '')" "\$COMMENT_RESULT" 2>/dev/null || echo "")
   Then read the comment back from the post to PROVE it rendered:
     python3 $REPO_DIR/scripts/linkedin_unipile.py comments --social-id '$PA_SOCIAL_ID' --contains-id "\$COMMENT_ID"
   This command exits 0 iff our comment_id is present in the post's comment list.
     SUCCESS  = COMMENT_OK is "True" AND (the read-back exited 0, OR COMMENT_ID
                was empty but COMMENT_RESULT showed status 201 / object CommentSent).
     REJECTED = ok false, non-2xx status, or an error response object.

5. If REJECTED, do NOT call the success log path. Mark candidate skipped:
     python3 -c "import sys; sys.path.insert(0,'$REPO_DIR/scripts'); from http_api import api_patch; api_patch('/api/v1/linkedin-candidates', {'activity_id': '$PA_ACTIVITY_ID', 'status': 'skipped'}, ok_on_404=True)"
   Then ledger the soft-block:
     python3 $REPO_DIR/scripts/log_post.py --rejected \\
       --platform linkedin \\
       --thread-url '$PA_URL' \\
       --our-content 'YOUR_COMMENT_TEXT' \\
       --project '$PA_PROJECT' \\
       --thread-author '$PA_AUTHOR_NAME' \\
       --thread-title '$PA_TITLE_HINT' \\
       --engagement-style STYLE_YOU_CHOSE \\
       --assigned-style '$PICKED_STYLE' \\
       --assigned-mode '$PICKED_MODE' \\
       --search-topic $PA_SEARCH_TOPIC_ARG \\
       --language '$PA_LANG' \\
       --rejection-reason 'UNIPILE: <verbatim status + response.object/error from COMMENT_RESULT>' \\
       --network-response "\$COMMENT_RESULT"
   Then STOP with '## Comment soft-blocked, ledgered'.

6. If SUCCESS, log the post and mark candidate posted:
     LOG_RESULT=\$(python3 $REPO_DIR/scripts/log_post.py \\
       --platform linkedin \\
       --thread-url '$PA_URL' \\
       --our-url '$PA_URL' \\
       --our-content 'YOUR_COMMENT_TEXT' \\
       --project '$PA_PROJECT' \\
       --thread-author '$PA_AUTHOR_NAME' \\
       --thread-title '$PA_TITLE_HINT' \\
       --engagement-style STYLE_YOU_CHOSE \\
       --assigned-style '$PICKED_STYLE' \\
       --assigned-mode '$PICKED_MODE' \\
       --search-topic $PA_SEARCH_TOPIC_ARG \\
       --language '$PA_LANG' \\
       --urns '$PA_ACTIVITY_ID')
     echo "\$LOG_RESULT"
     python3 -c "import sys; sys.path.insert(0,'$REPO_DIR/scripts'); from http_api import api_patch; api_patch('/api/v1/linkedin-candidates', {'activity_id': '$PA_ACTIVITY_ID', 'status': 'posted'}, ok_on_404=True)"
     If MINTED_SESSION is non-empty: extract post_id from LOG_RESULT and backfill:
       LOG_POST_ID=\$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('post_id',''))" "\$LOG_RESULT" 2>/dev/null || echo "")
       [ -n "\$LOG_POST_ID" ] && python3 $REPO_DIR/scripts/dm_short_links.py backfill-post \\
         --minted-session "\$MINTED_SESSION" --post-id "\$LOG_POST_ID"

CRITICAL: ONE post only. If anything fails, STOP — do NOT pick another candidate.
CRITICAL: Use ONLY the Bash tool plus linkedin_unipile.py / log_post.py /
dm_short_links.py / linkedin_url.py. There is NO browser; NEVER attempt
any browser MCP tools (none are loaded).
CRITICAL: NEVER use em dashes.
PROMPT_EOF
else
# ----- Phase B prompt: headed-Chrome browser backend (linkedin-harness) -----
cat > "$PHASE_B_PROMPT" <<PROMPT_EOF
You are the Social Autoposter (Phase B). Your job: post ONE comment on a
pre-selected LinkedIn post (already chosen + scored by Phase A), verify it
landed, log it. STOP. Do NOT search for other candidates.

Read $SKILL_FILE for tone and content rules.

$BROWSER_INSTRUCTIONS

## Pre-selected candidate (from Phase A — DO NOT rediscover)
- Project: **$PA_PROJECT**
- Thread URL: $PA_URL
- Activity URN: $PA_ACTIVITY_ID
- All URNs already seen: $PA_ALL_URNS
- Author: $PA_AUTHOR_NAME ($PA_AUTHOR_URL)
- Post excerpt: $PA_EXCERPT
- Post title hint: $PA_TITLE_HINT
- Language: $PA_LANG
- Velocity score: $PA_VELOCITY (Phase A picked this as the top candidate)
- Search topic that guided discovery: '$PA_SEARCH_TOPIC'
- Search query that surfaced it: '$PA_QUERY'

$AUTHOR_HISTORY_BLOCK

## Project config
$PROJECT_FULL

## Top performers feedback (use to pick a comment angle)
$TOP_REPORT

$STYLES_BLOCK

## Workflow

1. Navigate to $PA_URL (per the BROWSER BACKEND block).

   1a. URN-NAMESPACE FALLBACK. After navigation, read the page DOM/text (per the
       BROWSER BACKEND block: bh_run js("""return document.body.innerText""") or a
       screenshot). If it contains the markers 'Post not found' OR 'This post
       was deleted or removed' OR 'this content isn'\''t available', the
       URN namespace in $PA_URL may be wrong (activity/share/ugcPost are
       DIFFERENT namespaces with different numeric IDs — Phase A may have
       guessed wrong on a copy-link path). Before declaring the post
       unavailable, retry the other two namespaces:

         * Extract the bare numeric '$PA_ACTIVITY_ID'.
         * Extract the current namespace from $PA_URL (one of activity, share, ugcPost).
         * Try each of the OTHER two namespaces in turn:
             - https://www.linkedin.com/feed/update/urn:li:share:$PA_ACTIVITY_ID/
             - https://www.linkedin.com/feed/update/urn:li:ugcPost:$PA_ACTIVITY_ID/
             - https://www.linkedin.com/feed/update/urn:li:activity:$PA_ACTIVITY_ID/
           (skip whichever you already tried). Navigate to each (per the
           BROWSER BACKEND block); after each, read the DOM/text the same way;
           if the post-not-found markers are absent AND a comment editor / post
           body renders, that URL is the correct one — adopt it and continue
           from step 2.
         * If ALL THREE namespaces hit post-not-found markers, the post
           genuinely no longer exists. Mark candidate skipped:
             python3 -c "import sys; sys.path.insert(0,'$REPO_DIR/scripts'); from http_api import api_patch; api_patch('/api/v1/linkedin-candidates', {'activity_id': '$PA_ACTIVITY_ID', 'status': 'skipped'}, ok_on_404=True)"
           Update the run-level counter signal: print a line containing
           the literal token 'PHASE_B_SKIP_POST_UNAVAILABLE' so the wrapper
           can attribute it. Then STOP with '## Post unavailable, candidate skipped'.

   1b. If you found a working namespace different from $PA_URL, persist it
       so future navigations / engaged-id checks use the right canonical:
         python3 -c "import sys; sys.path.insert(0,'$REPO_DIR/scripts'); from http_api import api_patch; api_patch('/api/v1/linkedin-candidates', {'activity_id': '$PA_ACTIVITY_ID', 'post_url': '<WORKING_URL>'}, ok_on_404=True)"

2. Defensive engaged-id re-check (Phase A may have missed a URN that only
   surfaces after the post page fully loads). Walk the rendered DOM for ALL
   URNs (activity, share, ugcPost forms), merge with '$PA_ALL_URNS', and run:
     python3 $REPO_DIR/scripts/linkedin_url.py --check-engaged-ids 'MERGED_URNS'
   If exit code 0 (already engaged), mark the candidate skipped:
     python3 -c "import sys; sys.path.insert(0,'$REPO_DIR/scripts'); from http_api import api_patch; api_patch('/api/v1/linkedin-candidates', {'activity_id': '$PA_ACTIVITY_ID', 'status': 'skipped'}, ok_on_404=True)"
   then STOP with '## Already engaged (defensive catch in Phase B)'.

3. Draft the comment using the ASSIGNED engagement style (the style block above
   already assigns exactly one). This cycle: mode=$PICKED_MODE
   style='${PICKED_STYLE:-(invent)}'.
   - In USE mode ($PICKED_MODE=use) you MUST apply the assigned style
     '${PICKED_STYLE}' verbatim; do NOT pick a different style, do NOT invent a new
     name. (If your draft drifts, the orchestrator silently coerces it back to
     the assigned name at log time, so just use the assigned one.)
   - In INVENT mode ($PICKED_MODE=invent) you craft a NEW snake_case style name
     not in the curated block above, fitting the post + project. When you log
     (step 6 rejected / step 7 success), ALSO append this flag to the log_post.py
     command so the invention registers in engagement_styles_registry:
       --new-style '{\"description\":\"...\",\"example\":\"...\",\"why_existing_didnt_fit\":\"...\"}'
     (OMIT --new-style entirely in USE mode.)
   Apply the project's voice block (voice.tone, never violate voice.never,
   mirror voice.examples if present). Reply in $PA_LANG.
   The learned_preferences block in the Project config above (when present) is
   distilled human review feedback and is MANDATORY, not advisory: follow every
   learned_preferences.draft_style_notes entry when writing (it overrides the
   engagement style's structural template on conflict), and treat
   learned_preferences.audience_avoid / thread_avoid matches as strong reasons
   to skip. Never violate content_guardrails.do_not.
   NEVER use em dashes.

3a. LINK TAIL (A/B-gated, decided by the wrapper). The decision for THIS run is:
       TAIL_LINK_DECISION = '$TAIL_DECISION'
       LINK_URL           = '$LINK_URL'
    If TAIL_LINK_DECISION is 'link' AND LINK_URL is non-empty, append ONE short
    CTA bridge sentence ending in LINK_URL to your draft. Run via Bash:
       TAIL_RESULT=\$(python3 $REPO_DIR/scripts/link_tail.py \\
         --reply-text "YOUR_COMMENT_TEXT" \\
         --link-url '$LINK_URL' \\
         --thread-text "$PA_EXCERPT" \\
         --project '$PA_PROJECT' \\
         --platform linkedin)
       echo "\$TAIL_RESULT"
    Parse {ok, text}. If ok is true, REPLACE your draft with tail_result.text
    (it now ends in the URL); that becomes YOUR_COMMENT_TEXT for every step
    below. If ok is false, keep your original draft (no link this run).
    If TAIL_LINK_DECISION is 'no_link' OR LINK_URL is empty, SKIP this step and
    do NOT add any URL yourself (this is the control arm).

3b. Wrap any URLs in your draft before typing. Run:
     WRAP_RESULT=\$(python3 $REPO_DIR/scripts/dm_short_links.py wrap-post-text \\
       --text "YOUR_COMMENT_TEXT" --platform linkedin --project '$PA_PROJECT')
   If wrap_result.ok is true: use wrap_result.text as the final comment text
   and save wrap_result.minted_session as MINTED_SESSION. Otherwise use the
   original draft and set MINTED_SESSION to empty.

3c. AUTO-LIKE the main post (mandatory, deterministic, FAIL-SOFT). Before you
    comment, react Like to the post itself, mirroring the Twitter pipeline
    (every successful engagement also likes the parent). This is fail-soft: a
    like failure must NEVER block or fail the comment. If it doesn't work in
    two tries, log 'auto-like skipped' and proceed straight to step 4.
    Primary (deterministic) path via the BROWSER BACKEND block:
      bh_run js("""return (() => { const btn = document.querySelector('button.react-button__trigger, .feed-shared-social-action-bar button[aria-label*=Like]'); if(!btn) return JSON.stringify({ok:false, reason:'no_button'}); const pressed = (btn.getAttribute('aria-pressed')||'').toLowerCase(); if(pressed==='true') return JSON.stringify({ok:true, already_liked:true}); btn.click(); return JSON.stringify({ok:true, clicked:true}); })()""")
    Parse the JSON:
      - ok:true, already_liked:true  → post was already liked, do nothing.
      - ok:true, clicked:true        → liked. Optionally screenshot to confirm
                                       the reaction bar shows the filled Like.
      - ok:false (no_button)         → the deterministic selector missed.
                                       Fallback ONCE: capture a screenshot, Read
                                       it, locate the post's Like button (the
                                       leftmost action under the post body, NOT
                                       a Like on any comment), and click_at_xy
                                       it. If still not found, skip the like.
    NEVER click Like on a comment or on a different post; only the main
    pre-selected post. NEVER un-like (the aria-pressed guard prevents toggling
    an already-liked post off). Record AUTO_LIKE = liked | already | skipped
    for your final summary, then continue to step 4 regardless of outcome.

4. Post the comment via the BROWSER BACKEND block: scroll to the comment
   editor, click it (click_at_xy on the contenteditable box), type_text the
   (possibly wrapped) text from step 3b, then click the Post/Comment submit
   button (click_at_xy). The contenteditable box is the trickiest element —
   after clicking, capture a screenshot and Read it to confirm the caret is in
   the editor before typing.

5. POST-SUBMIT VERIFICATION (mandatory). The harness has NO network-capture
   tool, and reading /voyager or socialActions traffic is a flagged pattern —
   verify visually + via the rendered DOM only.
   5a. Harvest URNs from the rendered DOM (NOT from network). Read every
       16-19 digit URN present on the page:
         bh_run js("""return JSON.stringify(Array.from(document.querySelectorAll('[data-id],[data-urn],[href]')).map(e=>e.getAttribute('data-id')||e.getAttribute('data-urn')||e.getAttribute('href')).join(' ').match(/urn:li:(?:activity|share|ugcPost|comment):[0-9]{16,19}/g)||[])""")
       Dedupe the result with the seed URN list above into ALL_POST_URNS
       (comma-separated). Set NETWORK_RESPONSE to a short DOM/toast summary
       string (there is no real network payload to capture).
   5b. Capture a screenshot (bh_run print(capture_screenshot())) and Read the PNG
       to check for a toast.
   5c. Read the DOM (bh_run js("""...""")) and check:
         (a) comment count went up by at least 1
         (b) a fresh comment by 'Matthew Diakonov' / 'You' is rendered
         (c) NO 'could not be created' toast
         (d) editor textbox cleared
   5d. SUCCESS = all four pass. REJECTED = toast present OR count unchanged.

   5e. ON SUCCESS ONLY — capture OUR comment's full comment URN so stats can
       later match it. The post-stats pipeline keys engagement on the numeric
       comment id embedded in our_url's commentUrn; without it our comment's
       impressions/reactions/replies can NEVER be matched (they stay frozen).
       Read every rendered comment node WITH its text:
         bh_run js("""return JSON.stringify(Array.from(document.querySelectorAll('[data-id^="urn:li:comment:"]')).map(n=>({id:n.getAttribute('data-id'),text:(n.innerText||'').replace(/\\s+/g,' ').slice(0,160)})))""")
       From that array pick the ONE entry whose text matches the comment YOU
       just posted (YOUR_COMMENT_TEXT, possibly link-wrapped). Take its 'id' —
       it is the full parenthesized comment URN, e.g.
         urn:li:comment:(activity:7468708028956016640,7468710512147460096)
       (the trailing number is OUR comment id). Store it verbatim as
       OUR_COMMENT_URN. If you cannot confidently identify our comment (no
       data-id match), set OUR_COMMENT_URN to empty and proceed — step 7 falls
       back to the bare thread URL.

6. If REJECTED, do NOT call the success log path. Mark candidate skipped:
     python3 -c "import sys; sys.path.insert(0,'$REPO_DIR/scripts'); from http_api import api_patch; api_patch('/api/v1/linkedin-candidates', {'activity_id': '$PA_ACTIVITY_ID', 'status': 'skipped'}, ok_on_404=True)"
   Then ledger the soft-block:
     python3 $REPO_DIR/scripts/log_post.py --rejected \\
       --platform linkedin \\
       --thread-url '$PA_URL' \\
       --our-content 'YOUR_COMMENT_TEXT' \\
       --project '$PA_PROJECT' \\
       --thread-author '$PA_AUTHOR_NAME' \\
       --thread-title '$PA_TITLE_HINT' \\
       --engagement-style STYLE_YOU_CHOSE \\
       --assigned-style '$PICKED_STYLE' \\
       --assigned-mode '$PICKED_MODE' \\
       --search-topic $PA_SEARCH_TOPIC_ARG \\
       --language '$PA_LANG' \\
       --rejection-reason 'TOAST: <verbatim toast text or quiet-fail>' \\
       --network-response 'NETWORK_RESPONSE'
   Then STOP with '## Comment soft-blocked, ledgered'.

7. If SUCCESS, log the post and mark candidate posted. First build OUR_URL so
   it carries our comment's commentUrn (REQUIRED for stats matching). Substitute
   the OUR_COMMENT_URN you captured in step 5e in place of the literal token
   below, then run:
     OUR_URL=\$(python3 -c "import urllib.parse,sys; cu=sys.argv[1].strip(); base=sys.argv[2]; print(base + '?commentUrn=' + urllib.parse.quote(cu, safe='') if cu.startswith('urn:li:comment:(') else base)" 'OUR_COMMENT_URN' '$PA_URL')
   If OUR_COMMENT_URN was empty, OUR_URL falls back to the bare thread URL.
     LOG_RESULT=\$(python3 $REPO_DIR/scripts/log_post.py \\
       --platform linkedin \\
       --thread-url '$PA_URL' \\
       --our-url "\$OUR_URL" \\
       --our-content 'YOUR_COMMENT_TEXT' \\
       --project '$PA_PROJECT' \\
       --thread-author '$PA_AUTHOR_NAME' \\
       --thread-title '$PA_TITLE_HINT' \\
       --engagement-style STYLE_YOU_CHOSE \\
       --assigned-style '$PICKED_STYLE' \\
       --assigned-mode '$PICKED_MODE' \\
       --search-topic $PA_SEARCH_TOPIC_ARG \\
       --language '$PA_LANG' \\
       --urns 'ALL_POST_URNS')
     echo "\$LOG_RESULT"
     python3 -c "import sys; sys.path.insert(0,'$REPO_DIR/scripts'); from http_api import api_patch; api_patch('/api/v1/linkedin-candidates', {'activity_id': '$PA_ACTIVITY_ID', 'status': 'posted'}, ok_on_404=True)"
     If MINTED_SESSION is non-empty: extract post_id from LOG_RESULT and backfill:
       LOG_POST_ID=\$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('post_id',''))" "\$LOG_RESULT" 2>/dev/null || echo "")
       [ -n "\$LOG_POST_ID" ] && python3 $REPO_DIR/scripts/dm_short_links.py backfill-post \\
         --minted-session "\$MINTED_SESSION" --post-id "\$LOG_POST_ID"

CRITICAL: ONE post only. If anything fails, STOP — do NOT pick another candidate.
CRITICAL: Use ONLY the browser tool described in the BROWSER BACKEND block
(mcp__linkedin-harness__bh_run).
CRITICAL: NEVER use em dashes.
PROMPT_EOF
fi

# --- DORMANT unipile branch: OFF by default (see header). Reached ONLY with an
# --- explicit LINKEDIN_BACKEND=unipile override, which still 503s until the
# --- UniPile account is manually reconnected. Presence here != in use.
if [ "$LINKEDIN_BACKEND" = "unipile" ]; then
  # UniPile Phase B: comment via REST, no headed browser. No linkedin-browser
  # lock, no ensure_browser_healthy, no harness MCP, no hook lockfile.
  # --strict-mcp-config with NO --mcp-config = Bash-only tool surface; the
  # agent shells out to linkedin_unipile.py comment/comments.
  set +e
  "$REPO_DIR/scripts/run_claude.sh" "run-linkedin-phaseB" --strict-mcp-config --output-format stream-json --verbose -p "$(cat "$PHASE_B_PROMPT")" 2>&1 | tee -a "$LOG_FILE"
  PB_RC=${PIPESTATUS[0]}
  set -e
  rm -f "$PHASE_B_PROMPT"
  rm -f "$PHASE_A_OUT"
else
# Re-acquire linkedin-browser for Phase B. The lock was released after
# Phase A so peer pipelines could use the browser during our DB-ingest /
# candidate-pick / styles-prep window (~1-3s). If a peer (or a parallel
# linkedin cycle's Phase A) grabbed it in the meantime, this acquire blocks
# until they release; the FIFO ticket queue in lock.sh guarantees fairness.
acquire_lock "linkedin-browser" 3600
ensure_linkedin_browser_for_backend 2>&1 | tee -a "$LOG_FILE"

set +e
"$REPO_DIR/scripts/run_claude.sh" "run-linkedin-phaseB" --strict-mcp-config --mcp-config "$MCP_CONFIG_FILE" --output-format stream-json --verbose -p "$(cat "$PHASE_B_PROMPT")" 2>&1 | tee -a "$LOG_FILE"
PB_RC=${PIPESTATUS[0]}
set -e

release_lock "linkedin-browser"
# Defense-in-depth: explicit hook-lockfile cleanup; see Phase A note.
rm -f "$HOME/.claude/linkedin-agent-lock.json"
rm -f "$PHASE_B_PROMPT"
rm -f "$PHASE_A_OUT"
fi

# ===== Persist run-level summary =====
# Same logic that used to live inline now lives in
# _sa_emit_run_summary_oneshot (defined near the top after sourcing lock.sh).
# Calling it directly here on the happy path; the EXIT trap will short-
# circuit afterwards via _SA_RUN_SUMMARY_EMITTED. Under SIGTERM mid-script,
# the trap fires this same function so the dashboard still gets a row.
_sa_emit_run_summary_oneshot

echo "=== Run complete: $(date) ===" | tee -a "$LOG_FILE"
find "$LOG_DIR" -name "run-linkedin-*.log" -mtime +7 -delete 2>/dev/null || true
