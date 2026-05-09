#!/usr/bin/env python3
"""Fetch engagement stats for Reddit + Moltbook posts via public APIs.

Updates upvotes, comments_count, and status in the DB. No browser needed.
Reddit profile scrape (Step 1 of stats.sh) covers most stats; this script
acts as deletion/removal detection and as a fallback for rows the scrape
couldn't match.

Usage:
    python3 scripts/update_stats.py [--db PATH] [--quiet]
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
import db as dbmod
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
    """Raised when a fetch returns HTTP 404."""
    pass


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
            raise HttpNotFoundError(url)
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


def update_reddit(db, user_agent, config=None, quiet=False):
    config = config or {}
    posts = db.execute(
        "SELECT id, our_url, thread_url, upvotes, comments_count, "
        "COALESCE(scan_no_change_count, 0) as scan_no_change_count, posted_at, "
        "engagement_updated_at "
        "FROM posts "
        "WHERE platform='reddit' AND status='active' AND our_url IS NOT NULL ORDER BY id"
    ).fetchall()

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
            db.commit()
            progress.tick("reddit", total, len(posts),
                          updated=updated, changed=changed, errors=errors,
                          errors_404=errors_404,
                          errors_rate_limited=errors_rate_limited,
                          errors_empty=errors_empty,
                          errors_other=errors_other)
            if not quiet:
                rem = _reddit_rate_state.get("remaining")
                rem_str = f", rem={int(rem)}" if rem is not None else ""
                print(f"  Committed batch ({total}/{len(posts)} iterated, {updated} polled, {changed} changed, {errors} errors [404={errors_404} rl={errors_rate_limited} empty={errors_empty} other={errors_other}]{rem_str})", flush=True)
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
                # Require 2 consecutive deletion detections to avoid false positives
                # from Reddit API rate limiting / transient errors
                row = db.execute(
                    "SELECT COALESCE(deletion_detect_count, 0) FROM posts WHERE id=%s", [post_id]
                ).fetchone()
                detect_count = (row[0] if row else 0) + 1
                if detect_count >= 2:
                    db.execute("UPDATE posts SET status='deleted', deletion_detect_count=%s, status_checked_at=NOW() WHERE id=%s",
                               [detect_count, post_id])
                    deleted += 1
                    if not quiet:
                        print(f"DELETED [{post_id}] (confirmed after {detect_count} detections)")
                else:
                    db.execute("UPDATE posts SET deletion_detect_count=%s, status_checked_at=NOW() WHERE id=%s",
                               [detect_count, post_id])
                    if not quiet:
                        print(f"DELETION PENDING [{post_id}] (detection {detect_count}/2)")
                continue

            if body == "[removed]":
                row = db.execute(
                    "SELECT COALESCE(deletion_detect_count, 0) FROM posts WHERE id=%s", [post_id]
                ).fetchone()
                detect_count = (row[0] if row else 0) + 1
                if detect_count >= 2:
                    db.execute("UPDATE posts SET status='removed', deletion_detect_count=%s, status_checked_at=NOW() WHERE id=%s",
                               [detect_count, post_id])
                    removed += 1
                    if not quiet:
                        print(f"REMOVED [{post_id}] (confirmed after {detect_count} detections)")
                else:
                    db.execute("UPDATE posts SET deletion_detect_count=%s, status_checked_at=NOW() WHERE id=%s",
                               [detect_count, post_id])
                    if not quiet:
                        print(f"REMOVAL PENDING [{post_id}] (detection {detect_count}/2)")
                continue

            db.execute(
                "UPDATE posts SET upvotes=%s, comments_count=%s, "
                "engagement_updated_at=NOW(), status_checked_at=NOW(), deletion_detect_count=0 WHERE id=%s",
                [score, comment_reply_count, post_id],
            )
            updated += 1
            if score != prev_upvotes or comment_reply_count != prev_comments:
                changed += 1
            results.append({"id": post_id, "score": score, "comment_replies": comment_reply_count,
                            "thread_score": thread_score, "thread_comments": thread_comments,
                            "title": thread_title})
        else:
            # our_url is a thread URL without a comment ID
            # Check if it's our original post (we are the thread author)
            is_our_post = thread_author.lower() == config.get("accounts", {}).get("reddit", {}).get("username", "").lower()

            if is_our_post:
                # Original post — use thread-level stats (they ARE our stats)
                if thread_data.get("removed_by_category"):
                    row = db.execute(
                        "SELECT COALESCE(deletion_detect_count, 0) FROM posts WHERE id=%s", [post_id]
                    ).fetchone()
                    detect_count = (row[0] if row else 0) + 1
                    if detect_count >= 2:
                        db.execute("UPDATE posts SET status='removed', deletion_detect_count=%s, status_checked_at=NOW() WHERE id=%s",
                                   [detect_count, post_id])
                        removed += 1
                        if not quiet:
                            print(f"REMOVED (thread) [{post_id}] (confirmed after {detect_count} detections)")
                    else:
                        db.execute("UPDATE posts SET deletion_detect_count=%s, status_checked_at=NOW() WHERE id=%s",
                                   [detect_count, post_id])
                        if not quiet:
                            print(f"REMOVAL PENDING (thread) [{post_id}] (detection {detect_count}/2)")
                    continue

                db.execute(
                    "UPDATE posts SET upvotes=%s, comments_count=%s, "
                    "engagement_updated_at=NOW(), status_checked_at=NOW(), deletion_detect_count=0 WHERE id=%s",
                    [thread_score, thread_comments, post_id],
                )
                updated += 1
                if thread_score != prev_upvotes or thread_comments != prev_comments:
                    changed += 1
                results.append({"id": post_id, "score": thread_score, "thread_score": thread_score,
                                "thread_comments": thread_comments, "title": thread_title})
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
                            db.execute(
                                "UPDATE posts SET upvotes=%s, "
                                "engagement_updated_at=NOW(), status_checked_at=NOW(), deletion_detect_count=0 WHERE id=%s",
                                [score, post_id],
                            )
                            updated += 1
                            # No comments_count write in this branch (no-permalink
                            # comments lack per-comment reply visibility), so the
                            # change check is score-only.
                            if score != prev_upvotes:
                                changed += 1
                            results.append({"id": post_id, "score": score, "thread_score": thread_score,
                                            "thread_comments": thread_comments, "title": thread_title})
                        break

                if our_removed:
                    row = db.execute(
                        "SELECT COALESCE(deletion_detect_count, 0) FROM posts WHERE id=%s", [post_id]
                    ).fetchone()
                    detect_count = (row[0] if row else 0) + 1
                    if detect_count >= 2:
                        db.execute("UPDATE posts SET status='removed', deletion_detect_count=%s, status_checked_at=NOW() WHERE id=%s",
                                   [detect_count, post_id])
                        removed += 1
                        if not quiet:
                            print(f"REMOVED (no permalink) [{post_id}] (confirmed after {detect_count} detections)")
                    else:
                        db.execute("UPDATE posts SET deletion_detect_count=%s, status_checked_at=NOW() WHERE id=%s",
                                   [detect_count, post_id])
                        if not quiet:
                            print(f"REMOVAL PENDING (no permalink) [{post_id}] (detection {detect_count}/2)")
                elif not our_found:
                    # Comment not in top-level replies — just update checked timestamp
                    db.execute(
                        "UPDATE posts SET status_checked_at=NOW() WHERE id=%s",
                        [post_id],
                    )
                    if not quiet:
                        print(f"SKIP (no permalink, comment not in top-level) [{post_id}]")

        # Track whether stats changed for skip optimization
        # Compare current score to previous — if same, increment no-change counter
        if results and results[-1]["id"] == post_id:
            new_score = results[-1]["score"]
            if new_score == prev_upvotes:
                db.execute("UPDATE posts SET scan_no_change_count = COALESCE(scan_no_change_count, 0) + 1 WHERE id = %s", [post_id])
            else:
                db.execute("UPDATE posts SET scan_no_change_count = 0 WHERE id = %s", [post_id])

        # Pacing now happens at top of loop (before API call) via _reddit_pacing_sleep().

    db.commit()
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


def update_reddit_resurrect(db, user_agent, config=None, quiet=False, days=60):
    """Re-check Reddit posts marked 'deleted'/'removed' in the last N days.

    If the post/comment is now visible with real content, flip status back to 'active'.
    One live detection is enough (bias: don't falsely mark deleted).
    """
    config = config or {}
    our_username = config.get("accounts", {}).get("reddit", {}).get("username", "")

    posts = db.execute(
        "SELECT id, our_url, thread_url, status "
        "FROM posts "
        "WHERE platform='reddit' AND status IN ('deleted','removed') "
        "AND posted_at > NOW() - INTERVAL '%s days' "
        "AND our_url IS NOT NULL ORDER BY id" % int(days)
    ).fetchall()

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
            db.execute("UPDATE posts SET status_checked_at=NOW() WHERE id=%s", [post_id])
            db.commit()
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
            db.execute(
                "UPDATE posts SET status='active', deletion_detect_count=0, status_checked_at=NOW(), resurrected_at=NOW() WHERE id=%s",
                [post_id],
            )
            resurrected += 1
            if not quiet:
                print(f"RESURRECTED [{post_id}] ({prev_status} -> active): {our_url}", flush=True)
        else:
            still_dead += 1
            db.execute("UPDATE posts SET status_checked_at=NOW() WHERE id=%s", [post_id])

        # Commit per row so updates survive mid-run connection drops (Neon idle timeout).
        db.commit()

        # Pacing now happens at top of loop (before API call) via _reddit_pacing_sleep().

    db.commit()
    return {"total": total, "resurrected": resurrected, "still_dead": still_dead, "errors": errors,
            "errors_404": errors_404, "errors_rate_limited": errors_rate_limited,
            "errors_empty": errors_empty, "errors_malformed": errors_malformed,
            "errors_other": errors_other}


def update_moltbook(db, api_key, quiet=False):
    if not api_key:
        return {"skipped": True, "reason": "no_api_key"}

    posts = db.execute(
        "SELECT id, our_url, thread_url, upvotes, comments_count, "
        "COALESCE(scan_no_change_count, 0) AS scan_no_change_count, posted_at "
        "FROM posts WHERE platform='moltbook' AND status='active' AND our_url IS NOT NULL "
        "ORDER BY engagement_updated_at ASC NULLS FIRST, id DESC"
    ).fetchall()

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
        post_id, our_url, thread_url = post[0], post[1], post[2]
        prev_upvotes, prev_comments = post[3], post[4]
        no_change = post[5]
        posted_at = post[6]

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
                db.execute(
                    "UPDATE posts SET status_checked_at=NOW() WHERE id=%s",
                    [post_id],
                )
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
                row = db.execute(
                    "SELECT COALESCE(deletion_detect_count, 0) FROM posts WHERE id=%s", [post_id]
                ).fetchone()
                detect_count = (row[0] if row else 0) + 1
                if detect_count >= 2:
                    db.execute("UPDATE posts SET status='deleted', deletion_detect_count=%s, status_checked_at=NOW() WHERE id=%s",
                               [detect_count, post_id])
                    deleted += 1
                    if not quiet:
                        print(f"DELETED (Moltbook 404) [{post_id}] (confirmed after {detect_count} detections)")
                else:
                    db.execute("UPDATE posts SET deletion_detect_count=%s, status_checked_at=NOW() WHERE id=%s",
                               [detect_count, post_id])
                    if not quiet:
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
                    db.execute(
                        "UPDATE posts SET status_checked_at=NOW(), deletion_detect_count=0 WHERE id=%s",
                        [post_id],
                    )
                else:
                    # Post has few comments but ours is missing — likely deleted
                    row = db.execute(
                        "SELECT COALESCE(deletion_detect_count, 0) FROM posts WHERE id=%s", [post_id]
                    ).fetchone()
                    detect_count = (row[0] if row else 0) + 1
                    if detect_count >= 2:
                        db.execute(
                            "UPDATE posts SET status='deleted', deletion_detect_count=%s, status_checked_at=NOW() WHERE id=%s",
                            [detect_count, post_id],
                        )
                        deleted += 1
                        if not quiet:
                            print(f"DELETED (Moltbook comment missing) [{post_id}] (confirmed after {detect_count} detections)")
                    else:
                        db.execute(
                            "UPDATE posts SET deletion_detect_count=%s, status_checked_at=NOW() WHERE id=%s",
                            [detect_count, post_id],
                        )
                        if not quiet:
                            print(f"DELETION PENDING (Moltbook comment missing) [{post_id}] (detection {detect_count}/2)")
                continue

            if our_comment.get("is_deleted"):
                row = db.execute(
                    "SELECT COALESCE(deletion_detect_count, 0) FROM posts WHERE id=%s", [post_id]
                ).fetchone()
                detect_count = (row[0] if row else 0) + 1
                if detect_count >= 2:
                    db.execute("UPDATE posts SET status='deleted', deletion_detect_count=%s, status_checked_at=NOW() WHERE id=%s",
                               [detect_count, post_id])
                    deleted += 1
                else:
                    db.execute("UPDATE posts SET deletion_detect_count=%s, status_checked_at=NOW() WHERE id=%s",
                               [detect_count, post_id])
                continue

            # Comment-specific engagement
            comment_upvotes = our_comment.get("upvotes", 0)
            comment_score = our_comment.get("score", 0)
            # Server's `reply_count` is stale/zero on many comments; len(replies) is authoritative.
            replies_list = our_comment.get("replies") or []
            comment_replies = max(our_comment.get("reply_count") or 0, len(replies_list))
            verification = our_comment.get("verification_status", "unknown")
            thread_comment_count = data.get("count", 0)

            db.execute(
                "UPDATE posts SET upvotes=%s, comments_count=%s, "
                "engagement_updated_at=NOW(), status_checked_at=NOW(), deletion_detect_count=0 WHERE id=%s",
                [comment_upvotes, comment_replies, post_id],
            )
            updated += 1
            if comment_upvotes == prev_upvotes and comment_replies == prev_comments:
                db.execute("UPDATE posts SET scan_no_change_count = COALESCE(scan_no_change_count, 0) + 1 WHERE id=%s", [post_id])
            else:
                db.execute("UPDATE posts SET scan_no_change_count = 0 WHERE id=%s", [post_id])
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
                row = db.execute(
                    "SELECT COALESCE(deletion_detect_count, 0) FROM posts WHERE id=%s", [post_id]
                ).fetchone()
                detect_count = (row[0] if row else 0) + 1
                if detect_count >= 2:
                    db.execute("UPDATE posts SET status='deleted', deletion_detect_count=%s, status_checked_at=NOW() WHERE id=%s",
                               [detect_count, post_id])
                    deleted += 1
                    if not quiet:
                        print(f"DELETED (Moltbook 404) [{post_id}] (confirmed after {detect_count} detections)")
                else:
                    db.execute("UPDATE posts SET deletion_detect_count=%s, status_checked_at=NOW() WHERE id=%s",
                               [detect_count, post_id])
                    if not quiet:
                        print(f"DELETION PENDING (Moltbook 404) [{post_id}] (detection {detect_count}/2)")
                continue
            if not data or not data.get("success"):
                errors += 1
                continue

            post_data = data.get("post", {})
            if post_data.get("is_deleted"):
                row = db.execute(
                    "SELECT COALESCE(deletion_detect_count, 0) FROM posts WHERE id=%s", [post_id]
                ).fetchone()
                detect_count = (row[0] if row else 0) + 1
                if detect_count >= 2:
                    db.execute("UPDATE posts SET status='deleted', deletion_detect_count=%s, status_checked_at=NOW() WHERE id=%s",
                               [detect_count, post_id])
                    deleted += 1
                else:
                    db.execute("UPDATE posts SET deletion_detect_count=%s, status_checked_at=NOW() WHERE id=%s",
                               [detect_count, post_id])
                continue

            upvotes = post_data.get("upvotes", 0)
            comment_count = post_data.get("comment_count", post_data.get("comments_count", 0))
            score = post_data.get("score", 0)
            views = post_data.get("views", 0)

            db.execute(
                "UPDATE posts SET upvotes=%s, comments_count=%s, views=%s, "
                "engagement_updated_at=NOW(), status_checked_at=NOW(), deletion_detect_count=0 WHERE id=%s",
                [upvotes, comment_count, views, post_id],
            )
            updated += 1
            if upvotes == prev_upvotes and comment_count == prev_comments:
                db.execute("UPDATE posts SET scan_no_change_count = COALESCE(scan_no_change_count, 0) + 1 WHERE id=%s", [post_id])
            else:
                db.execute("UPDATE posts SET scan_no_change_count = 0 WHERE id=%s", [post_id])
            results.append({"id": post_id, "upvotes": upvotes, "score": score,
                            "comments": comment_count})

    db.commit()
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
        m = comment_re.match((post[1] or ""))
        if not m:
            continue
        owner, repo, number, cid = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
        threads[(owner, repo, number)].append((post[0], cid))

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
                    db.execute(
                        "UPDATE posts SET status='deleted', "
                        "deletion_detect_count=GREATEST(COALESCE(deletion_detect_count,0), 2), "
                        "source_summary=COALESCE(NULLIF(source_summary,''), '') "
                        "  || CASE WHEN COALESCE(source_summary,'')='' THEN '' ELSE ' ' END "
                        "  || %s, "
                        "status_checked_at=NOW() WHERE id=%s",
                        [f"[minimized:{reason}]", post_id],
                    )
                    minimized += 1
                    if not quiet:
                        owner, repo, number = key
                        print(f"MINIMIZED [{post_id}] {owner}/{repo}#{number} reason={reason}",
                              flush=True)
        db.commit()

    if not quiet:
        rl_note = f", failures={failures}" if failures else ""
        print(f"  github-minimize: flipped {minimized} hidden comments "
              f"across {len(threads)} threads{rl_note}", flush=True)
    return minimized


def update_github(db, quiet=False, limit=None):
    """Fetch engagement on our GitHub issue/PR comments via `gh api`.

    Stores reactions.total_count in posts.upvotes and the count of replies
    detected by scan_github_replies.py in posts.comments_count.

    Runs a GraphQL `isMinimized` pre-pass before the REST loop so hidden
    comments are flipped to status='deleted' and skipped by the REST select.
    """
    import subprocess

    sql = ("SELECT id, our_url FROM posts WHERE platform='github' "
           "AND status='active' AND our_url IS NOT NULL ORDER BY id")
    if limit:
        sql += f" LIMIT {int(limit)}"
    posts = db.execute(sql).fetchall()

    # Pre-pass: flag minimized (hidden) comments before REST. Wrapped
    # defensively, a GraphQL flake must not block the REST hot path.
    try:
        _detect_minimized_github_comments(db, posts, quiet=quiet)
    except Exception as e:
        if not quiet:
            print(f"  github-minimize: pre-pass crashed, skipping: {e}", flush=True)
    # Re-select after the pre-pass so flipped rows drop out of the REST loop.
    posts = db.execute(sql).fetchall()

    total = updated = deleted = errors = 0
    results = []
    comment_url_re = re.compile(
        r"https?://github\.com/([^/]+)/([^/]+)/(?:issues|pull)/\d+#issuecomment-(\d+)"
    )

    for post in posts:
        total += 1
        post_id, our_url = post[0], post[1]

        m = comment_url_re.match(our_url or "")
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
                    print(f"  github: rate-limited at {total}/{len(posts)}, sleeping 60s", flush=True)
                time.sleep(60)
                errors += 1
                continue
            if "Not Found" in err_text or "HTTP 404" in err_text:
                row = db.execute(
                    "SELECT COALESCE(deletion_detect_count, 0) FROM posts WHERE id=%s",
                    [post_id],
                ).fetchone()
                detect_count = (row[0] if row else 0) + 1
                if detect_count >= 2:
                    db.execute(
                        "UPDATE posts SET status='deleted', deletion_detect_count=%s, "
                        "status_checked_at=NOW() WHERE id=%s",
                        [detect_count, post_id],
                    )
                    deleted += 1
                    if not quiet:
                        print(f"DELETED (github 404) [{post_id}]", flush=True)
                else:
                    db.execute(
                        "UPDATE posts SET deletion_detect_count=%s, status_checked_at=NOW() "
                        "WHERE id=%s",
                        [detect_count, post_id],
                    )
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

        row = db.execute(
            "SELECT COUNT(*) FROM replies WHERE post_id=%s AND platform='github'",
            [post_id],
        ).fetchone()
        reply_count = int(row[0] or 0)

        db.execute(
            "UPDATE posts SET upvotes=%s, comments_count=%s, "
            "engagement_updated_at=NOW(), status_checked_at=NOW(), "
            "deletion_detect_count=0 WHERE id=%s",
            [total_reactions, reply_count, post_id],
        )
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
            db.commit()
            progress.tick("github", total, len(posts),
                          updated=updated, deleted=deleted, errors=errors)
            if not quiet:
                print(f"  github: {total}/{len(posts)} processed "
                      f"(updated={updated}, deleted={deleted}, errors={errors})",
                      flush=True)

    db.commit()
    progress.done("github", len(posts),
                  updated=updated, deleted=deleted, errors=errors)
    return {"total": total, "updated": updated, "deleted": deleted,
            "errors": errors, "results": results}


def update_twitter(db, config=None, quiet=False, audit_mode=False):
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

    Before this split, audit refreshed every active row daily and stamped
    engagement_updated_at on all of them, which silently locked the per-6h
    job out of the hot tier for a week at a time.
    """
    config = config or {}

    if audit_mode:
        posts = db.execute(
            "SELECT id, our_url, "
            "COALESCE(scan_no_change_count, 0) as scan_no_change_count, posted_at, "
            "upvotes, views, comments_count "
            "FROM posts "
            "WHERE platform='twitter' AND status='active' AND our_url IS NOT NULL "
            "AND posted_at <= NOW() - INTERVAL '7 days' "
            "ORDER BY id"
        ).fetchall()
    else:
        posts = db.execute(
            "SELECT id, our_url, "
            "COALESCE(scan_no_change_count, 0) as scan_no_change_count, posted_at, "
            "upvotes, views, comments_count "
            "FROM posts "
            "WHERE platform='twitter' AND status='active' AND our_url IS NOT NULL "
            "AND posted_at > NOW() - INTERVAL '7 days' "
            "AND (engagement_updated_at IS NULL OR engagement_updated_at < NOW() - INTERVAL '5 hours') "
            "ORDER BY id"
        ).fetchall()

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
        post_id, our_url = post[0], post[1]
        no_change = post[2]
        posted_at = post[3]
        prev_upvotes = post[4]
        prev_views = post[5]
        prev_comments = post[6]

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
        except HttpNotFoundError:
            data = {"code": 404, "tweet": None}

        if not data:
            # Retry once
            time.sleep(2)
            try:
                data = fetch_json(url)
            except HttpNotFoundError:
                data = {"code": 404, "tweet": None}
            if not data:
                errors += 1
                continue

        code = data.get("code", 0)
        tweet = data.get("tweet")

        if code == 404 or tweet is None:
            # Tweet not found, could be deleted or suspended. Run the 2-strike
            # confirmation in both modes so hot-tier deletions surface within
            # hours; the cold-tier audit still runs the same pipeline daily.
            row = db.execute(
                "SELECT COALESCE(deletion_detect_count, 0) FROM posts WHERE id=%s", [post_id]
            ).fetchone()
            detect_count = (row[0] if row else 0) + 1
            if detect_count >= 2:
                db.execute(
                    "UPDATE posts SET status='deleted', deletion_detect_count=%s, "
                    "status_checked_at=NOW() WHERE id=%s",
                    [detect_count, post_id]
                )
                deleted += 1
                if not quiet:
                    print(f"DELETED [{post_id}] (confirmed after {detect_count} detections)")
            else:
                db.execute(
                    "UPDATE posts SET deletion_detect_count=%s, status_checked_at=NOW() WHERE id=%s",
                    [detect_count, post_id]
                )
                if not quiet:
                    print(f"DELETION PENDING [{post_id}] (detection {detect_count}/2)")
            continue

        # Extract stats
        views = tweet.get("views") or 0
        likes = tweet.get("likes") or 0
        replies = tweet.get("replies") or 0
        retweets = tweet.get("retweets") or 0
        bookmarks = tweet.get("bookmarks") or 0

        db.execute(
            "UPDATE posts SET views=%s, upvotes=%s, comments_count=%s, "
            "engagement_updated_at=NOW(), status_checked_at=NOW(), "
            "deletion_detect_count=0 WHERE id=%s",
            [views, likes, replies, post_id],
        )
        dbmod.snapshot_post_views(db, post_id, views)

        updated += 1
        results.append({"id": post_id, "views": views, "likes": likes,
                        "replies": replies, "retweets": retweets})

        # Track no-change for skip optimization. The "actually moved" branch
        # also bumps `changed` so the printed summary can distinguish a
        # successful poll (updated) from one where any tracked stat shifted.
        # All three signals are watched (views, likes, replies) so a tweet
        # that stays flat on views/likes but is still picking up replies is
        # NOT mistakenly classified as "stable" and isn't skip-optimized.
        if (likes == prev_upvotes
                and views == prev_views
                and replies == prev_comments):
            db.execute("UPDATE posts SET scan_no_change_count = COALESCE(scan_no_change_count, 0) + 1 WHERE id = %s", [post_id])
        else:
            changed += 1
            db.execute("UPDATE posts SET scan_no_change_count = 0 WHERE id = %s", [post_id])

        # Rate limit: 1 request per second to be safe with fxtwitter
        time.sleep(1)

        # Commit every 50 tweets to save progress
        if total % 50 == 0:
            db.commit()
            progress.tick("twitter", total, len(posts),
                          updated=updated, changed=changed, deleted=deleted,
                          suspended=suspended, errors=errors, skipped=skipped)

    db.commit()
    progress.done("twitter", len(posts),
                  updated=updated, changed=changed, deleted=deleted,
                  suspended=suspended, errors=errors, skipped=skipped)
    if skipped and not quiet:
        print(f"  Skipped {skipped} stable tweets (3+ scans unchanged, older than 5 days)")
    return {"total": total, "updated": updated, "changed": changed,
            "deleted": deleted, "suspended": suspended,
            "errors": errors, "skipped": skipped, "results": results}


def update_reddit_replies(db, user_agent, quiet=False):
    """Refresh score + reply count for our Reddit comments stored in `replies`.

    Uses batch_fetch_info (up to 100 t1_ IDs per API call) so the whole table
    typically scans in 1-3 hits. Reddit doesn't expose per-comment views, so
    `views` stays 0. Skips rows refreshed within FRESH_WINDOW.
    """
    from reddit_tools import batch_fetch_info, RateLimitedError

    FRESH_WINDOW = timedelta(hours=4)
    now_utc = datetime.now(timezone.utc)

    rows = db.execute(
        "SELECT id, our_reply_id, engagement_updated_at FROM replies "
        "WHERE platform='reddit' AND status='replied' AND our_reply_id IS NOT NULL "
        "ORDER BY id"
    ).fetchall()

    pending = []
    skipped_fresh = 0
    for row in rows:
        rid, our_reply_id, eu = row[0], row[1], row[2]
        if eu:
            if eu.tzinfo is None:
                eu = eu.replace(tzinfo=timezone.utc)
            if now_utc - eu < FRESH_WINDOW:
                skipped_fresh += 1
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
        db.execute(
            "UPDATE replies SET upvotes=%s, comments_count=%s, "
            "engagement_updated_at=NOW() WHERE id=%s",
            [score, reply_count, rid],
        )
        updated += 1

    db.commit()
    progress.done("reddit_replies", total, updated=updated, errors=errors)
    if not quiet:
        print(f"  reddit replies: {total} checked, {updated} updated, "
              f"{errors} errors, {skipped_fresh} fresh", flush=True)
    return {"total": total, "updated": updated, "errors": errors,
            "skipped_fresh": skipped_fresh}


def update_twitter_replies(db, quiet=False):
    """Refresh per-reply stats (likes, replies count, views) for our reply
    tweets stored in `replies`. Reuses the fxtwitter API per reply tweet ID.
    """
    FRESH_WINDOW = timedelta(days=7)
    now_utc = datetime.now(timezone.utc)

    rows = db.execute(
        "SELECT id, our_reply_url, engagement_updated_at FROM replies "
        "WHERE platform='twitter' AND status='replied' AND our_reply_url IS NOT NULL "
        "ORDER BY id"
    ).fetchall()

    total = updated = errors = skipped_fresh = 0
    for row in rows:
        rid, url, eu = row[0], row[1], row[2]
        if eu:
            if eu.tzinfo is None:
                eu = eu.replace(tzinfo=timezone.utc)
            if now_utc - eu < FRESH_WINDOW:
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
        # See update_twitter() — same HttpNotFoundError guard for replies so
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

        db.execute(
            "UPDATE replies SET upvotes=%s, comments_count=%s, views=%s, "
            "engagement_updated_at=NOW() WHERE id=%s",
            [likes, replies_count, views, rid],
        )
        updated += 1

        # fxtwitter pacing — same 1s as posts
        time.sleep(1)
        if total % 50 == 0:
            db.commit()
            progress.tick("twitter_replies", total, len(rows) - skipped_fresh,
                          updated=updated, errors=errors)

    db.commit()
    progress.done("twitter_replies", total, updated=updated, errors=errors)
    if not quiet:
        print(f"  twitter replies: {total} checked, {updated} updated, "
              f"{errors} errors, {skipped_fresh} fresh", flush=True)
    return {"total": total, "updated": updated, "errors": errors,
            "skipped_fresh": skipped_fresh}


def update_github_replies(db, quiet=False, limit=None):
    """Refresh reaction count for our GitHub comments stored in `replies`.

    Uses `gh api` per comment. GitHub has no view counter, so views stays 0.
    comments_count is left at 0 (replies-on-replies are rare in our flows
    and would add a per-issue scan we don't need today).
    """
    import subprocess

    sql = ("SELECT id, our_reply_url, engagement_updated_at FROM replies "
           "WHERE platform='github' AND status='replied' AND our_reply_url IS NOT NULL "
           "ORDER BY id")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = db.execute(sql).fetchall()

    FRESH_WINDOW = timedelta(days=3)
    now_utc = datetime.now(timezone.utc)
    comment_url_re = re.compile(
        r"https?://github\.com/([^/]+)/([^/]+)/(?:issues|pull)/\d+#issuecomment-(\d+)"
    )

    total = updated = errors = skipped_fresh = 0
    for row in rows:
        rid, url, eu = row[0], row[1], row[2]
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
        db.execute(
            "UPDATE replies SET upvotes=%s, engagement_updated_at=NOW() WHERE id=%s",
            [reactions, rid],
        )
        updated += 1
        time.sleep(0.1)
        if total % 100 == 0:
            db.commit()
            progress.tick("github_replies", total, len(rows) - skipped_fresh,
                          updated=updated, errors=errors)

    db.commit()
    progress.done("github_replies", total, updated=updated, errors=errors)
    if not quiet:
        print(f"  github replies: {total} checked, {updated} updated, "
              f"{errors} errors, {skipped_fresh} fresh", flush=True)
    return {"total": total, "updated": updated, "errors": errors,
            "skipped_fresh": skipped_fresh}


def get_aggregate_totals(db):
    """Get aggregate stats across all platforms."""
    from datetime import datetime, timezone

    row = db.execute(
        "SELECT SUM(views), SUM(upvotes), SUM(comments_count), COUNT(*), MIN(posted_at) "
        "FROM posts WHERE status='active' AND platform NOT IN ('github_issues')"
    ).fetchone()

    total_views = row[0] or 0
    total_upvotes = row[1] or 0
    total_comments = row[2] or 0
    total_posts = row[3] or 0
    first_post = row[4]

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

    dbmod.load_env()
    db = dbmod.get_conn()

    reddit_stats = None
    reddit_resurrect_stats = None
    moltbook_stats = None
    twitter_stats = None
    github_stats = None
    reddit_reply_stats = None
    twitter_reply_stats = None
    github_reply_stats = None

    # Each platform's reply refresh piggybacks on that platform's stat pass
    # (no new launchd job, no shell-script edits). --skip-replies bypasses,
    # --replies-only runs only the reply pass for that platform's scope.
    do_replies = not args.skip_replies

    if args.replies_only:
        if args.twitter_only or args.twitter_audit:
            twitter_reply_stats = update_twitter_replies(db, quiet=args.quiet)
        elif args.reddit_only:
            reddit_reply_stats = update_reddit_replies(db, user_agent, quiet=args.quiet)
        elif args.github_only:
            github_reply_stats = update_github_replies(db, quiet=args.quiet, limit=args.github_limit)
        else:
            reddit_reply_stats = update_reddit_replies(db, user_agent, quiet=args.quiet)
            twitter_reply_stats = update_twitter_replies(db, quiet=args.quiet)
            github_reply_stats = update_github_replies(db, quiet=args.quiet)
    elif args.twitter_audit:
        twitter_stats = update_twitter(db, config=config, quiet=args.quiet, audit_mode=True)
        if do_replies:
            twitter_reply_stats = update_twitter_replies(db, quiet=args.quiet)
    elif args.twitter_only:
        twitter_stats = update_twitter(db, config=config, quiet=args.quiet)
        if do_replies:
            twitter_reply_stats = update_twitter_replies(db, quiet=args.quiet)
    elif args.reddit_resurrect:
        reddit_resurrect_stats = update_reddit_resurrect(db, user_agent, config=config, quiet=args.quiet, days=args.resurrect_days)
    elif args.reddit_only:
        reddit_stats = update_reddit(db, user_agent, config=config, quiet=args.quiet)
        if do_replies:
            reddit_reply_stats = update_reddit_replies(db, user_agent, quiet=args.quiet)
    elif args.moltbook_only:
        moltbook_stats = update_moltbook(db, os.environ.get("MOLTBOOK_API_KEY", ""), quiet=args.quiet)
    elif args.github_only:
        github_stats = update_github(db, quiet=args.quiet, limit=args.github_limit)
        if do_replies:
            github_reply_stats = update_github_replies(db, quiet=args.quiet, limit=args.github_limit)
    else:
        reddit_stats = update_reddit(db, user_agent, config=config, quiet=args.quiet)
        moltbook_stats = update_moltbook(db, os.environ.get("MOLTBOOK_API_KEY", ""), quiet=args.quiet)
        twitter_stats = update_twitter(db, config=config, quiet=args.quiet)
        github_stats = update_github(db, quiet=args.quiet)
        if do_replies:
            reddit_reply_stats = update_reddit_replies(db, user_agent, quiet=args.quiet)
            twitter_reply_stats = update_twitter_replies(db, quiet=args.quiet)
            github_reply_stats = update_github_replies(db, quiet=args.quiet)

    # Gather aggregate totals across all platforms
    totals = get_aggregate_totals(db)

    db.close()

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
            # See update_twitter() print line for the rationale: surface
            # `changed` (rows whose score/comments actually moved) under the
            # "updated" label so stats.sh -> log_run.py -> dashboard pill
            # reflects real activity instead of "every successful poll".
            print(f"\nReddit: {r['total']} total, {r.get('skipped', 0)} skipped, "
                  f"{r['total'] - r.get('skipped', 0)} checked, "
                  f"{r.get('changed', r['updated'])} updated, "
                  f"{r['deleted']} deleted, {r['removed']} removed, {r['errors']} errors" + err_break)
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

        if twitter_stats is not None:
            t = twitter_stats
            # Note: the "updated" field intentionally reports `changed` (rows
            # whose views/likes actually moved), not `updated` (every successful
            # poll). The downstream `stats.sh` greps this line for "updated"
            # and forwards to log_run.py --updated, which the dashboard renders
            # as the "updated" pill. Surfacing the no-op poll count there made
            # "checked == updated" trivially true and hid the real signal.
            print(f"\nTwitter: {t['total']} total, {t.get('skipped', 0)} skipped, "
                  f"{t['total'] - t.get('skipped', 0)} checked, "
                  f"{t.get('changed', t['updated'])} updated, "
                  f"{t['deleted']} deleted, {t['errors']} errors")
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

        print_aggregate_totals(totals)


if __name__ == "__main__":
    main()
