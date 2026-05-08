#!/usr/bin/env python3
"""Dump a thread's full message history as JSON for prompt injection.

Used by skill/check-web-chats.sh to build the Claude prompt:

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
import db as dbmod


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread", required=True)
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    dbmod.load_env()
    conn = dbmod.get_conn()

    thread = conn.execute(
        """
        SELECT thread_id, project_name, visitor_email, visitor_name,
               page_url, user_agent, referrer, created_at, last_message_at
          FROM web_chat_threads
         WHERE thread_id = %s
        """,
        (args.thread,),
    ).fetchone()

    if not thread:
        print(json.dumps({"error": f"thread {args.thread} not found"}))
        sys.exit(1)

    msgs = conn.execute(
        """
        SELECT id, sender, sender_name, text, read_by_founder, created_at
          FROM web_chat_messages
         WHERE thread_id = %s
         ORDER BY created_at ASC
         LIMIT %s
        """,
        (args.thread, args.limit),
    ).fetchall()

    out = {
        "thread_id": thread["thread_id"],
        "project": thread["project_name"],
        "visitor_email": thread["visitor_email"] or "",
        "visitor_name": thread["visitor_name"] or "",
        "page_url": thread["page_url"] or "",
        "user_agent": thread["user_agent"] or "",
        "referrer": thread["referrer"] or "",
        "created_at": thread["created_at"].isoformat() if thread["created_at"] else "",
        "last_message_at": thread["last_message_at"].isoformat() if thread["last_message_at"] else "",
        "messages": [
            {
                "id": m["id"],
                "sender": m["sender"],
                "sender_name": m["sender_name"] or "",
                "text": m["text"] or "",
                "read": bool(m["read_by_founder"]),
                "created_at": m["created_at"].isoformat() if m["created_at"] else "",
            }
            for m in msgs
        ],
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
