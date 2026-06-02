#!/usr/bin/env python3
"""stats_helper.py — CLI wrapper used by skill/stats.sh to replace the inline
`psql "$DATABASE_URL"` one-liner the LinkedIn Step 4 gate used to embed. The
direct-Postgres lane was removed 2026-06-01; DATABASE_URL is deliberately
ignored, no DB, no fallback. The subcommand prints exactly what the psql call
printed so the surrounding shell capture ($(...)) and integer compare are
unchanged.

Subcommands:
  linkedin-refresh-count
      -> GET /api/v1/posts/count?platform=linkedin&status=active&has_our_url=true
             &our_url_contains=linkedin.com/feed/update/&engagement_stale_days=7
      -> prints the integer count of LinkedIn posts eligible for a stats refresh
         (was: COUNT(*) FROM posts WHERE platform='linkedin' AND status='active'
          AND our_url IS NOT NULL AND our_url LIKE '%linkedin.com/feed/update/%'
          AND (engagement_updated_at IS NULL
               OR engagement_updated_at < NOW() - INTERVAL '7 days'))
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get  # noqa: E402


def cmd_linkedin_refresh_count() -> int:
    resp = api_get("/api/v1/posts/count", query={
        "platform": "linkedin",
        "status": "active",
        "has_our_url": "true",
        "our_url_contains": "linkedin.com/feed/update/",
        "engagement_stale_days": 7,
    })
    print(int((resp.get("data") or {}).get("count") or 0))
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("linkedin-refresh-count")
    args = p.parse_args()
    if args.cmd == "linkedin-refresh-count":
        return cmd_linkedin_refresh_count()
    return 1


if __name__ == "__main__":
    sys.exit(main())
