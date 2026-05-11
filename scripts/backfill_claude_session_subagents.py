#!/usr/bin/env python3
"""Backfill subagent counts/cost (and optionally cycle_id) onto historical
``claude_sessions`` rows.

Fast in-process implementation: imports the parser from
``log_claude_session.py`` and runs it directly against transcripts, instead
of spawning a subprocess per row (27k rows in a 14-day window makes the
subprocess path take 7+ hours).

What gets updated per row
-------------------------
  * ``task_call_count``      (orchestrator's count of Agent tool_use blocks)
  * ``subagent_count``       (distinct subagent transcripts discovered)
  * ``subagent_cost_usd``    (sum of subagent token cost)
  * ``subagent_breakdown``   (jsonb per-subagent detail)
  * ``cycle_id``             (only when --rebind-cycles is set and a log
                              binding can be found; preserved otherwise)

Token totals and ``total_cost_usd`` are intentionally NOT rewritten by the
backfill: the original row was computed at session-end and is authoritative
for those columns. We only fill in the subagent breakdown that was missing.

Usage
-----
    # default: last 14 days, subagent backfill only
    python3 scripts/backfill_claude_session_subagents.py

    # also bind cycle_id from log files (slower; scans skill/logs/)
    python3 scripts/backfill_claude_session_subagents.py --rebind-cycles

    # limit window or script
    python3 scripts/backfill_claude_session_subagents.py --days 30 --script post_reddit

    # dry run
    python3 scripts/backfill_claude_session_subagents.py --dry-run

Idempotent. Re-running skips rows where ``subagent_count IS NOT NULL`` (unless
``--force`` is passed).
"""

import argparse
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod
import log_claude_session as lcs

REPO_DIR = os.path.expanduser("~/social-autoposter")
LOG_DIR = os.path.join(REPO_DIR, "skill", "logs")
ARCHIVE_DIR = os.path.join(LOG_DIR, "claude-sessions")
PROJECTS_ROOT = os.path.expanduser("~/.claude/projects")


def find_transcript(session_id: str):
    """Locate transcript across Claude Code's project dirs and our archive."""
    for proj in glob.glob(os.path.join(PROJECTS_ROOT, "*")):
        p = os.path.join(proj, f"{session_id}.jsonl")
        if os.path.exists(p):
            return p
    for arch in glob.glob(os.path.join(ARCHIVE_DIR, "*", f"*_{session_id}.jsonl")):
        return arch
    return None


