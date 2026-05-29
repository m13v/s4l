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
    """Return (is_stale, reason) for a lock dir we found existing.

    Checked in order:
      1. pid file missing / unparseable / zero
      2. holder PID is dead
      3. lease TTL expired (`now() > expires_at`) — primary stale signal
      4. lock dir mtime older than STALE_LOCK_AGE (final backstop, only
         matters for legacy locks that never wrote `expires_at`)
    """
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
    # Primary staleness signal: TTL expired (no heartbeat in TTL window).
    expires_at = read_expires_at(lock_dir)
    if expires_at is not None:
        idle = time.time() - expires_at
        if idle > 0:
            return True, f"ttl_expired_idle_{int(idle)}s_holder_{holder}"
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


def sweep_orphan_browser_processes(name: str) -> None:
    """Kill orphan Chrome / playwright-mcp processes reparented to PID 1.

    Ported from skill/lock.sh:175-198 on 2026-05-10 as part of the migration
    that consolidates reddit-browser locking onto a single TTL-aware system.

    A prior holder may have exited without cleanly closing Chrome (parent
    playwright-mcp died with SIGKILL/OOM, Chrome reparented to PID 1, profile
    stays locked). Since we just acquired the exclusive lock, any Chrome on
    this profile is an orphan and safe to kill before the caller launches
    a fresh MCP session.

    The ppid==1 filter is load-bearing: a live peer's Chromium is parented
    to its mcp wrapper (alive). Without the guard, a peer that acquired
    concurrently would SIGTERM the legitimate holder's Chrome and trigger
    crashes like the GPU exit_code=15 we saw on 2026-04-28 14:12 PT.

    Only fires for `*-browser` locks; no-op for pipeline locks.
    """
    if not name.endswith("-browser"):
        return
    platform = name[: -len("-browser")]
    agent_marker = f"{platform}-agent.json"

    def _udd_is_platform_profile(cmd_str: str) -> bool:
        """True only when --user-data-dir points at the EXACT browser-profiles/<platform>
        dir (or a subdir of it), NOT a sibling like browser-profiles/<platform>-harness.

        The old test (`f"browser-profiles/{platform}" in cmd`) was a plain substring
        match, so for platform="reddit" it also matched "browser-profiles/reddit-harness"
        and swept the persistent reddit-harness Chrome (launched detached -> ppid=1) on
        every single lock acquire. That is exactly what kept killing the harness mid-cycle
        during the 2026-05-29 migration. Compare the first path component after
        "browser-profiles/" against the platform name instead.
        """
        marker = "user-data-dir="
        idx = cmd_str.find(marker)
        if idx == -1:
            return False
        val = cmd_str[idx + len(marker):].split(" ", 1)[0].strip().strip('"').strip("'")
        key = "browser-profiles/"
        j = val.find(key)
        if j == -1:
            return False
        seg = val[j + len(key):].split("/", 1)[0]
        return seg == platform

    try:
        r = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,command="],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return

    chrome_pids: list[int] = []
    mcp_pids: list[int] = []
    for line in r.stdout.splitlines():
        # ps output: "  PID  PPID command..."
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, ppid_s, cmd = parts
        if ppid_s != "1":
            continue
        if "user-data-dir=" in cmd and _udd_is_platform_profile(cmd):
            try:
                chrome_pids.append(int(pid_s))
            except ValueError:
                pass
        elif agent_marker in cmd:
            try:
                mcp_pids.append(int(pid_s))
            except ValueError:
                pass

    for pid in chrome_pids:
        try:
            os.kill(pid, 15)  # SIGTERM
        except (ProcessLookupError, PermissionError):
            pass
        except OSError:
            pass
    if chrome_pids:
        print(
            f"# swept orphan Chrome (ppid=1) holding {platform} profile: {chrome_pids}",
            flush=True,
        )
        time.sleep(1)

    for pid in mcp_pids:
        try:
            os.kill(pid, 15)
        except (ProcessLookupError, PermissionError):
            pass
        except OSError:
            pass
    if mcp_pids:
        print(
            f"# swept orphan MCP wrappers (ppid=1) for {platform}-agent: {mcp_pids}",
            flush=True,
        )
        time.sleep(1)


