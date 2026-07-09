#!/usr/bin/env python3
"""producer_deathwatch.py — dead-man's-switch for claude_job.py's blocking
provider wait (cmd_provider).

Watches a single PID (the provider process blocked waiting on a queued
Claude job). If that PID disappears while its arm marker still exists,
something killed it (SIGKILL / OOM / hard crash) rather than a normal
Python-level return — a clean return always disarms first via
_disarm_deathwatch(). This is the exact gap that made orphaned salvage
results ("worker drafted, no card") unexplainable: the dying process can
never log its own death, and salvage only sees the aftermath up to
--max-age-hours later. On an unexpected death this snapshots memory
pressure + related processes into producer-deathwatch.log so the next
occurrence gives more than "orphaned, cause unknown".

Spawned detached (start_new_session=True) by claude_job.py's
_arm_deathwatch() right after a job is enqueued, so it survives being in
the same process group as the watched pid if that group gets signaled.
Best-effort throughout: this is diagnostics only, never allowed to affect
the real job either way.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from claude_job import queue_root, _plog  # noqa: E402

POLL_S = 5.0


def arm_path(job_id: str) -> str:
    return os.path.join(queue_root(), f"deathwatch-armed-{job_id}.marker")


def _snapshot() -> tuple[str, str]:
    try:
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception as e:
        vm = f"(vm_stat failed: {e})"
    try:
        procs = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,%mem=,%cpu=,command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
        related = "\n".join(
            ln for ln in procs.splitlines()
            if "claude" in ln or "run-twitter-cycle" in ln or "run_claude" in ln
        ) or "(none matching claude/run-twitter-cycle/run_claude)"
    except Exception as e:
        related = f"(ps failed: {e})"
    return vm, related


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch-pid", type=int, required=True)
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--qtype", default="?")
    ap.add_argument("--batch", default="-")
    ns = ap.parse_args()

    marker = arm_path(ns.job_id)
    while True:
        if not os.path.exists(marker):
            return 0  # disarmed: the provider returned cleanly, nothing to report
        try:
            os.kill(ns.watch_pid, 0)
        except ProcessLookupError:
            break  # pid gone but still armed -> unexpected death
        except PermissionError:
            pass  # exists, just not signalable from here; keep watching
        except Exception:
            pass
        time.sleep(POLL_S)

    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    vm, related = _snapshot()
    block = (
        f"{ts} [deathwatch] UNEXPECTED DEATH watch_pid={ns.watch_pid} "
        f"job={ns.job_id} type={ns.qtype} batch={ns.batch}\n"
        f"--- vm_stat ---\n{vm}\n"
        f"--- related processes (pid ppid %mem %cpu command) ---\n{related}\n"
    )
    try:
        os.makedirs(queue_root(), exist_ok=True)
        with open(os.path.join(queue_root(), "producer-deathwatch.log"), "a") as f:
            f.write(block + "\n")
    except Exception:
        pass
    try:
        _plog(f"[deathwatch] UNEXPECTED DEATH watch_pid={ns.watch_pid} job={ns.job_id} "
              f"type={ns.qtype} batch={ns.batch} -> see producer-deathwatch.log")
    except Exception:
        pass
    try:
        os.remove(marker)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
