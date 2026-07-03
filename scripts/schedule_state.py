#!/usr/bin/env python3
"""Single source of truth for the draft-autopilot schedule state.

Both the Node MCP server (mcp/src/index.ts::scheduleState, via subprocess) and
the Python menu bar (mcp/menubar/s4l_menubar.py, via in-process import) read the
schedule state from HERE so the two surfaces can never drift. Previously the same
~40-line algorithm was hand-maintained in both languages.

The data source is the host's scheduled-task registries on disk:
  ~/Library/Application Support/Claude/claude-code-sessions/*/*/scheduled-tasks.json
A complete worker set must be present: the universal saps-worker task, or the
legacy pair (saps-phase1-query + saps-phase2b-draft) on pre-universal installs.
The LIVE account is the registry whose tasks have the freshest lastRunAt (only
the active account's scheduler advances it, so this is robust across the
session-id churn that Claude restarts cause).

States:
  'ok'       — a complete worker set present, enabled, and FIRING (lastRunAt
               within FIRING_WINDOW seconds).
  'disabled' — present but a worker task is disabled.
  'missing'  — not firing anywhere (orphaned / not registered for the live
               account) -> the dashboard offers "Set up draft schedule".

stdlib-only on purpose, so the MCP can run it with system python3 before the
owned runtime is provisioned. Run as a script -> prints {"state": "..."} as JSON.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time

# Keep in sync with QUEUE_WORKERS / LEGACY_QUEUE_WORKER_TASK_IDS in
# mcp/src/index.ts and WORKER_TASK_IDS in mcp/menubar/s4l_menubar.py.
# A registry counts as scheduled when ANY complete set is present: the
# universal type-blind worker (2026-07-02, single task drains every job type)
# or the legacy per-type pair from pre-universal installs.
WORKER_TASK_SETS = (
    ("saps-worker",),
    ("saps-phase1-query", "saps-phase2b-draft"),
)
# Flat legacy alias; s4l_menubar imports this for its relocation sweep.
WORKER_TASK_IDS = ("saps-worker", "saps-phase1-query", "saps-phase2b-draft")


def _registry_worker_recs(by_id):
    """Records for the first COMPLETE worker set in this registry, or None."""
    for task_ids in WORKER_TASK_SETS:
        recs = [by_id.get(tid) for tid in task_ids]
        if all(r is not None for r in recs):
            return recs
    return None

# A worker task whose lastRunAt is within this many seconds counts as "firing".
# 7 min tolerates the host's per-task throttle + Claude restart gaps without a
# false "not scheduled".
FIRING_WINDOW = 420

# Grace for a JUST-scheduled task that hasn't fired yet. When the user runs
# "Set up draft schedule", create_scheduled_task registers both worker tasks
# (cron "* * * * *", enabled) but lastRunAt is null until the host fires them the
# first time — up to a minute or two later, longer if the launchd kicker is still
# installing. Without this grace, compute() saw newest_epoch=None and returned
# "missing", so the menu bar kept flashing ⚠ right after the user successfully
# scheduled. If a freshly-created, enabled task hasn't fired yet but was created
# within this window, treat it as "ok" (waiting for first fire, not orphaned).
# 15 min comfortably covers a slow first fire; a genuinely dead schedule still
# flips to ⚠ once the grace lapses. Orphaned tasks from an old account carry a
# stale createdAt, so they never fall inside this window.
CREATED_GRACE = 900

# "Claude*" (not "Claude"): the host app can run with a custom --user-data-dir
# (per-account dirs like "Claude-mediar" on multi-account machines), and the
# registry lands under THAT dir while plain "Claude/" has no claude-code-sessions
# at all. The freshest-lastRunAt selection in compute() already picks the live
# registry among however many dirs match, so widening the glob is safe; the
# plain-"Claude" glob read a hard "missing" forever on such machines even while
# both worker tasks fired every minute (found 2026-07-02 during onboarding).
SCHED_REGISTRY_GLOB = os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", "Claude*",
    "claude-code-sessions", "*", "*", "scheduled-tasks.json",
)


def _iso_to_epoch(s):
    if not s:
        return None
    try:
        import calendar
        return calendar.timegm(
            time.strptime(str(s).strip().rstrip("Z").split(".")[0], "%Y-%m-%dT%H:%M:%S")
        )
    except Exception:
        return None


def _ms_to_epoch(ms):
    """createdAt is epoch MILLISECONDS in the registry; return epoch seconds."""
    try:
        return float(ms) / 1000.0
    except (TypeError, ValueError):
        return None


def compute(glob_pattern: str = SCHED_REGISTRY_GLOB) -> str:
    """Return 'ok' | 'disabled' | 'missing' for the live account's draft schedule."""
    newest_epoch, newest_enabled = None, False
    # Track the freshest just-created, enabled, never-yet-fired task so a schedule
    # the user only moments ago set up doesn't read as "missing" before its first
    # fire lands (see CREATED_GRACE).
    newest_fresh_created = None
    any_present, any_enabled = False, False
    for f in glob.glob(glob_pattern):
        try:
            with open(f) as fh:
                d = json.load(fh)
        except Exception:
            continue
        by_id = {t.get("id"): t for t in (d.get("scheduledTasks") or [])}
        recs = _registry_worker_recs(by_id)
        if recs is None:
            continue
        any_present = True
        enabled = all(r.get("enabled") for r in recs)
        any_enabled = any_enabled or enabled
        epochs = [_iso_to_epoch(r.get("lastRunAt")) for r in recs]
        e = max([x for x in epochs if x is not None], default=None)
        if e is not None and (newest_epoch is None or e > newest_epoch):
            newest_epoch, newest_enabled = e, enabled
        # Freshly scheduled + enabled + not yet fired anywhere in this registry.
        if enabled and e is None:
            created = [_ms_to_epoch(r.get("createdAt")) for r in recs]
            c = min([x for x in created if x is not None], default=None)
            if c is not None and (newest_fresh_created is None or c > newest_fresh_created):
                newest_fresh_created = c
    # Firing recently => the live account's schedule is active and healthy.
    if newest_epoch is not None and (time.time() - newest_epoch) <= FIRING_WINDOW:
        return "ok" if newest_enabled else "disabled"
    # Just scheduled, enabled, waiting for its first fire => "ok" (not orphaned).
    # Suppresses the false ⚠ right after the user sets up the draft schedule.
    if newest_fresh_created is not None and (time.time() - newest_fresh_created) <= CREATED_GRACE:
        return "ok"
    # Not firing anywhere. Registered-but-disabled => disabled; else missing.
    if any_present and not any_enabled:
        return "disabled"
    return "missing"


