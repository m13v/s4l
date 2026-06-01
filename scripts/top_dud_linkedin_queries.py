#!/usr/bin/env python3
"""
top_dud_linkedin_queries.py

Returns recent LinkedIn search queries that produced ZERO usable candidates
OR that returned candidates from a low-quality SERP (serp_quality_score < 4),
so the LLM scanner can be told "do not redraft these phrasings, they have
been flat or audience-wrong for the last week".

Why both signals (zero-result AND low-SERP-quality):
- Zero-result query: keyword too narrow, typos, or LinkedIn search index
  rejects the phrasing. Standard dud.
- Low-quality SERP: query returns 30 hits but all from influencer-bait
  accounts; technically not zero, but useless for our outbound posting.
  Same dud-class for the LLM's purposes.

Pair with top_linkedin_queries.py (positive signal).

    python3 scripts/top_dud_linkedin_queries.py [--project NAME] [--search-topic TOPIC] [--limit 30] [--window-days 7]

Output: JSON list of
    {"query": ..., "project": ..., "search_topic": ..., "attempts": N,
     "last_ran_h_ago": F, "reason": "zero_results"|"low_serp_quality"}

Window default 7 days (vs Twitter's 48h). LinkedIn cycle frequency is much
lower; need a wider window to gather enough samples.

Source: linkedin_search_attempts (one row per query per cycle, written by
run-linkedin.sh after Phase A scrape parses queries_used).

Migrated 2026-06-01 from direct db.py SELECTs to the s4l.ai HTTP API
(GET /api/v1/linkedin-search-attempts/duds). No DATABASE_URL needed.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--window-days", type=int, default=7,
                   help="Look back this many days for dud queries.")
    p.add_argument("--low-serp-threshold", type=float, default=4.0,
                   help="serp_quality_score below this counts as a dud.")
    p.add_argument("--project", default=None)
    p.add_argument("--search-topic", default=None)
    args = p.parse_args()

    query = {
        "limit": args.limit,
        "window_days": args.window_days,
        "low_serp_threshold": args.low_serp_threshold,
    }
    if args.project:
        query["project"] = args.project
    if args.search_topic:
        query["search_topic"] = args.search_topic

    resp = api_get("/api/v1/linkedin-search-attempts/duds", query)
    out = (resp.get("data") or {}).get("duds") or []
    json.dump(out, sys.stdout)
    print("", file=sys.stdout)


if __name__ == "__main__":
    main()
