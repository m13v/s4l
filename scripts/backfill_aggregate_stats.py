#!/usr/bin/env python3
"""Backfill aggregate_stats_daily from the daily audit-pipeline log files.

Pre-2026-04-24 we had no per-post engagement snapshots, so
`post_views_daily` is empty for historical days. The audit pipeline
(skill/audit-*.sh → stats.py) happens to print one "Posts/Views/
Upvotes/Comments" cumulative-totals line at the end of each run. Parsing
7+ days of those logs and diffing consecutive days reconstructs the
aggregate daily gains for views/upvotes/comments across ALL platforms.

The cumulative-totals line is cross-platform, so reconstructed days
cannot be filtered by platform; the dashboard endpoints emit 0 when a
platform filter is active on an aggregate row.

Idempotent: UPSERTs by day, safe to re-run.

Usage:
    python3 scripts/backfill_aggregate_stats.py [--dry-run]
"""
import argparse
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod


LOG_DIR = os.path.expanduser("~/social-autoposter/skill/logs")
LOG_GLOBS = (
    "audit-reddit-*.log",
    "audit-twitter-*.log",
    "audit-linkedin-*.log",
    "audit-moltbook-*.log",
)
TOTALS_RE = re.compile(
    r"Posts:\s*(\d[\d,]*)\s*\|\s*Views:\s*(\d[\d,]*)\s*\|\s*"
    r"Upvotes:\s*(\d[\d,]*)\s*\|\s*Comments:\s*(\d[\d,]*)"
)
FNAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{6})")


def collect_daily_totals():
    """Walk every audit log and keep the latest-timestamp totals line per
    day. Returns a list of (day_iso, {posts,views,upvotes,comments}) tuples
    sorted ascending by day."""
    by_day = {}
    for pat in LOG_GLOBS:
        for path in glob.glob(os.path.join(LOG_DIR, pat)):
            m_fname = FNAME_RE.search(os.path.basename(path))
            if not m_fname:
                continue
            day, ts = m_fname.group(1), m_fname.group(2)
            try:
                with open(path, "r", errors="replace") as fh:
                    for line in fh:
                        m = TOTALS_RE.search(line)
                        if not m:
                            continue
                        rec = {
                            "posts": int(m.group(1).replace(",", "")),
                            "views": int(m.group(2).replace(",", "")),
                            "upvotes": int(m.group(3).replace(",", "")),
                            "comments": int(m.group(4).replace(",", "")),
                            "ts": ts,
                        }
                        prior = by_day.get(day)
                        if not prior or prior["ts"] < ts:
                            by_day[day] = rec
                        break
            except OSError as e:
                print(f"  skip {path}: {e}", file=sys.stderr)
    return sorted(by_day.items())


def diff_to_rows(daily):
    """Given sorted [(day, totals), ...] produce backfill rows.

    Day N's gain = clamp(totals[N] - totals[N-1], 0). The first day in
    the series has no prior baseline and is skipped, matching the
    post_views_daily LAG behavior (first snapshot contributes nothing).
    """
    out = []
    prev = None
    for day, rec in daily:
        if prev is not None:
            out.append({
                "day": day,
                "views_gained":    max(0, rec["views"]    - prev["views"]),
                "upvotes_gained":  max(0, rec["upvotes"]  - prev["upvotes"]),
                "comments_gained": max(0, rec["comments"] - prev["comments"]),
            })
        prev = rec
    return out


def upsert_rows(rows, dry_run=False):
    if dry_run:
        for r in rows:
            print(f"  {r['day']}  views={r['views_gained']:>7}  "
                  f"upvotes={r['upvotes_gained']:>5}  comments={r['comments_gained']:>5}")
        return len(rows)
    dbmod.load_env()
    conn = dbmod.get_conn()
    try:
        for r in rows:
            conn.execute(
                "INSERT INTO aggregate_stats_daily "
                "(day, views_gained, upvotes_gained, comments_gained, source) "
                "VALUES (%s, %s, %s, %s, 'audit_log_backfill') "
                "ON CONFLICT (day) DO UPDATE SET "
                "  views_gained = EXCLUDED.views_gained, "
                "  upvotes_gained = EXCLUDED.upvotes_gained, "
                "  comments_gained = EXCLUDED.comments_gained, "
                "  source = EXCLUDED.source",
                [r["day"], r["views_gained"], r["upvotes_gained"], r["comments_gained"]],
            )
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    daily = collect_daily_totals()
    if not daily:
        print("No audit logs with totals line found.", file=sys.stderr)
        sys.exit(1)
    print(f"Parsed {len(daily)} daily snapshots: {daily[0][0]} -> {daily[-1][0]}")
    rows = diff_to_rows(daily)
    print(f"Will UPSERT {len(rows)} rows into aggregate_stats_daily"
          + (" (dry-run)" if args.dry_run else ""))
    n = upsert_rows(rows, dry_run=args.dry_run)
    print(f"Done ({n} rows).")


if __name__ == "__main__":
    main()
