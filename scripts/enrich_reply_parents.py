#!/usr/bin/env python3
"""Fill parent-thread linkage on X replies discovered via the notifications lane.

The X notifications feed does not expose the parent tweet id, so
scan_twitter_mentions_browser.py inserts `replies` rows with mention_id only:
no post_id, no parent_reply_id, and often no project_name. This script
resolves the parent chain AFTER the fact, deterministically, with no browser
and no model: fxtwitter's public JSON (already used by fetch_twitter_t1.py)
returns `replying_to_status` for any tweet.

Per row with (post_id IS NULL AND parent_reply_id IS NULL):
  1. fxtwitter GET on their_comment_id -> parent tweet id + handle.
  2. Walk up the ancestor chain (bounded hops) until the root.
  3. First ancestor that is one of OUR posts (/api/v1/posts/lookup, wide
     window) -> PATCH replies.post_id (+ project_name when the row has none).
  4. Immediate parent that is another tracked reply
     (/api/v1/replies?their_comment_id=) -> PATCH parent_reply_id + depth.
  5. Root author -> PATCH thread_author_handle.

Terminal misses (tweet deleted, protected, not-a-reply with nothing to link)
are remembered in a local state file so recurring runs don't refetch forever.

Usage:
  python3 scripts/enrich_reply_parents.py --limit 20            # recurring lane
  python3 scripts/enrich_reply_parents.py --backfill            # all missing rows
  python3 scripts/enrich_reply_parents.py --ids 546832 542579   # specific rows
  python3 scripts/enrich_reply_parents.py --limit 5 --dry-run

All DB I/O goes through the s4l.ai HTTP API (http_api), never direct SQL.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_patch  # noqa: E402

STATE_PATH = os.environ.get(
    "S4L_ENRICH_PARENTS_STATE",
    os.path.expanduser("~/.social-autoposter-enrich-parents.json"),
)
MAX_HOPS = 6
LOOKUP_DAYS = 3650


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def fetch_fxtwitter(handle, tweet_id):
    """Returns (status, tweet_dict). status: 'ok' | 'gone' | 'transient'."""
    url = f"https://api.fxtwitter.com/{handle or 'i'}/status/{tweet_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "social-autoposter/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # 401 = protected account, 404 = deleted. Both terminal.
        if e.code in (401, 404):
            return "gone", None
        return "transient", None
    except Exception:
        return "transient", None
    code = data.get("code")
    if code == 200 and data.get("tweet"):
        return "ok", data["tweet"]
    if code in (401, 404):
        return "gone", None
    return "transient", None


def walk_ancestors(handle, tweet_id, sleep_s):
    """Ancestor chain bottom-up: [(id, handle), ...] parent first, root last.

    Returns (focal, chain, terminal): focal is the fetched tweet object for
    tweet_id itself (None when it is gone/unfetchable — needed for quote
    linkage), terminal is 'root' when the walk reached a non-reply tweet,
    'gone'/'transient' when a hop was deleted/protected/errored (the chain up
    to that point is still usable, but root attribution is not), or 'hop_cap'.
    """
    chain = []
    focal = None
    cur_handle, cur_id = handle, tweet_id
    for _ in range(MAX_HOPS):
        status, tweet = fetch_fxtwitter(cur_handle, cur_id)
        time.sleep(sleep_s)
        if status != "ok":
            # 'gone' is terminal (deleted/protected); 'transient' must NOT be
            # remembered, the next run retries it.
            return focal, chain, status
        if focal is None:
            focal = tweet
        parent_id = tweet.get("replying_to_status")
        parent_handle = tweet.get("replying_to") or ""
        if not parent_id:
            return focal, chain, "root"
        chain.append((str(parent_id), parent_handle))
        cur_handle, cur_id = parent_handle, parent_id
    return focal, chain, "hop_cap"


def lookup_our_post(tweet_id):
    resp = api_get(
        "/api/v1/posts/lookup",
        query={"platform": "twitter", "post_id": str(tweet_id), "days": str(LOOKUP_DAYS)},
    )
    return (resp.get("data") or {}).get("post") or None


def lookup_tracked_reply(tweet_id):
    resp = api_get(
        "/api/v1/replies",
        query={"platform": "x", "their_comment_id": str(tweet_id), "limit": "1"},
    )
    rows = (resp.get("data") or {}).get("replies") or []
    return rows[0] if rows else None


def lookup_our_posted_reply(tweet_id):
    """Reply row where WE authored the tweet (our_reply_id / our_reply_url)."""
    resp = api_get(
        "/api/v1/replies",
        query={"platform": "x", "our_reply_status_id": str(tweet_id), "limit": "1"},
    )
    rows = (resp.get("data") or {}).get("replies") or []
    return rows[0] if rows else None


def fetch_work(limit, our_account=None, ids=None, before_id=None):
    if ids:
        out = []
        for rid in ids:
            resp = api_get(f"/api/v1/replies/{rid}")
            row = (resp.get("data") or {}).get("reply")
            if row:
                out.append(row)
        return out
    query = {
        "platform": "x",
        "missing_parent": "1",
        "order_by": "id",
        "limit": str(limit),
    }
    if our_account:
        query["our_account"] = our_account
    if before_id:
        query["before_id"] = str(before_id)
    resp = api_get("/api/v1/replies", query=query)
    return (resp.get("data") or {}).get("replies") or []


def enrich_row(row, state, sleep_s, dry_run):
    rid = row["id"]
    tid = str(row.get("their_comment_id") or "")
    handle = (row.get("their_author") or "").lstrip("@")
    if not tid:
        return "no_tweet_id"
    if state.get(tid):
        return "state_skip"

    focal, chain, terminal = walk_ancestors(handle, tid, sleep_s)
    if focal is None:
        if terminal == "transient":
            return "transient"  # retry next run, no state write
        state[tid] = "gone"  # deleted/protected focal tweet
        return "gone"

    patch = {}
    matched_post = None
    for anc_id, _anc_handle in chain:
        post = lookup_our_post(anc_id)
        if post:
            matched_post = post
            break
    if matched_post:
        patch["post_id"] = matched_post["id"]
        if not row.get("project_name") and matched_post.get("project_name"):
            patch["project_name"] = matched_post["project_name"]

    if chain:
        parent_id, _parent_handle = chain[0]
        # Immediate parent: another inbound reply we track, or a reply WE
        # posted (the dominant case: a fan replying to our engagement reply).
        tracked = lookup_tracked_reply(parent_id)
        if tracked and tracked["id"] != rid:
            patch["parent_reply_id"] = tracked["id"]
            patch["depth"] = (tracked.get("depth") or 1) + 1
        else:
            ours = lookup_our_posted_reply(parent_id)
            if ours and ours["id"] != rid:
                patch["parent_reply_id"] = ours["id"]
                patch["depth"] = (ours.get("depth") or 1) + 1
                if "post_id" not in patch and ours.get("post_id"):
                    patch["post_id"] = ours["post_id"]
                if not row.get("project_name") and not patch.get("project_name") \
                        and ours.get("project_name"):
                    patch["project_name"] = ours["project_name"]

    # Quote linkage: a quote-tweet of our post (or of a reply we posted) is
    # engagement on our content even when replying_to_status is empty.
    quote_id = str(((focal.get("quote") or {}).get("id")) or "")
    if quote_id and "post_id" not in patch:
        qpost = lookup_our_post(quote_id)
        if qpost:
            patch["post_id"] = qpost["id"]
            if not row.get("project_name") and not patch.get("project_name") \
                    and qpost.get("project_name"):
                patch["project_name"] = qpost["project_name"]
        elif "parent_reply_id" not in patch:
            qours = lookup_our_posted_reply(quote_id)
            if qours and qours["id"] != rid:
                patch["parent_reply_id"] = qours["id"]
                patch["depth"] = (qours.get("depth") or 1) + 1
                if qours.get("post_id"):
                    patch["post_id"] = qours["post_id"]

    if terminal == "root":
        root_handle = (chain[-1][1] if chain else (focal.get("author") or {}).get("screen_name") or "")
        root_handle = (root_handle or "").lstrip("@")
        if chain and root_handle and not row.get("thread_author_handle"):
            patch["thread_author_handle"] = root_handle

    if not patch:
        if terminal == "transient":
            return "transient"  # incomplete walk; retry next run
        # Chain walked to root / cut by a deleted ancestor / not a reply at
        # all, and nothing of ours anywhere in it: remember permanently.
        state[tid] = "no_link" if chain else "not_a_reply"
        return state[tid]

    if dry_run:
        print(f"  [DRY] reply {rid}: {json.dumps(patch)}")
        return "would_patch"

    resp = api_patch(f"/api/v1/replies/{rid}", patch)
    if resp.get("error"):
        print(f"  ERROR patching reply {rid}: {resp['error']}", file=sys.stderr)
        return "patch_error"
    state[tid] = "linked"
    kinds = "+".join(k for k in ("post_id", "parent_reply_id", "thread_author_handle") if k in patch)
    print(f"  linked reply {rid}: {kinds} {json.dumps(patch)}")
    return "linked"


def main():
    ap = argparse.ArgumentParser(description="Backfill parent-thread linkage on X replies")
    ap.add_argument("--limit", type=int, default=20, help="rows per run (recurring lane)")
    ap.add_argument("--backfill", action="store_true", help="keep paging until no work is left")
    ap.add_argument("--ids", nargs="*", type=int, help="enrich specific reply ids")
    ap.add_argument("--our-account", default=None, help="scope to one posting handle")
    ap.add_argument("--all-accounts", action="store_true", help="do not scope by handle")
    ap.add_argument("--sleep", type=float, default=0.5, help="seconds between fxtwitter calls")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    our_account = args.our_account
    if not our_account and not args.all_accounts and not args.ids:
        try:
            from account_resolver import resolve as _resolve_account
            our_account = _resolve_account("twitter")
        except Exception:
            our_account = None
        if not our_account:
            print("No twitter account resolvable; pass --our-account or --all-accounts", file=sys.stderr)
            sys.exit(1)

    state = load_state()
    totals = {}
    seen_ids = set()
    before_id = None
    while True:
        rows = fetch_work(
            args.limit if not args.backfill else 200, our_account, args.ids, before_id
        )
        rows = [r for r in rows if r["id"] not in seen_ids]
        if not rows:
            break
        for row in rows:
            seen_ids.add(row["id"])
            # Terminal misses (deleted tweet, foreign thread) never leave the
            # missing_parent queue; the id cursor pages past them.
            before_id = row["id"] if before_id is None else min(before_id, row["id"])
            outcome = enrich_row(row, state, args.sleep, args.dry_run)
            totals[outcome] = totals.get(outcome, 0) + 1
            if len(seen_ids) % 25 == 0 and not args.dry_run:
                save_state(state)
        if not args.dry_run:
            save_state(state)
        if args.ids or not args.backfill:
            break

    print(f"[enrich_reply_parents] processed={len(seen_ids)} " +
          " ".join(f"{k}={v}" for k, v in sorted(totals.items())))


if __name__ == "__main__":
    main()
