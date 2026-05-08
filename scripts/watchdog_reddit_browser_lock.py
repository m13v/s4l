#!/usr/bin/env python3
"""Reclaim the reddit-browser bash file lock when the holder is not using the browser.

Why
---
The bash-level reddit-browser lock at `/tmp/social-autoposter-reddit-browser.lock/`
queues sibling reddit pipelines so they do not all drive the reddit-agent Chrome
profile at once. But most holders (post_reddit.py, link-edit-reddit Claude
session) acquire the lock and then spend most of their time on NON-browser work:
SEO page-gen, DB writes, ripening sleeps, 3-min between-post sleeps, Claude
streaming. During those minutes the lock sits held while the browser is idle
and peer pipelines starve.

The actual browser exclusion is enforced at a different layer: `scripts/reddit_browser.py`
acquires `~/.claude/reddit-agent-lock.json` (LOCK_EXPIRY=300, refreshed during
CDP work via `_refresh_browser_lock`) for the duration of every CDP invocation.
That is the "true" browser-busy signal. When that python lock file is fresh
(mtime within the last 60s), the browser IS actively in use. When it is stale
or missing, the browser is idle even if some pipeline is "holding" the bash lock.

This watchdog reclaims the bash lock when:
  1. bash lock exists AND its age > MIN_BASH_LOCK_AGE_SEC (do not kill normal
     CDP bursts which easily run 30-60s back-to-back);
  2. AND `~/.claude/reddit-agent-lock.json` missing OR mtime older than
     PY_LOCK_IDLE_THRESHOLD_SEC (no live CDP in flight).

The released bash lock falls to the next FIFO ticket and a queued pipeline
proceeds. Correctness is preserved by the python-level lock if two pipelines
happen to launch CDP near-simultaneously: one wins the python lock, the other
waits up to LOCK_WAIT_MAX=45s.

Trade-off
---------
A holder that is sleeping between posts (e.g., post_reddit.py 3-min gap) may
get a transient `locked by session ...` error from reddit_browser.py on its
next CDP call if a peer grabbed the python lock during the sleep. post_reddit's
post_via_cdp() already retries 5 times with up to ~2.5 min of total backoff,
so this is the expected, healthy path.

Logging
-------
Every reclaim is appended to `skill/logs/watchdog-reddit-lock.log`. If nothing
to reclaim, the watchdog exits silently to keep the log tight.

Run via launchd
---------------
Schedule with `launchd/com.m13v.social-watchdog-reddit-lock.plist` at
StartInterval=30. Cheap (~50ms per fire), safe to run more often than the bash
lock is acquired.
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path("/Users/matthewdi/social-autoposter")
LOG_FILE = REPO / "skill" / "logs" / "watchdog-reddit-lock.log"

BASH_LOCK_DIR = Path("/tmp/social-autoposter-reddit-browser.lock")
BASH_LOCK_PID_FILE = BASH_LOCK_DIR / "pid"
# Lease expiry (Unix timestamp) written by reddit_browser_lock.py acquire/heartbeat.
# A fresh lease (now < expires_at) is the canonical "browser is being driven by
# this pipeline" signal — it covers gaps between Python CDP subprocess calls
# (DB writes, ripening sleeps) that PY_LOCK_FILE mtime alone cannot bridge.
BASH_LOCK_EXPIRES_FILE = BASH_LOCK_DIR / "expires_at"
PY_LOCK_FILE = Path.home() / ".claude" / "reddit-agent-lock.json"

# Holder must hold the bash lock at least this long before we consider
# reclaiming. Tight back-to-back CDP bursts (3-5 posts) can run 60-90s
# legitimately; 60s is a deliberate floor below the bash watchdog cap.
MIN_BASH_LOCK_AGE_SEC = 60

# If the python lock has not been touched in this long, the browser is idle.
# reddit_browser.py refreshes the file mtime on every step of a CDP call;
# a fresh CDP shows mtime within the last few seconds.
PY_LOCK_IDLE_THRESHOLD_SEC = 90


def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        with LOG_FILE.open("a") as f:
            f.write(line)
    except OSError:
        pass
    print(line.rstrip())


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as e:
        return e.errno != errno.ESRCH


def ps_command(pid: int) -> str:
    try:
        r = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def main() -> int:
    if not BASH_LOCK_DIR.is_dir():
        return 0

    try:
        bash_lock_age = time.time() - BASH_LOCK_DIR.stat().st_mtime
    except FileNotFoundError:
        return 0

    if bash_lock_age < MIN_BASH_LOCK_AGE_SEC:
        return 0

    holder_pid = None
    if BASH_LOCK_PID_FILE.is_file():
        try:
            holder_pid = int(BASH_LOCK_PID_FILE.read_text().strip() or "0")
        except Exception:
            holder_pid = None

    # We do NOT short-circuit on dead holder; lock.sh and reddit_browser_lock.py
    # both already handle dead-holder reclamation on next acquire. We focus on
    # the "alive but not using browser" case which they cannot detect.

    py_lock_idle = float("inf")
    if PY_LOCK_FILE.exists():
        try:
            py_lock_idle = time.time() - PY_LOCK_FILE.stat().st_mtime
        except FileNotFoundError:
            pass

    if py_lock_idle < PY_LOCK_IDLE_THRESHOLD_SEC:
        return 0  # CDP actively in use; respect the lock.

    # Check the bash-lock LEASE (expires_at). reddit_browser.py bumps this on
    # every CDP subprocess invocation via _heartbeat_bash_lease(); the MCP
    # PreToolUse/PostToolUse hooks bump it on every reddit-agent tool call.
    # So a fresh lease means EITHER a Python CDP pipeline OR an MCP-driven
    # pipeline is alive and using the browser. Respect it.
    lease_remaining = float("-inf")
    if BASH_LOCK_EXPIRES_FILE.is_file():
        try:
            lease_expires_at = float(BASH_LOCK_EXPIRES_FILE.read_text().strip() or "0")
            lease_remaining = lease_expires_at - time.time()
        except (ValueError, OSError):
            pass

    if lease_remaining > 0:
        return 0  # Lease fresh; pipeline is actively heart-beating.

    holder_cmd = ps_command(holder_pid) if holder_pid else ""
    holder_alive = pid_alive(holder_pid) if holder_pid else False
    py_idle_str = (
        f"{int(py_lock_idle)}s" if py_lock_idle != float("inf") else "missing"
    )
    lease_str = (
        f"{int(lease_remaining)}s" if lease_remaining != float("-inf") else "missing"
    )
    log(
        f"reclaim bash_lock=reddit-browser bash_age={int(bash_lock_age)}s "
        f"py_idle={py_idle_str} lease_remaining={lease_str} "
        f"holder_pid={holder_pid} alive={holder_alive} "
        f"cmd={holder_cmd[:120]!r}"
    )

    try:
        shutil.rmtree(BASH_LOCK_DIR, ignore_errors=True)
    except Exception as e:
        log(f"  rmtree failed: {e}")
        return 1

    log("  released; next FIFO ticket will acquire")
    return 0


if __name__ == "__main__":
    sys.exit(main())
