#!/usr/bin/env python3
"""Box-side autopilot stall watchdog (fleet backstop).

Fires a Sentry event when the draft autopilot's scheduled-task routines stop
draining the local job queue. The most common cause is the user logging Claude
Desktop into a DIFFERENT account, which leaves the two queue-worker routines
(saps-phase1-query / saps-phase2b-draft) registered only under the OLD account's
session, so nothing claims the jobs the pipeline enqueues. The routines' SKILL.md
files live in a GLOBAL dir and survive the switch, so the old "is the SKILL.md on
disk?" check stayed falsely green while drafting silently died for hours.

The menu bar already surfaces this to the user (title -> "S4L ⚠" + a "Re-arm
autopilot" item). This watcher is the part the user can't see: a fleet-side alert
so a sustained stall pages us even when nobody is looking at the menu bar.

Design mirrors the stall signal in mcp/menubar/s4l_menubar.py (_autopilot_stalled)
and mcp/src/index.ts (autopilotStalled) — keep the threshold in sync:
  stalled = the autopilot is configured (both worker SKILL.md files present)
            AND a draft job has sat unclaimed in pending/ past STALL_SECONDS.
False-positive free: an idle queue (no candidates) has no pending job at all, so
a quiet pipeline never trips this.

Idempotency: only ONE Sentry event per stall episode, and only after the stall
has persisted ALERT_AFTER consecutive checks (so a single slow claim during a
restart doesn't page). State lives in <queue>/stall-watch.json; reset when the
stall clears.

Runs as launchd com.m13v.social-autopilot-stall-watch (StartInterval 120) off the
owned venv (needs sentry-sdk + scripts/ on sys.path via SAPS_REPO_DIR). Stdlib
otherwise. Best-effort: never raises into launchd.
"""

from __future__ import annotations

import glob
import json
import os
import sys
import time

# Keep in sync with AUTOPILOT_STALL_SECONDS (menubar) / AUTOPILOT_STALL_MS (index.ts).
STALL_SECONDS = 180
# Require the stall to persist this many consecutive checks before paging, so a
# transient slow claim (e.g. right after a Claude restart) doesn't false-alarm.
# At StartInterval 120 that is ~6 min of continuous stall.
ALERT_AFTER = 3

WORKER_TASK_IDS = ("saps-phase1-query", "saps-phase2b-draft")


def _state_dir() -> str:
    return os.environ.get("SAPS_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".social-autoposter-mcp"
    )


def _queue_root() -> str:
    return os.path.join(_state_dir(), "claude-queue")


def _watch_state_path() -> str:
    return os.path.join(_queue_root(), "stall-watch.json")


def _claude_config_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude"
    )


def _autopilot_configured() -> bool:
    """Both worker routines have their SKILL.md on disk = the autopilot was set up
    here (so 'no drafts draining' is a real stall, not just unfinished setup)."""
    base = os.path.join(_claude_config_dir(), "scheduled-tasks")
    return all(
        os.path.exists(os.path.join(base, tid, "SKILL.md")) for tid in WORKER_TASK_IDS
    )


def _consecutive_timeouts() -> int:
    """The producer's LATCHED stall count: consecutive enqueue->timeout cycles with
    no drain since. Persists across the between-cycle gap, so it's the durable
    signal (the pending file is gone between cycles). Cleared on any successful
    drain. See claude_job.py::drain_status_path."""
    try:
        with open(os.path.join(_queue_root(), "drain-status.json")) as f:
            return int((json.load(f) or {}).get("consecutive_timeouts", 0) or 0)
    except Exception:
        return 0


def _oldest_pending_age() -> float | None:
    """Seconds since the oldest unclaimed pending draft job was written, or None
    if nothing is pending (idle queue). The FAST signal: catches a fresh stall
    before the first full producer timeout has latched."""
    pend_root = os.path.join(_queue_root(), "pending")
    oldest = None
    for sub in glob.glob(os.path.join(pend_root, "*")):
        for jf in glob.glob(os.path.join(sub, "*.json")):
            if jf.endswith(".tmp"):
                continue
            try:
                m = os.path.getmtime(jf)
            except OSError:
                continue
            if oldest is None or m < oldest:
                oldest = m
    if oldest is None:
        return None
    return time.time() - oldest


def _read_state() -> dict:
    try:
        with open(_watch_state_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(obj: dict) -> None:
    try:
        os.makedirs(_queue_root(), exist_ok=True)
        tmp = f"{_watch_state_path()}.tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, _watch_state_path())
    except Exception:
        pass


def _sentry():
    """Import the pipeline's Sentry helper (SAPS_REPO_DIR/scripts on path)."""
    repo = os.environ.get("SAPS_REPO_DIR")
    if repo:
        scripts = os.path.join(repo, "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
    import sentry_init  # noqa: E402

    return sentry_init


def main() -> int:
    age = _oldest_pending_age()
    timeouts = _consecutive_timeouts()
    # Durable latch OR fast pending-age (see the two helpers). Either alone is a
    # stall; both must be gated on the autopilot actually being configured here.
    stalled = _autopilot_configured() and (
        timeouts >= 1 or (age is not None and age > STALL_SECONDS)
    )

    st = _read_state()
    consecutive = int(st.get("consecutive", 0))
    alerted = bool(st.get("alerted", False))

    if not stalled:
        # Recovered (or never stalled) -> reset the episode so the next stall pages.
        if consecutive or alerted:
            _write_state({"consecutive": 0, "alerted": False})
        return 0

    consecutive += 1
    age_str = f"{int(age)}s" if age is not None else "n/a (between cycles)"
    if consecutive >= ALERT_AFTER and not alerted:
        try:
            sentry = _sentry()
            sentry.init()
            sentry.capture_message(
                "social-autoposter autopilot stalled: draft jobs are not being "
                "drained (scheduled-task routines likely orphaned — Claude Desktop "
                f"account change?). producer consecutive timeouts={timeouts}, "
                f"oldest pending job age={age_str}, sustained {consecutive} checks.",
                level="error",
                tags={
                    "component": "autopilot",
                    "issue": "stall",
                    "consecutive_timeouts": str(timeouts),
                    "oldest_pending_age_s": str(int(age)) if age is not None else "",
                },
            )
            sentry.flush()
        except Exception:
            # No Sentry (helper/SDK missing) -> at least leave a local breadcrumb.
            sys.stderr.write(
                f"[stall-watch] autopilot stalled (timeouts={timeouts}, "
                f"age={age_str}) but Sentry report failed\n"
            )
        alerted = True

    _write_state({"consecutive": consecutive, "alerted": alerted, "at": time.time()})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never let launchd see a non-zero/crash loop
        sys.stderr.write(f"[stall-watch] unexpected error: {e}\n")
        sys.exit(0)
