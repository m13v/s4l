#!/usr/bin/env python3
"""Backfill subagent counts/cost (and cycle_id where derivable) onto
historical ``claude_sessions`` rows.

Why
---
Until 2026-05-10 we only logged the orchestrator transcript and rolled
subagent costs into ``total_cost_usd``. Now ``log_claude_session.py`` scans
the sibling ``subagents/`` directory and breaks costs down into the new
columns:

  * ``task_call_count``    -> orchestrator's count of Agent tool_use blocks
  * ``subagent_count``     -> distinct subagent transcripts discovered
  * ``subagent_cost_usd``  -> sum of subagent token cost (above and beyond
                             ``total_cost_usd``, which only covers the
                             orchestrator thread)
  * ``subagent_breakdown`` -> jsonb per-subagent detail (model, role, cost)

For historical rows those columns are NULL. This script re-parses every
transcript and back-fills the four columns plus ``model_breakdown``,
``cache_*_tokens``, etc. via the existing ``log_claude_session.py`` ON
CONFLICT DO UPDATE path. (Reusing that path keeps one parser as the source
of truth.)

cycle_id backfill: for rows where ``cycle_id`` is NULL we scan the daily
``skill/logs/run-*.log`` and ``skill/logs/engage-*.log`` files for the
``BATCH_ID=...`` line emitted at cycle start and the ``--session-id <uuid>``
spawned within that cycle's wall-clock window. Best-effort; rows that we
can't bind stay NULL.

Usage
-----
    # default: last 14 days
    python3 scripts/backfill_claude_session_subagents.py

    # custom window
    python3 scripts/backfill_claude_session_subagents.py --days 30

    # specific script only
    python3 scripts/backfill_claude_session_subagents.py --script post_reddit

    # dry-run (no writes)
    python3 scripts/backfill_claude_session_subagents.py --dry-run

    # also rebind cycle_id (slow; reads many log files)
    python3 scripts/backfill_claude_session_subagents.py --rebind-cycles

Idempotent: re-running is safe. The ON CONFLICT DO UPDATE in
``log_claude_session.py`` preserves a non-NULL cycle_id once written.
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod

REPO_DIR = os.path.expanduser("~/social-autoposter")
SCRIPTS_DIR = os.path.join(REPO_DIR, "scripts")
LOG_DIR = os.path.join(REPO_DIR, "skill", "logs")
ARCHIVE_DIR = os.path.join(LOG_DIR, "claude-sessions")
PROJECTS_ROOT = os.path.expanduser("~/.claude/projects")
LOG_CLAUDE = os.path.join(SCRIPTS_DIR, "log_claude_session.py")


def find_transcript(session_id: str):
    """Locate transcript across Claude Code's project dirs and our archive."""
    # Live Claude Code projects
    for proj in glob.glob(os.path.join(PROJECTS_ROOT, "*")):
        p = os.path.join(proj, f"{session_id}.jsonl")
        if os.path.exists(p):
            return p
    # Our archive: skill/logs/claude-sessions/<date>/<HHMMSS>_<script>_<uuid>.jsonl
    for arch in glob.glob(os.path.join(ARCHIVE_DIR, "*", f"*_{session_id}.jsonl")):
        return arch
    return None


