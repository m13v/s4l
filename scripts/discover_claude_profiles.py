#!/usr/bin/env python3
"""Discover Claude Desktop profiles on this Mac, which one is running, and what's installed.

A "profile" is any user-data-dir Claude Desktop has ever run with:
  - the default:  ~/Library/Application Support/Claude
  - named ones:   ~/Library/Application Support/Claude-<label>  (account rotator convention)

Running detection: parse `ps` for /Applications/Claude.app/Contents/MacOS/Claude
and read its --user-data-dir flag (absence of the flag = default profile).
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

APP_SUPPORT = Path.home() / "Library" / "Application Support"
ROTATOR_LABELS = Path.home() / "claude-account-rotator" / "labels.json"

# Dirs that start with "Claude" but are not Claude Desktop profiles
NOT_PROFILES = {"Claude Extensions", "ClaudeMeter", "claude-code"}


def is_profile_dir(p: Path) -> bool:
    if not p.is_dir() or p.name in NOT_PROFILES:
        return False
    # An Electron user-data-dir has Local State and/or Preferences; a used
    # Claude profile additionally has config.json.
    return (p / "Local State").exists() or (p / "config.json").exists()


def rotator_accounts() -> dict:
    try:
        return json.loads(ROTATOR_LABELS.read_text())
    except Exception:
        return {}


def running_instances() -> dict:
    """Return {resolved_profile_path: pid} for every live Claude main process."""
    out = subprocess.run(
        ["ps", "ax", "-o", "pid=,command="], capture_output=True, text=True
    ).stdout
    found = {}
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(/Applications/Claude\.app/Contents/MacOS/Claude)(\s|$)", line)
        if not m:
            continue
        pid = int(m.group(1))
        # Value may contain spaces ("Application Support"); it runs to the
        # next --flag or end of line.
        dm = re.search(r"--user-data-dir=(.+?)(?=\s+--|$)", line)
        profile = Path(dm.group(1)) if dm else APP_SUPPORT / "Claude"
        found[str(profile)] = pid
    return found


def profile_info(p: Path, running: dict, accounts: dict) -> dict:
    info = {
        "profile": p.name,
        "path": str(p),
        "running_pid": running.get(str(p)),
    }
    # Account label: rotator convention Claude-<label>
    label = p.name.removeprefix("Claude-") if p.name != "Claude" else "default"
    acct = accounts.get(label, {})
    info["account_label"] = label
    info["account_email"] = acct.get("email")
    info["account_status"] = acct.get("status")

    cfg_path = p / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
            # Logged-in org UUIDs leak through the dxt allowlist cache keys
            orgs = sorted(
                {k.split(":")[-1] for k in cfg if k.startswith("dxt:allowlistEnabled:")}
            )
            info["org_uuids"] = orgs
            info["signed_in"] = "oauth:tokenCacheV2" in cfg or "oauth:tokenCache" in cfg
        except Exception as e:
            info["config_error"] = str(e)

    ext_dir = p / "Claude Extensions"
    info["extensions"] = sorted(d.name for d in ext_dir.iterdir() if d.is_dir()) if ext_dir.is_dir() else []

    # Last activity: mtime of config.json (touched constantly while running)
    try:
        ts = cfg_path.stat().st_mtime if cfg_path.exists() else p.stat().st_mtime
        info["last_active"] = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
    except OSError:
        pass
    return info


def main():
    profiles = sorted(
        (p for p in APP_SUPPORT.glob("Claude*") if is_profile_dir(p)),
        key=lambda p: p.name,
    )
    running = running_instances()
    accounts = rotator_accounts()
    result = {
        "running_count": len(running),
        "profiles": [profile_info(p, running, accounts) for p in profiles],
    }
    # Flag any running instance whose profile dir we failed to enumerate
    known = {str(p) for p in profiles}
    orphans = {path: pid for path, pid in running.items() if path not in known}
    if orphans:
        result["running_unknown_profiles"] = orphans
    json.dump(result, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
