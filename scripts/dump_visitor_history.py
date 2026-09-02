#!/usr/bin/env python3
"""Dump a visitor's cross-thread history as JSON for prompt injection.

Gives the web-chat agent memory of a returning visitor across ALL their
threads, so it never goes cold ("nothing like that on our side") on a known
user just because they opened a fresh thread. Reads:

    GET /api/v1/web-chat/visitor?email=<email>&exclude=<thread>&limit=N

Used by skill/check-web-chats.sh alongside dump_web_chat_history.py:

    $(python3 scripts/dump_visitor_history.py --email "$EMAIL" --exclude "$THREAD_ID")

Returns:
  {
    "visitor_email": ..., "thread_count": N, "excluded_thread": ...,
    "threads": [{ thread_id, project, page_url, created_at, last_message_at }, ...],
    "recent_messages": [{ thread_id, sender, sender_name, text, created_at }, ...]
  }

On any error (no email, thread-only visitor, endpoint 404) it prints an empty
skeleton and exits 0, so the caller can always inline the output safely.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get

EMPTY = {"visitor_email": "", "thread_count": 0, "threads": [], "recent_messages": []}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--exclude", default="", help="thread_id to leave out (the current one)")
    parser.add_argument("--project", default="")
    parser.add_argument("--limit", type=int, default=120)
    args = parser.parse_args()

    email = (args.email or "").strip()
    if not email or "@" not in email:
        print(json.dumps(EMPTY))
        return

    query = {"email": email, "limit": args.limit}
    if args.exclude:
        query["exclude"] = args.exclude
    if args.project:
        query["project"] = args.project

    try:
        resp = api_get("/api/v1/web-chat/visitor", query=query, ok_on_404=True)
    except Exception as e:  # never let a memory lookup crash the spawn
        print(json.dumps({**EMPTY, "visitor_email": email, "_error": str(e)}))
        return

    if resp.get("_not_found"):
        print(json.dumps({**EMPTY, "visitor_email": email}))
        return

    out = resp.get("data") or {}
    if not out:
        out = {**EMPTY, "visitor_email": email}
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
