#!/usr/bin/env python3
"""One-shot processor for the LinkedIn notifications discovery scan.

Reads the saved bh_run extraction output (ACCUMULATED_ACTIONABLE JSON line),
derives parent activity/ugcPost id from each comment_urn, and inserts new
pending replies (creating the parent post row when we have not engaged on
that thread before). Prints the required LINKEDIN_SCAN_SUMMARY marker line.

Migrated 2026-06-01 from raw psycopg2 (os.environ["DATABASE_URL"]) to the
s4l.ai HTTP API, so it runs on a machine that has no direct DB access. The
live successor for the regular pipeline is li_discover_insert.py; this stays
as the historical-extract-file one-shot but now shares the same endpoints.

Dedup is now enforced server-side rather than via bulk pre-reads:
  - POST /api/v1/posts is idempotent on (platform, thread_url): a duplicate
    returns 409 duplicate_thread with the existing post id, which we reuse.
  - POST /api/v1/replies is idempotent on (platform, their_comment_id): a
    duplicate returns 409, which we count as already-tracked.
The only read we still do is GET /api/v1/posts (active LinkedIn rows) to map
a parent activity/ugcPost id onto an existing post row before falling back to
create-or-reuse.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_post

EXTRACT_FILE = "/Users/matthewdi/.claude/projects/-/0152159f-c679-488f-954a-8312a9a58060/tool-results/toolu_01FaaCg5i5D5xp5ELcp1TiVS.txt"

EXCLUDED_AUTHORS = {"louis030195", "louis3195"}
OWN_NAMES = {"matthew diakonov", "m13v"}

DRY_RUN = "--apply" not in sys.argv


def load_actionable():
    raw = open(EXTRACT_FILE).read()
    obj = json.loads(raw)
    inner = json.loads(obj["result"])
    stdout = inner["stdout"]
    for line in stdout.splitlines():
        if line.startswith("ACCUMULATED_ACTIONABLE:"):
            return json.loads(line[len("ACCUMULATED_ACTIONABLE:"):].strip())
    raise SystemExit("ACCUMULATED_ACTIONABLE not found in extract file")


def parse_parent(comment_urn):
    """urn:li:comment:(activity:PARENT,COMMENT) or (ugcPost:PARENT,COMMENT)."""
    if not comment_urn:
        return None, None
    m = re.match(r"urn:li:comment:\((activity|ugcPost):(\d+),(\d+)\)", comment_urn)
    if not m:
        return None, None
    ns, parent_id, _comment_id = m.group(1), m.group(2), m.group(3)
    return ns, parent_id


def load_active_posts():
    """Return list of (id, our_url, thread_url) for active LinkedIn posts.

    Bounded by the endpoint's limit; if a parent post is older than the
    window it simply will not match here, and the create path falls through
    to POST /api/v1/posts which 409s on the duplicate (platform, thread_url)
    and hands back the existing id. Correctness does not depend on this map
    being exhaustive; it is purely a round-trip saver.
    """
    resp = api_get(
        "/api/v1/posts",
        {"platform": "linkedin", "status": "active", "limit": 500,
         "order_by": "id", "order_dir": "desc"},
    )
    rows = (resp.get("data") or {}).get("posts") or []
    return [(r["id"], r.get("our_url"), r.get("thread_url")) for r in rows]


def main():
    items = load_actionable()
    posts = load_active_posts()

    counts = dict(scanned=len(items), new=0, already=0, author_engaged=0,
                  excluded=0, own=0, no_urn=0, within_batch_dup=0)
    inserted_keys = set()  # (author_lower, parent_id) chosen this run
    actions = []

    for it in items:
        author = (it.get("author") or "").strip()
        author_l = author.lower()
        comment_urn = it.get("comment_urn")
        snippet = it.get("snippet") or ""
        href = it.get("href") or ""
        ns, parent_id = parse_parent(comment_urn)

        if not comment_urn or not parent_id:
            counts["no_urn"] += 1
            actions.append(("no_comment_urn", author, comment_urn))
            continue
        if author_l in EXCLUDED_AUTHORS or any(e in author_l for e in EXCLUDED_AUTHORS):
            counts["excluded"] += 1
            actions.append(("excluded_author", author, comment_urn))
            continue
        if author_l in OWN_NAMES:
            counts["own"] += 1
            actions.append(("own_account", author, comment_urn))
            continue

        # match a post by parent_id appearing in our_url or thread_url
        post_id = None
        our_url = None
        for pid, pu, tu in posts:
            if (pu and parent_id in pu) or (tu and parent_id in tu):
                post_id = pid
                our_url = pu
                break

        # within-batch dedup: one reply per author per thread
        batch_key = (author_l, parent_id)
        if batch_key in inserted_keys:
            counts["within_batch_dup"] += 1
            actions.append(("within_batch_dup", author, comment_urn))
            continue

        # need a post_id; create one (or reuse via 409) if no match
        if post_id is None:
            thread_url = ("https://www.linkedin.com/feed/update/"
                          "urn:li:%s:%s/" % (ns, parent_id))
            if DRY_RUN:
                actions.append(("WOULD_CREATE_POST+INSERT", author, comment_urn))
                post_id = -1  # placeholder for dry run
                our_url = thread_url
            else:
                resp = api_post(
                    "/api/v1/posts",
                    {
                        "platform": "linkedin",
                        "thread_url": thread_url,
                        "our_url": thread_url,
                        "our_content": "[discovered thread, engagement reply pending]",
                        "project": "general",
                        "thread_author": author or "(unknown)",
                        "our_account": "Matthew Diakonov",
                        "engagement_style": "curious_probe",
                        "status": "active",
                    },
                    ok_on_conflict=True,
                )
                if (resp.get("error") or {}).get("code") == "duplicate_thread":
                    post_id = (resp["error"].get("details") or {}).get("existing_post_id")
                else:
                    post_id = ((resp.get("data") or {}).get("post") or {}).get("id")
                our_url = thread_url
                if post_id is None:
                    counts["author_engaged"] += 1  # could not resolve a row
                    actions.append(("post_unresolved", author, comment_urn))
                    continue
                # keep the in-memory map fresh for later items this run
                posts.append((post_id, our_url, thread_url))

        if DRY_RUN:
            actions.append(("WOULD_INSERT", author, comment_urn, "post_id=%s" % post_id))
        else:
            resp = api_post(
                "/api/v1/replies",
                {
                    "platform": "linkedin",
                    "post_id": post_id,
                    "their_comment_id": comment_urn,
                    "their_author": author,
                    "their_content": snippet,
                    "their_comment_url": href,
                    "depth": 1,
                    "status": "pending",
                },
                ok_on_conflict=True,
            )
            if (resp.get("error") or {}).get("code") == "duplicate_reply":
                counts["already"] += 1
                actions.append(("already_tracked", author, comment_urn))
                continue

        inserted_keys.add(batch_key)
        counts["new"] += 1

    print("=== ACTION LOG (%s) ===" % ("DRY RUN" if DRY_RUN else "APPLIED"))
    for a in actions:
        print("  " + " | ".join(str(x) for x in a))
    print()
    print("%d new replies discovered" % counts["new"])
    print("%d already tracked" % counts["already"])
    print("%d author already engaged on thread" % counts["author_engaged"])
    print("%d within-batch duplicates (one reply per author per thread)" % counts["within_batch_dup"])
    print("%d excluded" % counts["excluded"])
    print("%d own account" % counts["own"])
    print("%d no comment URN" % counts["no_urn"])
    print()
    excluded_total = counts["excluded"] + counts["own"]
    print("LINKEDIN_SCAN_SUMMARY: scanned=%d new=%d already=%d excluded=%d unmatched=%d" % (
        counts["scanned"], counts["new"], counts["already"], excluded_total, counts["no_urn"]))


if __name__ == "__main__":
    main()
