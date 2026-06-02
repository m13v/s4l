#!/usr/bin/env python3
"""Attach a single outbound action to a campaign and increment its counter.

Usage:
    python3 campaign_bump.py --table posts       --id 123 --campaign-id 3
    python3 campaign_bump.py --table replies     --id 456 --campaign-id 3
    python3 campaign_bump.py --table dm_messages --id 789 --campaign-id 3

The named row's campaign_id column is set to the given campaign, and the
campaign's posts_made counter advances by one. Idempotent: if the row already
references this campaign, no counter bump happens.

HTTP-only lane (2026-06-01): routes through /api/v1/campaigns/bump. No
DATABASE_URL, no db.get_conn(), no fallback.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ALLOWED_TABLES = {"posts", "replies", "dm_messages"}


def _bump_via_api(table, row_id, campaign_id):
    from http_api import api_post
    resp = api_post(
        "/api/v1/campaigns/bump",
        {"table": table, "id": int(row_id), "campaign_id": int(campaign_id)},
    )
    data = (resp or {}).get("data") or {}
    bumped = bool(data.get("bumped"))
    return bumped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", required=True, choices=sorted(ALLOWED_TABLES))
    ap.add_argument("--id", type=int, required=True)
    ap.add_argument("--campaign-id", type=int, required=True)
    args = ap.parse_args()

    bumped = _bump_via_api(args.table, args.id, args.campaign_id)

    print(f"table={args.table} id={args.id} campaign={args.campaign_id} bumped={bumped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
