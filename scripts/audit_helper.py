#!/usr/bin/env python3
"""audit_helper.py — CLI wrapper used by the audit pipelines
(skill/audit.sh, skill/audit-dm-staleness.sh, skill/audit-reddit-resurrect.sh)
to replace the inline `psql "$DATABASE_URL"` one-liners they used to embed. The
direct-Postgres lane was removed 2026-06-01; DATABASE_URL is deliberately
ignored, no DB, no fallback. Every subcommand prints exactly what the
corresponding psql call printed so the surrounding shell capture ($(...)),
integer compares, and `IFS='|' read` loops are unchanged.

Subcommands:
  twitter-active-count
      -> GET /api/v1/posts/count?platform=twitter&status=active&has_our_url=true
      -> prints int (was: COUNT(*) FROM posts WHERE platform='twitter'
         AND status='active' AND our_url IS NOT NULL)
  orphan-report
      -> GET /api/v1/posts/status-breakdown
      -> prints pipe-delimited "platform|status|count" lines (one per group),
         empty when none (was: SELECT platform, status, COUNT(*) ... GROUP BY
         ... for status NOT IN ('active','deleted','removed'))
  broken-url-count
      -> GET /api/v1/posts/count?status=active&broken_url=true
      -> prints int (was: COUNT(*) FROM posts WHERE status='active'
         AND (our_url IS NULL OR ''='' OR our_url NOT LIKE 'http%'))
  status-count --status S
      -> GET /api/v1/posts/count?status=S
      -> prints int (was: COUNT(*) FROM posts WHERE status='S')
  resurrect-candidates
      -> GET /api/v1/posts/count?platform=reddit&status_in=deleted,removed
             &within_seconds=5184000&has_our_url=true
      -> prints int (was: COUNT(*) FROM posts WHERE platform='reddit'
         AND status IN ('deleted','removed') AND posted_at > NOW() - 60 days
         AND our_url IS NOT NULL)
  dm-staleness-sweep
      -> POST /api/v1/dms/staleness-sweep
      -> prints JSON {aged, downgraded} (was: two UPDATE ... RETURNING CTEs)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_post  # noqa: E402


def _count(query: dict) -> int:
    resp = api_get("/api/v1/posts/count", query=query)
    return int((resp.get("data") or {}).get("count") or 0)


def cmd_twitter_active_count(_args) -> int:
    print(_count({"platform": "twitter", "status": "active", "has_our_url": "true"}))
    return 0


def cmd_orphan_report(_args) -> int:
    resp = api_get("/api/v1/posts/status-breakdown")
    rows = (resp.get("data") or {}).get("rows") or []
    for r in rows:
        print(f"{r.get('platform')}|{r.get('status')}|{int(r.get('count') or 0)}")
    return 0


def cmd_broken_url_count(_args) -> int:
    print(_count({"status": "active", "broken_url": "true"}))
    return 0


def cmd_status_count(args) -> int:
    print(_count({"status": args.status}))
    return 0


def cmd_resurrect_candidates(_args) -> int:
    print(_count({
        "platform": "reddit",
        "status_in": "deleted,removed",
        "within_seconds": 60 * 24 * 60 * 60,  # 60 days
        "has_our_url": "true",
    }))
    return 0


def cmd_dm_staleness_sweep(_args) -> int:
    resp = api_post("/api/v1/dms/staleness-sweep", body={})
    data = resp.get("data") or {}
    out = {
        "aged": int(data.get("aged") or 0),
        "downgraded": int(data.get("downgraded") or 0),
    }
    json.dump(out, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("twitter-active-count")
    sub.add_parser("orphan-report")
    sub.add_parser("broken-url-count")
    sc = sub.add_parser("status-count")
    sc.add_argument("--status", required=True)
    sub.add_parser("resurrect-candidates")
    sub.add_parser("dm-staleness-sweep")
    args = p.parse_args()
    return {
        "twitter-active-count": cmd_twitter_active_count,
        "orphan-report": cmd_orphan_report,
        "broken-url-count": cmd_broken_url_count,
        "status-count": cmd_status_count,
        "resurrect-candidates": cmd_resurrect_candidates,
        "dm-staleness-sweep": cmd_dm_staleness_sweep,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
