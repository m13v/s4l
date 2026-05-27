#!/usr/bin/env python3
"""Seed project_search_topics from config.json (one-time-per-install bootstrap).

The DB is now the source of truth for the search-topic universe (see migration
2026-05-27-project-search-topics.sql and pick_search_topic.py). This script
mirrors the project_name -> search_topics[] block in ~/social-autoposter/config.json
into /api/v1/project-search-topics with source='seed', status='active'. The API
upserts on (install_id, project_name, topic), so re-running this script:

  - Inserts rows added to config.json since the last seed.
  - Leaves source unchanged for rows that already exist (server preserves
    source on UPDATE; status is touched only because of upsert semantics, but
    it lands on the same 'active' default we send).
  - Never deletes rows (paused/excluded topics in the DB are protected from
    config.json drift; explicit pause/exclude lives in the dashboard, not here).

CLI:
    python3 scripts/seed_search_topics.py            # seed every project
    python3 scripts/seed_search_topics.py --project fazm
    python3 scripts/seed_search_topics.py --dry-run  # show counts, don't POST
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from http_api import api_post  # noqa: E402

CONFIG_PATH = os.path.expanduser("~/social-autoposter/config.json")


def _load_projects(only_project: str | None = None):
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    out = []
    for p in cfg.get("projects", []):
        name = (p.get("name") or "").strip()
        if not name:
            continue
        if only_project and name.lower() != only_project.lower():
            continue
        topics = []
        seen = set()
        for t in (p.get("search_topics") or []):
            t = (t or "").strip()
            if t and t not in seen:
                seen.add(t)
                topics.append(t)
        out.append((name, topics))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", default=None,
                    help="Only seed this project name (default: all)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print counts; do not POST")
    args = ap.parse_args()

    projects = _load_projects(args.project)
    if not projects:
        sys.stderr.write(
            f"seed_search_topics: no projects matched "
            f"(filter={args.project!r})\n"
        )
        sys.exit(2)

    total_inserted = 0
    total_updated = 0
    total_failed = 0
    total_planned = 0

    for name, topics in projects:
        total_planned += len(topics)
        if args.dry_run:
            print(f"[dry] {name}: {len(topics)} topics")
            continue
        inserted = 0
        updated = 0
        for topic in topics:
            try:
                resp = api_post(
                    "/api/v1/project-search-topics",
                    body={
                        "project": name,
                        "topic": topic,
                        "source": "seed",
                        "status": "active",
                    },
                )
            except SystemExit as e:
                total_failed += 1
                print(f"[FAIL] {name}: {topic!r}: {e}", file=sys.stderr)
                continue
            data = (resp or {}).get("data") or resp or {}
            action = data.get("action") or ""
            if action == "inserted":
                inserted += 1
            elif action == "updated":
                updated += 1
        total_inserted += inserted
        total_updated += updated
        print(
            f"{name}: planned={len(topics)} inserted={inserted} "
            f"updated={updated}"
        )

    if args.dry_run:
        print(f"[dry] total topics across {len(projects)} project(s): {total_planned}")
        return

    print(
        f"\ndone. projects={len(projects)} planned={total_planned} "
        f"inserted={total_inserted} updated={total_updated} "
        f"failed={total_failed}"
    )
    if total_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
