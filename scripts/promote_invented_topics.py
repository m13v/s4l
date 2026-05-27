#!/usr/bin/env python3
"""Promote search topics invented in EXPLORE_INVENT cycles into the DB universe.

Closes the loop on pick_search_topic.py's EXPLORE_INVENT branch: when Claude
invents a fresh `search_topic` in that branch, the value gets stamped on
twitter_candidates and posts but it does NOT automatically land in
project_search_topics, so the USE branch (which reads project_search_topics)
cannot re-select it.

This script scans recent twitter_candidates rows for (matched_project,
search_topic) pairs that don't exist in project_search_topics yet, and POSTs
each missing pair with source='invented', status='active'. From the next
cycle onward, the picker can pick the invention via the normal
weighted-random USE branch and the existing _compute_weight gates
(conversion penalty, supply-dead penalty) handle quality filtering — no
manual curation needed.

Idempotent: only POSTs topics that aren't already in project_search_topics
(any status). Won't reactivate manually-paused inventions because the check
against the existing set covers status=all.

CLI:
    python3 scripts/promote_invented_topics.py            # default 48h lookback
    python3 scripts/promote_invented_topics.py --hours 168
    python3 scripts/promote_invented_topics.py --project fazm
    python3 scripts/promote_invented_topics.py --dry-run

Schedule: safe to run every cycle (it's a cheap GET-then-POST-only-new flow)
or once an hour out of band. No DB rows are deleted; failures only log.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from http_api import api_get, api_post  # noqa: E402


def _recent_candidate_pairs(since_iso: str, only_project: str | None):
    """Return {project_name: set(search_topic)} for candidates since `since_iso`.

    Paginates twitter-candidates by discovered_at (the API supports `since`).
    Stops at limit=500 per page — we only care about distinct pairs so a few
    skipped trailing rows are harmless; the next run picks them up.
    """
    query = {
        "since": since_iso,
        "limit": 500,
    }
    if only_project:
        query["matched_project"] = only_project
    resp = api_get("/api/v1/twitter-candidates", query=query)
    rows = ((resp or {}).get("data") or {}).get("candidates") or []
    pairs: dict[str, set[str]] = {}
    for r in rows:
        proj = (r.get("matched_project") or "").strip()
        topic = (r.get("search_topic") or "").strip()
        if not proj or not topic:
            continue
        pairs.setdefault(proj, set()).add(topic)
    return pairs


def _existing_topics(project: str) -> set[str]:
    """Return every topic for `project` in project_search_topics, any status."""
    resp = api_get(
        "/api/v1/project-search-topics",
        query={"project": project, "status": "all"},
    )
    rows = ((resp or {}).get("data") or {}).get("topics") or []
    return {
        (r.get("topic") or "").strip()
        for r in rows
        if r.get("topic")
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=int, default=48,
                    help="Lookback window for twitter_candidates (default 48)")
    ap.add_argument("--project", default=None,
                    help="Filter to one project (default: every project)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be promoted; do not POST")
    args = ap.parse_args()

    since = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).isoformat()
    print(f"[promote_invented] scan since={since} project={args.project or '*'}",
          file=sys.stderr)

    pairs = _recent_candidate_pairs(since, args.project)
    if not pairs:
        print("[promote_invented] no candidates in window", file=sys.stderr)
        return

    total_new = 0
    total_skipped = 0
    total_failed = 0

    for proj in sorted(pairs):
        topics = pairs[proj]
        try:
            existing = _existing_topics(proj)
        except SystemExit as e:
            print(f"[promote_invented] FAIL list project={proj!r}: {e}",
                  file=sys.stderr)
            total_failed += 1
            continue
        new_topics = sorted(t for t in topics if t not in existing)
        skipped = len(topics) - len(new_topics)
        total_skipped += skipped
        if not new_topics:
            print(
                f"{proj}: 0 new (candidate-distinct={len(topics)}, "
                f"already-known={skipped})"
            )
            continue
        if args.dry_run:
            for t in new_topics:
                print(f"[dry] would promote: project={proj!r} topic={t!r}")
            print(f"{proj}: {len(new_topics)} new (dry-run)")
            continue
        promoted = 0
        for topic in new_topics:
            try:
                api_post(
                    "/api/v1/project-search-topics",
                    body={
                        "project": proj,
                        "topic": topic,
                        "source": "invented",
                        "status": "active",
                    },
                )
                promoted += 1
                print(
                    f"[promote_invented] project={proj!r} topic={topic!r} "
                    f"source=invented status=active"
                )
            except SystemExit as e:
                total_failed += 1
                print(f"[promote_invented] FAIL {proj!r}/{topic!r}: {e}",
                      file=sys.stderr)
        total_new += promoted
        print(f"{proj}: {promoted} new promoted, {skipped} already known")

    print(
        f"\n[promote_invented] done. new={total_new} skipped_known="
        f"{total_skipped} failed={total_failed}"
    )
    if total_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
