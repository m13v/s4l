#!/usr/bin/env python3
"""live_status.py — ONE computation of "what is S4L doing right now, and when
does the scanner run next", shared by every surface that shows it.

Two surfaces render this today and must never drift:
  - the always-on menu bar (imports this module in-process), and
  - the dashboard widget (reads these fields off scripts/snapshot.py, which
    merges compute() into its snapshot).

It answers two questions from purely-local state files, stdlib only (so it works
before the owned runtime is provisioned, exactly like schedule_state.py):

  1. activity — the live pipeline verb the menu bar spinner narrates
     (scanning / drafting / posting / idle), read from ``activity.json`` with
     the SAME staleness TTL the menu bar applies, so a writer that died without
     clearing its label reads as idle instead of a frozen lie.

  2. next scanner run — the launchd kicker (com.m13v.social-twitter-cycle) fires
     on a fixed StartInterval, so ``next_run = last_cycle_start + interval``.
     ``last_cycle_start`` is stamped by run-draft-and-publish.sh at the top of
     each cycle (``last-cycle-start`` marker in the state dir); the interval is
     the kicker's StartInterval (QUEUE_KICKER_INTERVAL_SECS, mirrored here).

Purely informational: every read is best-effort and degrades to a safe default
(idle / unknown next-run) rather than raising, because the callers render this
in a tight tick loop / a paint path where an exception must never surface.
"""

from __future__ import annotations

import json
import os
import time

# Mirror mcp/src/index.ts QUEUE_KICKER_INTERVAL_SECS (the launchd StartInterval
# of com.m13v.social-twitter-cycle). Overridable for tests / a non-default plist.
KICKER_INTERVAL_SECS = int(os.environ.get("S4L_KICKER_INTERVAL_SECS", "60"))

# Mirror mcp/menubar/s4l_state.py ACTIVITY_TTL_SECONDS: a signal older than this
# can only be a stuck stamp (live work re-heartbeats every ~10-30s), so it reads
# as idle. Kept in sync deliberately — both surfaces must age the label the same.
ACTIVITY_TTL_SECONDS = float(os.environ.get("S4L_ACTIVITY_TTL_S", "120"))

# Pipeline verbs a writer may stamp; anything else (or absent/stale) is "idle".
KNOWN_ACTIVITY_STATES = ("scanning", "drafting", "posting")


def _state_dir() -> str:
    return os.environ.get("S4L_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".social-autoposter-mcp"
    )


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _activity_age_secs(act) -> float | None:
    """Seconds since act['since'], or None when it can't be parsed (fail open:
    an unparsable `since` is treated as fresh, never hidden)."""
    try:
        import datetime

        since = (act or {}).get("since")
        if not since:
            return None
        ts = datetime.datetime.fromisoformat(str(since).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds()
    except Exception:
        return None


def _last_cycle_start_epoch(sd: str) -> float | None:
    """Epoch seconds of the most recent scanner cycle start, from the
    ``last-cycle-start`` marker run-draft-and-publish.sh stamps. Falls back to
    the marker file's mtime if the body isn't a parseable number. None when the
    marker is absent (no cycle has started this install session yet)."""
    p = os.path.join(sd, "last-cycle-start")
    try:
        raw = ""
        with open(p, encoding="utf-8") as f:
            raw = f.read().strip()
        if raw:
            return float(raw)
    except Exception:
        pass
    try:
        return os.path.getmtime(p)
    except Exception:
        return None


def compute(state_dir: str | None = None) -> dict:
    """Return the live-status + next-run fields both surfaces render.

    Keys (all best-effort, always present):
      activity_state   str  one of scanning|drafting|posting|idle
      activity_label   str  human label the writer stamped ("scan 12/118 · 2m"),
                            "" when idle
      activity_since   str|None  ISO timestamp the current phase started
      next_run_epoch   float|None  when the scanner next fires (unix seconds)
      next_run_secs    int|None    seconds until then, floored at 0
      kicker_interval_secs int     the launchd StartInterval used
    """
    sd = state_dir or _state_dir()

    act = _read_json(os.path.join(sd, "activity.json"))
    state = "idle"
    label = ""
    since = None
    if isinstance(act, dict):
        age = _activity_age_secs(act)
        stale = age is not None and age > ACTIVITY_TTL_SECONDS
        raw_state = str(act.get("state") or "").lower()
        if not stale and raw_state in KNOWN_ACTIVITY_STATES:
            state = raw_state
            label = str(act.get("label") or "")
            since = act.get("since")

    last_start = _last_cycle_start_epoch(sd)
    next_run_epoch = None
    next_run_secs = None
    if last_start:
        next_run_epoch = last_start + KICKER_INTERVAL_SECS
        next_run_secs = max(0, int(round(next_run_epoch - time.time())))

    return {
        "activity_state": state,
        "activity_label": label,
        "activity_since": since,
        "next_run_epoch": next_run_epoch,
        "next_run_secs": next_run_secs,
        "kicker_interval_secs": KICKER_INTERVAL_SECS,
    }


def main() -> int:
    print(json.dumps(compute()))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
