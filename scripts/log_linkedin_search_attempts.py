#!/usr/bin/env python3
"""
log_linkedin_search_attempts.py

Insert one row per (query, project, search_topic, candidates_found,
serp_quality_score, dropped_below_floor) into linkedin_search_attempts. Reads a JSON array on
stdin shaped like:

    [
      {"query": "...", "project": "fazm",   "search_topic": "AI agents", "candidates_found": 0, "serp_quality_score": 1.5, "dropped_below_floor": 0},
      {"query": "...", "project": "mediar", "search_topic": "RPA replacement", "candidates_found": 7, "serp_quality_score": 8.0, "dropped_below_floor": 3},
      ...
    ]

candidates_found is the POST-floor count (cards that passed
discover_linkedin_candidates.py's velocity floor). dropped_below_floor is
the per-query count of cards that the SERP returned but the floor rejected;
absent or 0 for queries the floor didn't run on.

Used by run-linkedin.sh after Phase A scrape parses queries_used out of the
LLM envelope. Logging zero-result AND low-quality SERP queries here is the
whole point: linkedin_candidates only has rows for posts that were actually
extracted, so "query returned 30 influencer slop posts that we skipped" was
previously invisible.

Pair with top_dud_linkedin_queries.py.

    python3 scripts/log_linkedin_search_attempts.py --batch-id <id> < queries.json

Migrated 2026-06-01 from direct psycopg2/db.py INSERTs to the s4l.ai HTTP API
(POST /api/v1/linkedin-search-attempts, batch form). No DATABASE_URL needed.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_post


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--batch-id", default=None)
    args = p.parse_args()

    raw = sys.stdin.read().strip()
    if not raw:
        print("log_linkedin_search_attempts: empty stdin, nothing to log",
              file=sys.stderr)
        return 0

    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"log_linkedin_search_attempts: bad JSON on stdin: {e}",
              file=sys.stderr)
        return 1

    if not isinstance(rows, list) or not rows:
        print("log_linkedin_search_attempts: not a list or empty list, nothing to log",
              file=sys.stderr)
        return 0

    payload = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        query = (r.get("query") or "").strip()
        if not query:
            continue
        project = (r.get("project") or "").strip() or None
        search_topic = (r.get("search_topic") or "").strip() or None
        candidates_found = r.get("candidates_found")
        try:
            candidates_found = int(candidates_found if candidates_found is not None else 0)
        except (TypeError, ValueError):
            candidates_found = 0
        dropped = r.get("dropped_below_floor")
        try:
            dropped = int(dropped if dropped is not None else 0)
        except (TypeError, ValueError):
            dropped = 0
        serp = r.get("serp_quality_score")
        try:
            serp = float(serp) if serp is not None else None
        except (TypeError, ValueError):
            serp = None
        payload.append({
            "query": query,
            "project_name": project,
            "search_topic": search_topic,
            "candidates_found": candidates_found,
            "serp_quality_score": serp,
            "candidates_dropped_below_floor": dropped,
            "batch_id": args.batch_id,
        })

    if not payload:
        print("log_linkedin_search_attempts: no valid rows after parse, nothing to log",
              file=sys.stderr)
        return 0

    resp = api_post("/api/v1/linkedin-search-attempts", payload)
    inserted = (resp.get("data") or {}).get("inserted", len(payload))

    duds = sum(
        1 for r in rows
        if isinstance(r, dict) and not int(r.get("candidates_found") or 0)
    )
    low_quality = sum(
        1 for r in rows
        if isinstance(r, dict)
           and r.get("serp_quality_score") is not None
           and float(r["serp_quality_score"]) < 4.0
    )
    print(
        f"log_linkedin_search_attempts: inserted {inserted} rows "
        f"({duds} zero-result, {low_quality} low-SERP) for batch={args.batch_id}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
