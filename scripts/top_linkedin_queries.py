#!/usr/bin/env python3
"""
top_linkedin_queries.py

Returns the top-performing historical LinkedIn search queries by how many
candidates they produced that actually got posted. Used as STYLE inspiration
for the LLM that drafts new queries, NOT as literal keyword reuse (LinkedIn
SERP shifts daily, so reusing the exact same query is wasteful).

Pair with top_dud_linkedin_queries.py (negative signal).

    python3 scripts/top_linkedin_queries.py [--project NAME] [--search-topic TOPIC] [--limit 20] [--window-days 30]

Output: JSON list of {"query": ..., "project": ..., "search_topic": ..., "posts": N, "avg_velocity": X, "avg_serp_quality": Y}

Window default 30 days (vs Twitter's 14): LinkedIn cycle is sparser, longer
window captures enough samples.

Migrated 2026-06-01 from direct db.py SELECTs to the s4l.ai HTTP API
(GET /api/v1/linkedin-candidates/top-queries). No DATABASE_URL needed.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--window-days", type=int, default=30)
    p.add_argument("--project", default=None)
    p.add_argument("--search-topic", default=None)
    args = p.parse_args()

    query = {
        "limit": args.limit,
        "window_days": args.window_days,
    }
    if args.project:
        query["project"] = args.project
    if args.search_topic:
        query["search_topic"] = args.search_topic

    resp = api_get("/api/v1/linkedin-candidates/top-queries", query)
    out = (resp.get("data") or {}).get("queries") or []
    json.dump(out, sys.stdout)
    print("", file=sys.stdout)


if __name__ == "__main__":
    main()
