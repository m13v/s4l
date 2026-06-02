#!/usr/bin/env python3
"""reddit_threads_helper.py — CLI wrapper used by skill/run-reddit-threads.sh
to replace the four inline `psql "$DATABASE_URL"` reads that built prompt
context. The direct-Postgres lane was removed 2026-06-01; DATABASE_URL is
deliberately ignored, no DB, no fallback. Each subcommand prints exactly what
the psql call printed (one row per line, `|`-delimited like psql -t -A) so the
surrounding shell capture ($(...)) is unchanged.

Subcommands:
  recent-posts-sub --sub SLUG [--limit 10]
      -> own threads in r/SLUG, newest first. Prints
         "<thread_title> |ENDING| <last 200 chars of our_content>" per line.
  recent-posts-project --project P [--days 14] [--limit 15]
      -> own threads project-wide in the last N days, newest first. Same shape.
  recent-styles --project P [--limit 5]
      -> engagement_style of recent own threads (non-empty only), newest first.
  top-posts --project P [--min-score 5] [--limit 10]
      -> top own active threads by (upvotes + comments*3), highest first.
         Prints "<thread_title>|<upvotes>|<comments_count>|<views>" per line.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get  # noqa: E402


def _posts(query: dict) -> list:
    resp = api_get("/api/v1/posts", query=query)
    return (resp.get("data") or {}).get("posts") or []


def _ending_line(p: dict) -> str:
    title = p.get("thread_title") or ""
    content = p.get("our_content") or ""
    return f"{title} |ENDING| {content[-200:]}"


def cmd_recent_posts_sub(sub: str, limit: int) -> int:
    posts = _posts({
        "platform": "reddit",
        "own_threads_only": "true",
        "thread_url_contains": f"/r/{sub}/",
        "order_by": "posted_at",
        "order_dir": "desc",
        "limit": limit,
    })
    for p in posts:
        print(_ending_line(p))
    return 0


def cmd_recent_posts_project(project: str, days: int, limit: int) -> int:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    posts = _posts({
        "platform": "reddit",
        "project": project,
        "own_threads_only": "true",
        "since": since,
        "order_by": "posted_at",
        "order_dir": "desc",
        "limit": limit,
    })
    for p in posts:
        print(_ending_line(p))
    return 0


def cmd_recent_styles(project: str, limit: int) -> int:
    # Over-fetch then filter to non-empty engagement_style, mirroring the old
    # WHERE engagement_style IS NOT NULL AND != '' applied before LIMIT.
    posts = _posts({
        "platform": "reddit",
        "project": project,
        "own_threads_only": "true",
        "order_by": "posted_at",
        "order_dir": "desc",
        "limit": max(limit * 10, 50),
    })
    printed = 0
    for p in posts:
        style = (p.get("engagement_style") or "").strip()
        if not style:
            continue
        print(style)
        printed += 1
        if printed >= limit:
            break
    return 0


def cmd_top_posts(project: str, min_score: int, limit: int) -> int:
    posts = _posts({
        "platform": "reddit",
        "project": project,
        "own_threads_only": "true",
        "status": "active",
        "min_engagement_score": min_score,
        "order_by": "engagement_score",
        "limit": limit,
    })
    for p in posts:
        title = p.get("thread_title") or ""
        upvotes = p.get("upvotes") or 0
        comments = p.get("comments_count") or 0
        views = p.get("views")
        views_str = "" if views is None else str(views)
        print(f"{title}|{upvotes}|{comments}|{views_str}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("recent-posts-sub")
    ps.add_argument("--sub", required=True)
    ps.add_argument("--limit", type=int, default=10)

    pp = sub.add_parser("recent-posts-project")
    pp.add_argument("--project", required=True)
    pp.add_argument("--days", type=int, default=14)
    pp.add_argument("--limit", type=int, default=15)

    pst = sub.add_parser("recent-styles")
    pst.add_argument("--project", required=True)
    pst.add_argument("--limit", type=int, default=5)

    pt = sub.add_parser("top-posts")
    pt.add_argument("--project", required=True)
    pt.add_argument("--min-score", type=int, default=5)
    pt.add_argument("--limit", type=int, default=10)

    args = p.parse_args()
    if args.cmd == "recent-posts-sub":
        return cmd_recent_posts_sub(args.sub, args.limit)
    if args.cmd == "recent-posts-project":
        return cmd_recent_posts_project(args.project, args.days, args.limit)
    if args.cmd == "recent-styles":
        return cmd_recent_styles(args.project, args.limit)
    if args.cmd == "top-posts":
        return cmd_top_posts(args.project, args.min_score, args.limit)
    return 1


if __name__ == "__main__":
    sys.exit(main())
