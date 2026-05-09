#!/usr/bin/env python3
"""Reddit posting orchestrator.

Spawns a Claude session per post that uses reddit_tools.py (search, fetch) to find
threads and drafts replies. Python orchestrator handles CDP posting and DB logging.

Usage:
    python3 scripts/post_reddit.py
    python3 scripts/post_reddit.py --dry-run          # Print prompt without executing
    python3 scripts/post_reddit.py --limit 3           # Post at most 3 comments
    python3 scripts/post_reddit.py --timeout 3600      # Global timeout in seconds
    python3 scripts/post_reddit.py --project Cyrano    # Override project selection
"""

import argparse
import errno
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod

REPO_DIR = os.path.expanduser("~/social-autoposter")
CONFIG_PATH = os.path.join(REPO_DIR, "config.json")
REDDIT_BROWSER = os.path.join(REPO_DIR, "scripts", "reddit_browser.py")
REDDIT_TOOLS = os.path.join(REPO_DIR, "scripts", "reddit_tools.py")
RATELIMIT_FILE = "/tmp/reddit_ratelimit.json"
PREFLIGHT_WAIT_BUDGET_SECONDS = 180

# ---------------------------------------------------------------------------
# reddit_candidates queue parameters (mirrors twitter_candidates intent).
#
# 2026-05-06: persistent queue replaces the ephemeral tmpfile-only flow so
# transient post failures (CDP timeout, comment_box_not_found, browser crash)
# get retried on the next cycle's Phase 0 salvage rather than losing the
# discover+ripen+draft cost as wholesale waste. Permanent failures
# (thread_locked at submit time, archived, deleted, account_blocked) get
# marked status='failed' so we never re-evaluate them.
#
# Window choices:
#   FRESHNESS_HOURS=24    Reddit threads stay actionable longer than tweets
#                          (FRESHNESS_HOURS=6 on Twitter), so the hard-expire
#                          cutoff is wider. Past 24h the comment is unlikely
#                          to be seen.
#   MAX_ATTEMPTS=3         Cap retry budget so a chronically-broken thread
#                          (subreddit gone private mid-cycle, AutoMod glitch)
#                          drops out instead of recurring forever.
#   RETRY_BACKOFF_MIN=30   Don't re-attempt a freshly-failed candidate within
#                          the same 15-min cycle; let the failure reason
#                          stabilize before retrying.
#   DRAFT_TTL_MIN=60       A salvaged candidate whose draft was written < 60
#                          min ago re-uses it as-is (skips LLM redraft). Keeps
#                          us from paying $0.20-$0.40 of Claude cost twice on
#                          the same comment when the post step retries.
FRESHNESS_HOURS = 24
MAX_ATTEMPTS = 3
RETRY_BACKOFF_MIN = 30
DRAFT_TTL_MIN = 60

# Discover-phase search budget. Was hardcoded as "AT MOST 2 searches" inline
# in build_discover_prompt; bumped to 10 (2026-05-08) so each cycle gets a
# wider top-of-funnel and the new draft-gate-omit feedback report can steer
# rephrasings without starving the next attempt of fresh angles. Override via
# SAPS_REDDIT_MAX_SEARCHES env var without code change.
MAX_DISCOVER_SEARCHES = int(os.environ.get("SAPS_REDDIT_MAX_SEARCHES", "3"))

# CDP-error → permanence map. Permanent failures mark status='failed' and are
# never re-evaluated. Transient failures stay status='pending' with
# attempt_count++; Phase 0 salvages them on the next cycle.
_PERMANENT_CDP_ERRORS = {
    "thread_locked",
    "thread_archived",
    "thread_not_found",
    "account_blocked_in_sub",
    "no_permalink",  # we couldn't verify the post landed; retrying would dupe
}
_TRANSIENT_CDP_ERRORS = {
    "all_attempts_failed",
    "comment_box_not_found",
    "not_logged_in",
}

from engagement_styles import VALID_STYLES, get_styles_prompt, get_content_rules, validate_or_register


# ---------------------------------------------------------------------------
# reddit_candidates helpers.
#
# All DB-touching helpers swallow exceptions and log to stderr. The pipeline
# remains functional even if the queue table is unreachable; we just lose the
# salvage benefit for that cycle. This matches the cautious posture of
# log_post / campaign_bump / log_draft elsewhere in the file.

def _subreddit_from_url(thread_url):
    """Pull the bare subreddit name out of a Reddit thread URL, or None."""
    if not thread_url:
        return None
    m = re.search(r"/r/([^/]+)/", thread_url)
    return m.group(1).lower() if m else None


def _db_upsert_discovered_candidate(candidate, batch_id, project_name):
    """INSERT a freshly-discovered candidate row.

    Called by _discover_iteration after Claude returns. The candidate dict
    carries `score` and `num_comments` from the search response — those become
    score_t0/comments_t0 (T0 captured at discover time, no extra HTTP call,
    mirrors twitter_candidates likes_t0 capture in score_twitter_candidates).

    ON CONFLICT keeps the existing row's status, attempt_count, post linkage,
    AND original T0 intact, so:
      - a row that's already 'posted' or 'failed' isn't reset to 'pending'
      - a re-discovered row keeps the FIRST-SIGHTING T0 (cumulative delta
        works automatically — same trick Twitter uses for salvage)
    batch_id is updated to the current cycle so the dashboard's queue counts
    surface this run.
    """
    thread_url = (candidate.get("thread_url") or "").strip()
    if not thread_url:
        return
    try:
        dbmod.load_env()
        conn = dbmod.get_conn()
        conn.execute(
            "INSERT INTO reddit_candidates "
            "(thread_url, thread_author, thread_title, thread_selftext, subreddit, "
            " matched_project, search_topic, status, batch_id, "
            " draft_engagement_style, score_t0, comments_t0) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s) "
            "ON CONFLICT (thread_url) DO UPDATE SET "
            "  batch_id         = EXCLUDED.batch_id, "
            "  matched_project  = COALESCE(reddit_candidates.matched_project, EXCLUDED.matched_project), "
            "  search_topic     = COALESCE(reddit_candidates.search_topic, EXCLUDED.search_topic), "
            "  thread_title     = COALESCE(reddit_candidates.thread_title, EXCLUDED.thread_title), "
            "  thread_author    = COALESCE(reddit_candidates.thread_author, EXCLUDED.thread_author), "
            "  thread_selftext  = COALESCE(reddit_candidates.thread_selftext, EXCLUDED.thread_selftext), "
            "  subreddit        = COALESCE(reddit_candidates.subreddit, EXCLUDED.subreddit) "
            # Critical: do NOT touch status, attempt_count, post_id, posted_at,
            # or score_t0/comments_t0. Re-discovered rows that previously hit
            # a permanent failure should stay 'failed'; ones that already
            # posted should stay 'posted'; first-sighting T0 must persist so
            # cumulative delta keeps working across cycles.
            #
            # thread_selftext (added 2026-05-08): preserve OP body text for
            # downstream analytics (relevance scoring, content classification,
            # second-pass LLM review). Per user instruction, every candidate's
            # content + engagement stats are retained forever — we want the
            # full corpus to power future selection logic, not just status flags.
            ,
            [
                thread_url,
                candidate.get("thread_author"),
                candidate.get("thread_title"),
                candidate.get("selftext") or candidate.get("thread_selftext"),
                _subreddit_from_url(thread_url),
                project_name,
                candidate.get("search_topic"),
                batch_id,
                candidate.get("engagement_style"),
                # score / num_comments come from the Reddit search response;
                # tolerate either name and missing values (live T0 fallback).
                int(candidate["score"]) if candidate.get("score") is not None else None,
                int(candidate["num_comments"]) if candidate.get("num_comments") is not None else None,
            ],
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[post_reddit] WARNING: upsert candidate failed for {thread_url}: {e}",
              file=sys.stderr)


def _db_save_draft(thread_url, text, engagement_style):
    """Persist a freshly-written draft so a later salvage reuses it."""
    if not thread_url or not text:
        return
    try:
        dbmod.load_env()
        conn = dbmod.get_conn()
        conn.execute(
            "UPDATE reddit_candidates SET "
            "  draft_text = %s, "
            "  draft_engagement_style = %s, "
            "  drafted_at = NOW() "
            "WHERE thread_url = %s AND status = 'pending'",
            [text, engagement_style, thread_url],
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[post_reddit] WARNING: save_draft failed for {thread_url}: {e}",
              file=sys.stderr)


def _db_load_fresh_draft(thread_url):
    """Return (text, style) for a still-fresh draft, or (None, None)."""
    if not thread_url:
        return None, None
    try:
        dbmod.load_env()
        conn = dbmod.get_conn()
        cur = conn.execute(
            "SELECT draft_text, draft_engagement_style "
            "FROM reddit_candidates "
            "WHERE thread_url = %s "
            "  AND draft_text IS NOT NULL "
            "  AND drafted_at > NOW() - INTERVAL '%s minutes'",
            [thread_url, DRAFT_TTL_MIN],
        )
        row = cur.fetchone()
        conn.close()
        if row:
            return row[0], row[1]
    except Exception as e:
        print(f"[post_reddit] WARNING: load_fresh_draft failed for {thread_url}: {e}",
              file=sys.stderr)
    return None, None


def _db_mark_candidate_posted(thread_url, post_id):
    """Mark a candidate as successfully posted with linkage to posts.id."""
    if not thread_url:
        return
    try:
        dbmod.load_env()
        conn = dbmod.get_conn()
        conn.execute(
            "UPDATE reddit_candidates SET "
            "  status = 'posted', "
            "  post_id = %s, "
            "  posted_at = NOW(), "
            "  last_attempt_at = NOW() "
            "WHERE thread_url = %s",
            [post_id, thread_url],
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[post_reddit] WARNING: mark_posted failed for {thread_url}: {e}",
              file=sys.stderr)


