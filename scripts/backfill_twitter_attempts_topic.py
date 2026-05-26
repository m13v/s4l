#!/usr/bin/env python3
"""backfill_twitter_attempts_topic.py

Periodic UPDATE that fills `twitter_search_attempts.search_topic` from the
adjacent `twitter_candidates` rows once a scoring cycle finishes writing them.

Why this lives outside the cycle: `score_twitter_candidates.py` and the parent
`skill/run-twitter-cycle.sh` are both `chflags uchg` locked, and the canonical
SCAN_SCHEMA in the shell does not yet carry `search_topic` on each entry of
`queries_used` (so `log_twitter_search_attempts.py` cannot stamp it at INSERT
time). Until those locked files are extended, we backfill from the candidate
side, which DOES know the topic (set by `pick_search_topic.py` -> stamped onto
twitter_candidates.search_topic + search_attempt_id).

Two passes, both safe to rerun:

  A) Direct join via search_attempt_id (covers non-dud attempts that produced
     at least one candidate).

  B) Fanout via (batch_id, project_name) -> covers dud attempts whose siblings
     in the same cycle DID return candidates and therefore know the topic.
     Skips ambiguous batches (more than one distinct topic) to avoid noise.

Fully-dud cycles (every query for a project in the batch was a dud) stay NULL
until the locked shell is extended; those are rare and surface in the
dashboard as a single "(no topic)" bucket per project.

Run from cron (launchd `com.m13v.social-twitter-attempt-topic-backfill`,
every 5 min) or directly:

    python3 scripts/backfill_twitter_attempts_topic.py            # 7d window
    python3 scripts/backfill_twitter_attempts_topic.py --days 30
    python3 scripts/backfill_twitter_attempts_topic.py --all      # entire table
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7,
                   help="Only backfill rows where ran_at >= NOW() - INTERVAL "
                        "'N days' (default 7). Cron path uses 7; ad-hoc "
                        "operators can widen.")
    p.add_argument("--all", action="store_true",
                   help="Backfill the entire table; ignores --days.")
    args = p.parse_args()

    conn = db.get_conn()
    where_recent = (
        ""
        if args.all
        else f"AND a.ran_at >= NOW() - INTERVAL '{int(args.days)} days'"
    )

    t0 = time.time()

    # Pass A: candidate-join. Covers any attempt that produced >=1 scored tweet.
    cur = conn.execute(f"""
      UPDATE twitter_search_attempts a
         SET search_topic = sub.topic
        FROM (
          SELECT search_attempt_id, MIN(search_topic) AS topic
            FROM twitter_candidates
           WHERE search_attempt_id IS NOT NULL
             AND search_topic IS NOT NULL
             AND search_topic <> ''
           GROUP BY search_attempt_id
        ) sub
       WHERE a.id = sub.search_attempt_id
         AND a.search_topic IS NULL
         {where_recent}
    """)
    a_rows = cur.rowcount
    conn.commit()

    # Pass B: batch-fanout. Covers dud attempts whose sibling non-dud attempts
    # agree on a single topic. Ambiguous batches stay NULL.
    cur = conn.execute(f"""
      UPDATE twitter_search_attempts a
         SET search_topic = sub.topic
        FROM (
          SELECT batch_id, project_name,
                 MIN(search_topic) AS topic,
                 COUNT(DISTINCT search_topic) AS topic_n
            FROM twitter_search_attempts
           WHERE batch_id IS NOT NULL
             AND project_name IS NOT NULL
             AND search_topic IS NOT NULL
           GROUP BY batch_id, project_name
        ) sub
       WHERE a.batch_id = sub.batch_id
         AND a.project_name = sub.project_name
         AND a.search_topic IS NULL
         AND sub.topic_n = 1
         {where_recent}
    """)
    b_rows = cur.rowcount
    conn.commit()

    elapsed = time.time() - t0
    print(
        f"backfill_twitter_attempts_topic: "
        f"pass_a={a_rows} pass_b={b_rows} window={'all' if args.all else f'{args.days}d'} "
        f"elapsed={elapsed:.2f}s",
        file=sys.stderr,
    )
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
