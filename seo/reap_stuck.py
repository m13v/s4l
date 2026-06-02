#!/usr/bin/env python3
"""
Sweep rows stuck in transient states (scoring / in_progress) back to
runnable states. The SERP and GSC pipelines set these states before
invoking Claude or the generator; if the shell process is killed
(launchd timeout, SIGTERM, hung grep on /tmp FIFO, OOM), the row is
never written back and becomes invisible to the picker.

Runs at the start of each cron pass.

Rules (thresholds from --minutes, default 30):
  seo_keywords.scoring      older than N min → unscored
  seo_keywords.in_progress  older than N min → pending if score>=1.5 else unscored
  gsc_queries.in_progress   older than N min → pending

Usage:
  python3 reap_stuck.py                # default 30 min threshold
  python3 reap_stuck.py --minutes 0    # reset everything (one-shot orphan fix)
  python3 reap_stuck.py --dry-run      # show what would change
"""

import argparse
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)

sys.path.insert(0, os.path.join(ROOT_DIR, "scripts"))
from http_api import api_post, load_env  # noqa: E402

load_env()


def reap(minutes: int, dry_run: bool = False) -> int:
    """Return number of rows reset."""
    resp = api_post("/api/v1/seo/reap", {"minutes": int(minutes), "dry_run": bool(dry_run)})
    data = resp.get("data") or {}

    for r in data.get("scoring") or []:
        print(f"  [seo_keywords] scoring→unscored | age={float(r.get('age_min') or 0):.0f}min | "
              f"[{r.get('product')}] {str(r.get('keyword') or '')[:60]}")
    for r in data.get("in_progress") or []:
        print(f"  [seo_keywords] in_progress→{r.get('target')} | age={float(r.get('age_min') or 0):.0f}min | "
              f"score={r.get('score')} | [{r.get('product')}] {str(r.get('keyword') or '')[:60]}")
    for r in data.get("gsc") or []:
        print(f"  [gsc_queries] in_progress→pending | age={float(r.get('age_min') or 0):.0f}min | "
              f"[{r.get('product')}] {str(r.get('query') or '')[:60]}")

    return int(data.get("total") or 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=30,
                    help="Reap rows older than this many minutes (default: 30)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    n = reap(args.minutes, args.dry_run)
    prefix = "[reap_stuck] DRY-RUN: would reset" if args.dry_run else "[reap_stuck] reset"
    print(f"{prefix} {n} row(s) (threshold={args.minutes}min)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
