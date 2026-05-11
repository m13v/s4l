#!/usr/bin/env python3
"""Update LinkedIn engagement stats for OUR comments (not the parent posts).

Reads a JSON file produced by skill/stats-linkedin-comments.sh (Claude-driven
MCP linkedin-agent) containing per-comment engagement readings extracted in
ONE in-page DOM harvest from /in/me/recent-activity/comments/. Applies them
to the `replies` table.

Why this exists separately from update_linkedin_stats_from_feed.py:
  Posts and replies are different tables with different keys. Posts use
  activity URN; replies use the second numeric ID inside
  urn:li:comment:(<parent>:<id>,<comment_id>) extracted from
  our_reply_url's commentUrn= query parameter. We match on comment_id.

Input JSON shape (one record per OUR comment, virtualized list, partial
coverage per fire is expected):
  [
    {
      "comment_id":  "7457492815716032512",
      "parent_kind": "ugcPost"   | "activity",
      "parent_id":   "7457485938131161088",
      "impressions": 156,        // null if not yet computed
      "reactions":   7,          // 0 means LinkedIn omitted the counter
      "replies":     1
    },
    ...
  ]

Behavior:
  - Match each feed record by comment_id against
    replies.our_reply_url's commentUrn= second-numeric-id field.
    LinkedIn ID space guarantees comment_id uniqueness, so we don't
    need to also match on parent.
  - If matched: write upvotes (=reactions), comments_count (=replies),
    views (=impressions), engagement_updated_at = NOW(). Only overwrite a
    column when the new value is non-null (LinkedIn shows null impressions
    for very fresh comments; preserve last known reading).
  - Unmatched feed rows logged, no write.
  - We do NOT bump a 'no-change' counter for replies that were not in the
    feed this fire — virtualization on the /comments/ page makes
    "absence" non-informative. Coverage accrues across fires.

Usage:
  python3 scripts/update_linkedin_comment_stats_from_feed.py \\
      --from-json /tmp/li-comments-feed.json \\
      [--summary  /tmp/li-comments-summary.json] \\
      [--dry-run] [--quiet]

Output (stdout): one summary line stats.sh's extract_field can parse:
  LinkedInComments: <T> total, <S> skipped, <C> checked,
                    <U> updated, <D> deleted, <E> errors
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
import db as dbmod  # noqa: E402


# `urn:li:comment:(urn:li:activity:<parent>,<comment_id>)`
# `urn:li:comment:(urn:li:ugcPost:<parent>,<comment_id>)`
# `urn:li:comment:(activity:<parent>,<comment_id>)`        ← bare-kind form
# `urn:li:comment:(ugcPost:<parent>,<comment_id>)`         ← bare-kind form
# After URL-decoding our_reply_url, we extract the SECOND numeric run as
# comment_id. Take it from inside the parens to avoid catching activity
# IDs that LinkedIn also embeds in the same URL's path.
#
# LinkedIn stores the inner URN in two interchangeable formats. As of 2026,
# the link-share endpoint (used by our reply pipeline) returns the bare-kind
# form (no `urn:li:` prefix on the parent), while older / Voyager-derived
# URLs use the fully-qualified form. The `(?:urn:li:)?` non-capturing group
# matches both. Before this fix, 95/173 of our LinkedIn replies never got
# stats updated because their our_reply_url was the bare form (2026-05-11).
COMMENT_URN_RE = re.compile(
    r"urn:li:comment:\((?:urn:li:)?(?P<kind>\w+):(?P<parent>\d+),(?P<cid>\d+)\)"
)


def extract_comment_id(our_reply_url: Optional[str]) -> Optional[tuple[str, str, str]]:
    """Return (parent_kind, parent_id, comment_id) or None."""
    if not our_reply_url:
        return None
    decoded = urllib.parse.unquote(our_reply_url)
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


def load_active_replies(db) -> dict:
    """Return {comment_id: {id, our_reply_url, upvotes, comments_count, views}}.

    Includes status='replied' (the canonical posted state for replies) AND
    'posted' as a forward-compat alias. Only LinkedIn replies whose URL we
    can parse a comment_id out of are returned; the rest are noise we
    can't ever match against the feed.
    """
    cur = db.execute(
        "SELECT id, our_reply_url, "
        "       COALESCE(upvotes, 0)        AS upvotes, "
        "       COALESCE(comments_count, 0) AS comments_count, "
        "       COALESCE(views, 0)          AS views "
        "FROM replies "
        "WHERE platform='linkedin' "
        "  AND status IN ('replied', 'posted') "
        "  AND our_reply_url IS NOT NULL "
        "  AND our_reply_url ~ 'commentUrn'"
    )
    out = {}
    for r in cur.fetchall():
        parsed = extract_comment_id(r["our_reply_url"])
        if not parsed:
            continue
        _, _, cid = parsed
        out[cid] = {
            "id":             r["id"],
            "our_reply_url":  r["our_reply_url"],
            "upvotes":        int(r["upvotes"] or 0),
            "comments_count": int(r["comments_count"] or 0),
            "views":          int(r["views"] or 0),
        }
    return out


def apply_one(db, db_row: dict, feed: dict, dry_run: bool, quiet: bool) -> str:
    """Apply one feed record to one DB row.

    Returns 'updated' if any tracked field changed value, else 'unchanged'.
    Only overwrites a column when the feed value is non-null.
    """
    new_rxn = feed["reactions"]
    new_imp = feed["impressions"]
    new_rep = feed["replies"]

    # Decide what to write.
    next_upv = db_row["upvotes"]        if new_rxn is None else int(new_rxn)
    next_cmt = db_row["comments_count"] if new_rep is None else int(new_rep)
    next_vws = db_row["views"]          if new_imp is None else int(new_imp)

    changed = (
        next_upv != db_row["upvotes"]
        or next_cmt != db_row["comments_count"]
        or next_vws != db_row["views"]
    )

    if not dry_run:
        # engagement_updated_at always advances when we look at the row.
        db.execute(
            "UPDATE replies SET "
            "   upvotes               = %s, "
            "   comments_count        = %s, "
            "   views                 = %s, "
            "   engagement_updated_at = NOW() "
            "WHERE id = %s",
            [next_upv, next_cmt, next_vws, db_row["id"]],
        )

    if not quiet:
        tag = "UPDATED" if changed else "same"
        if dry_run:
            tag = f"DRY-{tag}"
        print(
            f"  [{db_row['id']}] cid={feed['comment_id']:>20s} "
            f"upv {db_row['upvotes']}->{next_upv}  "
            f"cmt {db_row['comments_count']}->{next_cmt}  "
            f"views {db_row['views']}->{next_vws}  [{tag}]",
            flush=True,
        )

    return "updated" if changed else "unchanged"


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
        replies_by_cid = load_active_replies(db)
        if not quiet:
            print(
                f"[stats] feed_rows={len(feed)} db_replies={len(replies_by_cid)}",
                flush=True,
            )

        updated = 0
        unchanged = 0
        unmatched = []
        errors = 0

        for fr in feed:
            row = replies_by_cid.get(fr["comment_id"])
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
        deleted = 0  # not detectable from this surface

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
            "Apply LinkedIn-comments engagement readings to the replies table."
        )
    )
    p.add_argument("--from-json", required=True,
                   help="Path to JSON produced by stats-linkedin-comments.sh.")
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
        f"LinkedInComments: {result['total']} total, "
        f"{result['skipped']} skipped, "
        f"{result['checked']} checked, "
        f"{result['updated']} updated, "
        f"{result['deleted']} deleted, "
        f"{result['errors']} errors"
    )


if __name__ == "__main__":
    main()
