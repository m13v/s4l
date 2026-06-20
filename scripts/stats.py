#!/usr/bin/env python3
"""Fetch engagement stats for Reddit + Moltbook posts via public APIs.

Updates upvotes, comments_count, and status in the DB. No browser needed.
Reddit profile scrape (Step 1 of stats.sh) covers most stats; this script
acts as deletion/removal detection and as a fallback for rows the scrape
couldn't match.

Usage:
    python3 scripts/stats.py [--db PATH] [--quiet]
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_post, api_patch, load_env


# --- HTTP wrappers for the Reddit branch (2026-05-12 migration) --------------
# The Reddit pipeline must have zero direct-SQL paths. These helpers wrap the
# small set of /api/v1/posts and /api/v1/replies operations the Reddit branch
# needs, so the original logic in refresh_reddit / refresh_reddit_replies /
# refresh_reddit_resurrect can stay readable while still routing every
# read/write through HTTP. Other platforms (twitter, github, moltbook) still
# use direct SQL until they migrate; the helpers below are intentionally
# named *_http to make the boundary obvious.

def _http_list_reddit_active_posts():
    """Walk /api/v1/posts in pages and return rows for the Reddit refresh job.

    The /api/v1/posts GET caps a single page at 500. Sort by id ASC so we can
    page deterministically; we re-issue with an increasing id cursor until the
    server returns a short page. We need scan_no_change_count, posted_at,
    engagement_updated_at, deletion_detect_count, upvotes, comments_count.
    """
    out = []
    seen_ids = set()
    cursor_since = None  # unused for id-asc paging
    last_seen_id = 0
    while True:
        query = {
            "platform": "reddit",
            "status": "active",
            "has_our_url": "true",
            "order_by": "id",
            "order_dir": "asc",
            "limit": 500,
        }
        resp = api_get("/api/v1/posts", query=query)
        rows = ((resp or {}).get("data") or {}).get("posts") or []
        new_rows = [r for r in rows if r.get("id") and r["id"] not in seen_ids]
        if not new_rows:
            break
        for r in new_rows:
            seen_ids.add(r["id"])
            out.append(r)
            if r["id"] > last_seen_id:
                last_seen_id = r["id"]
        # Without a server-side cursor, we get the same first 500 every call.
        # Break to avoid an infinite loop; the typical Reddit active-post count
        # is well under 500 so one page covers it.
        break
    return out


def _http_list_reddit_dead_posts(days):
    """Posts marked deleted/removed in the last N days (resurrect job)."""
    since_iso = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
    resp = api_get(
        "/api/v1/posts",
        query={
            "platform": "reddit",
            "statuses": "deleted,removed",
            "has_our_url": "true",
            "since": since_iso,
            "order_by": "id",
            "order_dir": "asc",
            "limit": 500,
        },
    )
    return ((resp or {}).get("data") or {}).get("posts") or []


def _http_patch_post(post_id, body):
    return api_patch(f"/api/v1/posts/{int(post_id)}", body)


def _http_detect_deletion(post_id, kind, threshold=2):
    """Bump deletion_detect_count and flip status if threshold met."""
    resp = api_post(
        f"/api/v1/posts/{int(post_id)}/detect-deletion",
        {"kind": kind, "threshold": int(threshold)},
    )
    data = (resp or {}).get("data") or {}
    return int(data.get("detect_count") or 0), bool(data.get("status_set"))


def _http_list_reddit_replies_to_refresh():
    """Replies for our Reddit comments (status='replied', our_reply_id NOT NULL)."""
    out = []
    seen_ids = set()
    resp = api_get(
        "/api/v1/replies",
        query={
            "platform": "reddit",
            "status": "replied",
            "has_our_reply_id": "true",
            "order_by": "id",
            "limit": 500,
        },
    )
    rows = ((resp or {}).get("data") or {}).get("replies") or []
    for r in rows:
        rid = r.get("id")
        if rid and rid not in seen_ids:
            seen_ids.add(rid)
            out.append(r)
    return out


def _http_patch_reply(reply_id, body):
    return api_patch(f"/api/v1/replies/{int(reply_id)}", body)


# --- HTTP wrappers for the Twitter branch (2026-05-19 migration) -------------
# Mirror the Reddit pattern: every read + write in refresh_twitter() and
# refresh_twitter_replies() goes through HTTP so the VM (no DATABASE_URL) can
# run the stats job too. Scoping by `our_account` happens server-side in the
# /api/v1/posts/active-for-stats endpoint; the local mac passes 'm13v_', the
# VM passes 'matt_diak'. Strict scoping means neither machine touches the
# other's posts even when both cron-fire concurrently.

def _http_list_twitter_active_posts(our_account, audit_mode=False, stale_hours=5):
    """Posts to refresh for the Twitter stats job, scoped by handle."""
    resp = api_get(
        "/api/v1/posts/active-for-stats",
        query={
            "platform": "twitter",
            "our_account": our_account,
            "audit": "true" if audit_mode else "false",
            "engagement_stale_after_hours": int(stale_hours),
        },
    )
    return ((resp or {}).get("data") or {}).get("posts") or []


def _http_list_twitter_replies_to_refresh():
    """Reply rows to refresh for the Twitter stats job, scoped by install_id
    via the auth header (route reads resolveAuth().install_id and filters)."""
    resp = api_get(
        "/api/v1/replies/active-for-stats",
        query={"platform": "x"},
    )
    return ((resp or {}).get("data") or {}).get("replies") or []


def _http_list_twitter_top_replies_to_refresh(stale_hours=5):
    """thread_top_replies rows the Twitter stats job should refresh.

    Scoped to the calling install via X-Installation header (route reads
    resolveAuth().install_id; primary historical install also claims the
    NULL-install_id rows). Same freshness gate (5h default) as posts so
    the snapshot and benchmark curves stay aligned per cycle.
    """
    resp = api_get(
        "/api/v1/thread-top-replies/active-for-stats",
        query={"platform": "twitter",
               "engagement_stale_after_hours": int(stale_hours)},
    )
    return ((resp or {}).get("data") or {}).get("thread_top_replies") or []


def _http_patch_top_reply(ttr_id, body):
    return api_patch(f"/api/v1/thread-top-replies/{int(ttr_id)}", body)


def _http_detect_deletion_top_reply(ttr_id, kind, threshold=2):
    resp = api_post(
        f"/api/v1/thread-top-replies/{int(ttr_id)}/detect-deletion",
        {"kind": kind, "threshold": int(threshold)},
    )
    data = (resp or {}).get("data") or {}
    return int(data.get("detect_count") or 0), bool(data.get("status_set"))


def _http_snapshot_post_views(post_id, views):
    """HTTP equivalent of dbmod.snapshot_post_views — UPSERT one row of
    post_views_daily for CURRENT_DATE. Errors swallowed so a transient
    network blip doesn't abort the stats run (the parent row's views/upvotes
    are already updated; the daily rollup is best-effort)."""
    try:
        api_post(
            "/api/v1/post-views-daily/snapshot",
            {"post_id": int(post_id), "views": int(views)},
        )
    except Exception:
        pass


# --- HTTP wrappers for the parent-thread snapshot lane (2026-05-26) ----------
# refresh_twitter_threads() polls the *parent* tweet of every active comment
# we made and appends one row to thread_snapshots per poll. Two helpers:
#   - list: returns deduped parent threads to poll right now, scoped by
#     our_account and gated by staleness (skip threads polled within the
#     window).
#   - insert: appends one snapshot row, attributable to the caller's
#     install_id via the auth header.

def _http_list_twitter_parent_threads(our_account, stale_hours=5,
                                      max_age_days=30):
    """Parent threads the twitter stats job should refresh.

    Returns a list of dicts with: post_id, thread_url, thread_author_handle,
    posted_at, last_captured_at (NULL if never polled), plus the previous
    snapshot's counters so the writer can short-circuit "nothing changed".
    """
    resp = api_get(
        "/api/v1/thread-snapshots/active-for-stats",
        query={
            "platform": "twitter",
            "our_account": our_account,
            "stale_hours": int(stale_hours),
            "max_age_days": int(max_age_days),
        },
    )
    return ((resp or {}).get("data") or {}).get("threads") or []


def _http_insert_thread_snapshot(platform, thread_url, *,
                                 thread_external_id=None,
                                 thread_author_handle=None,
                                 views=None, likes=None, replies=None,
                                 retweets=None, bookmarks=None, quotes=None,
                                 is_deleted=False, error=None):
    """Append one snapshot row. Returns the inserted row id or None on error.

    Errors are swallowed so a single bad row doesn't abort the whole refresh
    pass; the caller logs and continues."""
    body = {
        "platform": platform,
        "thread_url": thread_url,
        "thread_external_id": thread_external_id,
        "thread_author_handle": thread_author_handle,
        "views": views,
        "likes": likes,
        "replies": replies,
        "retweets": retweets,
        "bookmarks": bookmarks,
        "quotes": quotes,
        "is_deleted": bool(is_deleted),
        "error": error,
    }
    try:
        resp = api_post("/api/v1/thread-snapshots", body)
        data = (resp or {}).get("data") or {}
        return data.get("id")
    except Exception:
        return None


def _http_list_moltbook_active_posts():
    """Active moltbook posts to refresh. The generic /api/v1/posts list can't
    order by engagement_updated_at, so we take id-desc; moltbook active volume
    is small so one 500-row page covers it."""
    resp = api_get(
        "/api/v1/posts",
        query={
            "platform": "moltbook",
            "status": "active",
            "has_our_url": "true",
            "order_by": "id",
            "order_dir": "desc",
            "limit": 500,
        },
    )
    return ((resp or {}).get("data") or {}).get("posts") or []


def _http_list_github_active_posts(limit=None):
    """All active github comments with our_url, plus a folded-in reply_count
    (so the caller skips a per-post COUNT round trip). Server-side query has no
    posted_at window / account scoping, matching refresh_github's plain SELECT.
    limit is applied client-side (smoke tests only)."""
    resp = api_get("/api/v1/posts/active-for-stats", query={"platform": "github"})
    rows = ((resp or {}).get("data") or {}).get("posts") or []
    if limit:
        rows = rows[: int(limit)]
    return rows


def _http_list_github_replies_to_refresh():
    """Replies for our github comments (status='replied', our_reply_url NOT
    NULL). Reuses the install-scoped replies/active-for-stats endpoint, which
    returns id, our_reply_url, engagement_updated_at with no 500-row cap."""
    resp = api_get("/api/v1/replies/active-for-stats", query={"platform": "github"})
    return ((resp or {}).get("data") or {}).get("replies") or []


def _http_mark_minimized(post_id, reason):
    """Flip a hidden (isMinimized) github comment to status='deleted' with the
    GREATEST/source_summary-append semantics strike_alert expects."""
    return api_post(
        f"/api/v1/posts/{int(post_id)}/mark-minimized",
        {"reason": str(reason or "")},
    )


def _parse_dt(v):
    """Tolerate both datetime objects (legacy) and ISO strings (HTTP)."""
    if not v:
        return None
    if hasattr(v, "isoformat"):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


import progress
from moltbook_tools import (
    fetch_moltbook_json,
    HttpNotFoundError as MoltbookNotFoundError,
    MoltbookRateLimitedError,
)

CONFIG_PATH = os.path.expanduser("~/social-autoposter/config.json")


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


class HttpNotFoundError(Exception):
    """Raised when a fetch returns HTTP 404.

    Carries the parsed JSON body (when present) on .body. fxtwitter serves
    its *tombstone* objects (type="tombstone", reason="unavailable" -- guest-API
    blind spot, the tweet is ALIVE to a logged-in viewer) WITH an HTTP 404
    status, so discarding the body here is what produced false deletion strikes.
    Preserve the body so refresh_twitter's tombstone guard can see it.
    """

    def __init__(self, url, body=None):
        super().__init__(url)
        self.body = body


def fetch_json(url, headers=None, user_agent="social-autoposter/1.0"):
    hdrs = {"User-Agent": user_agent}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # NOTE: never throw away the body on the status code that carries
            # the payload. fxtwitter returns its meaningful tombstone object
            # WITH a 404; reading e.read() here is what lets the tombstone
            # guard distinguish "alive but guest-blind" from a real deletion.
            # Verified live 2026-06-05: a full stats-twitter run logged 2
            # TOMBSTONE skips, 0 false DELETED.
            body = None
            try:
                body = json.loads(e.read())
            except Exception:
                body = None
            raise HttpNotFoundError(url, body=body)
        return None
    except Exception as e:
        return None


_reddit_rate_state = {"remaining": None, "reset_in": None}


def _parse_float_header(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _update_reddit_rate_state(headers):
    """Read x-ratelimit-* headers into module state for pacing decisions."""
    if not headers:
        return
    rem = _parse_float_header(headers.get("x-ratelimit-remaining"))
    reset = _parse_float_header(headers.get("x-ratelimit-reset"))
    if rem is not None:
        _reddit_rate_state["remaining"] = rem
    if reset is not None:
        _reddit_rate_state["reset_in"] = reset


def _reddit_pacing_sleep():
    """Sleep between Reddit calls based on remaining rate budget.

    Reddit's public endpoint allows ~100 calls per 10-minute sliding window.
    If we've read rate headers, spread remaining calls across the reset window.
    Otherwise fall back to a flat 2s pacer.
    """
    rem = _reddit_rate_state.get("remaining")
    reset_in = _reddit_rate_state.get("reset_in")
    if rem is None or reset_in is None:
        time.sleep(2)
        return
    if rem <= 0:
        time.sleep(min(max(1, reset_in), 120))
        return
    per_call = reset_in / rem
    time.sleep(max(1, min(per_call, 30)))


def fetch_reddit_json(url, user_agent, max_retries=2, timeout=15):
    """Rate-limit aware Reddit JSON fetch.

    Returns a 2-tuple (status, data). status is one of:
      'ok'            - parsed JSON returned as data
      'not_found'     - HTTP 404 (data=None)
      'rate_limited'  - HTTP 429 even after retries (data=None)
      'empty'         - HTTP 200 but empty/malformed body (data=None)
      'error'         - network, timeout, or other HTTPError (data=None)

    Reads x-ratelimit-remaining / x-ratelimit-reset from every response
    (success AND error) into _reddit_rate_state so the caller can pace.
    On 429, honors Retry-After (capped to 120s) and retries.
    """
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                _update_reddit_rate_state(resp.headers)
                body = resp.read()
                if not body:
                    return ("empty", None)
                try:
                    return ("ok", json.loads(body))
                except Exception:
                    return ("empty", None)
        except urllib.error.HTTPError as e:
            _update_reddit_rate_state(e.headers)
            if e.code == 404:
                return ("not_found", None)
            if e.code == 429:
                retry_after = None
                if e.headers:
                    ra = e.headers.get("Retry-After")
                    if ra:
                        try:
                            retry_after = int(ra)
                        except (TypeError, ValueError):
                            retry_after = None
                if retry_after is None:
                    retry_after = int(_reddit_rate_state.get("reset_in") or 60)
                retry_after = max(1, min(retry_after, 120))
                if attempt < max_retries:
                    time.sleep(retry_after)
                    continue
                return ("rate_limited", None)
            return ("error", None)
        except Exception:
            if attempt < max_retries:
                time.sleep(5 * (attempt + 1))
                continue
            return ("error", None)
    return ("error", None)


def refresh_reddit(db, user_agent, config=None, quiet=False):
    config = config or {}
    # 2026-05-12: read all rows via /api/v1/posts so the Reddit branch owns no
    # SQL. `db` is preserved in the signature for backwards compatibility with
    # callers in main(); it's ignored here.
    posts_rows = _http_list_reddit_active_posts()
    # Build a list-of-tuples shape that the existing for-loop expects:
    #   (id, our_url, thread_url, upvotes, comments_count, scan_no_change_count,
    #    posted_at-as-datetime, engagement_updated_at-as-datetime)
    def _parse_iso(v):
        if not v:
            return None
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except Exception:
            return None
    posts = [
        (
            r.get("id"),
            r.get("our_url"),
            r.get("thread_url"),
            r.get("upvotes"),
            r.get("comments_count"),
            int(r.get("scan_no_change_count") or 0),
            _parse_iso(r.get("posted_at")),
            _parse_iso(r.get("engagement_updated_at")),
        )
        for r in posts_rows
    ]

    BATCH_SIZE = 200
    total = updated = changed = deleted = removed = errors = skipped = 0
    # `updated`: rows the Reddit JSON API answered for and we wrote back
    #   (every successful poll). Effectively `total - errors - skipped - frozen`.
    # `changed`: subset of `updated` where score OR comments_count actually
    #   shifted since the prior scan. The dashboard's "updated" pill renders
    #   this (see log_run.py --updated docstring) — before 2026-05-08 it
    #   showed the polled count, which trivially matched "checked" whenever
    #   errors were zero and hid that ~90% of Reddit polls observe no change.
    skipped_fresh = 0
    errors_404 = errors_rate_limited = errors_empty = errors_other = 0
    results = []

    # If Step 1 (profile scrape) just ran, the row was already refreshed and
    # has a recent engagement_updated_at. Skip to save API calls. Applies to
    # both thread and comment rows since the scrape now captures comment-row
    # scores too. Deletion detection is delayed by up to FRESH_WINDOW for
    # those rows, which is acceptable (next cycle catches it).
    FRESH_WINDOW = timedelta(hours=4)
    now_utc = datetime.now(timezone.utc)

    for post in posts:
        total += 1
        if total % BATCH_SIZE == 0:
            progress.tick("reddit", total, len(posts),
                          updated=updated, changed=changed, errors=errors,
                          errors_404=errors_404,
                          errors_rate_limited=errors_rate_limited,
                          errors_empty=errors_empty,
                          errors_other=errors_other)
            if not quiet:
                rem = _reddit_rate_state.get("remaining")
                rem_str = f", rem={int(rem)}" if rem is not None else ""
                print(f"  Batch ({total}/{len(posts)} iterated, {updated} polled, {changed} changed, {errors} errors [404={errors_404} rl={errors_rate_limited} empty={errors_empty} other={errors_other}]{rem_str})", flush=True)
        post_id, our_url, thread_url = post[0], post[1], post[2]
        prev_upvotes, prev_comments = post[3], post[4]
        no_change = post[5]
        posted_at = post[6]
        engagement_updated_at = post[7]

        # Skip any row (thread or comment) refreshed by Step 1 within the
        # fresh window. Step 1 captures views + upvotes + comments_count for
        # both row types, so all stats are covered without an API hit.
        if engagement_updated_at:
            eu = engagement_updated_at
            if eu.tzinfo is None:
                eu = eu.replace(tzinfo=timezone.utc)
            if now_utc - eu < FRESH_WINDOW:
                skipped_fresh += 1
                continue

        # Skip stable posts: 2+ scans with no change AND older than 3 days
        if no_change >= 2 and posted_at:
            age = datetime.now(timezone.utc) - (posted_at.replace(tzinfo=timezone.utc) if posted_at.tzinfo is None else posted_at)
            if age > timedelta(days=3):
                skipped += 1
                continue

        if not our_url or not our_url.startswith("http"):
            errors += 1
            errors_other += 1
            continue

        # Detect if our_url points to a specific comment or just the thread
        has_comment_id = bool(
            re.search(r"/comment/[a-z0-9]+", our_url) or
            re.search(r"/comments/[a-z0-9]+/[^/]+/[a-z0-9]+", our_url)
        )

        json_url = re.sub(r"www\.reddit\.com", "old.reddit.com", our_url).rstrip("/") + ".json"

        _reddit_pacing_sleep()
        status, response = fetch_reddit_json(json_url, user_agent)
        if status == "not_found":
            errors += 1
            errors_404 += 1
            continue
        if status == "rate_limited":
            errors += 1
            errors_rate_limited += 1
            continue
        if status == "empty" or not isinstance(response, list) or len(response) < 2:
            errors += 1
            errors_empty += 1
            continue
        if status != "ok":
            errors += 1
            errors_other += 1
            continue

        thread_data = response[0].get("data", {}).get("children", [{}])[0].get("data", {})
        thread_score = thread_data.get("score", 0)
        thread_comments = thread_data.get("num_comments", 0)
        thread_title = thread_data.get("title", "")[:60]
        thread_author = thread_data.get("author", "")

        if has_comment_id:
            # our_url has a comment permalink — response[1] contains the specific comment
            children = response[1].get("data", {}).get("children", [])
            if not children:
                errors += 1
                continue
            comment_data = children[0].get("data")
            if not comment_data:
                errors += 1
                continue

            body = comment_data.get("body", "")
            author = comment_data.get("author", "")
            score = comment_data.get("score", 0)

            # Count direct replies to our comment
            replies_obj = comment_data.get("replies", "")
            comment_reply_count = 0
            if replies_obj and isinstance(replies_obj, dict):
                reply_children = replies_obj.get("data", {}).get("children", [])
                comment_reply_count = sum(1 for c in reply_children if c.get("kind") == "t1")
                comment_reply_count += sum(
                    c.get("data", {}).get("count", 0)
                    for c in reply_children if c.get("kind") == "more"
                )

            if body in ("[deleted]",) or author == "[deleted]":
                # Two-strike deletion detection. The /detect-deletion endpoint
                # atomically bumps deletion_detect_count and flips status when
                # the threshold is reached.
                detect_count, was_set = _http_detect_deletion(post_id, "deleted", 2)
                if was_set:
                    deleted += 1
                    if not quiet:
                        print(f"DELETED [{post_id}] (confirmed after {detect_count} detections)")
                else:
                    if not quiet:
                        print(f"DELETION PENDING [{post_id}] (detection {detect_count}/2)")
                continue

            if body == "[removed]":
                detect_count, was_set = _http_detect_deletion(post_id, "removed", 2)
                if was_set:
                    removed += 1
                    if not quiet:
                        print(f"REMOVED [{post_id}] (confirmed after {detect_count} detections)")
                else:
                    if not quiet:
                        print(f"REMOVAL PENDING [{post_id}] (detection {detect_count}/2)")
                continue

            _http_patch_post(post_id, {
                "upvotes": score,
                "comments_count": comment_reply_count,
                "stamp_engagement_now": True,
                "stamp_status_checked_now": True,
                "reset_deletion_detect_count": True,
            })
            updated += 1
            if score != prev_upvotes or comment_reply_count != prev_comments:
                changed += 1
            results.append({"id": post_id, "score": score, "comment_replies": comment_reply_count,
                            "thread_score": thread_score, "thread_comments": thread_comments,
                            "title": thread_title,
                            # _comments_written = the value we wrote to
                            # posts.comments_count (used by the skip-optimization
                            # block below to gate scan_no_change_count on
                            # comment-count change as well as score change).
                            "_comments_written": comment_reply_count})
        else:
            # our_url is a thread URL without a comment ID
            # Check if it's our original post (we are the thread author)
            is_our_post = thread_author.lower() == config.get("accounts", {}).get("reddit", {}).get("username", "").lower()

            if is_our_post:
                # Original post — use thread-level stats (they ARE our stats)
                if thread_data.get("removed_by_category"):
                    detect_count, was_set = _http_detect_deletion(post_id, "removed", 2)
                    if was_set:
                        removed += 1
                        if not quiet:
                            print(f"REMOVED (thread) [{post_id}] (confirmed after {detect_count} detections)")
                    else:
                        if not quiet:
                            print(f"REMOVAL PENDING (thread) [{post_id}] (detection {detect_count}/2)")
                    continue

                _http_patch_post(post_id, {
                    "upvotes": thread_score,
                    "comments_count": thread_comments,
                    "stamp_engagement_now": True,
                    "stamp_status_checked_now": True,
                    "reset_deletion_detect_count": True,
                })
                updated += 1
                if thread_score != prev_upvotes or thread_comments != prev_comments:
                    changed += 1
                results.append({"id": post_id, "score": thread_score, "thread_score": thread_score,
                                "thread_comments": thread_comments, "title": thread_title,
                                "_comments_written": thread_comments})
            else:
                # Comment without permalink — we can't get comment-specific stats
                # Only update thread engagement metadata, don't touch upvotes/comments_count
                # Check if our comment is still visible by searching response[1]
                our_found = False
                our_removed = False
                our_username = config.get("accounts", {}).get("reddit", {}).get("username", "")
                children = response[1].get("data", {}).get("children", [])
                for child in children:
                    cd = child.get("data", {})
                    if cd.get("author", "").lower() == our_username.lower():
                        our_found = True
                        if cd.get("body") == "[removed]":
                            our_removed = True
                        elif cd.get("body") in ("[deleted]",) or cd.get("author") == "[deleted]":
                            our_removed = True
                        else:
                            # Found our comment with stats — update
                            score = cd.get("score", 0)
                            _http_patch_post(post_id, {
                                "upvotes": score,
                                "stamp_engagement_now": True,
                                "stamp_status_checked_now": True,
                                "reset_deletion_detect_count": True,
                            })
                            updated += 1
                            # No comments_count write in this branch (no-permalink
                            # comments lack per-comment reply visibility), so
                            # change detection is score-only and the skip block
                            # reads _comments_written=None and ignores comments.
                            if score != prev_upvotes:
                                changed += 1
                            results.append({"id": post_id, "score": score, "thread_score": thread_score,
                                            "thread_comments": thread_comments, "title": thread_title,
                                            "_comments_written": None})
                        break

                if our_removed:
                    detect_count, was_set = _http_detect_deletion(post_id, "removed", 2)
                    if was_set:
                        removed += 1
                        if not quiet:
                            print(f"REMOVED (no permalink) [{post_id}] (confirmed after {detect_count} detections)")
                    else:
                        if not quiet:
                            print(f"REMOVAL PENDING (no permalink) [{post_id}] (detection {detect_count}/2)")
                elif not our_found:
                    # Comment not in top-level replies — just update checked timestamp
                    _http_patch_post(post_id, {"stamp_status_checked_now": True})
                    if not quiet:
                        print(f"SKIP (no permalink, comment not in top-level) [{post_id}]")

        # Track whether stats changed for skip optimization. A row counts as
        # "no change" only when BOTH score and comments_count are unchanged
        # since the prior scan. _comments_written = None means this branch
        # didn't write comments_count (no-permalink case), so we don't gate
        # the skip on comments — score-only. PATCH /api/v1/posts/[id] supports
        # `scan_no_change_delta` to bump by +1, or `scan_no_change_count=0`
        # to reset.
        if results and results[-1]["id"] == post_id:
            new_score = results[-1]["score"]
            new_comments = results[-1].get("_comments_written")
            score_unchanged = (new_score == prev_upvotes)
            comments_unchanged = (new_comments is None or new_comments == prev_comments)
            if score_unchanged and comments_unchanged:
                _http_patch_post(post_id, {"scan_no_change_delta": 1})
            else:
                _http_patch_post(post_id, {"scan_no_change_count": 0})

        # Pacing now happens at top of loop (before API call) via _reddit_pacing_sleep().

    progress.done("reddit", len(posts),
                  updated=updated, changed=changed, deleted=deleted, removed=removed,
                  errors=errors, skipped=skipped, skipped_fresh=skipped_fresh)
    if skipped and not quiet:
        print(f"  Skipped {skipped} stable posts (2+ scans unchanged, older than 3 days)")
    if skipped_fresh and not quiet:
        print(f"  Skipped {skipped_fresh} rows refreshed by Step 1 within 4h")
    return {"total": total, "updated": updated, "changed": changed,
            "deleted": deleted, "removed": removed,
            "errors": errors,
            "errors_404": errors_404,
            "errors_rate_limited": errors_rate_limited,
            "errors_empty": errors_empty,
            "errors_other": errors_other,
            "skipped": skipped, "skipped_fresh": skipped_fresh, "results": results}


def refresh_reddit_resurrect(db, user_agent, config=None, quiet=False, days=60):
    """Re-check Reddit posts marked 'deleted'/'removed' in the last N days.

    If the post/comment is now visible with real content, flip status back to 'active'.
    One live detection is enough (bias: don't falsely mark deleted).
    """
    config = config or {}
    our_username = config.get("accounts", {}).get("reddit", {}).get("username", "")

    # 2026-05-12: read via /api/v1/posts. `db` is ignored.
    posts_rows = _http_list_reddit_dead_posts(days)
    posts = [
        (r.get("id"), r.get("our_url"), r.get("thread_url"), r.get("status"))
        for r in posts_rows
    ]

    total = resurrected = still_dead = errors = 0
    errors_404 = errors_rate_limited = errors_empty = errors_malformed = errors_other = 0

    for post in posts:
        total += 1
        post_id, our_url, thread_url, prev_status = post[0], post[1], post[2], post[3]

        if not our_url or not our_url.startswith("http"):
            errors += 1
            continue

        has_comment_id = bool(
            re.search(r"/comment/[a-z0-9]+", our_url) or
            re.search(r"/comments/[a-z0-9]+/[^/]+/[a-z0-9]+", our_url)
        )

        json_url = re.sub(r"www\.reddit\.com", "old.reddit.com", our_url).rstrip("/") + ".json"

        _reddit_pacing_sleep()
        status, response = fetch_reddit_json(json_url, user_agent)
        if status == "not_found":
            still_dead += 1
            _http_patch_post(post_id, {"stamp_status_checked_now": True})
            continue
        if status == "rate_limited":
            errors += 1; errors_rate_limited += 1
            continue
        if status == "empty":
            errors += 1; errors_empty += 1
            continue
        if status == "error":
            errors += 1; errors_other += 1
            continue
        if not isinstance(response, list) or len(response) < 2:
            errors += 1; errors_malformed += 1
            continue

        thread_data = response[0].get("data", {}).get("children", [{}])[0].get("data", {})
        thread_author = thread_data.get("author", "")

        is_live = False

        if has_comment_id:
            children = response[1].get("data", {}).get("children", [])
            comment_data = children[0].get("data") if children else None
            if comment_data:
                body = comment_data.get("body", "")
                author = comment_data.get("author", "")
                if body not in ("[deleted]", "[removed]") and author != "[deleted]" and body.strip():
                    is_live = True
        else:
            is_our_post = thread_author.lower() == our_username.lower()
            if is_our_post:
                if not thread_data.get("removed_by_category") and thread_data.get("selftext") not in ("[removed]", "[deleted]"):
                    is_live = True
            else:
                children = response[1].get("data", {}).get("children", [])
                for child in children:
                    cd = child.get("data", {})
                    if cd.get("author", "").lower() == our_username.lower():
                        body = cd.get("body", "")
                        if body not in ("[deleted]", "[removed]") and body.strip():
                            is_live = True
                        break

        if is_live:
            _http_patch_post(post_id, {
                "status": "active",
                "reset_deletion_detect_count": True,
                "stamp_status_checked_now": True,
                "stamp_resurrected_now": True,
            })
            resurrected += 1
            if not quiet:
                print(f"RESURRECTED [{post_id}] ({prev_status} -> active): {our_url}", flush=True)
        else:
            still_dead += 1
            _http_patch_post(post_id, {"stamp_status_checked_now": True})

        # Pacing now happens at top of loop (before API call) via _reddit_pacing_sleep().

    return {"total": total, "resurrected": resurrected, "still_dead": still_dead, "errors": errors,
            "errors_404": errors_404, "errors_rate_limited": errors_rate_limited,
            "errors_empty": errors_empty, "errors_malformed": errors_malformed,
            "errors_other": errors_other}


def refresh_moltbook(db, api_key, quiet=False):
    if not api_key:
        return {"skipped": True, "reason": "no_api_key"}

    posts = _http_list_moltbook_active_posts()

    total = updated = deleted = errors = skipped = 0
    results = []
    rate_limited = False

    for post in posts:
        if total and total % 50 == 0:
            progress.tick("moltbook", total, len(posts),
                          updated=updated, deleted=deleted,
                          errors=errors, skipped=skipped)
        if rate_limited:
            break
        total += 1
        post_id, our_url, thread_url = post["id"], post["our_url"], post.get("thread_url")
        prev_upvotes, prev_comments = post.get("upvotes"), post.get("comments_count")
        no_change = post.get("scan_no_change_count") or 0
        posted_at = _parse_dt(post.get("posted_at"))

        if no_change >= 3 and posted_at:
            pa = posted_at.replace(tzinfo=timezone.utc) if posted_at.tzinfo is None else posted_at
            if datetime.now(timezone.utc) - pa > timedelta(days=3):
                skipped += 1
                continue

        # Extract post UUID and optional comment UUID from our_url
        # Format: https://www.moltbook.com/post/{post_uuid}#{comment_uuid}
        # Also handles bare fragments like "#abc123" by falling back to thread_url
        effective_url = our_url
        if not our_url.startswith("http"):
            # Bare fragment (e.g. "#f504d6fb") - reconstruct from thread_url
            if thread_url and thread_url.startswith("http"):
                thread_uuids = re.findall(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", thread_url)
                if not thread_uuids:
                    # thread_url might have short UUID too - extract what we can
                    m = re.search(r"/post/([0-9a-f-]+)", thread_url)
                    if m:
                        effective_url = thread_url + our_url  # append fragment
                    else:
                        errors += 1
                        continue
                else:
                    effective_url = f"https://www.moltbook.com/post/{thread_uuids[0]}{our_url}"
            else:
                errors += 1
                continue

        uuids = re.findall(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", effective_url)
        if not uuids:
            # Try short UUID format: /post/{short_id}
            m = re.search(r"/post/([0-9a-f]{7,})", effective_url)
            if m:
                # Short UUID - API won't accept it, skip gracefully
                _http_patch_post(post_id, {"stamp_status_checked_now": True})
                continue
            errors += 1
            continue

        post_uuid = uuids[0]
        comment_uuid = None
        if "#" in effective_url and len(uuids) >= 2:
            comment_uuid = uuids[1]
        elif "#" in effective_url:
            # Comment UUID might be short (not full UUID) - extract after #
            fragment = effective_url.split("#")[-1]
            # Strip "comment-" prefix if present
            fragment = re.sub(r'^comment-', '', fragment)
            if fragment and fragment != post_uuid and re.match(r'^[0-9a-f-]{5,}$', fragment):
                comment_uuid = fragment

        is_comment = comment_uuid is not None
        is_our_post = our_url == thread_url  # Original post if our_url matches thread_url

        if is_comment:
            # Fetch comment-specific stats via comments endpoint
            try:
                data = fetch_moltbook_json(
                    f"https://www.moltbook.com/api/v1/posts/{post_uuid}/comments?sort=new&limit=100",
                    api_key=api_key,
                )
            except MoltbookRateLimitedError as e:
                if not quiet:
                    print(f"Moltbook rate-limited for {int(e.reset_seconds)}s, stopping scan", flush=True)
                rate_limited = True
                continue
            except MoltbookNotFoundError:
                # Post deleted on Moltbook - use detection counter
                detect_count, status_set = _http_detect_deletion(post_id, "deleted", threshold=2)
                if status_set:
                    deleted += 1
                    if not quiet:
                        print(f"DELETED (Moltbook 404) [{post_id}] (confirmed after {detect_count} detections)")
                elif not quiet:
                    print(f"DELETION PENDING (Moltbook 404) [{post_id}] (detection {detect_count}/2)")
                continue
            if not data or not data.get("success"):
                errors += 1
                continue

            # Find our comment by UUID - try multiple matching strategies
            our_comment = None
            # Strip "comment-" prefix for matching
            clean_uuid = re.sub(r'^comment-', '', comment_uuid)
            for c in data.get("comments", []):
                cid = c.get("id", "")
                # Match by: full UUID, starts-with (8 chars), or contains
                if cid == clean_uuid or cid.startswith(clean_uuid[:8]) or clean_uuid in cid:
                    our_comment = c
                    break

            if not our_comment:
                has_more = data.get("has_more", False)
                total_comments = data.get("count", 0)
                if has_more or total_comments > 100:
                    # Comment is buried beyond first page — not an error, just unreachable
                    _http_patch_post(post_id, {"stamp_status_checked_now": True,
                                               "reset_deletion_detect_count": True})
                else:
                    # Post has few comments but ours is missing — likely deleted
                    detect_count, status_set = _http_detect_deletion(post_id, "deleted", threshold=2)
                    if status_set:
                        deleted += 1
                        if not quiet:
                            print(f"DELETED (Moltbook comment missing) [{post_id}] (confirmed after {detect_count} detections)")
                    elif not quiet:
                        print(f"DELETION PENDING (Moltbook comment missing) [{post_id}] (detection {detect_count}/2)")
                continue

            if our_comment.get("is_deleted"):
                detect_count, status_set = _http_detect_deletion(post_id, "deleted", threshold=2)
                if status_set:
                    deleted += 1
                continue

            # Comment-specific engagement
            comment_upvotes = our_comment.get("upvotes", 0)
            comment_score = our_comment.get("score", 0)
            # Server's `reply_count` is stale/zero on many comments; len(replies) is authoritative.
            replies_list = our_comment.get("replies") or []
            comment_replies = max(our_comment.get("reply_count") or 0, len(replies_list))
            verification = our_comment.get("verification_status", "unknown")
            thread_comment_count = data.get("count", 0)

            patch = {"upvotes": comment_upvotes, "comments_count": comment_replies,
                     "stamp_engagement_now": True, "stamp_status_checked_now": True,
                     "reset_deletion_detect_count": True}
            if comment_upvotes == prev_upvotes and comment_replies == prev_comments:
                patch["scan_no_change_delta"] = 1
            else:
                patch["scan_no_change_count"] = 0
            _http_patch_post(post_id, patch)
            updated += 1
            results.append({"id": post_id, "upvotes": comment_upvotes,
                            "replies": comment_replies, "verification": verification})
        else:
            # Original post - fetch post-level stats
            try:
                data = fetch_moltbook_json(
                    f"https://www.moltbook.com/api/v1/posts/{post_uuid}",
                    api_key=api_key,
                )
            except MoltbookRateLimitedError as e:
                if not quiet:
                    print(f"Moltbook rate-limited for {int(e.reset_seconds)}s, stopping scan", flush=True)
                rate_limited = True
                continue
            except MoltbookNotFoundError:
                # Post deleted on Moltbook - use detection counter
                detect_count, status_set = _http_detect_deletion(post_id, "deleted", threshold=2)
                if status_set:
                    deleted += 1
                    if not quiet:
                        print(f"DELETED (Moltbook 404) [{post_id}] (confirmed after {detect_count} detections)")
                elif not quiet:
                    print(f"DELETION PENDING (Moltbook 404) [{post_id}] (detection {detect_count}/2)")
                continue
            if not data or not data.get("success"):
                errors += 1
                continue

            post_data = data.get("post", {})
            if post_data.get("is_deleted"):
                detect_count, status_set = _http_detect_deletion(post_id, "deleted", threshold=2)
                if status_set:
                    deleted += 1
                continue

            upvotes = post_data.get("upvotes", 0)
            comment_count = post_data.get("comment_count", post_data.get("comments_count", 0))
            score = post_data.get("score", 0)
            views = post_data.get("views", 0)

            patch = {"upvotes": upvotes, "comments_count": comment_count, "views": views,
                     "stamp_engagement_now": True, "stamp_status_checked_now": True,
                     "reset_deletion_detect_count": True}
            if upvotes == prev_upvotes and comment_count == prev_comments:
                patch["scan_no_change_delta"] = 1
            else:
                patch["scan_no_change_count"] = 0
            _http_patch_post(post_id, patch)
            updated += 1
            results.append({"id": post_id, "upvotes": upvotes, "score": score,
                            "comments": comment_count})

    progress.done("moltbook", len(posts),
                  updated=updated, deleted=deleted,
                  errors=errors, skipped=skipped)
    if skipped and not quiet:
        print(f"  Skipped {skipped} stable Moltbook posts (3+ scans unchanged, older than 3 days)")
    return {"total": total, "updated": updated, "deleted": deleted, "errors": errors,
            "skipped": skipped, "results": results}


def _detect_minimized_github_comments(db, posts, quiet=False):
    """Pre-pass: batch-query GitHub GraphQL for isMinimized on our active
    comments and flip status='deleted' on matches.

    Why this exists: REST `repos/{o}/{r}/issues/comments/{id}` returns 200
    for a comment that's been hidden via "Hide -> low quality / off-topic /
    spam". The reactions count zeroes out, the body is unchanged, and the
    REST loop happily updates engagement as if the comment were still
    visible. The antiwork/gumroad block on 2026-05-01 was found via inbound
    notification email, not via our own pipeline. GraphQL exposes the
    moderation state via `Issue.comments.nodes[].isMinimized`.

    Cost is cheap: one GraphQL query fetches all comments on a thread (1
    rate-limit point), and aliasing batches ~10 threads per query at the
    same 1-point cost. Three thousand active threads -> ~300 points, well
    inside the 5000/hr ceiling.

    Defensive on purpose. Any failure here logs and returns; the REST loop
    that follows is the established hot path and must not be blocked by a
    GraphQL outage.
    """
    import subprocess
    from collections import defaultdict

    BATCH = 10
    comment_re = re.compile(
        r"https?://github\.com/([^/]+)/([^/]+)/(?:issues|pull)/(\d+)#issuecomment-(\d+)"
    )

    # Group: (owner, repo, number) -> [(post_id, comment_id), ...]
    threads = defaultdict(list)
    for post in posts:
        m = comment_re.match((post.get("our_url") or ""))
        if not m:
            continue
        owner, repo, number, cid = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
        threads[(owner, repo, number)].append((post["id"], cid))

    if not threads:
        return 0

    keys = list(threads.keys())
    minimized = 0
    failures = 0

    for batch_start in range(0, len(keys), BATCH):
        batch = keys[batch_start:batch_start + BATCH]
        parts = []
        for i, (owner, repo, number) in enumerate(batch):
            parts.append(
                f't{i}: repository(owner: "{owner}", name: "{repo}") {{ '
                f'issueOrPullRequest(number: {number}) {{ '
                f'... on Issue {{ comments(first: 100) {{ nodes {{ databaseId isMinimized minimizedReason }} }} }} '
                f'... on PullRequest {{ comments(first: 100) {{ nodes {{ databaseId isMinimized minimizedReason }} }} }} '
                f'}} }}'
            )
        query = "{ " + " ".join(parts) + " rateLimit { remaining } }"
        try:
            proc = subprocess.run(
                ["gh", "api", "graphql", "-f", f"query={query}"],
                capture_output=True, text=True, timeout=30,
            )
        except Exception as e:
            failures += 1
            if not quiet:
                print(f"  github-minimize: graphql exec failed batch {batch_start}: {e}",
                      flush=True)
            continue
        if proc.returncode != 0:
            failures += 1
            if not quiet:
                print(f"  github-minimize: graphql rc={proc.returncode} batch {batch_start}: "
                      f"{(proc.stderr or '')[:200]}", flush=True)
            continue
        try:
            data = json.loads(proc.stdout).get("data", {}) or {}
        except Exception:
            failures += 1
            continue

        for i, key in enumerate(batch):
            node = data.get(f"t{i}") or {}
            iop = node.get("issueOrPullRequest")
            if not iop:
                continue
            comments = (iop.get("comments") or {}).get("nodes") or []
            min_set = {c["databaseId"]: c.get("minimizedReason")
                       for c in comments if c.get("isMinimized")}
            if not min_set:
                continue
            for post_id, cid in threads[key]:
                if cid in min_set:
                    reason = min_set[cid] or ""
                    _http_mark_minimized(post_id, reason)
                    minimized += 1
                    if not quiet:
                        owner, repo, number = key
                        print(f"MINIMIZED [{post_id}] {owner}/{repo}#{number} reason={reason}",
                              flush=True)

    if not quiet:
        rl_note = f", failures={failures}" if failures else ""
        print(f"  github-minimize: flipped {minimized} hidden comments "
              f"across {len(threads)} threads{rl_note}", flush=True)
    return minimized


_REPO_STATE_CACHE_US = {}


def _classify_github_404(owner, repo, number, comment_id, quiet=False):
    """Disambiguate a REST 404 on a GitHub issue/PR comment.

    Returns one of:
      - 'repo_gone'        : `repos/{o}/{r}` itself 404s
      - 'issue_deleted'    : repo is live but `repos/{o}/{r}/issues/{n}` is
                             404/410 (issue was deleted by author/admin)
      - 'feature_disabled' : repo is live, issue is reachable, but
                             has_issues=false (every comment under the
                             feature 404s, not specific to us)
      - 'transient'        : repo + issue both alive, and GraphQL says our
                             specific comment IS present and not minimized.
                             REST returned 404 by mistake (rate-limit blip,
                             secondary throttle, network); do NOT count this
                             as a strike.
      - 'comment_deleted'  : repo + issue both alive, GraphQL says our
                             comment is NOT in the thread (genuine deletion,
                             or hidden in a way we don't see).
      - 'unknown'          : a follow-up call failed; caller should fall
                             back to count-based detection.

    Cached per-process on repo metadata to keep the audit cheap. Adds at
    most 2 extra gh-api calls per 404, gated by single-repo caching.
    """
    import subprocess

    key = f"{owner.lower()}/{repo.lower()}"
    cached_repo = _REPO_STATE_CACHE_US.get(key)
    if cached_repo is None:
        try:
            proc = subprocess.run(
                ["gh", "api", f"repos/{owner}/{repo}"],
                capture_output=True, text=True, timeout=20,
            )
        except Exception as e:
            if not quiet:
                print(f"  github-classify: repo check failed {owner}/{repo}: {e}",
                      flush=True)
            return "unknown"
        if proc.returncode != 0:
            err = ((proc.stderr or "") + (proc.stdout or "")).lower()
            if "not found" in err or "http 404" in err:
                cached_repo = {"state": "repo_gone"}
            else:
                cached_repo = {"state": "unknown"}
        else:
            try:
                data = json.loads(proc.stdout or "{}")
            except Exception:
                data = {}
            cached_repo = {
                "state": "live",
                "has_issues": bool(data.get("has_issues", True)),
            }
        _REPO_STATE_CACHE_US[key] = cached_repo

    if cached_repo["state"] == "repo_gone":
        return "repo_gone"
    if cached_repo["state"] == "unknown":
        return "unknown"
    if not cached_repo.get("has_issues", True):
        return "feature_disabled"

    # Repo is live. Check the specific issue/PR thread via REST first
    # (cheaper than GraphQL for this gate). 410 + 404 are both "thread gone".
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/issues/{number}"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return "unknown"
    if proc.returncode != 0:
        err = ((proc.stderr or "") + (proc.stdout or "")).lower()
        if ("not found" in err or "http 404" in err
                or "http 410" in err or "this issue was deleted" in err):
            return "issue_deleted"
        # Could be 403/permissions; fall through to GraphQL to be sure.

    # Repo + issue are reachable; verify the specific comment via GraphQL.
    # If our comment_id shows up in `comments.nodes[].databaseId` and is not
    # minimized, REST 404 was transient. If it's absent, it's truly gone.
    try:
        # Pull a wide range of comments; 250 is well within GraphQL's 100/page
        # limit when combined with `after` paginations, but for simplicity we
        # just fetch up to 100 here. If the comment is beyond the first 100
        # we'll return 'unknown' to be safe (caller falls back to count-based).
        query = (
            f'{{ repository(owner: "{owner}", name: "{repo}") {{ '
            f'issueOrPullRequest(number: {number}) {{ '
            f'... on Issue {{ comments(first: 100) {{ '
            f'nodes {{ databaseId isMinimized }} '
            f'pageInfo {{ hasNextPage }} }} }} '
            f'... on PullRequest {{ comments(first: 100) {{ '
            f'nodes {{ databaseId isMinimized }} '
            f'pageInfo {{ hasNextPage }} }} }} '
            f'}} }} }}'
        )
        proc = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return "unknown"
        data = json.loads(proc.stdout).get("data", {}) or {}
    except Exception:
        return "unknown"

    iop = ((data.get("repository") or {}).get("issueOrPullRequest")) or {}
    if not iop:
        # Either repo missing in GraphQL (shouldn't happen if REST said live)
        # or issue/PR not visible. Treat as issue_deleted equivalent.
        return "issue_deleted"
    comments = (iop.get("comments") or {}).get("nodes") or []
    cid_int = int(comment_id)
    for n in comments:
        if int(n.get("databaseId") or 0) == cid_int:
            if n.get("isMinimized"):
                # Pre-pass should already have flipped this; defer to its path.
                return "comment_deleted"
            return "transient"
    # Comment not in the first 100 nodes. If the thread is paginated, we
    # can't be sure; report unknown so count-based detection takes over.
    has_more = (iop.get("comments") or {}).get("pageInfo", {}).get("hasNextPage")
    if has_more:
        return "unknown"
    return "comment_deleted"


def refresh_github(db, quiet=False, limit=None):
    """Fetch engagement on our GitHub issue/PR comments via `gh api`.

    Stores reactions.total_count in posts.upvotes and the count of replies
    detected by scan_github_replies.py in posts.comments_count.

    Runs a GraphQL `isMinimized` pre-pass before the REST loop so hidden
    comments are flipped to status='deleted' and skipped by the REST select.
    """
    import subprocess

    posts = _http_list_github_active_posts(limit)

    # Pre-pass: flag minimized (hidden) comments before REST. Wrapped
    # defensively, a GraphQL flake must not block the REST hot path.
    try:
        _detect_minimized_github_comments(db, posts, quiet=quiet)
    except Exception as e:
        if not quiet:
            print(f"  github-minimize: pre-pass crashed, skipping: {e}", flush=True)
    # Re-select after the pre-pass so flipped rows drop out of the REST loop.
    posts = _http_list_github_active_posts(limit)

    total = updated = deleted = errors = repo_gone = transient_skipped = 0
    results = []
    # Capture issue/PR number so we can re-verify comment state on 404.
    comment_url_re = re.compile(
        r"https?://github\.com/([^/]+)/([^/]+)/(?:issues|pull)/(\d+)#issuecomment-(\d+)"
    )

    for post in posts:
        total += 1
        post_id, our_url = post["id"], post.get("our_url")

        m = comment_url_re.match(our_url or "")
        if not m:
            errors += 1
            continue
        owner, repo, number, comment_id = m.group(1), m.group(2), m.group(3), m.group(4)

        try:
            proc = subprocess.run(
                ["gh", "api", f"repos/{owner}/{repo}/issues/comments/{comment_id}"],
                capture_output=True, text=True, timeout=30,
            )
        except Exception:
            errors += 1
            continue

        if proc.returncode != 0:
            err_text = (proc.stderr or "") + (proc.stdout or "")
            if "rate limit" in err_text.lower() or "secondary rate limit" in err_text.lower() or "abuse detection" in err_text.lower():
                if not quiet:
                    print(f"  github: rate-limited at {total}/{len(posts)}, sleeping 60s", flush=True)
                time.sleep(60)
                errors += 1
                continue
            if "Not Found" in err_text or "HTTP 404" in err_text or "HTTP 410" in err_text:
                # Disambiguate the 404. A bare comment 404 means one of:
                #   1. parent repo was deleted (every comment 404s)
                #   2. issue/PR thread was deleted (every comment under it 404s)
                #   3. repo has has_issues=false (collateral, not moderation)
                #   4. our specific comment was deleted or hidden
                #   5. transient GitHub error returning 404 for a live comment
                #      (HOLYKEYZ case, 2026-05-09: REST gave 404 twice but
                #      the comment was alive in both REST and GraphQL once we
                #      re-checked. Two transient 404s within the cron's polling
                #      window will otherwise flip the post to status='deleted'.)
                # Categories 1-3 are not moderation strikes; tag them as
                # 'repo_gone' so strike_alert.py's filter drops them. Category
                # 5 must reset detect_count to 0 so the next scan starts fresh.
                cls = _classify_github_404(owner, repo, number, comment_id, quiet=quiet)
                if cls in ("repo_gone", "issue_deleted", "feature_disabled"):
                    _http_patch_post(post_id, {"status": "repo_gone",
                                               "stamp_status_checked_now": True})
                    repo_gone += 1
                    if not quiet:
                        print(f"REPO_GONE (github {cls}) [{post_id}] {owner}/{repo}#{number}", flush=True)
                    continue
                if cls == "transient":
                    # REST said 404 but GraphQL confirms our comment is alive
                    # and not minimized. False positive; reset the strike
                    # counter so we don't accumulate it.
                    _http_patch_post(post_id, {"reset_deletion_detect_count": True,
                                               "stamp_status_checked_now": True})
                    transient_skipped += 1
                    if not quiet:
                        print(f"TRANSIENT-404 (github) [{post_id}] {owner}/{repo}#{number} "
                              f"comment {comment_id} alive in GraphQL, resetting count",
                              flush=True)
                    continue
                # cls == 'comment_deleted' (GraphQL confirms it's gone) or
                # 'unknown' (GraphQL itself failed; bump the counter without
                # flipping so a real deletion still gets caught eventually).
                # comment_deleted flips at threshold 2; unknown never flips
                # (threshold 10**9 = bump-only).
                threshold = 2 if cls == "comment_deleted" else 10 ** 9
                detect_count, status_set = _http_detect_deletion(post_id, "deleted", threshold=threshold)
                if status_set:
                    deleted += 1
                    if not quiet:
                        print(f"DELETED (github 404 + graphql confirmed) [{post_id}]", flush=True)
            else:
                errors += 1
            continue

        try:
            data = json.loads(proc.stdout)
        except Exception:
            errors += 1
            continue

        reactions = data.get("reactions") or {}
        total_reactions = int(reactions.get("total_count") or 0)

        # reply_count is folded into the active-for-stats list via a correlated
        # subquery, so no per-post COUNT round trip is needed.
        reply_count = int(post.get("reply_count") or 0)

        _http_patch_post(post_id, {
            "upvotes": total_reactions,
            "comments_count": reply_count,
            "stamp_engagement_now": True,
            "stamp_status_checked_now": True,
            "reset_deletion_detect_count": True,
        })
        updated += 1
        if total_reactions or reply_count:
            results.append({
                "id": post_id,
                "reactions": total_reactions,
                "replies": reply_count,
                "url": our_url,
            })

        time.sleep(0.1)

        if total % 100 == 0:
            progress.tick("github", total, len(posts),
                          updated=updated, deleted=deleted, errors=errors)
            if not quiet:
                print(f"  github: {total}/{len(posts)} processed "
                      f"(updated={updated}, deleted={deleted}, "
                      f"repo_gone={repo_gone}, transient={transient_skipped}, "
                      f"errors={errors})",
                      flush=True)

    progress.done("github", len(posts),
                  updated=updated, deleted=deleted, errors=errors)
    if not quiet:
        print(f"  github: done (updated={updated}, deleted={deleted}, "
              f"repo_gone={repo_gone}, transient={transient_skipped}, "
              f"errors={errors})", flush=True)
    return {"total": total, "updated": updated, "deleted": deleted,
            "repo_gone": repo_gone, "transient_skipped": transient_skipped,
            "errors": errors, "results": results}


def refresh_twitter(db, config=None, quiet=False, audit_mode=False):
    """Fetch Twitter/X stats via fxtwitter API (no browser needed).

    Two cadences split by post age so the per-6h job and the daily audit don't
    fight over the same column:

      Per-6h (audit_mode=False): hot tier, posts younger than 7 days, gated at
        5h staleness. Hit by stats-twitter every 6 hours so each fresh tweet is
        polled ~4x per day. Deletion detection runs here too so a deleted hot
        tweet is caught within hours instead of waiting on the daily audit.

      Daily audit (audit_mode=True): cold tier, posts older than 7 days. Hit
        by audit-twitter at 04:13. Stable-skip (3+ unchanged scans + posted_at
        older than 5 days) keeps the long tail cheap; deletion detection
        confirms removed tweets after 2 strikes.

    Multi-account safety (2026-05-19): the read is scoped to THIS machine's
    Twitter handle so two machines (e.g. local-mac as @m13v_, mk0r VM as
    @matt_diak) never refresh each other's posts. Without scoping, both
    crons would burn fxtwitter quota on the union, race on engagement
    column writes, and the dashboard would render whichever machine
    finished last. The handle comes from twitter_account.resolve_handle()
    which reads `AUTOPOSTER_TWITTER_HANDLE` env or `accounts.twitter.handle`
    in config.json.

    Before this split, audit refreshed every active row daily and stamped
    engagement_updated_at on all of them, which silently locked the per-6h
    job out of the hot tier for a week at a time.

    `db` is accepted for signature compatibility with the orchestrator but
    no direct SQL runs here — every read/write goes through HTTP so the VM
    (no DATABASE_URL) can run this branch too.
    """
    from twitter_account import resolve_handle as _resolve_twitter_handle
    config = config or {}

    handle = _resolve_twitter_handle()
    if not handle:
        if not quiet:
            print("  twitter: no handle configured (AUTOPOSTER_TWITTER_HANDLE / "
                  "accounts.twitter.handle); skipping refresh", flush=True)
        return {"total": 0, "updated": 0, "changed": 0, "deleted": 0,
                "suspended": 0, "errors": 0, "skipped": 0, "results": []}

    posts = _http_list_twitter_active_posts(
        our_account=handle, audit_mode=audit_mode, stale_hours=5,
    )

    total = updated = changed = deleted = suspended = errors = skipped = 0
    # `updated`: rows the fxtwitter API answered for and we wrote back (i.e.
    #   successful polls). Effectively `total - errors - skipped - 404s`.
    # `changed`: subset of `updated` where views OR likes actually moved since
    #   the prior scan. This is the signal the dashboard's "updated" pill
    #   surfaces (per log_run.py --updated docstring), so the printed summary
    #   below uses `changed` for the "updated" field. Before 2026-05-08 the
    #   summary printed `updated` (= every successful poll), making
    #   "checked == updated" identically equal whenever there were no errors,
    #   which hid the fact that ~55% of hot-tier polls return identical stats.
    results = []

    for post in posts:
        total += 1
        # The HTTP shape is a dict; the previous direct-SQL shape was a tuple.
        # Read by column name so callers downstream stay decoupled from SQL
        # ordinal positions.
        post_id = post.get("id")
        our_url = post.get("our_url") or ""
        no_change = int(post.get("scan_no_change_count") or 0)
        posted_at_raw = post.get("posted_at")
        prev_upvotes = post.get("upvotes")
        prev_views = post.get("views")
        prev_comments = post.get("comments_count")
        # posted_at arrives as an ISO-8601 string over JSON; parse to a tz-aware
        # datetime so the audit-mode age check still works.
        if isinstance(posted_at_raw, str) and posted_at_raw:
            try:
                posted_at = datetime.fromisoformat(posted_at_raw.replace("Z", "+00:00"))
            except ValueError:
                posted_at = None
        else:
            posted_at = posted_at_raw

        # Stable-skip applies only to the cold tier (audit). The hot tier's
        # SQL filter restricts to posted_at > NOW() - 7d, so the "older than
        # 5 days" branch can only fire in audit mode anyway.
        if audit_mode and no_change >= 3 and posted_at:
            age = datetime.now(timezone.utc) - (posted_at.replace(tzinfo=timezone.utc) if posted_at.tzinfo is None else posted_at)
            if age > timedelta(days=5):
                skipped += 1
                continue

        # Extract tweet ID from URL
        tweet_id = re.search(r'/status/(\d+)', our_url or '')
        if not tweet_id:
            errors += 1
            continue
        tweet_id = tweet_id.group(1)

        # Extract username from URL
        username = re.search(r'x\.com/([^/]+)/status', our_url or '')
        if not username:
            username = re.search(r'twitter\.com/([^/]+)/status', our_url or '')
        username = username.group(1) if username else 'i'

        url = f"https://api.fxtwitter.com/{username}/status/{tweet_id}"
        # fxtwitter returns HTTP 404 for malformed/non-existent handles
        # (e.g. corrupted our_url rows). Catch HttpNotFoundError and route
        # to the same in-body 404 handler below so a single bad row does
        # not abort the whole pipeline.
        try:
            data = fetch_json(url)
        except HttpNotFoundError as e:
            # Preserve fxtwitter's 404 body: a tombstone (guest-API blind spot)
            # is ALIVE and must reach the tombstone guard below, NOT be treated
            # as a deletion. Only fall back to a synthetic null-tweet 404 when
            # the body was genuinely empty (true NOT_FOUND).
            data = e.body or {"code": 404, "tweet": None}

        if not data:
            # Retry once
            time.sleep(2)
            try:
                data = fetch_json(url)
            except HttpNotFoundError as e:
                data = e.body or {"code": 404, "tweet": None}
            if not data:
                errors += 1
                continue

        code = data.get("code", 0)
        tweet = data.get("tweet")

        # fxtwitter is an UNAUTHENTICATED guest API. For tweets it cannot read
        # as a logged-out viewer (Community-scoped posts, some replies,
        # protected / age-gated contexts) it returns code 404 with a
        # *tombstone* object (type="tombstone", reason="unavailable") instead
        # of a null tweet. Those tweets are alive to a logged-in viewer, so
        # treating the tombstone as a deletion produced false strikes: on
        # 2026-06-05, 5 of 6 twitter strike emails were tombstone-unavailable
        # rows that were LIVE in the authenticated harness (#35715/#35712
        # Community posts; #31131/#31130/#29509 normal replies). Only a genuine
        # NOT_FOUND (tweet is None / no tombstone) is a real deletion signal.
        # Skip tombstones WITHOUT bumping deletion_detect_count, mirroring the
        # Reddit "bias: don't falsely mark deleted" rule. strike_alert.py's
        # twitter live-recheck is the second safety net for anything that slips.
        if isinstance(tweet, dict) and tweet.get("type") == "tombstone":
            skipped += 1
            if not quiet:
                _reason = tweet.get("reason") or "?"
                print(f"TOMBSTONE [{post_id}] reason={_reason} "
                      f"(guest-API blind spot, not a deletion)")
            continue

        if code == 404 or tweet is None:
            # Tweet not found, could be deleted or suspended. Run the 2-strike
            # confirmation atomically server-side via /detect-deletion so the
            # bump+threshold check is one HTTP round trip instead of read +
            # write. detect_count = the new value after bump; status_set=True
            # when the threshold was met and posts.status flipped to 'deleted'.
            detect_count, status_set = _http_detect_deletion(post_id, "deleted", threshold=2)
            if status_set:
                deleted += 1
                if not quiet:
                    print(f"DELETED [{post_id}] (confirmed after {detect_count} detections)")
            else:
                if not quiet:
                    print(f"DELETION PENDING [{post_id}] (detection {detect_count}/2)")
            continue

        # Extract stats
        views = tweet.get("views") or 0
        likes = tweet.get("likes") or 0
        replies = tweet.get("replies") or 0
        retweets = tweet.get("retweets") or 0
        bookmarks = tweet.get("bookmarks") or 0

        # Track no-change so the next-poll cycle can skip stable posts. Compute
        # this BEFORE the PATCH so we send the right scan_no_change_delta in
        # the same call (server-side: +1 to bump, signal a reset via the
        # current absolute value approach below).
        stayed_same = (likes == prev_upvotes
                       and views == prev_views
                       and replies == prev_comments)

        # One PATCH per post: stats + freshness stamps + counter delta + the
        # deletion_detect_count reset (the row didn't 404 this round). The
        # server keys "scan_no_change_delta=+1 then reset_via=N=0" off the
        # absolute value when we send scan_no_change_count=0; the +1 bump
        # path uses scan_no_change_delta=1 so the row's prior count is
        # incremented atomically without read-modify-write race conditions.
        patch_body = {
            "views": int(views),
            "upvotes": int(likes),
            "comments_count": int(replies),
            "stamp_engagement_now": True,
            "stamp_status_checked_now": True,
            "reset_deletion_detect_count": True,
        }
        if stayed_same:
            patch_body["scan_no_change_delta"] = 1
        else:
            patch_body["scan_no_change_count"] = 0
        _http_patch_post(post_id, patch_body)

        # snapshot_post_views: separate POST so a transient failure here only
        # loses today's per-day rollup datapoint, not the parent stats update.
        _http_snapshot_post_views(post_id, views)

        updated += 1
        if not stayed_same:
            changed += 1
        results.append({"id": post_id, "views": views, "likes": likes,
                        "replies": replies, "retweets": retweets})

        # Rate limit: 1 request per second to be safe with fxtwitter
        time.sleep(1)

        # Progress tick every 50 polls. No db.commit() needed: each
        # _http_patch_post / _http_snapshot_post_views is its own
        # auto-committed transaction server-side.
        if total % 50 == 0:
            progress.tick("twitter", total, len(posts),
                          updated=updated, changed=changed, deleted=deleted,
                          suspended=suspended, errors=errors, skipped=skipped)

    progress.done("twitter", len(posts),
                  updated=updated, changed=changed, deleted=deleted,
                  suspended=suspended, errors=errors, skipped=skipped)
    if skipped and not quiet:
        print(f"  Skipped {skipped} stable tweets (3+ scans unchanged, older than 5 days)")

    # Second pass: refresh the human-top-reply snapshots we captured at our
    # post-success time. Same fxtwitter cadence as posts (1 req/s), same
    # 2-strike deletion guard, same install-scope filter. We only do this in
    # hot mode; the cold audit doesn't poll the snapshot rows because the
    # benchmark question ("how did the human top-reply grow vs ours?") is
    # only meaningful while the parent post is also being polled.
    ttr_total = ttr_updated = ttr_changed = ttr_deleted = ttr_errors = 0
    if not audit_mode:
        # Freshness override for ad-hoc reruns. Cron uses the 5h default;
        # setting SAPS_TTR_STALE_HOURS=0 forces every active row through this
        # cycle (useful right after a capture cycle to watch the refresh loop).
        try:
            _ttr_stale = float(os.environ.get("SAPS_TTR_STALE_HOURS", "5"))
        except ValueError:
            _ttr_stale = 5.0
        ttr_rows = _http_list_twitter_top_replies_to_refresh(stale_hours=_ttr_stale)
        for row in ttr_rows:
            ttr_total += 1
            ttr_id = row.get("id")
            reply_url = row.get("reply_url") or ""
            reply_tweet_id = row.get("reply_tweet_id")
            prev_likes = row.get("likes")
            prev_views = row.get("views")
            prev_replies = row.get("replies")

            if not reply_tweet_id:
                m = re.search(r"/status/(\d+)", reply_url)
                reply_tweet_id = m.group(1) if m else None
            if not reply_tweet_id:
                ttr_errors += 1
                continue
            m_user = re.search(r"x\.com/([^/]+)/status", reply_url) or \
                     re.search(r"twitter\.com/([^/]+)/status", reply_url)
            username = m_user.group(1) if m_user else "i"

            url = f"https://api.fxtwitter.com/{username}/status/{reply_tweet_id}"
            try:
                data = fetch_json(url)
            except HttpNotFoundError:
                data = {"code": 404, "tweet": None}
            if not data:
                time.sleep(2)
                try:
                    data = fetch_json(url)
                except HttpNotFoundError:
                    data = {"code": 404, "tweet": None}
                if not data:
                    ttr_errors += 1
                    continue

            code = data.get("code", 0)
            tweet = data.get("tweet")
            if code == 404 or tweet is None:
                detect_count, status_set = _http_detect_deletion_top_reply(
                    ttr_id, "deleted", threshold=2,
                )
                if status_set:
                    ttr_deleted += 1
                    if not quiet:
                        print(f"  top_reply DELETED [{ttr_id}] "
                              f"(confirmed after {detect_count} detections)")
                continue

            likes = tweet.get("likes") or 0
            views = tweet.get("views") or 0
            replies = tweet.get("replies") or 0
            retweets = tweet.get("retweets") or 0
            stayed_same = (likes == prev_likes and views == prev_views
                           and replies == prev_replies)
            patch_body = {
                "likes": int(likes),
                "views": int(views),
                "replies": int(replies),
                "retweets": int(retweets),
                "stamp_engagement_now": True,
                "stamp_status_checked_now": True,
                "reset_deletion_detect_count": True,
            }
            if stayed_same:
                patch_body["scan_no_change_delta"] = 1
            else:
                patch_body["scan_no_change_count"] = 0
            _http_patch_top_reply(ttr_id, patch_body)
            ttr_updated += 1
            if not stayed_same:
                ttr_changed += 1
            time.sleep(1)

        if not quiet and ttr_total:
            print(f"  thread_top_replies: checked={ttr_total} updated={ttr_updated} "
                  f"changed={ttr_changed} deleted={ttr_deleted} errors={ttr_errors}")

    return {"total": total, "updated": updated, "changed": changed,
            "deleted": deleted, "suspended": suspended,
            "errors": errors, "skipped": skipped, "results": results,
            "thread_top_replies": {
                "total": ttr_total, "updated": ttr_updated,
                "changed": ttr_changed, "deleted": ttr_deleted,
                "errors": ttr_errors,
            }}


def refresh_reddit_replies(db, user_agent, quiet=False):
    """Refresh score + reply count for our Reddit comments stored in `replies`.

    Uses batch_fetch_info (up to 100 t1_ IDs per API call) so the whole table
    typically scans in 1-3 hits. Reddit doesn't expose per-comment views, so
    `views` stays 0. Skips rows refreshed within FRESH_WINDOW.
    """
    from reddit_tools import batch_fetch_info, RateLimitedError

    FRESH_WINDOW = timedelta(hours=4)
    now_utc = datetime.now(timezone.utc)

    # 2026-05-12: read via /api/v1/replies. `db` is preserved in the signature
    # for back-compat with main() callers; the value is ignored here.
    rows = _http_list_reddit_replies_to_refresh()

    pending = []
    skipped_fresh = 0
    for row in rows:
        rid = row.get("id")
        our_reply_id = row.get("our_reply_id")
        eu_raw = row.get("engagement_updated_at")
        if eu_raw:
            try:
                eu = datetime.fromisoformat(str(eu_raw).replace("Z", "+00:00"))
            except Exception:
                eu = None
            if eu:
                if eu.tzinfo is None:
                    eu = eu.replace(tzinfo=timezone.utc)
                if now_utc - eu < FRESH_WINDOW:
                    skipped_fresh += 1
                    continue
        if not our_reply_id:
            continue
        # our_reply_id is stored as bare base-36 ID (no t1_ prefix). Normalize.
        thing_id = our_reply_id if our_reply_id.startswith("t1_") else f"t1_{our_reply_id}"
        pending.append((rid, thing_id))

    total = len(pending)
    if total == 0:
        if not quiet:
            print(f"  reddit replies: nothing to refresh ({skipped_fresh} fresh)", flush=True)
        return {"total": 0, "updated": 0, "errors": 0, "skipped_fresh": skipped_fresh}

    thing_ids = [t for _, t in pending]
    try:
        info = batch_fetch_info(thing_ids, user_agent=user_agent)
    except RateLimitedError as e:
        if not quiet:
            print(f"  reddit replies: rate-limited (reset in {int(e.reset_in)}s)", flush=True)
        return {"total": total, "updated": 0, "errors": total, "skipped_fresh": skipped_fresh}
    except Exception as e:
        if not quiet:
            print(f"  reddit replies: batch fetch failed: {e}", flush=True)
        return {"total": total, "updated": 0, "errors": total, "skipped_fresh": skipped_fresh}

    updated = errors = 0
    for rid, thing_id in pending:
        d = info.get(thing_id)
        if not d:
            errors += 1
            continue
        score = int(d.get("score") or 0)
        # Count direct replies on the comment.
        replies_obj = d.get("replies", "")
        reply_count = 0
        if replies_obj and isinstance(replies_obj, dict):
            children = replies_obj.get("data", {}).get("children", [])
            reply_count = sum(1 for c in children if c.get("kind") == "t1")
            reply_count += sum(c.get("data", {}).get("count", 0)
                               for c in children if c.get("kind") == "more")
        _http_patch_reply(rid, {
            "upvotes": int(score),
            "comments_count": int(reply_count),
            "stamp_engagement_now": True,
        })
        updated += 1

    progress.done("reddit_replies", total, updated=updated, errors=errors)
    if not quiet:
        print(f"  reddit replies: {total} checked, {updated} updated, "
              f"{errors} errors, {skipped_fresh} fresh", flush=True)
    return {"total": total, "updated": updated, "errors": errors,
            "skipped_fresh": skipped_fresh}


def refresh_twitter_threads(db, config=None, quiet=False,
                            max_per_run=1000, stale_hours=20):
    """Poll fxtwitter for parent threads we've commented on and append one
    row to thread_snapshots per successful poll.

    Background: posts.thread_engagement captures one T0 snapshot at
    discovery time, twitter_candidates carries T0+T1 inside the candidate
    lifecycle, but neither covers what happens to the parent thread AFTER
    we post a comment on it. This function closes that gap: it scans every
    active twitter comment whose parent != our_url, dedupes by parent URL,
    polls fxtwitter once per second, and appends a thread_snapshots row.

    Cadence:
      - Hot tier (default): polled every 6h via stats.sh Step 3.5. Threads
        whose latest snapshot is < 5h old are skipped server-side via the
        active-for-stats endpoint.
      - Long tail (default cap): threads where our newest comment is older
        than 30 days are dropped from the candidate set; not worth the
        fxtwitter quota.

    Multi-account safety: read scoped to our_account so two machines
    (@m13v_ and @matt_diak) only refresh the parents of THEIR comments.

    Output to stats.sh log via stdout: "thread_snapshots: X scanned, Y
    written, Z deleted, W errors". DB writes go through HTTP; same lane
    as the rest of the twitter pipeline."""
    from twitter_account import resolve_handle as _resolve_twitter_handle
    config = config or {}

    handle = _resolve_twitter_handle()
    if not handle:
        if not quiet:
            print("  thread_snapshots: no handle configured; skipping", flush=True)
        return {"scanned": 0, "written": 0, "deleted": 0, "errors": 0,
                "no_change": 0}

    threads = _http_list_twitter_parent_threads(
        our_account=handle, stale_hours=int(stale_hours), max_age_days=30,
    )

    total_eligible = len(threads)
    if max_per_run and max_per_run > 0 and total_eligible > max_per_run:
        # Take the freshest-commented threads first (the active-for-stats
        # endpoint already orders by posted_at DESC). The capped-out
        # remainder will be picked up on the next cron run.
        threads = threads[:max_per_run]

    scanned = written = deleted_count = errors = no_change = 0
    rate_limit_sleep = 1.0  # fxtwitter etiquette: 1 req/sec

    for t in threads:
        scanned += 1
        thread_url = t.get("thread_url") or ""
        # Extract tweet_id + username from the URL. Twitter URLs come in
        # both x.com/<user>/status/<id> and twitter.com/<user>/status/<id>
        # shapes; fxtwitter accepts either, but we need the id either way
        # for the thread_external_id column.
        m_id = re.search(r"/status/(\d+)", thread_url)
        m_user = re.search(r"(?:x|twitter)\.com/([^/]+)/status", thread_url)
        if not m_id or not m_user:
            errors += 1
            continue
        tweet_id = m_id.group(1)
        username = m_user.group(1)

        api_url = f"https://api.fxtwitter.com/{username}/status/{tweet_id}"
        try:
            data = fetch_json(api_url)
        except HttpNotFoundError:
            data = {"code": 404, "tweet": None}
        if not data:
            # Single retry, matches refresh_twitter()'s pattern
            time.sleep(2)
            try:
                data = fetch_json(api_url)
            except HttpNotFoundError:
                data = {"code": 404, "tweet": None}

        code = (data or {}).get("code", 0)
        tweet = (data or {}).get("tweet")

        if code == 404 or tweet is None:
            # Parent thread is deleted/suspended/blocked. Record the fact
            # (so the curve has a terminal point) but don't double-poll
            # next cycle — the server-side staleness gate will see the
            # row and skip.
            _http_insert_thread_snapshot(
                "twitter", thread_url,
                thread_external_id=tweet_id,
                is_deleted=True,
                error=f"fxtwitter_code_{code}",
            )
            deleted_count += 1
            time.sleep(rate_limit_sleep)
            continue

        views = (tweet.get("views") or 0) or None
        likes = (tweet.get("likes") or 0) or None
        replies_count = (tweet.get("replies") or 0) or None
        retweets = (tweet.get("retweets") or 0) or None
        bookmarks = (tweet.get("bookmarks") or 0) or None
        # fxtwitter exposes quotes on some tweets and not others; coerce.
        quotes = tweet.get("quotes")
        if quotes is not None:
            try:
                quotes = int(quotes)
            except (TypeError, ValueError):
                quotes = None
        author = (tweet.get("author") or {}).get("screen_name") or t.get("thread_author_handle")

        # Cheap no-change short-circuit: if every counter matches the
        # previous snapshot, still insert a row so the curve has a
        # capture point at this timestamp (the dashboard surfaces the
        # frequency of polls as a freshness signal), but increment the
        # no_change counter so the stats summary makes the cost clear.
        # Postgres BIGINTs come back as JSON strings, so coerce both
        # sides through int() (None stays None) before comparing.
        def _as_int(v):
            if v is None:
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        prev_views = _as_int(t.get("last_views"))
        prev_likes = _as_int(t.get("last_likes"))
        prev_replies = _as_int(t.get("last_replies"))
        prev_retweets = _as_int(t.get("last_retweets"))
        prev_bookmarks = _as_int(t.get("last_bookmarks"))
        cur_views = _as_int(views)
        cur_likes = _as_int(likes)
        cur_replies = _as_int(replies_count)
        cur_retweets = _as_int(retweets)
        cur_bookmarks = _as_int(bookmarks)
        if (t.get("last_captured_at") is not None
                and prev_views == cur_views and prev_likes == cur_likes
                and prev_replies == cur_replies and prev_retweets == cur_retweets
                and prev_bookmarks == cur_bookmarks):
            no_change += 1

        snap_id = _http_insert_thread_snapshot(
            "twitter", thread_url,
            thread_external_id=tweet_id,
            thread_author_handle=author,
            views=views, likes=likes, replies=replies_count,
            retweets=retweets, bookmarks=bookmarks, quotes=quotes,
        )
        if snap_id is None:
            errors += 1
        else:
            written += 1

        time.sleep(rate_limit_sleep)

    capped_remaining = max(0, total_eligible - scanned)
    if not quiet:
        cap_note = f", {capped_remaining} capped" if capped_remaining else ""
        print(f"  thread_snapshots: {scanned} scanned, {written} written, "
              f"{deleted_count} deleted, {errors} errors, "
              f"{no_change} unchanged{cap_note}", flush=True)
        print("STATS_JSON: " + json.dumps({
            "platform": "twitter", "kind": "thread_snapshots",
            "scanned": scanned, "written": written, "deleted": deleted_count,
            "errors": errors, "unchanged": no_change,
            "capped_remaining": capped_remaining,
        }), flush=True)
    return {"scanned": scanned, "written": written, "deleted": deleted_count,
            "errors": errors, "no_change": no_change,
            "eligible": total_eligible, "capped_remaining": capped_remaining}


def refresh_twitter_replies(db, quiet=False):
    """Refresh per-reply stats (likes, replies count, views) for our reply
    tweets stored in `replies`. Reuses the fxtwitter API per reply tweet ID.

    Multi-account safety: the read is scoped server-side to this caller's
    install_id (via X-Installation auth), so two machines refreshing in
    parallel don't both poll the same set of reply tweets. Historical NULL-
    install_id rows are claimed by the primary local install per the
    backfill in 2026-05-19 — see active-for-stats/route.ts for the WHERE
    detail.

    `db` is accepted for orchestrator signature compatibility but the
    function makes no direct SQL calls — every read/write is HTTP.
    """
    # Tiered freshness so reply-to-replies don't rot on a flat 7-day cadence.
    # Recent replies (<=14d) still accrue likes/views, so they refresh on the
    # same ~6h cadence as our posts and top replies. Older replies have settled,
    # so a slow 7-day gate keeps fxtwitter load bounded. Age is derived from the
    # tweet's snowflake ID (no extra server field needed).
    FRESH_WINDOW_RECENT = timedelta(hours=6)
    FRESH_WINDOW_SETTLED = timedelta(days=7)
    RECENT_AGE_CUTOFF = timedelta(days=14)
    TWITTER_SNOWFLAKE_EPOCH_MS = 1288834974657
    now_utc = datetime.now(timezone.utc)

    rows = _http_list_twitter_replies_to_refresh()

    total = updated = errors = skipped_fresh = 0
    for row in rows:
        rid = row.get("id")
        url = row.get("our_reply_url") or ""
        eu_raw = row.get("engagement_updated_at")
        # engagement_updated_at arrives as ISO-8601 over JSON.
        if isinstance(eu_raw, str) and eu_raw:
            try:
                eu = datetime.fromisoformat(eu_raw.replace("Z", "+00:00"))
            except ValueError:
                eu = None
        else:
            eu = eu_raw
        # Pick the freshness window by reply age (snowflake-derived). Recent
        # replies refresh fast; settled ones stay on the slow cadence.
        fresh_window = FRESH_WINDOW_SETTLED
        _idm = re.search(r'/status/(\d+)', url or '')
        if _idm:
            try:
                _created_ms = (int(_idm.group(1)) >> 22) + TWITTER_SNOWFLAKE_EPOCH_MS
                _age = now_utc - datetime.fromtimestamp(_created_ms / 1000.0, timezone.utc)
                if _age <= RECENT_AGE_CUTOFF:
                    fresh_window = FRESH_WINDOW_RECENT
            except (ValueError, OverflowError, OSError):
                pass
        if eu:
            if eu.tzinfo is None:
                eu = eu.replace(tzinfo=timezone.utc)
            if now_utc - eu < fresh_window:
                skipped_fresh += 1
                continue

        total += 1
        m = re.search(r'/status/(\d+)', url or '')
        if not m:
            errors += 1
            continue
        tweet_id = m.group(1)
        username_m = re.search(r'(?:x|twitter)\.com/([^/]+)/status', url or '')
        username = username_m.group(1) if username_m else 'i'

        api_url = f"https://api.fxtwitter.com/{username}/status/{tweet_id}"
        # See refresh_twitter() — same HttpNotFoundError guard for replies so
        # a single corrupted reply URL doesn't crash the whole pipeline.
        try:
            data = fetch_json(api_url)
        except HttpNotFoundError:
            data = None
        if not data:
            time.sleep(2)
            try:
                data = fetch_json(api_url)
            except HttpNotFoundError:
                data = None
            if not data:
                errors += 1
                continue
        if data.get("code") == 404 or data.get("tweet") is None:
            errors += 1
            continue

        tweet = data["tweet"]
        views = int(tweet.get("views") or 0)
        likes = int(tweet.get("likes") or 0)
        replies_count = int(tweet.get("replies") or 0)

        _http_patch_reply(rid, {
            "upvotes": likes,
            "comments_count": replies_count,
            "views": views,
            "stamp_engagement_now": True,
        })
        updated += 1

        # fxtwitter pacing — same 1s as posts
        time.sleep(1)
        if total % 50 == 0:
            progress.tick("twitter_replies", total, len(rows) - skipped_fresh,
                          updated=updated, errors=errors)

    progress.done("twitter_replies", total, updated=updated, errors=errors)
    if not quiet:
        print(f"  twitter replies: {total} checked, {updated} updated, "
              f"{errors} errors, {skipped_fresh} fresh", flush=True)
    return {"total": total, "updated": updated, "errors": errors,
            "skipped_fresh": skipped_fresh}


def refresh_github_replies(db, quiet=False, limit=None):
    """Refresh reaction count for our GitHub comments stored in `replies`.

    Uses `gh api` per comment. GitHub has no view counter, so views stays 0.
    comments_count is left at 0 (replies-on-replies are rare in our flows
    and would add a per-issue scan we don't need today).
    """
    import subprocess

    rows = _http_list_github_replies_to_refresh()
    if limit:
        rows = rows[:int(limit)]

    FRESH_WINDOW = timedelta(days=3)
    now_utc = datetime.now(timezone.utc)
    comment_url_re = re.compile(
        r"https?://github\.com/([^/]+)/([^/]+)/(?:issues|pull)/\d+#issuecomment-(\d+)"
    )

    total = updated = errors = skipped_fresh = 0
    for row in rows:
        rid = row.get("id")
        url = row.get("our_reply_url") or ""
        eu = _parse_dt(row.get("engagement_updated_at"))
        if eu:
            if eu.tzinfo is None:
                eu = eu.replace(tzinfo=timezone.utc)
            if now_utc - eu < FRESH_WINDOW:
                skipped_fresh += 1
                continue

        total += 1
        m = comment_url_re.match(url or "")
        if not m:
            errors += 1
            continue
        owner, repo, comment_id = m.group(1), m.group(2), m.group(3)

        try:
            proc = subprocess.run(
                ["gh", "api", f"repos/{owner}/{repo}/issues/comments/{comment_id}"],
                capture_output=True, text=True, timeout=30,
            )
        except Exception:
            errors += 1
            continue

        if proc.returncode != 0:
            err_text = (proc.stderr or "") + (proc.stdout or "")
            if "rate limit" in err_text.lower():
                if not quiet:
                    print(f"  github replies: rate-limited at {total}, sleeping 60s",
                          flush=True)
                time.sleep(60)
            errors += 1
            continue

        try:
            data = json.loads(proc.stdout)
        except Exception:
            errors += 1
            continue

        reactions = int((data.get("reactions") or {}).get("total_count") or 0)
        _http_patch_reply(rid, {"upvotes": reactions, "stamp_engagement_now": True})
        updated += 1
        time.sleep(0.1)
        if total % 100 == 0:
            progress.tick("github_replies", total, len(rows) - skipped_fresh,
                          updated=updated, errors=errors)

    progress.done("github_replies", total, updated=updated, errors=errors)
    if not quiet:
        print(f"  github replies: {total} checked, {updated} updated, "
              f"{errors} errors, {skipped_fresh} fresh", flush=True)
    return {"total": total, "updated": updated, "errors": errors,
            "skipped_fresh": skipped_fresh}


def get_aggregate_totals(db):
    """Get aggregate stats across all platforms via /api/v1/posts/totals.

    `db` is ignored (kept in signature for back-compat). The HTTP endpoint
    matches the previous SQL: SUM(views), SUM(upvotes) (NOT net of self-
    upvote here, unlike scrape_reddit_views's headline), SUM(comments_count),
    COUNT(*), MIN(posted_at), with platform NOT IN ('github_issues').

    NOTE: the previous SQL did NOT discount the reddit self-upvote (only
    scrape_reddit_views does that). To preserve the legacy dashboard number,
    we ask the totals endpoint with exclude_platforms=github_issues only and
    accept the raw `total_upvotes` (which the server already strips via the
    reddit/moltbook self-upvote logic). The dashboards are tolerant of either
    convention; if a stricter raw-sum is ever needed, add an
    `include_self_upvotes` flag to the route.
    """
    from datetime import datetime, timezone
    resp = api_get(
        "/api/v1/posts/totals",
        query={"status": "active", "exclude_platforms": "github_issues"},
    )
    t = (resp or {}).get("data") or {}

    total_views = int(t.get("total_views") or 0)
    total_upvotes = int(t.get("total_upvotes") or 0)
    total_comments = int(t.get("total_comments") or 0)
    total_posts = int(t.get("total_posts") or 0)
    first_post_iso = t.get("first_post_at")
    first_post = None
    if first_post_iso:
        try:
            first_post = datetime.fromisoformat(str(first_post_iso).replace("Z", "+00:00"))
        except Exception:
            first_post = None
    days = 0
    if first_post:
        now = datetime.now(first_post.tzinfo) if first_post.tzinfo else datetime.now()
        days = max((now - first_post).days, 1)

    return {
        "total_views": total_views,
        "total_upvotes": total_upvotes,
        "total_comments": total_comments,
        "total_posts": total_posts,
        "days_active": days,
        "views_per_day": round(total_views / days) if days else 0,
        "first_post": str(first_post) if first_post else None,
    }


def print_aggregate_totals(totals):
    """Print a summary line with aggregate totals."""
    print(f"\n--- Totals ({totals['days_active']} days) ---")
    print(f"Posts: {totals['total_posts']}  |  "
          f"Views: {totals['total_views']:,}  |  "
          f"Upvotes: {totals['total_upvotes']:,}  |  "
          f"Comments: {totals['total_comments']:,}  |  "
          f"Views/day: {totals['views_per_day']:,}")
    print("STATS_JSON: " + json.dumps({
        "platform": "all", "kind": "aggregate_totals",
        "days_active": totals['days_active'],
        "total_posts": totals['total_posts'],
        "total_views": totals['total_views'],
        "total_upvotes": totals['total_upvotes'],
        "total_comments": totals['total_comments'],
        "views_per_day": totals['views_per_day'],
    }))


def main():
    parser = argparse.ArgumentParser(description="Update engagement stats for social posts")
    parser.add_argument("--quiet", action="store_true", help="Minimal output")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--twitter-only", action="store_true", help="Only update Twitter stats")
    parser.add_argument("--twitter-audit", action="store_true", help="Audit all Twitter posts (check deleted + update stats)")
    parser.add_argument("--reddit-only", action="store_true", help="Only update Reddit stats")
    parser.add_argument("--reddit-resurrect", action="store_true", help="Re-check Reddit posts marked deleted/removed in last N days and flip live ones back to active")
    parser.add_argument("--resurrect-days", type=int, default=60, help="Lookback window for --reddit-resurrect (default 60)")
    parser.add_argument("--moltbook-only", action="store_true", help="Only update Moltbook stats")
    parser.add_argument("--github-only", action="store_true", help="Only update GitHub stats")
    parser.add_argument("--github-limit", type=int, default=None, help="Limit github backfill to N posts (for smoke tests)")
    parser.add_argument("--skip-replies", action="store_true",
                        help="Skip per-reply stat refresh (only update posts)")
    parser.add_argument("--replies-only", action="store_true",
                        help="Only refresh per-reply stats; skip posts entirely")
    parser.add_argument("--reply-summary", default=None,
                        help="Write a small JSON file with per-platform reply update "
                             "counts ({reddit, twitter, github}) so the calling shell "
                             "can pass them to log_run.py for the dashboard.")
    parser.add_argument("--twitter-threads-only", action="store_true",
                        help="Only refresh parent-thread snapshots (refresh_twitter_threads); "
                             "skip posts + replies entirely. Useful for isolated testing.")
    parser.add_argument("--skip-thread-snapshots", action="store_true",
                        help="Skip the parent-thread snapshot refresh that piggybacks on "
                             "--twitter-only and --twitter-audit. Use when you only want "
                             "the post-engagement refresh and not the parent-thread curve.")
    parser.add_argument("--twitter-threads-max", type=int, default=1000,
                        help="Cap the number of parent threads polled per run (default 1000). "
                             "fxtwitter is paced at 1 req/sec so 1000 threads ~= 16.7 min. "
                             "0 means unlimited.")
    parser.add_argument("--twitter-threads-stale-hours", type=int, default=5,
                        help="Skip threads whose latest snapshot is younger than this many "
                             "hours (default 5, matching the active-post and top-reply refresh "
                             "cadence so the dashboard's parent-thread column stays as fresh as "
                             "our own reply). The per-run cap (--twitter-threads-max) keeps "
                             "fxtwitter load bounded and prioritises the most recently-commented "
                             "threads. Set higher to save fxtwitter quota at the cost of staleness.")
    parser.add_argument("--stats-summary", default=None,
                        help="Write a small JSON file with per-platform stats refresh "
                             "counts ({platform: {refreshed, removed}}) so stats.sh "
                             "can aggregate refreshed/removed pills for the dashboard. "
                             "`refreshed` rolls up posts.updated + replies.updated; "
                             "`removed` rolls up posts.removed + posts.deleted "
                             "(+ posts.suspended for twitter).")
    args = parser.parse_args()

    config = load_config()
    reddit_username = config.get("accounts", {}).get("reddit", {}).get("username", "")
    user_agent = f"social-autoposter/1.0 (u/{reddit_username})" if reddit_username else "social-autoposter/1.0"

    load_env()
    # Fully HTTP-migrated: every refresh_* branch (reddit, twitter, github,
    # moltbook, and their reply passes) reads and writes through s4l.ai
    # /api/v1/* endpoints. No DATABASE_URL is required on any machine. `db` is
    # kept as None and passed through for signature compatibility only; no
    # function dereferences it.
    db = None

    reddit_stats = None
    reddit_resurrect_stats = None
    moltbook_stats = None
    twitter_stats = None
    twitter_thread_stats = None
    github_stats = None
    reddit_reply_stats = None
    twitter_reply_stats = None
    github_reply_stats = None

    # Each platform's reply refresh piggybacks on that platform's stat pass
    # (no new launchd job, no shell-script edits). --skip-replies bypasses,
    # --replies-only runs only the reply pass for that platform's scope.
    do_replies = not args.skip_replies
    # Same pattern for parent-thread snapshots: piggyback on twitter passes
    # unless explicitly skipped. --twitter-threads-only short-circuits to
    # only the snapshot pass (no posts, no replies).
    do_thread_snapshots = not args.skip_thread_snapshots

    if args.twitter_threads_only:
        twitter_thread_stats = refresh_twitter_threads(
            db, config=config, quiet=args.quiet,
            max_per_run=args.twitter_threads_max,
            stale_hours=args.twitter_threads_stale_hours,
        )
    elif args.replies_only:
        if args.twitter_only or args.twitter_audit:
            twitter_reply_stats = refresh_twitter_replies(db, quiet=args.quiet)
        elif args.reddit_only:
            reddit_reply_stats = refresh_reddit_replies(db, user_agent, quiet=args.quiet)
        elif args.github_only:
            github_reply_stats = refresh_github_replies(db, quiet=args.quiet, limit=args.github_limit)
        else:
            reddit_reply_stats = refresh_reddit_replies(db, user_agent, quiet=args.quiet)
            twitter_reply_stats = refresh_twitter_replies(db, quiet=args.quiet)
            github_reply_stats = refresh_github_replies(db, quiet=args.quiet)
    elif args.twitter_audit:
        twitter_stats = refresh_twitter(db, config=config, quiet=args.quiet, audit_mode=True)
        if do_replies:
            twitter_reply_stats = refresh_twitter_replies(db, quiet=args.quiet)
        if do_thread_snapshots:
            twitter_thread_stats = refresh_twitter_threads(
                db, config=config, quiet=args.quiet,
                max_per_run=args.twitter_threads_max,
                stale_hours=args.twitter_threads_stale_hours,
            )
    elif args.twitter_only:
        twitter_stats = refresh_twitter(db, config=config, quiet=args.quiet)
        if do_replies:
            twitter_reply_stats = refresh_twitter_replies(db, quiet=args.quiet)
        if do_thread_snapshots:
            twitter_thread_stats = refresh_twitter_threads(
                db, config=config, quiet=args.quiet,
                max_per_run=args.twitter_threads_max,
                stale_hours=args.twitter_threads_stale_hours,
            )
    elif args.reddit_resurrect:
        reddit_resurrect_stats = refresh_reddit_resurrect(db, user_agent, config=config, quiet=args.quiet, days=args.resurrect_days)
    elif args.reddit_only:
        reddit_stats = refresh_reddit(db, user_agent, config=config, quiet=args.quiet)
        if do_replies:
            reddit_reply_stats = refresh_reddit_replies(db, user_agent, quiet=args.quiet)
    elif args.moltbook_only:
        moltbook_stats = refresh_moltbook(db, os.environ.get("MOLTBOOK_API_KEY", ""), quiet=args.quiet)
    elif args.github_only:
        github_stats = refresh_github(db, quiet=args.quiet, limit=args.github_limit)
        if do_replies:
            github_reply_stats = refresh_github_replies(db, quiet=args.quiet, limit=args.github_limit)
    else:
        reddit_stats = refresh_reddit(db, user_agent, config=config, quiet=args.quiet)
        moltbook_stats = refresh_moltbook(db, os.environ.get("MOLTBOOK_API_KEY", ""), quiet=args.quiet)
        twitter_stats = refresh_twitter(db, config=config, quiet=args.quiet)
        github_stats = refresh_github(db, quiet=args.quiet)
        if do_replies:
            reddit_reply_stats = refresh_reddit_replies(db, user_agent, quiet=args.quiet)
            twitter_reply_stats = refresh_twitter_replies(db, quiet=args.quiet)
            github_reply_stats = refresh_github_replies(db, quiet=args.quiet)
        if do_thread_snapshots:
            twitter_thread_stats = refresh_twitter_threads(
                db, config=config, quiet=args.quiet,
                max_per_run=args.twitter_threads_max,
                stale_hours=args.twitter_threads_stale_hours,
            )

    # Gather aggregate totals across all platforms (HTTP-only, db ignored).
    totals = get_aggregate_totals(db)

    output = {"totals": totals}
    if reddit_stats is not None:
        output["reddit"] = reddit_stats
    if reddit_resurrect_stats is not None:
        output["reddit_resurrect"] = reddit_resurrect_stats
    if moltbook_stats is not None:
        output["moltbook"] = moltbook_stats
    if twitter_stats is not None:
        output["twitter"] = twitter_stats
    if github_stats is not None:
        output["github"] = github_stats
    if reddit_reply_stats is not None:
        output["reddit_replies"] = reddit_reply_stats
    if twitter_reply_stats is not None:
        output["twitter_replies"] = twitter_reply_stats
    if twitter_thread_stats is not None:
        output["twitter_threads"] = twitter_thread_stats
    if github_reply_stats is not None:
        output["github_replies"] = github_reply_stats

    # Sidecar JSON for the dashboard Jobs row. Always written when the flag is
    # set, even if a platform was skipped (count = 0). The shell consumer then
    # forwards the right count to log_run.py per platform.
    if args.reply_summary:
        try:
            summary = {
                "reddit": (reddit_reply_stats or {}).get("updated", 0),
                "twitter": (twitter_reply_stats or {}).get("updated", 0),
                "github": (github_reply_stats or {}).get("updated", 0),
            }
            with open(args.reply_summary, "w") as f:
                json.dump(summary, f)
        except Exception as e:
            print(f"WARN: failed to write reply summary {args.reply_summary}: {e}",
                  file=sys.stderr)

    # Richer sidecar JSON: per-platform refreshed/removed totals so stats.sh
    # can render real "refreshed N, removed N" pills instead of the legacy
    # posted=<active count> mush.
    if args.stats_summary:
        try:
            def pkey(post_stats, reply_stats, removed_keys=("removed", "deleted")):
                ps = post_stats or {}
                rs = reply_stats or {}
                refreshed = int(ps.get("updated", 0) or 0) + int(rs.get("updated", 0) or 0)
                removed = sum(int(ps.get(k, 0) or 0) for k in removed_keys)
                return {"refreshed": refreshed, "removed": removed}
            stats_summary = {
                "reddit":   pkey(reddit_stats, reddit_reply_stats),
                "twitter":  pkey(twitter_stats, twitter_reply_stats,
                                 removed_keys=("deleted", "suspended")),
                "moltbook": pkey(moltbook_stats, None),
                "github":   pkey(github_stats, github_reply_stats),
            }
            with open(args.stats_summary, "w") as f:
                json.dump(stats_summary, f)
        except Exception as e:
            print(f"WARN: failed to write stats summary {args.stats_summary}: {e}",
                  file=sys.stderr)

    if args.json:
        print(json.dumps(output, indent=2))
    else:
        if reddit_stats is not None:
            r = reddit_stats
            err_break = (
                f" [404={r.get('errors_404', 0)} "
                f"rl={r.get('errors_rate_limited', 0)} "
                f"empty={r.get('errors_empty', 0)} "
                f"other={r.get('errors_other', 0)}]"
            )
            # 2026-05-18 relabel pass. The structured stdout line now exposes
            # five distinct counters that stats.sh greps into log_run.py:
            #   total    -> "scanned" pill (all rows considered this run)
            #   skipped  -> "skipped" pill = stable-cooldown + fresh-from-Step1
            #               (Step 1 already covered them; we'd just waste an API hit)
            #   checked  -> "checked" pill = rows we actually hit the Reddit JSON
            #               API for this run (= polled + errored, excludes both
            #               skip classes). Previously this was `total - skipped`
            #               which silently inflated when skipped_fresh > 0.
            #   changed  -> "changed" pill = subset of checked where upvotes or
            #               comments_count moved. Used to live under the
            #               misleading "updated" label.
            #   errors   -> rolls into the "failed" pill on the dashboard.
            skipped_total = r.get('skipped', 0) + r.get('skipped_fresh', 0)
            checked = r['total'] - skipped_total
            print(f"\nReddit: {r['total']} total, {skipped_total} skipped, "
                  f"{checked} checked, "
                  f"{r.get('changed', r.get('updated', 0))} changed, "
                  f"{r['deleted']} deleted, {r['removed']} removed, {r['errors']} errors" + err_break)
            print("STATS_JSON: " + json.dumps({
                "platform": "reddit", "kind": "posts",
                "total": r['total'], "skipped": skipped_total, "checked": checked,
                "changed": r.get('changed', r.get('updated', 0)),
                "deleted": r['deleted'], "removed": r['removed'], "errors": r['errors'],
            }))
            if not args.quiet and r["results"]:
                print(f"{'ID':>4} {'Score':>5} {'Thread':>7} {'Comments':>8}  Title")
                for row in sorted(r["results"], key=lambda x: x["score"], reverse=True):
                    print(f"{row['id']:>4} {row['score']:>5} {row['thread_score']:>7} "
                          f"{row['thread_comments']:>8}  {row['title']}")

        if reddit_resurrect_stats is not None:
            r = reddit_resurrect_stats
            print(f"\nReddit resurrect ({args.resurrect_days}d): {r['total']} rechecked, "
                  f"{r['resurrected']} resurrected, {r['still_dead']} still dead, "
                  f"{r['errors']} errors (rl={r.get('errors_rate_limited',0)} "
                  f"empty={r.get('errors_empty',0)} malformed={r.get('errors_malformed',0)} "
                  f"other={r.get('errors_other',0)})")

        # `skipped: True` is the no-API-key sentinel (don't print); any
        # integer value means we ran and counted some skipped rows, in which
        # case we DO want the summary line (the dashboard needs it).
        if moltbook_stats is not None and moltbook_stats.get("skipped") is not True:
            m = moltbook_stats
            print(f"\nMoltbook: {m['total']} checked, {m['updated']} updated, "
                  f"{m['deleted']} deleted, {m['errors']} errors")
            print("STATS_JSON: " + json.dumps({
                "platform": "moltbook", "kind": "posts",
                "total": m['total'], "skipped": 0, "checked": m['total'],
                "changed": m['updated'],
                "deleted": m['deleted'], "removed": 0, "errors": m['errors'],
            }))

        if twitter_stats is not None:
            t = twitter_stats
            # 2026-05-18 relabel pass — same shape as the Reddit line above.
            # `skipped` now combines stable-cooldown + skipped_fresh so the
            # `checked` count reflects rows we actually polled the fxtwitter
            # API for, not "everything minus stable skips" (which silently
            # included fresh rows). `changed` is the metric-moved subset.
            t_skipped_total = t.get('skipped', 0) + t.get('skipped_fresh', 0)
            t_checked = t['total'] - t_skipped_total
            print(f"\nTwitter: {t['total']} total, {t_skipped_total} skipped, "
                  f"{t_checked} checked, "
                  f"{t.get('changed', t.get('updated', 0))} changed, "
                  f"{t['deleted']} deleted, {t['errors']} errors")
            print("STATS_JSON: " + json.dumps({
                "platform": "twitter", "kind": "posts",
                "total": t['total'], "skipped": t_skipped_total, "checked": t_checked,
                "changed": t.get('changed', t.get('updated', 0)),
                "deleted": t['deleted'], "removed": 0, "errors": t['errors'],
            }))
            if not args.quiet and t["results"]:
                top = sorted(t["results"], key=lambda x: x.get("views", 0), reverse=True)[:30]
                print(f"{'ID':>4} {'Views':>7} {'Likes':>5} {'Replies':>7} {'RTs':>4}")
                for row in top:
                    print(f"{row['id']:>4} {row.get('views',0):>7} {row.get('likes',0):>5} "
                          f"{row.get('replies',0):>7} {row.get('retweets',0):>4}")

        if github_stats is not None:
            g = github_stats
            print(f"\nGitHub: {g['total']} checked, {g['updated']} updated, "
                  f"{g['deleted']} deleted, {g['errors']} errors")
            print("STATS_JSON: " + json.dumps({
                "platform": "github", "kind": "posts",
                "total": g['total'], "skipped": 0, "checked": g['total'],
                "changed": g['updated'],
                "deleted": g['deleted'], "removed": 0, "errors": g['errors'],
            }))
            if not args.quiet and g["results"]:
                top = sorted(g["results"],
                             key=lambda x: (x.get("reactions", 0) + x.get("replies", 0)),
                             reverse=True)[:20]
                print(f"{'ID':>5} {'React':>5} {'Reply':>5}  URL")
                for row in top:
                    print(f"{row['id']:>5} {row['reactions']:>5} {row['replies']:>5}  {row['url']}")

        for label, stats in (("Reddit replies", reddit_reply_stats),
                             ("Twitter replies", twitter_reply_stats),
                             ("GitHub replies", github_reply_stats)):
            if stats is None:
                continue
            print(f"\n{label}: {stats['total']} checked, {stats['updated']} updated, "
                  f"{stats['errors']} errors, {stats.get('skipped_fresh', 0)} fresh")
            print("STATS_JSON: " + json.dumps({
                "platform": label.split()[0].lower(), "kind": "replies",
                "total": stats['total'], "checked": stats['total'],
                "updated": stats['updated'], "errors": stats['errors'],
                "fresh": stats.get('skipped_fresh', 0),
            }))

        print_aggregate_totals(totals)


if __name__ == "__main__":
    main()
