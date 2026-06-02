#!/usr/bin/env python3
"""
sweep_post_link_clicks.py — behavioral bot-flagger for short-link click logs.

Runs in addition to the per-hit UA regex in @m13v/seo-components. The UA
regex catches obvious crawlers; this sweep catches everything that looks
human in isolation but stops looking human when you correlate hits across
ip_hash + code + post + time.

All five rules + the R2 per-post excess loop + the counter rebuild now run
server-side in POST /api/v1/post-links/clicks-sweep (HTTP-only, 2026-06-01;
no DATABASE_URL on the operator box). This script is a thin trigger that
POSTs the flags and prints the returned before/flips/after/counter numbers.

Rules (all idempotent — re-running won't double-flag):

  Tier 1 (zero false positives):
    R1  same ip_hash + same code + >=3 hits in a 240s sliding window
    R2  clicks on a post exceed views * platform_ctr_ceiling
    R3  same ip_hash hits >=5 different codes within the window

  Tier 2 (very low false positives, applied after Tier 1):
    R4  no referrer + browser-looking UA + ip_hash co-occurs with bot rows
    R5  same ip_hash hits >=4 different codes within any 60-second window

Each flipped row records the rule in `bot_reason` so we can audit and roll
back per-rule if a false positive shows up. After flipping, the counter
post_links.clicks is rebuilt from the per-hit log so the dashboard matches.

Usage:
  scripts/sweep_post_link_clicks.py [--dry-run] [--lookback-hours N]
                                    [--rules R1,R2,R3,R4,R5]
                                    [--cron] [--rebuild-counter]

  --lookback-hours N   only consider clicks newer than N hours (default 720
                       on first/manual run, 6 in --cron mode)
  --cron               quick-sweep mode: 6h lookback, decrement-only counter
  --rebuild-counter    full SUM(NOT is_bot) rebuild of post_links.clicks

Idempotent: only flips rows where is_bot=false today; never un-flips.
"""

from __future__ import annotations

import argparse
import os
import sys

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_DIR, "scripts"))

from http_api import api_post, load_env  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--lookback-hours", type=int, default=None)
    ap.add_argument("--cron", action="store_true",
                    help="quick-sweep mode: 6h lookback, decrement-only counter update")
    ap.add_argument("--rebuild-counter", action="store_true",
                    help="full counter rebuild from is_bot=false rows (safe, idempotent)")
    ap.add_argument("--rules", default="R1,R2,R3,R4,R5",
                    help="comma-separated rule list, default all five")
    args = ap.parse_args()

    load_env()

    rules = [r.strip().upper() for r in args.rules.split(",") if r.strip()]
    body = {
        "dry_run": args.dry_run,
        "cron": args.cron,
        "rebuild_counter": args.rebuild_counter,
        "rules": rules,
    }
    if args.lookback_hours is not None:
        body["lookback_hours"] = int(args.lookback_hours)

    resp = api_post("/api/v1/post-links/clicks-sweep", body)
    data = resp.get("data") or {}

    window = data.get("window_hours")
    before = data.get("before") or {}
    after = data.get("after") or {}
    flips = data.get("flips") or {}
    counter = data.get("counter") or {}

    print(f"[before] window={window}h humans={before.get('humans')} "
          f"bots={before.get('bots')} total={before.get('total')}", flush=True)
    print("[flips]", " ".join(f"{k}={flips[k]}" for k in sorted(flips)), flush=True)
    print(f"[after]  window={window}h humans={after.get('humans')} "
          f"bots={after.get('bots')} total={after.get('total')}", flush=True)

    mode = counter.get("mode")
    if mode == "dry-run":
        print(f"[counter] dry-run: would change SUM by ~{counter.get('would_change_sum')}; "
              f"humans-total now {counter.get('humans_total')}", flush=True)
    elif mode == "cron":
        print(f"[counter] cron-mode: rebuilt counters for codes touching "
              f"{counter.get('flagged_rows_touched')} flagged rows", flush=True)
    elif mode == "full-rebuild":
        print(f"[counter] full rebuild done; SUM(post_links.clicks) now = "
              f"{counter.get('sum_after')}", flush=True)


if __name__ == "__main__":
    main()
