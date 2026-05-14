#!/usr/bin/env python3
"""Attach a single outbound action to a campaign and increment its counter.

Usage:
    python3 campaign_bump.py --table posts       --id 123 --campaign-id 3
    python3 campaign_bump.py --table replies     --id 456 --campaign-id 3
    python3 campaign_bump.py --table dm_messages --id 789 --campaign-id 3

The named row's campaign_id column is set to the given campaign, and the
campaign's posts_made counter advances by one. Idempotent: if the row already
references this campaign, no counter bump happens.

Routes through /api/v1/campaigns/bump by default. Set
SOCIAL_AUTOPOSTER_LEGACY_NEON=1 to use direct Neon.
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


def _bump_via_neon(table, row_id, campaign_id):
    import db
    conn = db.get_conn()
    try:
        cur = conn.execute(
            f"UPDATE {table} SET campaign_id = %s "
            f"WHERE id = %s AND (campaign_id IS NULL OR campaign_id <> %s) "
            f"RETURNING id",
            [campaign_id, row_id, campaign_id],
        )
        bumped = cur.fetchone() is not None
        if bumped:
            conn.execute(
                "UPDATE campaigns SET posts_made = posts_made + 1, updated_at = NOW() "
                "WHERE id = %s",
                [campaign_id],
            )
        conn.commit()
        return bumped
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", required=True, choices=sorted(ALLOWED_TABLES))
    ap.add_argument("--id", type=int, required=True)
    ap.add_argument("--campaign-id", type=int, required=True)
    args = ap.parse_args()

    if os.environ.get("SOCIAL_AUTOPOSTER_LEGACY_NEON") == "1":
        bumped = _bump_via_neon(args.table, args.id, args.campaign_id)
    else:
        bumped = _bump_via_api(args.table, args.id, args.campaign_id)

    print(f"table={args.table} id={args.id} campaign={args.campaign_id} bumped={bumped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