def cmd_acquire(name: str, timeout: int, ttl: int) -> int:
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
                    # Write expires_at FIRST, then pid_file. Order matters:
                    # peers reading a partially-initialized lock during the
                    # tiny window between the two writes will see an absent
                    # pid_file → `no_pid_file` → treated as stale → safe
                    # steal. They never see "valid pid + no TTL" which would
                    # mean "respect lock indefinitely".
                    write_expires_at(lock_dir, time.time() + ttl)
                    pid_file.write_text(f"{owner_pid}\n")
                    # Sweep orphan Chrome / MCP wrappers reparented to PID 1
                    # before the caller launches a fresh MCP session. Ported
                    # from lock.sh:175-198 (2026-05-10) so the bash and Python
                    # locks no longer diverge in housekeeping behavior.
                    sweep_orphan_browser_processes(name)
                    print(
                        f"OK owner_pid={owner_pid} waited={waited:.1f} ttl={ttl}",
                        flush=True,
                    )
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


def cmd_heartbeat(name: str, ttl: int) -> int:
    """Bump the lease expiry. Called by the MCP wrapper on browser activity.

    Design intent: the heartbeat IS the activity signal. If any reddit-agent
    MCP browser call is happening anywhere on the box, the lease should stay
    alive — independent of which process tree branch is firing the bump. So
    we bump unconditionally as long as the lock dir exists.

    Why no ownership check: the orchestrator's bash subprocess (which calls
    `acquire`) and the MCP wrapper's heartbeat subprocess descend from
    different parents, so a strict `holder_pid == find_owner_pid()` check
    can falsely reject legit heartbeats in test environments and even in
    edge cases in prod (e.g. when the launchd → script → claude chain
    walks differently for a python subprocess vs. a bash subprocess).
    The lock's correctness is enforced at acquire/release; heartbeat is
    just a "yes, work is happening, don't expire me yet" pulse.

    Worst case if a peer's wrapper accidentally bumps the holder's lease:
    the holder keeps the lock 90s longer than strictly necessary. Bounded
    by `--ttl`. The peer's `acquire` queue still proceeds in FIFO order
    once activity ceases.
    """
    lock_dir, _pid_file, _ = lock_paths(name)
    if not lock_dir.is_dir():
        print("NOT_HELD", flush=True)
        return 1
    new_expires = time.time() + ttl
    if not write_expires_at(lock_dir, new_expires):
        print("NOT_HELD", flush=True)
        return 1
    print(f"OK expires_at={new_expires:.0f}", flush=True)
    return 0


def cmd_status(name: str) -> int:
    lock_dir, pid_file, queue_dir = lock_paths(name)
    info = {
        "name": name,
        "held": False,
        "holder_pid": None,
        "age_sec": None,
        "expires_at": None,
        "ttl_remaining_sec": None,
        "expired": False,
        "queue": [],
    }
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
        expires_at = read_expires_at(lock_dir)
        if expires_at is not None:
            info["expires_at"] = expires_at
            remaining = expires_at - time.time()
            info["ttl_remaining_sec"] = round(remaining, 1)
            info["expired"] = remaining <= 0
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
    p_acq.add_argument("--ttl", type=int, default=DEFAULT_LEASE_TTL_SECONDS,
                       help=f"Initial lease TTL in seconds (default {DEFAULT_LEASE_TTL_SECONDS}). "
                            "MCP browser wrapper will heartbeat to keep this fresh during real activity.")

    p_rel = sub.add_parser("release", help="Release the lock if held by us.")
    p_rel.add_argument("--name", default=DEFAULT_NAME)

    p_hb = sub.add_parser("heartbeat", help="Extend the lease (called by the MCP wrapper on browser activity).")
    p_hb.add_argument("--name", default=DEFAULT_NAME)
    p_hb.add_argument("--ttl", type=int, default=DEFAULT_LEASE_TTL_SECONDS,
                      help=f"Seconds to extend from now (default {DEFAULT_LEASE_TTL_SECONDS}).")

    p_stat = sub.add_parser("status", help="Print JSON state of the lock.")
    p_stat.add_argument("--name", default=DEFAULT_NAME)

    args = p.parse_args()
    if args.cmd == "acquire":
        return cmd_acquire(args.name, args.timeout, args.ttl)
    if args.cmd == "release":
        return cmd_release(args.name)
    if args.cmd == "heartbeat":
        return cmd_heartbeat(args.name, args.ttl)
    if args.cmd == "status":
        return cmd_status(args.name)
    return 2


if __name__ == "__main__":
    sys.exit(main())
