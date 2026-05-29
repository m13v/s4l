#!/usr/bin/env python3
"""Query claude_sessions to get total cost for a pipeline cycle.

Two modes:

1) Cycle mode (preferred): `--cycle-id rdcycle-...`
   Sums orchestrator_cost_usd (native SDK billing) for every row stamped with
   this cycle_id. This is the authoritative Anthropic bill, NOT the inflated
   transcript-derived estimate (total_cost_usd) we used to report. Accurate
   even when multiple cycles of the same script overlap in wall-clock time
   (run-reddit-search.sh, run-twitter-cycle.sh double-fork their work so
   stacked cycles are normal).

2) Legacy time-window mode: `--since <unix_ts> --scripts tag1 tag2 ...`
   Filters by script + started_at. Kept for backward compatibility with
   callers that don't pass cycle_id (older pipelines, historical reports).
   IMPORTANT: this mode over-counts when multiple cycles of the same script
   overlap, because it has no way to distinguish them. Migrate callers to
   --cycle-id when possible.

Either mode is acceptable; --cycle-id wins if both are passed. Prints the
total cost as a float (4 decimal places), or 0.0000 on any error.
Designed to be called from shell script EXIT traps to get real cost per run.
"""
import argparse
import os
import sys
from datetime import datetime, timezone

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_env():
    env_path = os.path.join(ROOT_DIR, '.env')
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--cycle-id', default=None,
                   help='Pipeline cycle batch id (e.g. rdcycle-20260510-110005). '
                        'Sums cost across every claude_sessions row stamped with '
                        'this id. Wins over --since/--scripts when both passed.')
    p.add_argument('--since', type=int, default=None,
                   help='Unix timestamp of run start (legacy mode; use cycle-id '
                        'instead when possible).')
    p.add_argument('--scripts', nargs='*', default=None,
                   help='claude_sessions.script values to sum (legacy mode).')
    p.add_argument('--breakdown', action='store_true',
                   help='Print "parent_cost subagent_cost task_count subagent_count" '
                        'instead of just the parent total. Useful when investigating '
                        'whether Task() subagents are inflating cost.')
    args = p.parse_args()

    _load_env()

    # Resolve which mode we're running in. Cycle id is authoritative if
    # given (and non-empty). Otherwise require the legacy pair.
    cycle_id = args.cycle_id.strip() if args.cycle_id else None
    if not cycle_id and (args.since is None or not args.scripts):
        # Bash EXIT traps shell out blind; keep the contract simple: any
        # missing-arg condition prints 0.0000 and exits 0 so the caller
        # never crashes its log_run.py emit on a malformed cost call.
        print("0.0000")
        return

    try:
        sys.path.insert(0, os.path.join(ROOT_DIR, 'scripts'))
        if os.environ.get('SOCIAL_AUTOPOSTER_LEGACY_NEON') == '1':
            parent_cost, subagent_cost, task_count, subagent_count = _fetch_via_db(
                cycle_id=cycle_id, since=args.since, scripts=args.scripts,
            )
        else:
            parent_cost, subagent_cost, task_count, subagent_count = _fetch_via_api(
                cycle_id=cycle_id, since=args.since, scripts=args.scripts,
            )
        if args.breakdown:
            print(f"{parent_cost:.4f} {subagent_cost:.4f} {task_count} {subagent_count}")
        else:
            print(f"{parent_cost:.4f}")
    except Exception:
        print("0.0000")


def _fetch_via_api(*, cycle_id, since, scripts):
    from http_api import api_get
    if cycle_id:
        query = {"cycle_id": cycle_id}
    else:
        query = {"since_ts": str(int(since)), "scripts": ",".join(scripts)}
    resp = api_get("/api/v1/claude-sessions/cost", query=query)
    data = (resp or {}).get("data") or {}
    return (
        float(data.get("parent_cost") or 0),
        float(data.get("subagent_cost") or 0),
        int(data.get("task_count") or 0),
        int(data.get("subagent_count") or 0),
    )


def _fetch_via_db(*, cycle_id, since, scripts):
    import psycopg2  # noqa: F401
    import db as dbmod
    conn = dbmod.get_conn()
    if cycle_id:
        cur = conn.execute(
            """SELECT COALESCE(SUM(orchestrator_cost_usd), 0),
                      COALESCE(SUM(subagent_cost_usd), 0),
                      COALESCE(SUM(task_call_count), 0),
                      COALESCE(SUM(subagent_count), 0)
               FROM claude_sessions
               WHERE cycle_id = %s""",
            [cycle_id],
        )
    else:
        since_ts = datetime.fromtimestamp(since, tz=timezone.utc).isoformat()
        placeholders = ','.join(['%s'] * len(scripts))
        cur = conn.execute(
            f"""SELECT COALESCE(SUM(orchestrator_cost_usd), 0),
                       COALESCE(SUM(subagent_cost_usd), 0),
                       COALESCE(SUM(task_call_count), 0),
                       COALESCE(SUM(subagent_count), 0)
                FROM claude_sessions
                WHERE script IN ({placeholders}) AND started_at >= %s""",
            list(scripts) + [since_ts],
        )
    row = cur.fetchone()
    return (
        float(row[0] or 0),
        float(row[1] or 0),
        int(row[2] or 0),
        int(row[3] or 0),
    )


if __name__ == '__main__':
    main()
