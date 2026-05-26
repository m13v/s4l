#!/usr/bin/env python3
"""
top_twitter_queries.py

Returns top-performing historical search queries scored by a composite of
clicks, likes, views, posts produced, AND raw supply (tweets_found per
attempt). Used as STYLE inspiration for the LLM that drafts new queries.

Per-query fields, structured so the model can see the FULL conversion
funnel AND distinguish "queries that find threads worth posting to" from
"queries that find viral noise we keep skipping":

    query                  , the literal X search string (with min_faves:N etc.)
    project                , project the query was drafted for (matched_project)
    tweets_found_avg       , SUPPLY: avg tweets X returned per attempt
    posted_n               , count of candidates with status='posted'
    skipped_n              , count of candidates with status IN ('skipped','expired')
    post_rate              , posted_n / (posted_n + skipped_n); draft-gate acceptance ratio
    avg_virality_posted    , avg source-thread virality_score for posted candidates
    avg_virality_skipped   , avg source-thread virality_score for skipped/expired
    views_total            , sum of views on OUR replies (downstream surface)
    likes_total            , sum of likes on OUR replies
    clicks_total           , sum of real_clicks attributed to our replies (CTA tracking)
    composite_score        , clicks*100 + likes + views*0.001  (clicks dominate)

The two virality fields together let the model diagnose query failure
modes that pure conversion data misses:
  - high avg_virality_posted + many posts → keep / mimic this query style
  - high avg_virality_skipped + few posts → reword: query is on-rank but
    semantically off-topic (e.g. studyly catching unrelated viral student
    drama because keywords overlap with study-related slang)
  - low avg_virality_skipped + few posts → query is just dead supply,
    drop the keyword cluster entirely

Source-thread virality_score is computed by score_twitter_candidates.py
(engagement velocity + retweet ratio + reply weight + author followers,
with 6h half-life decay). It's set on EVERY candidate at discovery time
regardless of posted/skipped/expired status, which is why we can split
the average by status group.

Usage:

    python3 scripts/top_twitter_queries.py [--limit 20] [--window-days 14] [--project NAME]

The optional --project filter is what enables per-project surfacing in the
Phase 1 scanner prompt: each cycle, the scanner can fetch the top queries
specifically for the project it's currently drafting for.

Migrated 2026-05-18: reads now go through /api/v1/twitter-search-attempts/
top-queries via scripts/http_api.py instead of a direct psycopg2 query.
The SQL composite-score join (cand_agg + supply_agg, click_total tiebreaker)
runs server-side; this script just shapes the response into the legacy JSON.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--window-days", type=int, default=14)
    p.add_argument("--project", default=None,
                   help="If set, only return top queries for this project (matched_project).")
    args = p.parse_args()

    query = {
        "limit": args.limit,
        "window_days": args.window_days,
    }
    if args.project:
        query["project"] = args.project

    resp = api_get("/api/v1/twitter-search-attempts/top-queries", query=query)
    rows = (resp.get("data") or {}).get("rows") or []

    # Pass-through shape, but float-coerce so the legacy JSON consumers
    # (run-twitter-cycle.sh's Phase 1 prompt) see the same types as before.
    # Derived field: post_rate = posted_n / (posted_n + skipped_n), the draft-gate
    # acceptance ratio. Lets the model see whether a query that LOOKS productive
    # by raw posts count actually clears the skip filter, or whether it surfaces
    # 100 candidates and we reject 95 of them. Safe-divide on empty denominator.
    # Dropped: 'posts' field (was identical to 'posted_n' in every observed row).
    def _post_rate(posted: int, skipped: int) -> float:
        denom = posted + skipped
        if denom <= 0:
            return 0.0
        return round(posted / denom, 3)

    out = []
    for r in rows:
        posted_n = int(r.get("posted_n") or 0)
        skipped_n = int(r.get("skipped_n") or 0)
        out.append({
            "query": r.get("query"),
            "project": r.get("project") or "",
            "posted_n": posted_n,
            "skipped_n": skipped_n,
            "post_rate": _post_rate(posted_n, skipped_n),
            "avg_virality_posted": round(float(r.get("avg_virality_posted") or 0), 2),
            "avg_virality_skipped": round(float(r.get("avg_virality_skipped") or 0), 2),
            "views_total": int(r.get("views_total") or 0),
            "likes_total": int(r.get("likes_total") or 0),
            "clicks_total": int(r.get("clicks_total") or 0),
            "tweets_found_avg": float(r.get("tweets_found_avg") or 0),
            "composite_score": round(float(r.get("composite_score") or 0), 2),
        })
    json.dump(out, sys.stdout)
    print("", file=sys.stdout)


if __name__ == "__main__":
    main()
