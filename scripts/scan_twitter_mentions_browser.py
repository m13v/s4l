#!/usr/bin/env python3
"""Scan Twitter notifications via the browser (no API cost) and insert new replies.

Browser-based replacement for the old API-powered scan_twitter_mentions.py.
Consumes JSON from `twitter_browser.py notifications [scroll] [tab]` which
defaults to the /notifications (All) tab so we catch nested replies where the
@-tag was dropped. Pass tab="mentions" to restrict to explicit @-mentions only.
Companion: scan_twitter_thread_followups.py revisits our recent replies to
pick up depth-2+ follow-ups that never surface in notifications at all.

Usage:
    python3 scripts/twitter_browser.py notifications 8 all > /tmp/twitter_notifs.json
    python3 scripts/scan_twitter_mentions_browser.py --json-file /tmp/twitter_notifs.json

Migrated 2026-05-18: reads/writes go through s4l.ai HTTP API (/api/v1/posts,
/api/v1/posts/lookup, /api/v1/replies) via scripts/http_api.py instead of
psycopg2. Note: the route enforces (platform, their_comment_id) uniqueness
server-side, so the "existing_ids" prefetch is now a soft local cache used
to short-circuit the POST loop; we still rely on the API's ON CONFLICT path
as the source of truth.

Migrated 2026-05-23: third-party mentions now write to the dedicated
`mentions` table via /api/v1/mentions instead of a placeholder row in
`posts`. The associated reply row carries `mention_id` instead of
`post_id`, enforced by the replies_post_or_mention_exclusive_check DB
constraint. See migrations/2026-05-23-mentions-table.sql and
scripts/migrate_mentions_out_of_posts.py for the cutover history.
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_post  # noqa: E402
from project_topics import topics_for_project  # noqa: E402
try:
    from account_resolver import resolve as _resolve_account  # noqa: E402
except Exception:
    def _resolve_account(_platform):  # type: ignore[unused-arg]
        return None

CONFIG_PATH = os.path.expanduser("~/social-autoposter/config.json")
MIN_WORDS = 3
OUR_HANDLE = _resolve_account("twitter")
if not OUR_HANDLE:
    # No hardcoded fallback: scanning/attributing under a default handle silently
    # impersonates the repo owner. Refuse to run so the missing config surfaces.
    sys.stderr.write(
        "[scan_twitter_mentions] no Twitter handle configured "
        "(accounts.twitter.handle / AUTOPOSTER_TWITTER_HANDLE); refusing to run "
        "to avoid wrong-account attribution. Run connect_x first.\n")
    sys.exit(1)

# Paginate the replies prefetch in chunks so we never blow the route's max
# limit. 500 is the per-call cap inside /api/v1/replies; we walk pages until
# the response is short.
REPLY_PAGE_LIMIT = 500
REPLY_MAX_PAGES = 200  # 100k rows of headroom; plenty for the dedup cache.


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def word_count(text):
    return len(text.split()) if text else 0


def get_existing_reply_ids():
    """Pull every existing replies.their_comment_id for platform=x as a dedup cache.

    The route caps responses at 500 rows per call; we paginate by id DESC and
    keep walking until we exhaust the set. The route also handles uniqueness
    on the server, so even if our local cache lags slightly we won't insert
    duplicates — we'll just get ok_on_conflict back from POST.
    """
    cache = set()
    max_id = None
    for _ in range(REPLY_MAX_PAGES):
        query = {
            "platform": "x",
            "limit": REPLY_PAGE_LIMIT,
            "order_by": "id",
        }
        # We don't have an explicit max_id filter on the route today; walk by
        # `since` instead is wrong (since acts on discovered_at). Easiest: ask
        # for the first 500 most-recent rows and trust that older rows in DB
        # already collided once at insert-time, so we don't need a perfect
        # global cache — just a recency window deep enough to catch this
        # cycle's incoming notifications.
        resp = api_get("/api/v1/replies", query=query)
        rows = (resp.get("data") or {}).get("replies") or []
        if not rows:
            break
        for r in rows:
            cid = r.get("their_comment_id")
            if cid:
                cache.add(cid)
        if len(rows) < REPLY_PAGE_LIMIT:
            break
        # Today's route has no "id <" cursor parameter, so one page is all we
        # get. That is enough: it caps memory + roundtrip and the server-side
        # UNIQUE index is still the canonical dedup. Break out.
        break
        # Suppress unused-binding lint warning for max_id while we leave the
        # placeholder in place; future route work may add an id-cursor.
        _ = max_id
    return cache


def get_our_posts():
    """Map tweet_id (last URL segment) -> post row for our active twitter posts."""
    resp = api_get(
        "/api/v1/posts",
        query={"platform": "twitter", "status": "active", "limit": 500},
    )
    rows = (resp.get("data") or {}).get("posts") or []
    posts = {}
    for row in rows:
        url = row.get("our_url")
        if not url:
            continue
        m = re.search(r"/status/(\d+)", url)
        if m:
            posts[m.group(1)] = row
    return posts


def guess_project(text, config):
    projects = config.get("projects", [])
    text_lower = (text or "").lower()
    for p in projects:
        name = p.get("name", "")
        # DB-backed seed list (post 2026-05-27 config.json removal).
        topics = topics_for_project(name)
        for topic in topics:
            if topic.lower() in text_lower:
                return name
        if name.lower() in text_lower:
            return name
    return config.get("default_project", "General")


def most_recent_active_project():
    """Project_name of the most recent active twitter post we made.

    Used as a fallback for replies-to-us where the notification feed doesn't
    expose the parent tweet ID, so we can't identify *which* of our posts
    the mention is under. Recency is a much stronger signal than
    keyword-matching a 3-word reply body.

    Post 2026-05-23 the "(mention - no original post)" placeholder rows no
    longer exist in `posts` (they live in `mentions` now), so the SQL/
    client-side filter that used to live here is gone.
    """
    resp = api_get(
        "/api/v1/posts",
        query={
            "platform": "twitter",
            "status": "active",
            "limit": 50,
        },
    )
    rows = (resp.get("data") or {}).get("posts") or []
    for r in rows:
        proj = r.get("project_name")
        if not proj:
            continue
        return proj
    return None


def process_notifications(notifications, config):
    exclusions = config.get("exclusions", {})
    excluded_accounts = {a.lower() for a in exclusions.get("twitter_accounts", [])}
    excluded_accounts.add(OUR_HANDLE.lower())

    existing_ids = get_existing_reply_ids()
    our_posts = get_our_posts()
    recent_project = most_recent_active_project()

    stats = {
        "new": 0,
        "already_tracked": 0,
        "excluded_author": 0,
        "own_account": 0,
        "too_short": 0,
        "no_tweet_id": 0,
    }

    for n in notifications:
        tweet_id = n.get("tweet_id", "")
        handle = (n.get("handle") or "").lstrip("@")
        text = n.get("text") or ""
        tweet_url = n.get("tweet_url") or (
            f"https://x.com/{handle}/status/{tweet_id}" if handle and tweet_id else ""
        )
        replying_to = (n.get("replying_to") or "").lstrip("@").lower()

        if not tweet_id:
            stats["no_tweet_id"] += 1
            continue

        if tweet_id in existing_ids:
            stats["already_tracked"] += 1
            continue

        if handle.lower() in excluded_accounts:
            stats["own_account" if handle.lower() == OUR_HANDLE.lower() else "excluded_author"] += 1
            continue

        if word_count(text) < MIN_WORDS:
            stats["too_short"] += 1
            continue

        # Resolve project for the mention. Reply-to-us inherits the project
        # of our most recent active post (short reply text is unreliable for
        # keyword matching); other mentions fall back to keyword guess.
        is_reply_to_us = replying_to == OUR_HANDLE.lower() and bool(our_posts)
        if is_reply_to_us and recent_project:
            project = recent_project
        else:
            project = guess_project(text, config)
        # _ = our_posts  # currently unused for direct post_id linkage; notifications
        # don't expose conversation_id, so we attribute via mentions table only.

        # Insert into /api/v1/mentions. Dedup on (platform, mentioning_url)
        # — if the row already exists we get back existing_mention_id from
        # the 409 body via ok_on_conflict.
        mention_body = {
            "platform": "twitter",
            "mentioning_url": tweet_url,
            "mentioning_handle": handle,
            "mentioning_text": text,
            "our_handle": OUR_HANDLE,
            "project": project,
            "status": "active",
        }
        mention_resp = api_post(
            "/api/v1/mentions", mention_body, ok_on_conflict=True,
        )
        mention_data = mention_resp.get("data") or {}
        mention_row = mention_data.get("mention") or {}
        mention_id = mention_row.get("id")
        if not mention_id and mention_resp.get("error"):
            details = (mention_resp.get("error") or {}).get("details") or {}
            mention_id = details.get("existing_mention_id")
            if not mention_id:
                inner = details.get("mention") or {}
                mention_id = inner.get("id")
        if not mention_id:
            print(
                f"  WARNING: could not resolve mention_id for {tweet_url!r}; skipping",
                file=sys.stderr,
            )
            continue

        reply_resp = api_post(
            "/api/v1/replies",
            {
                "mention_id": mention_id,
                "platform": "x",
                "their_comment_id": tweet_id,
                "their_author": handle,
                "their_content": text,
                "their_comment_url": tweet_url,
                "depth": 1,
                "status": "pending",
                "our_account": OUR_HANDLE,
            },
            ok_on_conflict=True,
        )
        # 409 means the row already existed under the server-side UNIQUE
        # (platform, their_comment_id) constraint; count it as already_tracked
        # rather than new so the summary matches reality.
        if (reply_resp.get("error") or {}).get("code") == "duplicate_reply":
            stats["already_tracked"] += 1
        else:
            stats["new"] += 1
            print(f"  NEW: @{handle}: {text[:80]}")
        existing_ids.add(tweet_id)

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Process Twitter notification data from browser scanner"
    )
    parser.add_argument(
        "--json-file",
        required=True,
        help="Path to JSON from twitter_browser.py notifications",
    )
    args = parser.parse_args()

    with open(args.json_file) as f:
        data = json.load(f)

    if isinstance(data, dict) and data.get("error"):
        print(f"ERROR from extractor: {data['error']}", file=sys.stderr)
        sys.exit(1)

    notifications = data.get("notifications", []) if isinstance(data, dict) else data
    print(f"Processing {len(notifications)} mentions...")

    config = load_config()
    stats = process_notifications(notifications, config)

    print(
        f"\nSummary: {stats['new']} new, "
        f"{stats['already_tracked']} already tracked, "
        f"{stats['excluded_author']} excluded, "
        f"{stats['own_account']} own account, "
        f"{stats['too_short']} too short, "
        f"{stats['no_tweet_id']} no tweet_id"
    )


if __name__ == "__main__":
    main()
