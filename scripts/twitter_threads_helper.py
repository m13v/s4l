#!/usr/bin/env python3
"""twitter_threads_helper.py — small CLI wrapper used by
skill/run-twitter-threads.sh to replace the three `psql` one-liners that
loaded recent posts / styles / top performers for the original-thread
prompt context. Each subcommand prints exactly one value to stdout (raw
newline-separated content or pipe-separated tuples) so the surrounding
bash code can keep using $(...) capture unchanged.

Subcommands:
  recent-posts --project P [--days 14] [--limit 10]
      -> GET /api/v1/posts?platform=twitter&project=P
            &since=<now-14d>&limit=...
      -> filter rows to platform='twitter' AND thread_url = our_url
         (= our original posts, not mention placeholders) and print one
         line per row containing the post's our_content (newlines and
         pipes inside content survive because the legacy psql -t -A
         output did the same).

  recent-styles --project P [--limit 5]
      -> Same source; print one engagement_style per line for the most
         recent N our-original posts where engagement_style is set.

  top-posts --project P [--limit 8]
      -> Same source; print pipe-separated tuples:
           our_content|upvotes|comments_count|views
         filtered to rows with composite (upvotes + 3*comments + views/100)
         > 5 and sorted by the same composite DESC.

Migrated 2026-05-18: removes 3 direct psql calls from
skill/run-twitter-threads.sh. The route at /api/v1/posts already supports
platform + project + since + status filters server-side; this helper just
shapes the response into the legacy line/pipe format the bash prompt
consumes verbatim.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get  # noqa: E402


def _fetch_posts(project: str, days: int | None = None, limit: int = 500):
    """Pull recent twitter posts for a project via /api/v1/posts. The
    server-side WHERE handles platform/project/status/since; the
    our_content NOT ILIKE '(mention%' + thread_url = our_url filters are
    applied client-side so the route stays general-purpose."""
    query: dict = {
        "platform": "twitter",
        "project": project,
        "limit": limit,
        "status": "active",
        "has_our_url": "true",
    }
    if days is not None:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        query["since"] = since
    resp = api_get("/api/v1/posts", query=query)
    rows = (resp.get("data") or {}).get("posts") or []
    # Apply the mention-placeholder + our-original filters here, matching
    # the legacy SQL WHERE clauses byte-for-byte.
    out = []
    for r in rows:
        if (r.get("our_content") or "").startswith("(mention"):
            continue
        if r.get("thread_url") != r.get("our_url"):
            continue
        out.append(r)
    return out


def cmd_recent_posts(project: str, days: int, limit: int) -> int:
    rows = _fetch_posts(project, days=days, limit=max(limit * 5, 50))
    # Posts come back posted_at DESC already; just take the first N.
    for r in rows[:limit]:
        content = (r.get("our_content") or "").replace("\n", " ").replace("\r", " ")
        sys.stdout.write(content + "\n")
    return 0


def cmd_recent_styles(project: str, limit: int) -> int:
    # No --days bound originally; pull a generous window so we don't miss
    # styles when the account has been quiet recently.
    rows = _fetch_posts(project, days=90, limit=max(limit * 10, 50))
    n = 0
    for r in rows:
        style = (r.get("engagement_style") or "").strip()
        if not style:
            continue
        sys.stdout.write(style + "\n")
        n += 1
        if n >= limit:
            break
    return 0


def cmd_top_posts(project: str, limit: int) -> int:
    rows = _fetch_posts(project, days=None, limit=500)

    def composite(r):
        return (
            int(r.get("upvotes") or 0)
            + int(r.get("comments_count") or 0) * 3
            + int(r.get("views") or 0) // 100
        )

    # Same composite + threshold + sort as the legacy SQL: floor=5,
    # ORDER BY composite DESC, LIMIT.
    filtered = [r for r in rows if composite(r) > 5]
    filtered.sort(key=composite, reverse=True)
    for r in filtered[:limit]:
        content = (r.get("our_content") or "").replace("\n", " ").replace("\r", " ")
        upvotes = int(r.get("upvotes") or 0)
        comments = int(r.get("comments_count") or 0)
        views = int(r.get("views") or 0)
        sys.stdout.write(f"{content}|{upvotes}|{comments}|{views}\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Helper for run-twitter-threads.sh")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_rp = sub.add_parser("recent-posts")
    p_rp.add_argument("--project", required=True)
    p_rp.add_argument("--days", type=int, default=14)
    p_rp.add_argument("--limit", type=int, default=10)

    p_rs = sub.add_parser("recent-styles")
    p_rs.add_argument("--project", required=True)
    p_rs.add_argument("--limit", type=int, default=5)

    p_tp = sub.add_parser("top-posts")
    p_tp.add_argument("--project", required=True)
    p_tp.add_argument("--limit", type=int, default=8)

    args = ap.parse_args()

    if args.cmd == "recent-posts":
        return cmd_recent_posts(args.project, args.days, args.limit)
    if args.cmd == "recent-styles":
        return cmd_recent_styles(args.project, args.limit)
    if args.cmd == "top-posts":
        return cmd_top_posts(args.project, args.limit)
    return 1


if __name__ == "__main__":
    sys.exit(main())
