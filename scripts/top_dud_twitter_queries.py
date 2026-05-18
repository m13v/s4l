#!/usr/bin/env python3
"""
top_dud_twitter_queries.py

Returns recent Twitter search queries that produced ZERO tweets so the
LLM scanner can be told "do not redraft these phrasings — they were flat
in the last N hours". Counterpart to top_twitter_queries.py (positive
signal): this is the negative-signal feed.

    python3 scripts/top_dud_twitter_queries.py [--limit 30] [--window-hours 48]

Output: JSON list of
    {"query": ..., "project": ..., "min_faves": N | null,
     "attempts": N, "last_ran_h_ago": F}
sorted by most-attempted dud first (so the most-wasteful repeats surface
at the top of the prompt anti-list).

The min_faves field is parsed from the query string (X operator
`min_faves:N`). Surfacing it lets the model correlate "every studyly dud
last 48h used min_faves:20" → drop the floor for that project.

Source: twitter_search_attempts (one row per query per cycle, written by
run-twitter-cycle.sh after the Phase 1 scan parses queries_used).

Migrated 2026-05-18: reads now go through /api/v1/twitter-search-attempts/
dud-queries via scripts/http_api.py instead of a direct psycopg2 query.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--window-hours", type=int, default=48,
                   help="Look back this many hours for dud queries.")
    p.add_argument("--project", default=None,
                   help="If set, only return duds for this project.")
    args = p.parse_args()

    query = {
        "limit": args.limit,
        "window_hours": args.window_hours,
    }
    if args.project:
        query["project"] = args.project

    resp = api_get("/api/v1/twitter-search-attempts/dud-queries", query=query)
    rows = (resp.get("data") or {}).get("rows") or []

    out = [
        {
            "query": r.get("query"),
            "project": r.get("project") or "",
            "min_faves": r.get("min_faves"),
            "attempts": int(r.get("attempts") or 0),
            "last_ran_h_ago": round(float(r.get("last_ran_h_ago") or 0), 1),
        }
        for r in rows
    ]
    json.dump(out, sys.stdout)
    print("", file=sys.stdout)


if __name__ == "__main__":
    main()
