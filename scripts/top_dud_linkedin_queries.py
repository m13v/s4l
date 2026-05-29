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
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--window-days", type=int, default=7,
                   help="Look back this many days for dud queries.")
    p.add_argument("--low-serp-threshold", type=float, default=4.0,
                   help="serp_quality_score below this counts as a dud.")
    p.add_argument("--project", default=None)
    p.add_argument("--search-topic", default=None)
    args = p.parse_args()

    conn = dbmod.get_conn()
    ensure_search_topic_schema(conn)
    filters = [
        "ran_at > NOW() - (%s || ' days')::interval",
        """
          (
            candidates_found = 0
            OR (serp_quality_score IS NOT NULL AND serp_quality_score < %s)
          )
        """,
    ]
    params = [str(args.window_days), args.low_serp_threshold]
    if args.project:
        filters.append("LOWER(COALESCE(project_name, '')) = LOWER(%s)")
        params.append(args.project)
    if args.search_topic:
        filters.append("LOWER(COALESCE(search_topic, '')) = LOWER(%s)")
        params.append(args.search_topic)
    where = " AND ".join(filters)
    params = [args.low_serp_threshold] + params + [args.limit]
    rows = conn.execute(
        f"""
        SELECT query,
               COALESCE(project_name, '') AS project,
               COALESCE(search_topic, '') AS search_topic,
               COUNT(*) AS attempts,
               EXTRACT(EPOCH FROM (NOW() - MAX(ran_at)))/3600.0 AS last_ran_h_ago,
               CASE
                   WHEN BOOL_AND(candidates_found = 0) THEN 'zero_results'
                   WHEN AVG(serp_quality_score) < %s THEN 'low_serp_quality'
                   ELSE 'mixed_dud'
               END AS reason
        FROM linkedin_search_attempts
        WHERE {where}
        GROUP BY query, COALESCE(project_name, ''), COALESCE(search_topic, '')
        ORDER BY attempts DESC, MAX(ran_at) DESC
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
            "attempts": r[3],
            "last_ran_h_ago": round(float(r[4] or 0), 1),
            "reason": r[5],
        }
        for r in rows
    ]
    json.dump(out, sys.stdout)
    print("", file=sys.stdout)


if __name__ == "__main__":
    main()