def scan_cycles_from_logs(days: int):
    """Walk skill/logs/*.log for BATCH_ID + --session-id pairs.

    Returns dict: session_id -> (batch_id, log_path).

    Format expected (best-effort, varies by script):
      BATCH_ID stamped at top:    "Cycle batch_id=rdcycle-20260510-133005"
                                  or "BATCH_ID=enrdt-20260510-120000"
                                  or just exported in env then echoed.
      session_id stamped:         "--session-id 8d421694-..."  in the same log

    We pair: every claude session uuid in a log file inherits the BATCH_ID
    of that file. One log file = one cycle (by construction; the BATCH_ID
    is set once at the top of each *.sh script).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    bindings = {}

    # Patterns covering all current script flavors.
    batch_pats = [
        re.compile(r"BATCH_ID=([A-Za-z0-9_-]+)"),
        re.compile(r"batch_id=([A-Za-z0-9_-]+)"),
        re.compile(r"SA_CYCLE_ID=([A-Za-z0-9_-]+)"),
    ]
    session_pat = re.compile(r"--session-id\s+([0-9a-f-]{36})")
    # Also pick up "session_id=<uuid>" / "Claude session: <uuid>" / "CLAUDE_SESSION_ID=<uuid>"
    session_alt = re.compile(
        r"(?:session[_-]id[=:]\s*|CLAUDE_SESSION_ID=)([0-9a-f-]{36})", re.IGNORECASE
    )

    for log_path in glob.glob(os.path.join(LOG_DIR, "*.log")):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(log_path), tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            continue
        try:
            with open(log_path, errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        batch_id = None
        for pat in batch_pats:
            m = pat.search(text)
            if m:
                batch_id = m.group(1)
                break
        if not batch_id:
            continue
        for sid in session_pat.findall(text):
            # First binding wins; later cycles overwriting earlier sessions
            # in the same file would be a bug in the log itself.
            bindings.setdefault(sid, (batch_id, log_path))
        for sid in session_alt.findall(text):
            bindings.setdefault(sid, (batch_id, log_path))
    return bindings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14, help="Window of sessions to backfill")
    ap.add_argument("--script", help="Limit to one script name (e.g. post_reddit)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--rebind-cycles",
        action="store_true",
        help="Also scan logs to assign cycle_id where currently NULL.",
    )
    ap.add_argument("--limit", type=int, default=0, help="Stop after N rows (debug)")
    args = ap.parse_args()

    dbmod.load_env()
    conn = dbmod.get_conn()
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    where = ["started_at >= %s"]
    params = [cutoff_iso]
    if args.script:
        where.append("script = %s")
        params.append(args.script)
    cur = conn.execute(
        f"""SELECT session_id, script, started_at, ended_at, cycle_id,
                   subagent_count, subagent_cost_usd, task_call_count
            FROM claude_sessions
            WHERE {' AND '.join(where)}
            ORDER BY started_at DESC""",
        params,
    )
    rows = cur.fetchall()
    print(f"[backfill] candidate rows: {len(rows)} (last {args.days}d)", file=sys.stderr)

    cycle_bindings = {}
    if args.rebind_cycles:
        print(f"[backfill] scanning logs for cycle bindings ...", file=sys.stderr)
        cycle_bindings = scan_cycles_from_logs(args.days)
        print(
            f"[backfill] discovered {len(cycle_bindings)} session->batch bindings",
            file=sys.stderr,
        )

    stats = {
        "total": len(rows),
        "reparsed": 0,
        "no_transcript": 0,
        "subagent_added": 0,
        "cycle_added": 0,
        "skipped_already_subagent": 0,
        "skipped_already_cycle": 0,
        "errors": 0,
    }

    processed = 0
    for row in rows:
        # row is psycopg2 RealDictRow-like or tuple depending on db wrapper
        if isinstance(row, dict):
            sid = row["session_id"]
            script = row["script"]
            started = row["started_at"]
            ended = row["ended_at"]
            existing_cycle = row.get("cycle_id")
            existing_sub_count = row.get("subagent_count")
        else:
            sid, script, started, ended, existing_cycle, existing_sub_count, _sc, _tc = row

        transcript = find_transcript(sid)
        if not transcript:
            stats["no_transcript"] += 1
            continue

        # Decide whether to re-parse for subagents (skip if already counted AND
        # not asking for cycle rebinding).
        need_subagent = existing_sub_count is None
        need_cycle = args.rebind_cycles and not existing_cycle and sid in cycle_bindings
        if not need_subagent and not need_cycle:
            stats["skipped_already_subagent"] += 1
            continue

        env = os.environ.copy()
        if need_cycle:
            env["SA_CYCLE_ID"] = cycle_bindings[sid][0]
        # Otherwise let log_claude_session.py preserve existing cycle_id via
        # COALESCE in its ON CONFLICT DO UPDATE clause.

        cmd = [
            "python3",
            LOG_CLAUDE,
            "--session-id",
            sid,
            "--script",
            script,
        ]
        if started:
            cmd += ["--started-at", str(started).replace(" ", "T")]
        if ended:
            cmd += ["--ended-at", str(ended).replace(" ", "T")]

        if args.dry_run:
            print(f"[dry-run] would re-log {sid} script={script} cycle={env.get('SA_CYCLE_ID','-')}")
            continue

        try:
            r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                stats["errors"] += 1
                print(f"[err] {sid}: rc={r.returncode} stderr={r.stderr[:200]}", file=sys.stderr)
                continue
            stats["reparsed"] += 1
            # Try to parse JSON to learn what we actually wrote.
            try:
                out = json.loads(r.stdout.strip().splitlines()[-1])
                if out.get("subagent_count", 0) > 0 and existing_sub_count is None:
                    stats["subagent_added"] += 1
                if need_cycle and out.get("cycle_id"):
                    stats["cycle_added"] += 1
            except (json.JSONDecodeError, IndexError):
                pass
        except subprocess.TimeoutExpired:
            stats["errors"] += 1
            print(f"[err] {sid}: timeout", file=sys.stderr)
            continue

        processed += 1
        if processed % 25 == 0:
            print(
                f"[backfill] progress: {processed}/{len(rows)} reparsed={stats['reparsed']} subagent_added={stats['subagent_added']} cycle_added={stats['cycle_added']}",
                file=sys.stderr,
            )
        if args.limit and processed >= args.limit:
            break

    conn.close()
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
