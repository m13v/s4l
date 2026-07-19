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
# Current installs run ONE universal worker task (s4l-worker). The phase pair
# is the retired legacy shape kept only so old installs still report. Checking
# ONLY the legacy names made every current install scream
# missing_worker_tasks=[saps-phase1-query, saps-phase2b-draft] forever while
# the real s4l-worker task fired every minute and wasn't even LISTED (Karol,
# 2026-07-03 — this false alarm derailed the whole onboarding investigation).
WORKER_TASK_IDS = ("s4l-worker", "saps-worker", "saps-phase1-query", "saps-phase2b-draft")
CURRENT_WORKER_TASK_IDS = ("s4l-worker", "saps-worker")
LEGACY_WORKER_TASK_IDS = ("saps-phase1-query", "saps-phase2b-draft")
DEPRECATED_TASK_IDS = ("social-autoposter-autopilot",)
WORKER_CWD = os.path.join(os.path.expanduser("~"), ".s4l-worker")
# "Claude*": the host app can run with a custom --user-data-dir (per-account
# dirs like "Claude-mediar"), putting the live registry outside plain "Claude/".
# Keep in sync with scripts/schedule_state.py::SCHED_REGISTRY_GLOB.
SCHED_REGISTRY_GLOB = os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", "Claude*",
    "claude-code-sessions", "*", "*", "scheduled-tasks.json",
)


def _cwd_tail(cwd: str) -> str:
    """Last path component only, so we surface WHERE a mislocated task points
    (e.g. 's4lsetup' vs '.s4l-worker') without shipping the full home path /
    username off-box."""
    if not cwd:
        return ""
    return os.path.basename(os.path.normpath(cwd))


def _registry_ident(path: str) -> dict:
    """Compact identity for one registry file WITHOUT shipping the home path:
    the user-data dir tail (e.g. 'Claude', 'Claude-mediar') plus the first 8
    chars of the account and session uuids from
    .../<user-data-dir>/claude-code-sessions/<account>/<session>/scheduled-tasks.json.
    The account uuid is what distinguishes an account-switch orphan (worker task
    under one account uuid, none under the other) from a single-account
    scheduler wedge — the Karol 2026-07-06 ambiguity this exists to remove."""
    parts = os.path.normpath(path).split(os.sep)
    try:
        session = parts[-2][:8]
        account = parts[-3][:8]
        # parts[-4] == "claude-code-sessions"; parts[-5] is the user-data dir.
        data_dir = parts[-5]
    except IndexError:
        session, account, data_dir = "", "", ""
    return {"data_dir": data_dir, "account": account, "session": session}


def _mtime_age(path: str) -> int | None:
    try:
        import time
        return int(time.time() - os.path.getmtime(path))
    except Exception:
        return None


def build_summary() -> dict:
    """Scan every scheduled-tasks.json registry and summarize the S4L worker
    tasks' folder state. Never raises; a broken/absent registry yields an empty
    (but well-formed) summary so the heartbeat body is always valid."""
    tasks: list[dict] = []
    registries = 0
    deprecated_present = False
    seen_ids: set[str] = set()
    per_registry: list[dict] = []

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
        reg = _registry_ident(f)
        reg["worker_ids"] = []
        reg["enabled"] = False
        reg["last_run_at"] = None
        # Freshness proxy for "is this the account the host is actively using":
        # the session dir's mtime moves with host activity even when no task
        # fires, while the registry file's mtime only moves on task writes.
        reg["registry_mtime_age_s"] = _mtime_age(f)
        reg["session_dir_mtime_age_s"] = _mtime_age(os.path.dirname(f))
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
            reg["worker_ids"].append(tid)
            reg["enabled"] = reg["enabled"] or bool(t.get("enabled"))
            if t.get("lastRunAt") and (reg["last_run_at"] is None or str(t.get("lastRunAt")) > str(reg["last_run_at"])):
                reg["last_run_at"] = t.get("lastRunAt")
            tasks.append({
                "id": tid,
                "enabled": bool(t.get("enabled")),
                "in_worker_dir": in_worker,
                "cwd_tail": _cwd_tail(cwd),
                "last_run_at": t.get("lastRunAt"),
            })
        per_registry.append(reg)

    mislocated = sum(1 for t in tasks if not t["in_worker_dir"])
    # "Missing" means NO viable worker lane at all: neither a current universal
    # task nor the complete legacy pair. Naming every absent id was wrong once
    # the id set spanned generations (a healthy current install always "misses"
    # the legacy pair and vice versa).
    have_current = bool(seen_ids & set(CURRENT_WORKER_TASK_IDS))
    have_legacy = set(LEGACY_WORKER_TASK_IDS) <= seen_ids
    return {
        "worker_dir_tail": _cwd_tail(WORKER_CWD),
        "registries": registries,
        "worker_tasks": len(tasks),
        "missing_worker_tasks": [] if (have_current or have_legacy) else ["s4l-worker"],
        "mislocated": mislocated,
        # all_in_worker_dir is False when there are zero worker tasks too, since
        # "no autopilot registered" is itself a state worth seeing centrally.
        "all_in_worker_dir": bool(tasks) and mislocated == 0,
        "deprecated_present": deprecated_present,
        "tasks": tasks,
        "per_registry": per_registry,
    }