def scan_cycles_from_logs(days: int):
    """Pair BATCH_ID lines with session uuids inside the same log file.

    Returns dict: session_id -> batch_id.

    Patterns covered (all SA_CYCLE_ID-emitting scripts produce at least one):
      - "BATCH_ID=<id>"
      - "batch_id=<id>"  (lowercase, used in some echo lines)
      - "SA_CYCLE_ID=<id>"
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    bindings = {}

    batch_pats = [
        re.compile(r"BATCH_ID=([A-Za-z0-9_-]+)"),
        re.compile(r"batch_id=([A-Za-z0-9_-]+)"),
        re.compile(r"SA_CYCLE_ID=([A-Za-z0-9_-]+)"),
    ]
    session_pat = re.compile(r"--session-id\s+([0-9a-f-]{36})")
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
            bindings.setdefault(sid, batch_id)
        for sid in session_alt.findall(text):
            bindings.setdefault(sid, batch_id)
    return bindings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--script", help="Limit to one script name")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="Reparse even if subagent_count IS NOT NULL")
    ap.add_argument("--rebind-cycles", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--commit-every", type=int, default=200)
    args = ap.parse_args()

    dbmod.load_env()
    conn = dbmod.get_conn()
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    where = ["started_at >= %s"]
    params = [cutoff_iso]
    if args.script:
        where.append("script = %s")
        params.append(args.script)
    if not args.force:
        where.append("subagent_count IS NULL")
    cur = conn.execute(
        f"""SELECT session_id, script, cycle_id, subagent_count
            FROM claude_sessions
            WHERE {' AND '.join(where)}
            ORDER BY started_at DESC""",
        params,
    )
    rows = cur.fetchall()
    print(f"[backfill] candidates: {len(rows)} (last {args.days}d, force={args.force})", file=sys.stderr)

    cycle_bindings = {}
    if args.rebind_cycles:
        print("[backfill] scanning logs for cycle bindings ...", file=sys.stderr)
        t0 = time.time()
        cycle_bindings = scan_cycles_from_logs(args.days)
        print(
            f"[backfill] discovered {len(cycle_bindings)} session->batch bindings in {time.time()-t0:.1f}s",
            file=sys.stderr,
        )

    stats = {
        "total": len(rows),
        "reparsed": 0,
        "no_transcript": 0,
        "with_subagents": 0,
        "cycle_assigned": 0,
        "errors": 0,
    }

    started_walltime = time.time()
    pending = 0
    for row in rows:
        if isinstance(row, dict):
            sid = row["session_id"]
            existing_cycle = row.get("cycle_id")
        else:
            sid, _script, existing_cycle, _sc = row

        transcript = find_transcript(sid)
        if not transcript:
            stats["no_transcript"] += 1
            continue

        try:
            parsed = lcs.parse_transcript(transcript)
        except Exception as e:
            stats["errors"] += 1
            print(f"[err] {sid}: parse failed: {e}", file=sys.stderr)
            continue

        sub_count = parsed.get("subagent_count", 0) or 0
        sub_cost = parsed.get("subagent_cost_usd", 0.0) or 0.0
        task_count = parsed.get("task_call_count", 0) or 0
        breakdown = parsed.get("subagent_breakdown") or {}
        breakdown_json = json.dumps(breakdown) if breakdown else None

        if sub_count > 0:
            stats["with_subagents"] += 1

        new_cycle = None
        if args.rebind_cycles and not existing_cycle and sid in cycle_bindings:
            new_cycle = cycle_bindings[sid]
            stats["cycle_assigned"] += 1

        if args.dry_run:
            stats["reparsed"] += 1
            if args.limit and stats["reparsed"] >= args.limit:
                break
            continue

        try:
            if new_cycle:
                conn.execute(
                    """UPDATE claude_sessions
                       SET task_call_count = %s,
                           subagent_count = %s,
                           subagent_cost_usd = %s,
                           subagent_breakdown = %s::jsonb,
                           cycle_id = COALESCE(cycle_id, %s)
                       WHERE session_id = %s""",
                    [task_count, sub_count, round(sub_cost, 6), breakdown_json, new_cycle, sid],
                )
            else:
                conn.execute(
                    """UPDATE claude_sessions
                       SET task_call_count = %s,
                           subagent_count = %s,
                           subagent_cost_usd = %s,
                           subagent_breakdown = %s::jsonb
                       WHERE session_id = %s""",
                    [task_count, sub_count, round(sub_cost, 6), breakdown_json, sid],
                )
        except Exception as e:
            stats["errors"] += 1
            print(f"[err] {sid}: update failed: {e}", file=sys.stderr)
            continue

        stats["reparsed"] += 1
        pending += 1
        if pending >= args.commit_every:
            conn.commit()
            pending = 0
            elapsed = time.time() - started_walltime
            rate = stats["reparsed"] / elapsed if elapsed > 0 else 0
            eta = (len(rows) - stats["reparsed"]) / rate if rate > 0 else -1
            print(
                f"[backfill] {stats['reparsed']}/{len(rows)} reparsed "
                f"({rate:.1f}/s, eta {eta/60:.1f}min) "
                f"subagents={stats['with_subagents']} cycle_assigned={stats['cycle_assigned']}",
                file=sys.stderr,
            )

        if args.limit and stats["reparsed"] >= args.limit:
            break

    if pending:
        conn.commit()
    conn.close()
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
