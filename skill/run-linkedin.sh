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
#   mcp__linkedin-agent, verify (network + DOM), log via log_post.py, mark
#   the candidate row 'posted' (or 'skipped' on rejection), STOP.
#
# Differences vs the pre-2026-04-29 shape:
#   - Phase A extracts ENGAGEMENT (not just URN); we don't fly blind anymore
#   - Phase A logs every search query (positive, zero, low-quality SERP) so
#     the LLM learns which phrasings work and which to retire
#   - Phase B reads its candidate from linkedin_candidates (DB-backed),
#     not from a file, so the same candidate isn't picked twice across runs

set -euo pipefail

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

# Idempotent run_monitor.log emitter wired into a chained EXIT/INT/TERM/HUP
# trap. Without this, SIGTERM landing between Phase B post (where Claude has
# already submitted the comment via the LinkedIn API and the row is in the
# `posts` table) and the inline summary write at the bottom of the script
# silently drops the run from run_monitor.log. Mirrors the same fix shipped
# to run-reddit-search.sh and run-twitter-cycle.sh.
#
# Reads counters from globals the cycle accumulates (RUN_START_EPOCH,
# PB_RC, LOG_FILE) and re-derives POSTED/SKIPPED/FAILED the same way the
# inline block does. All shell-outs are wrapped in `timeout 10` so a Neon
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
    if [ -n "${DATABASE_URL:-}" ]; then
        posted=$(timeout 10 psql "$DATABASE_URL" -t -A -c \
            "SELECT COUNT(*) FROM posts WHERE platform='linkedin' AND posted_at >= NOW() - interval '$window_sec seconds'" \
            2>/dev/null | tr -d '[:space:]' || true)
        [ -z "$posted" ] && posted=0
    fi
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
trap '_sa_emit_run_summary_oneshot; _sa_release_locks' EXIT INT TERM HUP

# ===== Phase A: discovery + scoring =====
PROJECT_DIST=$(python3 "$REPO_DIR/scripts/pick_project.py" --platform linkedin --distribution 2>/dev/null || echo "(distribution unavailable)")

