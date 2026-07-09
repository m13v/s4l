#!/usr/bin/env python3
"""Scheduled-task registry self-heal, run ONLY while Claude Desktop is DOWN
(the running app caches the registry in memory and clobbers a live edit on
its next fire).

WHY THIS IS A STANDALONE SCRIPT, NOT JUST A MENU BAR METHOD (2026-07-08):
this logic used to live only as mcp/menubar/s4l_menubar.py::_rewrite_scheduled_task_cwd,
called in-process from _mcpb_update_work and _relocate_restart_work. Both of
those run INSIDE the already-executing (old) menu bar process, BEFORE that
process quits and relaunches with the just-downloaded new code — Python does
not hot-reload an already-imported module just because newer .py files landed
on disk mid-run. So a fix shipped to that method would only ever take effect
on the update AFTER the one that shipped it: found on a real test box where a
newly-added fix (creating a missing registry) silently did nothing during the
very update that shipped it, because the self-heal call that fired still ran
the OLD in-process code.

The fix: unpack THIS script fresh from the just-downloaded bundle's embedded
pipeline.tgz and run it as a NEW subprocess (see the callers in
mcp/menubar/s4l_menubar.py) — a subprocess always imports whatever is on disk
at invocation time, so it can never be stale the way an in-process call can.
mcp/menubar/s4l_menubar.py::_rewrite_scheduled_task_cwd now delegates to
heal() here for in-process callers where staleness isn't the concern (kept for
back-compat rather than hunting down every caller).

stdlib-only on purpose, matching scripts/schedule_state.py's pattern.
Run as a script -> heals, then prints {"ok": true, ...} as JSON.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import sys
import tempfile
import time

import schedule_state  # noqa: E402  (lives next to this file in scripts/)

WORKER_TASK_ID = "s4l-worker"
WORKER_TASK_IDS = ("s4l-worker", "saps-worker", "saps-phase1-query", "saps-phase2b-draft")
LEGACY_WORKER_TASK_IDS = ("saps-worker", "saps-phase1-query", "saps-phase2b-draft")
DEPRECATED_TASK_IDS = ("social-autoposter-autopilot",)
WORKER_CWD = os.path.join(os.path.expanduser("~"), ".s4l-worker")

# Kept in sync with SCHED_REGISTRY_GLOB in mcp/menubar/s4l_menubar.py,
# scripts/schedule_state.py, scripts/scheduled_tasks_snapshot.py, and
# queueWorkerCwd()/QUEUE_WORKERS in mcp/src/index.ts (same constant,
# necessarily duplicated across languages/processes -- pre-existing pattern).
SCHED_REGISTRY_GLOB = os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", "Claude*",
    "claude-code-sessions", "*", "*", "scheduled-tasks.json",
)


def _ensure_worker_skill_md() -> bool:
    """Make sure ~/.claude/scheduled-tasks/s4l-worker/SKILL.md exists before we
    register a task that points at it. The MCP writes it on every boot
    (create-if-missing), so normally this is a no-op; as a belt-and-suspenders
    fallback we clone a legacy worker's file and fix the frontmatter name."""
    base = os.path.join(os.path.expanduser("~"), ".claude", "scheduled-tasks")
    dst = os.path.join(base, WORKER_TASK_ID, "SKILL.md")
    if os.path.exists(dst):
        return True
    for tid in LEGACY_WORKER_TASK_IDS:
        src = os.path.join(base, tid, "SKILL.md")
        try:
            with open(src) as fh:
                body = fh.read()
        except Exception:
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w") as fh:
                fh.write(body.replace(f"name: {tid}", f"name: {WORKER_TASK_ID}", 1))
            return True
        except Exception:
            continue
    return False


