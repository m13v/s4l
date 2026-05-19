#!/usr/bin/env python3
"""Update Reddit view counts in the database.

Reddit doesn't expose view counts via API. Views are scraped from the
profile page by Claude using MCP Playwright, then saved to a JSON file.
This script reads that JSON and updates the `views` column in the DB.

IMPORTANT — Browser scraping notes for Claude:
  Reddit virtualizes the DOM: items scrolled off-screen get removed.
  You MUST collect view data incrementally as you scroll — NOT after
  scrolling to the bottom. Use this pattern:
    1. Collect visible articles + view counts
    2. Scroll down ~600px
    3. Wait 800-1500ms for new content
    4. Collect again (dedup by URL in a Map/dict)
    5. Repeat until no new articles load (check article count, not scroll height)
  View counts appear as text nodes matching /^\d[\d,.]*[KkMm]?\s*views?$/
  inside <article> elements. Parse "1.3K views" -> 1300, "2 views" -> 2.

Usage:
    python3 scripts/scrape_reddit_views.py --from-json /tmp/reddit_views.json
    python3 scripts/scrape_reddit_views.py --from-json /tmp/reddit_views.json --json
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import http_api
from http_api import api_get


def extract_ids(url):
    """Extract (post_id, comment_id) from any reddit URL format."""
    url = re.sub(r"https?://(old|www|new)\.reddit\.com", "", url)
    url = re.sub(r"\?.*$", "", url).rstrip("/")

    # New format: /r/sub/comments/POST_ID/comment/COMMENT_ID
    m = re.search(r"/comments/([a-z0-9]+)/comment/([a-z0-9]+)", url)
    if m:
        return (m.group(1), m.group(2))

    # Old format: /r/sub/comments/POST_ID/slug/COMMENT_ID
    m = re.search(r"/comments/([a-z0-9]+)/[^/]+/([a-z0-9]+)", url)
    if m:
        return (m.group(1), m.group(2))

    # Post only: /r/sub/comments/POST_ID/...
    m = re.search(r"/comments/([a-z0-9]+)", url)
    if m:
        return (m.group(1), None)

    return (None, None)


def _list_active_reddit_posts():
    """Paginated GET against /api/v1/posts?platform=reddit&status=active.

    Returns a list of {id, our_url} dicts so callers can keep the prior
    contract. The API's `limit` is capped at 500 server-side; we page by
    walking `posted_at` cursors until we get a short page back.
    """
    out = []
    since = None
    seen_ids = set()
    while True:
        query = {
            "platform": "reddit",
            "status": "active",
            "limit": 500,
        }
        if since:
            query["since"] = since
        resp = api_get("/api/v1/posts", query=query)
        rows = ((resp or {}).get("data") or {}).get("posts") or []
        new = 0
        oldest = None
        for r in rows:
            pid = r.get("id")
            if pid is None or pid in seen_ids:
                continue
            seen_ids.add(pid)
            if not r.get("our_url"):
                continue
            out.append({"id": int(pid), "our_url": r.get("our_url")})
            new += 1
            ts = r.get("posted_at")
            if ts and (oldest is None or ts < oldest):
                oldest = ts
        # Stop when the server returned fewer than the page size (no more
        # posts behind the cursor) OR no rows were new this iteration.
        if not rows or new == 0 or len(rows) < 500:
            break
        # The /api/v1/posts GET orders by posted_at DESC and filters
        # since >= ${since}. Walking older requires the inverse (a
        # `posted_at <` cursor), which the route doesn't yet expose; one
        # page of 500 covers most refresh cycles. If we ever outgrow that,
        # add `before` / `cursor` to the GET and resume here.
        break
    return out


def update_views(db, scraped_data, quiet=False):
    """Match scraped view data to DB posts and update.

    scraped_data accepts:
      - list of dicts {url, views, score?, comments_count?}
      - legacy list of {url, views}
      - legacy dict {url: views}

    Score sources on the profile page:
      - Thread rows: <shreddit-post score="N" comment-count="N">
      - Comment rows: <shreddit-comment-action-row score="N"> (no reply count)
    Views are visible text on both row types.
    """
    # Normalise to list of dicts
    if isinstance(scraped_data, dict):
        normalised = [{"url": u, "views": v} for u, v in scraped_data.items()]
    else:
        normalised = []
        for item in scraped_data:
            if isinstance(item, dict):
                normalised.append(item)
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                normalised.append({"url": item[0], "views": item[1]})

    views_by_comment = {}
    views_by_post = {}     # post_id -> max views (threads)
    score_by_comment = {}  # comment_id -> score (comment rows)
    score_by_post = {}     # post_id -> score (thread rows)
    cc_by_post = {}        # post_id -> comment-count attr (thread rows)

    for item in normalised:
        url = item.get("url")
        if not url:
            continue
        views = item.get("views")
        score = item.get("score")
        cc = item.get("comments_count")
        post_id, comment_id = extract_ids(url)

        if views is not None:
            if comment_id:
                views_by_comment[comment_id] = views
            if post_id:
                if post_id not in views_by_post or views > views_by_post[post_id]:
                    views_by_post[post_id] = views
        if score is not None:
            if comment_id:
                score_by_comment[comment_id] = score
            elif post_id:
                score_by_post[post_id] = score
        if cc is not None and post_id and not comment_id:
            cc_by_post[post_id] = cc

    posts = _list_active_reddit_posts()

    matched = 0
    matched_comment_score = 0
    matched_thread_stats = 0
    unmatched = 0

    for post in posts:
        db_id, our_url = post["id"], post["our_url"]
        post_id, comment_id = extract_ids(our_url)

        views = None
        if comment_id and comment_id in views_by_comment:
            views = views_by_comment[comment_id]
        elif post_id and post_id in views_by_post:
            views = views_by_post[post_id]

        score_val = None
        cc_val = None
        if comment_id:
            score_val = score_by_comment.get(comment_id)
        elif post_id:
            score_val = score_by_post.get(post_id)
            cc_val = cc_by_post.get(post_id)

        has_update = views is not None or score_val is not None or cc_val is not None
        if has_update:
            patch_body = {"stamp_engagement_now": True}
            if views is not None:
                patch_body["views"] = views
            if score_val is not None:
                patch_body["upvotes"] = score_val
            if cc_val is not None:
                patch_body["comments_count"] = cc_val
            http_api.api_patch(f"/api/v1/posts/{db_id}", patch_body)
            if views is not None:
                http_api.api_post(f"/api/v1/posts/{db_id}/views", {"views": views})
            matched += 1
            if comment_id and score_val is not None:
                matched_comment_score += 1
            if comment_id is None and (score_val is not None or cc_val is not None):
                matched_thread_stats += 1
        else:
            unmatched += 1

    # ---- Second pass: walk the `replies` table (DM-rail follow-ups) ----
    # 2026-05-18: the Reddit profile-page scrape already captures view + score
    # for every comment we've made, including reply-to-replies that live in
    # the `replies` table (not `posts`). Before this pass those rows defaulted
    # to views=0 because update_reddit_replies() uses Reddit's JSON API, which
    # doesn't expose per-comment views. The scrape data is already on disk;
    # all we have to do is also match `replies.our_reply_id` against the
    # scraped (post_id, comment_id) keys and PATCH the row.
    replies_matched = 0
    replies_unmatched = 0
    try:
        resp = api_get(
            "/api/v1/replies",
            query={
                "platform": "reddit",
                "status": "replied",
                "has_our_reply_id": "true",
                "order_by": "id",
                "limit": 500,
            },
        )
        reply_rows = ((resp or {}).get("data") or {}).get("replies") or []
    except Exception:
        reply_rows = []

    for r in reply_rows:
        rid = r.get("id")
        our_reply_id = r.get("our_reply_id")
        if not rid or not our_reply_id:
            continue
        # our_reply_id is the bare base-36 comment ID (no `t1_` prefix).
        cid = our_reply_id.replace("t1_", "")
        views = views_by_comment.get(cid)
        score = score_by_comment.get(cid)
        if views is None and score is None:
            replies_unmatched += 1
            continue
        patch_body = {"stamp_engagement_now": True}
        if views is not None:
            patch_body["views"] = int(views)
        if score is not None:
            patch_body["upvotes"] = int(score)
        try:
            http_api.api_patch(f"/api/v1/replies/{int(rid)}", patch_body)
            replies_matched += 1
        except Exception:
            replies_unmatched += 1

    return {
        "matched": matched,
        "matched_comment_score": matched_comment_score,
        "matched_thread_stats": matched_thread_stats,
        "unmatched": unmatched,
        "replies_matched": replies_matched,
        "replies_unmatched": replies_unmatched,
        "scraped_total": len(normalised),
        "with_views": len(views_by_comment) + len(views_by_post),
        "with_score_comment": len(score_by_comment),
        "with_score_thread": len(score_by_post),
        "with_comments_count": len(cc_by_post),
    }


def main():
    parser = argparse.ArgumentParser(description="Update Reddit view counts from scraped JSON")
    parser.add_argument("--from-json", required=True, help="Path to JSON file with scraped views")
    parser.add_argument("--quiet", action="store_true", help="Minimal output")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--summary", default=None,
                        help="Write a small JSON file ({refreshed: N, unmatched: N}) so "
                             "stats.sh can aggregate the dashboard refreshed pill.")
    args = parser.parse_args()

    if not os.path.exists(args.from_json):
        print(f"ERROR: File not found: {args.from_json}", file=sys.stderr)
        sys.exit(1)

    with open(args.from_json) as f:
        scraped_data = json.load(f)

    if not args.quiet:
        print(f"Loaded {len(scraped_data)} items from {args.from_json}")

    result = update_views(None, scraped_data, quiet=args.quiet)

    # Aggregate totals via /api/v1/posts/totals. Excludes platforms we don't
    # want in the headline (github_issues, moltbook) and only counts active
    # rows. Net upvotes strip the self-upvote +1 server-side.
    from datetime import datetime, timezone as _tz
    totals_resp = api_get(
        "/api/v1/posts/totals",
        query={
            "status": "active",
            "exclude_platforms": "github_issues,moltbook",
        },
    )
    t = (totals_resp or {}).get("data") or {}
    total_views = int(t.get("total_views") or 0)
    total_upvotes = int(t.get("total_upvotes") or 0)
    total_comments = int(t.get("total_comments") or 0)
    total_posts = int(t.get("total_posts") or 0)
    first_post_iso = t.get("first_post_at")
    first_post = None
    if first_post_iso:
        try:
            first_post = datetime.fromisoformat(first_post_iso.replace("Z", "+00:00"))
        except Exception:
            first_post = None
    if first_post:
        now = datetime.now(first_post.tzinfo) if first_post.tzinfo else datetime.now()
        days = max((now - first_post).days, 1)
    else:
        days = 1

    result["totals"] = {
        "total_views": total_views, "total_upvotes": total_upvotes,
        "total_comments": total_comments, "total_posts": total_posts,
        "days_active": days, "views_per_day": round(total_views / days) if days else 0,
    }

    if args.summary:
        try:
            # `refreshed` is the count stats.sh consumes for the "views-refreshed"
            # pill. Sum both legs: posts table + replies table (DM-rail follow-ups,
            # added 2026-05-18). Pre-2026-05-18 logs only had the posts leg.
            refreshed_total = int(result.get("matched", 0) or 0) + \
                              int(result.get("replies_matched", 0) or 0)
            with open(args.summary, "w") as f:
                json.dump({
                    "refreshed": refreshed_total,
                    "refreshed_posts": int(result.get("matched", 0) or 0),
                    "refreshed_replies": int(result.get("replies_matched", 0) or 0),
                    "unmatched": int(result.get("unmatched", 0) or 0),
                }, f)
        except Exception as e:
            print(f"WARN: failed to write summary {args.summary}: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        # Stats.sh greps for "^Reddit Views:" and extracts the "<N> DB posts
        # updated" number for the views-refreshed pill. Include the replies
        # leg in the same number so the pill reflects ALL rows whose view
        # counts got written this run, not just the posts table.
        total_refreshed = result.get("matched", 0) + result.get("replies_matched", 0)
        print(
            f"Reddit Views: {result['with_views']} had views, "
            f"{total_refreshed} DB posts updated "
            f"(posts={result.get('matched', 0)} replies={result.get('replies_matched', 0)}), "
            f"{result['unmatched']} unmatched"
        )
        t = result["totals"]
        print(f"\n--- Totals ({t['days_active']} days) ---")
        print(f"Posts: {t['total_posts']}  |  "
              f"Views: {t['total_views']:,}  |  "
              f"Upvotes: {t['total_upvotes']:,}  |  "
              f"Comments: {t['total_comments']:,}  |  "
              f"Views/day: {t['views_per_day']:,}")


if __name__ == "__main__":
    main()
