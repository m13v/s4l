#!/usr/bin/env python3
"""Claim a web-chat thread (reset unread_by_founder=0, set 5-min cooldown).

Mirror of ~/fazm/inbox/scripts/claim-chat.js. The cooldown prevents multiple
Claude sessions from spawning for the same visitor when they send rapid-fire
messages.

Usage:
  python3 claim_web_chat.py <thread_id> [--check-only]

--check-only: exit 0 if claimable, exit 2 if still in cooldown (no write).
Without --check-only: claims (sets unread_by_founder=0, claimed_until=now+5min).
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod

COOLDOWN_MIN = 5


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("thread_id")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    dbmod.load_env()
    conn = dbmod.get_conn()

    row = conn.execute(
        "SELECT claimed_until FROM web_chat_threads WHERE thread_id = %s",
        (args.thread_id,),
    ).fetchone()

    if row and row["claimed_until"]:
        # claimed_until is timezone-aware; compare in DB to avoid TZ skew
        still = conn.execute(
            "SELECT claimed_until > NOW() AS still_locked FROM web_chat_threads WHERE thread_id = %s",
            (args.thread_id,),
        ).fetchone()
        if still and still["still_locked"]:
            print(f"thread {args.thread_id} in cooldown until {row['claimed_until']}")
            sys.exit(2)

    if args.check_only:
        print(f"thread {args.thread_id} claimable")
        sys.exit(0)

    conn.execute(
        f"""
        UPDATE web_chat_threads
           SET unread_by_founder = 0,
               claimed_until = NOW() + INTERVAL '{COOLDOWN_MIN} minutes'
         WHERE thread_id = %s
        """,
        (args.thread_id,),
    )
    conn.commit()
    print(f"claimed thread {args.thread_id} (cooldown {COOLDOWN_MIN}m)")


if __name__ == "__main__":
    main()
