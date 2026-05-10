#!/usr/bin/env python3
"""
twitter_supply_signal.py

Per-project supply table: at each `min_faves:N` tier, what's the median
number of tweets X actually returned for queries we ran for that project?

This is the answer to the question the Phase 1 scanner has been guessing
at since the cycle was written: "what min_faves should I use for this
project?". Today the prompt says a flat "broad=50, narrow=20" rule, which
works for tech-Twitter (mk0r, claude-meter, fazm) but starves student-
Twitter (studyly), where even niche audience tweets rarely clear 20 likes.

Output: JSON list of
    {"project": "<name>", "tiers": [{"min_faves": N, "attempts": N,
                                     "median_tweets_found": N,
                                     "zero_result_pct": 0-100}, ...]}
sorted by project. Within each project, tiers ordered ascending min_faves
so the model can read "as I raise the floor, supply collapses; pick the
lowest min_faves where supply is still ≥3".

Usage:

    python3 scripts/twitter_supply_signal.py [--window-days 14] [--project NAME]
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod

MIN_FAVES_RE = re.compile(r"min_faves:(\d+)", re.IGNORECASE)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--window-days", type=int, default=14)
    p.add_argument("--project", default=None,
                   help="If set, only return supply table for this project.")
    args = p.parse_args()

    where_proj = ""
    params = [str(args.window_days)]
    if args.project:
        where_proj = "AND project_name = %s"
        params.append(args.project)

    sql = f"""
        SELECT query,
               COALESCE(project_name, '(none)') AS project,
               tweets_found
        FROM twitter_search_attempts
        WHERE ran_at > NOW() - (%s || ' days')::interval
          {where_proj}
    """
    conn = dbmod.get_conn()
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    # Bucket by (project, min_faves_tier).
    # Treat queries without min_faves as tier 0 (no floor).
    buckets = defaultdict(list)  # (project, tier_int) -> [tweets_found, ...]
    for query, project, found in rows:
        m = MIN_FAVES_RE.search(query or "")
        tier = int(m.group(1)) if m else 0
        buckets[(project, tier)].append(int(found or 0))

    # Reshape: project -> [tier_dict, ...] sorted ascending min_faves.
    by_project = defaultdict(list)
    for (project, tier), found_list in buckets.items():
        if not found_list:
            continue
        attempts = len(found_list)
        med = median(found_list)
        zero_pct = round(
            sum(1 for x in found_list if x == 0) / attempts * 100, 1
        )
        by_project[project].append(
            {
                "min_faves": tier,
                "attempts": attempts,
                "median_tweets_found": int(med),
                "zero_result_pct": zero_pct,
            }
        )

    out = []
    for project in sorted(by_project.keys()):
        tiers = sorted(by_project[project], key=lambda t: t["min_faves"])
        out.append({"project": project, "tiers": tiers})

    json.dump(out, sys.stdout)
    print("", file=sys.stdout)


if __name__ == "__main__":
    main()
