#!/usr/bin/env python3
"""Prune Claude Desktop's session index of archived S4L worker runs.

WHY (2026-10-30). Claude Desktop keeps one record per session under
  ~/Library/Application Support/Claude*/claude-code-sessions/<acct>/<org>/local_*.json
loads ALL of them at launch, keeps the list in memory, and walks the whole
list (one stat() per record's cwd) on EVERY message of EVERY session. Traced
with fs_usage on a real install: 32,126 stat64 calls per message, 30,262 of
them for archived s4l-worker records (~140 new ones a day, one per poll).
Each message blocked the app's UI thread for 6 to 8 s, twice per worker
cycle, which the user saw as "Claude Desktop freezes". Moving the archived
worker records out and relaunching took it to zero.

WHEN. The app ignores on-disk changes until its next launch, so this MUST run
while Claude is DOWN: inside the menu bar's quit -> relaunch window
(s4l_menubar.py::_prune_session_index, called by every restart flow) and in
scripts/s4l_box_update.sh. Running it while Claude is up is harmless (the
running app never re-reads these files) but has no effect until the next
launch.

WHAT. Records that are archived AND belong to a worker (scheduledTaskId in
WORKER_TASK_IDS, or cwd == ~/.s4l-worker) AND are older than --min-age
(default 1 h, so the s4l MCP's 5-minute transcript relay and the dashboard's
recent-runs view never race it) are MOVED, never deleted outright, to
  <state>/session-index-prune/<YYYY-MM-DD>/<acct>-<org>/
and backup folders older than --keep-days (default 30) are removed.
archived-sessions.idx moves with them (the app rebuilds it on launch).
recordedSkips in scheduled-tasks.json (the per-minute per_task_limit skip
ledger: 7,345 entries / 600 KB rewritten every minute on the traced install)
is trimmed to its last 20 entries. Live and non-worker records, and the CLI
transcripts under ~/.claude/projects, are never touched.

Best-effort: never raises. prune() returns a summary dict; the CLI prints it
as JSON. --dry-run reports what would move without moving anything.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
import tempfile
import time

# Keep in sync with scheduled_task_selfheal.py / s4l_menubar.py / index.ts.
WORKER_TASK_IDS = ("s4l-worker", "saps-worker", "saps-phase1-query", "saps-phase2b-draft")
WORKER_CWD = os.path.join(os.path.expanduser("~"), ".s4l-worker")

# "Claude*": the host app can run with a custom --user-data-dir (per-account
# dirs like "Claude-mediar"). Same shape as SCHED_REGISTRY_GLOB elsewhere,
# but matching the <acct>/<org> store directory itself.
STORE_DIR_GLOB = os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", "Claude*",
    "claude-code-sessions", "*", "*",
)

DEFAULT_MIN_AGE_SEC = 3600
DEFAULT_KEEP_DAYS = 30
RECORDED_SKIPS_KEEP = 20


def _state_dir() -> str:
    # Mirrors mcp/menubar/s4l_state.py::state_dir() without importing it
    # (this script also runs standalone from the installed package).
    return os.environ.get("S4L_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".social-autoposter-mcp")


def _is_worker_record(rec: dict) -> bool:
    if rec.get("scheduledTaskId") in WORKER_TASK_IDS:
        return True
    return rec.get("cwd") == WORKER_CWD


def _trim_recorded_skips(store: str, dry_run: bool) -> int:
    """Trim every recordedSkips list in this store's scheduled-tasks.json to
    its last RECORDED_SKIPS_KEEP entries. Returns entries dropped."""
    path = os.path.join(store, "scheduled-tasks.json")
    if not os.path.isfile(path):
        return 0
    try:
        with open(path) as fh:
            d = json.load(fh)
    except Exception:
        return 0
    skips = d.get("recordedSkips")
    if not isinstance(skips, dict):
        return 0
    dropped = 0
    for k, v in list(skips.items()):
        if isinstance(v, list) and len(v) > RECORDED_SKIPS_KEEP:
            dropped += len(v) - RECORDED_SKIPS_KEEP
            skips[k] = v[-RECORDED_SKIPS_KEEP:]
    if dropped and not dry_run:
        try:
            fd, tmp = tempfile.mkstemp(dir=store, prefix=".scheduled-tasks.", suffix=".prune")
            with os.fdopen(fd, "w") as fh:
                json.dump(d, fh)
            os.replace(tmp, path)
        except Exception:
            return 0
    return dropped


def _expire_backups(backup_root: str, keep_days: int, dry_run: bool) -> int:
    removed = 0
    cutoff = time.time() - keep_days * 86400
    try:
        for name in os.listdir(backup_root):
            p = os.path.join(backup_root, name)
            if not os.path.isdir(p):
                continue
            try:
                if os.path.getmtime(p) >= cutoff:
                    continue
            except OSError:
                continue
            if not dry_run:
                shutil.rmtree(p, ignore_errors=True)
            removed += 1
    except FileNotFoundError:
        pass
    return removed


def prune(dry_run: bool = False,
          min_age_sec: int = DEFAULT_MIN_AGE_SEC,
          keep_days: int = DEFAULT_KEEP_DAYS,
          store_glob: str | None = None,
          backup_root: str | None = None) -> dict:
    """Move archived, aged worker session records out of every Claude Desktop
    session store. Run only while Claude is down (see module docstring).
    Never raises."""
    summary = {
        "ok": True, "dry_run": dry_run, "moved": 0, "kept_worker_live": 0,
        "skipped_young": 0, "other_records": 0, "unreadable": 0,
        "skips_dropped": 0, "idx_moved": 0, "backups_expired": 0,
        "stores": [], "backup_dir": None, "error": None,
    }
    try:
        backup_root = backup_root or os.path.join(_state_dir(), "session-index-prune")
        day_dir = os.path.join(backup_root, time.strftime("%Y-%m-%d"))
        summary["backup_dir"] = day_dir
        now = time.time()
        for store in sorted(glob.glob(store_glob or STORE_DIR_GLOB)):
            if not os.path.isdir(store):
                continue
            acct = os.path.basename(os.path.dirname(store))
            org = os.path.basename(store)
            dest = os.path.join(day_dir, f"{acct}-{org}")
            moved_here = 0
            for name in os.listdir(store):
                if not (name.startswith("local_") and name.endswith(".json")):
                    continue
                path = os.path.join(store, name)
                try:
                    with open(path) as fh:
                        rec = json.load(fh)
                    mtime = os.path.getmtime(path)
                except Exception:
                    summary["unreadable"] += 1
                    continue
                if not isinstance(rec, dict) or not _is_worker_record(rec):
                    summary["other_records"] += 1
                    continue
                if rec.get("isArchived") is not True:
                    summary["kept_worker_live"] += 1
                    continue
                if now - mtime < min_age_sec:
                    summary["skipped_young"] += 1
                    continue
                if not dry_run:
                    os.makedirs(dest, exist_ok=True)
                    shutil.move(path, os.path.join(dest, name))
                moved_here += 1
            if moved_here:
                idx = os.path.join(store, "archived-sessions.idx")
                if os.path.isfile(idx):
                    if not dry_run:
                        shutil.move(idx, os.path.join(dest, "archived-sessions.idx"))
                    summary["idx_moved"] += 1
            summary["moved"] += moved_here
            summary["skips_dropped"] += _trim_recorded_skips(store, dry_run)
            summary["stores"].append({"store": store, "moved": moved_here})
        summary["backups_expired"] = _expire_backups(backup_root, keep_days, dry_run)
    except Exception as e:  # never raise into a restart flow
        summary["ok"] = False
        summary["error"] = f"{type(e).__name__}: {e}"
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="report only, move nothing")
    ap.add_argument("--min-age-hours", type=float, default=DEFAULT_MIN_AGE_SEC / 3600,
                    help="leave records newer than this alone (default 1)")
    ap.add_argument("--keep-days", type=int, default=DEFAULT_KEEP_DAYS,
                    help="delete backup folders older than this (default 30)")
    ap.add_argument("--store-glob", default=None,
                    help="override the session-store directory glob (tests)")
    ap.add_argument("--backup-root", default=None,
                    help="override <state>/session-index-prune (tests)")
    a = ap.parse_args()
    r = prune(dry_run=a.dry_run, min_age_sec=int(a.min_age_hours * 3600),
              keep_days=a.keep_days, store_glob=a.store_glob,
              backup_root=a.backup_root)
    print(json.dumps(r, indent=2))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
