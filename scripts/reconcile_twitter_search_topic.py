#!/usr/bin/env python3
"""
reconcile_twitter_search_topic.py

Fixes query-level attribution for Twitter candidates.

PROBLEM
-------
twitter_candidates.search_topic is meant to hold the LITERAL X search string
that found the tweet, so it joins 1:1 against twitter_search_attempts.query
(the supply telemetry: tweets_found per query). In practice the Phase 1
scanner LLM often writes a short conceptual label into a tweet's
`search_topic` ("vibe coding", "MCP server", "voice AI for restaurants")
while writing the full literal query into the separate `queries_used` array.
The two diverge, so top_twitter_queries.py's
`twitter_search_attempts.query = twitter_candidates.search_topic` join misses
~half the rows and `tweets_found_avg` reads 0 for them.

FIX
---
Every candidate row carries `batch_id` + `matched_project`, and
twitter_search_attempts logs the canonical `query` per (batch_id,
project_name). There is exactly one drafted query per project per cycle, so
(batch_id, project) -> query is a deterministic lookup. This script rewrites
twitter_candidates.search_topic to that canonical query wherever they differ.

It is idempotent and safe to run repeatedly: rows already correct are skipped
(IS DISTINCT FROM), and (batch_id, project) pairs that map to more than one
distinct query are left untouched (ambiguous, reported separately).

Usage:
    python3 scripts/reconcile_twitter_search_topic.py                 # dry run
    python3 scripts/reconcile_twitter_search_topic.py --apply          # write
    python3 scripts/reconcile_twitter_search_topic.py --apply --window-days 90
    python3 scripts/reconcile_twitter_search_topic.py --quiet --apply   # cron
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod

SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="Write the changes. Without this flag, dry-run only.")
    p.add_argument("--window-days", type=int, default=90,
                   help="Only reconcile candidates discovered within this window.")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress per-project breakdown (for cron).")
    args = p.parse_args()

    conn = dbmod.get_conn()

    # Canonical (batch_id, project) -> query. nq>1 means the pair was logged
    # with more than one distinct query (rare batch_id collision); skip those.
    fixable = conn.execute(
        """
        WITH canon AS (
            SELECT batch_id, project_name,
                   MIN(query)            AS query,
                   COUNT(DISTINCT query) AS nq
            FROM twitter_search_attempts
            WHERE batch_id IS NOT NULL AND project_name IS NOT NULL
            GROUP BY batch_id, project_name
        )
        SELECT c.id, c.search_topic AS old_topic, canon.query AS new_topic,
               c.matched_project
        FROM twitter_candidates c
        JOIN canon ON canon.batch_id = c.batch_id
                  AND canon.project_name = c.matched_project
        WHERE canon.nq = 1
          AND c.search_topic IS DISTINCT FROM canon.query
          AND c.discovered_at > NOW() - (%s || ' days')::interval
        """,
        [str(args.window_days)],
    ).fetchall()

    ambiguous = conn.execute(
        """
        WITH canon AS (
            SELECT batch_id, project_name, COUNT(DISTINCT query) AS nq
            FROM twitter_search_attempts
            WHERE batch_id IS NOT NULL AND project_name IS NOT NULL
            GROUP BY batch_id, project_name
        )
        SELECT COUNT(*)
        FROM twitter_candidates c
        JOIN canon ON canon.batch_id = c.batch_id
                  AND canon.project_name = c.matched_project
        WHERE canon.nq > 1
          AND c.discovered_at > NOW() - (%s || ' days')::interval
        """,
        [str(args.window_days)],
    ).fetchone()[0]

    n = len(fixable)
    if not args.quiet:
        print(f"reconcile_twitter_search_topic: window={args.window_days}d")
        print(f"  candidates to reconcile : {n}")
        print(f"  ambiguous (skipped)     : {ambiguous}")
        by_proj = {}
        for _id, _old, _new, proj in fixable:
            by_proj[proj] = by_proj.get(proj, 0) + 1
        for proj, cnt in sorted(by_proj.items(), key=lambda kv: -kv[1]):
            print(f"    {proj:<26} {cnt}")

    if n == 0:
        print("reconcile_twitter_search_topic: nothing to do.")
        conn.close()
        return 0

    if not args.apply:
        print("reconcile_twitter_search_topic: DRY RUN, no rows written. "
              "Re-run with --apply to commit.")
        # Show a few example before/after pairs.
        for _id, old, new, proj in fixable[:5]:
            print(f"  [{proj}] id={_id}")
            print(f"      old: {old!r}")
            print(f"      new: {new!r}")
        conn.close()
        return 0

    # Snapshot old values so the change is reversible.
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap_path = os.path.join(SNAPSHOT_DIR, f"search_topic_snapshot_{stamp}.json")
    with open(snap_path, "w") as f:
        json.dump(
            [{"id": r[0], "old_search_topic": r[1]} for r in fixable],
            f, indent=2,
        )

    updated = 0
    for _id, _old, new, _proj in fixable:
        conn.execute(
            "UPDATE twitter_candidates SET search_topic = %s WHERE id = %s",
            [new, _id],
        )
        updated += 1
    conn.commit()
    conn.close()
    print(f"reconcile_twitter_search_topic: updated {updated} rows. "
          f"Snapshot of old values: {snap_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
