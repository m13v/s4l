#!/usr/bin/env python3
import json
import os
import sys
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_post
from account_resolver import resolve as _resolve_account

EXCLUDED_AUTHORS = {"louis030195", "louis3195"}
# Self-match guardrail: resolved LinkedIn display name (accounts.linkedin.name),
# never a hardcoded literal — a literal filters/attributes as the repo owner on
# every install. OWN_HANDLES keeps the legacy slug forms notifications carry.
OWN_NAME = _resolve_account("linkedin") or ""
OWN_HANDLES = {"m13v", "matthew-diakonov"}

def load_existing_comment_ids():
    s = set()
    with open("/tmp/li_existing_comment_ids.txt") as f:
        for line in f:
            line = line.strip()
            if line:
                s.add(line)
    return s

def load_engaged_pairs():
    s = set()
    with open("/tmp/li_engaged_pairs.txt") as f:
        for line in f:
            line = line.strip()
            if line:
                s.add(line)
    return s

def load_posts():
    """Build mapping by activity_id and ugc_id from our_url."""
    by_id = {}
    with open("/tmp/li_posts.txt") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            pid_str, _, our_url = line.partition("|")
            try:
                pid = int(pid_str)
            except ValueError:
                continue
            ids = set()
            for m in re.findall(r"urn:li:activity:(\d+)", our_url):
                ids.add(("activity", m))
            for m in re.findall(r"urn:li:ugcPost:(\d+)", our_url):
                ids.add(("ugcPost", m))
            for m in re.findall(r"/feed/update/urn:li:(activity|ugcPost):(\d+)", our_url):
                ids.add((m[0], m[1]))
            for m in re.findall(r"/posts/[^/?#]*?(\d{15,})", our_url):
                ids.add(("any", m))
            for m in re.findall(r"(\d{18,20})", our_url):
                ids.add(("any", m))
            for kind, urn_id in ids:
                by_id.setdefault(urn_id, (pid, our_url))
    return by_id

def main():
    existing = load_existing_comment_ids()
    engaged = load_engaged_pairs()
    posts_by_id = load_posts()

    items = []
    for fn in ("/tmp/li_notifications_batch1.json", "/tmp/li_notifications_batch2.json"):
        with open(fn) as f:
            items.extend(json.load(f))

    counts = {
        "discovered": 0,
        "already_tracked": 0,
        "author_already_engaged": 0,
        "excluded": 0,
        "own_account": 0,
        "no_comment_urn": 0,
        "post_not_found_skipped": 0,
        "post_created": 0,
    }

    for it in items:
        author = (it.get("author") or "").strip()
        comment_urn = it.get("comment_urn")
        href = it.get("href")
        snippet = it.get("snippet") or ""
        activity_id = it.get("activity_id")
        ugc_id = it.get("ugc_id")

        if not comment_urn or not (activity_id or ugc_id):
            counts["no_comment_urn"] += 1
            continue

        if author == OWN_NAME or author.lower() in OWN_HANDLES:
            counts["own_account"] += 1
            continue

        author_lower = author.lower()
        if any(ex in author_lower for ex in EXCLUDED_AUTHORS):
            counts["excluded"] += 1
            continue

        if comment_urn in existing:
            counts["already_tracked"] += 1
            continue

        # Find post by activity_id first, then ugc_id
        post_id = None
        our_url = None
        for candidate in (activity_id, ugc_id):
            if candidate and candidate in posts_by_id:
                post_id, our_url = posts_by_id[candidate]
                break

        if post_id is None:
            # Need to insert a new post row
            urn_for_url = activity_id or ugc_id
            kind = "activity" if activity_id else "ugcPost"
            our_url = f"https://www.linkedin.com/feed/update/urn:li:{kind}:{urn_for_url}/"
            # thread_author: best signal we have is the notification author
            # (the replier). It isn't the actual OP, but it's not us, so the
            # dashboard "threads vs comments" filter (server.js /api/top)
            # correctly classifies these as comments under someone else's post.
            thread_author = author or "(unknown)"
            resp = api_post(
                "/api/v1/posts",
                {
                    "platform": "linkedin",
                    "thread_url": our_url,
                    "our_url": our_url,
                    "our_content": "[discovered via notification, no original content tracked]",
                    "project": "general",
                    "thread_author": thread_author,
                    # Resolved posting identity, never hardcoded (mis-attributes
                    # rows on any non-operator install).
                    "our_account": OWN_NAME,
                    "engagement_style": "discovered_via_notification",
                    "status": "active",
                },
                ok_on_conflict=True,
            )
            if (resp.get("error") or {}).get("code") == "duplicate_thread":
                # Post already exists in DB but wasn't in our /tmp/li_posts.txt
                # local map; reuse the existing row instead of creating a dup.
                post_id = (resp["error"].get("details") or {}).get("existing_post_id")
            else:
                post_id = ((resp.get("data") or {}).get("post") or {}).get("id")
            if post_id is None:
                counts["post_not_found_skipped"] += 1
                continue
            counts["post_created"] += 1
            # Add to in-memory map so subsequent items in same loop reuse it
            for cand in (activity_id, ugc_id):
                if cand:
                    posts_by_id[cand] = (post_id, our_url)

        pair_key = f"{author}|||{our_url}"
        if pair_key in engaged:
            counts["author_already_engaged"] += 1
            continue

        # Insert reply (find-or-create; 409 duplicate is fine, the gate may
        # also return 200 with reply:null which we treat as discovered-then-gated).
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

        existing.add(comment_urn)
        engaged.add(pair_key)
        counts["discovered"] += 1

    print(json.dumps(counts, indent=2))

if __name__ == "__main__":
    main()
