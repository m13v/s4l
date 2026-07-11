#!/usr/bin/env python3
"""LinkedIn posting cadence: 4 active days, then a mandatory 2-day break.

Per user instruction (2026-07-11): LinkedIn activity should not run more than
four days at a time before taking a two-day break. "Active day" is counted
only when real posting/engagement activity actually happened that day (an
outage day does not count toward the four), and the break pauses ALL LinkedIn
traffic, including the passive presence-check job, not just posting.

Every LinkedIn entrypoint (skill/run-linkedin.sh, engage-linkedin.sh,
engage-dm-replies.sh, dm-outreach-linkedin.sh, audit-linkedin.sh,
linkedin-presence.sh) already gates on the existence of ONE file:
    ~/.claude/social-autoposter/linkedin.killswitch
That file is scripts/linkedin_killswitch.py's antibot killswitch. This module
reuses the SAME file for a scheduled break (signal="scheduled_break") so every
entrypoint pauses automatically, with zero edits to the locked entrypoint
scripts. It never overwrites a REAL antibot signal, and linkedin_killswitch.py
is patched (recover-check) to never try to auto-recover a scheduled_break.

State lives at ~/.claude/social-autoposter/linkedin_cadence.json:
    {
      "phase": "active" | "break",
      "phase_started": "2026-07-11T20:00:00Z",
      "active_days": ["2026-07-13", "2026-07-14", ...]   # UTC dates with
                                                          # confirmed posts,
                                                          # only meaningful
                                                          # while phase=active
    }

CLI:
    python3 scripts/linkedin_cadence.py enforce   # one tick; called every 15m
    python3 scripts/linkedin_cadence.py status    # print state (json)
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import http_api  # noqa: E402
import linkedin_killswitch as ks  # noqa: E402

STATE_DIR = os.path.expanduser(
    os.environ.get("LINKEDIN_KILLSWITCH_DIR", "~/.claude/social-autoposter")
)
STATE_FILE = os.path.expanduser(
    os.environ.get("LINKEDIN_CADENCE_FILE", os.path.join(STATE_DIR, "linkedin_cadence.json"))
)

ACTIVE_DAYS_TARGET = int(os.environ.get("LINKEDIN_CADENCE_ACTIVE_DAYS", "4"))
BREAK_DAYS = int(os.environ.get("LINKEDIN_CADENCE_BREAK_DAYS", "2"))

SCHEDULED_BREAK_SIGNAL = "scheduled_break"


def _now():
    return datetime.now(timezone.utc)


def _now_iso():
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_str():
    return _now().date().isoformat()


def _parse_ts(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _ensure_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return None


def save_state(state):
    _ensure_dir()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    os.replace(tmp, STATE_FILE)


def _default_state_starting_break():
    # First-ever run: user asked to start immediately with a two-day break.
    return {"phase": "break", "phase_started": _now_iso(), "active_days": []}


def _today_post_count():
    """LinkedIn posts made today (UTC), via the same API the dashboard uses.

    Best-effort: returns None on any failure so callers can skip counting
    this tick rather than wrongly recording 0 activity."""
    try:
        resp = http_api.api_get(
            "/api/v1/dashboard/posts-per-day", {"days": 1, "platform": "linkedin"}
        )
        rows = (resp or {}).get("data", {}).get("rows", [])
        today = _today_str()
        for row in rows:
            if row.get("day") == today:
                return int(row.get("posts_made") or 0)
        return 0
    except Exception as exc:
        print(f"[linkedin_cadence] WARN: posts-per-day query failed: {exc}", file=sys.stderr)
        return None


def _pause_marker_set():
    """Set the shared killswitch file for a scheduled break, unless a REAL
    antibot signal is already in charge (never stomp a genuine block)."""
    payload = ks.read()
    if payload is None:
        ks.engage(
            signal=SCHEDULED_BREAK_SIGNAL,
            detail="cadence: scheduled 2-day pause after 4 active days",
            send_email=False,
        )
        print("[linkedin_cadence] pause marker set (scheduled_break)", file=sys.stderr)
    elif payload.get("signal") == SCHEDULED_BREAK_SIGNAL:
        pass  # already set by us; idempotent, no trail spam
    else:
        print(
            f"[linkedin_cadence] real killswitch already active (signal="
            f"{payload.get('signal')!r}); deferring to it, not overwriting",
            file=sys.stderr,
        )


def _pause_marker_clear_if_ours():
    payload = ks.read()
    if payload is not None and payload.get("signal") == SCHEDULED_BREAK_SIGNAL:
        ks.clear()
        print("[linkedin_cadence] pause marker cleared (break ended)", file=sys.stderr)


def enforce():
    state = load_state() or _default_state_starting_break()
    if load_state() is None:
        save_state(state)
        print(
            f"[linkedin_cadence] no prior state; starting BREAK phase now "
            f"({BREAK_DAYS}d)",
            file=sys.stderr,
        )

    now = _now()
    phase = state["phase"]

    if phase == "break":
        started = _parse_ts(state["phase_started"])
        elapsed = now - started
        if elapsed >= timedelta(days=BREAK_DAYS):
            state = {"phase": "active", "phase_started": _now_iso(), "active_days": []}
            save_state(state)
            _pause_marker_clear_if_ours()
            print(
                f"[linkedin_cadence] break ended after {elapsed}; switching to ACTIVE",
                file=sys.stderr,
            )
            phase = "active"
        else:
            remaining = timedelta(days=BREAK_DAYS) - elapsed
            _pause_marker_set()
            print(
                f"[linkedin_cadence] BREAK phase: {remaining} remaining",
                file=sys.stderr,
            )
            return

    # phase == "active"
    real_block = ks.read()
    if real_block is not None and real_block.get("signal") != SCHEDULED_BREAK_SIGNAL:
        print(
            f"[linkedin_cadence] account down for a real reason (signal="
            f"{real_block.get('signal')!r}); not counting today, not pausing",
            file=sys.stderr,
        )
        return

    count = _today_post_count()
    today = _today_str()
    if count is not None and count > 0 and today not in state["active_days"]:
        state["active_days"].append(today)
        save_state(state)
        print(
            f"[linkedin_cadence] activity confirmed today ({count} posts); "
            f"active_days={len(state['active_days'])}/{ACTIVE_DAYS_TARGET} "
            f"{state['active_days']}",
            file=sys.stderr,
        )

    if len(state["active_days"]) >= ACTIVE_DAYS_TARGET:
        state = {"phase": "break", "phase_started": _now_iso(), "active_days": []}
        save_state(state)
        _pause_marker_set()
        print(
            f"[linkedin_cadence] {ACTIVE_DAYS_TARGET} active days reached; "
            f"switching to BREAK for {BREAK_DAYS}d",
            file=sys.stderr,
        )
    else:
        print(
            f"[linkedin_cadence] ACTIVE phase: "
            f"{len(state['active_days'])}/{ACTIVE_DAYS_TARGET} active days so far",
            file=sys.stderr,
        )


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("enforce", "status"):
        print("usage: linkedin_cadence.py [enforce|status]", file=sys.stderr)
        sys.exit(2)
    if sys.argv[1] == "status":
        state = load_state()
        print(json.dumps(state if state is not None else {"phase": None}, indent=2))
        sys.exit(0)
    enforce()
    sys.exit(0)


if __name__ == "__main__":
    main()
