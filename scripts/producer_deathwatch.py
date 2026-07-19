#!/usr/bin/env python3
"""producer_deathwatch.py — dead-man's-switch for every Claude-calling
producer process in the pipeline.

Watches a single PID: either scripts/claude_job.py's blocking provider wait
(cmd_provider, the queue path used by twitter-prep/feedback-digest/etc.), or
scripts/run_claude.sh's direct `claude -p` exec (every other platform:
reddit, linkedin, github, moltbook, instagram, dm-outreach-*, ...). If that
PID disappears while its arm marker still exists, something killed it
(SIGKILL / OOM / hard crash) rather than a normal return — a clean return
always disarms first. This is the exact gap that made orphaned salvage
results ("worker drafted, no card") unexplainable: the dying process can
never log its own death, and salvage only sees the aftermath up to
--max-age-hours later.

On an unexpected death this:
  1. Snapshots memory pressure + related processes.
  2. Appends a structured JSON line to producer-deathwatch.jsonl (local,
     box-only, for offline/box-local debugging).
  3. POSTs the same event to /api/v1/producer-death-events so it's queryable
     across every install from Postgres, not just grep-able on one box (see
     migrations/2026-07-09-producer-death-events.sql).
  4. Emits a one-line summary via claude_job._plog() into provider.log,
     which scripts/relay_provider_log.py already ships to Cloud Logging.

Three subcommands, so both Python (claude_job.py) and bash (run_claude.sh)
callers share one implementation:
  arm     — write the marker + spawn `watch` detached (start_new_session=True,
            so it survives being in the same process group as the watched
            pid if that group gets signaled). Called right before a caller
            starts blocking on the watched pid.
  disarm  — remove the marker. Called on every normal return path AND from
            a signal-trap cleanup (e.g. run_claude.sh's _sa_cleanup, which
            itself SIGKILLs the watched process group as part of ordinary
            TERM/INT/HUP handling — that must disarm too, or a normal
            watchdog-triggered shutdown would misreport as an unexpected
            death).
  watch   — the actual poll loop (internal; `arm` spawns this, nothing else
            should call it directly).

Best-effort throughout: this is diagnostics only, never allowed to affect
the real job either way.
"""
from __future__ import annotations

import argparse
import json
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


def arm(watch_pid: int, job_id: str, qtype: str, batch: str, call_path: str) -> None:
    """Write the marker and spawn a detached `watch` subprocess. Best-effort:
    any failure here must never block the real caller."""
    try:
        marker = arm_path(job_id)
        os.makedirs(queue_root(), exist_ok=True)
        with open(marker, "w") as f:
            f.write(str(watch_pid))
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "watch",
             "--watch-pid", str(watch_pid), "--job-id", job_id,
             "--qtype", qtype, "--batch", batch, "--call-path", call_path],
            start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def disarm(job_id: str) -> None:
    try:
        os.remove(arm_path(job_id))
    except Exception:
        pass


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
    # Cap length: this rides into a Postgres text column and a JSON line;
    # keep it bounded so a busy box's ps dump can't balloon either.
    return vm[:4000], related[:4000]


def _report_to_db(event: dict) -> None:
    """Best-effort POST to /api/v1/producer-death-events. Catches
    BaseException, not just Exception: http_api._request raises SystemExit
    on a terminal 4xx/5xx, which must never be allowed to break the
    diagnostic path (mirrors autopilot_stall_watch.py's same guard)."""
    try:
        import http_api  # noqa: E402 (sibling module, HERE already on sys.path)
        http_api.api_post("/api/v1/producer-death-events", {
            "watch_pid": event["watch_pid"],
            "job_id": event["job_id"],
            "batch_id": event["batch"] if event["batch"] != "-" else None,
            "qtype": event["qtype"],
            "call_path": event["call_path"],
            "vm_stat_summary": event["vm_stat"],
            "related_processes": event["related_processes"],
        })
    except BaseException:
        pass


def watch(watch_pid: int, job_id: str, qtype: str, batch: str, call_path: str) -> int:
    marker = arm_path(job_id)
    while True:
        if not os.path.exists(marker):
            return 0  # disarmed: the caller returned cleanly, nothing to report
        try:
            os.kill(watch_pid, 0)
        except ProcessLookupError:
            break  # pid gone but still armed -> unexpected death
        except PermissionError:
            pass  # exists, just not signalable from here; keep watching
        except Exception:
            pass
        time.sleep(POLL_S)

    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    vm, related = _snapshot()
    event = {
        "ts": ts,
        "event": "unexpected_death",
        "watch_pid": watch_pid,
        "job_id": job_id,
        "qtype": qtype,
        "batch": batch,
        "call_path": call_path,
        "vm_stat": vm,
        "related_processes": related,
    }
    try:
        os.makedirs(queue_root(), exist_ok=True)
        with open(os.path.join(queue_root(), "producer-deathwatch.jsonl"), "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        pass
    _report_to_db(event)
    try:
        _plog(f"[deathwatch] UNEXPECTED DEATH watch_pid={watch_pid} job={job_id} "
              f"type={qtype} batch={batch} path={call_path} -> see producer-deathwatch.jsonl")
    except Exception:
        pass
    try:
        os.remove(marker)
    except Exception:
        pass
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("arm")
    pa.add_argument("--watch-pid", type=int, required=True)
    pa.add_argument("--job-id", required=True)
    pa.add_argument("--qtype", default="?")
    pa.add_argument("--batch", default="-")
    pa.add_argument("--call-path", default="queue", choices=["queue", "direct"])

    pd = sub.add_parser("disarm")
    pd.add_argument("--job-id", required=True)

    pw = sub.add_parser("watch")
    pw.add_argument("--watch-pid", type=int, required=True)
    pw.add_argument("--job-id", required=True)
    pw.add_argument("--qtype", default="?")
    pw.add_argument("--batch", default="-")
    pw.add_argument("--call-path", default="queue", choices=["queue", "direct"])

    ns = ap.parse_args()
    if ns.cmd == "arm":
        arm(ns.watch_pid, ns.job_id, ns.qtype, ns.batch, ns.call_path)
        return 0
    if ns.cmd == "disarm":
        disarm(ns.job_id)
        return 0
    return watch(ns.watch_pid, ns.job_id, ns.qtype, ns.batch, ns.call_path)


if __name__ == "__main__":
    sys.exit(main())
