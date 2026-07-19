#!/usr/bin/env python3
"""Scan our recent X replies for new public follow-ups and ingest them.

Companion to scan_twitter_mentions_browser.py. The mentions tab only surfaces
explicit @-mentions, so replies to our replies without a retagged handle are
invisible. This script compensates by revisiting each of our recent X replies
and scraping the page for depth-2+ comments that aren't yet in the DB.

Flow:
  1. Query `replies` for our X replies in last N days (default 14) where
     `our_reply_url IS NOT NULL`. These are the threads we're subscribing to.
  2. Write those URLs to a temp file.
  3. Invoke `twitter_browser.py thread-followups <file>`, which scrapes each
     URL and returns a `{results: [{thread_url, anchor_tweet_id, followups}]}`
     JSON blob.
  4. For each followup not already in `replies` (by platform+their_comment_id),
     insert a new `replies` row with:
       - platform = 'x'
       - parent_reply_id = id of the original reply (the anchor)
       - post_id = anchor.post_id
       - depth = anchor.depth + 1
       - status = 'pending'
     Tweets we posted ourselves are skipped (OUR_HANDLE check). Own-account
     replies from us get status='replied' with our_reply_id populated, mirroring
     the mentions scanner.

Usage:
    python3 scripts/scan_twitter_thread_followups.py [--days N] [--max-urls N]

Migrated 2026-05-18: reads/writes now route through the s4l.ai HTTP API
(/api/v1/replies for both filter-list and insert) instead of psycopg2.
The (platform, their_comment_id) dedup runs server-side; the local
known_ids cache is now just for in-loop short-circuiting.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_post  # noqa: E402
try:
    from account_resolver import resolve as _resolve_account  # noqa: E402
except Exception:
    def _resolve_account(_platform):  # type: ignore[unused-arg]
        return None

# THE canonical config loader (scripts/config.py): S4L_CONFIG_PATH / state-dir /
# S4L_REPO_DIR aware, mtime-cached. Replaces this file's hand-rolled loader and
# its hardcoded config path (the S4L-4H dead-path class on customer boxes).
import os as _cfg_os, sys as _cfg_sys
_cfg_sys.path.insert(0, _cfg_os.path.dirname(_cfg_os.path.abspath(__file__)))
from config import config_path as _canonical_config_path, load_config
CONFIG_PATH = _canonical_config_path()
OUR_HANDLE = _resolve_account("twitter")
if not OUR_HANDLE:
    # No hardcoded fallback: scanning/attributing under a default handle silently
    # impersonates the repo owner. Refuse to run so the missing config surfaces.
    sys.stderr.write(
        "[scan_twitter_followups] no Twitter handle configured "
        "(accounts.twitter.handle / AUTOPOSTER_TWITTER_HANDLE); refusing to run "
        "to avoid wrong-account attribution. Run connect_x first.\n")
    sys.exit(1)
DEFAULT_DAYS = 14
DEFAULT_MAX_URLS = 40
REPO_DIR = os.path.expanduser("~/social-autoposter")
REPLY_PAGE_LIMIT = 500

# Pinned interpreter, never the literal "python3": twitter_browser.py (below)
# imports Playwright, and bare "python3" resolves to the caller's system
# Python on PATH, which has no Playwright on a fresh install (silent
# failure). See scripts/twitter_post_plan.py:131.
PYTHON = os.environ.get("S4L_PYTHON") or sys.executable




def fetch_our_recent_x_replies(days, max_urls):
    """Return list of (reply_id, our_reply_url, post_id, depth) for our recent X replies.

    Filters live in the route as:
      - platform = x
      - status = replied (the route's WHERE)
      - has_our_reply_content / has_our_reply_id NOT used here; we need
        our_reply_url, but the route returns it on every row and we filter
        client-side after the page comes back.
      - replied_at >= NOW() - <days>d
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    resp = api_get(
        "/api/v1/replies",
        query={
            "platform": "x",
            "status": "replied",
            "since": since,
            "limit": max_urls,
            "order_by": "replied_at",
        },
    )
    rows = (resp.get("data") or {}).get("replies") or []
    out = []
    for r in rows:
        url = r.get("our_reply_url")
        if not url:
            continue
        out.append((r["id"], url, r.get("post_id"), int(r.get("depth") or 1)))
    return out[:max_urls]


def existing_comment_ids():
    """First-page snapshot of replies.their_comment_id for platform=x.

    The route's UNIQUE (platform, their_comment_id) index is the canonical
    dedup; this cache short-circuits the per-followup POST loop and prints
    accurate "already tracked" counts. Bounded at REPLY_PAGE_LIMIT (500) by
    the route — fine because the most recent rows are the ones we'd
    otherwise collide with.
    """
    resp = api_get(
        "/api/v1/replies",
        query={"platform": "x", "limit": REPLY_PAGE_LIMIT, "order_by": "id"},
    )
    rows = (resp.get("data") or {}).get("replies") or []
    return {r.get("their_comment_id") for r in rows if r.get("their_comment_id")}


def anchor_id_from_url(url):
    m = re.search(r"/status/(\d+)", url or "")
    return m.group(1) if m else None


def run_browser_scrape(urls, scroll_count=3):
    """Shell out to twitter_browser.py thread-followups and parse JSON."""
    if not urls:
        return {"results": [], "urls_visited": 0}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        urls_path = f.name
        for u in urls:
            f.write(u + "\n")
    try:
        proc = subprocess.run(
            [PYTHON, os.path.join(REPO_DIR, "scripts/twitter_browser.py"),
             "thread-followups", urls_path, str(scroll_count)],
            capture_output=True, text=True, timeout=1800,
        )
        if proc.returncode != 0:
            print(f"ERROR: twitter_browser.py exited {proc.returncode}", file=sys.stderr)
            print(proc.stderr[-2000:], file=sys.stderr)
            return {"results": [], "error": "browser_failed"}
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            print(f"ERROR: could not parse browser output as JSON: {e}", file=sys.stderr)
            print(proc.stdout[-2000:], file=sys.stderr)
            return {"results": [], "error": "json_parse_failed"}
    finally:
        try:
            os.unlink(urls_path)
        except OSError:
            pass


def insert_followup(followup, parent_reply_id, post_id, parent_depth, root_author=None):
    """Insert one follow-up row via /api/v1/replies. Returns True if inserted,
    False if skipped (own handle, missing required fields, or 409 duplicate)."""
    tweet_id = followup.get("tweet_id") or ""
    handle = (followup.get("handle") or "").lstrip("@")
    text = followup.get("text") or ""
    url = followup.get("tweet_url") or ""
    if not tweet_id or not handle:
        return False
    if handle.lower() == OUR_HANDLE.lower():
        return False
    body = {
        "post_id": post_id,
        "platform": "x",
        "their_comment_id": tweet_id,
        "their_author": handle,
        "their_content": text,
        "their_comment_url": url,
        "depth": (parent_depth or 1) + 1,
        "status": "pending",
        "parent_reply_id": parent_reply_id,
        "our_account": OUR_HANDLE,
    }
    # OP of the thread our reply lives in, scraped for free from the conversation
    # page (twitter_browser.scrape_many_thread_followups). Always set when known,
    # including when the OP is the replier — that equality is the "OP replied"
    # signal the analytic needs.
    root_author = (root_author or "").lstrip("@")
    if root_author:
        body["thread_author_handle"] = root_author
    # Media of the followup tweet itself (images/videos/GIFs/link-cards),
    # captured for free during the same DOM pass in
    # twitter_browser.scrape_thread_followups (2026-06-03 thread-media feature).
    # The engage prompt reads this back via /api/v1/replies/next-pending so it
    # can reply to what the comment VISUALLY shows, not just its text. An empty
    # list [] is meaningful ("captured, none found"); only omit when the
    # extractor returned nothing parseable (None). Harmless no-op against the
    # pre-deploy API, which simply ignores the unknown field.
    media = followup.get("media")
    if isinstance(media, list):
        body["their_media"] = media
    resp = api_post("/api/v1/replies", body, ok_on_conflict=True)
    if (resp.get("error") or {}).get("code") == "duplicate_reply":
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"Look back N days for our replies (default {DEFAULT_DAYS})")
    parser.add_argument("--max-urls", type=int, default=DEFAULT_MAX_URLS,
                        help=f"Max thread URLs to revisit per run (default {DEFAULT_MAX_URLS})")
    parser.add_argument("--scroll-count", type=int, default=3,
                        help="Scrolls per thread page (default 3)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be inserted without writing")
    args = parser.parse_args()

    our_replies = fetch_our_recent_x_replies(args.days, args.max_urls)
    print(f"Revisiting {len(our_replies)} of our recent X replies (last {args.days}d)")
    if not our_replies:
        return 0

    url_to_meta = {url: (rid, pid, depth) for rid, url, pid, depth in our_replies}
    urls = list(url_to_meta.keys())

    print(f"Invoking browser scraper for {len(urls)} URLs...")
    data = run_browser_scrape(urls, scroll_count=args.scroll_count)

    results = data.get("results", [])
    known_ids = existing_comment_ids()
    new_count = 0
    skip_own = 0
    skip_existing = 0
    skip_anchor = 0
    skip_not_replying_to_us = 0

    for r in results:
        thread_url = r.get("thread_url") or ""
        anchor_id = r.get("anchor_tweet_id") or anchor_id_from_url(thread_url)
        root_author = (r.get("root_author") or "").lstrip("@")
        meta = url_to_meta.get(thread_url)
        if not meta:
            continue
        parent_reply_id, post_id, parent_depth = meta

        for fu in r.get("followups", []):
            tid = fu.get("tweet_id")
            handle = (fu.get("handle") or "").lstrip("@")
            if not tid:
                continue
            if tid == anchor_id:
                skip_anchor += 1
                continue
            if handle.lower() == OUR_HANDLE.lower():
                skip_own += 1
                continue
            # Filter: only keep tweets that are actually replying to us.
            # X tweet permalink pages inject "more from this author" / "you might
            # like" articles into the timeline. Without this check, those leak
            # in as fake follow-ups (observed 2026-05: ~80% of captures were
            # the seed author's later unrelated promotional tweets, not replies
            # to our reply). The extractor in twitter_browser.py captures
            # `replying_to` from the "Replying to @handle" block above each
            # tweet; if it's empty or doesn't point at our handle, it's not a
            # response to us.
            replying_to = (fu.get("replying_to") or "").lstrip("@").lower()
            if replying_to != OUR_HANDLE.lower():
                skip_not_replying_to_us += 1
                continue
            if tid in known_ids:
                skip_existing += 1
                continue
            if args.dry_run:
                print(f"  [DRY] @{handle} (tid={tid}) op=@{root_author or '?'} parent_reply={parent_reply_id} depth={(parent_depth or 1) + 1}: {(fu.get('text') or '')[:80]}")
                new_count += 1
                known_ids.add(tid)
                continue
            inserted = insert_followup(fu, parent_reply_id, post_id, parent_depth, root_author=root_author)
            if inserted:
                new_count += 1
                known_ids.add(tid)
                print(f"  NEW follow-up: @{handle} (tid={tid}) parent_reply={parent_reply_id} depth={(parent_depth or 1) + 1}: {(fu.get('text') or '')[:80]}")
            else:
                # 409 duplicate (someone else inserted between our local cache
                # and this POST). Count it as already-tracked, not new.
                known_ids.add(tid)
                skip_existing += 1

    print(f"\nSummary: {new_count} new follow-ups ingested, "
          f"{skip_existing} already tracked, {skip_own} own account, "
          f"{skip_anchor} anchor skips, {skip_not_replying_to_us} not replying to us")
    return new_count


if __name__ == "__main__":
    rc = main()
    sys.exit(0 if rc >= 0 else 1)
