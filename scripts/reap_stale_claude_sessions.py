#!/usr/bin/env python3
"""Reap stale Claude agent-mode worker sessions left behind by the autopilot lane.

WHY THIS EXISTS
---------------
The queue-backed autopilot (2026-06-23) drives the drafting pipeline by having
Claude Desktop fire two scheduled tasks (`saps-phase1-query`, `saps-phase2b-draft`)
every ~1 minute. Each fire spawns a fresh `claude` agent-mode CLI session
(~200 MB RSS) plus its paired `disclaimer` launcher stub. The session does ONE
queue iteration and reports "done"... but the `claude` process does NOT exit —
Desktop keeps the agent-mode session alive (`--input-format stream-json`), so the
finished workers accumulate. On the MacStadium test box this reached **226
processes / 22.5 GB RSS** in ~1h (load average 75, 90% sys CPU, near-OOM). Every
customer box running the autopilot leaks the same way until it falls over.

We do not control Claude Desktop's session teardown, so this is the durable fix:
a launchd job (`com.m13v.social-claude-reaper`, StartInterval 60) runs this script
every minute and kills the leaked sessions, capping memory at a small steady state.

SAFETY — never kill a real interactive session
----------------------------------------------
The leaked workers share a tiny number of `local-agent-mode-sessions/<uuid>`
session ids (the scheduled-task lane reuses ONE persistent agent-mode session per
task and piles every fired turn onto it). A human's interactive Claude Desktop
agent-mode session is its OWN uuid with a single live process. So we:

  1. Only consider processes matching the worker signature (agent-mode `claude`
     from the bundled claude-code, NOT the Desktop app, the MCP node server, or ssh).
  2. Group by session uuid.
  3. In any group with >1 live process (the leak signature — a healthy session is
     exactly one process), keep the single NEWEST process (it may be mid-draft) and
     kill the rest that are older than the age threshold.
  4. Never touch a group of size 1 → a real interactive session is always spared.

This is allow-by-exclusion: when there is no leak, the script kills nothing.

Run under SYSTEM python (`/usr/bin/python3`, always present, zero deps) so it works
even before the owned runtime is provisioned.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time

# Age (seconds) past which a leaked worker session is reaped. The threshold MUST
# sit above the longest a worker's output can still matter, so we never kill a
# session that is legitimately mid-draft.
#
# What bounds a legit worker turn — measured, not assumed:
#   * The producer (claude_job.py) abandons a queued job after
#     SAPS_CLAUDE_QUEUE_TIMEOUT (default 1800s / 30 min): once a worker has been
#     going longer than that, the producer has already removed the job and
#     discarded whatever the worker eventually writes. So the queue timeout is the
#     hard ceiling on USEFUL worker work. (It was 600s until 2026-06-27, but 600s
#     sat at the edge of the ~9-10 min draft call and dropped ~41% of twitter-prep
#     jobs on the QA box; raised to 1800s to match the draft's real need + the
#     direct `claude -p` lane's tolerance. This base MUST stay in lockstep with
#     claude_job.py:DEFAULT_TIMEOUT_S — both read SAPS_CLAUDE_QUEUE_TIMEOUT.)
#   * The 180-MINUTE budgets in watchdog_hung_runs.py are NOT this. Those govern
#     run-twitter-cycle.sh / stats.sh, which run as `bash`/python pipeline
#     processes, not `claude` agent-mode sessions — the reaper signature can never
#     match them. Do not conflate the pipeline budget with the worker-turn ceiling.
#
# So the floor is the queue timeout; we take 2x it as the default for margin. A
# session older than 2x the producer's own deadline is provably done/abandoned.
# The 2x relationship is what guarantees the reaper never SIGKILLs a draft the
# producer is still legitimately waiting on (reaper threshold >= producer timeout).
_QUEUE_TIMEOUT_S = int(os.environ.get("SAPS_CLAUDE_QUEUE_TIMEOUT", "1800"))
DEFAULT_MAX_AGE_SEC = _QUEUE_TIMEOUT_S * 2  # 3600s (60 min) by default

# Hard cap on kills per run, so a pathological ps parse can never SIGKILL the world.
MAX_KILL_PER_RUN = 500

# The worker signature: the bundled claude-code agent-mode CLI driven by a
# scheduled task. ALL of these must be present in the command line. This excludes
# the Desktop app (`Claude.app/Contents/MacOS/Claude`, no claude-code path), the
# MCP node server, ssh, and any non-agent-mode `claude`.
SIG_REQUIRED = (
    "claude-code/",
    "/Contents/MacOS/claude ",
    "--input-format stream-json",
    "local-agent-mode-sessions",
)

# The `disclaimer` launcher stub's command line embeds the full claude invocation
# it spawned, so it ALSO matches SIG_REQUIRED. Exclude it here: we only want the
# real `claude` child in the uuid groups. The stub is the child's parent, reaped
# separately via the ppid path so each pair is cleaned together.
DISCLAIMER_HINT = "Helpers/disclaimer"
SIG_EXCLUDED = (DISCLAIMER_HINT,)

UUID_RE = re.compile(r"local-agent-mode-sessions/([0-9a-fA-F-]{36})")


def parse_etime(etime: str) -> int:
    """macOS `ps -o etime` -> seconds. Format: [[dd-]hh:]mm:ss."""
    etime = etime.strip()
    days = 0
    if "-" in etime:
        d, etime = etime.split("-", 1)
        days = int(d)
    parts = etime.split(":")
    parts = [int(p) for p in parts]
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = 0, parts[0], parts[1]
    else:  # len 1
        h, m, s = 0, 0, parts[0]
    return ((days * 24 + h) * 60 + m) * 60 + s


def snapshot():
    """Return list of dicts {pid, ppid, age, cmd} for worker-signature processes."""
    out = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,etime=,command="],
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout
    me = os.getpid()
    procs = []
    by_pid = {}
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)$", line)
        if not m:
            continue
        pid, ppid, etime, cmd = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
        by_pid[pid] = cmd
        if pid == me or pid <= 1:
            continue
        if not all(tok in cmd for tok in SIG_REQUIRED):
            continue
        if any(tok in cmd for tok in SIG_EXCLUDED):
            continue
        u = UUID_RE.search(cmd)
        if not u:
            continue
        try:
            age = parse_etime(etime)
        except Exception:
            continue
        procs.append({"pid": pid, "ppid": ppid, "age": age, "uuid": u.group(1), "cmd": cmd})
    return procs, by_pid


def kill(pid: int) -> bool:
    """SIGTERM, brief grace, then SIGKILL. True if a signal was delivered."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    for _ in range(10):  # up to ~0.5s grace
        time.sleep(0.05)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        return False
    return True


