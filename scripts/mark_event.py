#!/usr/bin/env python3
"""Bracket a HOST-NATIVE tool call (create_scheduled_task, list_scheduled_tasks,
update_scheduled_task) with an explicit start/end marker.

WHY THIS EXISTS: those tools run entirely inside the Claude Desktop host
process, invisible to S4L's own tool-call telemetry (which only sees our own
MCP tools, e.g. queue_setup). During the Karol 2026-07-07 update-orphan
incident, create_scheduled_task and list_scheduled_tasks each silently hung
for 35-56 minutes with zero signal anywhere except manually reading gaps
between transcript message timestamps after the fact. This script is meant to
be run by the AGENT (via Bash), bracketing exactly those calls, per the
instructions baked into REARM_PROMPT / DIAGNOSE_PROMPT_TEMPLATE
(mcp/menubar/s4l_menubar.py) and the dashboard panel's copy (mcp/panel/panel.ts).

Usage:
  mark_event.py start <tool_name>   # call immediately BEFORE the host tool call
  mark_event.py end <tool_name>     # call immediately AFTER it returns

State: one small JSON file per tool_name under
~/.social-autoposter-mcp/host-tool-calls/<tool_name>.json. 'start' writes it;
'end' reads + removes it and computes the elapsed duration. A mismatched or
missing start on 'end' still reports (duration unknown) rather than silently
no-op'ing, so a broken bracket is itself visible in the log line.

Best-effort end to end: never raises, never blocks the caller, exits 0 always
(a failure here must not stop the actual scheduled-task registration).
"""

from __future__ import annotations

import json
import os
import sys
import time

# Only page/record when the call was slow enough to matter — routine
# sub-second calls shouldn't generate a Sentry event on every re-arm. 120s
# matches the "consecutive check" cadence used elsewhere in this codebase
# (e.g. autopilot_stall_watch.py's launchd interval).
SLOW_THRESHOLD_S = 120.0


def _state_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".social-autoposter-mcp", "host-tool-calls")


def _state_path(tool_name: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in tool_name)
    return os.path.join(_state_dir(), f"{safe}.json")


def _report(message: str, level: str, tags: dict, extra: dict | None = None) -> None:
    try:
        repo = os.environ.get("S4L_REPO_DIR")
        if repo:
            scripts_dir = os.path.join(repo, "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
        else:
            # No S4L_REPO_DIR in this chat turn's env — fall back to this
            # script's own directory, which IS scripts/ (sentry_init lives
            # right next to it).
            here = os.path.dirname(os.path.abspath(__file__))
            if here not in sys.path:
                sys.path.insert(0, here)
        import sentry_init

        sentry_init.init()
        sentry_init.capture_message(message, level=level, tags=tags, extra=extra)
        sentry_init.flush()
    except Exception:
        pass


def cmd_start(tool_name: str) -> int:
    try:
        os.makedirs(_state_dir(), exist_ok=True)
        tmp = f"{_state_path(tool_name)}.tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump({"tool_name": tool_name, "started_at": time.time()}, f)
        os.replace(tmp, _state_path(tool_name))
    except Exception as e:
        sys.stderr.write(f"[mark-event] start write failed for {tool_name}: {e}\n")
    sys.stderr.write(f"[mark-event] host tool call started: {tool_name}\n")
    return 0


def cmd_end(tool_name: str) -> int:
    started_at = None
    try:
        with open(_state_path(tool_name)) as f:
            started_at = (json.load(f) or {}).get("started_at")
    except Exception:
        pass
    try:
        os.remove(_state_path(tool_name))
    except Exception:
        pass

    duration = (time.time() - started_at) if started_at else None
    if duration is not None:
        sys.stderr.write(f"[mark-event] host tool call finished: {tool_name} took {duration:.1f}s\n")
        if duration > SLOW_THRESHOLD_S:
            _report(
                f"S4L host tool call was slow: {tool_name} took {duration:.1f}s",
                level="warning",
                tags={"component": "host_tool_timing", "tool_name": tool_name},
                extra={"duration_seconds": round(duration, 1)},
            )
    else:
        sys.stderr.write(f"[mark-event] host tool call finished: {tool_name} (no matching start)\n")
    return 0


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in ("start", "end"):
        sys.stderr.write("usage: mark_event.py start|end <tool_name>\n")
        return 0  # never a hard failure for the calling agent
    action, tool_name = sys.argv[1], sys.argv[2]
    return cmd_start(tool_name) if action == "start" else cmd_end(tool_name)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        sys.stderr.write(f"[mark-event] unexpected error: {e}\n")
        sys.exit(0)