def _db_mark_candidate_attempt(thread_url, reason, permanent=False):
    """Record a failed post attempt.

    Permanent failures jump straight to status='failed' (Phase 0 salvage skips
    these). Transient failures keep status='pending' with attempt_count++; if
    the bump puts attempt_count >= MAX_ATTEMPTS the row is auto-promoted to
    'failed' so we don't keep salvaging it forever.
    """
    if not thread_url:
        return
    try:
        dbmod.load_env()
        conn = dbmod.get_conn()
        if permanent:
            conn.execute(
                "UPDATE reddit_candidates SET "
                "  status = 'failed', "
                "  attempt_count = attempt_count + 1, "
                "  last_attempt_at = NOW(), "
                "  last_failure_reason = %s "
                "WHERE thread_url = %s",
                [reason, thread_url],
            )
        else:
            conn.execute(
                "UPDATE reddit_candidates SET "
                "  attempt_count = attempt_count + 1, "
                "  last_attempt_at = NOW(), "
                "  last_failure_reason = %s, "
                "  status = CASE "
                "    WHEN attempt_count + 1 >= %s THEN 'failed' "
                "    ELSE status END "
                "WHERE thread_url = %s",
                [reason, MAX_ATTEMPTS, thread_url],
            )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[post_reddit] WARNING: mark_attempt failed for {thread_url}: {e}",
              file=sys.stderr)


def _db_phase0_salvage(batch_id, freshness_hours=FRESHNESS_HOURS,
                       max_attempts=MAX_ATTEMPTS,
                       retry_backoff_min=RETRY_BACKOFF_MIN):
    """Phase 0: hard-expire stale rows + re-assign salvageable ones to this batch.

    Returns (expired_count, salvaged_count). Mirrors run-twitter-cycle.sh's
    Phase 0 SQL but with Reddit-tuned windows. We use a Python advisory-lock
    int distinct from Twitter's 7472346 (we pick 7472347) so concurrent
    Twitter+Reddit cycles don't block each other on the same lock.
    """
    try:
        dbmod.load_env()
        conn = dbmod.get_conn()
        # Single round-trip: combine the lock acquisition, expire, and salvage
        # into one transaction so a crash mid-Phase-0 doesn't half-update.
        cur = conn.execute(
            "WITH _lock AS (SELECT pg_advisory_xact_lock(7472347)), "
            "expired AS ( "
            "    UPDATE reddit_candidates "
            "    SET status='expired' "
            "    WHERE status='pending' "
            "      AND discovered_at < NOW() - INTERVAL '%s hours' "
            "    RETURNING id "
            "), salvaged AS ( "
            "    UPDATE reddit_candidates "
            "    SET batch_id = %s "
            "    WHERE status='pending' "
            "      AND attempt_count < %s "
            "      AND batch_id IS DISTINCT FROM %s "
            "      AND discovered_at >= NOW() - INTERVAL '%s hours' "
            "      AND (last_attempt_at IS NULL "
            "           OR last_attempt_at < NOW() - INTERVAL '%s minutes') "
            "    RETURNING id "
            ") "
            "SELECT (SELECT COUNT(*) FROM expired), (SELECT COUNT(*) FROM salvaged)",
            [freshness_hours, batch_id, max_attempts, batch_id,
             freshness_hours, retry_backoff_min],
        )
        row = cur.fetchone()
        conn.commit()
        conn.close()
        if row:
            return int(row[0] or 0), int(row[1] or 0)
    except Exception as e:
        print(f"[post_reddit] WARNING: phase0 salvage failed: {e}",
              file=sys.stderr)
    return 0, 0


def _db_pick_salvage_candidates(batch_id, limit=1):
    """Pull up to `limit` salvage-eligible rows from a SINGLE project.

    Phase 0 already re-assigned salvageable rows to `batch_id`. This helper
    picks the project with the most pending rows in this batch, then returns
    up to `limit` rows from THAT project (highest delta_score first, falling
    back to most recently discovered).

    Single-project batching is intentional: draft phase builds the Claude
    prompt around plan["project_name"], and post phase logs each comment's
    posts.project from the same field. Mixing projects in one salvage batch
    would either misattribute posts or require a per-decision project_name
    refactor across draft/post. The single-project rule keeps the existing
    contract intact while letting one ripen+draft+post cycle handle N rows
    instead of 1.

    Returns a {project_name, decisions:[{...}, ...], cost:0, salvaged:True}
    dict, or None if no eligible row remains.
    """
    limit = max(1, int(limit or 1))
    try:
        dbmod.load_env()
        conn = dbmod.get_conn()
        # Step 1: pick the project with the most pending rows in this batch.
        # Tie-break by max delta_score so the project with the strongest
        # remaining momentum signal wins.
        cur = conn.execute(
            "SELECT matched_project, COUNT(*) AS c "
            "FROM reddit_candidates "
            "WHERE batch_id = %s "
            "  AND status = 'pending' "
            "  AND attempt_count < %s "
            "GROUP BY matched_project "
            "ORDER BY c DESC, MAX(COALESCE(delta_score, 0)) DESC "
            "LIMIT 1",
            [batch_id, MAX_ATTEMPTS],
        )
        proj_row = cur.fetchone()
        if not proj_row:
            conn.close()
            return None
        project_name = proj_row[0] or "general"

        # Step 2: pull up to `limit` rows from that project, highest-momentum
        # first. The CASE on draft_text enforces draft_ttl_min freshness at
        # the SQL level so stale drafts don't ride into post (mirrors the
        # original single-row implementation).
        cur = conn.execute(
            "SELECT thread_url, thread_author, thread_title, subreddit, "
            "       matched_project, search_topic, "
            "       CASE WHEN drafted_at > NOW() - INTERVAL '%s minutes' "
            "            THEN draft_text ELSE NULL END AS fresh_draft, "
            "       draft_engagement_style, attempt_count "
            "FROM reddit_candidates "
            "WHERE batch_id = %s "
            "  AND status = 'pending' "
            "  AND attempt_count < %s "
            "  AND matched_project = %s "
            "ORDER BY COALESCE(delta_score, 0) DESC, discovered_at DESC "
            "LIMIT %s",
            [DRAFT_TTL_MIN, batch_id, MAX_ATTEMPTS, project_name, limit],
        )
        rows = cur.fetchall()
        conn.close()
        if not rows:
            return None
        decisions = []
        max_attempt = 0
        for row in rows:
            decision = {
                "action": "candidate",
                "thread_url": row[0],
                "thread_author": row[1] or "",
                "thread_title": row[2] or "",
                "search_topic": row[5] or "",
                "engagement_style": row[7] or "",
            }
            if row[6]:
                decision["draft_text"] = row[6]
            decisions.append(decision)
            attempt = int(row[8] or 0)
            if attempt > max_attempt:
                max_attempt = attempt
        return {
            "project_name": project_name,
            "decisions": decisions,
            "cost": 0.0,
            "salvaged": True,
            "salvaged_attempt": max_attempt + 1,
            "salvaged_count": len(decisions),
        }
    except Exception as e:
        print(f"[post_reddit] WARNING: pick_salvage_candidates failed: {e}",
              file=sys.stderr)
        return None


# Back-compat shim: older callers (and tests) may still call the singular
# name. Routes through the multi-row helper with limit=1 so we don't keep
# two SQL paths in sync.
def _db_pick_salvage_candidate(batch_id):
    return _db_pick_salvage_candidates(batch_id, limit=1)


def _apply_rate_limit_policy(remaining, reset_seconds, source, budget_seconds):
    """Given current quota, decide: proceed (True), wait then proceed, or skip (False)."""
    if remaining > 2 or reset_seconds <= 0:
        return True
    if reset_seconds > budget_seconds:
        print(f"[post_reddit] Reddit rate-limited ({source}), reset in "
              f"{int(reset_seconds)}s (> {budget_seconds}s budget). Skipping run.")
        return False
    wait = int(reset_seconds) + 3
    print(f"[post_reddit] Reddit rate-limited ({source}), waiting {wait}s "
          f"for reset before spawning Claude...")
    time.sleep(wait)
    return True


def _probe_reddit_quota():
    """One cheap request to Reddit to learn the live quota.

    Updates RATELIMIT_FILE so downstream reddit_tools.py calls share the
    fresh state. Returns (remaining, reset_seconds) or None on network error.
    """
    import urllib.request
    import urllib.error
    url = "https://old.reddit.com/r/popular.json?limit=1"
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        remaining = float(resp.headers.get("X-Ratelimit-Remaining", 100))
        reset = float(resp.headers.get("X-Ratelimit-Reset", 0))
        with open(RATELIMIT_FILE, "w") as f:
            json.dump({"remaining": remaining, "reset_at": time.time() + reset}, f)
        return remaining, reset
    except urllib.error.HTTPError as e:
        if e.code == 429:
            reset = float(e.headers.get("X-Ratelimit-Reset", 60))
            with open(RATELIMIT_FILE, "w") as f:
                json.dump({"remaining": 0, "reset_at": time.time() + reset}, f)
            return 0.0, reset
        return None
    except Exception:
        return None


def preflight_rate_limit(budget_seconds=PREFLIGHT_WAIT_BUDGET_SECONDS):
    """Block or bail before spawning Claude if Reddit search is throttled.

    Strategy:
      1. Cheap probe to Reddit to read live X-Ratelimit-Remaining headers.
         This catches the case where the shared state file is stale but the
         server still throttles us (10-min rolling window).
      2. Fall back to the cached state file if the probe network-fails.
    A $0.44 Claude spawn with 5 rate-limited searches is the cost we're
    avoiding; a single probe request is ~300ms.
    """
    probe = _probe_reddit_quota()
    if probe is not None:
        remaining, reset = probe
        print(f"[post_reddit] Reddit quota probe: remaining={remaining:.0f} "
              f"reset_in={int(reset)}s")
        return _apply_rate_limit_policy(remaining, reset, "probe", budget_seconds)
    try:
        with open(RATELIMIT_FILE) as f:
            rl = json.load(f)
    except Exception:
        return True
    wait = int(rl.get("reset_at", 0) - time.time())
    return _apply_rate_limit_policy(
        rl.get("remaining", 100), wait, "cached", budget_seconds,
    )


