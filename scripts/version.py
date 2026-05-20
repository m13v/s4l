"""Read the social-autoposter package.json version once and cache it.

Every write to posts / replies / dms stamps `autoposter_version` so we can
attribute engagement back to the release of the autoposter code that
produced it ("did 1.5.0 outperform 1.4.x on Reddit?").

The value comes from:
  1. AUTOPOSTER_VERSION env var, if set (lets us pin during testing or
     override for a one-off backfill).
  2. package.json `version` field in the repo root.

Returns None when both lookups fail. Callers MUST tolerate None and pass
it through to the API; the API stores NULL and the column stays empty for
that row rather than blocking the write.

Why not git SHA: the auto-commit agent at ~/git-dashboard/auto_commit.py
fires every minute, so the SHA changes constantly without release intent
and would be noise. The version string is manually bumped per meaningful
release (bin/cli.js + package.json), which is the right granularity for
"did this prompt change improve engagement?" analyses.
"""
from __future__ import annotations

import json
import os
from typing import Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG_PATH = os.path.join(_REPO_ROOT, "package.json")

_cached: Optional[str] = None
_cached_loaded = False


def read_version() -> Optional[str]:
    """Return the autoposter version string, or None if unavailable.

    Reads env first, then package.json. Result is cached for the process
    lifetime since the version never changes mid-run.
    """
    global _cached, _cached_loaded
    if _cached_loaded:
        return _cached

    env_val = (os.environ.get("AUTOPOSTER_VERSION") or "").strip()
    if env_val:
        _cached = env_val
        _cached_loaded = True
        return _cached

    try:
        with open(_PKG_PATH, "r", encoding="utf-8") as f:
            pkg = json.load(f)
        v = pkg.get("version")
        if isinstance(v, str) and v.strip():
            _cached = v.strip()
            _cached_loaded = True
            return _cached
    except (OSError, json.JSONDecodeError):
        pass

    _cached_loaded = True
    _cached = None
    return None


if __name__ == "__main__":
    # CLI: `python3 scripts/version.py` -> prints the version (or empty
    # line). Used by shell scripts that want to thread the value into env
    # before spawning sub-processes.
    v = read_version()
    print(v or "")
