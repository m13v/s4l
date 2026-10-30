#!/usr/bin/env python3
"""config.py — THE config.json loader. One repo-root resolution, one parser.

Before this module (2026-07-14), ~120 call sites each rolled their own
`json.load(open(.../config.json))` with at least nine different repo-root
variable conventions (REPO_DIR, REPO_ROOT, ROOT, _REPO_ROOT, ...), and most
hardcoded `~/social-autoposter`. That hardcode class runs code OUTSIDE the
managed package on customer boxes (unreachable by auto-update) and silently
no-ops where the directory doesn't exist — the exact bug that broke session
restore and tab cleanup on customer installs (S4L-4H triage 2026-07-12).

Usage:
    from config import repo_dir, config_path, load_config, get

    cfg = load_config()                      # dict, cached
    projects = get("projects", [])           # top-level key
    handle = get("accounts.twitter.handle")  # dotted path
    cfg = load_config(fresh=True)            # bypass cache (long-lived procs)

Resolution order for the repo root (single source of truth):
  1. $S4L_REPO_DIR      — set by managed-install launchd plists and the MCP
                          server, points at ~/.social-autoposter-mcp/repo/package
  2. this file's parent — scripts/ lives one level under the repo root, so a
                          direct checkout resolves to itself without any env
  3. ~/social-autoposter — legacy operator-box fallback

$S4L_CONFIG_PATH overrides the config file location outright (some installs
pin the operator config, e.g. ~/s4l/config.json, independent of the code dir).

Config writes: user-facing edits go through the MCP server's project_config
tool (it owns validation and the state snapshot). The few pipeline scripts
that must persist runtime state into config.json (subreddit bans) MUST use
update_config() below and nothing else: never `open(config, "w")` +
json.dump. That in-place pattern truncates the file to 0 bytes and streams it
back, so any interruption leaves a torn prefix that breaks every reader
(2026-09-06, 09-30, 10-22, 10-29 outages; the 10-29 one stopped all X
drafting for ~23h).
"""

from __future__ import annotations

import fcntl
import json
import os
import sys

_CACHE: dict | None = None
_CACHE_PATH: str | None = None
_CACHE_MTIME: float | None = None


def repo_dir() -> str:
    env = os.environ.get("S4L_REPO_DIR")
    if env:
        return os.path.expanduser(env)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.exists(os.path.join(here, "config.json")):
        return here
    return os.path.expanduser("~/social-autoposter")


def config_path() -> str:
    env = os.environ.get("S4L_CONFIG_PATH")
    if env:
        p = os.path.expanduser(env)
    else:
        sp = state_config_path()
        p = sp if os.path.exists(sp) else os.path.join(repo_dir(), "config.json")
    # Resolve symlinks to the real target. os.replace()-style atomic writers
    # otherwise REPLACE a symlink with a plain file instead of writing through
    # it, which is exactly how the operator Mac's config split in two on
    # 2026-07-11 and again on 2026-07-13. realpath of a plain file is itself,
    # so customer installs are unaffected.
    return os.path.realpath(p) if os.path.exists(p) else p


def state_config_path() -> str:
    """Canonical home of config.json (2026-07-13): the state dir, which no
    package update, re-materialize, or plist regeneration ever touches. The
    MCP server migrates a legacy $S4L_REPO_DIR/config.json here on boot and
    leaves a symlink behind for direct-path readers; config_path() prefers
    this file whenever it exists."""
    state_dir = os.environ.get("S4L_STATE_DIR") or "~/.social-autoposter-mcp"
    return os.path.join(os.path.expanduser(state_dir), "config.json")


def load_config(fresh: bool = False) -> dict:
    """Parsed config.json. Cached per (path, mtime); pass fresh=True to force
    a re-read regardless (e.g. long-lived processes reacting to edits)."""
    global _CACHE, _CACHE_PATH, _CACHE_MTIME
    path = config_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if not fresh and _CACHE is not None and _CACHE_PATH == path and _CACHE_MTIME == mtime:
        return _CACHE
    try:
        with open(path) as f:
            _CACHE = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _CACHE if (_CACHE is not None and _CACHE_PATH == path) else {}
    _CACHE_PATH = path
    _CACHE_MTIME = mtime
    return _CACHE


def update_config(mutate, path: str | None = None):
    """Locked, atomic read-modify-write of config.json.

    `mutate(cfg)` edits the dict in place and returns a truthy value when it
    changed something (nothing is written otherwise); its return value is
    passed back to the caller. The whole read->mutate->write span holds the
    same fcntl lock on <config>.lock that learned_preferences.py takes, so
    concurrent writers cannot erase each other's edits. The new content goes
    to a temp file in the same directory and is swapped in with os.replace,
    so readers only ever see the complete old file or the complete new one;
    a writer killed mid-write leaves a stray temp file, never a torn config.

    Raises if config.json is missing or unparseable: callers must not
    "repair" a broken config by writing a partial dict over it.
    """
    global _CACHE, _CACHE_PATH, _CACHE_MTIME
    path = path or config_path()
    with open(path + ".lock", "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        with open(path) as f:
            cfg = json.load(f)
        result = mutate(cfg)
        if not result:
            return result
        tmp = f"{path}.tmp-{os.getpid()}"
        try:
            with open(tmp, "w") as f:
                f.write(json.dumps(cfg, indent=2) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        # One attribution line per write, so the next incident names its writer.
        caller = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else "?"
        print(f"[config] saved {path} by {caller} pid={os.getpid()}", file=sys.stderr)
        _CACHE, _CACHE_PATH, _CACHE_MTIME = None, None, None
    return result


def get(path: str, default=None):
    """Dotted-path lookup into config.json: get("accounts.twitter.handle").
    A single-segment path reads a top-level key. Lists are not traversed;
    fetch the list and iterate at the call site."""
    node = load_config()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def project(name: str) -> dict | None:
    """The projects[] entry with this name (case-insensitive), else None."""
    for p in load_config().get("projects") or []:
        if isinstance(p, dict) and str(p.get("name", "")).lower() == name.lower():
            return p
    return None