def mark_comment_blocked(thread_url: str) -> None:
    """Add a subreddit to config.json subreddit_bans.comment_blocked at runtime.

    Called when the bot's comment attempt is rejected (no comment form, locked,
    restricted). The sub gets blocked for future comment attempts so the
    drafter never targets it again. Thread-posting eligibility is tracked
    separately in subreddit_bans.thread_blocked.
    """
    sub_match = re.search(r'/r/([^/]+)/', thread_url)
    if not sub_match:
        return
    sub = sub_match.group(1).lower()
    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        bans = config.setdefault("subreddit_bans", {})
        blocked = bans.setdefault("comment_blocked", [])
        existing = {s.lower() for s in blocked}
        if sub not in existing:
            blocked.append(sub)
            blocked.sort(key=str.lower)
            with open(CONFIG_PATH, "w") as f:
                json.dump(config, f, indent=2)
            print(f"[post_reddit] Added r/{sub} to subreddit_bans.comment_blocked")
    except Exception as e:
        print(f"[post_reddit] WARNING: could not persist blocked sub r/{sub}: {e}")


# Keywords that indicate a permanent account/subreddit block rather than a
# transient failure.  Case-insensitive match against Claude's abort_reason.
# Tuned 2026-04-29: broaden to catch mod-rule bans expressed in present tense
# ("the sub bans software", "no software allowed") in addition to account-level
# bans ("u/X has been banned"). Each new pattern observed from real abort logs.
_THREAD_BLOCK_PATTERNS = [
    r"\bbanned\b",
    r"\bbans\b\s+(all|any|every|every kind|posts?|comments?|software|websites?|self[- ]promo|advertising|promotional)",
    r"\bban\b.*\b(software|posts?|websites?|self[- ]promo|advertising)\b",
    r"access was denied",
    r"\b403\b",
    r"link[- ]only",
    r"text posts? (are )?disabled",
    r"text (tab|option) (is )?disabled",
    r"does not allow text",
    r"not allowed to post",
    r"posting.*restricted",
    r"no (software|self[- ]promo|promotional|advertising|ads)",
    r"\bprohibit(ed|s)?\b",
    r"\bremoved\b.*\b(rule|mod)\b",   # "would be removed per rule X"
    r"would (get )?removed",
    r"\bnot permitted\b",
    r"approved (submitter|user)s? only",
    r"forbidden",
]

def _abort_is_permanent_block(abort_reason: str) -> bool:
    """Return True if abort_reason signals a permanent account/sub block."""
    lower = abort_reason.lower()
    for pat in _THREAD_BLOCK_PATTERNS:
        if re.search(pat, lower):
            return True
    return False


def mark_thread_blocked(subreddit: str, abort_reason: str = "") -> None:
    """Add a subreddit to config.json subreddit_bans.thread_blocked at runtime.

    Called when a thread-post attempt is permanently blocked (account banned,
    link-only sub, text posts disabled, 403). The sub is skipped by
    pick_thread_target.py on all future runs.  Comment eligibility is tracked
    separately in subreddit_bans.comment_blocked.

    subreddit may be bare ('programming') or prefixed ('r/programming').
    """
    sub = re.sub(r"^r/", "", subreddit, flags=re.IGNORECASE).strip().lower()
    if not sub:
        return
    if abort_reason and not _abort_is_permanent_block(abort_reason):
        return
    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        bans = config.setdefault("subreddit_bans", {})
        blocked = bans.setdefault("thread_blocked", [])
        existing = {s.lower() for s in blocked}
        if sub not in existing:
            blocked.append(sub)
            blocked.sort(key=str.lower)
            with open(CONFIG_PATH, "w") as f:
                json.dump(config, f, indent=2)
                f.write("\n")
            print(f"[post_reddit] Auto-blocked r/{sub} from future thread posts (permanent block detected)")
        else:
            print(f"[post_reddit] r/{sub} already in thread_blocked, skipping")
    except Exception as e:
        print(f"[post_reddit] WARNING: could not persist thread-blocked sub r/{sub}: {e}")


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def pick_project(platform="reddit", exclude=None):
    try:
        cmd = ["python3", os.path.join(REPO_DIR, "scripts", "pick_project.py"),
               "--platform", platform, "--json"]
        if exclude:
            cmd.extend(["--exclude", ",".join(exclude)])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
    except Exception:
        pass
    return None


