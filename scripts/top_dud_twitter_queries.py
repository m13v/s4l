#!/usr/bin/env python3
"""
top_dud_twitter_queries.py

Returns recent Twitter search queries that produced ZERO tweets so the
LLM scanner can be told "do not redraft these phrasings — they were flat
in the last N hours". Counterpart to top_twitter_queries.py (positive
signal): this is the negative-signal feed.

    python3 scripts/top_dud_twitter_queries.py [--limit 30] [--window-hours 48]

Output: JSON list of
    {"query": ..., "project": ..., "min_faves": N | null,
     "attempts": N, "last_ran_h_ago": F}
sorted by most-attempted dud first (so the most-wasteful repeats surface
at the top of the prompt anti-list).

The min_faves field is parsed from the query string (X operator
`min_faves:N`). Surfacing it lets the model correlate "every studyly dud
last 48h used min_faves:20" → drop the floor for that project.

Source: twitter_search_attempts (one row per query per cycle, written by
run-twitter-cycle.sh after the Phase 1 scan parses queries_used).
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod

MIN_FAVES_RE = re.compile(r"min_faves:(\d+)", re.IGNORECASE)


def extract_min_faves(query: str):
    """Return the integer N from `min_faves:N` if present, else None."""
    if not query:
        return None
    m = MIN_FAVES_RE.search(query)
    return int(m.group(1)) if m else None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--window-hours", type=int, default=48,
                   help="Look back this many hours for dud queries.")
    args = p.parse_args()

    conn = dbmod.get_conn()
    rows = conn.execute(
        """
        SELECT query,
               COALESCE(project_name, '') AS project,
               COUNT(*) AS attempts,
               EXTRACT(EPOCH FROM (NOW() - MAX(ran_at)))/3600.0 AS last_ran_h_ago
        FROM twitter_search_attempts
        WHERE tweets_found = 0
          AND ran_at > NOW() - (%s || ' hours')::interval
        GROUP BY query, COALESCE(project_name, '')
        ORDER BY attempts DESC, MAX(ran_at) DESC
        LIMIT %s
        """,
        [str(args.window_hours), args.limit],
    ).fetchall()
    conn.close()

    out = [
        {
            "query": r[0],
            "project": r[1],
            "min_faves": extract_min_faves(r[0]),
            "attempts": r[2],
            "last_ran_h_ago": round(float(r[3] or 0), 1),
        }
        for r in rows
    ]
    json.dump(out, sys.stdout)
    print("", file=sys.stdout)


if __name__ == "__main__":
    main()
