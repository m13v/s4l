#!/usr/bin/env python3
"""
log_twitter_search_attempts.py

Insert one row per (query, project, tweets_found) into twitter_search_attempts.
Reads a JSON array on stdin shaped like:

    [
      {"query": "...", "project": "fazm", "tweets_found": 0},
      {"query": "...", "project": "mediar", "tweets_found": 3},
      ...
    ]

Used by run-twitter-cycle.sh after Phase 1 scan parses queries_used out of the
LLM envelope. Logging zero-result queries here is the whole point — the
twitter_candidates table only has rows for tweets that were actually scraped,
so duds were previously invisible. Pair with top_dud_twitter_queries.py.

    python3 scripts/log_twitter_search_attempts.py --batch-id <id> < queries.json
    python3 scripts/log_twitter_search_attempts.py --batch-id <id> \
        --attempts-out /tmp/attempts.json < queries.json

When --attempts-out is provided, writes a JSON list of
    [{"query": ..., "project": ..., "attempt_id": <int>}, ...]
to that path so the downstream scorer can stamp twitter_candidates.search_
attempt_id and the dashboard gets exact 1:1 query<->post attribution. Without
this, the dashboard falls back to a (batch_id, project_name) fanout that
credits every query in the batch — including dud ones — with every posted
candidate (the bug user spotted 2026-05-21).

Migrated 2026-05-18: writes now POST to /api/v1/twitter-search-attempts via
scripts/http_api.py instead of opening a psycopg2 connection.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_post  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--batch-id", default=None)
    p.add_argument(
        "--attempts-out",
        default=None,
        help="Optional path; if set, write JSON list of "
             "[{query, project, attempt_id}, ...] for the scorer to consume.",
    )
    # kind: which pipeline drafted these attempts. 'cycle' (default) preserves
    # back-compat for every existing caller (run-twitter-cycle.sh + friends).
    # invent_topics.py passes --kind invent so qualified_query_bank can union
    # the proven invented set into the Phase 1 bank.
    p.add_argument("--kind", default="cycle", choices=("cycle", "invent"),
                   help="Pipeline lane writing these attempts.")
    args = p.parse_args()

    raw = sys.stdin.read().strip()
    if not raw:
        print("log_twitter_search_attempts: empty stdin, nothing to log", file=sys.stderr)
        if args.attempts_out:
            # Write empty list so the caller can still pass --attempts to the
            # scorer without a missing-file race.
            with open(args.attempts_out, "w") as f:
                json.dump([], f)
        return 0

    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"log_twitter_search_attempts: bad JSON on stdin: {e}", file=sys.stderr)
        return 1

    if not isinstance(rows, list) or not rows:
        print("log_twitter_search_attempts: not a list or empty list, nothing to log", file=sys.stderr)
        if args.attempts_out:
            with open(args.attempts_out, "w") as f:
                json.dump([], f)
        return 0

    inserted = 0
    attempts_map = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        query = (r.get("query") or "").strip()
        project = (r.get("project") or "").strip() or None
        tweets_found = r.get("tweets_found")
        try:
            tweets_found = int(tweets_found if tweets_found is not None else 0)
        except (TypeError, ValueError):
            tweets_found = 0
        # search_topic is the higher-level theme driving this query (set by
        # pick_search_topic.py at the start of the cycle). Optional, because
        # run-twitter-cycle.sh hasn't been threaded through the queries_used
        # envelope yet; score_twitter_candidates.py also backfills it from
        # twitter_candidates.search_topic on its end of the pipeline.
        search_topic = (r.get("search_topic") or "").strip() or None
        if not query:
            continue
        try:
            payload = {
                "query": query,
                "project_name": project,
                "tweets_found": tweets_found,
                "batch_id": args.batch_id,
                "kind": args.kind,
            }
            if search_topic:
                payload["search_topic"] = search_topic
            resp = api_post(
                "/api/v1/twitter-search-attempts",
                payload,
            )
            inserted += 1
            attempt_id = ((resp.get("data") or {}).get("attempt") or {}).get("id")
            if attempt_id is not None:
                attempts_map.append({
                    "query": query,
                    "project": project,
                    "attempt_id": int(attempt_id),
                })
        except SystemExit as e:
            # http_api raises SystemExit on terminal failure. Log and keep
            # going so a single bad row doesn't drop the rest of the batch.
            print(f"log_twitter_search_attempts: API error for {query!r}: {e}", file=sys.stderr)
            continue

    if args.attempts_out:
        with open(args.attempts_out, "w") as f:
            json.dump(attempts_map, f)
        print(
            f"log_twitter_search_attempts: wrote {len(attempts_map)} attempt-id "
            f"entries to {args.attempts_out}",
            file=sys.stderr,
        )

    duds = sum(1 for r in rows if isinstance(r, dict) and not int(r.get("tweets_found") or 0))
    print(
        f"log_twitter_search_attempts: inserted {inserted} rows ({duds} duds) "
        f"for batch={args.batch_id}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
