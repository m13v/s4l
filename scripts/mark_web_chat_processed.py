#!/usr/bin/env python3
"""Stamp web_chat_threads.processed_at = NOW() for a thread.

Called by skill/check-web-chats.sh at the END of a successful Claude session
(exit code 0), regardless of whether Claude replied or skipped. This is the
idempotency gate the recovery query in check_unread_web_chats.py uses to
avoid re-spawning Claude on threads that have already been handled.

Without this, threads where Claude legitimately skipped (smoke test, off-topic,
no useful answer) get re-flagged unread every cooldown expiry for 24h, since
last_message_sender stays 'visitor' (no agent message inserted on skip).

Usage:
  python3 mark_web_chat_processed.py <thread_id>
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
           SET processed_at = NOW()
         WHERE thread_id = %s
        """,
        (args.thread_id,),
    )
    conn.commit()
    print(f"marked thread {args.thread_id} processed_at=NOW()")


if __name__ == "__main__":
    main()
