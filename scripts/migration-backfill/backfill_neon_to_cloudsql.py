#!/usr/bin/env python3
"""Backfill rows that exist in Neon but not in Cloud SQL.

Runs since the .env cutover timestamp (mtime of ~/social-autoposter/.env).
For each table:
  1. Pulls the set of PKs from Neon that were created post-cutover.
  2. Pulls the set of PKs from Cloud SQL post-cutover (a superset of what's already there).
  3. Diffs to find PKs present in Neon but missing in Cloud SQL.
  4. For each missing PK, fetches the full row from Neon and INSERTs into Cloud SQL
     with ON CONFLICT (<pk>) DO NOTHING — safe to re-run.

Skips claude_sessions by default (cost-tracking only, low business value); pass --include-sessions to include.

This script ignores db.py / .env loading entirely — it reads URLs from cli args.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

try:
    import psycopg2
    import psycopg2.extras
    from psycopg2.extras import Json
except ImportError:
    print("psycopg2 not installed — running via system python3? Try /opt/homebrew/bin/python3.11", file=sys.stderr)
    sys.exit(1)


def _adapt(v):
    """psycopg2 doesn't auto-adapt dict/list to jsonb — wrap them in Json()."""
    if isinstance(v, (dict, list)):
        return Json(v)
    return v


TABLES = [
    # (table_name, pk_column, timestamp_column_for_cutover_filter)
    # FK parents FIRST so children can reference them.
    ("posts",                   "id",          "posted_at"),
    ("twitter_search_attempts", "id",          "ran_at"),
    ("reddit_search_attempts",  "id",          "ran_at"),
    ("replies",                 "id",          "discovered_at"),
    ("twitter_candidates",      "id",          "discovered_at"),
    ("reddit_candidates",       "id",          "discovered_at"),
    # claude_sessions optional — see --include-sessions
    ("claude_sessions",         "session_id",  "started_at"),
]


def get_columns(conn, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = %s AND table_schema = 'public'
            ORDER BY ordinal_position
            """,
            (table,),
        )
        return [r[0] for r in cur.fetchall()]


def get_missing_pks(neon, gcp, table: str, pk: str, ts_col: str, cutover_epoch: int) -> list:
    cutover_ts = datetime.fromtimestamp(cutover_epoch, tz=timezone.utc).isoformat()

    with neon.cursor() as cur:
        cur.execute(
            f"SELECT {pk} FROM {table} WHERE {ts_col} >= %s",
            (cutover_ts,),
        )
        neon_pks = {r[0] for r in cur.fetchall()}

    with gcp.cursor() as cur:
        cur.execute(
            f"SELECT {pk} FROM {table} WHERE {ts_col} >= %s",
            (cutover_ts,),
        )
        gcp_pks = {r[0] for r in cur.fetchall()}

    return sorted(neon_pks - gcp_pks, key=lambda v: (str(v)))


def backfill_table(neon, gcp, table: str, pk: str, ts_col: str, cutover_epoch: int, dry_run: bool) -> tuple[int, int]:
    print(f"\n=== {table} ({pk}, ts={ts_col}) ===")
    missing = get_missing_pks(neon, gcp, table, pk, ts_col, cutover_epoch)
    print(f"  missing in cloud sql: {len(missing)}")
    if not missing:
        return (0, 0)

    cols = get_columns(neon, table)
    col_list = ", ".join(cols)

    # Fetch the missing rows from Neon
    with neon.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        # Use ANY(array) for the missing PK set
        cur.execute(
            f"SELECT {col_list} FROM {table} WHERE {pk} = ANY(%s)",
            (missing,),
        )
        rows = cur.fetchall()

    print(f"  pulled rows: {len(rows)}")
    if dry_run:
        print(f"  DRY RUN — would insert {len(rows)} rows into {table}")
        return (len(rows), 0)

    # Insert into Cloud SQL with conflict handling
    placeholders = ", ".join(["%s"] * len(cols))
    insert_sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT ({pk}) DO NOTHING"
    inserted = 0
    with gcp.cursor() as cur:
        for row in rows:
            try:
                cur.execute(insert_sql, [_adapt(row[c]) for c in cols])
                inserted += cur.rowcount
            except Exception as exc:
                gcp.rollback()
                pk_val = row[pk]
                print(f"  ERROR on {pk}={pk_val}: {exc}")
                # Re-open transaction by re-executing — psycopg2 needs a clean tx after error
                continue
        gcp.commit()
    print(f"  inserted: {inserted}/{len(rows)}")
    return (len(rows), inserted)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--neon-url", required=True)
    parser.add_argument("--gcp-url", required=True)
    parser.add_argument("--cutover-epoch", type=int, required=True, help="unix epoch of .env mtime")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-sessions", action="store_true", help="also backfill claude_sessions")
    parser.add_argument("--only", help="comma-separated table names to limit to")
    args = parser.parse_args()

    neon = psycopg2.connect(args.neon_url)
    gcp  = psycopg2.connect(args.gcp_url)

    only = set(args.only.split(",")) if args.only else None
    print(f"Cutover: {datetime.fromtimestamp(args.cutover_epoch, tz=timezone.utc).isoformat()}")
    print(f"Dry run: {args.dry_run}")

    total_pulled = 0
    total_inserted = 0
    for table, pk, ts_col in TABLES:
        if only and table not in only:
            continue
        if table == "claude_sessions" and not args.include_sessions:
            print(f"\n=== {table}: SKIPPED (pass --include-sessions to include) ===")
            continue
        pulled, inserted = backfill_table(neon, gcp, table, pk, ts_col, args.cutover_epoch, args.dry_run)
        total_pulled += pulled
        total_inserted += inserted

    print(f"\n=== TOTAL: pulled={total_pulled} inserted={total_inserted} ===")
    neon.close()
    gcp.close()


if __name__ == "__main__":
    main()