STATE_CACHE_PATH = os.path.join(
    os.path.expanduser("~"), ".social-autoposter-mcp", "scheduled-tasks-last-state.json"
)


def _load_last_state() -> dict:
    try:
        with open(STATE_CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_last_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_CACHE_PATH), exist_ok=True)
        tmp = f"{STATE_CACHE_PATH}.tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_CACHE_PATH)
    except Exception:
        pass


def _registry_key(reg: dict) -> str:
    return f"{reg.get('data_dir')}/{reg.get('account')}/{reg.get('session')}"


def _report_registry_change(message: str, changes: list) -> None:
    """Best-effort Sentry breadcrumb for a registry transition. Silent no-op if
    sentry-sdk/S4L_REPO_DIR are unavailable, so this stays additive and never
    threatens the script's primary --summary (heartbeat) path."""
    try:
        repo = os.environ.get("S4L_REPO_DIR")
        if repo:
            scripts_dir = os.path.join(repo, "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
        import sentry_init

        sentry_init.init()
        sentry_init.capture_message(
            message,
            level="info",
            tags={"component": "scheduled-tasks-snapshot", "issue": "registry_change"},
            extra={"changes": changes},
        )
        sentry_init.flush()
    except Exception:
        pass


def diff_and_emit(per_registry: list) -> list:
    """Compare the current per-registry worker_ids/enabled state against the
    last observed state (cached locally) and emit ONE distinct event per real
    transition: a registry gaining/losing the worker task, `enabled` flipping,
    or a registry appearing/disappearing entirely. Silent when nothing changed.

    This exists because reconstructing "when did the registry actually change"
    previously required diffing dozens of raw --summary snapshots by hand (see
    the Karol 2026-07-07 update-orphan investigation) — every poll logged full
    state unconditionally, with no distinct signal for an actual transition.
    Returns the list of change strings (empty if nothing changed). Best-effort:
    never raises, and never touches the registry files themselves."""
    changes: list = []
    try:
        last = _load_last_state()
        current = {}
        for reg in per_registry:
            key = _registry_key(reg)
            snap = {
                "worker_ids": sorted(reg.get("worker_ids") or []),
                "enabled": bool(reg.get("enabled")),
            }
            current[key] = snap
            prev = last.get(key)
            if prev is None:
                if snap["worker_ids"]:
                    changes.append(
                        f"{key}: registry appeared with worker_ids={snap['worker_ids']} "
                        f"enabled={snap['enabled']}"
                    )
                continue
            if prev.get("worker_ids") != snap["worker_ids"] or prev.get("enabled") != snap["enabled"]:
                changes.append(
                    f"{key}: worker_ids {prev.get('worker_ids')}->{snap['worker_ids']} "
                    f"enabled {prev.get('enabled')}->{snap['enabled']}"
                )
        for key, prev in last.items():
            if key not in current and (prev or {}).get("worker_ids"):
                changes.append(f"{key}: registry disappeared (had worker_ids={prev.get('worker_ids')})")

        if changes:
            message = "scheduled-tasks registry changed: " + "; ".join(changes)
            sys.stderr.write(f"[scheduled-tasks-snapshot] {message}\n")
            _report_registry_change(message, changes)
        _save_last_state(current)
    except Exception:
        pass
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print a slim JSON summary to stdout and exit. Used by the heartbeat.",
    )
    args = parser.parse_args()

    summary = build_summary()
    # Side effect deliberately confined to the standalone-script path (not
    # build_summary(), which s4l_menubar.py also imports in-process on every
    # warning capture — doing the diff there would cache-thrash and could
    # double-report on every menu-bar poll instead of once per real change).
    diff_and_emit(summary.get("per_registry") or [])
    if args.summary:
        sys.stdout.write(json.dumps(summary, separators=(",", ":")))
    else:
        sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
