#!/usr/bin/env python3
"""
top_dud_twitter_topics.py

Returns recent Twitter search_topic SEEDS that are pulling in alive-but-off-fit
candidates: the search returns viral tweets, Phase 1 stamps them on
twitter_candidates rows, but Phase 2b's draft gate keeps skipping them (or
they expire un-drafted). The CONCEPT SEED is finding noise, not buyers.

Where this fits in the feedback ladder:
- top_search_topics.py        = positive signal (seed -> posted -> engagement)
- top_dud_twitter_queries.py  = "search returned 0 tweets" signal (the query
                                phrasing is dead; reword the phrasing)
- THIS                        = "search returned viral content, draft gate
                                killed it" signal (the CONCEPT is off-fit;
                                reword the seed narrower or drop it)

Output: JSON list (so build_discover_prompt can paste it directly), sorted
most-skipped first:

    [{"search_topic": "...", "project": "...",
      "posted_n": N, "skipped_n": M,
      "avg_virality_posted": F, "avg_virality_skipped": F,
      "omit_rate": 0.NN, "last_skip_h_ago": F.F,
      "sample_skip_reasons": ["off_brand_crypto", "audience_mismatch", ...]}]

Usage:
    python3 scripts/top_dud_twitter_topics.py [--project NAME] [--limit 15] [--window-hours 168]

Routed through GET /api/v1/twitter-candidates/dud-topics. omit_rate and
last_skip_h_ago are computed server-side; the route returns the legacy CLI
contract directly so the Python wrapper is thin.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--project", default=None,
                   help="Filter to a single project (matches matched_project).")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--window-hours", type=int, default=168,
                   help="Look back this many hours (default 7d).")
    p.add_argument("--min-skips", type=int, default=3,
                   help="Suppress seeds with fewer than this many "
                        "skipped/expired candidates in the window "
                        "(default 3; below that the signal is too thin).")
    args = p.parse_args()

    query = {
        "limit": args.limit,
        "window_hours": args.window_hours,
        "min_skips": args.min_skips,
    }
    if args.project:
        query["project"] = args.project

    resp = api_get("/api/v1/twitter-candidates/dud-topics", query=query)
    rows = (resp.get("data") or {}).get("rows") or []

    # Route already returns the legacy shape. Mirror keys explicitly so
    # downstream callers see a stable contract even if the route adds
    # extra fields later.
    out = [
        {
            "search_topic": r.get("search_topic"),
            "project": r.get("project") or "",
            "posted_n": int(r.get("posted_n") or 0),
            "skipped_n": int(r.get("skipped_n") or 0),
            "avg_virality_posted": (
                round(float(r["avg_virality_posted"]), 2)
                if r.get("avg_virality_posted") is not None
                else None
            ),
            "avg_virality_skipped": (
                round(float(r["avg_virality_skipped"]), 2)
                if r.get("avg_virality_skipped") is not None
                else None
            ),
            "omit_rate": round(float(r.get("omit_rate") or 0), 2),
            "last_skip_h_ago": (
                round(float(r["last_skip_h_ago"]), 1)
                if r.get("last_skip_h_ago") is not None
                else None
            ),
            "sample_skip_reasons": r.get("sample_skip_reasons") or [],
        }
        for r in rows
    ]

    json.dump(out, sys.stdout)
    print("", file=sys.stdout)


if __name__ == "__main__":
    main()
