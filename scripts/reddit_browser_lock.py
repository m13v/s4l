#!/usr/bin/env python3
"""reddit_browser_lock.py — explicit per-post browser lock for Reddit.

Why this exists
---------------
link-edit-reddit.sh used to acquire the bash-level reddit-browser lock for
its entire claude session (~90 min). During that window the orchestrator
was 99% in SEO page-gen / DB / file-IO time, NOT actually using the reddit
browser. Other reddit pipelines (engage-reddit, dm-replies-reddit,
post-reddit) sat blocked the whole time.

This helper lets the claude orchestrator acquire/release the reddit-browser
lock per post, so the lock is only held during the actual `mcp__reddit-agent__*`
browser ops (typically 15-60s per comment edit) instead of the full run.

Interop with skill/lock.sh
--------------------------
We share the exact lock-dir format used by skill/lock.sh:

    /tmp/social-autoposter-<name>.lock/      (the lock; mkdir-atomic)
        pid                                  (one line: owner PID)
    /tmp/social-autoposter-<name>.lock.queue/   (FIFO ticket queue)
        <ns_timestamp>-<pid>                 (one ticket per waiter)

Bash and Python helpers can BOTH compete for the same lock safely. Stale
detection mirrors lock.sh: missing pid file, dead holder PID, or lock-dir
age > 3h all trigger steal.

Owner PID
---------
The lock's owner PID is NOT this short-lived python process — that would
be detected as dead the moment we exit. Instead, we walk up the process
tree from os.getppid() looking for the long-lived link-edit-reddit.sh
(or any claude --session-id ancestor) and use THAT pid. If none is found,
we fall back to os.getppid() (typically the bash subprocess from claude's
Bash tool, which lives at least until the tool call returns).

Lease/TTL semantics (added 2026-05-08)
--------------------------------------
On acquire we also write `expires_at` (a Unix timestamp) inside the lock dir.
The lock is considered STALE (and stealable by a peer) once `now() > expires_at`.

The MCP browser wrapper (`scripts/mcp_lock_proxy.py`) bumps `expires_at` on every
`tools/call` request that crosses it, and again on every response, plus a periodic
30s pulse while a request is in flight. So as long as actual reddit-agent browser
work is happening, the lease keeps renewing. The moment the holder stops calling
the browser (page-gen, sleeps, DB writes, agent crashes), no more bumps fire and
the lease auto-expires within `LEASE_TTL_SECONDS` seconds (default 90). Peers
in the queue then see the lock as stale and steal it.

This eliminates orphaned-lock outages without needing the agent to remember to
call `release` in every code path. `release` still works (and is preferred when
the agent knows it's done early), but is no longer load-bearing for correctness.

CLI
---
    python3 reddit_browser_lock.py acquire   [--name reddit-browser] [--timeout 600] [--ttl 90]
    python3 reddit_browser_lock.py release   [--name reddit-browser]
    python3 reddit_browser_lock.py status    [--name reddit-browser]
    python3 reddit_browser_lock.py heartbeat [--name reddit-browser] [--ttl 90]

Acquire prints a single line:
    OK owner_pid=<N> waited=<sec> ttl=<sec>     (success)
    BUSY holder_pid=<N> age=<sec>               (timed out)
    ERROR <reason>                              (unexpected)

Release prints:
    OK
    NOT_HELD                          (lock dir missing)
    HELD_BY_OTHER holder_pid=<N>      (don't release — different owner)
    ERROR <reason>

Heartbeat prints:
    OK expires_at=<unix_ts>           (lease extended)
    NOT_HELD                          (no lock dir; nothing to extend)
    HELD_BY_OTHER holder_pid=<N>      (we're not the owner; refused to bump)
    ERROR <reason>

Status prints JSON:
    {"name":"...", "held":bool, "holder_pid":N|null, "age_sec":N|null,
     "expires_at":F|null, "ttl_remaining_sec":F|null, "expired":bool, "queue":[..]}

Exit codes
----------
    0 — success / lock state read cleanly
    1 — timeout / busy / not-held / refused-foreign-release
    2 — usage / argument error
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

LOCK_ROOT = "/tmp"
DEFAULT_NAME = "reddit-browser"
DEFAULT_ACQUIRE_TIMEOUT = 600  # 10 min — generous for a per-post browser slot
POLL_INTERVAL = 2.0
STALE_LOCK_AGE = 10800  # 3h, matches lock.sh safety net (final backstop)

# Lease/TTL: how long the lock stays valid without a heartbeat. The MCP
# wrapper auto-heartbeats during browser activity, so a holder that stops
# making MCP browser calls will see its lease expire after this many seconds
# of idleness, allowing peers to steal the lock.
#
# Sized from real reddit-agent MCP call distribution (n=2390, 14 days):
#   p99 = 30s, max-legit = 5.6 min (one outlier on browser_close).
#   90s = 15x p99, leaves comfortable headroom inside the wrapper's 30s
#   periodic pulse for in-flight calls. For the rare 5+ min outlier the
#   pulse keeps renewing, so the lease never accidentally expires under
#   real activity.
DEFAULT_LEASE_TTL_SECONDS = 90


def lock_paths(name: str) -> tuple[Path, Path, Path]:
    base = Path(LOCK_ROOT) / f"social-autoposter-{name}.lock"
    return base, base / "pid", Path(f"{base}.queue")


def expires_file_path(lock_dir: Path) -> Path:
    return lock_dir / "expires_at"


def read_expires_at(lock_dir: Path) -> float | None:
    f = expires_file_path(lock_dir)
    if not f.is_file():
        return None
    try:
        return float(f.read_text().strip())
    except Exception:
        return None


def write_expires_at(lock_dir: Path, ts: float) -> bool:
    """Write `expires_at` atomically. Returns True on success.

    Uses write-then-rename to keep readers from seeing a half-written value.
    Safe no-op if `lock_dir` was removed mid-write (e.g. lock got released
    or stolen between the existence check and the write).
    """
    try:
        if not lock_dir.is_dir():
            return False
        tmp = lock_dir / f"expires_at.tmp.{os.getpid()}"
        tmp.write_text(f"{ts:.3f}\n")
        tmp.replace(expires_file_path(lock_dir))
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists, we just can't signal it
        return True
    except OSError as e:
        return e.errno != errno.ESRCH


def ps_command(pid: int) -> str:
    """Return the full command line for `pid`, or '' if not found."""
    try:
        r = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def ps_ppid(pid: int) -> int:
    try:
        r = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
        return int(r.stdout.strip() or 0)
    except Exception:
        return 0


def find_owner_pid() -> int:
    """Walk up the process tree to find a long-lived owner.

    Looks for: link-edit-reddit.sh, run-reddit-search.sh, engage-reddit.sh,
    or any `claude --session-id` ancestor. Returns the FIRST match. Falls
    back to os.getppid() if none found within depth 12.
    """
    pid = os.getppid()
    for _ in range(12):
        if pid <= 1:
            break
        cmd = ps_command(pid)
        if not cmd:
            break
        if (
            "link-edit-reddit.sh" in cmd
            or "run-reddit-search.sh" in cmd
            or "engage-reddit.sh" in cmd
            or "engage-dm-replies-reddit.sh" in cmd
            or "scan-reddit-replies" in cmd
            or ("claude" in cmd and "--session-id" in cmd)
        ):
            return pid
        pid = ps_ppid(pid)
    return os.getppid()


def gc_stale_tickets(queue_dir: Path) -> None:
    if not queue_dir.is_dir():
        return
    for ticket in queue_dir.iterdir():
        try:
            tpid = int(ticket.read_text().strip() or "0")
        except Exception:
            continue
        if not pid_alive(tpid):
            try:
                ticket.unlink()
            except Exception:
                pass


def lock_is_stale(lock_dir: Path, pid_file: Path) -> tuple[bool, str]:
    """Return (is_stale, reason) for a lock dir we found existing."""
    if not pid_file.is_file():
        return True, "no_pid_file"
    try:
        holder = int(pid_file.read_text().strip() or "0")
    except Exception:
        return True, "unparseable_pid"
    if holder <= 0:
        return True, "zero_pid"
    if not pid_alive(holder):
        return True, f"dead_holder_{holder}"
    try:
        age = time.time() - lock_dir.stat().st_mtime
        if age > STALE_LOCK_AGE:
            return True, f"age_{int(age)}s_>_{STALE_LOCK_AGE}s"
    except FileNotFoundError:
        return True, "lock_dir_vanished"
    return False, ""


def remove_lock(lock_dir: Path) -> None:
    try:
        shutil.rmtree(lock_dir, ignore_errors=True)
    except Exception:
        pass


def cmd_acquire(name: str, timeout: int) -> int:
    lock_dir, pid_file, queue_dir = lock_paths(name)
    queue_dir.mkdir(parents=True, exist_ok=True)

    owner_pid = find_owner_pid()
    ticket_name = f"{time.time_ns()}-{os.getpid()}"
    ticket_path = queue_dir / ticket_name
    try:
        ticket_path.write_text(f"{owner_pid}\n")
    except Exception as e:
        print(f"ERROR ticket_write_failed:{e}", flush=True)
        return 2

    waited = 0.0
    try:
        while True:
            gc_stale_tickets(queue_dir)
            tickets = sorted([t.name for t in queue_dir.iterdir() if t.is_file()])
            head = tickets[0] if tickets else None
            if head == ticket_name:
                # Try to acquire by mkdir
                try:
                    lock_dir.mkdir()
                    pid_file.write_text(f"{owner_pid}\n")
                    print(f"OK owner_pid={owner_pid} waited={waited:.1f}", flush=True)
                    return 0
                except FileExistsError:
                    stale, reason = lock_is_stale(lock_dir, pid_file)
                    if stale:
                        print(f"# steal_stale_lock reason={reason}", flush=True)
                        remove_lock(lock_dir)
                        continue
            if waited >= timeout:
                # Identify holder for diagnostics
                holder_pid = None
                age = None
                if pid_file.is_file():
                    try:
                        holder_pid = int(pid_file.read_text().strip() or "0")
                    except Exception:
                        holder_pid = None
                if lock_dir.is_dir():
                    try:
                        age = int(time.time() - lock_dir.stat().st_mtime)
                    except FileNotFoundError:
                        age = None
                print(
                    f"BUSY holder_pid={holder_pid if holder_pid else 'unknown'} "
                    f"age={age if age is not None else 'unknown'}s waited={waited:.1f}s",
                    flush=True,
                )
                return 1
            time.sleep(POLL_INTERVAL)
            waited += POLL_INTERVAL
    finally:
        # Clean up our ticket regardless of outcome
        try:
            ticket_path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass


def cmd_release(name: str) -> int:
    lock_dir, pid_file, _ = lock_paths(name)
    if not lock_dir.is_dir():
        print("NOT_HELD", flush=True)
        return 1
    holder_pid = None
    if pid_file.is_file():
        try:
            holder_pid = int(pid_file.read_text().strip() or "0")
        except Exception:
            holder_pid = None
    expected_owner = find_owner_pid()
    if holder_pid is not None and holder_pid != expected_owner and pid_alive(holder_pid):
        # Don't release a lock we didn't acquire
        print(f"HELD_BY_OTHER holder_pid={holder_pid}", flush=True)
        return 1
    remove_lock(lock_dir)
    print("OK", flush=True)
    return 0


def cmd_status(name: str) -> int:
    lock_dir, pid_file, queue_dir = lock_paths(name)
    info = {"name": name, "held": False, "holder_pid": None, "age_sec": None, "queue": []}
    if lock_dir.is_dir():
        info["held"] = True
        if pid_file.is_file():
            try:
                info["holder_pid"] = int(pid_file.read_text().strip() or "0")
            except Exception:
                info["holder_pid"] = None
        try:
            info["age_sec"] = int(time.time() - lock_dir.stat().st_mtime)
        except FileNotFoundError:
            pass
    if queue_dir.is_dir():
        info["queue"] = sorted([t.name for t in queue_dir.iterdir() if t.is_file()])
    print(json.dumps(info), flush=True)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Per-post browser lock helper for the reddit-agent profile.")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_acq = sub.add_parser("acquire", help="Acquire the lock; blocks up to --timeout sec.")
    p_acq.add_argument("--name", default=DEFAULT_NAME)
    p_acq.add_argument("--timeout", type=int, default=DEFAULT_ACQUIRE_TIMEOUT,
                       help=f"Max seconds to wait (default {DEFAULT_ACQUIRE_TIMEOUT}).")

    p_rel = sub.add_parser("release", help="Release the lock if held by us.")
    p_rel.add_argument("--name", default=DEFAULT_NAME)

    p_stat = sub.add_parser("status", help="Print JSON state of the lock.")
    p_stat.add_argument("--name", default=DEFAULT_NAME)

    args = p.parse_args()
    if args.cmd == "acquire":
        return cmd_acquire(args.name, args.timeout)
    if args.cmd == "release":
        return cmd_release(args.name)
    if args.cmd == "status":
        return cmd_status(args.name)
    return 2


if __name__ == "__main__":
    sys.exit(main())
