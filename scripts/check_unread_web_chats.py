#!/usr/bin/env python3
"""Check for web-chat threads with unread visitor messages (HTTP-only).

Reads GET /api/v1/web-chat/unread, which (1) recovers stuck threads and
(2) returns each unread, claimable thread with its first 200 messages embedded
in one round trip. Replaces the inline psycopg2 reads.

Prints a JSON array of:
  { thread_id, project, visitor_email, visitor_name, unread, last_message,
    page_url, messages: [{ id, text, sender, sender_name, created_at, read }] }
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get


def main():
    resp = api_get("/api/v1/web-chat/unread")
    threads = (resp.get("data") or {}).get("threads") or []
    print(json.dumps(threads))


if __name__ == "__main__":
    main()
