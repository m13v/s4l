#!/usr/bin/env python3
"""Claim a web-chat thread (reset unread_by_founder=0, set 5-min cooldown).

HTTP-only: POST /api/v1/web-chat/threads/<thread_id>/claim. The cooldown
prevents multiple Claude sessions from spawning for the same visitor when they
send rapid-fire messages.

Usage:
  python3 claim_web_chat.py <thread_id> [--check-only]

--check-only: exit 0 if claimable, exit 2 if still in cooldown (no write).
Without --check-only: claims (sets unread_by_founder=0, claimed_until=now+5min).
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_post


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("thread_id")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    resp = api_post(
        f"/api/v1/web-chat/threads/{args.thread_id}/claim",
        {"check_only": bool(args.check_only)},
    )
    data = resp.get("data") or {}
    state = data.get("state")

    if state == "cooldown":
        print(f"thread {args.thread_id} in cooldown until {data.get('claimed_until')}")
        sys.exit(2)
    if state == "claimable":
        print(f"thread {args.thread_id} claimable")
        sys.exit(0)
    # state == "claimed"
    print(f"claimed thread {args.thread_id} (cooldown {data.get('cooldown_min', 5)}m)")


if __name__ == "__main__":
    main()
