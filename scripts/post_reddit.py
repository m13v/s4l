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

from __future__ import annotations  # PEP 604 unions (str | None) for Python 3.9 launchd

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
from http_api import api_get, api_post, api_patch
from author_history_block import render as _render_author_history
from project_topics import topics_for_project
from active_experiments import collect as _collect_exps

# Honor S4L_REPO_DIR so managed-package installs resolve helper scripts inside
# the package instead of a nonexistent ~/social-autoposter (the same $HOME
# hardcode class that silently no-op'd session restore on customer boxes).
REPO_DIR = os.path.expanduser(os.environ.get("S4L_REPO_DIR") or "~/social-autoposter")
CONFIG_PATH = os.path.join(REPO_DIR, "config.json")
REDDIT_BROWSER = os.path.join(REPO_DIR, "scripts", "reddit_browser.py")
REDDIT_BROWSER_LOCK = os.path.join(REPO_DIR, "scripts", "reddit_browser_lock.py")
REDDIT_TOOLS = os.path.join(REPO_DIR, "scripts", "reddit_tools.py")
RUN_CLAUDE_SH = os.path.join(REPO_DIR, "scripts", "run_claude.sh")

# JSON schema for the queue-routed draft turn (tag post-reddit-draft ->
# claude_job.py type reddit-draft). One object, two arrays; posts[] entries
# now carry TWO independent drafts per thread (draft_a_*/draft_b_*, mirroring
# run-twitter-cycle.sh's PREP_SCHEMA) instead of a single `text` field, so the
# review card can show both and let the reviewer pick (2026-07-15). rejects[]
# entries feed _propose_excludes_from_rejects (thread_url + reason +
# proposed_excludes).
REDDIT_DRAFT_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "posts": {"type": "array", "items": {"type": "object", "properties": {
            "thread_url": {"type": "string"},
            "reply_to_url": {"type": ["string", "null"]},
            "draft_a_text": {"type": "string"},
            "draft_a_style": {"type": "string"},
            "draft_a_new_style": {"type": ["object", "null"], "properties": {
                "description": {"type": "string"},
                "example": {"type": "string"},
            }},
            "draft_b_text": {"type": "string"},
            "draft_b_style": {"type": "string"},
            "draft_b_new_style": {"type": ["object", "null"], "properties": {
                "description": {"type": "string"},
                "example": {"type": "string"},
            }},
            "thread_author": {"type": "string"},
            "thread_title": {"type": "string"},
            "search_topic": {"type": "string"},
        }, "required": ["thread_url", "draft_a_text", "draft_a_style",
                        "draft_b_text", "draft_b_style", "thread_author",
                        "thread_title"]}},
        "rejects": {"type": "array", "items": {"type": "object", "properties": {
            "thread_url": {"type": "string"},
            "reason": {"type": "string"},
            "proposed_excludes": {"type": "array", "items": {"type": "string"}},
        }, "required": ["thread_url", "reason"]}},
    },
    "required": ["posts", "rejects"],
})

# Interpreter every child subprocess must run under. A bare PYTHON resolved
# to the user's system python, which lacks the pipeline deps (Playwright and
# friends) that live only in the owned uv runtime — so on a fresh box every
# reddit_browser.py reply died (the same class as the Karol/Twitter bug,
# 2026-06-22). Honor the authoritative S4L_PYTHON pin (set by the launchd
# plist), else sys.executable (the owned interpreter the MCP launches us under).
# Never the literal PYTHON: that re-rolls the PATH dice. Re-exported so
# grandchildren inherit it.
PYTHON = os.environ.get("S4L_PYTHON") or sys.executable
os.environ["S4L_PYTHON"] = PYTHON
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
# S4L_REDDIT_MAX_SEARCHES env var without code change.
MAX_DISCOVER_SEARCHES = int(os.environ.get("S4L_REDDIT_MAX_SEARCHES", "3"))

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
    # A concurrent reddit pipeline navigated the shared harness tab out from
    # under the poster (2026-07-14: 5/5 approvals misclassified as
    # account_blocked_in_sub this way). Always retryable.
    "tab_contention",
}

# ---- posting-active flag (2026-07-14, mirrors twitter's posting-active) ----
# Stamped for the whole --phase post run and heartbeated per row. Readers:
# run-reddit-search.sh (skips a cycle fire while fresh) and reddit-backend.sh's
# pre-launch defer hook (skips every other reddit pipeline's fire), so scans
# and engagement never grab the ONE shared harness tab mid-post. A stale file
# (heartbeat older than ~120s) never blocks anyone: a killed poster must not
# wedge the fleet.
_S4L_STATE_DIR = os.path.expanduser(os.environ.get("S4L_STATE_DIR") or "~/.social-autoposter-mcp")
POSTING_ACTIVE_FILE = os.path.join(_S4L_STATE_DIR, "reddit-posting-active.json")


def _stamp_posting_active():
    try:
        os.makedirs(_S4L_STATE_DIR, exist_ok=True)
        with open(POSTING_ACTIVE_FILE, "w") as f:
            json.dump({"pid": os.getpid(), "hb": time.time()}, f)
    except Exception:
        pass


def _clear_posting_active():
    try:
        with open(POSTING_ACTIVE_FILE) as f:
            if json.load(f).get("pid") != os.getpid():
                return  # a peer poster owns the flag now; not ours to clear
    except Exception:
        pass
    try:
        os.remove(POSTING_ACTIVE_FILE)
    except Exception:
        pass

from engagement_styles import (
    VALID_STYLES, get_styles_prompt, get_content_rules, validate_or_register,
    pick_style_for_post, get_voice_relationship_rule,
)
# Audience-page routing: tells Claude which curated landing pages exist for the
# project so it can bake a deep URL (e.g. https://s4l.ai/ghostwriting) into the
# draft when the thread topic matches. See scripts/audience_pages.py + the
# landing_pages.audience_pages block in config.json.
from audience_pages import (
    prompt_block as _audience_prompt_block,
    classify_url_as_audience_page as _audience_classify_url,
)
# Learned preferences (2026-07-03): human review feedback distilled by
# feedback_digest.py into the project's learned_preferences config block.
# Rendered as an explicit prompt section here because this pipeline does not
# embed the raw project JSON. Empty string when the project has no block.
try:
    from learned_preferences import prompt_block as _learned_prefs_block
except Exception:  # never let a missing module break the poster
    def _learned_prefs_block(_project_cfg):
        return ""


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
    """INSERT a freshly-discovered candidate row via /api/v1/reddit-candidates.

    Server-side ON CONFLICT keeps the existing row's status, attempt_count,
    post linkage, AND original T0 intact (see route source); batch_id is
    updated to the current cycle so the dashboard's queue counts surface
    this run.
    """
    thread_url = (candidate.get("thread_url") or "").strip()
    if not thread_url:
        return
    try:
        score_raw = candidate.get("score")
        comments_raw = candidate.get("num_comments")
        body = {
            "thread_url": thread_url,
            "thread_author": candidate.get("thread_author"),
            "thread_title": candidate.get("thread_title"),
            "thread_selftext": candidate.get("selftext") or candidate.get("thread_selftext"),
            "subreddit": _subreddit_from_url(thread_url),
            "matched_project": project_name,
            "search_topic": candidate.get("search_topic"),
            "batch_id": batch_id,
            "draft_engagement_style": candidate.get("engagement_style"),
            "score_t0": int(score_raw) if score_raw is not None else None,
            "comments_t0": int(comments_raw) if comments_raw is not None else None,
        }
        api_post("/api/v1/reddit-candidates", body)
    except Exception as e:
        print(f"[post_reddit] WARNING: upsert candidate failed for {thread_url}: {e}",
              file=sys.stderr)


def _db_save_draft(thread_url, text, engagement_style):
    """Persist a freshly-written draft so a later salvage reuses it.

    Routes through /api/v1/reddit-candidates/by-thread-url action=save_draft.
    Returns 404 silently when there is no pending row for the URL (e.g. when
    the discover-side INSERT race hadn't completed yet); a save_draft on a
    row that already moved past 'pending' would be a no-op anyway.
    """
    if not thread_url or not text:
        return
    try:
        api_patch(
            "/api/v1/reddit-candidates/by-thread-url",
            {
                "thread_url": thread_url,
                "action": "save_draft",
                "draft_text": text,
                "draft_engagement_style": engagement_style,
            },
            ok_on_404=True,
        )
    except Exception as e:
        print(f"[post_reddit] WARNING: save_draft failed for {thread_url}: {e}",
              file=sys.stderr)


def _db_load_fresh_draft(thread_url):
    """Return (text, style) for a still-fresh draft, or (None, None).

    Calls /api/v1/reddit-candidates?thread_url=...&has_fresh_draft=true&fresh_draft_minutes=N
    so the server enforces the TTL window at the SQL level.
    """
    if not thread_url:
        return None, None
    try:
        resp = api_get(
            "/api/v1/reddit-candidates",
            query={
                "thread_url": thread_url,
                "has_fresh_draft": "true",
                "fresh_draft_minutes": DRAFT_TTL_MIN,
                "limit": 1,
            },
        )
        rows = ((resp or {}).get("data") or {}).get("candidates") or []
        if rows:
            r = rows[0]
            return r.get("draft_text"), r.get("draft_engagement_style")
    except Exception as e:
        print(f"[post_reddit] WARNING: load_fresh_draft failed for {thread_url}: {e}",
              file=sys.stderr)
    return None, None


def _db_mark_candidate_posted(thread_url, post_id):
    """Mark a candidate as successfully posted with linkage to posts.id.

    The server-side action=mark_posted runs the same two recovery layers as
    the previous Python implementation: if post_id is NULL, it first tries
    `SELECT id FROM posts WHERE thread_url=...` to recover, then falls back
    to status='failed' with last_failure_reason='log_post_returned_null'.
    See scripts/post_reddit.py CLAUDE.md commentary for the rationale.
    """
    if not thread_url:
        return
    try:
        body = {"thread_url": thread_url, "action": "mark_posted"}
        if post_id is not None:
            body["post_id"] = int(post_id)
        resp = api_patch(
            "/api/v1/reddit-candidates/by-thread-url",
            body,
            ok_on_404=True,
        )
        data = (resp or {}).get("data") or {}
        if data.get("recovery") == "marked_failed_no_post_id":
            print(
                f"[post_reddit] WARNING: log_post returned None and posts.thread_url "
                f"lookup failed for {thread_url}. Marked status='failed' to prevent "
                f"Phase 0 re-post (would dupe). Comment is live on Reddit; backfill "
                f"required for click attribution.",
                file=sys.stderr,
            )
        elif data.get("recovery") == "ok" and post_id is None:
            # Server-side recovery succeeded — log for parity with the prior
            # Python WARNING so dashboard ingestion is unchanged.
            recovered = ((data.get("candidate") or {}).get("post_id"))
            print(
                f"[post_reddit] WARNING: recovered post_id={recovered} via posts.thread_url "
                f"after log_post returned None for {thread_url}",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"[post_reddit] WARNING: mark_posted failed for {thread_url}: {e}",
              file=sys.stderr)