def _detail(glob_pattern: str = SCHED_REGISTRY_GLOB) -> dict:
    """Cheap diagnostics for the JSON output: which registries the glob saw and
    which contain both worker tasks, with each one's freshest lastRunAt age.
    This is what makes a 'missing' verdict debuggable from a log line instead of
    requiring filesystem forensics (the 2026-07-02 rotated-dir bug hid here)."""
    regs = []
    for f in glob.glob(glob_pattern):
        entry = {"path": f, "has_workers": False, "last_run_age_s": None}
        try:
            with open(f) as fh:
                d = json.load(fh)
            by_id = {t.get("id"): t for t in (d.get("scheduledTasks") or [])}
            recs = _registry_worker_recs(by_id)
            if recs is not None:
                entry["has_workers"] = True
                epochs = [_iso_to_epoch(r.get("lastRunAt")) for r in recs]
                e = max([x for x in epochs if x is not None], default=None)
                if e is not None:
                    entry["last_run_age_s"] = int(time.time() - e)
        except Exception as exc:
            entry["error"] = str(exc)
        regs.append(entry)
    return {"glob": glob_pattern, "registries": regs}


def main() -> int:
    out = {}
    try:
        out["state"] = compute()
    except Exception as exc:
        out["state"] = "missing"
        out["error"] = str(exc)
    # Extra keys are ignored by the Node caller (it reads .state only) but give
    # menubar Sentry captures and hand-runs the WHY behind a 'missing'.
    try:
        out["detail"] = _detail()
    except Exception:
        pass
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
