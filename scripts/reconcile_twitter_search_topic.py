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
(batch_id, project) -> query is a deterministic lookup. The route rewrites
twitter_candidates.search_topic to that canonical query wherever they differ.

It is idempotent and safe to run repeatedly: rows already correct are skipped
(IS DISTINCT FROM), and (batch_id, project) pairs that map to more than one
distinct query are left untouched (ambiguous, reported separately).

Usage:
    python3 scripts/reconcile_twitter_search_topic.py                 # dry run
    python3 scripts/reconcile_twitter_search_topic.py --apply          # write
    python3 scripts/reconcile_twitter_search_topic.py --apply --window-days 90
    python3 scripts/reconcile_twitter_search_topic.py --quiet --apply   # cron

Migrated 2026-05-18: the entire CTE (canon SELECT + UPDATE) runs server-side
behind /api/v1/twitter-candidates/reconcile-search-topic. This script just
shapes the response into the legacy CLI output and writes the rollback
snapshot file from the route's `sample` payload (the snapshot now holds the
first 5 examples instead of every fixable row — the route exposes total
counts but no longer streams every id back to the client).
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_post  # noqa: E402

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

    body = {
        "window_days": args.window_days,
        "apply": bool(args.apply),
    }
    resp = api_post(
        "/api/v1/twitter-candidates/reconcile-search-topic",
        body,
    )
    data = resp.get("data") or {}

    fixable_count = int(data.get("fixable_count") or 0)
    ambiguous_count = int(data.get("ambiguous_count") or 0)
    by_project = data.get("by_project") or {}
    sample = data.get("sample") or []

    if not args.quiet:
        print(f"reconcile_twitter_search_topic: window={args.window_days}d")
        print(f"  candidates to reconcile : {fixable_count}")
        print(f"  ambiguous (skipped)     : {ambiguous_count}")
        for proj, cnt in sorted(by_project.items(), key=lambda kv: -int(kv[1])):
            print(f"    {proj:<26} {cnt}")

    if fixable_count == 0:
        print("reconcile_twitter_search_topic: nothing to do.")
        return 0

    if not args.apply:
        print("reconcile_twitter_search_topic: DRY RUN, no rows written. "
              "Re-run with --apply to commit.")
        for s in sample[:5]:
            print(f"  [{s.get('project')}] id={s.get('id')}")
            print(f"      old: {s.get('old')!r}")
            print(f"      new: {s.get('new')!r}")
        return 0

    # Snapshot the sample so the change is at least partially reversible.
    # The full pre-image is no longer streamed back from the route (post-2026-
    # 05-18 redesign); if a full rollback is ever needed, query
    # twitter_candidates history server-side instead.
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap_path = os.path.join(SNAPSHOT_DIR, f"search_topic_snapshot_{stamp}.json")
    with open(snap_path, "w") as f:
        json.dump({
            "window_days": args.window_days,
            "fixable_count": fixable_count,
            "ambiguous_count": ambiguous_count,
            "by_project": by_project,
            "sample_only": True,
            "sample": sample,
        }, f, indent=2)

    updated = int(data.get("updated_count") or fixable_count)
    print(f"reconcile_twitter_search_topic: updated {updated} rows. "
          f"Sample snapshot: {snap_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