def _db_mark_candidate_attempt(thread_url, reason, permanent=False):
    """Record a failed post attempt via /api/v1/reddit-candidates/by-thread-url.

    Server-side action=mark_attempt mirrors the previous Python branching
    (permanent vs transient with auto-promote at MAX_ATTEMPTS).
    """
    if not thread_url:
        return
    try:
        api_patch(
            "/api/v1/reddit-candidates/by-thread-url",
            {
                "thread_url": thread_url,
                "action": "mark_attempt",
                "reason": reason,
                "permanent": bool(permanent),
                "max_attempts": MAX_ATTEMPTS,
            },
            ok_on_404=True,
        )
    except Exception as e:
        print(f"[post_reddit] WARNING: mark_attempt failed for {thread_url}: {e}",
              file=sys.stderr)


def _db_phase0_salvage(batch_id, freshness_hours=FRESHNESS_HOURS,
                       max_attempts=MAX_ATTEMPTS,
                       retry_backoff_min=RETRY_BACKOFF_MIN):
    """Phase 0 via /api/v1/reddit-candidates/phase0-salvage.

    The route runs the same single-transaction WITH _lock / expired / salvaged
    CTE that this function used to issue directly. Returns (expired, salvaged).
    """
    try:
        resp = api_post(
            "/api/v1/reddit-candidates/phase0-salvage",
            {
                "batch_id": batch_id,
                "freshness_hours": int(freshness_hours),
                "max_attempts": int(max_attempts),
                "retry_backoff_minutes": int(retry_backoff_min),
            },
        )
        data = (resp or {}).get("data") or {}
        return int(data.get("expired_count") or 0), int(data.get("salvaged_count") or 0)
    except Exception as e:
        print(f"[post_reddit] WARNING: phase0 salvage failed: {e}",
              file=sys.stderr)
        return 0, 0