# Slim project list: name + description + qualification + search_topics.
# Phase A needs search_topics so the LLM can draft per-project queries.
PROJECTS_SLIM_JSON=$(python3 -c "
import json, os
config = json.load(open(os.path.expanduser('~/social-autoposter/config.json')))
slim = {}
for p in config.get('projects', []):
    if 'linkedin' in (p.get('platforms_disabled') or []):
        continue
    rec = {k: p[k] for k in ('name','description','qualification') if k in p}
    rec['search_topics'] = p.get('search_topics') or []
    slim[p['name']] = rec
print(json.dumps(slim, indent=2))
" 2>/dev/null || echo "{}")

# Top-performing historical queries (positive signal, last 30d).
TOP_QUERIES=$(python3 "$REPO_DIR/scripts/top_linkedin_queries.py" --limit 15 --window-days 30 2>/dev/null || echo "[]")

# Dud queries to AVOID redrafting (zero-result OR low-SERP, last 7d).
DUD_QUERIES=$(python3 "$REPO_DIR/scripts/top_dud_linkedin_queries.py" --limit 30 --window-days 7 2>/dev/null || echo "[]")

# BSD mktemp on macOS only substitutes XXXXXX at the end of the template.
PHASE_A_OUT=$(mktemp /tmp/sa-run-linkedin-phaseA-XXXXXX)
PHASE_A_PROMPT=$(mktemp /tmp/sa-run-linkedin-phaseA-prompt-XXXXXX)

cat > "$PHASE_A_PROMPT" <<PROMPT_EOF
You are the Social Autoposter LinkedIn discovery + scoring scout (Phase A).

Your job: pick ONE project, draft 8 DYNAMIC search queries informed by
historical performance, browse each query's LinkedIn SERP, extract
engagement metrics for every visible candidate post, write a structured
JSON envelope to $PHASE_A_OUT, and STOP. Do NOT draft a comment. Do NOT
post anything. Phase B handles drafting + posting using whatever you write
to the candidates list.

## Project candidates (only LinkedIn-eligible projects shown)
$PROJECTS_SLIM_JSON

## Today's distribution (prefer underrepresented projects)
$PROJECT_DIST

## Top-performing historical queries (last 30 days, sorted by posts produced)
These are STYLE inspiration only — do NOT reuse them verbatim. LinkedIn
SERPs shift daily, so reusing the exact same phrasing is wasteful. Mine
them for the angle/keyword combo that worked, then craft something new.
$TOP_QUERIES

## DUD queries to AVOID (last 7 days, zero-result or low-SERP-quality)
Do NOT redraft any of these phrasings. They have been flat or
audience-wrong recently. Note the 'reason' field — 'zero_results' means
LinkedIn rejected the keywords; 'low_serp_quality' means results came
back but were influencer slop / off-target audience.
$DUD_QUERIES

## Workflow

1. Pick ONE underrepresented project from the distribution that has a
   plausible content fit. Do NOT iterate through many projects.

2. Draft 8 search queries for the chosen project. Each query should:
   - Be 2-4 words (LinkedIn search hates long phrases)
   - Target practitioners, not influencers (no "expert tips", "thought
     leadership", or buzzwordy phrasing)
   - Be FRESH — different from the dud list, different angle from the
     top-performers list (steal the recipe, change the dish)
   - Map to the project's search_topics
   - Cover DIFFERENT facets / pains / personas of the ICP — not 4 reskins
     of the same query. Wider net = higher chance of one ICP-fit hit.

   Run 8 queries this run. More surface area beats narrow targeting:
   most queries will return slop and get retired into the dud list, so the
   2-3 that survive should reach the LLM with real candidates. The
   LinkedIn rate budget (40/24h, 150/30d) accommodates this fine; rate
   caps are not the bottleneck, candidate quality is.

3. PRIME the linkedin-agent MCP browser ONCE before the per-query loop.
   The Playwright MCP server launches Chrome lazily on the first browser
   tool call; without this step the discover script tries to CDP-attach to
   a dead port and returns mcp_not_running.
   3pre. mcp__linkedin-agent__browser_navigate to https://www.linkedin.com/
         (one navigation; this brings Chrome up and writes a fresh port to
         DevToolsActivePort).
   3pre-check. If the resulting URL contains /uas/login or /checkpoint/,
         the persistent session is dead. Write an empty envelope (no
         queries_used, no candidates) and STOP. The user must re-auth the
         linkedin-agent profile interactively before the next run.

4. For EACH query, shell out via the Bash tool:

       SOCIAL_AUTOPOSTER_LINKEDIN_SEARCH=1 python3 \\
         $REPO_DIR/scripts/discover_linkedin_candidates.py content "<query>"

   The script CDP-attaches to the linkedin-agent MCP's already-running
   Chrome (same cookies/session/fingerprint, no second browser), navigates
   the SERP, extracts every visible card, and prints a JSON envelope to
   stdout. Do NOT call mcp__linkedin-agent__browser_navigate or
   browser_run_code for discovery — the script handles both.

   Result shape on success:

       {
         "ok": true,
         "url": "https://www.linkedin.com/search/results/content/?keywords=...",
         "vertical": "content",
         "query": "<query>",
         "result_count": N,
         "dropped_below_virality_floor": M,
         "virality_floor": 5.0,
         "results": [   // SORTED by velocity_score DESC — top of list = highest score
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

   result_count is the POST-floor card count (cards that survived the
   velocity floor). dropped_below_virality_floor is how many cards the
   SERP returned but the floor rejected — copy this into queries_used as
   dropped_below_floor (see envelope shape below). The dashboard reports
   raw SERP volume as candidates_found + dropped so the operator can tell
   "SERP returned nothing" apart from "SERP returned weak cards".

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
       via mcp__linkedin-agent__browser_click on the matching card.
       (Use the post_text first ~60 chars to disambiguate which card
       on the SERP is the winner.) Click on exactly one card per run.

   5b. After the navigation settles, read the resulting page URL via
       mcp__linkedin-agent__browser_evaluate(() => location.href).
       Match /urn:li:(activity|share|ugcPost):(\\d{16,19})/ — capture
       BOTH the URN type (activity / share / ugcPost) and the numeric.

       CRITICAL: activity / share / ugcPost URNs are DIFFERENT namespaces.
       The same numeric ID resolves to different posts (or to nothing) in
       different namespaces. You MUST preserve the type when building the
       canonical URL — never collapse share/ugcPost to activity.

         post_url = https://www.linkedin.com/feed/update/urn:li:<TYPE>:<NUM>/
         activity_id = <NUM>            (bare numeric, for engaged-id check)

       If your click in 5a did NOT navigate (page still shows the SERP
       URL), fall back to the 3-dot menu → "Copy link to post" route:
         - browser_click on the 3-dot control menu of the winner card
         - browser_click on the "Copy link to post" menu item
         - read the URL from clipboard via browser_evaluate +
           navigator.clipboard.readText() (may fail with permission denied
           in headed Chrome — try Bash 'pbpaste' as a backup)
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
  "project": "PROJECT_NAME",
  "language": "en",
  "queries_used": [
    {"query": "ai agents production",   "candidates_found": 4, "serp_quality_score": 7.5, "dropped_below_floor": 2},
    {"query": "macos automation tools", "candidates_found": 0, "serp_quality_score": null, "dropped_below_floor": 0},
    {"query": "claude code workflow",   "candidates_found": 6, "serp_quality_score": 5.0, "dropped_below_floor": 9}
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
   - candidates_found is the POST-floor count (cards that survived the
     velocity floor — same as the discover script's result_count).
     dropped_below_floor is the per-query count of cards the SERP returned
     but the floor rejected — copy it from the discover script's
     dropped_below_virality_floor field. Use 0 when the discover script
     didn't report one (zero-result, error, or non-content vertical). The
     dashboard surfaces raw SERP volume as candidates_found + dropped, so
     getting this right is what tells "SERP returned nothing" apart from
     "SERP returned 30 weak cards that all scored under the floor".
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

CRITICAL: Use ONLY mcp__linkedin-agent__* tools. NEVER click the comment
textbox. NEVER call createComment. NEVER navigate to a post-compose flow.
Phase B does all of that.
CRITICAL: Run exactly 8 search queries this run. Not 2, not 4, not 6. Eight.
Wider net = better odds of one ICP-fit hit. The rate budget can absorb it.
CRITICAL: NEVER use em dashes anywhere.
PROMPT_EOF

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
ensure_browser_healthy "linkedin"

set +e
"$REPO_DIR/scripts/run_claude.sh" "run-linkedin-phaseA" --strict-mcp-config --mcp-config "$HOME/.claude/browser-agent-configs/linkedin-agent-mcp.json" --output-format stream-json --verbose -p "$(cat "$PHASE_A_PROMPT")" 2>&1 | tee -a "$LOG_FILE"
PA_RC=${PIPESTATUS[0]}
set -e

release_lock "linkedin-browser"
# Defense-in-depth: explicitly clear the hook-layer lockfile so the next
# pipeline cycle's PreToolUse never sees a stale entry from us. The
# run_claude.sh exit trap already does this in the happy path; this
# repeat is harmless and covers SIGKILL of run_claude.sh.
rm -f "$HOME/.claude/linkedin-agent-lock.json"
rm -f "$PHASE_A_PROMPT"

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

# Ingest queries_used into linkedin_search_attempts (one row per query, dud-aware).
python3 -c "
import json
env = json.load(open('$PHASE_A_OUT'))
project = env.get('project','')
out = []
for q in env.get('queries_used') or []:
    out.append({
        'query': q.get('query',''),
        'project': project,
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
python3 -c "
import json
env = json.load(open('$PHASE_A_OUT'))
quality_by_query = {q.get('query',''): q.get('serp_quality_score') for q in env.get('queries_used') or []}
project = env.get('project','')
lang = env.get('language','en')
cands = []
for c in env.get('candidates') or []:
    if not isinstance(c, dict):
        continue
    c.setdefault('matched_project', project)
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
# fall back to the broader pending pool — that pool would otherwise just
# expire after MAX_AGE_HOURS without ever being attempted.
PA_PICK=$(python3 -c "
import json, sys
sys.path.insert(0, '$REPO_DIR/scripts')
import db as dbmod
conn = dbmod.get_conn()
row = conn.execute('''
    SELECT post_url, activity_id, all_urns, author_name, author_profile_url,
           post_text, language, matched_project, velocity_score, search_query
    FROM linkedin_candidates
    WHERE status='pending' AND batch_id=%s
    ORDER BY velocity_score DESC NULLS LAST, discovered_at DESC
    LIMIT 1
''', ['$BATCH_ID']).fetchone()
if not row:
    row = conn.execute('''
        SELECT post_url, activity_id, all_urns, author_name, author_profile_url,
               post_text, language, matched_project, velocity_score, search_query
        FROM linkedin_candidates
        WHERE status='pending' AND age_hours <= 96
        ORDER BY velocity_score DESC NULLS LAST, discovered_at DESC
        LIMIT 1
    ''').fetchone()
conn.close()
if not row:
    print(json.dumps({}))
else:
    out = {
        'post_url': row[0],
        'activity_id': row[1],
        'all_urns': row[2] or '',
        'author_name': row[3] or '',
        'author_profile_url': row[4] or '',
        'post_text': (row[5] or ''),
        'language': row[6] or 'en',
        'project': row[7] or '$PA_PROJECT',
        'velocity_score': float(row[8] or 0),
        'search_query': row[9] or '',
    }
    print(json.dumps(out))
" 2>/dev/null || echo "{}")

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
[ -z "${PA_PROJECT:-}" ] && PA_PROJECT=$(echo "$PA_PICK" | python3 -c "import json,sys; print(json.load(sys.stdin).get('project',''))")

# ===== If no candidate, exit cleanly =====
# Path D: Phase A's LLM is responsible for clicking-into-best to capture the
# URN, so every row reaching this gate must already have a numeric URN.
if [ -z "$PA_ACTIVITY_ID" ] || [ -z "$PA_URL" ]; then
  echo "Phase A: no postable candidate after scoring (project='$PA_PROJECT'). Skipping Phase B." | tee -a "$LOG_FILE"
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

echo "Phase A: chose project=$PA_PROJECT activity=$PA_ACTIVITY_ID velocity=$PA_VELOCITY query='$PA_QUERY'" | tee -a "$LOG_FILE"

# Look up the chosen project's full config (only this one).
PROJECT_FULL=$(python3 -c "
import json, os
c = json.load(open(os.path.expanduser('~/social-autoposter/config.json')))
p = next((p for p in c.get('projects',[]) if p['name']=='$PA_PROJECT'), {})
print(json.dumps(p, indent=2))
")

# Phase B inputs (only Phase B needs styles + top performers).
TOP_REPORT=$(python3 "$REPO_DIR/scripts/top_performers.py" --platform linkedin 2>/dev/null || echo "(top performers report unavailable)")
source "$REPO_DIR/skill/styles.sh"
STYLES_BLOCK=$(generate_styles_block linkedin posting)

# Prior-interactions context: surface our last 5 comments on threads by the
# same author in the past 30 days (soft context — vary angle, don't repeat).
# Empty when we have no history with this person. Failure is silent.
AUTHOR_HISTORY_BLOCK=""
if [ -n "${PA_AUTHOR_NAME:-}" ]; then
    AUTHOR_HISTORY_BLOCK=$(python3 "$REPO_DIR/scripts/author_history_block.py" --platform linkedin --author "$PA_AUTHOR_NAME" --days 30 --limit 5 2>/dev/null || true)
fi

# Allow Chrome's profile lockfile to release between phases.
sleep 3

# ===== Phase B: compose + post + verify + log =====
PHASE_B_PROMPT=$(mktemp /tmp/sa-run-linkedin-phaseB-prompt-XXXXXX)
cat > "$PHASE_B_PROMPT" <<PROMPT_EOF
You are the Social Autoposter (Phase B). Your job: post ONE comment on a
pre-selected LinkedIn post (already chosen + scored by Phase A), verify it
landed, log it. STOP. Do NOT search for other candidates.

Read $SKILL_FILE for tone and content rules.

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
- Search query that surfaced it: '$PA_QUERY'

$AUTHOR_HISTORY_BLOCK

## Project config
$PROJECT_FULL

## Top performers feedback (use to pick a comment angle)
$TOP_REPORT

$STYLES_BLOCK

## Workflow

1. Navigate to $PA_URL via mcp__linkedin-agent__browser_navigate.

   1a. URN-NAMESPACE FALLBACK. After navigation, take a browser_snapshot.
       If the snapshot contains the markers 'Post not found' OR 'This post
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
           (skip whichever you already tried). browser_navigate to each;
           after each, browser_snapshot; if the post-not-found markers are
           absent AND a comment editor / post body renders, that URL is
           the correct one — adopt it and continue from step 2.
         * If ALL THREE namespaces hit post-not-found markers, the post
           genuinely no longer exists. Mark candidate skipped:
             python3 -c "import sys; sys.path.insert(0,'$REPO_DIR/scripts'); import db; c=db.get_conn(); c.execute(\"UPDATE linkedin_candidates SET status='skipped' WHERE activity_id=%s\", ['$PA_ACTIVITY_ID']); c.commit(); c.close()"
           Update the run-level counter signal: print a line containing
           the literal token 'PHASE_B_SKIP_POST_UNAVAILABLE' so the wrapper
           can attribute it. Then STOP with '## Post unavailable, candidate skipped'.

   1b. If you found a working namespace different from $PA_URL, persist it
       so future navigations / engaged-id checks use the right canonical:
         python3 -c "import sys; sys.path.insert(0,'$REPO_DIR/scripts'); import db; c=db.get_conn(); c.execute(\"UPDATE linkedin_candidates SET post_url=%s WHERE activity_id=%s\", ['<WORKING_URL>','$PA_ACTIVITY_ID']); c.commit(); c.close()"

2. Defensive engaged-id re-check (Phase A may have missed a URN that only
   surfaces after the post page fully loads). Walk the rendered DOM for ALL
   URNs (activity, share, ugcPost forms), merge with '$PA_ALL_URNS', and run:
     python3 $REPO_DIR/scripts/linkedin_url.py --check-engaged-ids 'MERGED_URNS'
   If exit code 0 (already engaged), mark the candidate skipped:
     python3 -c "import sys; sys.path.insert(0,'$REPO_DIR/scripts'); import db; c=db.get_conn(); c.execute(\"UPDATE linkedin_candidates SET status='skipped' WHERE activity_id=%s\", ['$PA_ACTIVITY_ID']); c.commit(); c.close()"
   then STOP with '## Already engaged (defensive catch in Phase B)'.

3. Pick the engagement style that best fits the post + project's voice
   block (apply voice.tone, never violate voice.never, mirror voice.examples
   if present). Reply in $PA_LANG.
   NEVER use em dashes.

3b. Wrap any URLs in your draft before typing. Run:
     WRAP_RESULT=\$(python3 $REPO_DIR/scripts/dm_short_links.py wrap-post-text \\
       --text "YOUR_COMMENT_TEXT" --platform linkedin --project '$PA_PROJECT')
   If wrap_result.ok is true: use wrap_result.text as the final comment text
   and save wrap_result.minted_session as MINTED_SESSION. Otherwise use the
   original draft and set MINTED_SESSION to empty.

4. Post the comment via mcp__linkedin-agent (find textbox, click, type, submit)
   using the (possibly wrapped) text from step 3b.

5. POST-SUBMIT VERIFICATION (mandatory).
   5a. mcp__linkedin-agent__browser_network_requests with:
         filter: 'normCommentsCreate|normComments|contentcreation|socialActions'
         requestBody: true
         static: false
       Save response verbatim as NETWORK_RESPONSE.
   5b. Walk NETWORK_RESPONSE for every 16-19 digit URN, dedupe with the
       seed URN list above into ALL_POST_URNS (comma-separated).
   5c. mcp__linkedin-agent__browser_take_screenshot for the toast.
   5d. mcp__linkedin-agent__browser_snapshot (depth 12). Check:
         (a) comment count went up by at least 1
         (b) a fresh comment by 'Matthew Diakonov' / 'You' is rendered
         (c) NO 'could not be created' toast
         (d) editor textbox cleared
   5e. SUCCESS = all four pass. REJECTED = toast present OR count unchanged.

6. If REJECTED, do NOT call the success log path. Mark candidate skipped:
     python3 -c "import sys; sys.path.insert(0,'$REPO_DIR/scripts'); import db; c=db.get_conn(); c.execute(\"UPDATE linkedin_candidates SET status='skipped' WHERE activity_id=%s\", ['$PA_ACTIVITY_ID']); c.commit(); c.close()"
   Then ledger the soft-block:
     python3 $REPO_DIR/scripts/log_post.py --rejected \\
       --platform linkedin \\
       --thread-url '$PA_URL' \\
       --our-content 'YOUR_COMMENT_TEXT' \\
       --project '$PA_PROJECT' \\
       --thread-author '$PA_AUTHOR_NAME' \\
       --thread-title '$PA_TITLE_HINT' \\
       --engagement-style STYLE_YOU_CHOSE \\
       --language '$PA_LANG' \\
       --rejection-reason 'TOAST: <verbatim toast text or quiet-fail>' \\
       --network-response 'NETWORK_RESPONSE'
   Then STOP with '## Comment soft-blocked, ledgered'.

7. If SUCCESS, log the post and mark candidate posted:
     LOG_RESULT=\$(python3 $REPO_DIR/scripts/log_post.py \\
       --platform linkedin \\
       --thread-url '$PA_URL' \\
       --our-url '$PA_URL' \\
       --our-content 'YOUR_COMMENT_TEXT' \\
       --project '$PA_PROJECT' \\
       --thread-author '$PA_AUTHOR_NAME' \\
       --thread-title '$PA_TITLE_HINT' \\
       --engagement-style STYLE_YOU_CHOSE \\
       --language '$PA_LANG' \\
       --urns 'ALL_POST_URNS')
     echo "\$LOG_RESULT"
     python3 -c "import sys; sys.path.insert(0,'$REPO_DIR/scripts'); import db; c=db.get_conn(); c.execute(\"UPDATE linkedin_candidates SET status='posted', posted_at=NOW() WHERE activity_id=%s\", ['$PA_ACTIVITY_ID']); c.commit(); c.close()"
     If MINTED_SESSION is non-empty: extract post_id from LOG_RESULT and backfill:
       LOG_POST_ID=\$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('post_id',''))" "\$LOG_RESULT" 2>/dev/null || echo "")
       [ -n "\$LOG_POST_ID" ] && python3 $REPO_DIR/scripts/dm_short_links.py backfill-post \\
         --minted-session "\$MINTED_SESSION" --post-id "\$LOG_POST_ID"

CRITICAL: ONE post only. If anything fails, STOP — do NOT pick another candidate.
CRITICAL: Use ONLY mcp__linkedin-agent__* tools.
CRITICAL: NEVER use em dashes.
PROMPT_EOF

# Re-acquire linkedin-browser for Phase B. The lock was released after
# Phase A so peer pipelines could use the browser during our DB-ingest /
# candidate-pick / styles-prep window (~1-3s). If a peer (or a parallel
# linkedin cycle's Phase A) grabbed it in the meantime, this acquire blocks
# until they release; the FIFO ticket queue in lock.sh guarantees fairness.
acquire_lock "linkedin-browser" 3600
ensure_browser_healthy "linkedin"

set +e
"$REPO_DIR/scripts/run_claude.sh" "run-linkedin-phaseB" --strict-mcp-config --mcp-config "$HOME/.claude/browser-agent-configs/linkedin-agent-mcp.json" --output-format stream-json --verbose -p "$(cat "$PHASE_B_PROMPT")" 2>&1 | tee -a "$LOG_FILE"
PB_RC=${PIPESTATUS[0]}
set -e

release_lock "linkedin-browser"
# Defense-in-depth: explicit hook-lockfile cleanup; see Phase A note.
rm -f "$HOME/.claude/linkedin-agent-lock.json"
rm -f "$PHASE_B_PROMPT"
rm -f "$PHASE_A_OUT"

# ===== Persist run-level summary =====
# Same logic that used to live inline now lives in
# _sa_emit_run_summary_oneshot (defined near the top after sourcing lock.sh).
# Calling it directly here on the happy path; the EXIT trap will short-
# circuit afterwards via _SA_RUN_SUMMARY_EMITTED. Under SIGTERM mid-script,
# the trap fires this same function so the dashboard still gets a row.
_sa_emit_run_summary_oneshot

echo "=== Run complete: $(date) ===" | tee -a "$LOG_FILE"
find "$LOG_DIR" -name "run-linkedin-*.log" -mtime +7 -delete 2>/dev/null || true
