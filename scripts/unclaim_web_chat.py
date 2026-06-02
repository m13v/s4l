#!/usr/bin/env python3
"""Unclaim a web-chat thread (re-set unread_by_founder=1, clear cooldown).

HTTP-only: POST /api/v1/web-chat/threads/<thread_id>/unclaim. Used when a Claude
session fails so the next pipeline tick will retry.

Usage:
  python3 unclaim_web_chat.py <thread_id>
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

    api_post(f"/api/v1/web-chat/threads/{args.thread_id}/unclaim", {})
    print(f"unclaimed thread {args.thread_id}")


if __name__ == "__main__":
    main()
