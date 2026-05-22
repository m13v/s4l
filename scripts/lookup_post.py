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
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod


PLATFORM_PATTERNS = {
    "twitter": r"/status/{id}([^0-9]|$)",
    "x": r"/status/{id}([^0-9]|$)",
    "linkedin": r"urn:li:activity:{id}([^0-9]|$)",
}


def lookup(platform, post_id):
    pattern_template = PLATFORM_PATTERNS.get(platform.lower())
    if not pattern_template:
        return {"project": None, "error": f"unknown platform: {platform}"}

    if not re.fullmatch(r"[0-9]+", post_id):
        return {"project": None, "error": "post_id must be digits"}

    db_platform = "twitter" if platform.lower() in ("twitter", "x") else platform.lower()
    pattern = pattern_template.format(id=post_id)

    dbmod.load_env()
    conn = dbmod.get_conn()
    try:
        row = conn.execute(
            """
            SELECT project_name, our_content, thread_url, posted_at
            FROM posts
            WHERE platform = %s
              AND status = 'active'
              AND our_url ~ %s
              AND posted_at > NOW() - INTERVAL '30 days'
            ORDER BY posted_at DESC
            LIMIT 1
            """,
            (db_platform, pattern),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return {"project": None}

    return {
        "project": row[0] if isinstance(row, (list, tuple)) else row["project_name"],
        "our_content": row[1] if isinstance(row, (list, tuple)) else row["our_content"],
        "thread_url": row[2] if isinstance(row, (list, tuple)) else row["thread_url"],
        "posted_at": (row[3] if isinstance(row, (list, tuple)) else row["posted_at"]).isoformat(),
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
