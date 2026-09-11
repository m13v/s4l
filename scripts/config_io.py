#!/usr/bin/env python3
"""config_io.py — THE config.json writer: flock + tmp + os.replace.

scripts/config.py is the reader (and deliberately carries no write helpers);
this module is the single write path. Every config.json truncation traced in
the 2026-09-09 investigation (Sentry S4L-9E / S4L-95) was a non-atomic
`open(path, "w")` + `json.dump` either killed mid-stream or caught in the
write window by a concurrent reader. The mechanics that make a write safe
live here once (copied from scripts/learned_preferences.py, the one writer
that never truncated):

  1. exclusive fcntl.flock on <config>.lock — a sibling lock file, so the
     config itself is never opened for writing; serializes all writers that
     go through this module (learned_preferences.py uses the same lock path)
  2. serialize the full document to <config>.tmp
  3. os.replace() onto the real path — atomic on POSIX, so readers see either
     the old document or the new one, never a partial

Backups are OFF by default: the daily subreddit-ban writers would otherwise
mint an endless stream of .bak files. Pass backup=True for rare, high-value
writes (setup flows keep their own backups already).

Usage — one-shot overwrite (caller already holds the final dict):

    from config_io import save_config
    save_config(cfg)                      # default resolved config path
    save_config(cfg, cfg_path=path)

Usage — read-modify-write. The read happens under the same flock the write
uses, so two concurrent writers can no longer lose each other's update.
save() is explicit: leave without calling it and nothing is written.

    from config_io import locked_config
    with locked_config() as (cfg, save):
        bans = cfg.setdefault("subreddit_bans", {})
        ...mutate cfg in place...
        if changed:
            save()
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def _resolve(cfg_path: str | None) -> str:
    if cfg_path is None:
        from config import config_path

        return config_path()
    p = os.path.expanduser(cfg_path)
    # Write through symlinks, never over them: os.replace() on a symlink path
    # would swap the link itself for a plain file and split the config in two
    # (the 2026-07-11/13 operator-Mac incidents). realpath of a plain file is
    # itself, so non-symlink installs are unaffected.
    return os.path.realpath(p) if os.path.exists(p) else p


def _write_atomic(cfg: dict, path: str, backup: bool) -> None:
    if backup:
        try:
            if os.path.exists(path):
                stamp = datetime.now(timezone.utc).isoformat().replace(":", "-").replace(".", "-")
                shutil.copyfile(path, f"{path}.bak-{stamp}")
        except Exception:
            pass
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps(cfg, indent=2) + "\n")
    os.replace(tmp, path)


@contextmanager
def locked_config(cfg_path: str | None = None, backup: bool = False):
    """Exclusive read-modify-write session on config.json.

    Yields (cfg, save): cfg is the parsed document (fresh read under the
    lock), save() writes it back atomically while the lock is still held.
    Not calling save() abandons the session without writing.
    """
    path = _resolve(cfg_path)
    lock_path = path + ".lock"
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        cfg = json.loads(Path(path).read_text()) if os.path.exists(path) else {}

        def save() -> None:
            _write_atomic(cfg, path, backup)

        yield cfg, save


def save_config(cfg: dict, cfg_path: str | None = None, backup: bool = False) -> None:
    """Atomically overwrite config.json with cfg (flock + tmp + os.replace)."""
    path = _resolve(cfg_path)
    lock_path = path + ".lock"
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        _write_atomic(cfg, path, backup)
