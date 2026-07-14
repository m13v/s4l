#!/usr/bin/env python3
"""Posting-volume mode CLI (2026-07-13).

Thin wrapper over /api/v1/installations/posting-mode for the MCP `posting_volume`
tool and the panel. The mode (high|medium|low|None) is stored SERVER-SIDE on
installations.posting_mode; the virality-threshold route maps it to a
percentile and overrides the cycle driver's request, so changes apply on the
install's next cycle with no client update. There is deliberately NO local
mode.json copy (dashboard, menubar, and panel must always agree with the
server).

Usage:
  python3 scripts/s4l_posting_mode.py get
      -> {"mode": ..., "rates": [{mode,pctile,threshold,est_posts_per_day}...],
          "pool_count": N, "batch_count": N, "window_days": 7}
  python3 scripts/s4l_posting_mode.py set <high|medium|low|default>
      -> {"mode": ...}   ("default" clears the override)

Prints JSON on stdout; exits non-zero with an {"error": ...} JSON on failure.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_post  # noqa: E402

VALID = ("high", "medium", "low")


def main() -> int:
    argv = sys.argv[1:]
    cmd = argv[0] if argv else "get"
    try:
        if cmd == "get":
            r = api_get("/api/v1/installations/posting-mode")
            print(json.dumps((r or {}).get("data") or {}))
            return 0
        if cmd == "set":
            if len(argv) < 2:
                print(json.dumps({"error": "usage: set <high|medium|low|default>"}))
                return 2
            raw = argv[1].strip().lower()
            mode = None if raw in ("default", "none", "null", "") else raw
            if mode is not None and mode not in VALID:
                print(json.dumps({"error": f"invalid mode {raw!r}"}))
                return 2
            r = api_post("/api/v1/installations/posting-mode", {"mode": mode})
            print(json.dumps((r or {}).get("data") or {}))
            return 0
        print(json.dumps({"error": f"unknown command {cmd!r}"}))
        return 2
    except Exception as e:  # network / API failure: JSON error, non-zero exit
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
