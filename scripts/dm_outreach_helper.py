#!/usr/bin/env python3
"""dm_outreach_helper.py — shell-friendly entrypoints for the dm-outreach
{reddit,twitter,linkedin}.sh pipelines that used to inline `psql` calls.

Subcommands (all route through /api/v1/dms* on the website):

  count --platform reddit --status pending
    -> prints integer count to stdout (one line). Used by the bash
       script's "DM_PENDING / SENT / STILL_PENDING" sentinels.

  outreach-queue --platform reddit
    -> prints JSON shape matching the legacy
       `psql ... "SELECT json_agg(q) FROM ... JOIN replies ... JOIN posts ..."`
       query: an array of dms rows joined with reply + post + a 60-day
       other_engagement summary per author. Output is exactly the same
       JSON the LLM prompt expects (DM_DATA variable in dm-outreach-*.sh).

  patch --id 123 --status error --skip-reason send_unverified
    -> PATCH /api/v1/dms/123. Replaces the bash-embedded
       `psql ... "UPDATE dms SET status=..., skip_reason=..."` blocks
       (the ones the LLM is told to run). Supports any combo of
       --status / --skip-reason / --claude-session-id.

       (NOTE: this still flips status freely. dm_send_log.py is the only
        path that's allowed to set status='sent' with verification — DO
        NOT use this `patch` subcommand to mark a DM as sent. The legacy
        bash script's prompt was already careful about this; we preserve
        that constraint.)

This script intentionally does NOT touch dm_conversation.py or the dms
DB schema directly. Everything goes through HTTP routes, no psycopg2.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_patch


def _cmd_count(args):
    query = {
        "platform": args.platform,
        "status": args.status,
        "count_only": "true",
    }
    if args.target_project:
        query["target_project"] = args.target_project
    resp = api_get("/api/v1/dms", query=query)
    data = (resp or {}).get("data") or {}
    print(int(data.get("count") or 0))


def _cmd_outreach_queue(args):
    query = {
        "platform": args.platform,
        "status": args.status,
        "limit": args.limit,
        "other_engagement_days": args.other_engagement_days,
    }
    resp = api_get("/api/v1/dms/outreach-queue", query=query)
    data = (resp or {}).get("data") or {}
    rows = data.get("rows") or []
    # Mirror the legacy `SELECT json_agg(q) FROM (...) q;` output shape.
    # The psql command returned a single JSON array (or empty string when
    # zero rows). The LLM prompt expects an array literal it can read
    # directly. Print [] when empty to match.
    json.dump(rows, sys.stdout)
    print("", file=sys.stdout)


def _cmd_patch(args):
    if args.status == "sent":
        # status='sent' must go through scripts/dm_send_log.py (verified
        # outbound path). Anything else (error, skipped, queued, ...) is
        # fine to flip from here.
        print(
            "ERROR: dm_outreach_helper.py patch refuses to set status=sent. "
            "Use scripts/dm_send_log.py with --verified instead.",
            file=sys.stderr,
        )
        sys.exit(2)

    body: dict = {}
    if args.status:
        body["status"] = args.status
    if args.skip_reason is not None:
        body["skip_reason"] = args.skip_reason
    if args.claude_session_id:
        body["claude_session_id"] = args.claude_session_id
    if args.conversation_status:
        body["conversation_status"] = args.conversation_status

    if not body:
        print("ERROR: nothing to patch (no --status / --skip-reason / ...)",
              file=sys.stderr)
        sys.exit(2)

    resp = api_patch(f"/api/v1/dms/{args.id}", body)
    data = (resp or {}).get("data") or {}
    dm = data.get("dm")
    if dm:
        print(f"PATCHED dm_id={dm.get('id')} status={dm.get('status')} "
              f"skip_reason={dm.get('skip_reason')}")
    else:
        # Route returned no body (shouldn't happen on 200) — emit raw resp.
        json.dump(resp, sys.stdout)
        print("", file=sys.stdout)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("count", help="Print COUNT(*) for filtered dms")
    pc.add_argument("--platform", required=True)
    pc.add_argument("--status", default="pending")
    pc.add_argument("--target-project", default=None)
    pc.set_defaults(func=_cmd_count)

    pq = sub.add_parser("outreach-queue",
                        help="Emit the join'd DM/reply/post JSON for the LLM prompt")
    pq.add_argument("--platform", required=True)
    pq.add_argument("--status", default="pending")
    pq.add_argument("--limit", type=int, default=50)
    pq.add_argument("--other-engagement-days", type=int, default=60)
    pq.set_defaults(func=_cmd_outreach_queue)

    pp = sub.add_parser("patch",
                        help="PATCH a dms row (status / skip_reason / etc.)")
    pp.add_argument("--id", required=True, type=int)
    pp.add_argument("--status", default=None,
                    help="New status (NOT 'sent' — use dm_send_log.py for that).")
    pp.add_argument("--skip-reason", default=None,
                    help="Reason string (e.g. 'send_unverified', 'reddit_browser_busy').")
    pp.add_argument("--conversation-status", default=None)
    pp.add_argument("--claude-session-id", default=None)
    pp.set_defaults(func=_cmd_patch)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
