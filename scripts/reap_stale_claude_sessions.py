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
     exactly one process), apply the TYPE-DRIVEN rule: a worker checks the queue
     exactly once per fire (claude_job.py::cmd_next is single-shot), so it either
     CLAIMS a job (stamps claim_pid -> actively drafting) or is a permanent typeless
     husk. Spare (a) claim-holders and (b) newborns still inside their boot+claim
     window (age < claim_grace); reap every other claimless session immediately.
  4. Never touch a group of size 1 → a real interactive session is always spared.

This is allow-by-exclusion: when there is no leak, the script kills nothing. The old
"keep the single newest by count" heuristic (max_group) is retained only as a
pathological backstop; the claim/grace rule is the primary brake.

Run under SYSTEM python (`/usr/bin/python3`, always present, zero deps) so it works
even before the owned runtime is provisioned.
"""

from __future__ import annotations

import datetime as dt
import json
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
# The floor is the queue timeout; we add a FIXED MARGIN (not a full 2x) on top.
# Once a worker outlives the producer's deadline the producer has already discarded
# its result, so the session is provably useless: there is nothing left to protect,
# and killing it sooner is strictly better. A ~200MB agent-mode session that lingers
# to the old 2x (60 min) piles up toward OOM on busy boxes (cf. the Ezra leaked-
# session pileup: 29 sessions, ~4GB, near-OOM). The margin's only job is to avoid
# racing a draft the producer is still reading AT the deadline. Invariant preserved:
# the reaper threshold (timeout + margin) is always strictly greater than the
# producer timeout. Override the margin with SAPS_REAPER_AGE_MARGIN_SEC, or pin an
# absolute age with SAPS_REAPER_MAX_AGE_SEC.
_QUEUE_TIMEOUT_S = int(os.environ.get("SAPS_CLAUDE_QUEUE_TIMEOUT", "1800"))
_REAPER_AGE_MARGIN_S = int(os.environ.get("SAPS_REAPER_AGE_MARGIN_SEC", "300"))
DEFAULT_MAX_AGE_SEC = _QUEUE_TIMEOUT_S + _REAPER_AGE_MARGIN_S  # 2100s (35 min) by default

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

# A LOOSER probe used purely for telemetry (never for killing): any process that
# looks like a bundled claude-code agent-mode worker, even if it does NOT satisfy
# the full SIG_REQUIRED tuple or its session path fails UUID_RE. This is the exact
# blind spot that let Karol's box leak undetected: a newer Claude Code changed the
# session-path shape so UUID_RE stopped matching, the worker fell out of `procs`,
# and the reaper saw "nothing to do" while ~289 workers piled up. We count these
# separately (`unparsed_worker_procs`) so a future regression is VISIBLE centrally
# instead of silent.
WORKER_PROBE = ("claude-code/", "--input-format stream-json")

UUID_RE = re.compile(r"local-agent-mode-sessions/([0-9a-fA-F-]{36})")

# The paired leak: every leaked `claude` worker spawns a `mcp-server-macos-use`
# node child (the remote-macos-use MCP). When the reaper SIGKILLs the worker, that
# child is ORPHANED (reparented to launchd) and never exits — so it accumulates in
# lockstep with the claude workers. Karol's box hit 280 orphaned MCP procs / 11 GB
# this way. This regex mirrors memory_snapshot.py::_is_remote_macos_mcp_server so we
# kill exactly the process the telemetry measures as leaking. ssh commands that merely
# mention the string are excluded via _SSH_RE below.
MACOS_MCP_RE = re.compile(r"(^|\s)(?:/[^ \t]+/)?mcp-server-macos-use(?:\s|$)")
_SSH_RE = re.compile(r"^(?:/[^ \t]+/)?ssh(?:\s|$)")


def _run_ps() -> str:
    """`ps -axo` with a generous timeout + one retry. Under a runaway leak the box is
    at load 75 / 90% sys CPU and a 20s ps can time out -> the old code raised, caught,
    and reaped NOTHING exactly when reaping mattered most. Bump to 45s and retry once
    before giving up."""
    for attempt in range(2):
        try:
            return subprocess.run(
                ["/bin/ps", "-axo", "pid=,ppid=,etime=,command="],
                capture_output=True,
                text=True,
                timeout=45,
            ).stdout
        except subprocess.TimeoutExpired:
            if attempt == 0:
                time.sleep(1.0)
                continue
            raise
    return ""


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
    """Snapshot the process table once.

    Returns (procs, by_pid, macos_mcp, meta, stats):
      * procs     — claude agent-mode worker-signature processes (the primary target).
      * by_pid    — {pid: cmd} for every process (used to pair the disclaimer stub).
      * macos_mcp — {pid, ppid, age, cmd} for every `mcp-server-macos-use` node server
                    (the paired leak, reaped in main()).
      * meta      — {pid: {ppid, age}} for every process, so main() can tell whether an
                    MCP server's parent is still alive (orphan detection).
      * stats     — {ps_timed_out, snapshot_empty, worker_probe_seen, reapable_workers,
                    unparsed_worker_procs, macos_mcp_seen, total_procs}. Pure telemetry
                    so a future regression (e.g. UUID_RE stops matching a newer Claude
                    Code, the exact blind spot on Karol's box) is VISIBLE centrally
                    instead of silently piling up.
    """
    stats = {
        "ps_timed_out": False,
        "snapshot_empty": False,
        "worker_probe_seen": 0,     # procs that look like a claude-code agent worker
        "reapable_workers": 0,      # of those, ones that satisfy SIG + UUID (=len(procs))
        "unparsed_worker_procs": 0, # probe-positive but NOT reapable (regex/sig miss)
        "macos_mcp_seen": 0,
        "total_procs": 0,
    }
    try:
        out = _run_ps()
    except subprocess.TimeoutExpired:
        # ps timed out even after the retry (box is at load 75 / 90% sys under a
        # runaway leak). Surface it: a blind reaper cycle is a first-class datapoint,
        # not a swallowed exception.
        stats["ps_timed_out"] = True
        stats["snapshot_empty"] = True
        return [], {}, [], {}, stats
    if not out.strip():
        stats["snapshot_empty"] = True
    me = os.getpid()
    procs = []
    macos_mcp = []
    by_pid = {}
    meta = {}
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)$", line)
        if not m:
            continue
        pid, ppid, etime, cmd = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
        by_pid[pid] = cmd
        stats["total_procs"] += 1
        try:
            age = parse_etime(etime)
        except Exception:
            age = 0
        meta[pid] = {"ppid": ppid, "age": age}
        if pid == me or pid <= 1:
            continue
        # (a) remote-macos-use MCP node servers — the paired leak. NOT gated by the
        # claude worker signature; these are separate node procs the workers spawn.
        if MACOS_MCP_RE.search(cmd) and not _SSH_RE.match(cmd):
            macos_mcp.append({"pid": pid, "ppid": ppid, "age": age, "cmd": cmd})
            stats["macos_mcp_seen"] += 1
            continue
        # Telemetry probe: does this look like a claude-code agent worker at all?
        # Deliberately looser than SIG_REQUIRED, and it EXCLUDES the disclaimer stub
        # so we don't double-count the launcher parent.
        is_probe = (
            all(tok in cmd for tok in WORKER_PROBE)
            and not any(tok in cmd for tok in SIG_EXCLUDED)
        )
        if is_probe:
            stats["worker_probe_seen"] += 1
        # (b) claude agent-mode worker sessions — the REAPABLE set.
        if not all(tok in cmd for tok in SIG_REQUIRED):
            if is_probe:
                stats["unparsed_worker_procs"] += 1  # looks like a worker, sig miss
            continue
        if any(tok in cmd for tok in SIG_EXCLUDED):
            continue
        u = UUID_RE.search(cmd)
        if not u:
            # Full signature but the session path shape defeated UUID_RE — THE Karol
            # blind spot. Count it so the leak is never invisible again.
            if is_probe:
                stats["unparsed_worker_procs"] += 1
            continue
        procs.append({"pid": pid, "ppid": ppid, "age": age, "uuid": u.group(1), "cmd": cmd})
    stats["reapable_workers"] = len(procs)
    return procs, by_pid, macos_mcp, meta, stats


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


def _state_dir() -> str:
    """Same resolution claude_job.py uses: $SAPS_STATE_DIR or ~/.social-autoposter-mcp."""
    return os.environ.get("SAPS_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".social-autoposter-mcp"
    )


def write_status(status: dict) -> None:
    """Persist the last reaper cycle to <state_dir>/claude-queue/reaper-status.json
    (atomic write). memory_snapshot.py reads this file and carries it on the heartbeat,
    so the reaper — a SEPARATE launchd job whose stderr only lands in a local file — is
    finally observable centrally. Mirrors the drain_status.json pattern. Best-effort:
    the reaper's real work must never fail because telemetry could not be written."""
    try:
        d = os.path.join(_state_dir(), "claude-queue")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "reaper-status.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(status, f)
        os.replace(tmp, path)
    except Exception:
        pass


def count_running_jobs():
    """Number of IN-FLIGHT claimed jobs, or None if the queue dir is unreadable.

    The producer (claude_job.py) moves a job into <state_dir>/claude-queue/running/
    the instant a worker CLAIMS it (`next`), and removes it the instant the worker
    REPORTS back (`result`) OR the producer abandons it at its own timeout. So the
    count of files here is an upper bound on how many workers are legitimately busy
    right now. When this is readable we spare exactly that many (plus a margin) of
    the newest workers and reap the rest immediately — no 20-minute wait. When it is
    unreadable we return None and the caller falls back to the pure age gate, so a
    missing/renamed queue can never turn the reaper INTO a regression.
    """
    d = os.path.join(_state_dir(), "claude-queue", "running")
    try:
        return sum(
            1 for n in os.listdir(d) if n.endswith(".json") and not n.endswith(".tmp")
        )
    except OSError:
        return None


def running_claim_pids():
    """Set of agent-session pids that currently hold a LIVE claim. The worker stamps
    its agent-session pid into <state_dir>/claude-queue/running/<job>.json the instant
    it claims a job (claude_job.py::cmd_next). A session that holds a claim is, by
    definition, the one doing real drafting work right now — so we spare those pids
    UNCONDITIONALLY (regardless of age / group size) and only reap sessions that do
    NOT hold a claim. This is what makes a multi-minute draft survive: it is no longer
    confused with a leaked/done zombie just because newer empty sessions spawned on
    top of it. Empty set if the dir is unreadable or nothing has been stamped (then
    the caller falls back to the newest-spare heuristic, i.e. prior behaviour)."""
    d = os.path.join(_state_dir(), "claude-queue", "running")
    pids: set[int] = set()
    try:
        names = os.listdir(d)
    except OSError:
        return pids
    for n in names:
        if not n.endswith(".json") or n.endswith(".tmp"):
            continue
        try:
            with open(os.path.join(d, n)) as f:
                job = json.load(f)
            pid = job.get("claim_pid")
            if isinstance(pid, int) and pid > 1:
                pids.add(pid)
        except Exception:
            continue
    return pids


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def main() -> int:
    dry = "--dry-run" in sys.argv
    max_age = _env_int("SAPS_REAPER_MAX_AGE_SEC", DEFAULT_MAX_AGE_SEC)
    # (1) Queue-correlated reaping knob.
    #
    # ONE age ceiling, `max_age` (35 min = producer deadline 1800s + margin). There is
    # deliberately no second, shorter timer.
    #
    # History (2026-06-29): the queue-readable branch below used to apply its OWN short
    # `grace` (90s, then 300s) as the age gate -- an activity-BLIND timer that governed
    # normal operation and silently overrode this 35-min ceiling. An actively-DRAFTING
    # session ages out of the "inflight+margin newest" window after ~2 min as fresh
    # empty workers spawn on top of it, so the short grace SIGTERMed it mid-draft -> the
    # "~120s code-143 kill". That second timer is removed: a session is reapable by age
    # ONLY once it outlives max_age, by which point the producer has already discarded
    # its result, so it is provably useless regardless of whether it ever claimed.
    #
    # What bounds MEMORY instead of a short timer: (a) claim-holders are spared outright
    # via running_claim_pids() -- the actively-drafting session is "dragged" along and
    # never reaped; (b) the count-cap (max_group) reaps the oldest-beyond-N by COUNT,
    # regardless of age, and never touches a claim-holder.
    keep_margin = _env_int("SAPS_REAPER_KEEP_MARGIN", 1)  # extra newest spared beyond busy set
    # (2) Count-cap backstop: never let one uuid group hold more than this many live
    # workers, regardless of queue state. 0 disables. This is now the PRIMARY brake,
    # not just a pathological backstop: at inflight=0 the age ceiling never fires
    # (sessions never live 35 min), so the count-cap is the only thing trimming the
    # pile of typeless empty warm sessions.
    #
    # Why 2 (2026-07-01, per Matthew): the Desktop scheduled-task launcher spawns every
    # worker with a BYTE-IDENTICAL command line (verified on the box: task name, plugin
    # token, and session uuid are the same across all 24 live workers), so the reaper
    # cannot distinguish "scan" from "draft" workers via ps. It doesn't need to: the
    # serial producer guarantees <=1 active job PER TYPE (<=2 total), those active
    # sessions are the claim-holders spared outright by running_claim_pids(), and every
    # session beyond them is a typeless idle empty. So a global cap of 2 == the intended
    # "1 scan + 1 draft" per-type cap, without needing type visibility in ps. It never
    # caps below inflight+margin (see keep = max(...) below), so an active drafter is
    # never at risk.
    max_group = _env_int("SAPS_REAPER_MAX_GROUP", 2)

    # (3) Claim grace — the PRIMARY brake (2026-07-01, per Matthew). A worker checks
    # the queue EXACTLY ONCE per fire: claude_job.py::cmd_next is single-shot — it
    # claims one pending job (stamping claim_pid) or prints {} and returns; it never
    # polls again. So within one cron tick of spawning, a session either CLAIMS a job
    # (=> it has a "type", is actively drafting, and is spared outright via
    # running_claim_pids()) or finds the queue empty and becomes a PERMANENT typeless
    # husk that will NEVER claim again. Those husks are exactly what we want to kill.
    #
    # The ONLY reason to spare a claimless session is that it may not have run its one
    # cmd_next yet (cold agent-mode boot: skill load + MCP init before the first tool
    # call). claim_grace bounds that boot+claim window. Measured on the box:
    # enqueue->claim was ALWAYS < 60s (3-55s across 85 claims); 120s is a generous
    # margin. Past claim_grace a claimless session is a proven husk -> reap it now,
    # regardless of the 35-min age ceiling and regardless of group size. This is the
    # type-driven rule: spare drafters + spare boot-window newborns, reap all the rest.
    # Worst case of an over-tight grace is a job delayed one tick (it stays in pending
    # for the next worker), never a lost draft. A DRAFTING session is protected by
    # claim_pids, not by grace, so no grace value can kill a real draft (this is what
    # makes the old "~120s code-143 mid-draft kill" impossible now).
    claim_grace = _env_int("SAPS_REAPER_CLAIM_GRACE_SEC", 120)

    inflight = count_running_jobs()  # None => queue unreadable => age-gate fallback
    claim_pids = running_claim_pids()  # agent-session pids actively holding a claim

    procs, by_pid, macos_mcp, meta, stats = snapshot()

    # Group by session uuid.
    groups: dict[str, list[dict]] = {}
    for p in procs:
        groups.setdefault(p["uuid"], []).append(p)

    targets_by_pid: dict[int, dict] = {}  # dedup across the two rules below
    for uuid, members in groups.items():
        if len(members) <= 1:
            continue  # a healthy / interactive session — never touch.
        members.sort(key=lambda p: p["age"])  # ascending: newest first

        if inflight is not None:
            # (1) TYPE-DRIVEN reaping — the primary rule. A session is spared iff it
            # (a) holds a live claim (actively drafting — never reap, at any age), OR
            # (b) is younger than claim_grace (may not have run its one-shot cmd_next
            # yet — the cold-boot window). EVERY other session in a leaked group is a
            # claimless husk that already ran its single queue check and found nothing,
            # so it will never claim again: reap it now, no age ceiling needed.
            for p in members:
                if p["pid"] in claim_pids:
                    continue  # holds a live claim -> actively drafting, never reap
                if p["age"] < claim_grace:
                    continue  # newborn: may still run its one-shot claim
                targets_by_pid[p["pid"]] = p  # claimless past grace = proven husk
        else:
            # Fallback: queue unreadable -> can't tell claimed from husk, so drop back
            # to the conservative age gate (keep newest, reap only past the 35-min
            # ceiling). A missing/renamed queue must never turn the reaper aggressive.
            for p in members[1:]:
                if p["pid"] in claim_pids:
                    continue
                if p["age"] >= max_age:
                    targets_by_pid[p["pid"]] = p

        # (2) Count-cap backstop. With rule (1) already sweeping every claimless husk
        # past grace, this is now REDUNDANT in steady state and kept only as a
        # pathological guard (e.g. a spawn storm of sessions all still inside their
        # grace window). It never caps below the busy set, never reaps a live
        # claim-holder, and — matching rule (1) — never reaps a newborn inside its
        # claim window, so it can only ever add provably-idle husks.
        if max_group > 0:
            keep = max_group
            if inflight is not None:
                keep = max(keep, inflight + keep_margin)
            for p in members[keep:]:
                if p["pid"] in claim_pids:
                    continue
                if p["age"] < claim_grace:
                    continue  # never reap a session still inside its boot+claim window
                targets_by_pid[p["pid"]] = p

    targets = list(targets_by_pid.values())[:MAX_KILL_PER_RUN]

    # Visibility (per the 2026-06-29 draft-kill investigation): whenever a draft is
    # in flight, log that we SAW the claim-holder(s) and are sparing them, so a
    # future "why did the draft die" check can confirm the reaper protected the
    # right session — or catch it red-handed if this logic ever regresses.
    if claim_pids:
        live = sorted(p for p in claim_pids if p in by_pid)
        dead = sorted(p for p in claim_pids if p not in by_pid)
        print(
            f"[claude-reaper] sparing {len(live)} live claim-holder session(s)"
            f" pids={live}" + (f" (stale-claim pids={dead})" if dead else "")
            + f"; inflight={inflight} ceiling={max_age}s",
            file=sys.stderr,
        )

    live_pids = set(meta.keys())

    killed = 0
    disclaimers = 0
    killed_pids: set[int] = set()
    for p in targets:
        ok = dry or kill(p["pid"])
        if not ok:
            continue
        killed += 1
        killed_pids.add(p["pid"])
        # Reap the paired `disclaimer` launcher stub (the claude proc's parent) too.
        parent_cmd = by_pid.get(p["ppid"], "")
        if DISCLAIMER_HINT in parent_cmd:
            if dry or kill(p["ppid"]):
                disclaimers += 1

    # (3) Reap paired / orphaned remote-macos-use MCP node servers — the SECOND half of
    # the double leak. SIGKILLing a worker orphans its `mcp-server-macos-use` child
    # (reparented to launchd), so it survives forever. Reap an MCP proc when (a) its
    # parent is a worker we just killed, or (b) it is ALREADY orphaned (parent pid gone)
    # AND older than max_age. An MCP proc whose parent is a LIVE process (a healthy
    # in-flight worker, or the Desktop app itself) is never touched — so this can only
    # remove provably dead-parented servers. This sweep runs even when no claude worker
    # was reaped this cycle, to clean up orphans left by earlier reaps.
    macos_killed = 0
    for mp in macos_mcp:
        pp = mp["ppid"]
        if pp in killed_pids:
            pass  # its worker just died -> orphan-to-be, take it out now
        elif (pp <= 1 or pp not in live_pids) and mp["age"] >= max_age:
            pass  # already orphaned + stale
        else:
            continue
        if dry or kill(mp["pid"]):
            macos_killed += 1

    mode = "queue" if inflight is not None else "age-fallback"
    leaked_groups = sum(1 for g in groups.values() if len(g) > 1)

    # Always persist the cycle outcome + always emit ONE structured marker, even on
    # the common no-leak path. Two reasons this replaced the old silent early-return:
    #   * The reaper is a separate launchd job; without a per-cycle heartbeat there is
    #     no way to tell "reaper ran and found nothing" from "reaper is dead/stuck".
    #   * `unparsed_worker_procs > 0` on a quiet cycle is the EARLY WARNING that the
    #     worker signature has drifted (Karol's blind spot) — it must be visible even
    #     when we killed nothing, precisely because we killed nothing.
    status = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "dry_run": bool(dry),
        "mode": mode,
        "inflight": inflight,
        "ceiling_sec": max_age,
        "max_group": max_group,
        "claim_grace_sec": claim_grace,
        "leaked_groups": leaked_groups,
        "claude_killed": killed,
        "disclaimer_killed": disclaimers,
        "macos_mcp_killed": macos_killed,
        "spared_claim_pids": sorted(claim_pids),
        "worker_probe_seen": stats["worker_probe_seen"],
        "reapable_workers": stats["reapable_workers"],
        "unparsed_worker_procs": stats["unparsed_worker_procs"],
        "macos_mcp_seen": stats["macos_mcp_seen"],
        "total_procs": stats["total_procs"],
        "ps_timed_out": stats["ps_timed_out"],
        "snapshot_empty": stats["snapshot_empty"],
    }
    write_status(status)

    prefix = "[claude-reaper]" + (" DRY-RUN" if dry else "")
    print(
        f"{prefix} cycle mode={mode} inflight={inflight} ceiling={max_age}s"
        f" worker_seen={stats['worker_probe_seen']} reapable={stats['reapable_workers']}"
        f" unparsed={stats['unparsed_worker_procs']} leaked_groups={leaked_groups}"
        f" mcp_seen={stats['macos_mcp_seen']} killed={killed}"
        f" disclaimer_killed={disclaimers} mcp_killed={macos_killed}"
        f" ps_timeout={int(stats['ps_timed_out'])} empty={int(stats['snapshot_empty'])}"
        f" max_group={max_group} claim_grace={claim_grace}s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never let the reaper itself crash the launchd job loudly
        print(f"[claude-reaper] error: {e}", file=sys.stderr)
        sys.exit(0)
