#!/usr/bin/env python3
"""Close every CDP "page" tab in harness Chrome except one.

Called from skill/lib/twitter-backend.sh::ensure_twitter_browser_for_backend
and skill/engage-twitter.sh's inline harness branch as part of pre-flight.
Safe to call any time: exits 0 silently when harness Chrome is down. Workers
and iframe targets are left alone; they auto-clean when their parent page
closes.

The standalone-script form (vs an inline heredoc) is required because bash
3.2 on macOS cannot parse a nested heredoc inside a function body inside a
sourced file. See git history around 2026-05-14 for the prior inline form
that broke every launchd-fired twitter script.
"""
from __future__ import annotations

import json
import sys
import urllib.request

CDP_URL = "http://127.0.0.1:9555"


def main() -> int:
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json", timeout=2) as r:
            tabs = json.loads(r.read())
    except Exception:
        return 0
    pages = [t for t in tabs if t.get("type") == "page"]
    if len(pages) <= 1:
        print(f"[cleanup_harness_tabs] {len(pages)} page tab(s), no cleanup needed")
        return 0
    closed = 0
    for t in pages[1:]:
        tid = t.get("id")
        if not tid:
            continue
        try:
            urllib.request.urlopen(f"{CDP_URL}/json/close/{tid}", timeout=2).read()
            closed += 1
        except Exception:
            pass
    print(f"[cleanup_harness_tabs] closed {closed}/{len(pages) - 1} extra page tabs (kept 1)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
