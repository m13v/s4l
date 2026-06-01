#!/usr/bin/env python3
"""LinkedIn Phase A discovery helpers, HTTP-backed (no DATABASE_URL).

Replaces the raw psql SELECT/INSERT lines that engage-linkedin.sh Phase A
embedded in its Claude prompt (Steps 1-3 dedup reads, Step 7 reply insert /
post create). Everything routes through the s4l.ai HTTP API so the engage
runner works on a machine with no direct DB access.

Subcommands (each prints to stdout in the same shape the old psql `-t -A`
output produced, so the prompt's downstream parsing is unchanged):

  comment-ids
      One their_comment_id per line. Phase A Step 1 dedup list.

  engaged-pairs
      One "author|||our_url" per line. Phase A Step 2 dedup list.

  posts
      One "id|our_url" per line. Phase A Step 3 post-matching index.

  insert-reply --post-id N --comment-urn URN --author A --content C --href URL
      Find-or-create a pending linkedin reply. Idempotent: a duplicate
      (platform, their_comment_id) returns 409 which we treat as success.
      Prints the resulting reply id (or "gated"/"duplicate") for the log.

  create-post --activity-id ID --project NAME --author A
      Create (or reuse) a linkedin post row for a discovered thread when no
      existing post matched the activity id. Prints the post id. 409
      duplicate_thread reuses the existing row's id.

All three read subcommands hit GET /api/v1/linkedin-discovery-context once
and slice the field they need, so a caller that needs all three can also just
run `context` to dump the raw JSON.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_post


def _context():
    resp = api_get("/api/v1/linkedin-discovery-context")
    return resp.get("data") or {}


def cmd_comment_ids():
    for cid in _context().get("existing_comment_ids") or []:
        print(cid)


def cmd_engaged_pairs():
    for pair in _context().get("engaged_pairs") or []:
        print(pair)


def cmd_posts():
    for p in _context().get("posts") or []:
        print(f"{p['id']}|{p['our_url']}")


def cmd_context():
    print(json.dumps(_context(), indent=2))


def cmd_insert_reply(args):
    resp = api_post(
        "/api/v1/replies",
        {
            "platform": "linkedin",
            "post_id": args.post_id,
            "their_comment_id": args.comment_urn,
            "their_author": args.author,
            "their_content": args.content or "",
            "their_comment_url": args.href or "",
            "depth": 1,
            "status": "pending",
        },
        ok_on_conflict=True,
    )
    error = resp.get("error") or {}
    if error.get("code") == "duplicate_reply":
        print("duplicate")
        return
    data = resp.get("data") or {}
    # The blocklist / velocity gate returns ok with reply:null + gated reason.
    if data.get("gated"):
        print("gated:%s" % data.get("gated"))
        return
    reply = data.get("reply") or {}
    print(reply.get("id", "inserted"))


def cmd_create_post(args):
    our_url = (
        "https://www.linkedin.com/feed/update/"
        "urn:li:activity:%s/" % args.activity_id
    )
    resp = api_post(
        "/api/v1/posts",
        {
            "platform": "linkedin",
            "thread_url": our_url,
            "our_url": our_url,
            "our_content": "[discovered via notification, no original content tracked]",
            "project": args.project or "general",
            "thread_author": args.author or "(unknown)",
            "our_account": "Matthew Diakonov",
            "engagement_style": "discovered_via_notification",
            "status": "active",
        },
        ok_on_conflict=True,
    )
    error = resp.get("error") or {}
    if error.get("code") == "duplicate_thread":
        print((error.get("details") or {}).get("existing_post_id"))
        return
    post = (resp.get("data") or {}).get("post") or {}
    print(post.get("id", ""))


def main():
    p = argparse.ArgumentParser(description="LinkedIn Phase A discovery helpers (HTTP).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("comment-ids")
    sub.add_parser("engaged-pairs")
    sub.add_parser("posts")
    sub.add_parser("context")

    ins = sub.add_parser("insert-reply")
    ins.add_argument("--post-id", type=int, required=True)
    ins.add_argument("--comment-urn", required=True)
    ins.add_argument("--author", required=True)
    ins.add_argument("--content", default="")
    ins.add_argument("--href", default="")

    cp = sub.add_parser("create-post")
    cp.add_argument("--activity-id", required=True)
    cp.add_argument("--project", default="general")
    cp.add_argument("--author", default="")

    args = p.parse_args()
    if args.cmd == "comment-ids":
        cmd_comment_ids()
    elif args.cmd == "engaged-pairs":
        cmd_engaged_pairs()
    elif args.cmd == "posts":
        cmd_posts()
    elif args.cmd == "context":
        cmd_context()
    elif args.cmd == "insert-reply":
        cmd_insert_reply(args)
    elif args.cmd == "create-post":
        cmd_create_post(args)


if __name__ == "__main__":
    main()