def heal() -> dict:
    """Five fixes in one pass, across every scheduled-tasks.json:
      1. Point worker tasks' cwd at ~/.s4l-worker.
      2. REMOVE the deprecated single autopilot task.
      3. CONSOLIDATE every legacy worker entry into ONE s4l-worker entry (the
         universal type-blind worker): drop the legacy entries and, if no
         s4l-worker is registered there yet, add one inheriting the legacy
         cron/enabled state. Migration path for pre-universal-queue installs.
      4. ENSURE an enabled s4l-worker entry exists in EVERY account registry
         that already has a scheduled-tasks.json (the account-switch orphan
         heal, 2026-07-06): switching Claude accounts leaves the task under
         the old account's registry and the new account never fires it.
         Writing the record into every EXISTING registry while Claude is down
         means whichever account the user logs into has the task; copies
         under logged-out accounts are inert. Guarded by user intent: if ANY
         registry holds an explicitly DISABLED worker copy, the user turned
         it off -- add nothing anywhere. (The Quit flow deletes the SKILL.md
         dirs, so worker_skill_ok also gates this from resurrecting a quit
         install.) This restores the June 27 direct-write re-arm (45f1c45d)
         with the targeting problem dissolved by writing everywhere instead
         of guessing the live account.
      5. CREATE a fresh registry for the active account when it has NONE at
         all (2026-07-08): fix 4 only edits files the glob finds -- it can
         never create one where none exists. An account that's never had a
         scheduled-tasks.json (just switched into, never scheduled on this
         box) got nothing written despite fix 4 running: found on a real test
         box where the active account's entire claude-code-sessions/<uuid>/
         tree held zero registry files. Resolves the active account via
         schedule_state.py's config.json lookup (lastKnownAccountUuid,
         verified correct against real installs 2026-07-08) and writes a
         fresh worker entry into its most-recently-touched EXISTING session
         directory (never fabricates a new directory). Same user-intent and
         worker_skill_ok guards as fix 4.
    Best-effort: never raises. Returns a small summary dict for logging."""
    summary = {"ok": True, "edited": [], "created": [], "error": None}
    try:
        os.makedirs(WORKER_CWD, exist_ok=True)
    except Exception:
        pass
    worker_skill_ok = _ensure_worker_skill_md()

    # Pre-scan for user intent + a template: an explicitly disabled worker
    # copy anywhere means the user opted out -- never re-add. Otherwise clone
    # cron from an existing record so the shape matches what the host wrote.
    any_disabled = False
    tmpl_cron = "* * * * *"
    try:
        for f in glob.glob(SCHED_REGISTRY_GLOB):
            try:
                with open(f) as fh:
                    d = json.load(fh)
            except Exception:
                continue
            for t in (d.get("scheduledTasks") or []):
                if t.get("id") in WORKER_TASK_IDS:
                    if not t.get("enabled", True):
                        any_disabled = True
                    if t.get("cronExpression"):
                        tmpl_cron = t.get("cronExpression")
    except Exception:
        pass

    # Fixes 1-4: edit every EXISTING registry file the glob finds.
    try:
        for f in glob.glob(SCHED_REGISTRY_GLOB):
            try:
                with open(f) as fh:
                    d = json.load(fh)
            except Exception:
                continue
            tasks = d.get("scheduledTasks") or []
            legacy = [t for t in tasks if t.get("id") in LEGACY_WORKER_TASK_IDS]
            has_worker = any(t.get("id") == WORKER_TASK_ID for t in tasks)
            new_tasks = []
            dirty = False
            for t in tasks:
                tid = t.get("id")
                if tid in DEPRECATED_TASK_IDS:
                    dirty = True          # drop it
                    continue
                if tid in LEGACY_WORKER_TASK_IDS and worker_skill_ok:
                    dirty = True          # consolidated into s4l-worker below
                    continue
                if tid in WORKER_TASK_IDS and t.get("cwd") != WORKER_CWD:
                    t["cwd"] = WORKER_CWD
                    dirty = True
                new_tasks.append(t)
            add_worker = worker_skill_ok and not has_worker and (
                legacy                      # fix 3: legacy consolidation
                or not any_disabled         # fix 4: orphan heal (user intent guard)
            )
            if add_worker:
                tmpl = legacy[0] if legacy else {}
                new_tasks.append({
                    "id": WORKER_TASK_ID,
                    "cronExpression": tmpl.get("cronExpression") or tmpl_cron,
                    "enabled": bool(tmpl.get("enabled", True)),
                    "filePath": os.path.join(
                        os.path.expanduser("~"), ".claude",
                        "scheduled-tasks", WORKER_TASK_ID, "SKILL.md",
                    ),
                    # Fresh createdAt keeps schedule_state's CREATED_GRACE
                    # treating the never-yet-fired task as "ok" until its
                    # first fire lands (no ⚠ flap during the restart).
                    "createdAt": int(time.time() * 1000),
                    "cwd": WORKER_CWD,
                })
                dirty = True
            if not dirty:
                continue
            d["scheduledTasks"] = new_tasks
            try:
                fd, tmp = tempfile.mkstemp(dir=os.path.dirname(f))
                with os.fdopen(fd, "w") as fh:
                    json.dump(d, fh, indent=2)
                os.replace(tmp, f)
                summary["edited"].append(f)
            except Exception:
                pass
    except Exception as e:
        summary["error"] = str(e)

    # Fix 5: create a fresh registry for the active account when it has none.
    try:
        if worker_skill_ok and not any_disabled:
            for cfg in schedule_state._config_json_paths():
                root = os.path.dirname(cfg)
                uuid = schedule_state._active_account_uuid(cfg)
                if not uuid:
                    continue
                account_dir = os.path.join(root, "claude-code-sessions", uuid)
                existing = glob.glob(os.path.join(account_dir, "*", "scheduled-tasks.json"))
                if existing:
                    continue  # fixes 1-4 above already cover this account
                session_dirs = [
                    p for p in glob.glob(os.path.join(account_dir, "*"))
                    if os.path.isdir(p)
                ]
                if not session_dirs:
                    continue  # Desktop has never created a session for this
                              # account on this box -- nowhere safe to write
                # Most-recently-touched session dir is the best available
                # guess for "the one Desktop is actively using".
                target_dir = max(session_dirs, key=lambda p: os.path.getmtime(p))
                target_file = os.path.join(target_dir, "scheduled-tasks.json")
                new_entry = {
                    "id": WORKER_TASK_ID,
                    "cronExpression": tmpl_cron,
                    "enabled": True,
                    "filePath": os.path.join(
                        os.path.expanduser("~"), ".claude",
                        "scheduled-tasks", WORKER_TASK_ID, "SKILL.md",
                    ),
                    "createdAt": int(time.time() * 1000),
                    "cwd": WORKER_CWD,
                }
                try:
                    fd, tmp = tempfile.mkstemp(dir=target_dir)
                    with os.fdopen(fd, "w") as fh:
                        json.dump(
                            {"scheduledTasks": [new_entry], "recordedSkips": []},
                            fh, indent=2,
                        )
                    os.replace(tmp, target_file)
                    summary["created"].append(target_file)
                except Exception:
                    pass
    except Exception as e:
        summary["error"] = summary["error"] or str(e)

    # Remove retired tasks' on-disk SKILL.md dirs too, so they can't be
    # re-registered from a stale prompt file (and the MCP's boot refresh
    # stops resurrecting the legacy prompts).
    try:
        retired = list(DEPRECATED_TASK_IDS)
        if worker_skill_ok:
            retired += list(LEGACY_WORKER_TASK_IDS)
        for tid in retired:
            shutil.rmtree(os.path.join(os.path.expanduser("~"), ".claude",
                                       "scheduled-tasks", tid), ignore_errors=True)
    except Exception:
        pass

    return summary


def can_create_for_active_account() -> bool:
    """Read-only: would fix 5 (see heal()) actually be able to create a fresh
    registration right now? True only if the active account (resolved the same
    way heal() does, via schedule_state's config.json lookup) has at least one
    EXISTING session directory to write into — fix 5 never fabricates one.
    Used by callers (the menu bar) to decide whether to offer an automatic
    "restart to finish setup" action or fall back to the manual re-arm prompt,
    BEFORE committing to a restart that would turn out to fix nothing."""
    try:
        for cfg in schedule_state._config_json_paths():
            root = os.path.dirname(cfg)
            uuid = schedule_state._active_account_uuid(cfg)
            if not uuid:
                continue
            account_dir = os.path.join(root, "claude-code-sessions", uuid)
            session_dirs = [
                p for p in glob.glob(os.path.join(account_dir, "*"))
                if os.path.isdir(p)
            ]
            if session_dirs:
                return True
    except Exception:
        pass
    return False


def main() -> int:
    out = heal()
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
