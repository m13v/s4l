#!/usr/bin/env python3
"""Posting-volume mode CLI (2026-07-13).

Thin wrapper over /api/v1/installations/posting-mode for the MCP `posting_volume`
tool and the panel. The mode (high|medium|low|None) is stored SERVER-SIDE on
installations.posting_mode; the virality-threshold route maps it to a
percentile and overrides the cycle driver's request, so changes apply on the
install's next cycle with no client update. There is deliberately NO local
mode.json copy that RESOLVES the mode (dashboard, menubar, and panel must
always agree with the server).

Display cache (2026-07-17): a last-known copy of the server's answer IS kept
at <state_dir>/posting-mode.cache.json, written on every successful get/set
and read only when the network is unavailable (and by the menubar to seed its
boot display). Without it, every menubar/panel restart showed the default
"Steady" until the first network round trip landed, which read as the user's
chosen mode having been reset. The cache never overrides a server answer.

Usage:
  python3 scripts/s4l_posting_mode.py get
      -> {"mode": ..., "rates": [{mode,pctile,threshold,est_posts_per_day}...],
          "pool_count": N, "batch_count": N, "window_days": 7}
      (on network failure with a cache present: {"mode": ..., "cached": true})
  python3 scripts/s4l_posting_mode.py set <high|medium|low|default>
      -> {"mode": ...}   ("default" clears the override)

Prints JSON on stdout; exits non-zero with an {"error": ...} JSON on failure.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_post  # noqa: E402

VALID = ("high", "medium", "low")


def _cache_path() -> str:
    state_dir = os.environ.get("S4L_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".social-autoposter-mcp"
    )
    return os.path.join(state_dir, "posting-mode.cache.json")


def read_cached_mode():
    """Last-known SERVER mode ('high'|'medium'|'low') or None (unset/no cache).
    Display-only: the server stays the source of truth."""
    try:
        with open(_cache_path()) as fh:
            d = json.load(fh)
        m = d.get("mode")
        return m if m in VALID else None
    except Exception:
        return None


def write_cached_mode(mode):
    """Persist the last-known server mode (None = server says unset). Best
    effort, never raises: a cache miss only costs the boot display."""
    try:
        path = _cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"mode": mode if mode in VALID else None, "at": time.time()}, fh)
        os.replace(tmp, path)
    except Exception:
        pass


def main() -> int:
    argv = sys.argv[1:]
    cmd = argv[0] if argv else "get"
    try:
        if cmd == "get":
            try:
                # http_api raises SystemExit (not Exception) on terminal
                # failure, so both must be caught here and below.
                r = api_get("/api/v1/installations/posting-mode")
            except (Exception, SystemExit) as e:
                # Offline / server unreachable: answer from the last-known
                # cache so UIs keep showing the user's chosen mode instead of
                # silently falling back to "Steady". No "error" key: consumers
                # treat that as a hard failure and drop the payload.
                cached = read_cached_mode()
                if cached is not None:
                    print(json.dumps({"mode": cached, "cached": True}))
                    return 0
                raise e
            d = (r or {}).get("data") or {}
            if "mode" in d:
                write_cached_mode(d.get("mode"))
            print(json.dumps(d))
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
            d = (r or {}).get("data") or {}
            if "mode" in d:
                write_cached_mode(d.get("mode"))
            print(json.dumps(d))
            return 0
        print(json.dumps({"error": f"unknown command {cmd!r}"}))
        return 2
    except (Exception, SystemExit) as e:  # network / API failure: JSON error, non-zero exit
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
