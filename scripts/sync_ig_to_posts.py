#!/usr/bin/env python3
"""Mirror posted Instagram rows from media_posts -> posts.

media_posts holds the IG-only fields (video_path, post_type, target_account,
overlays, source_clips, composition_id). posts holds the platform-agnostic
fields the dashboard reads (platform, our_url, our_content, our_account,
posted_at, upvotes, comments_count, views, engagement_updated_at).

This script copies the dashboard-essential fields across so the existing
dashboard surfaces (Trends, Top, Activity, Stats by Engagement Style, Cohort)
treat Instagram identically to Reddit/Twitter/LinkedIn.

Idempotent: skips rows already mirrored (matched on platform='instagram' AND
our_url=<IG permalink>). Safe to rerun. Called at end of run-instagram-daily.sh
so new posts mirror immediately, and once on-demand for backfill.

Usage:
    python3 scripts/sync_ig_to_posts.py [--quiet] [--limit N]
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod


def log(msg, quiet=False):
    if not quiet:
        print(msg)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap rows processed (for testing).")
    args = parser.parse_args()

    db = dbmod.get_conn()

    # NB: db._translate_sql blindly rewrites `?` -> `%s`, so we can't use the
    # JSONB `?` exists operator. Use ->'key' IS NOT NULL instead.
    sql = """
        SELECT id, post_number, target_account, project_name, caption_text,
               posted_at, posted_urls
        FROM media_posts
        WHERE status = 'posted'
          AND posted_urls -> 'instagram' IS NOT NULL
        ORDER BY posted_at ASC
    """
    rows = db.execute(sql).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    log(f"[sync] media_posts: {len(rows)} posted IG rows", args.quiet)

    inserted = 0
    skipped = 0
    for r in rows:
        posted_urls = r["posted_urls"]
        if isinstance(posted_urls, str):
            posted_urls = json.loads(posted_urls)
        ig_url = posted_urls.get("instagram")
        if not ig_url:
            continue

        existing = db.execute(
            "SELECT id FROM posts WHERE platform='instagram' AND our_url=%s",
            (ig_url,),
        ).fetchone()
        if existing:
            skipped += 1
            continue

        # thread_url is NOT NULL; for original posts we self-reference
        # (established pattern, 2,124 rows across other platforms).
        db.execute(
            """
            INSERT INTO posts (
                platform, thread_url, our_url, our_content, our_account,
                posted_at, project_name, status, autoposter_version
            )
            VALUES (
                'instagram', %s, %s, %s, %s, %s, %s, 'active', 'ig-sync-v1'
            )
            """,
            (
                ig_url,                                # thread_url = our_url
                ig_url,                                # our_url
                r["caption_text"] or "",               # our_content
                r["target_account"] or "matt_diak",    # our_account
                r["posted_at"],
                r["project_name"],
            ),
        )
        inserted += 1
        log(f"[sync] inserted post-{r['post_number']} ({r['target_account']}) -> {ig_url}", args.quiet)

    db.commit()
    db.close()
    log(f"[sync] done: inserted={inserted} skipped_existing={skipped} total_scanned={len(rows)}", args.quiet)


if __name__ == "__main__":
    main()
