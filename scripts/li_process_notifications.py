#!/usr/bin/env python3
"""Process LinkedIn notifications captured via browser_run_code.

Reads /tmp/li_notifications.json (list of {type, author, href, activity_id,
comment_urn, snippet}) and inserts new rows into the replies table for any
notification not already tracked.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_post

EXCLUDED_AUTHORS = {"louis030195", "louis3195"}
OWN_NAMES = {"Matthew Diakonov", "m13v"}

NOTIFS_FILE = "/tmp/li_notifications.json"
EXISTING_COMMENTS_FILE = "/tmp/li_existing_comments.txt"
EXISTING_PAIRS_FILE = "/tmp/li_existing_pairs.txt"
POSTS_FILE = "/tmp/li_posts.txt"


def load_lines(path):
    with open(path) as f:
        return {line.strip() for line in f if line.strip()}


def load_posts():
    posts = []
    with open(POSTS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            pid, our_url = line.split("|", 1)
            posts.append((int(pid), our_url))
    return posts


def find_post_by_activity(posts, activity_id):
    if not activity_id:
        return None
    for pid, our_url in posts:
        if activity_id in our_url:
            return (pid, our_url)
    return None


def main():
    notifs = json.load(open(NOTIFS_FILE))
    existing_comments = load_lines(EXISTING_COMMENTS_FILE)
    existing_pairs = load_lines(EXISTING_PAIRS_FILE)
    posts = load_posts()

    counts = {
        "new": 0,
        "already_tracked": 0,
        "author_already_engaged": 0,
        "excluded": 0,
        "own_account": 0,
        "no_comment_urn": 0,
        "post_created": 0,
    }

    for n in notifs:
        author = n.get("author") or ""
        comment_urn = n.get("comment_urn")
        activity_id = n.get("activity_id")
        href = n.get("href")
        snippet = (n.get("snippet") or "").strip()

        if not comment_urn or not activity_id:
            counts["no_comment_urn"] += 1
            continue
        if author in OWN_NAMES:
            counts["own_account"] += 1
            continue
        if any(ex.lower() in author.lower() for ex in EXCLUDED_AUTHORS):
            counts["excluded"] += 1
            continue
        if comment_urn in existing_comments:
            counts["already_tracked"] += 1
            continue

        # Find or create post
        match = find_post_by_activity(posts, activity_id)
        if match:
            post_id, our_url = match
        else:
            our_url = f"https://www.linkedin.com/feed/update/urn:li:activity:{activity_id}/"
            resp = api_post(
                "/api/v1/posts",
                {
                    "platform": "linkedin",
                    "thread_url": our_url,
                    "thread_author": author,            # best signal we have
                    "thread_content": snippet[:500],    # best we have
                    "our_url": our_url,
                    "our_content": "[discovered from notifications, no original content tracked]",
                    "our_account": "matthew-autoposter",
                    "source_summary": "discovered_from_notifications",
                    "project": "general",               # topics empty in config
                    "engagement_style": "discovery",
                    "status": "active",
                },
                ok_on_conflict=True,
            )
            if (resp.get("error") or {}).get("code") == "duplicate_thread":
                # Already in DB but not in our /tmp/li_posts.txt local map;
                # reuse the existing row rather than create a duplicate.
                post_id = (resp["error"].get("details") or {}).get("existing_post_id")
            else:
                post_id = ((resp.get("data") or {}).get("post") or {}).get("id")
            if post_id is None:
                counts["no_comment_urn"] += 1
                continue
            posts.append((post_id, our_url))
            counts["post_created"] += 1

        pair_key = f"{author}|||{our_url}"
        if pair_key in existing_pairs:
            counts["author_already_engaged"] += 1
            continue

        api_post(
            "/api/v1/replies",
            {
                "platform": "linkedin",
                "post_id": post_id,
                "their_comment_id": comment_urn,
                "their_author": author,
                "their_content": snippet,
                "their_comment_url": href,
                "depth": 1,
                "status": "pending",
            },
            ok_on_conflict=True,
        )
        existing_comments.add(comment_urn)
        existing_pairs.add(pair_key)
        counts["new"] += 1

    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
