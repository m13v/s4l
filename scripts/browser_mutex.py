#!/usr/bin/env python3
"""browser_mutex.py — THE per-platform browser-session mutex (2026-07-14).

A faithful, parameterized extraction of scripts/twitter_browser.py's
battle-hardened file mutex, so every platform driver shares ONE implementation
instead of three divergent ones (reddit_browser.py's old copy still had the
check-then-write claim race AND the dead-holder starvation twitter fixed on
2026-06-16). The twitter semantics are preserved bit-for-bit: same lockfile
JSON shape ({session_id, timestamp, role}), same reclaim ladder, same
"locked by session" give-up string downstream parsers grep for.

This mutex serializes the PYTHON drivers of one harness Chrome. It deliberately
does NOT share a path with the shell pipeline lock (skill/lock.sh's
/tmp/social-autoposter-<name>.lock dirs): pipelines hold that lock around whole
phases while their child python ops take THIS one per op — merging the two onto
one path would deadlock that nesting (or need ancestor-walk inheritance in
every acquire). Unification here means one LIBRARY, not one lock.

Reclaim ladder (a holder we can PROVE dead is taken immediately, so a crashed
peer can never starve the fleet):
  1. holder == us            -> re-entrant; refresh and proceed.
  1b. holder == $S4L_LOCK_OWNER (live) -> batch inherit: a poster parent holds
      the lock across a whole approved batch; its child reply subprocesses
      refresh instead of contending, and leave release to the parent.
  2. UUID holder, pid gone   -> stale legacy (Claude session) lock, reclaim.
  3. python:PID, pid gone    -> dead peer, reclaim.
  4. age >= expiry           -> failsafe (role "post" holders get the shorter
                                post_lock_expiry so a hung poster self-clears).
  5. live UUID holder        -> inherit (parent Claude session).
  5b. we are role "post", holder is a LIVE lower-priority python peer ->
      PREEMPT it (SIGTERM, grace, SIGKILL) and claim: posting is the scarce
      user-initiated action; the aborted scan re-runs next tick.
  6. live python peer        -> wait, then give up after wait_max with the
                                structured "locked by session" error.

Do NOT "simplify" by letting shell pipelines rm -f the lockfile: that blind rm
deleted LIVE peers' locks (defect b, removed 2026-06-16). See
docs/twitter_browser_lock.md for the incident history behind every branch.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


class BrowserMutex:
    def __init__(
        self,
        lock_file: str,
        label: str,
        lock_expiry: int = 300,
        post_lock_expiry: int = 180,
        wait_max: int = 45,
        poll_interval: int = 2,
        preempt_kill_wait: int = 5,
        role: str | None = None,
        on_touch=None,
    ):
        """label is the human prefix in error strings ("Twitter browser",
        "Reddit browser") — downstream parsers grep "locked by session", keep
        the shape. on_touch (optional callable) runs after every successful
        acquire/refresh (reddit bumps its bash lease there); it must never
        raise consequences: exceptions are swallowed."""
        self.lock_file = os.path.expanduser(lock_file)
        self.label = label
        self.lock_expiry = lock_expiry
        self.post_lock_expiry = post_lock_expiry
        self.wait_max = wait_max
        self.poll_interval = poll_interval
        self.preempt_kill_wait = preempt_kill_wait
        self.role = (role or os.environ.get("S4L_LOCK_ROLE") or "scan").strip() or "scan"
        self.on_touch = on_touch
        self.session_id = f"python:{os.getpid()}"
        self.inherited = False
        self.acquired_at: float | None = None

    # ---- liveness probes ----------------------------------------------------
    @staticmethod
    def _is_uuid_holder_alive(holder: str) -> bool:
        if not holder:
            return False
        try:
            return (
                subprocess.run(
                    ["pgrep", "-f", f"claude.*--session-id {holder}"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                ).returncode
                == 0
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return True  # err on the side of NOT stealing

    @staticmethod
    def _is_python_holder_alive(holder: str) -> bool:
        if not holder.startswith("python:"):
            return True  # not a python holder; this probe makes no claim
        try:
            pid = int(holder.split(":", 1)[1])
        except (ValueError, IndexError):
            return True
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True

    # ---- claim / preempt ----------------------------------------------------
    def _try_take(self) -> bool:
        """O_CREAT|O_EXCL makes "is it free? then take it" one syscall, so two
        cold-start acquirers can't both win (defect c)."""
        try:
            fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        except OSError:
            return False
        try:
            os.write(fd, json.dumps(
                {"session_id": self.session_id, "timestamp": int(time.time()), "role": self.role}
            ).encode())
        finally:
            os.close(fd)
        return True

    def _preempt(self, pid: int) -> bool:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        deadline = time.time() + self.preempt_kill_wait
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            time.sleep(0.2)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        return False

    def _touch(self):
        if self.on_touch:
            try:
                self.on_touch()
            except Exception:
                pass

    # ---- public API -----------------------------------------------------------
    def acquire(self):
        deadline = time.time() + self.wait_max
        try:
            os.makedirs(os.path.dirname(self.lock_file), exist_ok=True)
        except OSError:
            pass
        while True:
            if not os.path.exists(self.lock_file):
                if self._try_take():
                    break
                if time.time() >= deadline:
                    print(json.dumps({
                        "success": False,
                        "error": f"{self.label} lock contended on create; waited {self.wait_max}s, giving up."
                    }))
                    sys.exit(1)
                time.sleep(self.poll_interval)
                continue
            try:
                with open(self.lock_file) as f:
                    lock = json.load(f)
            except (json.JSONDecodeError, OSError):
                if self._try_take():
                    break
                if time.time() >= deadline:
                    print(json.dumps({
                        "success": False,
                        "error": f"{self.label} lock unreadable; waited {self.wait_max}s, giving up."
                    }))
                    sys.exit(1)
                time.sleep(self.poll_interval)
                continue
            age = time.time() - lock.get("timestamp", 0)
            holder = lock.get("session_id", "")
            holder_role = lock.get("role", "scan")  # legacy locks (no role) = preemptable

            # 1. Re-entrant.
            if holder == self.session_id and not self.inherited:
                self.refresh()
                break

            # 1b. Batch-owner inherit (posting).
            batch_owner = os.environ.get("S4L_LOCK_OWNER") or ""
            if holder and holder == batch_owner and self._is_python_holder_alive(holder):
                self.session_id = holder
                self.inherited = True
                self.refresh()
                print(f"[browser_lock] inherited batch owner={holder} "
                      f"role={holder_role} -> pid={os.getpid()}", file=sys.stderr)
                break

            # 2-4. Reclaim provably dead/expired holders.
            reclaim_reason = ""
            if _UUID_RE.match(holder or "") and not self._is_uuid_holder_alive(holder):
                reclaim_reason = "dead_uuid"
            elif holder.startswith("python:") and not self._is_python_holder_alive(holder):
                reclaim_reason = "dead_python"
            elif age >= (self.post_lock_expiry if holder_role == "post" else self.lock_expiry):
                reclaim_reason = "expired"
            if reclaim_reason:
                try:
                    os.remove(self.lock_file)
                except OSError:
                    pass
                if self._try_take():
                    print(f"[browser_lock] reclaimed holder={holder or '<none>'} "
                          f"reason={reclaim_reason} age={int(age)}s -> pid={os.getpid()}",
                          file=sys.stderr)
                    break
                time.sleep(self.poll_interval)
                continue

            # 5. Live UUID holder = parent Claude session -> inherit.
            if _UUID_RE.match(holder or ""):
                self.session_id = holder
                self.inherited = True
                break

            # 5b. POSTING PRIORITY: preempt a live lower-priority python peer.
            if (
                self.role == "post"
                and holder.startswith("python:")
                and holder_role != "post"
                and self._is_python_holder_alive(holder)
            ):
                try:
                    victim_pid = int(holder.split(":", 1)[1])
                except (ValueError, IndexError):
                    victim_pid = 0
                if victim_pid and self._preempt(victim_pid):
                    try:
                        os.remove(self.lock_file)
                    except OSError:
                        pass
                    if self._try_take():
                        print(
                            f"[browser_lock] post preempted holder={holder} "
                            f"role={holder_role} age={int(age)}s -> pid={os.getpid()}",
                            file=sys.stderr,
                        )
                        break
                time.sleep(self.poll_interval)
                continue

            # 6. Live python peer: wait, then give up (real contention).
            if time.time() >= deadline:
                print(json.dumps({
                    "success": False,
                    "error": f"{self.label} locked by session {holder} ({int(age)}s, peer alive); waited {self.wait_max}s, giving up."
                }))
                sys.exit(1)
            time.sleep(self.poll_interval)
            continue
        self.acquired_at = time.time()
        self._touch()

    def refresh(self):
        try:
            with open(self.lock_file, "w") as f:
                json.dump({"session_id": self.session_id, "timestamp": int(time.time()), "role": self.role}, f)
        except OSError:
            pass
        self._touch()

    def release(self):
        """Inherited locks are the PARENT'S to release; never clobber them."""
        if self.inherited:
            return
        try:
            if os.path.exists(self.lock_file):
                with open(self.lock_file) as f:
                    lock = json.load(f)
                if lock.get("session_id") == self.session_id:
                    os.remove(self.lock_file)
                    if self.acquired_at:
                        held = time.time() - self.acquired_at
                        if held >= 5:
                            print(
                                f"[browser-lock] held {held:.0f}s "
                                f"(role={self.role}, pid={os.getpid()})",
                                file=sys.stderr,
                            )
        except (json.JSONDecodeError, OSError):
            pass
