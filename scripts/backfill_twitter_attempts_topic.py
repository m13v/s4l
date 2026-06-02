#!/usr/bin/env python3
"""backfill_twitter_attempts_topic.py

Periodic UPDATE that fills `twitter_search_attempts.search_topic` from the
adjacent `twitter_candidates` rows once a scoring cycle finishes writing them.

HTTP-only (no DATABASE_URL): the two backfill passes run server-side behind
`POST /api/v1/twitter-search-attempts/backfill-topic`. This script is now a
thin trigger that POSTs the window and prints the rows-updated counts. The
published package carries no direct-DB dependency.

Why this backfill exists at all: `score_twitter_candidates.py` and the parent
`skill/run-twitter-cycle.sh` are both `chflags uchg` locked, and the canonical
SCAN_SCHEMA in the shell does not yet carry `search_topic` on each entry of
`queries_used` (so `log_twitter_search_attempts.py` cannot stamp it at INSERT
time). Until those locked files are extended, we backfill from the candidate
side, which DOES know the topic (set by `pick_search_topic.py` -> stamped onto
twitter_candidates.search_topic + search_attempt_id).

The endpoint runs two passes, both safe to rerun:

  A) Direct join via search_attempt_id (covers non-dud attempts that produced
     at least one candidate).

  B) Fanout via (batch_id, project_name) -> covers dud attempts whose siblings
     in the same cycle DID return candidates and therefore know the topic.
     Skips ambiguous batches (more than one distinct topic) to avoid noise.

Fully-dud cycles stay NULL until the locked shell is extended; rare, and they
surface in the dashboard as a single "(no topic)" bucket per project.

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
from http_api import api_post, load_env  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7,
                   help="Only backfill rows where ran_at >= NOW() - INTERVAL "
                        "'N days' (default 7). Cron path uses 14; ad-hoc "
                        "operators can widen.")
    p.add_argument("--all", action="store_true",
                   help="Backfill the entire table; ignores --days.")
    args = p.parse_args()

    load_env()
    t0 = time.time()

    resp = api_post(
        "/api/v1/twitter-search-attempts/backfill-topic",
        {"days": int(args.days), "all": bool(args.all)},
    )
    data = resp.get("data") or {}
    a_rows = data.get("pass_a", 0)
    b_rows = data.get("pass_b", 0)
    window = data.get("window", "all" if args.all else f"{args.days}d")

    elapsed = time.time() - t0
    print(
        f"backfill_twitter_attempts_topic: "
        f"pass_a={a_rows} pass_b={b_rows} window={window} "
        f"elapsed={elapsed:.2f}s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
