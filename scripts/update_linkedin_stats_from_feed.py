#!/usr/bin/env python3
"""Update LinkedIn engagement stats for OUR engagement-comments stored in `posts`.

This is the posts-table sibling of update_linkedin_comment_stats_from_feed.py.
Same feed JSON shape (produced by skill/stats-linkedin.sh via
scrape_linkedin_comment_stats.py); different DB target.

Why this exists separately from the replies-table updater:
  LinkedIn engagement-comments are stored in the `posts` table (Twitter
  parity: posts table holds top-level + reply rows alike, identified by
  the URL extracted from `our_url`). The legacy `replies` table holds an
  older sliver (~173 rows) from a previous pipeline shape and is updated
  by update_linkedin_comment_stats_from_feed.py. New rows land in `posts`.

Input JSON shape (one record per OUR comment visible on the activity tab,
virtualized list, partial coverage per fire is expected; identical to the
shape the replies-table updater consumes):
  [
    {
      "comment_id":  "7457492815716032512",
      "parent_kind": "ugcPost"  | "activity" | "share",
      "parent_id":   "7457485938131161088",
      "impressions": 156,
      "reactions":   7,
      "replies":     1
    },
    ...
  ]

Matching strategy:
  - Posts written 2026-05-11 onward have `our_url` containing
    `?commentUrn=urn:li:comment:(...,<comment_id>)` because
    linkedin_api.py:comment_on_post now embeds it (and reply_to_comment
    already did). We parse comment_id out of `our_url` and key on it.
  - Older rows (pre-2026-05-11) where the autoposter set
    `our_url = thread_url` (parent post URL only, no commentUrn) cannot
    be matched here and will silently miss. They are not backfillable
    without per-permalink scraping (the exact pattern that triggered
    the 2026-04-17 + 2026-05-05 LinkedIn lockouts), so we accept the
    loss. Going forward every new engagement-comment is captured.
  - The 97 pre-existing rows that already have `?commentUrn=` in
    `our_url` (replies-to-comments via reply_to_comment) work
    immediately.

Behavior:
  - Match each feed record by comment_id against posts.our_url's
    `commentUrn=` second-numeric-id field.
  - If matched: write upvotes (=reactions), comments_count (=replies),
    views (=impressions), engagement_updated_at = NOW(). Only overwrite
    a column when the new value is non-null.
  - Unmatched feed rows are logged but NOT errors (the same feed JSON
    is consumed by the replies-table updater immediately after this
    script, so a row unmatched here might match there).
  - scan_no_change_count IS maintained, matching stats.py's
    Twitter behavior so dashboard sorting / freshness gates work the
    same way across platforms.

Output (stdout) one line for stats.sh's extract_field to parse:
  LinkedInPosts: <T> total, <S> skipped, <C> checked,
                 <U> updated, <D> deleted, <E> errors

Usage:
  python3 scripts/update_linkedin_stats_from_feed.py \\
      --from-json /tmp/li-stats-feed.json \\
      [--summary  /tmp/li-stats-summary.json] \\
      [--dry-run] [--quiet]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_post  # noqa: E402


# `urn:li:comment:(urn:li:activity:<parent>,<comment_id>)`
# `urn:li:comment:(urn:li:ugcPost:<parent>,<comment_id>)`
# `urn:li:comment:(activity:<parent>,<comment_id>)`     ← bare-kind form
# `urn:li:comment:(ugcPost:<parent>,<comment_id>)`      ← bare-kind form
#
# Same lenient regex as update_linkedin_comment_stats_from_feed.py — the
# inner `urn:li:` prefix on the parent namespace is optional because both
# forms appear in our data depending on which posting path wrote the row.
COMMENT_URN_RE = re.compile(
    r"urn:li:comment:\((?:urn:li:)?(?P<kind>\w+):(?P<parent>\d+),(?P<cid>\d+)\)"
)


def extract_comment_id(our_url: Optional[str]) -> Optional[tuple[str, str, str]]:
    """Return (parent_kind, parent_id, comment_id) parsed from our_url, or None."""
    if not our_url:
        return None
    decoded = urllib.parse.unquote(our_url)
    m = COMMENT_URN_RE.search(decoded)
    if not m:
        return None
    return (m.group("kind"), m.group("parent"), m.group("cid"))


def load_feed(path: str) -> list[dict]:
    with open(path) as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raise ValueError(f"feed file must be a JSON array, got {type(raw).__name__}")
    out = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        cid = r.get("comment_id")
        if not cid:
            continue
        out.append({
            "comment_id":  str(cid),
            "parent_kind": r.get("parent_kind") or "",
            "parent_id":   str(r.get("parent_id") or ""),
            "impressions": r.get("impressions"),
            "reactions":   r.get("reactions"),
            "replies":     r.get("replies"),
        })
    return out


def load_engagement_comments() -> dict:
    """Return {comment_id: {id, our_url, upvotes, comments_count, views}}.

    Only includes LinkedIn `posts` rows where `our_url` carries a
    commentUrn (i.e., we can identify OUR comment, not just the parent
    thread). Status='active' OR 'removed' (removed rows still benefit
    from a final-stats read in case they come back).

    Migrated 2026-06-01 to GET /api/v1/linkedin-engagement-comments. The
    server returns every candidate row; the brittle commentUrn regex
    (extract_comment_id) stays single-sourced here in Python so the API
    surface never has to replicate it.
    """
    resp = api_get("/api/v1/linkedin-engagement-comments")
    rows = (resp.get("data") or {}).get("rows") or []
    out = {}
    for r in rows:
        parsed = extract_comment_id(r.get("our_url"))
        if not parsed:
            continue
        _, _, cid = parsed
        out[cid] = {
            "id":             r["id"],
            "our_url":        r["our_url"],
            "upvotes":        int(r.get("upvotes") or 0),
            "comments_count": int(r.get("comments_count") or 0),
            "views":          int(r.get("views") or 0),
        }
    return out


def compute_one(db_row: dict, feed: dict, dry_run: bool, quiet: bool) -> dict:
    """Compute the update for one feed record against one DB row.

    Returns {post_id, upvotes, comments_count, views, changed}. Only
    overwrites a column when the feed value is non-null (preserves last
    known value for fresh comments that don't yet have impressions
    computed by LinkedIn). The actual write (UPDATE posts +
    scan_no_change_count maintenance + post_views_daily snapshot) happens
    server-side when the batch is POSTed.
    """
    new_rxn = feed["reactions"]
    new_imp = feed["impressions"]
    new_rep = feed["replies"]

    next_upv = db_row["upvotes"]        if new_rxn is None else int(new_rxn)
    next_cmt = db_row["comments_count"] if new_rep is None else int(new_rep)
    next_vws = db_row["views"]          if new_imp is None else int(new_imp)

    changed = (
        next_upv != db_row["upvotes"]
        or next_cmt != db_row["comments_count"]
        or next_vws != db_row["views"]
    )

    if not quiet:
        tag = "UPDATED" if changed else "same"
        if dry_run:
            tag = f"DRY-{tag}"
        print(
            f"  [{db_row['id']:>6}] cid={feed['comment_id']:>20s} "
            f"upv {db_row['upvotes']}->{next_upv}  "
            f"cmt {db_row['comments_count']}->{next_cmt}  "
            f"views {db_row['views']}->{next_vws}  [{tag}]",
            flush=True,
        )

    return {
        "post_id":        db_row["id"],
        "upvotes":        next_upv,
        "comments_count": next_cmt,
        "views":          next_vws,
        "changed":        changed,
    }


def run(from_json: str,
        summary_path: Optional[str],
        dry_run: bool,
        quiet: bool) -> dict:
    feed = load_feed(from_json)
    if not feed:
        return {
            "ok": True,
            "total": 0, "skipped": 0, "checked": 0,
            "updated": 0, "deleted": 0, "errors": 0,
            "note": "empty_feed",
        }

    dbmod.load_env()
    db = dbmod.get_conn()
    try:
        posts_by_cid = load_engagement_comments(db)
        if not quiet:
            print(
                f"[stats] feed_rows={len(feed)} db_posts_w_commentUrn={len(posts_by_cid)}",
                flush=True,
            )

        updated = 0
        unchanged = 0
        unmatched = []
        errors = 0

        for fr in feed:
            row = posts_by_cid.get(fr["comment_id"])
            if row is None:
                unmatched.append(fr["comment_id"])
                continue
            try:
                outcome = apply_one(db, row, fr, dry_run=dry_run, quiet=quiet)
            except Exception as e:
                errors += 1
                if not quiet:
                    print(f"  ERROR id={row['id']} {e}", flush=True)
                continue
            if outcome == "updated":
                updated += 1
            elif outcome == "unchanged":
                unchanged += 1

        if not dry_run:
            db.commit()

        total   = len(feed)
        checked = updated + unchanged
        skipped = len(unmatched)
        deleted = 0

        result = {
            "ok": True,
            "total":     total,
            "skipped":   skipped,
            "checked":   checked,
            "updated":   updated,
            "unchanged": unchanged,
            "deleted":   deleted,
            "errors":    errors,
            "unmatched": unmatched,
        }

        if summary_path:
            try:
                with open(summary_path, "w") as f:
                    json.dump({
                        "refreshed":   updated,
                        "removed":     deleted,
                        "unavailable": 0,
                        "not_found":   len(unmatched),
                    }, f)
            except Exception as e:
                print(
                    f"WARN: failed to write summary {summary_path}: {e}",
                    file=sys.stderr,
                )

        return result
    finally:
        db.close()


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Apply LinkedIn engagement-comment readings to the posts table."
        )
    )
    p.add_argument("--from-json", required=True,
                   help="Path to JSON produced by scrape_linkedin_comment_stats.py.")
    p.add_argument("--summary", default=None,
                   help="Path to write {refreshed,removed,unavailable,not_found} sidecar.")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute updates but do not write to DB.")
    p.add_argument("--quiet", action="store_true", help="Minimal output.")
    args = p.parse_args()

    try:
        result = run(args.from_json, args.summary, args.dry_run, args.quiet)
    except Exception as e:
        print(json.dumps({"ok": False, "error": "fatal", "detail": str(e)}),
              file=sys.stderr)
        sys.exit(1)

    if not result.get("ok"):
        print(json.dumps(result, indent=2), file=sys.stderr)
        sys.exit(1)

    print(
        f"LinkedInPosts: {result['total']} total, "
        f"{result['skipped']} skipped, "
        f"{result['checked']} checked, "
        f"{result['updated']} updated, "
        f"{result['deleted']} deleted, "
        f"{result['errors']} errors"
    )


if __name__ == "__main__":
    main()
