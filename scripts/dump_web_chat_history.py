#!/usr/bin/env python3
"""Dump a thread's full message history as JSON for prompt injection (HTTP-only).

Reads GET /api/v1/web-chat/threads/<thread_id>?limit=N. Used by
skill/check-web-chats.sh to build the Claude prompt:

    $(python3 scripts/dump_web_chat_history.py --thread $THREAD_ID)

Returns:
  {
    "thread_id": ..., "project": ..., "visitor_email": ..., "visitor_name": ...,
    "page_url": ..., "user_agent": ..., "referrer": ...,
    "created_at": "...", "last_message_at": "...",
    "messages": [{ id, sender, sender_name, text, created_at, read }, ...]
  }
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread", required=True)
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    resp = api_get(
        f"/api/v1/web-chat/threads/{args.thread}",
        query={"limit": args.limit},
        ok_on_404=True,
    )
    if resp.get("_not_found"):
        print(json.dumps({"error": f"thread {args.thread} not found"}))
        sys.exit(1)

    out = resp.get("data") or {}
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
