#!/usr/bin/env python3
"""Backfill subagent counts/cost onto historical ``claude_sessions`` rows.

Fast path (default): partition rows into
  (a) **bulk-zero** — rows whose transcript directory has no ``subagents/``
      sibling. These get task_call_count=0/subagent_count=0/cost=0 via one
      bulk UPDATE. 99% of rows fall here, so this is the speed win.
  (b) **per-row parse** — rows whose transcript directory DOES have a
      ``subagents/`` sibling. For these we import ``log_claude_session.
      parse_transcript`` and re-parse to get the real breakdown.

Optional: ``--rebind-cycles`` also walks ``skill/logs/*.log`` for BATCH_ID +
session_id pairs and stamps cycle_id where currently NULL. Historical logs
rarely contain session_ids in plaintext (they're stamped by the spawned
``claude`` process, not echoed), so this typically binds 0 rows for old
data, but it's harmless to run.

Why per-row parse can't be skipped entirely
-------------------------------------------
The presence of a ``subagents/`` sibling directory is signal that subagents
ran, but the actual cost has to come from parsing the per-subagent
transcripts. Once we've filtered to ~1% of rows the parse cost is tiny.

Idempotent. Re-running skips rows where ``subagent_count IS NOT NULL``
unless ``--force`` is passed.

Usage
-----
    python3 scripts/backfill_claude_session_subagents.py             # last 14d
    python3 scripts/backfill_claude_session_subagents.py --days 30
    python3 scripts/backfill_claude_session_subagents.py --rebind-cycles
    python3 scripts/backfill_claude_session_subagents.py --dry-run
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


def build_subagent_index():
    """Walk ~/.claude/projects/*/<uuid>/subagents/ and return set of session_ids
    that have any subagent transcripts on disk."""
    sids = set()
    for sub_dir in glob.glob(os.path.join(PROJECTS_ROOT, "*", "*", "subagents")):
        parent_uuid = os.path.basename(os.path.dirname(sub_dir))
        # Only count if there's at least one agent-*.jsonl inside
        if any(fn.endswith(".jsonl") for fn in os.listdir(sub_dir) if fn.startswith("agent-")):
            sids.add(parent_uuid)
    # Also check archive layout (skill/logs/claude-sessions/<date>/<HHMMSS>_<script>_<uuid>.jsonl)
    # Archived transcripts don't carry sibling subagents/, so we can't gain
    # signal there. The live ~/.claude/projects/ tree is authoritative.
    return sids


def find_transcript(session_id: str):
    """Locate transcript across Claude Code project dirs and our archive."""
    for proj in glob.glob(os.path.join(PROJECTS_ROOT, "*")):
        p = os.path.join(proj, f"{session_id}.jsonl")
        if os.path.exists(p):
            return p
    for arch in glob.glob(os.path.join(ARCHIVE_DIR, "*", f"*_{session_id}.jsonl")):
        return arch
    return None


def scan_cycles_from_logs(days: int):
    """Pair BATCH_ID lines with session uuids in log files.

    Historical reality: most pipeline logs DON'T contain session_ids in
    plaintext (the uuid is generated programmatically and passed to claude
    via flag, not echoed). So this typically binds zero rows for old data.
    Going forward, SA_CYCLE_ID is stamped directly by log_claude_session.py
    via env var inheritance, so the rebind path is mostly a safety net.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    bindings = {}

    batch_pats = [
        re.compile(r"BATCH_ID=([A-Za-z0-9_-]+)"),
        re.compile(r"batch_id=([A-Za-z0-9_-]+)"),
        re.compile(r"SA_CYCLE_ID=([A-Za-z0-9_-]+)"),
    ]
    session_pat = re.compile(r"--session-id\s+([0-9a-f-]{36})")

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
    return bindings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--script", help="Limit to one script name")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="Reparse even if subagent_count IS NOT NULL")
    ap.add_argument("--rebind-cycles", action="store_true")
    ap.add_argument("--no-bulk-zero", action="store_true",
                    help="Skip the bulk-zero UPDATE (debug)")
    args = ap.parse_args()

    dbmod.load_env()
    conn = dbmod.get_conn()
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()

    # Phase 1: build the index of session_ids that have subagent transcripts.
    print("[backfill] phase 1: scanning ~/.claude/projects/*/*/subagents/ ...", file=sys.stderr)
    t0 = time.time()
    subagent_sids = build_subagent_index()
    print(f"[backfill] phase 1: {len(subagent_sids)} sessions have subagent transcripts "
          f"(scan took {time.time()-t0:.1f}s)", file=sys.stderr)

    # Phase 2: bulk-zero rows that DON'T appear in subagent_sids and currently
    # have subagent_count IS NULL. One UPDATE statement, very fast.
    bulk_filter = ["started_at >= %s", "subagent_count IS NULL"]
    bulk_params = [cutoff_iso]
    if args.script:
        bulk_filter.append("script = %s")
        bulk_params.append(args.script)
    if subagent_sids:
        bulk_filter.append("session_id NOT IN %s")
        bulk_params.append(tuple(subagent_sids))
    bulk_sql = f"""
        UPDATE claude_sessions
        SET task_call_count = 0,
            subagent_count = 0,
            subagent_cost_usd = 0
        WHERE {' AND '.join(bulk_filter)}
    """
    if args.no_bulk_zero or args.dry_run:
        cur = conn.execute(
            f"SELECT COUNT(*) FROM claude_sessions WHERE {' AND '.join(bulk_filter)}",
            bulk_params,
        )
        bulk_count = cur.fetchone()[0]
        print(f"[backfill] phase 2 (dry-run): would bulk-zero {bulk_count} rows", file=sys.stderr)
    else:
        cur = conn.execute(bulk_sql, bulk_params)
        bulk_count = cur.rowcount
        conn.commit()
        print(f"[backfill] phase 2: bulk-zeroed {bulk_count} rows", file=sys.stderr)

    # Phase 3: per-row parse for sessions that DO have subagent transcripts.
    where = ["started_at >= %s", "session_id IN %s"]
    params = [cutoff_iso, tuple(subagent_sids) if subagent_sids else ("00000000-0000-0000-0000-000000000000",)]
    if args.script:
        where.append("script = %s")
        params.append(args.script)
    if not args.force:
        where.append("subagent_count IS NULL")
    cur = conn.execute(
        f"""SELECT session_id, script, cycle_id
            FROM claude_sessions
            WHERE {' AND '.join(where)}
            ORDER BY started_at DESC""",
        params,
    )
    rows = cur.fetchall()
    print(f"[backfill] phase 3: {len(rows)} rows to parse individually", file=sys.stderr)

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
        "phase1_subagent_sessions_on_disk": len(subagent_sids),
        "phase2_bulk_zeroed": bulk_count,
        "phase3_candidates": len(rows),
        "phase3_parsed": 0,
        "phase3_no_transcript": 0,
        "phase3_with_subagents": 0,
        "phase3_cycle_assigned": 0,
        "phase3_errors": 0,
    }

    for row in rows:
        if isinstance(row, dict):
            sid = row["session_id"]
            existing_cycle = row.get("cycle_id")
        else:
            sid, _script, existing_cycle = row

        transcript = find_transcript(sid)
        if not transcript:
            stats["phase3_no_transcript"] += 1
            continue

        try:
            parsed = lcs.parse_transcript(transcript)
        except Exception as e:
            stats["phase3_errors"] += 1
            print(f"[err] {sid}: parse failed: {e}", file=sys.stderr)
            continue

        sub_count = parsed.get("subagent_count", 0) or 0
        sub_cost = parsed.get("subagent_cost_usd", 0.0) or 0.0
        task_count = parsed.get("task_call_count", 0) or 0
        breakdown = parsed.get("subagent_breakdown") or {}
        breakdown_json = json.dumps(breakdown) if breakdown else None

        if sub_count > 0:
            stats["phase3_with_subagents"] += 1

        new_cycle = None
        if args.rebind_cycles and not existing_cycle and sid in cycle_bindings:
            new_cycle = cycle_bindings[sid]
            stats["phase3_cycle_assigned"] += 1

        if args.dry_run:
            stats["phase3_parsed"] += 1
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
                    [task_count, sub_count, round(sub_cost, 6),
                     breakdown_json, new_cycle, sid],
                )
            else:
                conn.execute(
                    """UPDATE claude_sessions
                       SET task_call_count = %s,
                           subagent_count = %s,
                           subagent_cost_usd = %s,
                           subagent_breakdown = %s::jsonb
                       WHERE session_id = %s""",
                    [task_count, sub_count, round(sub_cost, 6),
                     breakdown_json, sid],
                )
        except Exception as e:
            stats["phase3_errors"] += 1
            print(f"[err] {sid}: update failed: {e}", file=sys.stderr)
            continue
        stats["phase3_parsed"] += 1

    if not args.dry_run:
        conn.commit()
    conn.close()
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
