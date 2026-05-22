#!/usr/bin/env python3
"""Poll Postgres for new visitor messages on a thread, blocking until one arrives.

Mirror of ~/fazm/inbox/scripts/poll-chat.js. The Claude session calls this
after sending a reply so it stays in the conversation while the visitor is
active.

Usage:
  python3 poll_web_chat.py --thread <thread_id> --after <ISO timestamp>
                           [--timeout 180] [--interval 15]

Exit codes:
  0  new visitor message(s) found (prints JSON array)
  1  error
  2  timeout (no new visitor messages within timeout)
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread", required=True)
    parser.add_argument("--after", required=True, help="ISO timestamp; only messages strictly after will return")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--interval", type=int, default=15)
    args = parser.parse_args()

    dbmod.load_env()
    conn = dbmod.get_conn()

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        cur = conn.execute(
            """
            SELECT id, sender, text, created_at
              FROM web_chat_messages
             WHERE thread_id = %s
               AND sender = 'visitor'
               AND created_at > %s::timestamptz
             ORDER BY created_at ASC
            """,
            (args.thread, args.after),
        )
        rows = cur.fetchall()
        if rows:
            out = [
                {
                    "id": r["id"],
                    "text": r["text"],
                    "sender": r["sender"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else "",
                }
                for r in rows
            ]
            print(json.dumps(out))
            sys.exit(0)
        time.sleep(args.interval)

    print("[]")
    sys.exit(2)


if __name__ == "__main__":
    main()
