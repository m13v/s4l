#!/usr/bin/env python3
"""Backfill: move placeholder 'mention' rows out of `posts` into `mentions`.

Background
----------
Prior to 2026-05-23, scripts/scan_twitter_mentions_browser.py inserted
a row into `posts` for every third-party tweet that mentioned us, with
  our_content = '(mention - no original post)'
  our_url    = <third-party tweet URL>
That URL then got scraped by update_stats.py which wrote *the other
person's* view count into posts.views, polluting every dashboard metric.

This script:
  1. INSERTs each placeholder row into the new `mentions` table.
  2. Repoints existing replies (replies.post_id = <legacy_post>) to point
     at the new mention via replies.mention_id, NULLing out post_id.
  3. (When --commit-delete) deletes the legacy placeholder rows from posts
     and applies a forward-going CHECK constraint blocking re-insertion.

Idempotent: re-running without --commit-delete is safe; the ON CONFLICT
DO NOTHING + PRIMARY KEY on the map table prevent duplicate work.

Usage:
    python3 scripts/migrate_mentions_out_of_posts.py
    python3 scripts/migrate_mentions_out_of_posts.py --commit-delete
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg2

PLACEHOLDER = "(mention - no original post)"


def _resolve_db_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    env_path = os.path.expanduser("~/social-autoposter/.env")
    if os.path.exists(env_path):
        with open(env_path) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("DATABASE_URL="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("DATABASE_URL not found in env or ~/social-autoposter/.env")


def _conn():
    return psycopg2.connect(_resolve_db_url())


def backfill(conn) -> dict[str, int]:
    """Phase 1: insert into mentions + build map + repoint replies. Idempotent."""
    counts: dict[str, int] = {}
    with conn:
        with conn.cursor() as cur:
            # Persistent map table (not TEMP) so rollback / re-run can read it
            # and we have an audit trail of which post_id became which mention_id.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS mentions_legacy_post_id_map (
                    old_post_id    INT PRIMARY KEY,
                    new_mention_id INT NOT NULL
                );
                """
            )

            # Insert into mentions. ON CONFLICT DO NOTHING handles re-runs.
            # NOTE: posts.thread_url for these rows IS the third-party tweet URL
            # (the scanner copied tweet_url into both thread_url and our_url).
            cur.execute(
                """
                INSERT INTO mentions (
                    platform, mentioning_url, mentioning_handle, mentioning_text,
                    our_handle, project_name, status, discovered_at, install_id
                )
                SELECT
                    'twitter',
                    p.thread_url,
                    p.thread_author,
                    p.thread_title,
                    p.our_account,
                    p.project_name,
                    COALESCE(p.status, 'active'),
                    COALESCE(p.posted_at, NOW()),
                    p.install_id
                FROM posts p
                WHERE p.our_content = %s
                  AND p.thread_url IS NOT NULL
                ON CONFLICT (platform, mentioning_url) DO NOTHING
                """,
                (PLACEHOLDER,),
            )
            counts["mentions_inserted_or_existing"] = cur.rowcount

            # Populate map: every legacy placeholder post -> mention row that
            # shares its (platform=twitter, mentioning_url=thread_url).
            cur.execute(
                """
                INSERT INTO mentions_legacy_post_id_map (old_post_id, new_mention_id)
                SELECT p.id, m.id
                  FROM posts p
                  JOIN mentions m
                    ON m.mentioning_url = p.thread_url
                   AND m.platform = 'twitter'
                 WHERE p.our_content = %s
                   AND p.thread_url IS NOT NULL
                ON CONFLICT (old_post_id) DO NOTHING
                """,
                (PLACEHOLDER,),
            )
            counts["map_rows_added"] = cur.rowcount

            cur.execute("SELECT COUNT(*) FROM mentions_legacy_post_id_map")
            counts["map_total"] = cur.fetchone()[0]

            # Repoint replies.post_id -> replies.mention_id via the map.
            # The CHECK constraint enforces mutual exclusion, so we NULL out
            # post_id in the same UPDATE.
            cur.execute(
                """
                UPDATE replies r
                   SET mention_id = mlm.new_mention_id,
                       post_id    = NULL
                  FROM mentions_legacy_post_id_map mlm
                 WHERE r.post_id = mlm.old_post_id
                """
            )
            counts["replies_repointed"] = cur.rowcount

            cur.execute(
                "SELECT COUNT(*) FROM replies WHERE mention_id IS NOT NULL"
            )
            counts["replies_with_mention_id_total"] = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM mentions")
            counts["mentions_total"] = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(*) FROM posts WHERE our_content = %s",
                (PLACEHOLDER,),
            )
            counts["posts_placeholder_remaining"] = cur.fetchone()[0]
    return counts


def commit_delete(conn) -> dict[str, int]:
    """Phase 2: delete the legacy posts rows + apply CHECK constraint."""
    counts: dict[str, int] = {}
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM posts
                 WHERE id IN (SELECT old_post_id FROM mentions_legacy_post_id_map)
                """
            )
            counts["posts_deleted"] = cur.rowcount

            cur.execute(
                "SELECT COUNT(*) FROM posts WHERE our_content = %s",
                (PLACEHOLDER,),
            )
            counts["posts_placeholder_remaining"] = cur.fetchone()[0]
            if counts["posts_placeholder_remaining"] != 0:
                raise SystemExit(
                    "ABORT: posts placeholder rows still present after delete; "
                    "CHECK constraint NOT applied. Investigate before re-running."
                )

            # Defense-in-depth: future-proof CHECK constraint. NOT VALID +
            # VALIDATE so the validation pass uses a non-blocking lock.
            cur.execute(
                """
                ALTER TABLE posts
                    DROP CONSTRAINT IF EXISTS posts_no_mention_placeholders_check
                """
            )
            cur.execute(
                """
                ALTER TABLE posts
                    ADD CONSTRAINT posts_no_mention_placeholders_check
                    CHECK (our_content <> '(mention - no original post)') NOT VALID
                """
            )
            cur.execute(
                "ALTER TABLE posts VALIDATE CONSTRAINT posts_no_mention_placeholders_check"
            )
            counts["check_constraint_applied"] = 1
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit-delete",
        action="store_true",
        help="After backfill, delete legacy placeholder rows from posts and apply the CHECK constraint.",
    )
    args = parser.parse_args()

    conn = _conn()
    try:
        backfill_counts = backfill(conn)
    finally:
        conn.close()
    print("=== Backfill phase ===")
    for k, v in backfill_counts.items():
        print(f"  {k:38s} = {v}")

    if not args.commit_delete:
        print("\nNo --commit-delete; legacy placeholder rows remain in posts.")
        print("Re-run with --commit-delete to delete them and apply CHECK constraint.")
        return

    # Sanity gate: every placeholder must be mapped before we delete.
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM posts p
                  LEFT JOIN mentions_legacy_post_id_map mlm
                    ON mlm.old_post_id = p.id
                 WHERE p.our_content = %s
                   AND mlm.old_post_id IS NULL
                """,
                (PLACEHOLDER,),
            )
            unmapped = cur.fetchone()[0]
    finally:
        conn.close()
    if unmapped > 0:
        print(
            f"ABORT: {unmapped} placeholder rows have no mapping; "
            "investigate before --commit-delete."
        )
        sys.exit(2)

    conn = _conn()
    try:
        delete_counts = commit_delete(conn)
    finally:
        conn.close()
    print("\n=== Commit-delete phase ===")
    for k, v in delete_counts.items():
        print(f"  {k:38s} = {v}")


if __name__ == "__main__":
    main()
