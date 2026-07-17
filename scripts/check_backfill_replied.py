#!/usr/bin/env python3
"""Verify: for each backfill_old reply, fetch the full Reddit thread JSON and
check whether u/Deep_Ad1959 is a direct child of the target comment OR of the
thread's OP post (the latter covers the case where the reply was to the post
itself). Output: id, target_author, status, url.

Status values:
  NOT_REPLIED     - Deep_Ad1959 has not replied to this comment
  ALREADY_REPLIED - Deep_Ad1959 is a direct child of the target comment
  ERROR:<...>     - fetch/parse error
  NOT_FOUND       - target comment not found in thread JSON
"""
import json, os, re, sys, urllib.request, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod
from scan_reddit_replies import load_cookies
from account_resolver import require as _require_account

OUR_ACCOUNT = _require_account("reddit")
UA = "social-autoposter/verify 1.0"

# old.reddit.com URL: /r/<sub>/comments/<thread_id>/<slug>/<comment_id>/
URL_RE = re.compile(r"/r/([^/]+)/comments/([a-z0-9]+)/[^/]*/([a-z0-9]+)")


def fetch_thread(thread_id, cookie):
    url = f"https://old.reddit.com/comments/{thread_id}.json?limit=500&depth=10"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Cookie": cookie, "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def find_comment(node, target_id):
    """DFS for a t1 with data.id == target_id. Return the comment node or None."""
    if isinstance(node, list):
        for v in node:
            hit = find_comment(v, target_id)
            if hit is not None:
                return hit
    elif isinstance(node, dict):
        if node.get("kind") == "t1":
            d = node.get("data") or {}
            if d.get("id") == target_id:
                return node
            rep = d.get("replies")
            if isinstance(rep, dict):
                hit = find_comment(rep, target_id)
                if hit is not None:
                    return hit
        for v in node.values():
            hit = find_comment(v, target_id)
            if hit is not None:
                return hit
    return None


def direct_children(comment_node):
    rep = (comment_node.get("data") or {}).get("replies")
    if not isinstance(rep, dict):
        return []
    return ((rep.get("data") or {}).get("children") or [])


def check_url(url, cookie):
    m = URL_RE.search(url)
    if not m:
        return "ERROR:bad_url"
    _, thread_id, comment_id = m.group(1), m.group(2), m.group(3)
    try:
        payload = fetch_thread(thread_id, cookie)
    except Exception as e:
        return f"ERROR:fetch:{type(e).__name__}:{e}"

    target = find_comment(payload, comment_id)
    if target is None:
        return "NOT_FOUND"

    for ch in direct_children(target):
        d = (ch.get("data") or {}) if isinstance(ch, dict) else {}
        if (d.get("author") or "").lower() == OUR_ACCOUNT.lower():
            return f"ALREADY_REPLIED:{d.get('id')}"
    return "NOT_REPLIED"


def main():
    cookie = load_cookies()
    if not cookie:
        print("no cookies; cannot verify", file=sys.stderr); sys.exit(2)
    db = dbmod.get_conn()
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    rows = db.execute("""
        SELECT r.id, r.their_author, r.their_comment_url
        FROM replies r
        WHERE r.status='skipped' AND r.skip_reason='backfill_old'
        ORDER BY r.id
        LIMIT %s
    """, [limit]).fetchall()
    for rid, author, url in rows:
        st = check_url(url, cookie)
        print(f"{rid}\t{author}\t{st}\t{url}")
        time.sleep(1.5)


if __name__ == "__main__":
    main()
