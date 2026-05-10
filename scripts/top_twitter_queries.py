#!/usr/bin/env python3
"""
top_twitter_queries.py

Returns top-performing historical search queries scored by a composite of
clicks, likes, views, posts produced, AND raw supply (tweets_found per
attempt). Used as STYLE inspiration for the LLM that drafts new queries.

Per-query fields (so the model can see the full conversion funnel and
decide on min_faves tier intelligently, not just on flat rules):

    query              — the actual X search string (with min_faves:N etc.)
    project            — project the query was drafted for
    tweets_found_avg   — supply: avg tweets X returned per attempt
    posts              — quality: how many of those tweets we replied to
    views_total        — surface engagement on our replies (sum)
    likes_total        — sum
    clicks_total       — link clicks attributed to our replies (real_clicks)
    composite_score    — clicks*100 + likes + views*0.001  (clicks dominate)

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
                   help="If set, only return top queries for this project_name.")
    args = p.parse_args()

    # The query joins three sources:
    #   posts (replies we shipped)             ← project, search_topic, views, upvotes
    #   post_links (tracked CTAs in those replies) ← real_clicks
    #   twitter_search_attempts (X search supply)  ← tweets_found
    #
    # We aggregate at search_topic granularity (the literal query string,
    # which encodes the min_faves operator). This way two near-identical
    # queries with different min_faves tiers show up as separate rows so
    # the model sees "min_faves:50 found 6 tweets/attempt, min_faves:20
    # for the same project found 0".
    where_proj = ""
    params = [str(args.window_days), str(args.window_days)]
    if args.project:
        where_proj = "AND p.project_name = %s"
        params.append(args.project)
    params.append(args.limit)

    sql = f"""
        WITH post_agg AS (
            SELECT p.search_topic,
                   p.project_name,
                   COUNT(DISTINCT p.id) AS posts,
                   COALESCE(SUM(p.views), 0) AS views_total,
                   COALESCE(SUM(p.upvotes), 0) AS likes_total,
                   COALESCE(SUM(pl.real_clicks), 0) AS clicks_total
            FROM posts p
            LEFT JOIN post_links pl ON pl.post_id = p.id
            WHERE p.platform = 'twitter'
              AND p.search_topic IS NOT NULL
              AND p.search_topic <> ''
              AND p.posted_at > NOW() - (%s || ' days')::interval
              {where_proj}
            GROUP BY p.search_topic, p.project_name
        ),
        supply_agg AS (
            SELECT query AS search_topic,
                   project_name,
                   COUNT(*) AS attempts,
                   ROUND(AVG(tweets_found)::numeric, 2) AS tweets_found_avg
            FROM twitter_search_attempts
            WHERE ran_at > NOW() - (%s || ' days')::interval
            GROUP BY query, project_name
        )
        SELECT pa.search_topic,
               pa.project_name,
               pa.posts,
               pa.views_total,
               pa.likes_total,
               pa.clicks_total,
               COALESCE(sa.tweets_found_avg, 0) AS tweets_found_avg,
               -- composite: clicks dominate (×100), likes mid, views faint
               (pa.clicks_total * 100
                + pa.likes_total
                + pa.views_total * 0.001) AS composite_score
        FROM post_agg pa
        LEFT JOIN supply_agg sa
               ON sa.search_topic = pa.search_topic
              AND sa.project_name = pa.project_name
        ORDER BY composite_score DESC, pa.posts DESC
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
