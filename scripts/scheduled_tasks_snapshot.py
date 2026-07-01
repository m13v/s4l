#!/usr/bin/env python3
"""Slim snapshot of the S4L autopilot scheduled-task registry state.

Answers, per install, the question we were previously blind to: "are the
queue-worker scheduled tasks actually running from the dedicated ~/.s4l-worker
folder (so their once-a-minute sessions don't flood the user's project history),
or are they still mislocated, and is the deprecated autopilot task lingering?"

The heartbeat (scripts/heartbeat.sh + mcp/src/telemetry.ts) attaches the
`--summary` output as the top-level `scheduled_tasks` field of the heartbeat
body, so the state lands on the installations row centrally, keyed by
install_id, with no SSH needed.

Read-only: never edits a registry (that is the menubar's
`_rewrite_scheduled_task_cwd` job). Stdlib only, /usr/bin/python3 compatible.

Kept in sync with mcp/menubar/s4l_menubar.py (WORKER_TASK_IDS,
DEPRECATED_TASK_IDS, WORKER_CWD, SCHED_REGISTRY_GLOB) and scripts/s4l_box_update.sh.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

# --- Kept in sync with mcp/menubar/s4l_menubar.py ---------------------------
WORKER_TASK_IDS = ("saps-phase1-query", "saps-phase2b-draft")
DEPRECATED_TASK_IDS = ("social-autoposter-autopilot",)
WORKER_CWD = os.path.join(os.path.expanduser("~"), ".s4l-worker")
SCHED_REGISTRY_GLOB = os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", "Claude",
    "claude-code-sessions", "*", "*", "scheduled-tasks.json",
)


def _cwd_tail(cwd: str) -> str:
    """Last path component only, so we surface WHERE a mislocated task points
    (e.g. 's4lsetup' vs '.s4l-worker') without shipping the full home path /
    username off-box."""
    if not cwd:
        return ""
    return os.path.basename(os.path.normpath(cwd))


def build_summary() -> dict:
    """Scan every scheduled-tasks.json registry and summarize the S4L worker
    tasks' folder state. Never raises; a broken/absent registry yields an empty
    (but well-formed) summary so the heartbeat body is always valid."""
    tasks: list[dict] = []
    registries = 0
    deprecated_present = False
    seen_ids: set[str] = set()

    try:
        files = glob.glob(SCHED_REGISTRY_GLOB)
    except Exception:
        files = []

    for f in files:
        try:
            with open(f) as fh:
                d = json.load(fh)
        except Exception:
            continue
        registries += 1
        for t in (d.get("scheduledTasks") or []):
            tid = t.get("id")
            if tid in DEPRECATED_TASK_IDS:
                deprecated_present = True
                continue
            if tid not in WORKER_TASK_IDS:
                continue
            cwd = t.get("cwd") or ""
            in_worker = os.path.normpath(cwd) == os.path.normpath(WORKER_CWD) if cwd else False
            seen_ids.add(tid)
            tasks.append({
                "id": tid,
                "enabled": bool(t.get("enabled")),
                "in_worker_dir": in_worker,
                "cwd_tail": _cwd_tail(cwd),
                "last_run_at": t.get("lastRunAt"),
            })

    mislocated = sum(1 for t in tasks if not t["in_worker_dir"])
    return {
        "worker_dir_tail": _cwd_tail(WORKER_CWD),
        "registries": registries,
        "worker_tasks": len(tasks),
        "missing_worker_tasks": sorted(set(WORKER_TASK_IDS) - seen_ids),
        "mislocated": mislocated,
        # all_in_worker_dir is False when there are zero worker tasks too, since
        # "no autopilot registered" is itself a state worth seeing centrally.
        "all_in_worker_dir": bool(tasks) and mislocated == 0,
        "deprecated_present": deprecated_present,
        "tasks": tasks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print a slim JSON summary to stdout and exit. Used by the heartbeat.",
    )
    args = parser.parse_args()

    summary = build_summary()
    if args.summary:
        sys.stdout.write(json.dumps(summary, separators=(",", ":")))
    else:
        sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
