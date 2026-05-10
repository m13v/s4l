#!/usr/bin/env python3
"""
top_twitter_queries.py

Returns top-performing historical search queries scored by a composite of
clicks, likes, views, posts produced, AND raw supply (tweets_found per
attempt). Used as STYLE inspiration for the LLM that drafts new queries.

Per-query fields, structured so the model can see the FULL conversion
funnel AND distinguish "queries that find threads worth posting to" from
"queries that find viral noise we keep skipping":

    query                  — the literal X search string (with min_faves:N etc.)
    project                — project the query was drafted for (matched_project)
    tweets_found_avg       — SUPPLY: avg tweets X returned per attempt
    posts                  — QUALITY: distinct candidates we replied to (status='posted')
    posted_n               — count of candidates with status='posted'
    skipped_n              — count of candidates with status IN ('skipped','expired')
    avg_virality_posted    — avg source-thread virality_score for the threads
                             we DID post to. High = the query surfaces threads
                             that are both viral AND on-topic enough to engage.
    avg_virality_skipped   — avg source-thread virality_score for the threads
                             we DID NOT post to (skipped by Claude or aged out).
                             High avg_virality_skipped + low posts = the query
                             keeps finding viral NOISE (off-topic loud threads),
                             so the keyword cluster is mismatched even though
                             the engagement floor is fine.
    views_total            — sum of views on OUR replies (downstream surface)
    likes_total            — sum of likes on OUR replies
    clicks_total           — sum of real_clicks attributed to our replies (CTA tracking)
    composite_score        — clicks*100 + likes + views*0.001  (clicks dominate)

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

Note on the join shape: posts.search_topic is NOT populated for twitter
(reddit-only). The reliable chain is:
    twitter_candidates (search_topic = literal X query, post_id, matched_project,
                        virality_score, status)
        -> posts via post_id (views, upvotes)            [posted only]
        -> post_links via post_id (real_clicks)          [posted only]
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

    # cand_agg drops the old "status='posted'" hard filter and uses FILTER
    # clauses instead, so we can compute posted-only engagement (views,
    # likes, clicks, posts count) AND status-split virality averages
    # (posted vs skipped/expired) in a single pass over the candidate
    # rows. The HAVING posts > 0 below keeps the result set focused on
    # queries that actually produced replies — queries that never posted
    # are surfaced via the dud-queries feed instead.
    sql = f"""
        WITH cand_agg AS (
            SELECT c.search_topic AS query,
                   c.matched_project AS project_name,
                   -- posted engagement (downstream metrics — only meaningful for posted rows)
                   COUNT(DISTINCT c.post_id) FILTER (WHERE c.status='posted' AND c.post_id IS NOT NULL) AS posts,
                   COALESCE(SUM(p.views)         FILTER (WHERE c.status='posted'), 0) AS views_total,
                   COALESCE(SUM(p.upvotes)       FILTER (WHERE c.status='posted'), 0) AS likes_total,
                   COALESCE(SUM(pl.real_clicks)  FILTER (WHERE c.status='posted'), 0) AS clicks_total,
                   -- sample sizes per status group so the model can weight the averages
                   COUNT(*) FILTER (WHERE c.status='posted')                          AS posted_n,
                   COUNT(*) FILTER (WHERE c.status IN ('skipped', 'expired'))         AS skipped_n,
                   -- source-thread virality, split by what we did with the candidate.
                   -- Both AVGs are over twitter_candidates.virality_score (set at discovery).
                   AVG(c.virality_score) FILTER (WHERE c.status='posted')             AS avg_virality_posted,
                   AVG(c.virality_score) FILTER (WHERE c.status IN ('skipped', 'expired')) AS avg_virality_skipped
            FROM twitter_candidates c
            LEFT JOIN posts      p  ON p.id  = c.post_id
            LEFT JOIN post_links pl ON pl.post_id = c.post_id
            WHERE c.discovered_at > NOW() - (%s || ' days')::interval
              AND c.search_topic IS NOT NULL
              AND c.search_topic <> ''
              {where_proj}
            GROUP BY c.search_topic, c.matched_project
            HAVING COUNT(*) FILTER (WHERE c.status='posted') > 0
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
               ca.posted_n,
               ca.skipped_n,
               ca.avg_virality_posted,
               ca.avg_virality_skipped,
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
            "posted_n": r[3],
            "skipped_n": r[4],
            "avg_virality_posted": round(float(r[5] or 0), 2),
            "avg_virality_skipped": round(float(r[6] or 0), 2),
            "views_total": r[7],
            "likes_total": r[8],
            "clicks_total": r[9],
            "tweets_found_avg": float(r[10] or 0),
            "composite_score": round(float(r[11] or 0), 2),
        }
        for r in rows
    ]
    json.dump(out, sys.stdout)
    print("", file=sys.stdout)


if __name__ == "__main__":
    main()
