#!/usr/bin/env python3
"""
top_dud_reddit_queries.py

Returns recent Reddit search queries that produced ZERO post-filter
candidates so post_reddit.py:build_prompt can tell the LLM scanner
"do not redraft these phrasings, they were flat in the last N hours".
Counterpart to top_search_topics.py (positive signal): this is the
negative-signal feed.

    python3 scripts/top_dud_reddit_queries.py [--project NAME] [--limit 30] [--window-hours 168]

Output: JSON list of
  {"query": ..., "subreddits": ..., "project": ..., "attempts": N, "last_ran_h_ago": F}
sorted by most-attempted dud first (so the most-wasteful repeats surface
at the top of the prompt anti-list).

Source: reddit_search_attempts (one row per (query, subreddits, project)
per cmd_search call, written by reddit_tools.py:cmd_search). Routed
through GET /api/v1/reddit-search-attempts/dud-queries on the website
to keep this script HTTP-only (no psycopg2 / no DATABASE_URL).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--project", default=None,
                   help="Filter to a single project (matches project_name).")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--window-hours", type=int, default=168,
                   help="Look back this many hours for dud queries (default 7d).")
    args = p.parse_args()

    query = {
        "limit": args.limit,
        "window_hours": args.window_hours,
    }
    if args.project:
        query["project"] = args.project

    resp = api_get("/api/v1/reddit-search-attempts/dud-queries", query=query)
    data = (resp or {}).get("data") or {}
    rows = data.get("rows") or []

    out = [
        {
            "query": r.get("query"),
            "subreddits": r.get("subreddits") or None,
            "project": r.get("project") or "",
            "attempts": int(r.get("attempts") or 0),
            "last_ran_h_ago": round(float(r.get("last_ran_h_ago") or 0), 1),
        }
        for r in rows
    ]
    json.dump(out, sys.stdout)
    print("", file=sys.stdout)


if __name__ == "__main__":
    main()
