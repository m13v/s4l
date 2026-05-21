#!/usr/bin/env python3
"""refresh_thread_top_replies_stats.py - refresh engagement counts on the
human top-replies we snapshotted via capture_thread_top_replies.py.

Mirrors the fxtwitter-driven pattern in scripts/update_stats.py:update_twitter
exactly so the dashboard's "is this still tracked?" semantics align with our
own posts. We don't piggyback inside update_stats.py because that file is
locked per CLAUDE.md; we drive the same cadence from a sibling launchd job.

Run on the same ~6h cadence as `com.m13v.social-stats-twitter`.

Usage:
    python3 scripts/refresh_thread_top_replies_stats.py
    python3 scripts/refresh_thread_top_replies_stats.py --stale-hours 5
    python3 scripts/refresh_thread_top_replies_stats.py --limit 200
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from db import get_conn  # noqa: E402


FXTWITTER_BASE = "https://api.fxtwitter.com"
# Per-call pacing — same 1 req/s as update_stats.py update_twitter().
REQUEST_DELAY_SEC = 1.0


def fetch_json(url: str, timeout: int = 15) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "social-autoposter-thread-top-replies/1.0",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"code": 404, "tweet": None}
        return None
    except Exception:
        return None


def list_active_replies(db, stale_hours: int, limit: int):
    """Pull active top-reply rows that haven't been refreshed in stale_hours.

    NULL engagement_updated_at = never refreshed (just captured); always elig.
    """
    cur = db.execute(
        "SELECT id, post_id, reply_url, reply_tweet_id, reply_author_handle, "
        "       likes, replies, retweets, views, scan_no_change_count "
        "FROM thread_top_replies "
        "WHERE status = 'active' "
        "  AND (engagement_updated_at IS NULL "
        "       OR engagement_updated_at < NOW() - INTERVAL '%s hours') "
        "ORDER BY COALESCE(engagement_updated_at, captured_at) ASC "
        "LIMIT %s",
        [stale_hours, limit],
    )
    return [dict(r) for r in cur.fetchall()]


def update_row(db, row_id: int, likes, replies, retweets, views,
               stayed_same: bool) -> None:
    if stayed_same:
        db.execute(
            "UPDATE thread_top_replies SET "
            "  likes = %s, replies = %s, retweets = %s, views = %s, "
            "  engagement_updated_at = NOW(), "
            "  status_checked_at = NOW(), "
            "  scan_no_change_count = COALESCE(scan_no_change_count, 0) + 1, "
            "  deletion_detect_count = 0 "
            "WHERE id = %s",
            [likes, replies, retweets, views, row_id],
        )
    else:
        db.execute(
            "UPDATE thread_top_replies SET "
            "  likes = %s, replies = %s, retweets = %s, views = %s, "
            "  engagement_updated_at = NOW(), "
            "  status_checked_at = NOW(), "
            "  scan_no_change_count = 0, "
            "  deletion_detect_count = 0 "
            "WHERE id = %s",
            [likes, replies, retweets, views, row_id],
        )


def bump_deletion(db, row_id: int, threshold: int = 2) -> tuple[int, bool]:
    """Bump deletion_detect_count; flip status='deleted' at threshold.
    Returns (new_count, flipped).
    """
    cur = db.execute(
        "UPDATE thread_top_replies "
        "SET deletion_detect_count = COALESCE(deletion_detect_count, 0) + 1, "
        "    status_checked_at = NOW() "
        "WHERE id = %s "
        "RETURNING deletion_detect_count",
        [row_id],
    )
    new_count = cur.fetchone()[0]
    if new_count >= threshold:
        db.execute(
            "UPDATE thread_top_replies SET status = 'deleted' WHERE id = %s",
            [row_id],
        )
        return new_count, True
    return new_count, False


def parse_tweet_id_handle(reply_url: str, fallback_handle: str | None) -> tuple[str | None, str | None]:
    tid_match = re.search(r"/status/(\d+)", reply_url or "")
    tid = tid_match.group(1) if tid_match else None
    handle_match = re.search(r"(?:x\.com|twitter\.com)/([^/]+)/status", reply_url or "")
    handle = handle_match.group(1) if handle_match else (fallback_handle or "i")
    return tid, handle


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stale-hours", type=int, default=5,
                    help="Skip rows refreshed within this window")
    ap.add_argument("--limit", type=int, default=300,
                    help="Max rows per run")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    log = (lambda *a, **kw: None) if args.quiet else (lambda *a, **kw: print(*a, **kw, flush=True))

    db = get_conn()
    rows = list_active_replies(db, args.stale_hours, args.limit)

    if not rows:
        log("[refresh-top-replies] nothing to refresh")
        return 0

    log(f"[refresh-top-replies] {len(rows)} row(s) to refresh")

    total = updated = changed = deleted = errors = 0

    for row in rows:
        total += 1
        rid = row["id"]
        reply_url = row.get("reply_url") or ""
        cached_tid = row.get("reply_tweet_id")
        tid, handle = parse_tweet_id_handle(reply_url, row.get("reply_author_handle"))
        if not tid and cached_tid:
            tid = cached_tid

        if not tid:
            errors += 1
            log(f"[refresh-top-replies row={rid}] no tweet id in {reply_url!r}")
            continue

        url = f"{FXTWITTER_BASE}/{handle}/status/{tid}"
        data = fetch_json(url)
        if data is None:
            time.sleep(2)
            data = fetch_json(url)
        if data is None:
            errors += 1
            continue

        code = data.get("code", 0)
        tweet = data.get("tweet")

        if code == 404 or tweet is None:
            new_count, flipped = bump_deletion(db, rid)
            if flipped:
                deleted += 1
                log(f"[refresh-top-replies row={rid}] DELETED (after {new_count} strikes)")
            else:
                log(f"[refresh-top-replies row={rid}] deletion pending {new_count}/2")
            db.commit()
            time.sleep(REQUEST_DELAY_SEC)
            continue

        likes = int(tweet.get("likes") or 0)
        replies = int(tweet.get("replies") or 0)
        retweets = int(tweet.get("retweets") or 0)
        views = int(tweet.get("views") or 0)

        stayed_same = (
            likes == (row.get("likes") or 0)
            and replies == (row.get("replies") or 0)
            and retweets == (row.get("retweets") or 0)
            and views == (row.get("views") or 0)
        )

        update_row(db, rid, likes, replies, retweets, views, stayed_same)
        db.commit()

        updated += 1
        if not stayed_same:
            changed += 1

        if total % 50 == 0:
            log(f"[refresh-top-replies] progress {total}/{len(rows)} "
                f"updated={updated} changed={changed} errors={errors}")

        time.sleep(REQUEST_DELAY_SEC)

    log(f"[refresh-top-replies] done total={total} updated={updated} "
        f"changed={changed} deleted={deleted} errors={errors}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
