#!/usr/bin/env python3
"""saps_activity.py — shared writer for the menu-bar activity.json signal.

The S4L menu bar polls ``<state_dir>/activity.json`` every second and shows a
spinner + label while work is happening (scanning / drafting / posting / …).
Several lanes do work the menu bar should narrate, but historically only two of
them wrote this file:

  - the MCP server (TypeScript ``writeActivity``) — for IN-CHAT tool calls only,
  - ``twitter_post_plan.py`` — for per-post posting progress.

The unattended draft autopilot was invisible: the launchd kicker's scan phase
runs inside the (locked) ``run-twitter-cycle.sh`` with no writer, and Phase-2b
drafting is done by the queue provider (which only blocks) and the Claude Desktop
scheduled-task worker (which never wrote anything). So "scanning" and "drafting"
never showed on the box.

This module is the single Python writer those lanes share, keeping the JSON shape
and the state-dir resolution byte-identical to the TS + poster writers so the menu
bar reads one consistent signal regardless of who produced the work.

Purely cosmetic and fully best-effort: a failure here MUST never affect the work
it narrates. Every public call swallows its own exceptions.

State-dir resolution matches everything else: ``$SAPS_STATE_DIR`` or
``~/.social-autoposter-mcp``. The scheduled-task worker sets ``SAPS_STATE_DIR``
in the env before calling in (see ``claude_job.py::_apply_state_dir_override``),
so the worker lane lands in the same dir the launchd kicker and menu bar use.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone


def state_dir() -> str:
    return os.environ.get("SAPS_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".social-autoposter-mcp"
    )


def _path() -> str:
    return os.path.join(state_dir(), "activity.json")


def write(state: str, label: str) -> None:
    """Mirror the Node server's writeActivity shape: {state, label, since}.

    Written atomically (tmp + os.replace) so the menu bar's 1s poll never reads a
    half-written file. Best-effort: any failure is swallowed.
    """
    try:
        sd = state_dir()
        os.makedirs(sd, exist_ok=True)
        payload = {
            "state": state,
            "label": label,
            "since": datetime.now(timezone.utc).isoformat(),
        }
        target = _path()
        tmp = f"{target}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
        os.replace(tmp, target)
    except Exception:
        pass


def clear() -> None:
    """Remove the activity signal so no stuck 'scanning/drafting' lingers after a
    cycle, a worker turn, or an early exit. Idempotent; safe to double-clear."""
    try:
        p = _path()
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def _main(argv: list[str]) -> int:
    # CLI used by shell lanes (run-draft-and-publish.sh):
    #   saps_activity.py write <state> <label words...>
    #   saps_activity.py clear
    if not argv:
        return 0
    cmd = argv[0]
    if cmd == "clear":
        clear()
    elif cmd == "write":
        state = argv[1] if len(argv) > 1 else "working"
        label = " ".join(argv[2:]) if len(argv) > 2 else ""
        write(state, label)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
