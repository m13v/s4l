#!/usr/bin/env python3
"""
twitter_supply_signal.py

Per-project supply table: at each `min_faves:N` tier, what's the median
number of tweets X actually returned for queries we ran for that project?

This is the answer to the question the Phase 1 scanner has been guessing
at since the cycle was written: "what min_faves should I use for this
project?". Today the prompt says a flat "broad=50, narrow=20" rule, which
works for tech-Twitter (mk0r, claude-meter, fazm) but starves student-
Twitter (studyly), where even niche audience tweets rarely clear 20 likes.

Output: JSON list of
    {"project": "<name>", "tiers": [{"min_faves": N, "attempts": N,
                                     "median_tweets_found": N,
                                     "zero_result_pct": 0-100}, ...]}
sorted by project. Within each project, tiers ordered ascending min_faves
so the model can read "as I raise the floor, supply collapses; pick the
lowest min_faves where supply is still ≥3".

Usage:

    python3 scripts/twitter_supply_signal.py [--window-days 14] [--project NAME]

Migrated 2026-05-18: reads now go through /api/v1/twitter-search-attempts/
supply-signal via scripts/http_api.py instead of psycopg2.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--window-days", type=int, default=14)
    p.add_argument("--project", default=None,
                   help="If set, only return supply table for this project.")
    args = p.parse_args()

    query = {"window_days": args.window_days}
    if args.project:
        query["project"] = args.project

    resp = api_get("/api/v1/twitter-search-attempts/supply-signal", query=query)
    out = (resp.get("data") or {}).get("rows") or []

    json.dump(out, sys.stdout)
    print("", file=sys.stdout)


if __name__ == "__main__":
    main()
