#!/usr/bin/env python3
"""
fetch_twitter_t1.py

Phase 2 of the twitter-cycle. Re-polls fxtwitter for every candidate in a
given batch_id, writes T1 engagement columns and computes delta_score.

    python3 scripts/fetch_twitter_t1.py --batch-id <id>

delta_score formula:
    Δlikes + 3*Δretweets + 2*Δreplies + Δviews/1000 + Δbookmarks
Weights picked so retweets/replies (stronger virality signals) beat raw likes,
views are divided down so they don't dominate.

Migrated 2026-05-18: pending-batch read and per-row T1 writes now go through
the s4l.ai HTTP API (/api/v1/twitter-candidates/pending-batch +
/api/v1/twitter-candidates/by-id action=set_t1) instead of psycopg2.
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_patch  # noqa: E402


def fetch_fxtwitter(handle, tweet_id):
    url = f"https://api.fxtwitter.com/{handle}/status/{tweet_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "social-autoposter/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  fxtwitter error for {handle}/{tweet_id}: {e}", file=sys.stderr)
        return None


def parse(url):
    m = re.search(r"x\.com/([^/]+)/status/(\d+)", url or "")
    if not m:
        m = re.search(r"twitter\.com/([^/]+)/status/(\d+)", url or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def compute_delta(t0, t1):
    dl = (t1.get("likes", 0) or 0) - (t0.get("likes", 0) or 0)
    dr = (t1.get("retweets", 0) or 0) - (t0.get("retweets", 0) or 0)
    dp = (t1.get("replies", 0) or 0) - (t0.get("replies", 0) or 0)
    dv = (t1.get("views", 0) or 0) - (t0.get("views", 0) or 0)
    db = (t1.get("bookmarks", 0) or 0) - (t0.get("bookmarks", 0) or 0)
    return dl + 3 * dr + 2 * dp + dv / 1000.0 + db


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--batch-id", required=True)
    args = p.parse_args()

    resp = api_get(
        "/api/v1/twitter-candidates/pending-batch",
        query={"batch_id": args.batch_id},
    )
    rows = (resp.get("data") or {}).get("candidates") or []

    if not rows:
        print(f"No pending rows for batch {args.batch_id}", file=sys.stderr)
        return

    print(f"Re-polling {len(rows)} candidates for batch {args.batch_id}", file=sys.stderr)

    def fetch_row(row):
        cid = row["id"]
        url = row["tweet_url"]
        l0 = row.get("likes_t0")
        r0 = row.get("retweets_t0")
        p0 = row.get("replies_t0")
        v0 = row.get("views_t0")
        b0 = row.get("bookmarks_t0")
        handle, tweet_id = parse(url)
        if not handle:
            return None
        data = fetch_fxtwitter(handle, tweet_id)
        if not data or not data.get("tweet"):
            return None
        t = data["tweet"]
        t1 = {
            "likes": t.get("likes", 0),
            "retweets": t.get("retweets", 0),
            "replies": t.get("replies", 0),
            "views": t.get("views", 0),
            "bookmarks": t.get("bookmarks", 0),
        }
        t0 = {"likes": l0 or 0, "retweets": r0 or 0, "replies": p0 or 0, "views": v0 or 0, "bookmarks": b0 or 0}
        return (cid, url, t1, compute_delta(t0, t1))

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(fetch_row, rows))

    for result in results:
        if result is None:
            continue
        cid, url, t1, delta = result
        try:
            api_patch(
                "/api/v1/twitter-candidates/by-id",
                {
                    "id": cid,
                    "action": "set_t1",
                    "likes_t1": t1["likes"],
                    "retweets_t1": t1["retweets"],
                    "replies_t1": t1["replies"],
                    "views_t1": t1["views"],
                    "bookmarks_t1": t1["bookmarks"],
                    "delta_score": delta,
                    "likes": t1["likes"],
                    "retweets": t1["retweets"],
                    "replies": t1["replies"],
                    "views": t1["views"],
                    "bookmarks": t1["bookmarks"],
                },
                ok_on_404=True,
            )
            print(f"  #{cid} {url} Δ={delta:.1f}", file=sys.stderr)
        except SystemExit as e:
            print(f"  #{cid} {url} set_t1 failed: {e}", file=sys.stderr)
            continue


if __name__ == "__main__":
    main()
