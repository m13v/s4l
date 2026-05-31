#!/usr/bin/env python3
"""human_dm_replies_helper.py — shell-friendly entrypoints for Phase 0 of
engage-dm-replies.sh, replacing the inline `psql "$DATABASE_URL"` blocks that
read and mutate the `human_dm_replies` queue.

Everything routes through /api/v1/human-dm-replies* on the website. No
psycopg2, no DATABASE_URL — there is intentionally NO direct-DB fallback.

Subcommands:

  pending [--platform reddit|twitter|x|linkedin]
    GET /api/v1/human-dm-replies?mode=pending[&platform=...]
    -> prints the pending+retry queue as a JSON array (the same shape the
       legacy `SELECT json_agg(q) FROM (... human_dm_replies h JOIN dms ...)`
       produced). Prints NOTHING (empty output) when zero rows, so the bash
       `[ -n "$HUMAN_REPLIES" ]` guard falls through to the "no replies"
       branch exactly like psql's NULL -> empty string did.

  kb [--limit 20]
    GET /api/v1/human-dm-replies?mode=kb&limit=N
    -> prints the last N SENT instructions as a JSON array (the Human Reply
       Knowledge Base). Empty -> empty output (same as the legacy query).

  patch --id N [--status S] [--last-error E] [--public-reply-id M]
        [--increment-attempts] [--stamp-sent]
    PATCH /api/v1/human-dm-replies/N
    -> the four Phase 0 status transitions:
         cancelled : --status cancelled --last-error "human reclassified: ..."
         paired    : --public-reply-id M
         sent      : --status sent
         failed    : --status failed --increment-attempts --last-error "..."
       (sent_at auto-stamps server-side on sent/cancelled; --stamp-sent forces
        it otherwise.)

  insert-public-reply --post-id N|--no-post-id --platform P --comment-id C
        --author A --comment-url U --our-content TEXT --our-url URL [--depth 2]
    POST /api/v1/replies
    -> inserts the public reply row the delivery bot just posted and prints
       the new replies.id to stdout (so it can be paired back via
       `patch --public-reply-id`). 409 (duplicate their_comment_id) returns the
       existing row's id, never an error.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_patch, api_post


def _print_rows_or_empty(rows):
    """Mirror psql `json_agg` -t -A output: a JSON array when rows exist,
    an empty string (nothing) when there are none."""
    if not rows:
        return
    json.dump(rows, sys.stdout)
    print("")


def _cmd_pending(args):
    query = {"mode": "pending"}
    if args.platform:
        query["platform"] = args.platform
    resp = api_get("/api/v1/human-dm-replies", query=query)
    rows = ((resp or {}).get("data") or {}).get("rows") or []
    _print_rows_or_empty(rows)


def _cmd_kb(args):
    query = {"mode": "kb", "limit": args.limit}
    resp = api_get("/api/v1/human-dm-replies", query=query)
    rows = ((resp or {}).get("data") or {}).get("rows") or []
    _print_rows_or_empty(rows)


def _cmd_patch(args):
    body = {}
    if args.status is not None:
        body["status"] = args.status
    if args.last_error is not None:
        body["last_error"] = args.last_error
    if args.public_reply_id is not None:
        body["public_reply_id"] = int(args.public_reply_id)
    if args.increment_attempts:
        body["increment_attempts"] = True
    if args.stamp_sent:
        body["stamp_sent_now"] = True
    if not body:
        print("patch: nothing to update (pass at least one field)", file=sys.stderr)
        sys.exit(2)
    resp = api_patch(f"/api/v1/human-dm-replies/{int(args.id)}", body)
    row = ((resp or {}).get("data") or {}).get("human_dm_reply") or {}
    print(json.dumps({"id": row.get("id"), "status": row.get("status"),
                      "attempts": row.get("attempts"),
                      "public_reply_id": row.get("public_reply_id")}))


def _cmd_insert_public_reply(args):
    body = {
        "platform": args.platform,
        "their_comment_id": args.comment_id,
        "their_author": args.author,
        "their_comment_url": args.comment_url,
        "our_reply_content": args.our_content,
        "our_reply_url": args.our_url,
        "depth": args.depth,
        "status": "replied",
        "replied_at": "now",
    }
    if args.post_id is not None:
        body["post_id"] = int(args.post_id)
    # ok_on_conflict so a duplicate their_comment_id returns the existing row
    # instead of raising; we still want its id to pair back.
    resp = api_post("/api/v1/replies", body, ok_on_conflict=True)
    data = (resp or {}).get("data") or {}
    row = data.get("reply") or data.get("row") or data
    rid = row.get("id") if isinstance(row, dict) else None
    if rid is None and isinstance(data, dict):
        # 409 path may nest the existing row under error.details
        err = (resp or {}).get("error") or {}
        details = err.get("details") if isinstance(err, dict) else None
        if isinstance(details, dict):
            rid = (details.get("reply") or details).get("id")
    if rid is None:
        print("insert-public-reply: no id in response: "
              + json.dumps(resp)[:300], file=sys.stderr)
        sys.exit(1)
    print(int(rid))


def main():
    p = argparse.ArgumentParser(description="human_dm_replies Phase 0 helper (HTTP-only)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("pending")
    sp.add_argument("--platform", default=None)
    sp.set_defaults(func=_cmd_pending)

    sk = sub.add_parser("kb")
    sk.add_argument("--limit", type=int, default=20)
    sk.set_defaults(func=_cmd_kb)

    spt = sub.add_parser("patch")
    spt.add_argument("--id", required=True)
    spt.add_argument("--status", default=None)
    spt.add_argument("--last-error", dest="last_error", default=None)
    spt.add_argument("--public-reply-id", dest="public_reply_id", default=None)
    spt.add_argument("--increment-attempts", dest="increment_attempts", action="store_true")
    spt.add_argument("--stamp-sent", dest="stamp_sent", action="store_true")
    spt.set_defaults(func=_cmd_patch)

    si = sub.add_parser("insert-public-reply")
    si.add_argument("--post-id", dest="post_id", default=None)
    si.add_argument("--platform", required=True)
    si.add_argument("--comment-id", dest="comment_id", required=True)
    si.add_argument("--author", required=True)
    si.add_argument("--comment-url", dest="comment_url", required=True)
    si.add_argument("--our-content", dest="our_content", required=True)
    si.add_argument("--our-url", dest="our_url", required=True)
    si.add_argument("--depth", type=int, default=2)
    si.set_defaults(func=_cmd_insert_public_reply)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
