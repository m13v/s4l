#!/usr/bin/env python3
"""Ship a menu-bar "Diagnose & fix" report back to the S4L developers.

Usage: send_diagnostic_report.py <report.md> [reason-code]

The menu bar's ⚠ "Diagnose & fix in Claude…" item hands Claude a prompt that ends
with: write a short markdown report of the diagnosis (symptom, root cause, actions
taken, current state) and run this script on it. We ship it over the same Sentry
lane the reaper and menu bar already use (sentry_init tags every event with the
install identity), so field diagnoses land next to the "autopilot needs attention"
warnings they resolve. The report body rides in the message itself, truncated to
stay inside Sentry's message limits; the full file stays on disk under
~/.social-autoposter-mcp/diagnostics/ for follow-up over the QA/SSH lane.

Exit codes: 0 shipped, 1 usage / unreadable file, 2 telemetry unavailable (the
report file still exists locally either way — say so, never lose the diagnosis).
"""

from __future__ import annotations

import os
import sys

# 8KB is Sentry's formatted-message ceiling; leave headroom for the header line.
MAX_BODY_CHARS = 6000

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: send_diagnostic_report.py <report.md> [reason-code]", file=sys.stderr)
        return 1
    path = sys.argv[1]
    reason = sys.argv[2] if len(sys.argv) > 2 else "unspecified"
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            body = f.read().strip()
    except OSError as e:
        print(f"cannot read report file: {e}", file=sys.stderr)
        return 1
    if not body:
        print("report file is empty — write the diagnosis first", file=sys.stderr)
        return 1
    truncated = len(body) > MAX_BODY_CHARS
    if truncated:
        body = body[:MAX_BODY_CHARS] + "\n…[truncated; full report on the box]"

    try:
        import sentry_init
        sentry_init.init()
        sentry_init.capture_message(
            "S4L field diagnosis report\n\n" + body,
            level="warning",
            tags={
                "component": "diagnose_fix",
                "phase": "field_report",
                "reason": reason,
                "truncated": str(truncated).lower(),
            },
        )
        sentry_init.flush(5.0)
    except Exception as e:
        print(
            f"telemetry unavailable ({e}); report kept locally at {path}",
            file=sys.stderr,
        )
        return 2
    print(f"report shipped to S4L telemetry (kept locally at {path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
