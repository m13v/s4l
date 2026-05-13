#!/usr/bin/env python3
"""
top_omitted_reddit_topics.py

Returns recent Reddit search_topic seeds whose threads consistently survived
the ripen gate (numerical engagement check) but were then OMITTED by
post_reddit.py's draft-time SELECTION GATE (build_draft_prompt's bridge test).

Why this matters:
- top_search_topics.py = positive signal (seed -> posted -> engagement)
- top_dud_reddit_queries.py = "no results returned" signal (search dud)
- THIS = "results returned, ripen survived, draft gate killed them" signal
  i.e. the seed is producing alive-but-unfit threads. Category-level
  mismatch, the LLM should drop or rephrase that seed.

Output: JSON list (so build_discover_prompt can paste it directly), sorted
by most-omitted first:

    [{"search_topic": "...", "project": "...",
      "draft_omits": N, "ripen_survivors": M, "posted": P,
      "omit_rate": 0.NN, "last_omit_h_ago": F.F,
      "sample_subreddits": ["r/foo", "r/bar", ...]}]

Usage:
    python3 scripts/top_omitted_reddit_topics.py [--project NAME] [--limit 15] [--window-hours 168]

Routed through GET /api/v1/reddit-candidates/omitted-topics on the
website. omit_rate and last_omit_h_ago are computed server-side; the
shape returned by the route already matches the legacy CLI contract,
so callers (build_discover_prompt et al.) don't need to change.
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
                   help="Filter to a single project (matches matched_project).")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--window-hours", type=int, default=168,
                   help="Look back this many hours (default 7d).")
    p.add_argument("--min-omits", type=int, default=1,
                   help="Suppress seeds with fewer than this many draft omits "
                        "in the window (default 1).")
    args = p.parse_args()

    query = {
        "limit": args.limit,
        "window_hours": args.window_hours,
        "min_omits": args.min_omits,
    }
    if args.project:
        query["project"] = args.project

    resp = api_get("/api/v1/reddit-candidates/omitted-topics", query=query)
    data = (resp or {}).get("data") or {}
    rows = data.get("rows") or []

    # Route already returns the legacy shape. Mirror keys explicitly so
    # downstream callers see a stable contract even if the route adds
    # extra fields later.
    out = [
        {
            "search_topic": r.get("search_topic"),
            "project": r.get("project") or "",
            "draft_omits": int(r.get("draft_omits") or 0),
            "ripen_survivors": int(r.get("ripen_survivors") or 0),
            "posted": int(r.get("posted") or 0),
            "omit_rate": round(float(r.get("omit_rate") or 0), 2),
            "last_omit_h_ago": (
                round(float(r["last_omit_h_ago"]), 1)
                if r.get("last_omit_h_ago") is not None
                else None
            ),
            "sample_subreddits": r.get("sample_subreddits") or [],
        }
        for r in rows
    ]

    json.dump(out, sys.stdout)
    print("", file=sys.stdout)


if __name__ == "__main__":
    main()
