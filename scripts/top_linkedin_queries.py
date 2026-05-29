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
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod
from linkedin_search_topic_schema import ensure as ensure_search_topic_schema


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--window-days", type=int, default=30)
    p.add_argument("--project", default=None)
    p.add_argument("--search-topic", default=None)
    args = p.parse_args()

    conn = dbmod.get_conn()
    ensure_search_topic_schema(conn)
    filters = [
        "search_query IS NOT NULL",
        "search_query <> ''",
        "discovered_at > NOW() - (%s || ' days')::interval",
    ]
    params = [str(args.window_days)]
    if args.project:
        filters.append("LOWER(COALESCE(matched_project, '')) = LOWER(%s)")
        params.append(args.project)
    if args.search_topic:
        filters.append("LOWER(COALESCE(search_topic, '')) = LOWER(%s)")
        params.append(args.search_topic)
    where = " AND ".join(filters)
    params.append(args.limit)
    rows = conn.execute(
        f"""
        SELECT search_query,
               COALESCE(matched_project, '') AS project,
               COALESCE(search_topic, '') AS search_topic,
               COUNT(*) FILTER (WHERE status='posted') AS posts,
               AVG(velocity_score) AS avg_velocity,
               AVG(serp_quality_score) AS avg_serp
        FROM linkedin_candidates
        WHERE {where}
        GROUP BY search_query, COALESCE(matched_project, ''), COALESCE(search_topic, '')
        HAVING COUNT(*) FILTER (WHERE status='posted') > 0
        ORDER BY posts DESC, avg_velocity DESC
        LIMIT %s
        """,
        params,
    ).fetchall()
    conn.close()

    out = [
        {
            "query": r[0],
            "project": r[1],
            "search_topic": r[2],
            "posts": r[3],
            "avg_velocity": round(float(r[4] or 0), 2),
            "avg_serp_quality": round(float(r[5] or 0), 2) if r[5] is not None else None,
        }
        for r in rows
    ]
    json.dump(out, sys.stdout)
    print("", file=sys.stdout)


if __name__ == "__main__":
    main()
