#!/usr/bin/env python3
"""Unclaim a web-chat thread (re-set unread_by_founder=1, clear cooldown).

Mirror of ~/fazm/inbox/scripts/unclaim-chat.js. Used when a Claude session
fails so the next pipeline tick will retry.

Usage:
  python3 unclaim_web_chat.py <thread_id>
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("thread_id")
    args = parser.parse_args()

    dbmod.load_env()
    conn = dbmod.get_conn()
    conn.execute(
        """
        UPDATE web_chat_threads
           SET unread_by_founder = GREATEST(unread_by_founder, 1),
               claimed_until = NULL
         WHERE thread_id = %s
        """,
        (args.thread_id,),
    )
    conn.commit()
    print(f"unclaimed thread {args.thread_id}")


if __name__ == "__main__":
    main()
