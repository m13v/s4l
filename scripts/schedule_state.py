#!/usr/bin/env python3
"""Single source of truth for the draft-autopilot schedule state.

Both the Node MCP server (mcp/src/index.ts::scheduleState, via subprocess) and
the Python menu bar (mcp/menubar/s4l_menubar.py, via in-process import) read the
schedule state from HERE so the two surfaces can never drift. Previously the same
~40-line algorithm was hand-maintained in both languages.

The data source is the host's scheduled-task registries on disk:
  ~/Library/Application Support/Claude/claude-code-sessions/<account-uuid>/*/scheduled-tasks.json
A complete worker set must be present: the universal s4l-worker task (or its short-lived
staging predecessor saps-worker), or the
legacy pair (saps-phase1-query + saps-phase2b-draft) on pre-universal installs.
The LIVE account for each Claude* root is read from THAT root's config.json ->
lastKnownAccountUuid (see _active_registry_glob_patterns) — NOT inferred by
picking whichever registry has the freshest lastRunAt. An account switch
leaves the previous account's scheduled-tasks.json sitting untouched on disk
under its OWN account-uuid directory; picking "freshest lastRunAt across every
account this machine has ever logged into" can pick that orphaned registry
right back up and read a genuinely missing schedule (zero tasks for the
account actually logged in) as merely 'stalled' (found 2026-07-08: a box's
active account had NO scheduled-tasks.json anywhere, while a different,
no-longer-active account's directory still had s4l-worker enabled with a
lastRunAt from hours earlier — every "just restart Claude" attempt was
guaranteed to do nothing, because there was never anything registered for the
account that was actually running).

States:
  'ok'       — a complete worker set present, enabled, and FIRING (lastRunAt
               within FIRING_WINDOW seconds), for the ACTIVE account.
  'disabled' — present but a worker task is disabled, for the ACTIVE account.
  'stalled'  — present AND enabled for the ACTIVE account, but lastRunAt is
               stale: the host scheduler stopped launching it for THIS
               account. Known cause: the Claude Desktop warm-session wedge
               (finished worker sessions never exit, the overlap guard skips
               every fire; full app restart fixes it — Karol, 2026-07-06).
               Account-switch orphaning is no longer a 'stalled' cause — it
               now correctly resolves to 'missing' (below), since the account
               scoping means an orphaned OTHER account's registry is never
               consulted for the active account's health.
               DIAGNOSTIC-ONLY for the menu bar ⚠ since 2026-07-09: under the
               wedge the tick can skip 70-90% of fires while the queue still
               drains every job (jobs arrive ~every 8 min vs 60 fires/hr), so
               a point-in-time lastRunAt check flip-flops against a healthy
               pipeline. The menu bar alarms on queue latency instead and
               renders 'stalled' as a plain detail line via tick_stats().
  'missing'  — the ACTIVE account has no registry with a complete worker set
               (deleted / never scheduled / orphaned by an account switch)
               -> the dashboard offers "Set up draft schedule". Consumers
               that predate 'stalled' treated both cases as 'missing';
               anything gating on == 'ok' is unaffected.

stdlib-only on purpose, so the MCP can run it with system python3 before the
owned runtime is provisioned. Run as a script -> prints {"state": "..."} as JSON.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time

# SAPS_->S4L_ env mirror (brand rename 2026-07-03): old launchd plists and
# scheduled-task prompts still export SAPS_*; this process reads S4L_*.
import s4l_env  # noqa: E402  (lives next to this file in scripts/)

s4l_env.mirror()

# Keep in sync with QUEUE_WORKERS / LEGACY_QUEUE_WORKER_TASK_IDS in
# mcp/src/index.ts and WORKER_TASK_IDS in mcp/menubar/s4l_menubar.py.
# A registry counts as scheduled when ANY complete set is present: the
# universal type-blind worker (2026-07-02, single task drains every job type)
# or the legacy per-type pair from pre-universal installs.
WORKER_TASK_SETS = (
    ("s4l-worker",),
    ("saps-worker",),  # transitional: staging rc.2/rc.3 only, pre brand rename
    ("saps-phase1-query", "saps-phase2b-draft"),
)
# Flat legacy alias; s4l_menubar imports this for its relocation sweep.
WORKER_TASK_IDS = ("s4l-worker", "saps-worker", "saps-phase1-query", "saps-phase2b-draft")


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

# --- Stall thresholds (single source of truth) --------------------------------
# The longest a fully healthy drain can take end to end: a draft job can wait
# minutes for a worker slot (per-minute scheduler cadence, workers busy on other
# jobs) and the drafting turn itself runs multiple minutes. Observed healthy
# episodes reach 8-10 min (2026-07-13); alarm at 20 min for margin. Every stall
# detector derives from this ONE number so they can't drift apart again (99e695bf
# retuned the two per-state timers to 1200s but left the end-to-end DRAFT_STUCK
# check at 300s, which then false-alarmed on healthy 8-minute drains). The Node
# server can't import this module — mcp/src/index.ts::AUTOPILOT_STALL_MS must be
# kept in sync by hand.
HEALTHY_DRAIN_MAX_SECONDS = 1200

# A pending draft job unclaimed this long: no routine is draining the queue
# (an idle queue has no pending job at all, so a quiet pipeline never trips it).
STALL_SECONDS = HEALTHY_DRAIN_MAX_SECONDS
# A job claimed but still sitting in running/ this long: the worker picked it up
# and then wedged mid-run (the claude -p drafting child died / never spawned).
RUNNING_STALL_SECONDS = HEALTHY_DRAIN_MAX_SECONDS
# The producer's "drafting Nm" label alive this long: the end-to-end clock. This
# is the only timer that spans claim→die→requeue churn loops, which reset the two
# per-state timers above on every transition while nothing ever completes.
DRAFT_STUCK_SECONDS = HEALTHY_DRAIN_MAX_SECONDS

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
# at all. UNSCOPED across every account under each root — deliberately, this is
# also imported by callers that need to see EVERY account's registries (e.g. the
# menu bar's relocation/legacy-consolidation sweep, which must find and clean up
# old accounts' leftover tasks, not just the live one). compute()/_detail() do
# NOT use this directly by default any more — see _active_registry_glob_patterns.
SCHED_REGISTRY_GLOB = os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", "Claude*",
    "claude-code-sessions", "*", "*", "scheduled-tasks.json",
)


def _config_json_paths():
    """One config.json per Claude* root (multi-account machines can run
    several distinct --user-data-dir roots, e.g. Claude-mediar, each a
    separate host app instance with its own logged-in account)."""
    return glob.glob(os.path.join(
        os.path.expanduser("~"), "Library", "Application Support", "Claude*",
        "config.json",
    ))


def _active_account_uuid(config_path):
    """The account currently logged into the Claude* root config_path lives
    under, or None if config.json is missing/unreadable/predates this field."""
    try:
        with open(config_path) as fh:
            d = json.load(fh)
        u = d.get("lastKnownAccountUuid")
        return u if isinstance(u, str) and u else None
    except Exception:
        return None


def _active_registry_glob_patterns():
    """One glob pattern per Claude* root, scoped to ONLY the account that root
    is currently logged into (verified 2026-07-08: claude-code-sessions/<uuid>/
    is keyed by account — lastKnownAccountUuid in that root's config.json
    matches the top-level directory local-agent-mode-sessions/<uuid>/ actually
    running under, and a different, no-longer-active account's own <uuid>/
    directory sits untouched alongside it). Falls back to the OLD
    every-account-under-this-root pattern for a root whose config.json doesn't
    resolve an account, so an unusual/older install never regresses to a blind
    'missing' just because this field happens to be absent."""
    patterns = []
    for cfg in _config_json_paths():
        root = os.path.dirname(cfg)
        uuid = _active_account_uuid(cfg)
        if uuid:
            patterns.append(os.path.join(
                root, "claude-code-sessions", uuid, "*", "scheduled-tasks.json"
            ))
        else:
            patterns.append(os.path.join(
                root, "claude-code-sessions", "*", "*", "scheduled-tasks.json"
            ))
    # No Claude* root found at all (host app never launched here) -> fall back
    # to the unscoped glob so callers still see SOMETHING rather than nothing.
    return patterns or [SCHED_REGISTRY_GLOB]


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


def compute(glob_pattern: str | None = None) -> str:
    """Return 'ok' | 'disabled' | 'stalled' | 'missing' for the draft schedule.

    With no argument (every real caller), scans ONLY the active account's
    registries via _active_registry_glob_patterns() — see its docstring and
    the module docstring for why. Pass an explicit glob_pattern to scan a
    literal pattern instead (e.g. a test fixture dir); this bypasses account
    scoping entirely, matching the old unconditional behavior."""
    patterns = [glob_pattern] if glob_pattern is not None else _active_registry_glob_patterns()
    newest_epoch, newest_enabled = None, False
    # Track the freshest just-created, enabled, never-yet-fired task so a schedule
    # the user only moments ago set up doesn't read as "missing" before its first
    # fire lands (see CREATED_GRACE).
    newest_fresh_created = None
    any_present, any_enabled = False, False
    for pattern in patterns:
        for f in glob.glob(pattern):
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
    # Not firing anywhere for the active account. Registered-but-disabled =>
    # disabled; registered and enabled but the host stopped launching it =>
    # stalled (Desktop scheduler wedge, SAME account); absent for the active
    # account entirely (never scheduled, or orphaned by an account switch) =>
    # missing.
    if any_present and not any_enabled:
        return "disabled"
    if any_present and any_enabled:
        return "stalled"
    return "missing"


def _detail(glob_pattern: str | None = None) -> dict:
    """Cheap diagnostics for the JSON output: which registries the glob(s) saw
    and which contain both worker tasks, with each one's freshest lastRunAt
    age. This is what makes a 'missing' verdict debuggable from a log line
    instead of requiring filesystem forensics (the 2026-07-02 rotated-dir bug,
    and the 2026-07-08 wrong-account bug, both hid here)."""
    patterns = [glob_pattern] if glob_pattern is not None else _active_registry_glob_patterns()
    regs = []
    for pattern in patterns:
        for f in glob.glob(pattern):
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
    return {"glob": patterns, "registries": regs}


def tick_stats(window_s: int = 3600, glob_pattern: str | None = None) -> dict:
    """Diagnostic tick-level stats for the active account's worker set: how many
    per-minute fires the host SKIPPED inside the last window_s (the registry's
    recordedSkips ledger — under the Desktop warm-session wedge these carry
    reason per_task_limit) plus the age of the newest successful run. Powers the
    menu bar's non-alarm "scheduler degraded" detail line when compute() returns
    'stalled'. NEVER a health verdict on its own: a box skipping 80% of ticks
    can still drain every job (jobs arrive ~every 8 min vs 60 fires/hr), which
    is exactly why tick staleness stopped driving the attention ⚠ (2026-07-09).
    Same account scoping as compute(); pass glob_pattern only for tests."""
    patterns = [glob_pattern] if glob_pattern is not None else _active_registry_glob_patterns()
    now = time.time()
    skips = 0
    last_skip_epoch = None
    newest_epoch = None
    for pattern in patterns:
        for f in glob.glob(pattern):
            try:
                with open(f) as fh:
                    d = json.load(fh)
            except Exception:
                continue
            by_id = {t.get("id"): t for t in (d.get("scheduledTasks") or [])}
            recs = _registry_worker_recs(by_id)
            if recs is None:
                continue
            epochs = [_iso_to_epoch(r.get("lastRunAt")) for r in recs]
            e = max([x for x in epochs if x is not None], default=None)
            if e is not None and (newest_epoch is None or e > newest_epoch):
                newest_epoch = e
            recorded = d.get("recordedSkips") or {}
            for tid in WORKER_TASK_IDS:
                for s in recorded.get(tid) or []:
                    at = _ms_to_epoch((s or {}).get("at"))
                    if at is None or (now - at) > window_s:
                        continue
                    skips += 1
                    if last_skip_epoch is None or at > last_skip_epoch:
                        last_skip_epoch = at
    return {
        "skips_in_window": skips,
        "window_s": window_s,
        "last_run_age_s": int(now - newest_epoch) if newest_epoch is not None else None,
        "last_skip_age_s": int(now - last_skip_epoch) if last_skip_epoch is not None else None,
    }


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