def _db_pick_salvage_candidates(batch_id, limit=1):
    """Pull up to `limit` salvage-eligible rows from a SINGLE project.

    Routes through /api/v1/reddit-candidates/pick-salvage, which performs
    the same two-step (project picker + atomic claim) inside a single PG
    transaction. The route stamps last_attempt_at=NOW() at pick-time using
    FOR UPDATE SKIP LOCKED so two concurrent post phases can never re-pick
    the same row. See route source for the full SQL.

    Returns {project_name, decisions:[...], cost:0, salvaged:True, ...} or
    None if no eligible row remains.
    """
    limit = max(1, int(limit or 1))
    try:
        resp = api_post(
            "/api/v1/reddit-candidates/pick-salvage",
            {
                "batch_id": batch_id,
                "max_attempts": MAX_ATTEMPTS,
                "draft_ttl_minutes": DRAFT_TTL_MIN,
                "limit": limit,
            },
        )
        data = (resp or {}).get("data") or {}
        if not data.get("decisions"):
            return None
        return {
            "project_name": data.get("project_name") or "general",
            "decisions": data.get("decisions") or [],
            "cost": float(data.get("cost") or 0.0),
            "salvaged": bool(data.get("salvaged", True)),
            "salvaged_attempt": int(data.get("salvaged_attempt") or 0),
            "salvaged_count": int(data.get("salvaged_count") or 0),
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


# ---------------------------------------------------------------------------
# subreddit_bans audit shape (introduced 2026-05-11)
# ---------------------------------------------------------------------------
# Each entry in subreddit_bans.comment_blocked / .thread_blocked is now an
# object with the audit metadata we wished we'd been recording all along:
#   {"sub": "powerbi", "added_at": "2026-05-11T00:31:49Z",
#    "reason": "account_blocked_in_sub", "project": "WhatsApp MCP"}
#
# Pre-migration entries are bare strings; the readers/writers handle both
# shapes transparently. The migration script
# (scripts/migrate_subreddit_bans_to_objects.py) backfills existing strings to
# objects with null metadata.
#
# _ban_entry_sub(entry):   extract the sub slug from either shape (returns
#                           lowercase string or None).
# _ban_entries_to_subs(L): set of lowercase sub slugs in a ban list.
# _make_ban_entry(...):    build a fresh entry with current UTC timestamp.

def _ban_entry_sub(entry) -> str | None:
    """Return the lowercased sub slug from a ban-list entry (str or dict)."""
    if isinstance(entry, str):
        s = entry.strip().lower()
        return s or None
    if isinstance(entry, dict):
        s = (entry.get("sub") or "").strip().lower()
        return s or None
    return None


def _ban_entries_to_subs(entries) -> set[str]:
    out: set[str] = set()
    for e in entries or []:
        s = _ban_entry_sub(e)
        if s:
            out.add(s)
    return out


def _make_ban_entry(sub: str, reason: str | None, project: str | None) -> dict:
    """Build a new ban-list entry with the current UTC timestamp.

    Stamps the current Reddit account (top-level config.json reddit_account
    .username) so per-account scoping in reddit_tools._load_comment_blocked_subs
    can ignore this entry on other machines posting as a different account.
    Returns account=None if the config has no reddit_account, in which case
    the reader treats the entry as global (back-compat with pre-2026-05-15).

    Project scope (2026-05-19 cleanup): subreddit_bans.comment_blocked entries
    are ALWAYS account-level by definition: if a sub silently strips the
    comment form (or other account-triggered automod gate) for our account,
    that gate applies regardless of which project's pipeline noticed it.
    Project-specific relevance rejects live in `project_search_excludes`,
    NOT here. So we drop the `project` field semantically (kept as audit
    breadcrumb `noticed_by_project` for forensics, but the reader ignores
    it). Account is the only scope dimension.
    """
    from datetime import datetime, timezone
    account = None
    try:
        with open(CONFIG_PATH) as _f:
            account = (json.load(_f).get("reddit_account") or {}).get("username") or None
    except Exception:
        pass
    return {
        "sub": sub.strip().lower(),
        "added_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": reason or None,
        # Kept for audit (who first hit this); reader ignores. Use `account`
        # for actual scoping.
        "noticed_by_project": project or None,
        "account": account,
    }


def mark_comment_blocked(thread_url: str,
                          reason: str | None = "account_blocked_in_sub",
                          project: str | None = None) -> None:
    """Add a subreddit to config.json subreddit_bans.comment_blocked at runtime.

    Called when the bot's comment attempt is rejected (no comment form, locked,
    restricted). The sub gets blocked for future comment attempts so the
    drafter never targets it again. Thread-posting eligibility is tracked
    separately in subreddit_bans.thread_blocked.

    Records audit metadata (added_at / reason / project) on the entry.
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
        existing = _ban_entries_to_subs(blocked)
        if sub not in existing:
            blocked.append(_make_ban_entry(sub, reason, project))
            blocked.sort(key=lambda e: _ban_entry_sub(e) or "")
            with open(CONFIG_PATH, "w") as f:
                json.dump(config, f, indent=2)
                f.write("\n")
            print(f"[post_reddit] Added r/{sub} to subreddit_bans.comment_blocked "
                  f"(reason={reason!r} project={project!r})")
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


def mark_thread_blocked(subreddit: str, abort_reason: str = "",
                         project: str | None = None,
                         force: bool = False) -> None:
    """Add a subreddit to config.json subreddit_bans.thread_blocked at runtime.

    Called when a thread-post attempt is permanently blocked (account banned,
    link-only sub, text posts disabled, 403). The sub is skipped by
    pick_thread_target.py on all future runs.  Comment eligibility is tracked
    separately in subreddit_bans.comment_blocked.

    subreddit may be bare ('programming') or prefixed ('r/programming').

    Records audit metadata (added_at / reason / project) on the entry.
    The reason field captures the abort_reason verbatim (truncated to 280
    chars) so we can audit why the sub got blocked months later.

    force=True bypasses the abort_reason regex gate (used when an upstream
    signal — e.g. the model's permanent_block=true — has already decided
    this is permanent and the reason text alone wouldn't match the patterns).
    """
    sub = re.sub(r"^r/", "", subreddit, flags=re.IGNORECASE).strip().lower()
    if not sub:
        return
    if not force and abort_reason and not _abort_is_permanent_block(abort_reason):
        return
    reason_str: str | None = (abort_reason or "").strip()[:280] or None
    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        bans = config.setdefault("subreddit_bans", {})
        blocked = bans.setdefault("thread_blocked", [])
        existing = _ban_entries_to_subs(blocked)
        if sub not in existing:
            blocked.append(_make_ban_entry(sub, reason_str, project))
            blocked.sort(key=lambda e: _ban_entry_sub(e) or "")
            with open(CONFIG_PATH, "w") as f:
                json.dump(config, f, indent=2)
                f.write("\n")
            print(f"[post_reddit] Auto-blocked r/{sub} from future thread posts "
                  f"(reason={reason_str!r} project={project!r})")
        else:
            print(f"[post_reddit] r/{sub} already in thread_blocked, skipping")
    except Exception as e:
        print(f"[post_reddit] WARNING: could not persist thread-blocked sub r/{sub}: {e}")


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def pick_project(platform="reddit", exclude=None):
    try:
        cmd = [PYTHON, os.path.join(REPO_DIR, "scripts", "pick_project.py"),
               "--platform", platform, "--json"]
        if exclude:
            cmd.extend(["--exclude", ",".join(exclude)])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
    except Exception:
        pass
    return None


def get_top_performers(project_name, platform="reddit", style=None):
    """Fetch the top_performers feedback report.

    2026-05-19: optional `style` arg passes through to top_performers.py
    as --style so the per-style exemplars section gets restricted to the
    style assigned by pick_style_for_post(). When None, returns the full
    multi-style report (legacy behavior, still used in invent mode and by
    callers that have not flipped to the picker yet).
    """
    try:
        cmd = [PYTHON, os.path.join(REPO_DIR, "scripts", "top_performers.py"),
               "--platform", platform, "--project", project_name]
        if style:
            cmd.extend(["--style", style])
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
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
            [PYTHON, os.path.join(REPO_DIR, "scripts", "top_search_topics.py"),
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
            [PYTHON, os.path.join(REPO_DIR, "scripts", "top_omitted_reddit_topics.py"),
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
            [PYTHON, os.path.join(REPO_DIR, "scripts", "top_dud_reddit_queries.py"),
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


def _recent_comment_text(item):
    """Accept either str (legacy shape) or (id, content) tuple (2026-05-12
    shape) and return the content string. Lets all three prompt builders
    consume recent_comments without caring which shape they got. If
    you're refactoring the upstream shape again, update this one place."""
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        return item[1] or ""
    return item or ""


def _strip_active_suffixes(text, active_campaigns):
    """Remove any active-campaign suffix from `text` (idempotent, trailing-only).

    Mirrors engage_reddit.strip_active_suffixes (commit 8cdde18) so we have
    the same protection for the post_reddit drafting path. Without this,
    `get_recent_comments()` feeds the LLM prior `posts.our_content` rows
    that already end in the campaign suffix (e.g. " written with s4lai"),
    the LLM copies the literal suffix into its draft because it looks like
    part of our voice, and the tool-level append at line ~2092 stacks a
    SECOND suffix on top. Observed in production 2026-05-18 on Deep_Ad1959
    (reply rows 70412 + 70413) via engage_reddit; same risk exists here.

    Strips trailing suffix repeatedly so a historically-doubled row also
    collapses to clean text. Active campaign list is passed in by the
    caller so we only strip patterns we're actively using (avoids
    unbounded false-positive matches on incidental phrasing).
    """
    if not text or not active_campaigns:
        return text
    cleaned = text.rstrip()
    changed = True
    while changed:
        changed = False
        for camp in active_campaigns:
            suffix = (camp.get("suffix") or "").strip()
            if suffix and cleaned.endswith(suffix):
                cleaned = cleaned[: -len(suffix)].rstrip()
                changed = True
    return cleaned


def get_recent_comments(limit=5):
    """Recent Reddit posts.our_content via /api/v1/posts.

    Returns list of (id, content) tuples (2026-05-12 change). The IDs
    feed into the generation_trace audit blob so a later reader can
    JOIN back to the source posts; the content still feeds the prompt
    builders verbatim. Prompt-builders below were updated to accept
    both the old (str) and new (tuple) shapes so any straggler caller
    keeps working without a coordinated change.

    2026-05-18: active-campaign suffixes are stripped from `our_content`
    BEFORE returning, so the LLM never sees suffixed exemplars and
    cannot copy the campaign tag into its draft (which would then get
    a SECOND tool-level append, producing "written with s4lai written
    with s4lai"). See `_strip_active_suffixes` docstring.
    """
    resp = api_get(
        "/api/v1/posts",
        query={"platform": "reddit", "limit": int(limit)},
    )
    rows = ((resp or {}).get("data") or {}).get("posts") or []
    raw = [
        (int(r["id"]), r.get("our_content") or "")
        for r in rows
        if r.get("our_content") and r.get("id") is not None
    ]
    # Sanitize exemplars against the currently-active campaign suffixes.
    # If the campaign-load call fails we fall back to raw content (better
    # than crashing the discover/draft pipeline over a degraded API call).
    try:
        active_camps = load_active_reddit_campaigns()
    except Exception as e:
        print(f"[post_reddit] WARNING: load_active_reddit_campaigns failed "
              f"during recent_comments sanitize ({e}); returning raw content",
              file=sys.stderr)
        return raw
    cleaned = []
    for pid, content in raw:
        stripped = _strip_active_suffixes(content, active_camps)
        if stripped:
            cleaned.append((pid, stripped))
    return cleaned


def load_active_reddit_campaigns():
    """Active Reddit campaigns that carry a literal suffix.

    Tool-level enforcement: the LLM never sees these. We append the suffix to
    the drafted text in Python before posting, so the literal text is
    guaranteed to land on Reddit. sample_rate gates the per-post coin flip
    for concurrent A/B (e.g. 0.5 = ~half of posts get tagged).

    Calls /api/v1/campaigns?status=active&platform=reddit&has_suffix=true&with_budget_remaining=true.
    """
    resp = api_get(
        "/api/v1/campaigns",
        query={
            "status": "active",
            "platform": "reddit",
            "has_suffix": "true",
            "with_budget_remaining": "true",
            "limit": 500,
        },
    )
    rows = ((resp or {}).get("data") or {}).get("campaigns") or []
    return [
        {
            "id": int(r["id"]),
            "suffix": r.get("suffix"),
            "sample_rate": float(r.get("sample_rate") if r.get("sample_rate") is not None else 1.0),
        }
        for r in rows
    ]


def _angle_str(v):
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        return "; ".join(f"{k}: {_angle_str(x)}" for k, x in v.items() if x)
    if isinstance(v, (list, tuple)):
        return ", ".join(_angle_str(x) for x in v if x)
    return str(v) if v else ""


def build_content_angle(project, config):
    """Prefer project-specific positioning over the global config angle.

    Always appends the project's audience-pages block (when configured) so the
    draft prompt knows which curated landing pages it should link to for
    topic-matched threads. Single source of truth flows through every caller
    that consumes content_angle.
    """
    if project.get("content_angle"):
        base = project["content_angle"]
    else:
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

        base = " ".join(parts) if parts else config.get("content_angle", "")

    try:
        ap_block = _audience_prompt_block(project.get("name") or "")
    except Exception:
        ap_block = ""
    if ap_block:
        return (base + "\n\n" + ap_block).strip() if base else ap_block.strip()
    return base


def build_discover_prompt(project, config, limit, top_report, recent_comments,
                          top_topics_report="", dud_queries_report="",
                          omitted_topics_report=""):
    """DISCOVER phase: scan-only. Model picks search queries, runs them in
    OPAQUE mode (never sees thread content), outputs DONE. No fetching, no
    judging, no drafting. The dump_dir harvest converts raw search results
    into candidates passed to ripen.

    Mirrors Twitter's scan phase: the only Claude work here is choosing
    search queries. Style picking, top_performers filtering, and the
    actual comment drafting all happen in the draft phase (the only
    Claude call in this cycle that writes a comment).
    """
    content_angle = build_content_angle(project, config)
    topics_list = list(topics_for_project(project.get("name") or ""))
    project_json = json.dumps({
        "name": project.get("name"),
        "description": project.get("description"),
        "search_topics": topics_list,
    }, indent=2)

    recent_ctx = ""
    if recent_comments:
        # _recent_comment_text handles both legacy str and current (id, content) shapes.
        snippets = "\n".join(
            f"  - {_recent_comment_text(c)}"
            for c in recent_comments
            if _recent_comment_text(c)
        )
        recent_ctx = f"\nYour last {len(recent_comments)} comments (don't repeat these threads):\n{snippets}\n"

    top_ctx = ""
    if top_report:
        lines = top_report.split("\n")[:20]
        top_ctx = f"\n## Past performance feedback:\n{chr(10).join(lines)}\n"

    top_topics_ctx = ""
    if top_topics_report:
        top_topics_ctx = (
            "\n## Past top-performing search topics "
            "(sorted by clicks DESC first, then composite-scored: "
            "clicks*100 + comments + upvotes). "
            "CLICKS ARE THE PRIORITY SIGNAL. Any topic with `clicks > 0` is "
            "GOLD TIER, clicks are the only metric that proves our reply drove "
            "someone to actually visit the project's link. Comments and upvotes "
            "are vanity. If a project in your draft set has a gold-tier topic "
            "in this list, mimic ITS framing (subreddit fit, keyword cluster, "
            "specificity) FIRST before falling back to other styles. The "
            "Δpost / Δskip columns also matter: high Δskip + few posts = the "
            "topic surfaces alive but off-topic threads (reword more narrowly); "
            "low Δskip + few posts = dead supply (drop the topic). Optimize the "
            "entire pipeline for clicks; everything else is leading indicators.\n"
            f"{top_topics_report}\n"
        )

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


def _truncate(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n] + "..."


def _prefetch_thread_digests(candidates, reddit_username=None):
    """Pre-fetch thread content for the draft prompt (2026-07-14).

    The draft turn used to be agentic: Claude ran `reddit_tools.py fetch` via
    the Bash tool per candidate. Queue routing (claude_job.py, tag
    post-reddit-draft) only carries pure text->JSON turns, so the fetches now
    happen HERE, in Python, and the digests are inlined into the prompt —
    the same tool-free conversion the twitter Phase 2b prep got on 2026-06-26.
    Same access pattern as before (reddit_tools fetch rides the harness
    Chrome's CDP, which is multi-client safe), just a different caller.

    Returns (digests, dropped): digests maps thread_url -> digest text for
    candidates worth drafting; dropped is a list of (candidate, reason) for
    threads that can no longer be posted to (archived/locked/blocked) so the
    caller marks them permanently failed. Transient fetch failures land in
    NEITHER (candidate stays pending; Phase 0 salvage retries next cycle).
    """
    digests = {}
    dropped = []
    our_name = (reddit_username or "").lower()
    for c in candidates:
        url = c.get("thread_url") or ""
        if not url:
            continue
        try:
            proc = subprocess.run(
                [PYTHON, REDDIT_TOOLS, "fetch", url],
                capture_output=True, text=True, timeout=90,
            )
            data = json.loads((proc.stdout or "").strip() or "{}")
        except Exception as e:
            print(f"[post_reddit] prefetch FAILED (transient) {url}: {e}",
                  file=sys.stderr, flush=True)
            continue
        err = data.get("error")
        if err:
            if err in ("thread_archived", "thread_locked", "subreddit_blocked"):
                dropped.append((c, err))
            else:
                print(f"[post_reddit] prefetch error (transient) {url}: {err}",
                      file=sys.stderr, flush=True)
            continue
        thread = data.get("thread") or {}
        comments = data.get("comments") or []
        lines = [
            f"subreddit: {thread.get('subreddit', '')}  score: {thread.get('score', 0)}"
            f"  comments: {thread.get('num_comments', 0)}",
            f"OP u/{thread.get('author', '')}: {thread.get('title', '')}",
        ]
        selftext = (thread.get("selftext") or "").strip()
        if selftext:
            lines.append(_truncate(selftext, 1500))
        already = False
        if comments:
            lines.append("top comments:")
            for cm in comments[:12]:
                author = cm.get("author") or ""
                if our_name and author.lower() == our_name:
                    already = True
                body = _truncate((cm.get("body") or "").strip().replace("\n", " "), 280)
                lines.append(f"  u/{author} ({cm.get('score', 0)}): {body}")
        if already:
            lines.append("NOTE: one of OUR accounts already commented in this"
                         " thread (astroturf OMIT rule applies).")
        digests[url] = "\n".join(lines)
    return digests, dropped


def build_draft_prompt(project, config, candidates, top_report, recent_comments,
                       style_assignment_a=None, style_assignment_b=None, digests=None):
    """DRAFT phase: write comments only for ripen-survivors.

    `candidates` is the list of decisions that passed the delta gate, each
    annotated with ripen data (delta_up, delta_comments, composite). Thread
    content is pre-fetched by _prefetch_thread_digests and inlined per
    candidate (tool-free turn since 2026-07-14; the model must not fetch).

    2026-05-19: `style_assignment_a`/`style_assignment_b` are two independent
    pick_style_for_post() results the draft phase picks up front, so the
    model enforces the SAME two assignments instead of free-picking (and
    overwhelmingly defaulting to pattern_recognizer).

    2026-07-15: two independent drafts (A/B, mirroring run-twitter-cycle.sh's
    Draft A/Draft B) are requested per thread instead of one, so the review
    card can show both and the reviewer picks. Card rendering and the
    edit-learning digest already handle a 2-element `drafts[]` generically
    (built for twitter); this just needs to populate it.
    """
    content_angle = build_content_angle(project, config)

    recent_ctx = ""
    if recent_comments:
        # _recent_comment_text handles both legacy str and current (id, content) shapes.
        snippets = "\n".join(
            f"  - {_recent_comment_text(c)}"
            for c in recent_comments
            if _recent_comment_text(c)
        )
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
        history_line = ""
        try:
            _hb = _render_author_history(
                "reddit", c.get("thread_author") or "", days=30, limit=5
            )
            if _hb:
                history_line = "\n    " + _hb.replace("\n", "\n    ")
        except Exception:
            pass
        digest_block = ""
        if digests and c.get("thread_url") in digests:
            _d = digests[c["thread_url"]]
            digest_block = "\n    THREAD CONTENT:\n      " + _d.replace("\n", "\n      ")
        candidate_lines.append(
            f"  - {c['thread_url']}{delta_info}\n"
            f"    title: {c.get('thread_title', '')}\n"
            f"    suggested style: {c.get('engagement_style', '')}"
            f"{history_line}{digest_block}"
        )
    candidates_block = "\n".join(candidate_lines)

    return f"""You will be handed up to {len(candidates)} Reddit thread(s) that survived the engagement-velocity (ripen) gate. Your job is to draft TWO independent comments (Draft A and Draft B, one under Draft A's assigned style and one under Draft B's assigned style below) for the ones where you can write something genuinely useful to that audience. Do not judge or rank them, the reviewer reads both and picks. Lean toward DRAFTING when the audience overlaps even partially with the project's user, and only OMIT on clear no-bridge cases.

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

## THREAD CONTENT (pre-fetched)
Each candidate above carries a THREAD CONTENT block (OP + top comments), already fetched for you. You have NO tools this session: do NOT fetch, search, or browse anything. Apply the SELECTION GATE using the inlined content only. If a candidate is somehow missing its THREAD CONTENT block, OMIT it.

## CRITICAL CONTENT RULES (apply only to threads that pass the gate)
- Go BIMODAL on length: 1 punchy sentence (<100 chars) OR 4-5 sentences of real substance. Avoid 2-3 sentence middle-ground.
- GROUNDING RULE — pick ONE lane per comment:
  LANE 1 - DISCLOSED STORY: open with a hedge ("hypothetically", "imagine someone running this", "scenario:") then you may invent specifics freely.
  LANE 2 - NO FABRICATION: every specific (numbers, durations, places, tools) must appear verbatim in the content_angle above. Otherwise drop the specific and pattern-frame ("the part that breaks down is...", "the typical failure mode is...").
- VOICE RELATIONSHIP: see the dedicated section below; it governs whether you speak AS the maker or as an outside observer.
- NEVER mention product names (fazm, assrt, pieline, cyrano, terminator, mk0r, s4l).
- NEVER include URLs or links in your comment text.
- Prefer replying to OP (top-level reply). ONE comment per thread.
- Statements beat questions. Be authoritative, not inquisitive.

## Content rules
{get_content_rules("reddit")}

## DRAFT A: assigned style
{get_styles_prompt("reddit", context="posting", assignment=style_assignment_a)}

## DRAFT B: assigned style
{get_styles_prompt("reddit", context="posting", assignment=style_assignment_b)}

{_learned_prefs_block(project)}
{get_voice_relationship_rule()}

## OUTPUT FORMAT
Return ONE JSON object with two arrays (a JSON schema is enforced on this session). Both draft_a_text and draft_b_text are REQUIRED for every posts[] entry — write both, under their respective assigned styles above, applying the CRITICAL CONTENT RULES and Content rules to each independently:

{{"posts": [...], "rejects": [...]}}

Each posts[] entry is one thread that PASSES the SELECTION GATE:
{{"thread_url": "SAME_URL_AS_GIVEN", "reply_to_url": null, "draft_a_text": "your Draft A comment here", "draft_a_style": "{(style_assignment_a or {}).get('style') or 'style_name'}", "draft_a_new_style": null, "draft_b_text": "your Draft B comment here", "draft_b_style": "{(style_assignment_b or {}).get('style') or 'style_name'}", "draft_b_new_style": null, "thread_author": "username", "thread_title": "thread title", "search_topic": "the seed concept"}}

For threads that FAIL the gate, simply leave them out of posts. The shell handles unhandled candidates correctly (Phase 0 salvage on the next cycle re-checks them, and one-strike ripen failure has already pruned dead threads). When nothing passes, return {{"posts": [], "rejects": []}}.

## OPTIONAL: rejects[] (self-improving denylist)
When you OMIT a thread because of a recurring CLASS of false-positive (the SUB itself surfaces wrong-audience threads, not just this one thread), you MAY add a rejects[] entry for that thread:

{{"thread_url": "SAME_URL_AS_GIVEN", "reason": "short reason", "proposed_excludes": ["subreddit:bestofredditorupdates"]}}

Rules:
- proposed_excludes entries MUST use the typed form `subreddit:<slug>` (lowercase, no `r/` prefix). Future shape: `keyword:<word>` is accepted but unused today.
- DO emit when: the false-positive is structural — e.g. r/bestofredditorupdates is family drama matching on the word "alternative"; r/hfy is sci-fi narrative matching on the word "spaced"; r/superstonk is GME meme stock matching on "anki" via a random comment. The SUB is the false positive, not just this one post.
- DO NOT emit when: this specific thread is bad but the sub is fine in general (e.g. r/{project.get('name', 'project')}'s natural audience like r/medicalschool, r/anki, r/getstudying — never propose excluding a top-performing sub).
- Activation gate: a term needs >=2 SEPARATE batches to propose it before it goes live on future Reddit searches. A single mistaken proposal cannot mute a sub. Propose if a thoughtful future cycle would likely agree; otherwise omit.
- 1-3 entries per reject is plenty. When in doubt, omit the field. Default (no reject line) is safe.

Examples of GOOD proposals:
- Reject r/bestofredditorupdates "Husband lied" → ["subreddit:bestofredditorupdates"]
- Reject r/hfy "The Trial of Humanity" → ["subreddit:hfy"]
- Reject r/battlefield6 "GAME UPDATE 1.3.1.0" → ["subreddit:battlefield6"]
- Reject r/superstonk "GMERICA acquisition" → ["subreddit:superstonk"]
- Reject r/nosleep "cursed doll" → ["subreddit:nosleep"]

Examples of WRONG proposals (do not emit):
- Reject a specific r/nursing thread because OP is venting → DO NOT exclude r/nursing (it's our target audience; just omit this thread)
- Reject one r/anki thread that's off-topic → DO NOT exclude r/anki (core ICP)

Do NOT narrate. Gate, draft-or-reject, return the single JSON object.
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

    # DB-backed search_topics (post 2026-05-27 config.json removal).
    topics_list = list(topics_for_project(project.get("name") or ""))

    project_json = json.dumps({
        "name": project.get("name"),
        "description": project.get("description"),
        "search_topics": topics_list,
    }, indent=2)

    recent_ctx = ""
    if recent_comments:
        # _recent_comment_text handles both legacy str and current (id, content) shapes.
        snippets = "\n".join(
            f"  - {_recent_comment_text(c)}"
            for c in recent_comments
            if _recent_comment_text(c)
        )
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
## Past top-performing search topics (sorted by clicks DESC first, then composite-scored: clicks*100 + comments + upvotes)
CLICKS ARE THE PRIORITY SIGNAL. Any topic with `clicks > 0` is GOLD TIER, clicks
are the only metric that proves our reply drove someone to actually visit the
project's link. Comments and upvotes are vanity. If a project in your draft set
has a gold-tier topic in this list, mimic ITS framing (subreddit fit, keyword
cluster, specificity) FIRST before falling back to other styles. The Δpost /
Δskip columns also matter: high Δskip + few posts = topic surfaces alive but
off-topic threads (reword more narrowly); low Δskip + few posts = dead supply
(drop the topic). Optimize the entire pipeline for clicks; everything else is
leading indicators.

{top_topics_report}

If none of the top topics match this run's angle, pick any seed from the
project's search_topics list. New topics with 0 clicks are fine — we still need
to explore — but a gold-tier topic that fits should beat any unproven topic.
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
  LANE 2 - NO FABRICATION: every specific (numbers, durations, places, course names, headcount,
  named tools) must appear verbatim in this project's content_angle / voice / messaging in
  config.json. Otherwise drop the specific or pattern-frame ("the part that breaks down is...",
  "the typical failure mode is...").
Never present an invented specific as a personal first-hand claim without a Lane 1 opener.
VOICE RELATIONSHIP: see the dedicated section below; it governs whether you speak AS the maker or as an outside observer.
{recent_ctx}{top_ctx}{top_topics_ctx}{dud_queries_ctx}
{get_styles_prompt("reddit", context="posting", assignment=style_assignment)}

{_learned_prefs_block(project)}
{get_voice_relationship_rule()}

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
- GROUNDING has TWO valid forms. Lane 1: open with a disclosure phrase ("hypothetically", "imagine someone running this", "scenario:") and then invent freely. Lane 2: every specific (numbers/places/programs) must be grounded in content_angle/voice/messaging in config.json, or drop the specific and pattern-frame ("the part that breaks down is...", "the typical failure mode is..."). Never present an invented specific as a personal first-hand claim without a Lane 1 opener.
- VOICE: see the VOICE RELATIONSHIP section below; it governs whether you speak AS the maker or as an outside observer based on the matched project's voice_relationship field.
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


def run_claude_structured(prompt, timeout=1200):
    """Run the draft turn through scripts/run_claude.sh, tag post-reddit-draft.

    The tag is mapped in claude_job.py TAG_TO_TYPE, so run_claude.sh routes it
    through the file job queue and the s4l-worker scheduled task performs the
    LLM turn (mapped tags deliberately have NO claude -p fallback: on a box
    with no live worker the provider times out with exit 79 and this phase
    skips, exactly like the twitter draft lane when Claude Desktop is closed).
    run_claude.sh also owns session accounting (claude_sessions cost rows); it
    honors our pre-set CLAUDE_SESSION_ID so posts keep their attribution.

    Returns (ok, result, usage): result is the parsed structured_output dict
    (posts/rejects per REDDIT_DRAFT_SCHEMA), or raw text when the envelope
    carried an unparseable result (caller falls back to the legacy JSONL
    regex parsers), or an error string when ok is False.
    """
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0,
             "cache_create": 0, "cost_usd": 0.0}
    session_id = str(uuid.uuid4())
    usage["session_id"] = session_id
    # Inherited by run_claude.sh (which honors a caller-set session id) and by
    # the log_post -> reddit_tools.py chain for posts.claude_session_id.
    os.environ["CLAUDE_SESSION_ID"] = session_id
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)  # claude uses OAuth, not API key
    cmd = ["bash", RUN_CLAUDE_SH, "post-reddit-draft", "-p",
           "--output-format", "json", "--json-schema", REDDIT_DRAFT_SCHEMA]
    try:
        proc = subprocess.run(cmd, input=prompt, env=env, text=True,
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT", usage
    # run_claude.sh narrates on stderr (queue enqueue, quota stamps); forward
    # into the run log so cycle debugging keeps its trail.
    for ln in (proc.stderr or "").splitlines():
        print(f"[post_reddit] {ln[:300]}", file=sys.stderr, flush=True)
    text = (proc.stdout or "").strip()
    if proc.returncode != 0:
        # exit 79 = queue provider timeout (no live worker) — the standard
        # claude-blocked skip code; the caller records draft_error and the
        # candidates stay pending for the next cycle.
        return False, (text[:2000] or f"exit {proc.returncode}"), usage
    try:
        envlp, _ = json.JSONDecoder().raw_decode(text)
    except Exception as e:
        return False, f"envelope parse error: {e}: {text[:500]}", usage
    try:
        usage["cost_usd"] = float(envlp.get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        pass
    so = envlp.get("structured_output")
    if so is None:
        so = envlp.get("result")
    if isinstance(so, str):
        try:
            so = json.loads(so)
        except (json.JSONDecodeError, TypeError):
            pass  # caller regex-parses the raw text
    return True, so, usage


def run_claude(prompt, timeout=600):
    """Run claude -p in bare mode with Bash tool only (no MCP needed).

    DEPRECATED 2026-07-14: the draft phase (the only caller) moved to
    run_claude_structured, which routes through run_claude.sh -> the
    claude_job.py queue (tag post-reddit-draft) so the s4l-worker scheduled
    task performs the turn. Kept for reference and emergency manual use only;
    do NOT wire new callers to this (it bypasses session cost accounting and
    the queue seam).

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
            log_args = [PYTHON, os.path.join(REPO_DIR, "scripts", "log_claude_session.py"),
                 "--session-id", session_id, "--script", "post_reddit"]
            orch_cost = usage.get("cost_usd")
            if isinstance(orch_cost, (int, float)) and orch_cost > 0:
                log_args.extend(["--orchestrator-cost-usd", str(orch_cost)])
            subprocess.run(log_args, capture_output=True, text=True, timeout=30)
        except Exception as e:
            print(f"[post_reddit] WARNING: log_claude_session failed: {e}", file=sys.stderr)
        return proc.returncode == 0, text_output + stderr_out, usage
    except Exception as e:
        return False, str(e), usage


def _acquire_browser_lease(timeout: int = 600, ttl: int = 90):
    """Acquire the reddit-browser lease for THIS row's CDP work.

    Per-post acquire (not per-cycle, per-phase) is the load-bearing migration
    shipped 2026-05-13. Before this change, run-reddit-search.sh held the
    lease around the entire `--phase post` invocation, so a 10-row salvage
    batch monopolised the browser for ~30 min (10 × ~45s post + 9 × 180s
    between-post sleep) while peers (link-edit-reddit, dm-outreach-reddit,
    engage-reddit, engage-dm-replies-reddit) sat blocked. Pushing acquire/
    release down to per-row means lease is only held during the actual CDP
    posting work (~45s incl. retries), and the 3-min between-post sleeps
    happen unlocked.

    The MCP wrapper's auto-heartbeat (PreToolUse/PostToolUse hooks bumping
    `expires_at`) keeps the lease alive during real browser activity, so no
    manual heartbeat is needed here. Default TTL of 90s leaves enough headroom
    for post_via_cdp's 5-attempt retry loop with internal sleeps.

    Returns (ok: bool, msg: str). msg is the helper's last stdout line on
    success, or BUSY/ERROR diagnostic on failure.
    """
    try:
        r = subprocess.run(
            [PYTHON, REDDIT_BROWSER_LOCK, "acquire",
             "--timeout", str(timeout), "--ttl", str(ttl)],
            capture_output=True, text=True, timeout=timeout + 30,
        )
        out_lines = [ln for ln in (r.stdout or "").strip().splitlines() if ln]
        last = out_lines[-1] if out_lines else ""
        if r.returncode == 0 and last.startswith("OK"):
            return True, last
        return False, last or (r.stderr or "").strip()[:200] or f"rc={r.returncode}"
    except subprocess.TimeoutExpired:
        return False, "subprocess_timeout"
    except Exception as e:
        return False, f"exception:{e}"


def _release_browser_lease() -> None:
    """Release the reddit-browser lease. Idempotent (NOT_HELD is fine).

    Always called in a `finally` so peers can acquire during the 3-min
    between-post sleep even if post_via_cdp raised. The lease auto-decays
    after 90s of idleness anyway (no MCP heartbeats while we're sleeping),
    but explicit release frees peers immediately.
    """
    try:
        subprocess.run(
            [PYTHON, REDDIT_BROWSER_LOCK, "release"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        pass


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
            cmd = [PYTHON, REDDIT_BROWSER, "reply" if reply_to_url else "post-comment", target, text]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            cdp_out = proc.stdout.strip()
            if not cdp_out:
                # Full stderr (was [:200] until 2026-05-14; truncation hid the
                # actual exception class/message, leaving cdp_no_response
                # failures undiagnosable in postmortems).
                _stderr_full = proc.stderr or ""
                print(f"[post_reddit] CDP attempt {attempt + 1}: no stdout. stderr:\n{_stderr_full}")
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


def log_post(thread_url, permalink, text, project_name, thread_author, thread_title, reddit_username, engagement_style=None, search_topic=None, generation_trace_path=None, link_source=None):
    """Log a successful post to the database. Returns the new post_id, or None.

    generation_trace_path (2026-05-12): optional path to a JSON file with
    the few-shot context Claude saw before drafting (top_performers
    report, recent comments, top_search_topics). Forwarded to
    reddit_tools.py as --generation-trace and stored in
    posts.generation_trace JSONB. File-based (not inline) to keep argv
    short. Same trace blob is reused for every post produced from this
    Claude draft, since they all share the same few-shot context.

    link_source (2026-05-17): optional string written to posts.link_source so
    the dashboard can break out audience-page traffic (e.g.
    'audience_page:founder-ghostwriting') from generic homepage links. Set by
    the post loop after URL wrapping based on which curated landing page
    (if any) Claude baked into the reply text.
    """
    try:
        cmd = [PYTHON, REDDIT_TOOLS, "log-post",
             thread_url, permalink or "", text, project_name,
             thread_author, thread_title,
             "--account", reddit_username]
        if engagement_style:
            cmd.extend(["--engagement-style", engagement_style])
        if search_topic:
            cmd.extend(["--search-topic", search_topic])
        if generation_trace_path:
            cmd.extend(["--generation-trace", generation_trace_path])
        if link_source:
            cmd.extend(["--link-source", link_source])
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
                [PYTHON, bump,
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


def parse_reject_decisions(output):
    """Extract action='reject' JSON lines from the draft prompt (2026-05-11).

    Reject lines may carry a `proposed_excludes` array of typed exclude terms
    (`subreddit:<slug>` or `keyword:<word>`). These get fed to
    project_excludes.propose() so the 2-batch activation gate accumulates
    them without auto-trusting a single false rejection. The "thread itself
    is bad" reasons (no proposed_excludes) are still parsed for audit but
    have no side effect on the denylist.

    Multiline-safe regex (the `proposed_excludes` array may contain commas
    and span lines if Claude pretty-prints). Each JSON parse failure is
    silently dropped — the JSON shape stamp `"action":"reject"` is the only
    discriminator, so reject lines that don't parse are simply ignored.
    """
    rejects = []
    seen_urls = set()
    for match in re.finditer(
        r'\{[^{}]*?"action"\s*:\s*"reject"[^{}]*?\}',
        output, flags=re.DOTALL,
    ):
        try:
            r = json.loads(match.group())
            url = r.get("thread_url", "")
            if not url or url in seen_urls:
                continue
            rejects.append(r)
            seen_urls.add(url)
        except (json.JSONDecodeError, TypeError):
            continue
    return rejects


def _propose_excludes_from_rejects(rejects, project_name, batch_id, candidates_by_url):
    """Forward Claude-proposed excludes into project_search_excludes (reddit).

    Mirrors the twitter cycle's behavior at run-twitter-cycle.sh:929-966:
    each proposed term is normalize/validated by project_excludes.propose()
    against the platform's allowed kinds and the project's reserved-keyword
    list. The activation gate (>=2 distinct batch_ids) is enforced inside
    propose(); a single false-rejection in this cycle cannot mute a sub.

    Best-effort: import / DB failures are logged once and the post pipeline
    continues. The propose() side effect is not on the critical path for
    posting; if it dies, the only consequence is that we don't accumulate
    new exclude proposals this cycle.

    Returns a dict with counters for logging.
    """
    if not rejects or not project_name:
        return {"rejects_seen": len(rejects or []), "proposed": 0,
                "inserted": 0, "bumped": 0, "rejected": 0, "active_now": 0}
    counters = {"rejects_seen": len(rejects), "proposed": 0,
                "inserted": 0, "bumped": 0, "rejected": 0, "active_now": 0}
    try:
        scripts_dir = os.path.dirname(os.path.abspath(__file__))
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import project_excludes as pe
    except Exception as e:
        print(f"[post_reddit] WARN: project_excludes import failed: {e}",
              file=sys.stderr, flush=True)
        return counters

    for r in rejects:
        url = r.get("thread_url") or ""
        terms = r.get("proposed_excludes") or []
        if not isinstance(terms, list):
            continue
        reason = (r.get("reason") or "")[:500]
        cand = candidates_by_url.get(url) or {}
        # candidate_id is the reddit_candidates.id for audit purposes; falls
        # back to None when the candidate object doesn't carry it through.
        cand_id = cand.get("id") or cand.get("candidate_id")
        for t in terms[:5]:  # cap so a runaway prompt can't spam the table
            counters["proposed"] += 1
            try:
                out = pe.propose(
                    "reddit", project_name, t,
                    candidate_id=cand_id,
                    batch_id=batch_id,
                    reason=reason or None,
                )
            except Exception as e:
                print(f"[post_reddit] WARN: propose failed term={t!r}: {e}",
                      file=sys.stderr, flush=True)
                counters["rejected"] += 1
                continue
            action = out.get("action") or ""
            if not out.get("ok"):
                counters["rejected"] += 1
            elif action == "inserted":
                counters["inserted"] += 1
            elif action in ("bumped", "duplicate_batch"):
                counters["bumped"] += 1
            if out.get("active"):
                counters["active_now"] += 1
    return counters


# Stopwords stripped before computing query<->thread topical overlap. Kept small
# and generic: these are the high-frequency English glue words that cause the
# Reddit `sort=relevance` leak (a chatty natural-language query like "claude
# artifacts built me a little tool to track my habits" matches an unrelated BORU
# thread purely on shared words like "me", "to", "my", "a", "little"). We do NOT
# strip domain words here — only structural filler — so the surviving overlap is
# a real topical signal.
_OVERLAP_STOPWORDS = frozenset("""
a an the and or but if then else of to in on at for with without from by about into
over under again further is are was were be been being am do does did doing have has
had having i me my myself we our ours you your yours he him his she her it its they
them their this that these those what which who whom whose how when where why all any
both each few more most other some such no nor not only own same so than too very can
will just dont don't should now get got make made want need like really actually
something someone anyone everyone thing things stuff lot lots little bit kind sort
""".split())

# Token must be >=3 chars to count toward overlap (drops "ai" etc.? no — keep 2+
# but exclude pure stopwords). We use 2 to keep short domain tokens like "db", "os".
_OVERLAP_MIN_LEN = 2


def _overlap_tokens(text):
    """Lowercase alphanumeric tokens of length >= _OVERLAP_MIN_LEN, minus stopwords."""
    if not text:
        return set()
    toks = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in toks if len(t) >= _OVERLAP_MIN_LEN and t not in _OVERLAP_STOPWORDS}


def _topical_overlap(query, title, selftext):
    """Fraction of distinct content tokens in `query` that also appear in the
    thread's title+selftext. 0.0 = no shared topical token (likely relevance-sort
    garbage), 1.0 = every query content word is present in the thread.

    This is a *soft signal* used only to rank/prioritize candidates, never to hard-
    drop them — per the conservative directive, we isolate + surface the garbage
    rather than silently filtering it.
    """
    q = _overlap_tokens(query)
    if not q:
        return 0.0
    body = _overlap_tokens((title or "") + " " + (selftext or ""))
    if not body:
        return 0.0
    return len(q & body) / len(q)


def _discover_iteration(args, config, reddit_username, already_picked):
    """DISCOVER phase: search and select threads. No drafting.

    Returns {project_name, decisions: [candidates], cost, session_id} where
    each candidate has thread_url, title, author, search_topic but NO text
    field (drafting happens in the draft phase). cost is always 0.0 and
    session_id None: as of 2026-06-01 discover is fully programmatic (Python
    builds the query bank and runs reddit_tools.cmd_search directly; no Claude
    session). Uses `decisions` key for downstream-phase compatibility.
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

    # 2026-05-11: surface the per-project sub denylist for visibility in run
    # logs (twitter cycle does the equivalent at run-twitter-cycle.sh:410).
    # The actual *enforcement* happens server-side in reddit_tools._load_
    # comment_blocked_subs via the S4L_REDDIT_PROJECT env var set below.
    # mark_used stamps last_used_at on every active term so decay (60d
    # unused → prune) only fires on terms that truly stopped contributing.
    try:
        scripts_dir = os.path.dirname(os.path.abspath(__file__))
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import project_excludes as _pe
        _split = _pe.active_excludes_by_kind("reddit", project_name)
        _active_subs = _split.get("subreddit") or []
        _active_kws = _split.get("keyword") or []
        if _active_subs or _active_kws:
            _sub_preview = ",".join(_active_subs[:8]) + ("..." if len(_active_subs) > 8 else "")
            _kw_preview = ",".join(_active_kws[:8]) + ("..." if len(_active_kws) > 8 else "")
            print(
                f"[project_excludes] platform=reddit project={project_name} "
                f"active_subs={len(_active_subs)} active_keywords={len(_active_kws)} "
                f"subs=[{_sub_preview}] keywords=[{_kw_preview}]"
            )
            # Stamp last_used_at so decay doesn't prune still-live terms.
            # mark_used wants the FULL typed-term form (subreddit:foo).
            _full_terms = (
                [f"subreddit:{s}" for s in _active_subs]
                + [f"keyword:{k}" for k in _active_kws]
            )
            try:
                _pe.mark_used("reddit", project_name, _full_terms)
            except Exception as e:
                print(f"[project_excludes] WARN: mark_used failed: {e}", file=sys.stderr)
    except Exception as e:
        # Visibility-only path. Never fail discover because of it.
        print(f"[project_excludes] WARN: active-excludes log failed: {e}", file=sys.stderr)

    # 2026-06-01: discover is now FULLY PROGRAMMATIC (no Claude session).
    # Previously discover burned an entire Claude session in OPAQUE mode just
    # to pick query phrasings and fire reddit_tools.py search calls whose
    # results Claude never even saw (the dump_dir harvest below is what
    # actually feeds candidates). Query selection + search execution are both
    # deterministic, so we now build the query bank in Python (mirroring the
    # Twitter cycle: scan = deterministic Python, Claude only enters at draft)
    # and run each search via reddit_tools.cmd_search directly. The picker
    # (engagement style) still fires once at the start of the draft phase —
    # the only Claude call that actually writes a comment.
    #
    # reddit_query_bank pulls proven query phrasings from
    # /api/v1/search-topics/ranked?platform=reddit (on Reddit the harvested
    # search_topic IS the raw query string) ranked clicks-first, then appends
    # config.json seeds for cold-start coverage, deduped by normalized core.
    import reddit_query_bank as _rqb
    max_searches = int(os.environ.get("S4L_REDDIT_MAX_SEARCHES", "6") or "6")
    bank = _rqb.build_bank(project_name, limit=max_searches)
    queries = [(b.get("query") or "").strip() for b in bank if (b.get("query") or "").strip()]
    n_proven = sum(1 for b in bank if b.get("source") == "proven")
    n_seed = len(bank) - n_proven
    print(f"[discover_bank] project={project_name} queries={len(queries)} "
          f"proven={n_proven} seed={n_seed} cap={max_searches} :: {queries}")

    if args.dry_run:
        print(f"=== DRY RUN discover (project={project_name}) ===")
        for i, q in enumerate(queries, 1):
            print(f"  {i}. {q}")
        print("=== END DRY RUN ===")
        return {"project_name": project_name, "decisions": [], "cost": 0.0, "dry_run": True}

    if not queries:
        print(f"[post_reddit] discover: no queries for project={project_name} "
              f"(empty bank: no proven queries and no config seeds)")
        return {"project_name": project_name, "decisions": [], "cost": 0.0,
                "error": "no_queries"}

    plan_batch_id = f"reddit-discover-{project_name}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    os.environ["S4L_REDDIT_PROJECT"] = project_name
    os.environ["S4L_REDDIT_BATCH_ID"] = plan_batch_id

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
    os.environ["S4L_REDDIT_DUMP_DIR"] = dump_dir

    print(f"[post_reddit] Starting programmatic discover "
          f"(queries={len(queries)}, limit={args.limit}, dump_dir={dump_dir})")
    import reddit_tools as _rt
    import types as _types
    start = time.time()
    searches_ok = 0
    try:
        for q in queries:
            sargs = _types.SimpleNamespace(
                query=q,
                limit=int(args.limit or 25),
                sort="relevance",
                time="week",
                subreddits=None,
            )
            try:
                _rt.cmd_search(sargs)  # writes result-*.json into dump_dir
                searches_ok += 1
            except SystemExit as se:
                # cmd_search may sys.exit on a hard rate-limit / stop. Halt the
                # loop but KEEP whatever already dumped (harvested below).
                print(f"[post_reddit] discover search halted on {q!r}: "
                      f"SystemExit({getattr(se, 'code', '?')})")
                break
            except Exception as e:
                # One bad query (transient 500, parse error) must not kill the
                # whole discover. Skip it and continue with the rest of the bank.
                print(f"[post_reddit] discover search failed for {q!r}: {e}",
                      file=sys.stderr)
    finally:
        # Always unset so a subsequent (non-discover) reddit_tools call in
        # this process doesn't accidentally inherit dump mode.
        os.environ.pop("S4L_REDDIT_DUMP_DIR", None)
    elapsed = time.time() - start
    print(f"[post_reddit] Discover ran {searches_ok}/{len(queries)} searches "
          f"in {elapsed:.0f}s ($0.0000)")

    # Harvest the dump dir: every cmd_search call that returned threads wrote a
    # result-*.json. Even if a later query halted the loop, earlier searches'
    # dumps are still valid candidates.
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

    # Zero successful searches AND nothing harvested = real search-layer
    # failure (rate-limit / all queries 500'd). Return an error so the runner
    # counts it failed (rc 5). If searches ran but simply found no fresh
    # threads, candidates is empty WITHOUT an error → rc 6 (skipped).
    if searches_ok == 0 and not candidates:
        print(f"[post_reddit] Discover FAILED: 0/{len(queries)} searches succeeded, "
              f"no candidates harvested")
        return {"project_name": project_name, "decisions": [], "cost": 0.0,
                "error": "no_search_results"}

    print(f"[post_reddit] Discover harvested {len(candidates)} candidate(s) from dump dir")
    if not candidates:
        print(f"[post_reddit] No candidates dumped — {searches_ok}/{len(queries)} "
              f"searches ran but returned no fresh threads")

    # --- Topical-overlap scoring + top-N cap (replaces the old ripen momentum
    # gate, retired 2026-06-01 to align with the Twitter pipeline which dropped
    # its inter-phase momentum sleep on 2026-05-31). Reddit's sort=relevance
    # leaks high-engagement OFF-topic threads that share only structural English
    # words with a chatty natural-language query (e.g. an on-topic query about a
    # habit-tracking tool matching an unrelated BORU drama thread). Without the
    # ripen stage thinning the set over 30 min, we instead sort by a topical-
    # overlap signal and keep the top N so draft spends its budget on the most
    # on-topic + active threads. We do NOT hard-drop low-overlap rows: every
    # harvested candidate is still persisted to the queue for analytics + salvage;
    # the cap is a soft prioritization only (conservative per user directive —
    # isolate + surface the garbage in logs rather than silently filtering it).
    DISCOVER_CAP = int(os.environ.get("S4L_REDDIT_DISCOVER_CAP", "25") or "25")
    for c in candidates:
        ov = _topical_overlap(c.get("search_topic"), c.get("thread_title"), c.get("selftext"))
        # velocity proxy: comments weighted 4x upvotes, echoing the old ripen
        # composite (Δup + 4·Δcomments) but on absolute counts since we no longer
        # sample momentum over a time window.
        vel = int(c.get("score") or 0) + 4 * int(c.get("num_comments") or 0)
        c["topical_overlap"] = round(ov, 3)
        c["velocity"] = vel
    # Primary sort: overlap desc (on-topic first). Tiebreak: velocity desc.
    ranked = sorted(candidates, key=lambda c: (c["topical_overlap"], c["velocity"]), reverse=True)
    selected = ranked[:DISCOVER_CAP] if DISCOVER_CAP > 0 else ranked

    # [discover_harvest] marker: surface the overlap distribution so relevance-sort
    # garbage is visible in logs. overlap_zero = rows sharing NO content token with
    # the query = almost certainly leak; if these dominate the harvest we know the
    # query/search is misfiring without having dropped anything.
    n_zero = sum(1 for c in candidates if c["topical_overlap"] == 0.0)
    n_low = sum(1 for c in candidates if 0.0 < c["topical_overlap"] < 0.34)
    n_mid = sum(1 for c in candidates if 0.34 <= c["topical_overlap"] < 0.67)
    n_high = sum(1 for c in candidates if c["topical_overlap"] >= 0.67)
    cut = selected[-1]["topical_overlap"] if selected else 0.0
    print(f"[discover_harvest] project={project_name} harvested={len(candidates)} "
          f"selected={len(selected)} cap={DISCOVER_CAP} cutoff_overlap={cut:.3f} "
          f"overlap_zero={n_zero} low={n_low} mid={n_mid} high={n_high}")
    for c in selected:
        print(f"[discover_harvest]   ov={c['topical_overlap']:.2f} vel={c['velocity']:>5} "
              f"q={(c.get('search_topic') or '')[:40]!r} :: {(c.get('thread_title') or '')[:70]!r}")

    # Persist freshly-discovered candidates to reddit_candidates so a
    # transient post failure on a later phase can be retried by the next
    # cycle's Phase 0 salvage. Best-effort: if the queue write fails, the
    # tmpfile flow still works for this cycle, we just lose the salvage
    # benefit. See module-level _db_upsert_discovered_candidate. We persist
    # ALL harvested candidates (not just the capped `selected`) so the queue
    # keeps full history per the no-pruning rule.
    queue_batch = getattr(args, "batch_id", None) or plan_batch_id
    if not args.dry_run and candidates:
        for c in candidates:
            _db_upsert_discovered_candidate(c, queue_batch, project_name)

    # Backfill seed on reddit_search_attempts rows from this batch so the
    # Search Queries dashboard can join attempts → posts via search_topic.
    # Use the top-ranked selected candidate's search_topic so the seed reflects
    # what actually flows into draft.
    if selected and plan_batch_id:
        seed = (selected[0].get("search_topic") or "").strip()
        if seed:
            try:
                api_patch(
                    "/api/v1/reddit-search-attempts",
                    {"batch_id": plan_batch_id, "seed": seed},
                )
            except Exception as e:
                print(f"[post_reddit] WARNING: seed backfill failed: {e}", file=sys.stderr)

    return {"project_name": project_name, "decisions": selected,
            "cost": 0.0, "session_id": None,
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
        # Build a "reused draft" marker trace so the audit row isn't empty.
        # We can't recover the exact context the prior cycle's Claude saw,
        # but the current top_performers/recent_comments document what the
        # few-shot prompt WOULD have contained had we redrafted. The
        # reused_from_prior_cycle flag tells future auditors "this is
        # current-cycle context, not what produced the draft" — without it
        # the trace would look like Claude saw this report and chose to
        # reuse, which it didn't (Claude wasn't invoked at all). Marker
        # also gives 100% trace coverage on the platform so SQL queries
        # don't have to special-case NULL rows.
        try:
            from generation_trace import build_trace, write_trace_tempfile
            top_report = get_top_performers(project_name)
            recent_comments = get_recent_comments()
            trace = build_trace(
                platform="reddit",
                project_name=project_name,
                prompt_chars=0,  # no Claude call this cycle
                top_performers_text=top_report or "",
                top_search_topics_text="",
                recent_comment_ids=[pid for pid, _ in (recent_comments or [])],
                model="reused_from_prior_cycle",
                min_score_floor=10,
                extras={
                    "reused_from_prior_cycle": True,
                    "draft_ttl_min": DRAFT_TTL_MIN,
                    "reused_candidate_count": len(candidates),
                },
            )
            trace_path = write_trace_tempfile(trace, prefix="reddit_reused_trace_")
            if trace_path:
                plan["generation_trace_path"] = trace_path
                print(f"[post_reddit] Reused-draft trace marker: {trace_path}")
        except Exception as e:
            print(f"[post_reddit] WARNING: reused-draft trace build failed "
                  f"({e}); proceeding without trace")
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

    # 2026-05-19: pick the engagement style HERE — draft is the only
    # Claude call in the Reddit cycle that actually writes a comment, so
    # this is where the picker belongs. (Discover is scan-only opaque
    # mode; it never sees thread content and never drafts text, so a
    # picker there would just be useless decoration.)
    # Mirrors the Twitter engage cycle: pick once → filter top_performers
    # to the assigned style → embed the assignment block in the prompt →
    # JSON example shows the literal assigned style name. End-to-end
    # adherence comes from those three lined-up signals.
    style_assignment_a = pick_style_for_post("reddit", context="posting")
    style_assignment_b = pick_style_for_post("reddit", context="posting")
    picked_style_a = style_assignment_a.get("style")
    picked_style_b = style_assignment_b.get("style")
    print(f"[post_reddit] draft style A assigned: mode={style_assignment_a['mode']} "
          f"style={picked_style_a or '(invent)'}")
    print(f"[post_reddit] draft style B assigned: mode={style_assignment_b['mode']} "
          f"style={picked_style_b or '(invent)'}")
    top_report = get_top_performers(project_name, style=picked_style_a)
    recent_comments = get_recent_comments()
    # We don't have a Reddit equivalent of top_search_topics_report in
    # the draft phase (the discover phase loads it for the search step).
    # Pass empty string; the trace audit still captures top_performers
    # and recent_comments, which is the bulk of the few-shot context.
    # Tool-free turn (2026-07-14): fetch every thread HERE and inline the
    # digests, so the draft prompt is pure text->JSON and queue-routable.
    # Dead threads (archived/locked/blocked) are marked permanently failed
    # now instead of at post time; transient fetch failures stay pending
    # (dropped from THIS cycle only; Phase 0 salvage retries them).
    _before_prefetch = len(candidates)
    digests, dropped = _prefetch_thread_digests(candidates, reddit_username)
    for _c, _why in dropped:
        _url = _c.get("thread_url", "")
        print(f"[post_reddit] prefetch drop {_url}: {_why}")
        _db_mark_candidate_attempt(_url, reason=f"prefetch_{_why}", permanent=True)
    candidates = [c for c in candidates if c.get("thread_url") in digests]
    print(f"[post_reddit] prefetch: {len(candidates)} thread(s) inlined, "
          f"{len(dropped)} dropped dead, "
          f"{_before_prefetch - len(candidates) - len(dropped)} transient-skipped")
    if not candidates:
        plan = dict(plan)
        plan["decisions"] = []
        plan["phase"] = "draft"
        return plan

    prompt = build_draft_prompt(project, config, candidates, top_report, recent_comments,
                                style_assignment_a=style_assignment_a,
                                style_assignment_b=style_assignment_b, digests=digests)

    # Build the generation_trace audit blob: what Claude is about to see.
    # Captured BEFORE the Claude call so we never end up with a post row
    # missing its trace if Claude errors out. The path is stashed in
    # `plan` so the post-phase (_post_iteration → log_post) can forward
    # it to reddit_tools.py for INSERT into posts.generation_trace.
    # Same trace reused for every post produced from this draft session.
    try:
        from generation_trace import build_trace, write_trace_tempfile
        trace = build_trace(
            platform="reddit",
            project_name=project_name,
            prompt_chars=len(prompt or ""),
            top_performers_text=top_report or "",
            top_search_topics_text="",  # Reddit draft phase doesn't surface this
            recent_comment_ids=[pid for pid, _ in (recent_comments or [])],
            model=None,
            min_score_floor=10,  # PLATFORM_MIN_SCORE['reddit']
        )
        trace_path = write_trace_tempfile(trace, prefix="reddit_gen_trace_")
        if trace_path:
            plan["generation_trace_path"] = trace_path
            print(f"[post_reddit] Generation trace: {trace_path} "
                  f"({os.path.getsize(trace_path)} bytes)")
    except Exception as e:
        # Audit row is nice-to-have, never a blocker.
        print(f"[post_reddit] WARNING: generation_trace build failed "
              f"({e}); proceeding without trace")

    print(f"[post_reddit] Starting draft session for {len(candidates)} thread(s)...")
    start = time.time()
    ok, output, usage = run_claude_structured(prompt, timeout=1800)
    elapsed = time.time() - start
    print(f"[post_reddit] Draft finished in {elapsed:.0f}s (${usage['cost_usd']:.4f})")

    if not ok:
        print(f"[post_reddit] Draft FAILED: {str(output)[:300]}")
        plan["draft_error"] = "claude_failed"
        plan["draft_cost"] = usage["cost_usd"]
        return plan

    if isinstance(output, dict):
        # Queue/schema path: posts[] mirrors the legacy JSONL post-line shape.
        drafted = []
        _seen_urls = set()
        for _d in output.get("posts") or []:
            if not isinstance(_d, dict):
                continue
            _url = _d.get("thread_url", "")
            if _d.get("draft_a_text") and _url and _url not in _seen_urls:
                _d.setdefault("action", "post")
                drafted.append(_d)
                _seen_urls.add(_url)
    else:
        # Legacy fallback: the envelope carried free text; regex-parse it.
        drafted = parse_post_decisions(output or "")
    print(f"[post_reddit] Draft produced {len(drafted)} post(s)")

    # 2026-05-11: parse optional action=reject lines and forward any
    # `proposed_excludes` arrays into project_search_excludes via the
    # activation gate (>=2 distinct batches required before a term goes
    # live). Self-improving denylist mirroring twitter's behavior. Errors
    # here MUST NOT kill the draft phase; the post pipeline is the critical
    # path. See parse_reject_decisions / _propose_excludes_from_rejects.
    try:
        if isinstance(output, dict):
            rejects = [r for r in (output.get("rejects") or []) if isinstance(r, dict)]
        else:
            rejects = parse_reject_decisions(output or "")
        if rejects:
            cand_by_url = {c.get("thread_url"): c for c in candidates if c.get("thread_url")}
            counters = _propose_excludes_from_rejects(
                rejects, project_name, plan.get("batch_id"), cand_by_url,
            )
            if counters["proposed"]:
                print(
                    f"[post_reddit] reject lines={counters['rejects_seen']} "
                    f"proposed={counters['proposed']} inserted={counters['inserted']} "
                    f"bumped={counters['bumped']} rejected={counters['rejected']} "
                    f"active_now={counters['active_now']}"
                )
    except Exception as e:
        print(f"[post_reddit] WARN: reject-line processing failed: {e}", file=sys.stderr)

    # Merge text back into the original candidates by thread_url so we
    # preserve ripen annotations, search_topic, etc. from discover phase.
    # Each freshly-written draft is also persisted to reddit_candidates so a
    # later salvage iteration can reuse it without paying the LLM cost again.
    # Experiment/scenario arms (2026-07-15, mirrors run-twitter-cycle.sh):
    # collect() reads S4L_EXP_* from THIS process's env once and the same
    # dict rides onto every candidate below. merge_review_queue.py carries it
    # through untouched; it does not stamp anything itself.
    _exps = _collect_exps()

    by_url = {d["thread_url"]: d for d in drafted}
    merged = []
    for c in candidates:
        url = c.get("thread_url", "")
        drafted_d = by_url.get(url)
        if drafted_d and drafted_d.get("draft_a_text"):
            merged_d = dict(c)
            _text_a = drafted_d.get("draft_a_text") or ""
            _style_a = drafted_d.get("draft_a_style") or picked_style_a or ""
            _text_b = drafted_d.get("draft_b_text") or ""
            _style_b = drafted_d.get("draft_b_style") or picked_style_b or ""
            # Two-draft card (2026-07-15): drafts[] mirrors twitter's shape
            # exactly (variant/text/style/assigned_style/assigned_mode) so
            # s4l_card.py's dual-box rendering, pairwise hover/choice
            # tracking, and the edit-learning digest apply unchanged — that
            # machinery already keys off `isinstance(drafts, list) and
            # len(drafts) == 2`, not platform. Only populate when Draft B
            # actually came back (defensive; the schema requires it).
            merged_d["drafts"] = [
                {
                    "variant": "a",
                    "text": _text_a,
                    "style": _style_a,
                    "assigned_style": picked_style_a,
                    "assigned_mode": style_assignment_a.get("mode"),
                },
                {
                    "variant": "b",
                    "text": _text_b,
                    "style": _style_b,
                    "assigned_style": picked_style_b,
                    "assigned_mode": style_assignment_b.get("mode"),
                },
            ] if _text_b else None
            # Legacy singular mirrors: Draft A is the single-draft
            # representative, same convention as twitter (validate_or_register,
            # _db_save_draft, and any older reader expecting `text`/
            # `engagement_style` sees Draft A's values by default).
            merged_d["text"] = _text_a
            merged_d["reply_to_url"] = drafted_d.get("reply_to_url")
            merged_d["thread_author"] = drafted_d.get("thread_author") or c.get("thread_author")
            merged_d["thread_title"] = drafted_d.get("thread_title") or c.get("thread_title")
            merged_d["engagement_style"] = _style_a or c.get("engagement_style")
            # Top-level assigned_style/assigned_mode default to Draft A (the
            # legacy-mirror draft); the MCP post_drafts edit path overwrites
            # these on the card when a human switches to Draft B (mirrors
            # twitter_post_plan.py's per-candidate override, see
            # _post_iteration below and index.ts's reddit approval branch).
            merged_d["assigned_style"] = picked_style_a
            merged_d["assigned_mode"] = style_assignment_a.get("mode")
            merged_d["action"] = "post"
            merged_d["experiments"] = dict(_exps)
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
    # Also stamp the key _post_iteration actually reads: in two-phase mode
    # (draft in process A, post in process B) it re-exports
    # plan["session_id"] as CLAUDE_SESSION_ID, and this was never set after
    # discover went programmatic (session attribution silently dropped on
    # the two-phase path until 2026-07-14).
    plan["session_id"] = usage.get("session_id")
    plan["phase"] = "draft"
    # Stash the picker assignment so _post_iteration (which runs in a
    # separate process via JSON-serialized plan) can pass it to
    # validate_or_register for USE-mode drift coercion + INVENT-mode gating.
    # This is the BATCH-LEVEL fallback (Draft A); per-decision
    # assigned_style/assigned_mode (set above, and overridable by a human
    # draft-switch) takes precedence in _post_iteration when present.
    plan["style_assignment"] = style_assignment_a
    return plan


def _post_iteration(plan, reddit_username):
    """Execute browser CDP posts for the decisions in plan. Returns (posted, failed)."""
    project_name = plan["project_name"]
    decisions = plan.get("decisions") or []
    # Picker assignment was stamped by _draft_iteration; survives JSON
    # serialization across the draft→post process boundary. Used below
    # in validate_or_register for USE-mode drift coercion + INVENT-mode
    # gating. Fallback to {} for plans drafted before this field landed.
    style_assignment = plan.get("style_assignment") or {}

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
        # Heartbeat the posting-active flag per row so readers see a fresh
        # stamp for the whole drain (rows are ~45s + 180s inter-post sleeps;
        # the reader freshness window is 120s, so re-stamp before EACH row).
        _stamp_posting_active()
        thread_url = decision["thread_url"]
        reply_to_url = decision.get("reply_to_url")
        text = decision["text"]
        thread_author = decision.get("thread_author", "unknown")
        thread_title = decision.get("thread_title", "unknown")
        # validate_or_register: in USE mode, coerces any drifted style name
        # back to the assigned one (so picker authority is preserved even if
        # the drafter ignores the assignment). In INVENT mode (5% slot),
        # registers the new style into engagement_styles_registry via
        # /api/v1/engagement-styles/registry.
        #
        # Two-draft cards (2026-07-15): each decision may carry its OWN
        # (assigned_style, assigned_mode) reflecting whichever draft is
        # actually posting — either Draft A (stamped at plan-write time) or
        # Draft B (stamped by the MCP post_drafts edit path when a human
        # switched drafts on the review card). Without this, every card
        # would coerce back to the single batch-wide Draft A assignment even
        # when it posted under Draft B's style, silently corrupting the
        # engagement_style label the picker's performance stats are learned
        # from. Mirrors twitter_post_plan.py's identical per-candidate
        # override. Falls back to the batch-level assignment for any
        # decision that predates this (assigned_mode key absent).
        if "assigned_mode" in decision:
            _cand_style = decision.get("assigned_style")
            _cand_mode = decision.get("assigned_mode")
        else:
            _cand_style = (style_assignment or {}).get("style")
            _cand_mode = (style_assignment or {}).get("mode")
        engagement_style, _style_action = validate_or_register(
            decision,
            source_post={
                "platform": "reddit",
                "post_url": thread_url,
                "post_id": None,
                "model": decision.get("model"),
            },
            assigned_style=_cand_style,
            assigned_mode=_cand_mode,
        )
        search_topic = decision.get("search_topic") or None

        applied_campaign_ids = []
        for camp in active_campaigns:
            if random.random() < camp["sample_rate"]:
                text = text + camp["suffix"]
                applied_campaign_ids.append(camp["id"])
        if applied_campaign_ids:
            print(f"[post_reddit] applied campaigns {applied_campaign_ids} (suffix appended)")

        # Audience-page detection (2026-05-17). Inspect the unwrapped text for
        # any URL that exactly matches a curated audience-page (e.g.
        # https://s4l.ai/ghostwriting). When found, posts.link_source is
        # stamped 'audience_page:<angle>' for the row so the dashboard can
        # break out curated traffic from generic homepage links. Detection
        # runs BEFORE wrap_text_for_post because wrapping rewrites the URLs
        # to /r/<code> short links; classify_url_as_audience_page() needs
        # the original target URL.
        audience_page_link_source = None
        try:
            for _url_m in re.finditer(r'https?://[^\s)\]>"\']+', text):
                _raw = _url_m.group(0).rstrip('.,);!?]')
                _angle = _audience_classify_url(_raw, project_name)
                if _angle:
                    audience_page_link_source = f"audience_page:{_angle}"
                    break
        except Exception as _e:
            print(f"[post_reddit] WARNING: audience-page classify raised ({_e})")

        # URL-wrap the final text (URLs in suffix included). Mints into
        # post_links with NULL post_id; we backfill after log_post returns
        # below. On wrap failure, post unwrapped — losing attribution is
        # better than failing a post that already passed planning.
        minted_session = None
        try:
            from dm_short_links import wrap_text_for_post, utm_only_text
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
                      f"({wrap_res.get('error')}); falling back to UTM-only")
                text = utm_only_text(text=text, platform="reddit", project_name=project_name)
        except Exception as e:
            print(f"[post_reddit] WARNING: URL wrap raised ({e}); falling back to UTM-only")
            try:
                from dm_short_links import utm_only_text
                text = utm_only_text(text=text, platform="reddit", project_name=project_name)
            except Exception as ee:
                print(f"[post_reddit] WARNING: UTM-only fallback also failed ({ee}); posting unwrapped")

        # Per-row reddit-browser lease (2026-05-13). Acquire JUST around the
        # CDP work, release before this row's DB post-processing and the 3-min
        # between-post sleep. Peers (link-edit, dm-outreach, engage,
        # engage-dm-replies) can use the browser during our sleeps and DB
        # writes instead of sitting blocked until the whole batch finishes.
        lease_ok, lease_msg = _acquire_browser_lease(timeout=600, ttl=90)
        if not lease_ok:
            print(f"[post_reddit] {i + 1}/{len(decisions)} LEASE: {lease_msg}; skipping post")
            failed += 1
            # Treat lease-acquire failure as TRANSIENT so phase0 salvages
            # the row next cycle (it's not the candidate's fault that a
            # peer pipeline held the browser too long).
            _db_mark_candidate_attempt(thread_url, "lease_acquire_timeout", permanent=False)
            if i < len(decisions) - 1:
                time.sleep(180)
            continue

        try:
            print(f"[post_reddit] Posting {i + 1}/{len(decisions)}: {thread_title[:50]}...")
            result = post_via_cdp(thread_url, reply_to_url, text)
        finally:
            _release_browser_lease()

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
                     search_topic=search_topic,
                     # Forward the trace blob built during draft phase.
                     # Same trace for every post in this plan because they
                     # all saw the same few-shot context. None when the
                     # draft phase used a reused/cached draft (no Claude
                     # call) — that's fine, audit just records no trace.
                     generation_trace_path=plan.get("generation_trace_path"),
                     link_source=audience_page_link_source)
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
                # project=None: account-level ban applies across ALL projects,
                # not just the one currently posting. Backfill of 28 existing
                # project-scoped entries applied 2026-05-19.
                mark_comment_blocked(thread_url, reason=err, project=None)
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
        # Hard preflight: _post_iteration shells to reddit_browser.py, the only
        # Playwright importer on this rail. If the resolved interpreter can't
        # import it the owned runtime is missing/half-provisioned and every post
        # would die with CDP_ERROR. Fail LOUD with a distinct signal instead.
        # Gated on real decisions so an empty plan still exits clean.
        if plan.get("decisions"):
            _chk = subprocess.run(
                [PYTHON, "-c", "import playwright"],
                capture_output=True, text=True,
            )
            if _chk.returncode != 0:
                print(f"[post_reddit] FATAL runtime_incomplete: interpreter {PYTHON!r} "
                      f"cannot import playwright — the owned Python runtime is missing or "
                      f"unprovisioned. Run the `runtime` install (action:'install') before "
                      f"posting. stderr: {(_chk.stderr or '').strip()[:300]}", file=sys.stderr)
                sys.exit(3)
        try:
            _stamp_posting_active()
            posted, failed = _post_iteration(plan, reddit_username)
            print(f"[post_reddit] phase=post project={plan.get('project_name')} posted={posted} failed={failed}")
        finally:
            _clear_posting_active()
            # Clean up the generation_trace temp file. By this point every
            # post that landed has the trace JSONB persisted to its row,
            # so the on-disk file is redundant. Best-effort delete.
            try:
                from generation_trace import cleanup_trace_tempfile
                cleanup_trace_tempfile(plan.get("generation_trace_path"))
            except Exception:
                pass


if __name__ == "__main__":
    main()
