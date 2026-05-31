#!/usr/bin/env python3
"""Universal browser watchdog + activity logger (no-edit instrumentation).

Runs as a launchd single-shot every 60s (com.m13v.browser-watch). Two jobs:

  1. REAP  — kill any browser-harness/server.py whose parent is PID 1 (a true
             orphan: its owning Claude/uv session died but stdin-EOF cleanup
             never fired). Logged as action=reap. Currently rare (0 at install
             time) but this is the safety net the user asked for so future
             orphans get caught + recorded instead of silently piling up.

  2. POLL  — snapshot the live tab URL on each harness CDP port (9555 twitter,
             9556 linkedin, 9557 reddit) via the DevTools /json endpoint and
             append a line to browser-activity.log. This is the ONLY coverage
             for the Python-CDP scripts (linkedin_browser.py,
             discover_linkedin_candidates.py, reddit_browser.py) which attach
             via connect_over_cdp and bypass the instrumented MCP server.py.
             A LinkedIn URL showing up on port 9555 (twitter) here is the
             smoking gun for a cross-profile leak.

Stdlib only (urllib, subprocess) so launchd can run it under /usr/bin/python3
with zero deps. Best-effort: never raises into launchd.
"""
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

ACTIVITY_LOG = Path(
    os.environ.get(
        "BH_ACTIVITY_LOG",
        str(Path.home() / ".claude" / "browser-profiles" / "browser-activity.log"),
    )
)

# port -> the harness that is SUPPOSED to own it. Used to flag leaks.
PORT_OWNER = {9555: "twitter-harness", 9556: "linkedin-harness", 9557: "reddit-harness"}

# host substring -> platform, to detect a tab on the wrong harness.
HOST_PLATFORM = [
    ("linkedin.com", "linkedin"),
    ("reddit.com", "reddit"),
    ("twitter.com", "twitter"),
    ("x.com", "twitter"),
]


def _append(line: str) -> None:
    try:
        ACTIVITY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ACTIVITY_LOG.open("a") as f:
            f.write(line if line.endswith("\n") else line + "\n")
    except Exception:
        pass


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def reap_orphans() -> None:
    """Kill browser-harness/server.py processes whose parent is PID 1."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,ppid,command"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return
    for ln in out.splitlines():
        if "browser-harness/server.py" not in ln or "grep" in ln:
            continue
        parts = ln.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if ppid != 1:
            continue  # only true orphans
        try:
            os.kill(pid, 15)  # SIGTERM, graceful
            _append(
                f"[{_ts()}] watch action=reap pid={pid} ppid=1 "
                f"reason=orphan_parent_dead sig=TERM"
            )
        except Exception as e:
            _append(f"[{_ts()}] watch action=reap_fail pid={pid} err={e!r}")


def poll_ports() -> None:
    """Log the active tab URL on each harness CDP port; flag wrong-profile."""
    for port, owner in PORT_OWNER.items():
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json", timeout=2
            ) as r:
                tabs = json.load(r)
        except Exception:
            continue  # harness down; nothing to log
        # pick the foreground page tab(s) with a real http(s) url
        urls = [
            t.get("url", "")
            for t in tabs
            if t.get("type") == "page"
            and t.get("url", "").startswith("http")
            and "newtab" not in t.get("url", "")
        ]
        if not urls:
            continue
        for url in urls[:5]:
            platform = next(
                (p for host, p in HOST_PLATFORM if host in url), "other"
            )
            owner_platform = owner.split("-")[0]
            leak = ""
            if platform != "other" and platform != owner_platform:
                leak = f" LEAK_wrong_profile expected={owner_platform} got={platform}"
            _append(
                f"[{_ts()}] watch action=cdp_poll port={port} owner={owner} "
                f"platform={platform} url={url[:120]}{leak}"
            )


def rotate_if_large(max_bytes: int = 10 * 1024 * 1024) -> None:
    """Keep browser-activity.log bounded: roll to .1 once it passes max_bytes."""
    try:
        if ACTIVITY_LOG.exists() and ACTIVITY_LOG.stat().st_size > max_bytes:
            bak = ACTIVITY_LOG.with_suffix(ACTIVITY_LOG.suffix + ".1")
            ACTIVITY_LOG.replace(bak)  # atomic; .1 overwritten if present
    except Exception:
        pass


def main() -> None:
    rotate_if_large()
    reap_orphans()
    poll_ports()


if __name__ == "__main__":
    main()
