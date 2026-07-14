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

Do NOT add write helpers here. Config writes go through the MCP server's
project_config tool (it owns validation and the state snapshot); pipeline
scripts are readers.
"""

from __future__ import annotations

import json
import os

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
        return os.path.expanduser(env)
    return os.path.join(repo_dir(), "config.json")


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
