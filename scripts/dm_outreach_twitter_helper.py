#!/usr/bin/env python3
"""dm_outreach_twitter_helper.py — small CLI wrapper used by
skill/dm-outreach-twitter.sh to replace the four direct `psql` one-liners
the script used to embed inline (pending count, outreach JSON aggregation,
MCP-failure recovery sweep, sent/still-pending summary counts).

Subcommands:
  pending-count
      -> GET /api/v1/dms/counts?platform=twitter (canonicalises to 'x')
      -> prints the integer pending count

  outreach-queue
      -> GET /api/v1/dms/outreach-queue?platform=twitter&status=pending
      -> prints the rows as a JSON ARRAY (mirrors the legacy
         `SELECT json_agg(q) FROM (...) q` shape the bash prompt embeds)

  recover-mcp --session-id UUID
      -> POST /api/v1/dms/recover-mcp-failures { platform, claude_session_id }
      -> prints the recovered_count integer

  summary
      -> GET /api/v1/dms/counts?platform=twitter
      -> prints "<sent> <still_pending>" so the legacy two-variable capture
         keeps working (SENT/STILL_PENDING in dm-outreach-twitter.sh).

Migrated 2026-05-18: removes 4 direct psql calls from
skill/dm-outreach-twitter.sh. The dms table stores Twitter rows with
platform='x' (scan_dm_candidates.py:219-220 normalises 'twitter' → 'x'
before INSERT); routes accept either form and the canonicalDmPlatform
helper rewrites the WHERE clauses uniformly.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_post  # noqa: E402


def _counts_dict() -> dict[str, int]:
    resp = api_get(
        "/api/v1/dms/counts",
        query={"platform": "twitter"},
    )
    rows = (resp.get("data") or {}).get("counts") or []
    out: dict[str, int] = {}
    for r in rows:
        s = r.get("status")
        if s is None:
            continue
        try:
            out[str(s)] = int(r.get("count") or 0)
        except (TypeError, ValueError):
            out[str(s)] = 0
    return out


def cmd_pending_count() -> int:
    counts = _counts_dict()
    sys.stdout.write(f"{int(counts.get('pending') or 0)}\n")
    return 0


def cmd_summary() -> int:
    counts = _counts_dict()
    sent = int(counts.get("sent") or 0)
    pending = int(counts.get("pending") or 0)
    sys.stdout.write(f"{sent} {pending}\n")
    return 0


def cmd_outreach_queue() -> int:
    resp = api_get(
        "/api/v1/dms/outreach-queue",
        query={
            "platform": "twitter",
            "status": "pending",
            "limit": 200,
            "other_engagement_days": 60,
        },
    )
    rows = (resp.get("data") or {}).get("rows") or []
    # The legacy psql query returned an array (json_agg result); reshape
    # to that same array shape. Each row already carries the embedded
    # other_engagement array from the route's correlated subquery.
    sys.stdout.write(json.dumps(rows))
    sys.stdout.write("\n")
    return 0


def cmd_recover_mcp(session_id: str) -> int:
    resp = api_post(
        "/api/v1/dms/recover-mcp-failures",
        {"platform": "twitter", "claude_session_id": session_id},
    )
    d = resp.get("data") or {}
    sys.stdout.write(f"{int(d.get('recovered_count') or 0)}\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Helper for dm-outreach-twitter.sh")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("pending-count")
    sub.add_parser("outreach-queue")
    sub.add_parser("summary")

    p_rec = sub.add_parser("recover-mcp")
    p_rec.add_argument("--session-id", required=True)

    args = ap.parse_args()

    if args.cmd == "pending-count":
        return cmd_pending_count()
    if args.cmd == "summary":
        return cmd_summary()
    if args.cmd == "outreach-queue":
        return cmd_outreach_queue()
    if args.cmd == "recover-mcp":
        return cmd_recover_mcp(args.session_id)
    return 1


if __name__ == "__main__":
    sys.exit(main())