def main() -> int:
    dry = "--dry-run" in sys.argv
    try:
        max_age = int(os.environ.get("SAPS_REAPER_MAX_AGE_SEC", DEFAULT_MAX_AGE_SEC))
    except ValueError:
        max_age = DEFAULT_MAX_AGE_SEC

    procs, by_pid = snapshot()

    # Group by session uuid.
    groups: dict[str, list[dict]] = {}
    for p in procs:
        groups.setdefault(p["uuid"], []).append(p)

    targets: list[dict] = []
    for uuid, members in groups.items():
        if len(members) <= 1:
            continue  # a healthy / interactive session — never touch.
        members.sort(key=lambda p: p["age"])  # ascending: newest first
        newest = members[0]
        for p in members[1:]:  # everything except the single newest
            if p["age"] >= max_age:
                targets.append(p)
        # newest is always spared (may be mid-draft); also spare anything younger
        # than the threshold (handled by the age check above).
        _ = newest

    targets = targets[:MAX_KILL_PER_RUN]

    if not targets:
        # Stay quiet on the common no-leak path to keep the log stream clean.
        return 0

    freed_kb = 0
    killed = 0
    disclaimers = 0
    for p in targets:
        ok = dry or kill(p["pid"])
        if not ok:
            continue
        killed += 1
        # Reap the paired `disclaimer` launcher stub (the claude proc's parent) too.
        parent_cmd = by_pid.get(p["ppid"], "")
        if DISCLAIMER_HINT in parent_cmd:
            if dry or kill(p["ppid"]):
                disclaimers += 1

    prefix = "[claude-reaper]" + (" DRY-RUN" if dry else "")
    print(
        f"{prefix} reaped {killed} stale agent-mode claude session(s)"
        f" + {disclaimers} disclaimer stub(s) across {sum(1 for g in groups.values() if len(g) > 1)}"
        f" leaked uuid group(s); threshold={max_age}s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never let the reaper itself crash the launchd job loudly
        print(f"[claude-reaper] error: {e}", file=sys.stderr)
        sys.exit(0)
