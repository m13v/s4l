#!/usr/bin/env python3
"""Backend-first client for subreddit ban/quarantine state.

SOURCE OF TRUTH (2026-07-19): the `subreddit_bans` table in the backend,
via GET/POST /api/config/subreddit-bans (install-scoped through the standard
X-Installation identity in http_api). The local config.json
subreddit_bans.{comment_blocked,thread_blocked} lists are demoted to a
WRITE-THROUGH CACHE / offline fallback: writers (post_reddit.
mark_thread_blocked, reddit_ban_check.record_confirmed_bans) keep writing
config.json for offline robustness AND mirror every write here; readers
(pick_thread_target, reddit_tools) union backend entries with the local list
so neither a missed mirror nor an API outage can un-ban a sub.

Caching: GET responses are cached on disk next to config.json for
CACHE_TTL seconds so per-cycle readers do not add an API round-trip each.
On API failure a stale cache (any age) is served before giving up; a `None`
return means "backend unknown, use your local fallback only".

Usage (ad-hoc):
    python3 scripts/subreddit_bans_client.py fetch thread_blocked
    python3 scripts/subreddit_bans_client.py record thread_blocked somesub \
        --reason "thread_removed_by_moderation: test" --project Podlog
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_post  # noqa: E402

CACHE_TTL = 600  # seconds
VALID_KINDS = ("comment_blocked", "thread_blocked")


def _cache_path() -> str:
    try:
        from config import config_path
        return os.path.join(os.path.dirname(os.path.abspath(config_path())),
                            ".subreddit_bans_cache.json")
    except Exception:
        return os.path.expanduser("~/social-autoposter/.subreddit_bans_cache.json")


def _cache_key(kind: str, account: str | None) -> str:
    return f"{kind}|{account or ''}"


def _read_cache() -> dict:
    try:
        with open(_cache_path()) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _write_cache(cache: dict) -> None:
    try:
        tmp = _cache_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, _cache_path())
    except Exception:
        pass  # cache is best-effort by definition


def invalidate_cache() -> None:
    try:
        os.remove(_cache_path())
    except OSError:
        pass


def fetch_entries(kind: str, account: str | None = None,
                  max_age: float = CACHE_TTL):
    """Return the backend's entry list for one kind (config.json entry shape:
    {sub, added_at, reason, account, noticed_by_project}), or None when the
    backend is unreachable AND no cached copy exists. Callers MUST treat None
    as "unknown" and fall back to their local list, never as "no bans"."""
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {VALID_KINDS}")
    key = _cache_key(kind, account)
    cache = _read_cache()
    hit = cache.get(key)
    if hit and (time.time() - float(hit.get("ts") or 0)) < max_age:
        return hit.get("entries")
    try:
        resp = api_get("/api/config/subreddit-bans",
                       {"kind": kind, "account": account})
        entries = (resp or {}).get(kind)
        if not isinstance(entries, list):
            raise ValueError(f"unexpected response shape: {type(entries)}")
        cache[key] = {"ts": time.time(), "entries": entries}
        _write_cache(cache)
        return entries
    except BaseException as e:  # http_api raises SystemExit on terminal failure
        if isinstance(e, KeyboardInterrupt):
            raise
        if hit:  # stale beats nothing
            return hit.get("entries")
        return None


def record(kind: str, sub: str, reason: str | None = None,
           account: str | None = None, project: str | None = None,
           added_at: str | None = None) -> bool:
    """Mirror one ban entry to the backend. Best-effort: returns False on any
    failure (caller's local config.json write already happened, and readers
    union both sources, so a missed mirror degrades to local-only state)."""
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {VALID_KINDS}")
    entry = {"kind": kind, "sub": sub, "reason": reason, "account": account,
             "noticed_by_project": project, "added_at": added_at}
    try:
        api_post("/api/config/subreddit-bans",
                 {"entries": [{k: v for k, v in entry.items() if v is not None}]})
        invalidate_cache()
        return True
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            raise
        print(f"[subreddit_bans_client] backend mirror failed for r/{sub}: {e}",
              file=sys.stderr)
        return False


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub_ap = ap.add_subparsers(dest="cmd", required=True)
    f = sub_ap.add_parser("fetch")
    f.add_argument("kind", choices=VALID_KINDS)
    f.add_argument("--account", default=None)
    f.add_argument("--fresh", action="store_true", help="bypass cache")
    r = sub_ap.add_parser("record")
    r.add_argument("kind", choices=VALID_KINDS)
    r.add_argument("sub")
    r.add_argument("--reason", default=None)
    r.add_argument("--account", default=None)
    r.add_argument("--project", default=None)
    args = ap.parse_args()
    from http_api import load_env
    load_env()
    if args.cmd == "fetch":
        entries = fetch_entries(args.kind, args.account,
                                max_age=0 if args.fresh else CACHE_TTL)
        print(json.dumps(entries, indent=2, default=str))
        return 0 if entries is not None else 1
    ok = record(args.kind, args.sub, args.reason, args.account, args.project)
    print("mirrored" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
