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
import os
import sys
import urllib.request

# Port can be overridden via BH_CLEANUP_PORT so the LinkedIn backend
# (skill/lib/linkedin-backend.sh) can reuse this same cleanup script against
# its own harness Chrome on 9556. Default 9555 keeps Twitter callers unchanged.
CDP_PORT = int(os.environ.get("BH_CLEANUP_PORT", "9555"))
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"


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
    # Keep a REAL (http/https) tab when one exists, not blindly pages[0]. The
    # /json order is roughly most-recently-active first, so a freshly-spawned
    # about:blank can sit at index 0 and the old code would keep the blank and
    # close the live x.com tab the harness daemon is attached to. Closing the
    # daemon's tab forces it to re-attach and re-spawn another about:blank, which
    # is exactly the orphan-tab churn this script is meant to clean up. Falling
    # back to pages[0] preserves the prior behavior when every tab is blank.
    def _is_real(t):
        return (t.get("url") or "").startswith(("http://", "https://"))

    keep = next((t for t in pages if _is_real(t)), pages[0])
    closed = 0
    for t in pages:
        if t is keep:
            continue
        tid = t.get("id")
        if not tid:
            continue
        try:
            urllib.request.urlopen(f"{CDP_URL}/json/close/{tid}", timeout=2).read()
            closed += 1
        except Exception:
            pass
    kept_kind = "1 real" if _is_real(keep) else "1"
    print(f"[cleanup_harness_tabs] closed {closed}/{len(pages) - 1} extra page tabs (kept {kept_kind})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
