#!/usr/bin/env python3
"""Query Postgres for SEO run stats and log to run_monitor.log.

Called from EXIT traps in cron_seo.sh and cron_gsc.sh to report
actual pages produced and Claude cost instead of hardcoded zeros.

Usage:
    python3 seo/log_seo_run.py --script serp_seo --since <unix_ts> --failed <exit_code> --elapsed <secs>
    python3 seo/log_seo_run.py --script gsc_seo  --since <unix_ts> --failed <exit_code> --elapsed <secs>
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(ROOT_DIR, "scripts"))
from http_api import api_get, load_env  # noqa: E402


SUPPORTED_SCRIPTS = [
    'serp_seo',
    'gsc_seo',
    'seo_improve',
    'seo_top_pages',
    'seo_top_posts',
    'seo_weekly_roundup',
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--script', required=True, choices=SUPPORTED_SCRIPTS)
    parser.add_argument('--since', type=int, required=True, help='Unix timestamp of run start')
    parser.add_argument('--failed', type=int, default=0, help='Shell exit code of the wrapping script')
    parser.add_argument('--elapsed', type=float, default=0.0)
    # When the wrapper already counts skips in-shell (e.g. seo_top_posts iterates
    # 15 projects and most early-exit before staking a seo_keywords row), it can
    # pass --skipped-override so run_monitor.log shows the real number instead
    # of zero from an empty DB query.
    parser.add_argument('--skipped-override', type=int, default=None)
    parser.add_argument('--posted-override', type=int, default=None)
    args = parser.parse_args()

    run_start_ts = datetime.fromtimestamp(args.since, tz=timezone.utc).isoformat()

    pages = 0
    skipped = 0
    db_failed = 0
    cost = 0.0

    load_env()
    try:
        resp = api_get("/api/v1/seo/run-stats",
                       query={"script": args.script, "since": run_start_ts})
        data = resp.get("data") or {}
        pages = int(data.get("posted") or 0)
        skipped = int(data.get("skipped") or 0)
        db_failed = int(data.get("failed") or 0)
        cost = float(data.get("cost") or 0)
    except Exception as e:
        print(f'[log_seo_run] run-stats query failed: {e}', file=sys.stderr)

    # Sum content-level failures (from DB) with shell-level failure (non-zero
    # exit from the wrapping script). If the script crashed outright, db_failed
    # is usually 0 but we still want the row flagged.
    total_failed = db_failed + (1 if args.failed else 0)

    # Honor wrapper-supplied overrides when the DB query can't see the work
    # (e.g. seo_top_posts skips early before staking a row).
    final_skipped = args.skipped_override if args.skipped_override is not None else skipped
    final_pages = args.posted_override if args.posted_override is not None else pages

    log_run = os.path.join(ROOT_DIR, 'scripts', 'log_run.py')
    subprocess.run(
        [
            sys.executable, log_run,
            '--script', args.script,
            '--posted', str(final_pages),
            '--skipped', str(final_skipped),
            '--failed', str(total_failed),
            '--cost', f'{cost:.4f}',
            '--elapsed', str(int(args.elapsed)),
        ],
        capture_output=True,
    )


if __name__ == '__main__':
    main()
