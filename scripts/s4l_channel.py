#!/usr/bin/env python3
"""Release-channel knob for a single S4L box (stable vs staging).

ONE tiny file that every update surface reads so a box can opt into pre-release
(`staging`) builds without affecting any other box:

- `stable` (default): the box tracks GitHub `releases/latest`, i.e. the newest
  NON-prerelease release. This is the historical behavior and what every box
  gets when this file / the channel marker is absent.
- `staging`: the box tracks the newest release OVERALL (prerelease RCs included).
  A staging box is therefore always >= stable: it picks up each `-rc.N` first,
  and once an RC is promoted to a full release it stays current on that too.

The channel lives in a single JSON marker in the state dir so the TypeScript MCP
server (mcp/src/version.ts), the menu-bar snapshot (scripts/snapshot.py), the
menu bar itself (mcp/menubar/s4l_menubar.py), and the SSH updater
(scripts/s4l_box_update.sh) all resolve the SAME value. Keep the filename and the
semantics in lockstep across those four surfaces.

CLI (SSH-drivable, zero deps beyond stdlib):
    python3 scripts/s4l_channel.py get                 # -> stable | staging
    python3 scripts/s4l_channel.py set staging         # opt in
    python3 scripts/s4l_channel.py set stable          # opt back out
"""
from __future__ import annotations

import json
import os
import sys

VALID_CHANNELS = ("stable", "staging")
DEFAULT_CHANNEL = "stable"
CHANNEL_FILE = "channel.json"


def state_dir() -> str:
    return os.environ.get("SAPS_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".social-autoposter-mcp"
    )


def channel_path() -> str:
    return os.path.join(state_dir(), CHANNEL_FILE)


def read_channel() -> str:
    """Current channel for this box. Any read error or unknown value falls back
    to `stable` (fail-safe: a corrupt marker must never silently push a box onto
    pre-release builds)."""
    try:
        with open(channel_path()) as f:
            v = (json.load(f) or {}).get("channel")
        if isinstance(v, str) and v.strip().lower() in VALID_CHANNELS:
            return v.strip().lower()
    except Exception:
        pass
    return DEFAULT_CHANNEL


def is_staging() -> bool:
    return read_channel() == "staging"


def set_channel(channel: str) -> str:
    channel = (channel or "").strip().lower()
    if channel not in VALID_CHANNELS:
        raise ValueError(
            "invalid channel %r (want one of %s)" % (channel, ", ".join(VALID_CHANNELS))
        )
    d = state_dir()
    os.makedirs(d, exist_ok=True)
    tmp = channel_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"channel": channel}, f)
        f.write("\n")
    os.replace(tmp, channel_path())
    return channel


def _main(argv) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    cmd = argv[0]
    if cmd == "get":
        print(read_channel())
        return 0
    if cmd == "set":
        if len(argv) < 2:
            print("usage: s4l_channel.py set <stable|staging>", file=sys.stderr)
            return 2
        try:
            print(set_channel(argv[1]))
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        return 0
    print("unknown command: %s (want get|set)" % cmd, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