def get_top_performers(project_name, platform="reddit"):
    try:
        result = subprocess.run(
            ["python3", os.path.join(REPO_DIR, "scripts", "top_performers.py"),
             "--platform", platform, "--project", project_name],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def get_top_search_topics(project_name, platform="reddit", limit=8, window_days=30):
    """Return a short text block of best-performing search_topic seeds for this
    project on this platform, or '' if no data yet. See top_search_topics.py."""
    try:
        result = subprocess.run(
            ["python3", os.path.join(REPO_DIR, "scripts", "top_search_topics.py"),
             "--project", project_name, "--platform", platform,
             "--window-days", str(window_days), "--limit", str(limit)],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def get_omitted_reddit_topics(project_name, limit=10, window_hours=168, min_omits=2):
    """Return a JSON list (as a string) of search_topic seeds that have
    consistently produced threads which survive the ripen gate but get
    OMITTED by the draft-time SELECTION GATE (build_draft_prompt's bridge
    test). These are category-level mismatches the LLM should drop or
    rephrase. See scripts/top_omitted_reddit_topics.py.

    `min_omits=2` suppresses one-off omits (could be noise) and surfaces
    only seeds where the pattern has repeated.
    """
    try:
        result = subprocess.run(
            ["python3", os.path.join(REPO_DIR, "scripts", "top_omitted_reddit_topics.py"),
             "--project", project_name,
             "--window-hours", str(window_hours),
             "--limit", str(limit),
             "--min-omits", str(min_omits)],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def get_dud_reddit_queries(project_name, limit=15, window_hours=168):
    """Return a JSON list (as a string) of recent dud Reddit queries for this
    project so build_prompt can paste an anti-list into the LLM scanner.

    Source: reddit_search_attempts (one row per cmd_search call), surfaced via
    scripts/top_dud_reddit_queries.py. Window mirrors the LinkedIn-style 7d
    default — Reddit cycles fire every 30min, so 7d gives a wide enough sample
    to flag truly dead phrasings without overweighting same-day noise.
    """
    try:
        result = subprocess.run(
            ["python3", os.path.join(REPO_DIR, "scripts", "top_dud_reddit_queries.py"),
             "--project", project_name,
             "--window-hours", str(window_hours),
             "--limit", str(limit)],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def get_recent_comments(limit=5):
    dbmod.load_env()
    conn = dbmod.get_conn()
    cur = conn.execute(
        "SELECT our_content FROM posts "
        "WHERE platform='reddit' ORDER BY id DESC LIMIT %s",
        [limit],
    )
    results = [row[0] for row in cur.fetchall()]
    conn.close()
    return results


def load_active_reddit_campaigns():
    """Active Reddit campaigns that carry a literal suffix.

    Tool-level enforcement: the LLM never sees these. We append the suffix to
    the drafted text in Python before posting, so the literal text is
    guaranteed to land on Reddit. sample_rate gates the per-post coin flip
    for concurrent A/B (e.g. 0.5 = ~half of posts get tagged).
    """
    dbmod.load_env()
    conn = dbmod.get_conn()
    try:
        cur = conn.execute(
            """SELECT id, suffix, COALESCE(sample_rate, 1.000)
               FROM campaigns
               WHERE status = 'active'
                 AND (',' || platforms || ',') LIKE '%,reddit,%'
                 AND max_posts_total IS NOT NULL
                 AND posts_made < max_posts_total
                 AND suffix IS NOT NULL AND suffix <> ''
               ORDER BY id"""
        )
        return [
            {"id": r[0], "suffix": r[1], "sample_rate": float(r[2])}
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


def _angle_str(v):
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        return "; ".join(f"{k}: {_angle_str(x)}" for k, x in v.items() if x)
    if isinstance(v, (list, tuple)):
        return ", ".join(_angle_str(x) for x in v if x)
    return str(v) if v else ""


def build_content_angle(project, config):
    """Prefer project-specific positioning over the global config angle."""
    if project.get("content_angle"):
        return project["content_angle"]

    parts = []
    for key in ("description", "differentiator", "icp", "setup"):
        s = _angle_str(project.get(key))
        if s:
            parts.append(s)

    messaging = project.get("messaging", {}) or {}
    for key in ("lead_with_pain", "solution", "proof"):
        s = _angle_str(messaging.get(key))
        if s:
            parts.append(s)

    voice = project.get("voice", {}) or {}
    if voice.get("tone"):
        parts.append(f"Voice: {voice['tone']}")
    if voice.get("never"):
        parts.append("Never: " + "; ".join(voice["never"]))
    examples = voice.get("examples") or voice.get("examples_good") or []
    if examples:
        parts.append("Voice examples: " + " | ".join(examples[:3]))

    if parts:
        return " ".join(parts)

    return config.get("content_angle", "")


def build_discover_prompt(project, config, limit, top_report, recent_comments,
                          top_topics_report="", dud_queries_report="",
                          omitted_topics_report=""):
    """DISCOVER phase: search and select threads only. No drafting.

    Claude outputs action=candidate JSON objects (thread_url, title, author,
    search_topic, engagement_style — no text). Drafting is deferred until after
    ripen filters the list so LLM spend only hits threads that passed the delta gate.
    """
    content_angle = build_content_angle(project, config)
    topics_list = project.get("search_topics") or []
    project_json = json.dumps({
        "name": project.get("name"),
        "description": project.get("description"),
        "search_topics": topics_list,
    }, indent=2)

    recent_ctx = ""
    if recent_comments:
        snippets = "\n".join(f"  - {c}" for c in recent_comments)
        recent_ctx = f"\nYour last {len(recent_comments)} comments (don't repeat these threads):\n{snippets}\n"

    top_ctx = ""
    if top_report:
        lines = top_report.split("\n")[:20]
        top_ctx = f"\n## Past performance feedback:\n{chr(10).join(lines)}\n"

    top_topics_ctx = ""
    if top_topics_report:
        top_topics_ctx = f"\n## Search-topic feedback (seeds with best engagement):\n{top_topics_report}\n"

    dud_queries_ctx = ""
    if dud_queries_report and dud_queries_report.strip() not in ("[]", ""):
        dud_queries_ctx = f"\n## Dead queries (skip these exact phrasings):\n{dud_queries_report}\n"

    omitted_topics_ctx = ""
    if omitted_topics_report and omitted_topics_report.strip() not in ("[]", ""):
        omitted_topics_ctx = (
            "\n## Category-mismatch seeds (returned alive threads but the draft "
            "SELECTION GATE killed them — i.e. this seed surfaces wrong-audience "
            "subs; rephrase MORE NARROWLY around your project's actual domain, "
            "or drop the seed entirely):\n"
            f"{omitted_topics_report}\n"
        )

    max_searches = MAX_DISCOVER_SEARCHES
    pick_low = min(2, max_searches)
    pick_high = max_searches

    return f"""You generate Reddit search queries. The search tool runs in OPAQUE mode this cycle: it dumps every returned thread to a side file for the ripen pipeline and prints back ONLY a one-line summary count. You do NOT see thread content, titles, scores, or URLs. You cannot filter results — the ripen step (numerical delta gate) is the only filter.

Topic area: {project_json}
Content angle: {content_angle}
{recent_ctx}{top_ctx}{top_topics_ctx}{omitted_topics_ctx}{dud_queries_ctx}
## Tool (via Bash)
- Search: python3 {REDDIT_TOOLS} search "QUERY" --limit 25
- Search by sub: python3 {REDDIT_TOOLS} search "QUERY" --subreddits AI_Agents,SaaS --time month
- Search broader time: python3 {REDDIT_TOOLS} search "QUERY" --time month

## What you'll see from the tool
- stdout: one short line, e.g. `OK: 23 threads passed to ripen pipeline (results not shown)`
- stderr: `[reddit_search] q="..." raw=25 returned=23 blocked_sub=2 archived=0 locked=0 too_old=0 already_posted_flagged=0 top_score=187 top_comments=48`

You can use these counts to decide whether to run another query. You CANNOT
read the threads themselves. They are already on disk for ripen.

## CRITICAL Bash rules
- NEVER use run_in_background=true. All commands run foreground.
- Run AT MOST {max_searches} searches total. Each search dumps up to 25 threads.
- Do NOT cat, ls, find, or otherwise inspect /tmp or any dump file. The dump
  directory is private to the ripen step. You don't need to know the path.
- If rate-limited, stop. The ripen step uses whatever was dumped before the limit.

## Steps
1. Pick {pick_low}-{pick_high} concepts from the project's search_topics: {json.dumps(topics_list)}.
   Rephrase each into a natural Reddit search query (vernacular, pain points).
   Avoid the dud queries listed above. If a seed appears in the
   "Category-mismatch seeds" section above, EITHER rephrase it MUCH more
   narrowly (constrain to your project's exact audience/subreddit) OR skip
   it and pick a different seed.
2. Run the searches. Watch the stdout/stderr summary for each call. Prefer
   covering DIFFERENT angles across queries (e.g. don't run 5 near-duplicate
   rephrasings of one seed).
3. (Optional) If a query returns `returned=0`, you may try ONE more rephrasing.
   You may also stop early at {pick_low} if your queries returned plenty of
   results — quality > quota. Never exceed {max_searches} total.
4. Output DONE on its own line.

## OUTPUT FORMAT
Just output `DONE` on its own line after running your searches. No JSON,
no candidate lines, no commentary about thread content (you don't see any).
"""


def build_draft_prompt(project, config, candidates, top_report, recent_comments):
    """DRAFT phase: write comments only for ripen-survivors.

    `candidates` is the list of decisions that passed the delta gate, each
    annotated with ripen data (delta_up, delta_comments, composite). Claude
    fetches each thread, reads context, then writes the best comment.
    """
    content_angle = build_content_angle(project, config)

    recent_ctx = ""
    if recent_comments:
        snippets = "\n".join(f"  - {c}" for c in recent_comments)
        recent_ctx = f"\nYour last {len(recent_comments)} comments (don't repeat talking points):\n{snippets}\n"

    top_ctx = ""
    if top_report:
        lines = top_report.split("\n")[:20]
        top_ctx = f"\n## Past performance feedback:\n{chr(10).join(lines)}\n"

    candidate_lines = []
    for c in candidates:
        rip = c.get("ripen") or {}
        delta_info = ""
        if rip.get("composite") is not None:
            delta_info = (f" [active: Δup={rip.get('delta_up', 0)},"
                          f" Δcomm={rip.get('delta_comments', 0)},"
                          f" composite={rip.get('composite', 0):.1f} over"
                          f" {rip.get('window_sec', 300)}s]")
        candidate_lines.append(
            f"  - {c['thread_url']}{delta_info}\n"
            f"    title: {c.get('thread_title', '')}\n"
            f"    suggested style: {c.get('engagement_style', '')}"
        )
    candidates_block = "\n".join(candidate_lines)

    return f"""You will be handed up to {len(candidates)} Reddit thread(s) that survived the engagement-velocity (ripen) gate. Your job is to draft comments for the ones where you can write something genuinely useful to that audience. Lean toward DRAFTING when the audience overlaps even partially with the project's user, and only OMIT on clear no-bridge cases.

Content angle: {content_angle}
{recent_ctx}{top_ctx}
## Candidate threads (post-ripen):
{candidates_block}

## SELECTION GATE — soft fits are OK; reject only clear mismatches

The ripen step proves a thread is alive (people are voting/commenting). It does NOT prove the thread fits the project. Reddit search returns false positives based on raw token overlap (e.g. a search for "no-code app maker" surfaces r/gamemaker shader threads because of the word "maker"; a search for "E2E testing developer productivity QA" can surface a JonBenet murder thread because of how Reddit indexes acronyms). The gate exists to catch those token-overlap false positives, NOT to demand a perfect product fit on every thread.

For each thread, ask the **bridge test**:
"Could a thoughtful person from {project.get('name', 'this project')}'s audience plausibly read my comment and find it useful, regardless of whether they ever try the product?"

DRAFT it if YES. OMIT only if NO bridge exists at all (clear off-topic / hostile audience / token-overlap false positive). Soft / partial / adjacent fits are GOOD enough — a useful comment in an adjacent sub builds reputation even when no one converts. Don't optimize for purity. Don't artificially cap output. The post-phase will cap actual posting at a reasonable number, so feel free to draft for any thread that passes the soft bridge test.

DRAFT THESE (broad, inclusive — not just direct hits):
- Project: AI test automation (Assrt). Thread: "Playwright selectors keep breaking on every refactor" → direct fit. DRAFT.
- Project: AI test automation. Thread: r/QualityAssurance "How are people handling flaky CI tests?" → adjacent topic, same audience. DRAFT.
- Project: AI app builder (mk0r). Thread: "I want to prototype a tip calculator without learning React" → direct fit. DRAFT.
- Project: AI app builder. Thread: r/SaaS "Indie hackers shipping MVPs in a weekend" → adjacent: same builder mindset. DRAFT (helpful comment about iteration speed).
- Project: study tool (Studyly). Thread: r/medschool "best way to handle 200-slide lectures" → direct fit. DRAFT.
- Project: study tool. Thread: r/GetStudying "I'm burnt out, can't retain anything" → adjacent: study-habit audience. DRAFT (empathetic comment about active recall, even if no product mention).
- Project: home security camera (Cyrano). Thread: r/HomeImprovement "wired vs wireless cameras" → direct fit. DRAFT.

OMIT THESE (clear no-bridge cases only):
- Project: AI test automation. Thread: r/JonBenet "The Absurdity of the BDI Theory" → token-overlap false positive (BDI ≠ a testing acronym here). 1996 murder case audience. NO bridge. OMIT.
- Project: AI app builder. Thread: r/BostonSocialClub "Events worth leaving the house for this weekend" → matched on "tried"/"maker". Locals planning weekends. NO bridge. OMIT.
- Project: AI app builder. Thread: r/gamemaker "Using surfaces to create paper-like behavior" → GameMaker is a code IDE, not a no-code generator. Audience writes GML shaders. NO bridge. OMIT.
- Project: study tool. Thread: r/SubredditDrama "the alternative option is still running" → meta drama, no study angle. OMIT.
- Project: study tool. Thread: r/trichotillomania "the trich trance" → medical condition, not studying. OMIT.
- Project: study tool. Thread where you've ALREADY commented under any of our accounts (`already_posted=true` or our usernames in the comment list): obvious astroturfing. OMIT.
- Any thread where you'd be embarrassed to have your comment shown next to a {project.get('name', 'this project')} link in the same Reddit thread.

## Tools (via Bash)
- Fetch thread: python3 {REDDIT_TOOLS} fetch "THREAD_URL"
- Do NOT run any searches. The threads are already chosen.

## CRITICAL Bash rules
- NEVER use run_in_background=true. All commands foreground.
- Fetch each thread once to read OP and top comments for context. Use the fetched content to apply the SELECTION GATE.

## CRITICAL CONTENT RULES (apply only to threads that pass the gate)
- Go BIMODAL on length: 1 punchy sentence (<100 chars) OR 4-5 sentences of real substance. Avoid 2-3 sentence middle-ground.
- GROUNDING RULE — pick ONE lane per comment:
  LANE 1 - DISCLOSED STORY: open with a hedge ("hypothetically", "imagine someone running this", "scenario:") then you may invent specifics freely.
  LANE 2 - NO FABRICATION: first-person only if every specific (numbers, durations, places, tools) appears verbatim in the content_angle above. Otherwise drop the specific and pattern-frame ("the part that breaks down is...", "the typical failure mode is...").
- NEVER mention product names (fazm, assrt, pieline, cyrano, terminator, mk0r, s4l).
- NEVER include URLs or links in your comment text.
- Prefer replying to OP (top-level reply). ONE comment per thread.
- Statements beat questions. Be authoritative, not inquisitive.

## Content rules
{get_content_rules("reddit")}

## OUTPUT FORMAT
For each thread that PASSES the SELECTION GATE, output one JSON object per line:
{{"action": "post", "thread_url": "SAME_URL_AS_GIVEN", "reply_to_url": null, "text": "your comment here", "thread_author": "username", "thread_title": "thread title", "engagement_style": "style_name", "search_topic": "the seed concept", "new_style": null}}

For threads that FAIL the gate, simply omit them. Do NOT emit a JSON line; do NOT draft a comment. The shell handles unhandled candidates correctly (Phase 0 salvage on the next cycle re-checks them, and one-strike ripen failure has already pruned dead threads).

Output DONE after all JSONs. Do NOT narrate. Fetch, gate, draft (or omit), output JSON, DONE.
"""


def parse_candidates(output):
    """Extract action=candidate JSON objects from Claude's discover output."""
    candidates = []
    seen_urls = set()
    for match in re.finditer(r'\{[^{}]*?"action"\s*:\s*"candidate"[^{}]*?\}', output):
        try:
            c = json.loads(match.group())
            url = c.get("thread_url", "")
            if url and url not in seen_urls:
                candidates.append(c)
                seen_urls.add(url)
        except (json.JSONDecodeError, TypeError):
            continue
    return candidates


def build_prompt(project, config, limit, top_report, recent_comments,
                 top_topics_report="", dud_queries_report=""):
    """Build prompt for Claude to search, evaluate, and draft replies (no posting).

    `dud_queries_report` is a JSON list of recent zero-result queries for this
    project (see get_dud_reddit_queries). When non-empty, an anti-list block is
    inserted alongside the positive top_topics_report so the LLM is steered
    away from phrasings that have already proven flat in the last 7 days.
    """
    content_angle = build_content_angle(project, config)

    # Unified search_topics (post 2026-04-30 legacy field cleanup).
    topics_list = project.get("search_topics") or []

    project_json = json.dumps({
        "name": project.get("name"),
        "description": project.get("description"),
        "search_topics": topics_list,
    }, indent=2)

    recent_ctx = ""
    if recent_comments:
        snippets = "\n".join(f"  - {c}" for c in recent_comments)
        recent_ctx = f"""
Your last {len(recent_comments)} comments (don't repeat talking points):
{snippets}
"""

    top_ctx = ""
    if top_report:
        lines = top_report.split("\n")[:30]
        top_ctx = f"""
## Feedback from past performance:
{chr(10).join(lines)}
"""

    top_topics_ctx = ""
    if top_topics_report:
        top_topics_ctx = f"""
## Search-topic feedback (which seeds actually led to engagement):
{top_topics_report}

Prefer seeds with higher total/avg scores when they fit. If none of the top
seeds match this run's angle, pick any seed from the project's search_topics
list. New seeds with 0 posts are fine — we need to explore.
"""

    # NEGATIVE-signal feedback: queries that have produced zero post-filter
    # candidates in the last 7 days. Mirrors twitter_search_attempts /
    # top_dud_twitter_queries.py but speaks in terms of (query, subreddits)
    # since Reddit search is sub-scoped. Keep this list short — Reddit is
    # more keyword-rigid than Twitter, so even "the same phrase but in a
    # different sub" can still produce results.
    dud_queries_ctx = ""
    if dud_queries_report and dud_queries_report.strip() not in ("[]", ""):
        dud_queries_ctx = f"""
## Dead queries (DO NOT redraft these — flat for the last 7 days):
{dud_queries_report}

Each entry is a (query, subreddits) phrasing that has returned ZERO usable
threads on every recent attempt. Pick fresh wording, a different angle, or a
different subreddit slate. Reusing an exact dead phrasing wastes a search
slot and burns rate-limit budget for no upside.
"""

    return f"""Find {limit} Reddit threads where you can add genuine value as someone with expertise in {project.get('name', 'general')}.

Topic area: {project_json}
Content angle: {content_angle}

Your role: You are a knowledgeable observer of this space. Your comments should be indistinguishable
from any other knowledgeable person in the conversation. NEVER mention product names, links, websites,
or GitHub repos. Product mentions happen ONLY later in the reply pipeline when people respond to you.

CRITICAL: every comment picks ONE of two lanes (see the GROUNDING RULE below).
  LANE 1 - DISCLOSED STORY: open with a hedge ("hypothetically", "imagine someone running this",
  "scenario:", "say a friend tried") and you may then invent any specifics.
  LANE 2 - NO FABRICATION: stay first-person / plain voice, but every specific (numbers, durations,
  places, course names, headcount, named tools) must appear verbatim in this project's content_angle /
  voice / messaging in config.json. Otherwise drop the specific or pattern-frame ("the part that
  breaks down is...", "the typical failure mode is...").
Never present an invented specific as a personal first-hand claim without a Lane 1 opener.
{recent_ctx}{top_ctx}{top_topics_ctx}{dud_queries_ctx}
{get_styles_prompt("reddit", context="posting")}

## Tools (via Bash) - ALWAYS foreground, NEVER run_in_background
- Search (global, by relevance): python3 {REDDIT_TOOLS} search "QUERY" --limit 15
- Search (scoped to specific subs): python3 {REDDIT_TOOLS} search "QUERY" --subreddits AI_Agents,SaaS,smallbusiness --time month
- Search (broader time range): python3 {REDDIT_TOOLS} search "QUERY" --time month
- Fetch thread: python3 {REDDIT_TOOLS} fetch "THREAD_URL"
- Check dedup: python3 {REDDIT_TOOLS} already-posted "THREAD_URL"

Search defaults to sort=relevance and time=week. Use --time month for broader results. Use --subreddits for targeted sub searches.

## Delta gating (new 2026-05-05)
Each thread in the search JSON now carries delta fields populated from a
persistent reddit_thread_snapshots table:
  - sightings: how many search cycles have surfaced this exact thread
  - delta_score: upvote change since first_seen_at
  - delta_comments: comment change since first_seen_at
  - delta_window_min: minutes between first_seen_at and now
  - first_seen_at: when we first saw this thread

Use these to PREFER threads that are still picking up momentum since we last
saw them (positive delta_score with recent activity) over stale threads that
peaked hours ago. A thread with sightings>=2 and delta_score<=0 over 60+ min
is going cold; skip it for a fresher candidate.

## CRITICAL Bash rules
- NEVER use run_in_background=true. All bash commands must run foreground and return quickly (under 20s each).
- NEVER use `sleep` commands. NEVER run `sleep N && cat ...` to wait for background tasks.
- NEVER pipe multiple searches with `&` or `&&`. Run ONE search command at a time, wait for output, then decide next step.
- If you see `{{"error": "rate_limited", ...}}` in the output, DO NOT retry that command. Skip it and move on.
  Rate limits are global. Waiting won't help this session. Use whatever search results you already have.
- If you can't find enough threads after 5 search attempts total, draft fewer posts (even 1-2 is fine) rather than searching more.

## CRITICAL CONTENT RULES
- Study the style performance data in the feedback report below. Pick styles with the highest avg upvotes.
- Go BIMODAL on length: either 1 punchy sentence (<100 chars) or 4-5 sentences of real substance. AVOID the 2-3 sentence middle.
- First-person ("I"/"my") has TWO valid forms (see GROUNDING RULE). Lane 1: open with a disclosure phrase ("hypothetically", "imagine someone running this", "scenario:") and then invent freely. Lane 2: stay first-person only if every specific is grounded in content_angle/voice/messaging in config.json, or drop the specific and pattern-frame ("the part that breaks down is...", "the typical failure mode is..."). Never undisclosed first-person with invented numbers/places/programs.
- NEVER mention product names (fazm, assrt, pieline, cyrano, terminator, mk0r, s4l).
- NEVER include URLs or links.
- Prefer replying to OP (top-level reply).
- ONE comment per thread.
- Statements beat questions. Be authoritative, not inquisitive.

## Steps
1. Pick 2 concepts from the project's search_topics list: {json.dumps(topics_list)}.
   These are shared concept seeds across platforms (Twitter, Reddit, GitHub, LinkedIn). Some
   phrases are tuned for other platforms — rephrase each into natural Reddit search terms
   (vernacular, problem-framing, pain points) before running the search. Skip already_posted=true threads.
2. Pick {limit} best threads where you have genuine expertise to contribute. Prefer replying to OP. Fetch each one.
3. Draft the comment following the CRITICAL CONTENT RULES above. Quality over quantity.
4. Output each as a JSON object, then DONE. Include the seed concept you used in "search_topic".

## Content rules
{get_content_rules("reddit")}

## CRITICAL OUTPUT FORMAT
You MUST output each draft as a raw JSON object on its own line. No commentary before or after. Example:

{{"action": "post", "thread_url": "https://old.reddit.com/r/sub/comments/abc/title/", "reply_to_url": null, "text": "your comment here", "thread_author": "username", "thread_title": "thread title", "engagement_style": "critic", "search_topic": "the seed concept you picked", "new_style": null}}

If, and ONLY if, none of the listed styles fits, you may invent one. Set "engagement_style" to your snake_case name AND replace `"new_style": null` with `{{"description": "...", "example": "...", "note": "...", "why_existing_didnt_fit": "..."}}`. Inventing should be rare; prefer an existing style if it's even 80% right.

After all {limit} JSON objects, output DONE on its own line.
Do NOT describe what you are doing. Do NOT narrate. Just search, draft, output JSON, DONE.
"""


def run_claude(prompt, timeout=600):
    """Run claude -p in bare mode with Bash tool only (no MCP needed).

    Streams output in real time to stderr (picked up by tee in the shell wrapper)
    while collecting the full output for JSON parsing.
    """
    import time as _time
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_create": 0, "cost_usd": 0.0}
    session_id = str(uuid.uuid4())
    usage["session_id"] = session_id
    # Set in this process's env so subsequent log_post → reddit_tools.py inherits it
    os.environ["CLAUDE_SESSION_ID"] = session_id
    cmd = ["claude", "-p", "--session-id", session_id, "--output-format", "stream-json", "--verbose"]
    cmd += ["--tools", "Bash,Read"]
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)  # ensure claude uses OAuth, not API key
    try:
        proc = subprocess.Popen(
            cmd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        proc.stdin.write(prompt)
        proc.stdin.close()
        collected = []
        deadline = _time.time() + timeout
        import select
        while True:
            remaining = deadline - _time.time()
            if remaining <= 0:
                proc.kill()
                return False, "TIMEOUT", usage
            ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 30))
            if ready:
                line = proc.stdout.readline()
                if not line:
                    break
                collected.append(line)
                # Stream meaningful events to stderr so tee/log captures them
                try:
                    evt = json.loads(line.strip())
                    etype = evt.get("type", "")
                    if etype == "assistant":
                        msg = evt.get("message", {})
                        for block in msg.get("content", []):
                            if block.get("type") == "tool_use":
                                print(f"[post_reddit] tool: {block.get('name','')} | {str(block.get('input',{}).get('command',''))[:120]}", file=sys.stderr, flush=True)
                            elif block.get("type") == "text" and block.get("text","").strip():
                                txt = block["text"].strip()[:200]
                                print(f"[post_reddit] {txt}", file=sys.stderr, flush=True)
                    elif etype == "user":
                        # Tool results land in user messages. reddit_tools.py
                        # search emits a `[reddit_search] q=... raw=N returned=R`
                        # line on its own stderr, which Claude Code's Bash tool
                        # bundles into the tool_result content. Forward those
                        # markers into our log so enrichPostCommentsRedditRuns
                        # can derive raw/passed pills per run.
                        msg = evt.get("message", {})
                        for block in msg.get("content", []):
                            if block.get("type") != "tool_result":
                                continue
                            content = block.get("content", "")
                            if isinstance(content, list):
                                content = "".join(c.get("text","") for c in content if isinstance(c, dict))
                            for ln in str(content).splitlines():
                                if ln.startswith("[reddit_search]"):
                                    print(ln, file=sys.stderr, flush=True)
                    elif etype == "result":
                        print(f"[post_reddit] done: cost=${evt.get('total_cost_usd',0):.4f}", file=sys.stderr, flush=True)
                except (json.JSONDecodeError, TypeError):
                    print(f"[post_reddit] {line.rstrip()[:200]}", file=sys.stderr, flush=True)
            elif proc.poll() is not None:
                # Process ended, read remaining
                rest = proc.stdout.read()
                if rest:
                    collected.append(rest)
                break
            else:
                print(f"[post_reddit] ... still running ({int(_time.time() - (deadline - timeout))}s)", file=sys.stderr, flush=True)
        proc.wait()
        # Parse stream-json: collect ALL text blocks (not just the final result)
        # JSON post decisions can appear in any assistant message, not just the last one
        all_text_parts = []
        for line_str in collected:
            line_str = line_str.strip()
            if not line_str:
                continue
            try:
                event = json.loads(line_str)
                etype = event.get("type", "")
                if etype == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            all_text_parts.append(block["text"])
                elif etype == "result":
                    if event.get("result"):
                        all_text_parts.append(event["result"])
                    usage["cost_usd"] = event.get("total_cost_usd", 0.0)
                    u = event.get("usage", {})
                    usage["input_tokens"] = u.get("input_tokens", 0)
                    usage["output_tokens"] = u.get("output_tokens", 0)
                    usage["cache_read"] = u.get("cache_read_input_tokens", 0)
                    usage["cache_create"] = u.get("cache_creation_input_tokens", 0)
            except (json.JSONDecodeError, TypeError):
                pass
        text_output = "\n".join(all_text_parts) if all_text_parts else "".join(collected)
        stderr_out = proc.stderr.read() if proc.stderr else ""
        try:
            subprocess.run(
                ["python3", os.path.join(REPO_DIR, "scripts", "log_claude_session.py"),
                 "--session-id", session_id, "--script", "post_reddit"],
                capture_output=True, text=True, timeout=30,
            )
        except Exception as e:
            print(f"[post_reddit] WARNING: log_claude_session failed: {e}", file=sys.stderr)
        return proc.returncode == 0, text_output + stderr_out, usage
    except Exception as e:
        return False, str(e), usage


def post_via_cdp(thread_url, reply_to_url, text):
    """Post a comment or reply via CDP. Returns parsed JSON result."""
    # 5 attempts with lock-aware backoff. Lock contention (engage.sh or other
    # reddit-agent sessions mid-work) gets longer waits since those sessions
    # have natural gaps every 20-60s between replies. Other errors use a short
    # retry in case of transient network issues.
    MAX_ATTEMPTS = 5
    for attempt in range(MAX_ATTEMPTS):
        try:
            target = reply_to_url or thread_url
            cmd = ["python3", REDDIT_BROWSER, "reply" if reply_to_url else "post-comment", target, text]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            cdp_out = proc.stdout.strip()
            if not cdp_out:
                print(f"[post_reddit] CDP attempt {attempt + 1}: no stdout. stderr: {proc.stderr[:200]}")
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(10)
                continue
            result = json.loads(cdp_out)
            if result.get("ok"):
                return result
            err = result.get("error", "unknown")
            print(f"[post_reddit] CDP attempt {attempt + 1}: {err}")
            if err in ("thread_not_found", "thread_locked", "thread_archived", "already_replied", "not_logged_in", "account_blocked_in_sub"):
                return result  # Don't retry these
            # Lock contention: another reddit-agent session is actively working.
            # Back off in increasing intervals to catch a natural gap between
            # their reply drafts. Total wait across 5 attempts: ~2.5 min.
            if "locked by session" in err.lower():
                if attempt < MAX_ATTEMPTS - 1:
                    wait = [20, 35, 50, 60][attempt]
                    print(f"[post_reddit] CDP waiting {wait}s for browser lock to free...")
                    time.sleep(wait)
                continue
            # Any other error: short sleep then retry
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(5)
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, json.JSONDecodeError) as e:
            print(f"[post_reddit] CDP attempt {attempt + 1} exception: {e}")
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(10)
    return {"ok": False, "error": "all_attempts_failed"}


def log_post(thread_url, permalink, text, project_name, thread_author, thread_title, reddit_username, engagement_style=None, search_topic=None):
    """Log a successful post to the database. Returns the new post_id, or None."""
    try:
        cmd = ["python3", REDDIT_TOOLS, "log-post",
             thread_url, permalink or "", text, project_name,
             thread_author, thread_title,
             "--account", reddit_username]
        if engagement_style:
            cmd.extend(["--engagement-style", engagement_style])
        if search_topic:
            cmd.extend(["--search-topic", search_topic])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        try:
            payload = json.loads((result.stdout or "").strip())
            return payload.get("post_id")
        except (json.JSONDecodeError, AttributeError, TypeError):
            return None
    except Exception as e:
        print(f"[post_reddit] WARNING: log-post failed: {e}")
        return None


def bump_campaigns(table, row_id, campaign_ids):
    """Attach a row in {posts,replies,dm_messages} to its applied campaigns."""
    if not row_id or not campaign_ids:
        return
    bump = os.path.join(REPO_DIR, "scripts", "campaign_bump.py")
    for cid in campaign_ids:
        try:
            subprocess.run(
                ["python3", bump,
                 "--table", table, "--id", str(row_id), "--campaign-id", str(cid)],
                capture_output=True, text=True, timeout=15,
            )
        except Exception as e:
            print(f"[post_reddit] WARNING: campaign_bump failed (id={row_id} c={cid}): {e}")


def parse_post_decisions(output):
    """Extract JSON post decisions from Claude's output, deduplicated by thread_url."""
    decisions = []
    seen_urls = set()
    for match in re.finditer(r'\{[^{}]*?"action"\s*:\s*"post"[^{}]*?\}', output):
        try:
            decision = json.loads(match.group())
            url = decision.get("thread_url", "")
            if decision.get("text") and url and url not in seen_urls:
                decisions.append(decision)
                seen_urls.add(url)
        except (json.JSONDecodeError, TypeError):
            continue
    return decisions


def _discover_iteration(args, config, reddit_username, already_picked):
    """DISCOVER phase: search and select threads. No drafting.

    Returns {project_name, decisions: [candidates], cost, session_id} where
    each candidate has thread_url, title, author, search_topic, engagement_style
    but NO text field. Uses `decisions` key so ripen_reddit_plan.py needs no
    changes (it reads decisions[].thread_url regardless of text presence).
    """
    if args.project:
        project = None
        for p in config.get("projects", []):
            if p["name"].lower() == args.project.lower():
                project = p
                break
        if not project:
            print(f"[post_reddit] ERROR: project '{args.project}' not found")
            return None
    else:
        project = pick_project("reddit", exclude=already_picked)
        if not project:
            print(f"[post_reddit] No eligible project left (already picked: {already_picked})")
            return None

    project_name = project.get("name", "general")
    print(f"[post_reddit] Project: {project_name}")

    top_report = get_top_performers(project_name)
    recent_comments = get_recent_comments()
    top_topics_report = get_top_search_topics(project_name, platform="reddit")
    dud_queries_report = get_dud_reddit_queries(project_name)
    omitted_topics_report = get_omitted_reddit_topics(project_name)
    prompt = build_discover_prompt(project, config, args.limit, top_report, recent_comments,
                                   top_topics_report=top_topics_report,
                                   dud_queries_report=dud_queries_report,
                                   omitted_topics_report=omitted_topics_report)

    if args.dry_run:
        print(f"=== DRY RUN discover (project={project_name}) ===")
        print(prompt)
        print("=== END DRY RUN ===")
        return {"project_name": project_name, "decisions": [], "cost": 0.0, "dry_run": True}

    plan_batch_id = f"reddit-discover-{project_name}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    os.environ["SAPS_REDDIT_PROJECT"] = project_name
    os.environ["SAPS_REDDIT_BATCH_ID"] = plan_batch_id

    # Opaque-results discover (post 2026-05-07 refactor): create a private
    # dump dir and tell reddit_tools.py via env var to write thread JSON
    # there instead of stdout. Claude only sees count summaries, never
    # individual threads, so it cannot pre-filter the way it did in the
    # 20:16:39 cycle (returned 0 of 39 expected). After Claude exits we
    # harvest every dumped file directly into the candidate plan.
    import tempfile as _tempfile
    import shutil as _shutil
    import glob as _glob
    dump_dir = _tempfile.mkdtemp(prefix=f"reddit-discover-{project_name}-")
    os.environ["SAPS_REDDIT_DUMP_DIR"] = dump_dir

    print(f"[post_reddit] Starting discover session (limit={args.limit}, timeout={args.timeout}s, dump_dir={dump_dir})")
    start = time.time()
    try:
        ok, output, usage = run_claude(prompt, timeout=args.timeout)
    finally:
        # Always unset so a subsequent (non-discover) reddit_tools call in
        # this process doesn't accidentally inherit dump mode.
        os.environ.pop("SAPS_REDDIT_DUMP_DIR", None)
    elapsed = time.time() - start
    print(f"[post_reddit] Discover finished in {elapsed:.0f}s (${usage['cost_usd']:.4f})")

    # Harvest the dump dir BEFORE checking the Claude exit code: even if
    # Claude crashed mid-run, any searches it completed before the crash
    # produced dump files we should still feed to ripen.
    candidates = []
    seen_urls = set()
    dump_files = sorted(_glob.glob(os.path.join(dump_dir, "result-*.json")))
    print(f"[post_reddit] Discover dump dir contains {len(dump_files)} file(s)")
    for dump_path in dump_files:
        try:
            with open(dump_path) as df:
                payload = json.load(df)
        except Exception as e:
            print(f"[post_reddit] WARN: skipping unreadable dump {dump_path}: {e}",
                  file=sys.stderr)
            continue
        query = payload.get("query") or ""
        for t in payload.get("threads") or []:
            url = t.get("url") or ""
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            candidates.append({
                "action": "candidate",
                "thread_url": url,
                "thread_title": t.get("title") or "",
                "thread_author": t.get("author") or "",
                "selftext": t.get("selftext") or "",  # captured for analytics + future relevance gates
                "score": int(t.get("score") or 0),
                "num_comments": int(t.get("num_comments") or 0),
                "search_topic": query,
            })
    # Best-effort cleanup; the OS will eventually reap /tmp anyway.
    try:
        _shutil.rmtree(dump_dir, ignore_errors=True)
    except Exception:
        pass

    if not ok:
        print(f"[post_reddit] Discover FAILED: {output[:300]}")
        # If Claude crashed but we have harvested candidates, keep them.
        # Only fall through to claude_failed when we got nothing.
        if not candidates:
            return {"project_name": project_name, "decisions": [], "cost": usage["cost_usd"],
                    "error": "claude_failed"}

    print(f"[post_reddit] Discover harvested {len(candidates)} candidate(s) from dump dir")
    if not candidates:
        print(f"[post_reddit] No candidates dumped — Claude may have skipped searches. Last 10 lines of output:")
        for line in output.strip().split("\n")[-10:]:
            print(f"  {line}")

    # Persist freshly-discovered candidates to reddit_candidates so a
    # transient post failure on a later phase can be retried by the next
    # cycle's Phase 0 salvage. Best-effort: if the queue write fails, the
    # tmpfile flow still works for this cycle, we just lose the salvage
    # benefit. See module-level _db_upsert_discovered_candidate.
    queue_batch = getattr(args, "batch_id", None) or plan_batch_id
    if not args.dry_run and candidates:
        for c in candidates:
            _db_upsert_discovered_candidate(c, queue_batch, project_name)

    # Backfill seed on reddit_search_attempts rows from this batch so the
    # Search Queries dashboard can join attempts → posts via search_topic.
    # Use the first candidate's search_topic — LIMIT=1 means one seed/batch.
    if candidates and plan_batch_id:
        seed = (candidates[0].get("search_topic") or "").strip()
        if seed:
            try:
                dbmod.load_env()
                conn = dbmod.get_conn()
                conn.execute(
                    "UPDATE reddit_search_attempts SET seed = %s "
                    "WHERE batch_id = %s AND seed IS NULL",
                    [seed, plan_batch_id],
                )
                conn.commit()
                conn.close()
            except Exception as e:
                print(f"[post_reddit] WARNING: seed backfill failed: {e}", file=sys.stderr)

    return {"project_name": project_name, "decisions": candidates,
            "cost": usage["cost_usd"], "session_id": usage.get("session_id"),
            "phase": "discover"}


def _draft_iteration(plan, config, reddit_username):
    """DRAFT phase: write comments for ripen-survivors only.

    `plan` is the ripen-filtered discover output. Each decision has thread_url
    + ripen annotations. Claude fetches each thread and writes the comment.
    Returns the plan with `text` added to each decision (i.e. ready for _post_iteration).

    Salvage shortcut (2026-05-06): for each candidate we first check if a
    still-fresh draft exists in reddit_candidates (drafted < DRAFT_TTL_MIN min
    ago, written by a prior cycle whose post phase failed transiently). If
    every candidate has a fresh draft, we skip the Claude session entirely
    and merge the persisted text in. Mirrors twitter_post_plan.py's "EXISTING
    DRAFT" reuse path; saves $0.20-$0.40 per salvaged candidate.
    """
    project_name = plan.get("project_name", "general")
    candidates = [d for d in (plan.get("decisions") or []) if d.get("thread_url")]
    if not candidates:
        return plan

    # Salvage shortcut: check each candidate for a still-fresh persisted draft
    # before paying the LLM cost. If ALL candidates are covered, skip Claude
    # and return the merged plan immediately. Order matters here: we must
    # consult the DB before building the Claude prompt so we don't waste
    # tokens prepping a session we won't run.
    fresh_drafts = {}
    for c in candidates:
        # An in-memory draft_text from _db_pick_salvage_candidate also counts.
        if c.get("draft_text"):
            fresh_drafts[c["thread_url"]] = (
                c["draft_text"],
                c.get("engagement_style") or "reused",
            )
            continue
        text, style = _db_load_fresh_draft(c["thread_url"])
        if text:
            fresh_drafts[c["thread_url"]] = (text, style or c.get("engagement_style") or "reused")

    if fresh_drafts and len(fresh_drafts) == len(candidates):
        print(f"[post_reddit] Draft shortcut: all {len(candidates)} candidate(s) "
              f"have fresh drafts (<{DRAFT_TTL_MIN}m), skipping Claude session.")
        merged = []
        for c in candidates:
            text, style = fresh_drafts[c["thread_url"]]
            merged_d = dict(c)
            merged_d["text"] = text
            merged_d["engagement_style"] = style
            merged_d["action"] = "post"
            merged_d.setdefault("reply_to_url", None)
            merged.append(merged_d)
        plan = dict(plan)
        plan["decisions"] = merged
        plan["draft_cost"] = 0.0
        plan["phase"] = "draft"
        plan["draft_reused"] = True
        return plan

    project = None
    config_projects = config.get("projects", [])
    for p in config_projects:
        if p["name"].lower() == project_name.lower():
            project = p
            break
    if not project:
        print(f"[post_reddit] WARNING: project '{project_name}' not found in config, drafting with generic context")
        project = {"name": project_name}

    top_report = get_top_performers(project_name)
    recent_comments = get_recent_comments()
    prompt = build_draft_prompt(project, config, candidates, top_report, recent_comments)

    print(f"[post_reddit] Starting draft session for {len(candidates)} thread(s)...")
    start = time.time()
    ok, output, usage = run_claude(prompt, timeout=600)
    elapsed = time.time() - start
    print(f"[post_reddit] Draft finished in {elapsed:.0f}s (${usage['cost_usd']:.4f})")

    if not ok:
        print(f"[post_reddit] Draft FAILED: {output[:300]}")
        plan["draft_error"] = "claude_failed"
        plan["draft_cost"] = usage["cost_usd"]
        return plan

    drafted = parse_post_decisions(output)
    print(f"[post_reddit] Draft produced {len(drafted)} post(s)")

    # Merge text back into the original candidates by thread_url so we
    # preserve ripen annotations, search_topic, etc. from discover phase.
    # Each freshly-written draft is also persisted to reddit_candidates so a
    # later salvage iteration can reuse it without paying the LLM cost again.
    by_url = {d["thread_url"]: d for d in drafted}
    merged = []
    for c in candidates:
        url = c.get("thread_url", "")
        drafted_d = by_url.get(url)
        if drafted_d and drafted_d.get("text"):
            merged_d = dict(c)
            merged_d["text"] = drafted_d["text"]
            merged_d["reply_to_url"] = drafted_d.get("reply_to_url")
            merged_d["thread_author"] = drafted_d.get("thread_author") or c.get("thread_author")
            merged_d["thread_title"] = drafted_d.get("thread_title") or c.get("thread_title")
            merged_d["engagement_style"] = drafted_d.get("engagement_style") or c.get("engagement_style")
            merged_d["action"] = "post"
            merged.append(merged_d)
            _db_save_draft(url, merged_d["text"], merged_d.get("engagement_style"))
        else:
            # Claude OMITted this thread (build_draft_prompt's SELECTION GATE
            # decided no plausible bridge between the thread's audience and
            # the project — token-overlap false positive, off-topic sub, etc.).
            # Mark status='failed' with reason='draft_gate_omit' so Phase 0
            # salvage on the next cycle stops re-pulling it. Without this the
            # same dead thread would keep clearing ripen (engagement is real)
            # and burning ~$0.05/cycle on a fetch + gate decision that always
            # lands the same way. Mirrors the one-strike rule at ripen time,
            # applied at draft time for active-but-unfit threads.
            print(f"[post_reddit] Draft gate OMIT for {url}: marking status=failed")
            _db_mark_candidate_attempt(url, reason="draft_gate_omit", permanent=True)

    plan = dict(plan)
    plan["decisions"] = merged
    plan["draft_cost"] = usage["cost_usd"]
    plan["draft_session_id"] = usage.get("session_id")
    plan["phase"] = "draft"
    return plan


def _post_iteration(plan, reddit_username):
    """Execute browser CDP posts for the decisions in plan. Returns (posted, failed)."""
    project_name = plan["project_name"]
    decisions = plan.get("decisions") or []

    if not decisions:
        return 0, 0

    # 2026-05-08: post-phase cap REMOVED per user instruction. Three serial
    # gates already filter the candidate pool (search-time blocks,
    # ripen composite floor, softened LLM relevance gate). Anything that
    # survives all three has earned its post; an arbitrary 10/cycle cap was
    # just throwing away qualified work. If Reddit rate-limits start firing
    # under runaway-cycle conditions, revisit by adding a per-minute throttle
    # to _post_iteration's loop body, NOT a hard count cap.

    # In two-phase mode (plan in process A, post in process B), the env var
    # set by run_claude in process A is gone. Re-export here so log_post →
    # reddit_tools.py log-post stamps posts.claude_session_id correctly and
    # the dashboard activity feed can join to claude_sessions for cost.
    plan_session_id = plan.get("session_id")
    if plan_session_id:
        os.environ["CLAUDE_SESSION_ID"] = plan_session_id

    active_campaigns = load_active_reddit_campaigns()
    if active_campaigns:
        for c in active_campaigns:
            print(f"[post_reddit] active campaign id={c['id']} "
                  f"sample_rate={c['sample_rate']:.3f} suffix={c['suffix']!r}")

    posted = 0
    failed = 0

    for i, decision in enumerate(decisions):
        thread_url = decision["thread_url"]
        reply_to_url = decision.get("reply_to_url")
        text = decision["text"]
        thread_author = decision.get("thread_author", "unknown")
        thread_title = decision.get("thread_title", "unknown")
        # validate_or_register accepts known styles, registers well-formed
        # new ones as candidates, and returns None for unknown-and-undocumented.
        # source_post URL is the thread we're replying to; we don't have our
        # own URL until after the post lands.
        engagement_style, _style_action = validate_or_register(
            decision,
            source_post={
                "platform": "reddit",
                "post_url": thread_url,
                "post_id": None,
                "model": decision.get("model"),
            },
        )
        search_topic = decision.get("search_topic") or None

        applied_campaign_ids = []
        for camp in active_campaigns:
            if random.random() < camp["sample_rate"]:
                text = text + camp["suffix"]
                applied_campaign_ids.append(camp["id"])
        if applied_campaign_ids:
            print(f"[post_reddit] applied campaigns {applied_campaign_ids} (suffix appended)")

        # URL-wrap the final text (URLs in suffix included). Mints into
        # post_links with NULL post_id; we backfill after log_post returns
        # below. On wrap failure, post unwrapped — losing attribution is
        # better than failing a post that already passed planning.
        minted_session = None
        try:
            from dm_short_links import wrap_text_for_post
            wrap_res = wrap_text_for_post(text=text, platform="reddit",
                                            project_name=project_name)
            if wrap_res.get("ok"):
                text = wrap_res["text"]
                minted_session = wrap_res.get("minted_session")
                if wrap_res.get("codes"):
                    print(f"[post_reddit] wrapped {len(wrap_res['codes'])} URL(s): "
                          f"{wrap_res['codes']}")
            else:
                print(f"[post_reddit] WARNING: URL wrap failed "
                      f"({wrap_res.get('error')}); posting unwrapped")
        except Exception as e:
            print(f"[post_reddit] WARNING: URL wrap raised ({e}); posting unwrapped")

        print(f"[post_reddit] Posting {i + 1}/{len(decisions)}: {thread_title[:50]}...")
        result = post_via_cdp(thread_url, reply_to_url, text)

        if result.get("ok"):
            if result.get("already_replied"):
                print(f"[post_reddit] DEDUP: already posted in this thread")
                # Treat dedup as a successful queue resolution: the row should
                # come out of 'pending' so Phase 0 stops salvaging it.
                _db_mark_candidate_posted(thread_url, None)
                continue
            permalink = result.get("permalink", "")
            if not permalink or not permalink.startswith("http"):
                print(f"[post_reddit] SKIPPED LOG: no valid permalink captured (got: {permalink!r})")
                failed += 1
                # No-permalink is permanent: the post may have actually
                # landed but we can't verify it; retrying would dupe.
                _db_mark_candidate_attempt(thread_url, "no_permalink", permanent=True)
                continue
            new_post_id = log_post(thread_url, permalink, text, project_name,
                     thread_author, thread_title, reddit_username,
                     engagement_style=engagement_style,
                     search_topic=search_topic)
            bump_campaigns("posts", new_post_id, applied_campaign_ids)
            # Backfill post_links.post_id for the codes minted at wrap time
            # so /api/short-links/<code> resolver knows which post each
            # click attributes to. Idempotent; no-op when minted_session is
            # None (post had no URLs).
            if minted_session and new_post_id:
                try:
                    from dm_short_links import backfill_post_id
                    backfill_post_id(minted_session=minted_session,
                                     post_id=new_post_id)
                except Exception as e:
                    print(f"[post_reddit] WARNING: backfill_post_id failed ({e})")
            posted += 1
            print(f"[post_reddit] POSTED: {permalink}")
            _db_mark_candidate_posted(thread_url, new_post_id)
        else:
            err = result.get("error", "unknown")
            failed += 1
            print(f"[post_reddit] CDP FAILED: {err}")
            if err == "account_blocked_in_sub":
                mark_comment_blocked(thread_url)
            # Classify the CDP error for queue retry. Unknown errors default
            # to TRANSIENT so we don't permanently kill candidates on a new
            # error string we haven't classified yet; the MAX_ATTEMPTS cap
            # auto-promotes them to 'failed' after 3 retries anyway.
            permanent = err in _PERMANENT_CDP_ERRORS
            _db_mark_candidate_attempt(thread_url, err, permanent=permanent)

        if i < len(decisions) - 1:
            time.sleep(180)  # 3 min gap between posts within a single Claude session

    return posted, failed


def main():
    parser = argparse.ArgumentParser(description="Reddit posting orchestrator")
    parser.add_argument("--dry-run", action="store_true", help="Print prompt without executing")
    parser.add_argument("--limit", type=int, default=3, help="Max comments per Claude session (default: 3)")
    parser.add_argument("--timeout", type=int, default=3600, help="Timeout for Claude session")
    parser.add_argument("--project", default=None, help="Override project selection")
    parser.add_argument("--phase",
                        choices=["discover", "draft", "post", "phase0", "salvage"],
                        required=True,
                        help="discover: search+select threads only (no drafting), writes JSON to --out. "
                             "draft: write comments for ripen-survivors from --in, writes JSON to --out. "
                             "post: read JSON from --in and post via CDP. "
                             "phase0: hard-expire stale pending rows + re-assign salvageable rows "
                             "to --batch-id. Prints `expired=N salvaged=M` for the orchestrator. "
                             "salvage: pull ONE salvage-eligible row (already re-assigned to "
                             "--batch-id by phase0) and write it as a discover-shape JSON to --out. "
                             "Exits 0 with a candidate, 6 if nothing salvageable.")
    parser.add_argument("--out", default=None,
                        help="Output JSON path (--phase discover, --phase draft, --phase salvage)")
    parser.add_argument("--in", dest="in_path", default=None,
                        help="Input JSON path (--phase draft, --phase post)")
    parser.add_argument("--exclude", default="", help="Comma-separated project names to exclude")
    parser.add_argument("--batch-id", dest="batch_id", default=None,
                        help="Cycle-level batch_id (e.g. rdcycle-YYYYMMDD-HHMMSS). Used by "
                             "--phase phase0 / --phase salvage / --phase discover to attribute "
                             "rows in reddit_candidates and reddit_batches. Required for "
                             "phase0 and salvage; optional for discover (defaults to a "
                             "per-discover synthetic id).")
    args = parser.parse_args()

    config = load_config()
    reddit_username = config.get("accounts", {}).get("reddit", {}).get("username", "Deep_Ad1959")

    if args.phase == "phase0":
        # Hard-expire stale pending rows + re-assign salvageable rows to the
        # current cycle's batch_id. Single advisory-lock'd transaction so two
        # concurrent cycles can't double-salvage the same row. Output is the
        # one line `expired=N salvaged=M` parsed by run-reddit-search.sh.
        if not args.batch_id:
            print("[post_reddit] ERROR: --phase phase0 requires --batch-id", file=sys.stderr)
            sys.exit(2)
        expired, salvaged = _db_phase0_salvage(args.batch_id)
        print(f"expired={expired} salvaged={salvaged}")
        return

    if args.phase == "salvage":
        # Pull up to --limit salvage-eligible rows (already re-assigned to
        # args.batch_id by phase0) from a SINGLE project and write a
        # discover-shape JSON to --out. The shell can then feed that file
        # to ripen → draft → post like a normal candidate batch.
        if not args.out:
            print("[post_reddit] ERROR: --phase salvage requires --out PATH", file=sys.stderr)
            sys.exit(2)
        if not args.batch_id:
            print("[post_reddit] ERROR: --phase salvage requires --batch-id", file=sys.stderr)
            sys.exit(2)
        salvage_limit = max(1, int(args.limit or 1))
        plan = _db_pick_salvage_candidates(args.batch_id, limit=salvage_limit)
        if not plan:
            print("[post_reddit] salvage: no eligible pending rows for this cycle")
            sys.exit(6)
        with open(args.out, "w") as f:
            json.dump(plan, f)
        urls = [d["thread_url"] for d in plan["decisions"]]
        print(f"[post_reddit] SALVAGED {plan['salvaged_count']} candidate(s) "
              f"(max attempt={plan['salvaged_attempt']}/{MAX_ATTEMPTS}) "
              f"project={plan['project_name']} urls={urls}")
        return

    if args.phase == "discover":
        if not args.out:
            print("[post_reddit] ERROR: --phase discover requires --out PATH", file=sys.stderr)
            sys.exit(2)
        if not preflight_rate_limit():
            print("[post_reddit] rate-limited, discover skipped")
            sys.exit(3)
        excluded = [x.strip() for x in args.exclude.split(",") if x.strip()]
        plan = _discover_iteration(args, config, reddit_username, excluded)
        if plan is None:
            sys.exit(4)
        with open(args.out, "w") as f:
            json.dump(plan, f)
        if plan.get("dry_run"):
            sys.exit(0)
        if plan.get("error"):
            sys.exit(5)
        if not plan.get("decisions"):
            sys.exit(6)
        return

    if args.phase == "draft":
        if not args.in_path or not os.path.exists(args.in_path):
            print(f"[post_reddit] ERROR: --phase draft requires --in PATH (got {args.in_path!r})",
                  file=sys.stderr)
            sys.exit(2)
        if not args.out:
            print("[post_reddit] ERROR: --phase draft requires --out PATH", file=sys.stderr)
            sys.exit(2)
        with open(args.in_path) as f:
            plan = json.load(f)
        if not plan.get("decisions"):
            print("[post_reddit] draft: no survivors in plan, nothing to draft")
            sys.exit(6)
        plan = _draft_iteration(plan, config, reddit_username)
        with open(args.out, "w") as f:
            json.dump(plan, f)
        if plan.get("draft_error"):
            sys.exit(5)
        if not plan.get("decisions"):
            sys.exit(6)
        return

    if args.phase == "post":
        if not args.in_path or not os.path.exists(args.in_path):
            print(f"[post_reddit] ERROR: --phase post requires --in PATH (got {args.in_path!r})", file=sys.stderr)
            sys.exit(2)
        with open(args.in_path) as f:
            plan = json.load(f)
        posted, failed = _post_iteration(plan, reddit_username)
        print(f"[post_reddit] phase=post project={plan.get('project_name')} posted={posted} failed={failed}")


if __name__ == "__main__":
    main()
