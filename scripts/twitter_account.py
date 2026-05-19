"""Single source of truth for the Twitter handle this machine posts as.

Resolution order (first non-empty wins):

  1. Env var `AUTOPOSTER_TWITTER_HANDLE` (used by the VM systemd unit to
     override config.json without touching the checked-in file).
  2. `accounts.twitter.handle` in `config.json` at the repo root.

The handle is normalized: leading `@` and surrounding whitespace are
stripped. So both `@matt_diak` and `matt_diak` resolve to `matt_diak`,
matching the canonical shape stored in `posts.our_account`.

Returns None if neither source has a value. Callers should treat None as
"unknown account" and decline to scope per-account work that needs a
handle (e.g. engaged-tweet-ids filtering).
"""
from __future__ import annotations

import json
import os
from functools import lru_cache


def _normalize(handle: str | None) -> str | None:
    if not handle:
        return None
    h = handle.strip()
    if h.startswith("@"):
        h = h[1:]
    h = h.strip()
    return h or None


@lru_cache(maxsize=1)
def _from_config() -> str | None:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg_path = os.path.join(repo_root, "config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    accounts = cfg.get("accounts") or {}
    twitter = accounts.get("twitter") or {}
    return _normalize(twitter.get("handle"))


def resolve_handle() -> str | None:
    """Return the normalized Twitter handle for this machine, or None."""
    env = _normalize(os.environ.get("AUTOPOSTER_TWITTER_HANDLE"))
    if env:
        return env
    return _from_config()


def require_handle() -> str:
    """Like resolve_handle() but raises if no handle is configured. Use this
    in code paths where scoping by account is mandatory (e.g. the stats
    refresh queries)."""
    h = resolve_handle()
    if not h:
        raise RuntimeError(
            "No Twitter handle configured. Set AUTOPOSTER_TWITTER_HANDLE "
            "or accounts.twitter.handle in config.json."
        )
    return h


if __name__ == "__main__":
    import sys
    h = resolve_handle()
    if h:
        sys.stdout.write(h + "\n")
        sys.exit(0)
    sys.stderr.write("no twitter handle configured\n")
    sys.exit(1)
