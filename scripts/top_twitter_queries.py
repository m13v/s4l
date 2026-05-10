#!/usr/bin/env python3
"""
top_twitter_queries.py

Returns top-performing historical search queries scored by a composite of
clicks, likes, views, posts produced, AND raw supply (tweets_found per
attempt). Used as STYLE inspiration for the LLM that drafts new queries.

Per-query fields (so the model can see the full conversion funnel and
decide on min_faves tier intelligently, not just on flat rules):

    query              — the actual X search string (with min_faves:N etc.)
    project            — project the query was drafted for (matched_project)
    tweets_found_avg   — supply: avg tweets X returned per attempt
    posts              — quality: how many of those tweets we replied to
    views_total        — surface engagement on our replies (sum)
    likes_total        — sum
    clicks_total       — link clicks attributed to our replies (real_clicks)
    composite_score    — clicks*100 + likes + views*0.001  (clicks dominate)

Note on the join shape: posts.search_topic is NOT populated for twitter
(reddit-only). The reliable chain is:
    twitter_candidates (search_topic = literal X query, post_id, matched_project)
        -> posts via post_id (views, upvotes)
        -> post_links via post_id (real_clicks)
        -> twitter_search_attempts via query == search_topic (tweets_found)

Usage:

    python3 scripts/top_twitter_queries.py [--limit 20] [--window-days 14] [--project NAME]

The optional --project filter is what enables per-project surfacing in the
Phase 1 scanner prompt: each cycle, the scanner can fetch the top queries
specifically for the project it's currently drafting for.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--window-days", type=int, default=14)
    p.add_argument("--project", default=None,
                   help="If set, only return top queries for this project (matched_project).")
    args = p.parse_args()

    # Param order must match SQL placeholder order:
    #   1) cand_agg interval   (%s for window_days)
    #   2) optional where_proj (%s for project)        ← only when --project set
    #   3) supply_agg interval (%s for window_days)
    #   4) LIMIT (%s)
    where_proj = ""
    params = [str(args.window_days)]
    if args.project:
        where_proj = "AND c.matched_project = %s"
        params.append(args.project)
    params.append(str(args.window_days))
    params.append(args.limit)

    sql = f"""
        WITH cand_agg AS (
            -- Aggregate candidates → posts → links by literal query string.
            SELECT c.search_topic AS query,
                   c.matched_project AS project_name,
                   COUNT(DISTINCT c.post_id) FILTER (WHERE c.post_id IS NOT NULL) AS posts,
                   COALESCE(SUM(p.views), 0) AS views_total,
                   COALESCE(SUM(p.upvotes), 0) AS likes_total,
                   COALESCE(SUM(pl.real_clicks), 0) AS clicks_total
            FROM twitter_candidates c
            LEFT JOIN posts p ON p.id = c.post_id
            LEFT JOIN post_links pl ON pl.post_id = c.post_id
            WHERE c.discovered_at > NOW() - (%s || ' days')::interval
              AND c.search_topic IS NOT NULL
              AND c.search_topic <> ''
              AND c.status = 'posted'
              {where_proj}
            GROUP BY c.search_topic, c.matched_project
        ),
        supply_agg AS (
            SELECT query,
                   project_name,
                   COUNT(*) AS attempts,
                   ROUND(AVG(tweets_found)::numeric, 2) AS tweets_found_avg
            FROM twitter_search_attempts
            WHERE ran_at > NOW() - (%s || ' days')::interval
            GROUP BY query, project_name
        )
        SELECT ca.query,
               ca.project_name,
               ca.posts,
               ca.views_total,
               ca.likes_total,
               ca.clicks_total,
               COALESCE(sa.tweets_found_avg, 0) AS tweets_found_avg,
               -- composite: clicks dominate (×100), likes mid, views faint
               (ca.clicks_total * 100
                + ca.likes_total
                + ca.views_total * 0.001) AS composite_score
        FROM cand_agg ca
        LEFT JOIN supply_agg sa
               ON sa.query = ca.query
              AND sa.project_name = ca.project_name
        ORDER BY composite_score DESC, ca.posts DESC
        LIMIT %s
    """

    conn = dbmod.get_conn()
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    out = [
        {
            "query": r[0],
            "project": r[1],
            "posts": r[2],
            "views_total": r[3],
            "likes_total": r[4],
            "clicks_total": r[5],
            "tweets_found_avg": float(r[6] or 0),
            "composite_score": round(float(r[7] or 0), 2),
        }
        for r in rows
    ]
    json.dump(out, sys.stdout)
    print("", file=sys.stdout)


if __name__ == "__main__":
    main()
