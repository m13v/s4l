#!/usr/bin/env python3
"""github_engage_helper.py — small CLI wrapper used by skill/github-engage.sh
to replace the five `psql "$DATABASE_URL" -t -A -c "..."` one-liners the shell
used to embed inline. The direct-Postgres lane was removed 2026-06-01;
DATABASE_URL is deliberately ignored, no DB, no fallback. Every subcommand
prints exactly what the corresponding psql call printed so the surrounding
shell capture ($(...)) and integer compares are unchanged.

Subcommands:
  posts-active-count
      -> GET /api/v1/posts/count?platform=github&status=active
      -> prints the integer count (was: SELECT COUNT(*) FROM posts
         WHERE platform='github' AND status='active')
  pending-count
      -> GET /api/v1/replies/counts?platform=github
      -> prints the integer pending count (was: SELECT COUNT(*) FROM replies
         WHERE platform='github' AND status='pending')
  reply-counts
      -> GET /api/v1/replies/counts?platform=github
      -> prints JSON {pending, replied, skipped} (replaces the three trailing
         psql COUNT one-liners in Phase C)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get  # noqa: E402


def _counts_dict() -> dict[str, int]:
    resp = api_get("/api/v1/replies/counts", query={"platform": "github"})
    data = resp.get("data") or {}
    # github has no mentions/orphan nuance; the raw `counts` field is the
    # authoritative per-status tally. (eligible_counts would also work but
    # for github every reply is post-rooted.)
    rows = data.get("counts") or []
    out: dict[str, int] = {}
    for r in rows:
        s = r.get("status")
        if s is None:
            continue
        try:
            out[str(s)] = int(r.get("count") or 0)
        except (TypeError, ValueError):
            out[str(s)] = 0
    return out


def cmd_posts_active_count() -> int:
    resp = api_get("/api/v1/posts/count", query={"platform": "github", "status": "active"})
    print(int((resp.get("data") or {}).get("count") or 0))
    return 0


def cmd_pending_count() -> int:
    print(int(_counts_dict().get("pending") or 0))
    return 0


def cmd_reply_counts() -> int:
    counts = _counts_dict()
    out = {
        "pending": int(counts.get("pending") or 0),
        "replied": int(counts.get("replied") or 0),
        "skipped": int(counts.get("skipped") or 0),
    }
    json.dump(out, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("posts-active-count")
    sub.add_parser("pending-count")
    sub.add_parser("reply-counts")
    args = p.parse_args()
    if args.cmd == "posts-active-count":
        return cmd_posts_active_count()
    if args.cmd == "pending-count":
        return cmd_pending_count()
    if args.cmd == "reply-counts":
        return cmd_reply_counts()
    return 1


if __name__ == "__main__":
    sys.exit(main())
