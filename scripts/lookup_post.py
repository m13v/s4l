#!/usr/bin/env python3
"""Look up one of our posts by platform-native ID (tweet_id / activity_id).

Used by engage-twitter.sh and engage-linkedin.sh after the engage agent
navigates a thread, extracts the parent post ID, and needs to resolve which
project that post belongs to (so it can override replies.project_name and
draft in the right voice). Replaces the per-prompt OUR_POSTS_INDEX blob
that was costing 360-573 KB per engage prompt.

Usage:
    python3 scripts/lookup_post.py twitter <tweet_id>
    python3 scripts/lookup_post.py linkedin <activity_id>

Output (JSON, single line):
    {"project": "fazm", "our_content": "...full text...", "thread_url": "..."}

If no match in the last 30 days of active posts:
    {"project": null}

Migrated 2026-06-01 from raw psycopg2 (db.get_conn) to the s4l.ai HTTP API
(GET /api/v1/posts/lookup?platform=&post_id=). The platform-native id regex
match (twitter /status/<id>, linkedin urn:li:activity:<id>) now runs server-
side; the PLATFORM_PATTERNS table is mirrored there. Runs on a machine with
no DATABASE_URL.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get


# Mirrored server-side in /api/v1/posts/lookup (ID_PATTERNS). Kept here only
# for input validation / unknown-platform rejection before the round trip.
PLATFORM_PATTERNS = {
    "twitter": r"/status/{id}([^0-9]|$)",
    "x": r"/status/{id}([^0-9]|$)",
    "linkedin": r"urn:li:activity:{id}([^0-9]|$)",
}


def lookup(platform, post_id):
    if platform.lower() not in PLATFORM_PATTERNS:
        return {"project": None, "error": f"unknown platform: {platform}"}

    if not re.fullmatch(r"[0-9]+", post_id):
        return {"project": None, "error": "post_id must be digits"}

    resp = api_get(
        "/api/v1/posts/lookup",
        {"platform": platform.lower(), "post_id": post_id},
    )
    post = (resp.get("data") or {}).get("post")
    if not post:
        return {"project": None}

    return {
        "project": post.get("project_name"),
        "our_content": post.get("our_content"),
        "thread_url": post.get("thread_url"),
        "posted_at": post.get("posted_at"),
    }


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    platform, post_id = sys.argv[1], sys.argv[2]
    result = lookup(platform, post_id)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
