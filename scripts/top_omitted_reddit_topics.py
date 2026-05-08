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
  mismatch — the LLM should drop or rephrase that seed.

Output: JSON list (so build_discover_prompt can paste it directly), sorted
by most-omitted first:

    [{"search_topic": "...", "project": "...",
      "draft_omits": N, "ripen_survivors": M, "posted": P,
      "omit_rate": 0.NN, "last_omit_h_ago": F.F,
      "sample_subreddits": ["r/foo", "r/bar", ...]}]

Usage:
    python3 scripts/top_omitted_reddit_topics.py [--project NAME] [--limit 15] [--window-hours 168]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod


SQL = """
WITH base AS (
  SELECT search_topic,
         matched_project,
         subreddit,
         status,
         last_failure_reason,
         draft_text,
         last_attempt_at
  FROM reddit_candidates
  WHERE search_topic IS NOT NULL
    AND search_topic <> ''
    AND COALESCE(last_attempt_at, discovered_at) > NOW() - (%s || ' hours')::interval
    {project_filter}
)
SELECT search_topic,
       matched_project,
       COUNT(*) FILTER (WHERE last_failure_reason = 'draft_gate_omit') AS draft_omits,
       (COUNT(*) FILTER (WHERE draft_text IS NOT NULL)
        + COUNT(*) FILTER (WHERE last_failure_reason = 'draft_gate_omit'))
         AS ripen_survivors,
       COUNT(*) FILTER (WHERE status = 'posted') AS posted,
       MAX(last_attempt_at) FILTER (WHERE last_failure_reason = 'draft_gate_omit')
         AS last_omit_at,
       (
         SELECT json_agg(DISTINCT s)
         FROM (
           SELECT subreddit AS s
           FROM reddit_candidates rc2
           WHERE rc2.search_topic = base.search_topic
             AND rc2.matched_project = base.matched_project
             AND rc2.last_failure_reason = 'draft_gate_omit'
             AND rc2.subreddit IS NOT NULL
             AND rc2.subreddit <> ''
             AND COALESCE(rc2.last_attempt_at, rc2.discovered_at)
                 > NOW() - (%s || ' hours')::interval
           LIMIT 8
         ) sub
       ) AS sample_subreddits
FROM base
GROUP BY search_topic, matched_project
HAVING COUNT(*) FILTER (WHERE last_failure_reason = 'draft_gate_omit') >= %s
ORDER BY draft_omits DESC, ripen_survivors DESC
LIMIT %s
"""


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

    dbmod.load_env()
    conn = dbmod.get_conn()

    if args.project:
        sql = SQL.format(project_filter="AND matched_project = %s")
        params = [str(args.window_hours), args.project,
                  str(args.window_hours), args.min_omits, args.limit]
    else:
        sql = SQL.format(project_filter="")
        params = [str(args.window_hours),
                  str(args.window_hours), args.min_omits, args.limit]

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    out = []
    for r in rows:
        search_topic, matched_project, draft_omits, ripen_survivors, posted, \
            last_omit_at, sample_subreddits = r
        denom = max(int(ripen_survivors or 0), 1)
        omit_rate = round(int(draft_omits or 0) / denom, 2)
        last_omit_h_ago = None
        if last_omit_at is not None:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            try:
                delta = now - last_omit_at
                last_omit_h_ago = round(delta.total_seconds() / 3600.0, 1)
            except Exception:
                last_omit_h_ago = None
        out.append({
            "search_topic": search_topic,
            "project": matched_project or "",
            "draft_omits": int(draft_omits or 0),
            "ripen_survivors": int(ripen_survivors or 0),
            "posted": int(posted or 0),
            "omit_rate": omit_rate,
            "last_omit_h_ago": last_omit_h_ago,
            "sample_subreddits": sample_subreddits or [],
        })

    json.dump(out, sys.stdout)
    print("", file=sys.stdout)


if __name__ == "__main__":
    main()
