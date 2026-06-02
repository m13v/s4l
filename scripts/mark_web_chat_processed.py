#!/usr/bin/env python3
"""Stamp web_chat_threads.processed_at = NOW() for a thread (HTTP-only).

POST /api/v1/web-chat/threads/<thread_id>/processed. Called by
skill/check-web-chats.sh at the END of a successful Claude session (exit code
0), regardless of whether Claude replied or skipped. This is the idempotency
gate the recovery query in /api/v1/web-chat/unread uses to avoid re-spawning
Claude on threads that have already been handled.

Usage:
  python3 mark_web_chat_processed.py <thread_id>
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_post


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("thread_id")
    args = parser.parse_args()

    api_post(f"/api/v1/web-chat/threads/{args.thread_id}/processed", {})
    print(f"marked thread {args.thread_id} processed_at=NOW()")


if __name__ == "__main__":
    main()
