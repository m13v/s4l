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
from http_api import api_get, api_post


def log(msg, quiet=False):
    if not quiet:
        print(msg)


def _load_canonical_style_names(quiet=False):
    """Return the set of allowlisted engagement_style names.

    Union of the hardcoded STYLES dict + the registry (seed + model_invented +
    human_derived rows). Used to gate what we write to posts.engagement_style:
    Claude sometimes stamps caption-style metadata with non-canonical labels
    (e.g. 'studyly-rescue-arc') that never went through validate_or_register
    on the IG render path. We refuse to mirror those into posts so they don't
    pollute the dashboard's engagement-style A/B picker.
    """
    names = set()
    try:
        from engagement_styles import get_all_styles
        names.update((get_all_styles() or {}).keys())
    except Exception as e:
        log(f"[sync] WARNING — could not load canonical styles: {e!r}", quiet)
    return names


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap rows processed (for testing).")
    args = parser.parse_args()

    # 2026-05-25: gate posts.engagement_style writes against the canonical
    # registry. IG renders pre-pick a style (run-instagram-render.sh) and ask
    # Claude to stamp metadata.engagement_style=<picked>, but Claude has been
    # observed writing caption_style/description_style with off-list labels
    # (e.g. 'studyly-rescue-arc') instead. Mirroring those into posts.* lets
    # them pollute the engagement_style A/B picker. We mirror NULL for any
    # value not in the canonical set; the orphan label remains in
    # media_posts.metadata for forensics.
    canonical_styles = _load_canonical_style_names(args.quiet)

    # NB: db._translate_sql blindly rewrites `?` -> `%s`, so we can't use the
    # JSONB `?` exists operator. Use ->'key' IS NOT NULL instead.
    sql = """
        SELECT id, post_number, target_account, project_name, caption_text,
               posted_at, posted_urls, metadata
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
        metadata = r["metadata"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        elif metadata is None:
            metadata = {}
        engagement_style = metadata.get("engagement_style") or metadata.get("caption_style")
        if engagement_style and canonical_styles and engagement_style not in canonical_styles:
            log(f"[sync] WARNING — dropping non-canonical engagement_style "
                f"{engagement_style!r} for post-{r['post_number']} "
                f"({r['target_account']}); mirroring NULL", args.quiet)
            engagement_style = None
        db.execute(
            """
            INSERT INTO posts (
                platform, thread_url, our_url, our_content, our_account,
                posted_at, project_name, status, autoposter_version,
                engagement_style
            )
            VALUES (
                'instagram', %s, %s, %s, %s, %s, %s, 'active', 'ig-sync-v1', %s
            )
            """,
            (
                ig_url,                                # thread_url = our_url
                ig_url,                                # our_url
                r["caption_text"] or "",               # our_content
                r["target_account"] or "matt_diak",    # our_account
                r["posted_at"],
                r["project_name"],
                engagement_style,
            ),
        )
        inserted += 1
        log(f"[sync] inserted post-{r['post_number']} ({r['target_account']}) -> {ig_url}", args.quiet)

    db.commit()
    db.close()
    log(f"[sync] done: inserted={inserted} skipped_existing={skipped} total_scanned={len(rows)}", args.quiet)


if __name__ == "__main__":
    main()
