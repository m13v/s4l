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
    """Return per-project candidate stats since `since_iso`.

    Paginates twitter-candidates by discovered_at (the API supports `since`).
    Stops at limit=500 per page — distinct pairs are stable across truncation
    but raw row counts may be a slight under-read on heavy windows; the next
    run picks up the tail.

    Returns dict keyed by project_name with:
        topics        set[str]              — distinct search_topic strings
        row_total     int                   — raw rows in window
        rows_by_topic dict[str, int]        — row count per topic
    """
    query = {
        "since": since_iso,
        "limit": 500,
    }
    if only_project:
        query["matched_project"] = only_project
    resp = api_get("/api/v1/twitter-candidates", query=query)
    rows = ((resp or {}).get("data") or {}).get("candidates") or []
    out: dict[str, dict] = {}
    for r in rows:
        proj = (r.get("matched_project") or "").strip()
        topic = (r.get("search_topic") or "").strip()
        if not proj or not topic:
            continue
        bucket = out.setdefault(proj, {
            "topics": set(),
            "row_total": 0,
            "rows_by_topic": {},
        })
        bucket["topics"].add(topic)
        bucket["row_total"] += 1
        bucket["rows_by_topic"][topic] = bucket["rows_by_topic"].get(topic, 0) + 1
    return out


def _existing_topics_with_source(project: str) -> dict[str, str]:
    """Return {topic: source} for every row in project_search_topics, any status.

    Used both as the "already known" idempotency check (key membership) and
    to identify which existing topics are invented (value=='invented') so
    `posts_for_known_inventions` can be reported on a 0-new cycle.
    """
    resp = api_get(
        "/api/v1/project-search-topics",
        query={"project": project, "status": "all"},
    )
    rows = ((resp or {}).get("data") or {}).get("topics") or []
    return {
        (r.get("topic") or "").strip(): (r.get("source") or "seed").strip()
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
        bucket = pairs[proj]
        topics = bucket["topics"]
        row_total = bucket["row_total"]
        rows_by_topic = bucket["rows_by_topic"]
        try:
            existing = _existing_topics_with_source(proj)
        except SystemExit as e:
            print(f"[promote_invented] FAIL list project={proj!r}: {e}",
                  file=sys.stderr)
            total_failed += 1
            continue
        existing_keys = set(existing.keys())
        new_topics = sorted(t for t in topics if t not in existing_keys)
        skipped = len(topics) - len(new_topics)
        total_skipped += skipped
        if not new_topics:
            # "0 new" used to be silent on whether known inventions
            # earned activity in the window. Now we surface raw row
            # counts so a quiet cycle (truly nothing happening) is
            # distinguishable from a busy-but-no-net-new cycle (known
            # inventions accruing candidates).
            known_invention_rows = {
                t: rows_by_topic[t]
                for t in rows_by_topic
                if existing.get(t) == "invented"
            }
            inv_summary = (
                " activity_for_known_inventions="
                + ",".join(
                    f"{t!r}:{n}" for t, n in
                    sorted(known_invention_rows.items(), key=lambda kv: -kv[1])
                )
            ) if known_invention_rows else ""
            print(
                f"{proj}: 0 new (candidate-distinct={len(topics)}, "
                f"already-known={skipped}, candidate-rows={row_total})"
                + inv_summary
            )
            continue
        if args.dry_run:
            for t in new_topics:
                print(f"[dry] would promote: project={proj!r} topic={t!r}")
            print(
                f"{proj}: {len(new_topics)} new (dry-run, "
                f"candidate-rows={row_total})"
            )
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
        print(
            f"{proj}: {promoted} new promoted, {skipped} already known, "
            f"candidate-rows={row_total}"
        )

    print(
        f"\n[promote_invented] done. new={total_new} skipped_known="
        f"{total_skipped} failed={total_failed}"
    )
    if total_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
