#!/usr/bin/env python3
"""Pick the next (project, topic_angle) pair for an original Twitter thread.

Mirrors scripts/pick_thread_target.py (Reddit), adapted for Twitter:

Differences vs the Reddit picker:
- No subreddit dimension. The natural floor unit is (project, topic_angle).
- Hard global daily cap. Across all projects, never post more than
  TWITTER_DAILY_CAP original threads in a UTC calendar day. Enforced via a
  COUNT(*) of posts where platform='twitter' AND thread_url=our_url AND
  posted_at::date = CURRENT_DATE. If hit, exit non-zero so the orchestrator
  cleanly skips the launchd fire.
- Per-project per-angle floor window (twitter_threads.topic_floor_days,
  default 2). Picks an angle that is either never-used or older than the
  floor for the given project.
- Project weight + inverse recent-share weighting (same as Reddit picker)
  so we don't pile every fire on one project.

Usage:
  python3 scripts/pick_twitter_thread_target.py              # PROJECT\tANGLE
  python3 scripts/pick_twitter_thread_target.py --json       # full context
  python3 scripts/pick_twitter_thread_target.py --show-all   # debug view
"""

import argparse
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get  # noqa: E402

# THE canonical config loader (scripts/config.py): S4L_CONFIG_PATH / state-dir /
# S4L_REPO_DIR aware, mtime-cached. Replaces this file's hand-rolled loader and
# its hardcoded config path (the S4L-4H dead-path class on customer boxes).
import os as _cfg_os, sys as _cfg_sys
_cfg_sys.path.insert(0, _cfg_os.path.dirname(_cfg_os.path.abspath(__file__)))
from config import config_path as _canonical_config_path, load_config
CONFIG_PATH = _canonical_config_path()
DEFAULT_TOPIC_FLOOR_DAYS = 2
TWITTER_DAILY_CAP = 3      # hard global cap. user requirement, do not raise without explicit ask.




def _fetch_picker_context(angle_window_days=14, counts_window_days=7):
    """Single call to /api/v1/twitter/picker-context for all three reads.

    Replaces the previous trio of direct-DB SELECTs (daily_count_today,
    recent_angles_by_project, recent_posts_by_project) with one HTTP roundtrip.
    Returned by the route already trimmed to the same row shape we used to
    derive in Python: { daily_count_today: int, recent_posts_by_project:
    {name: int}, project_angles: {name: [{summary, days_ago}, ...]} }.
    """
    resp = api_get(
        "/api/v1/twitter/picker-context",
        query={
            "angle_window_days": angle_window_days,
            "counts_window_days": counts_window_days,
        },
    )
    return resp.get("data") or {}


def recent_angles_by_project(project_angles_payload):
    """Reshape the route's project_angles dict into the same {project: {_rows: [...]}}
    structure the old direct-DB helper produced, so angle_recency() works
    unchanged.
    """
    out = {}
    for project_name, rows in (project_angles_payload or {}).items():
        bucket = out.setdefault(project_name, {})
        bucket.setdefault("_rows", []).extend(
            (r.get("summary") or "", float(r.get("days_ago") or 0)) for r in rows
        )
    return out


def angle_recency(project_recents, angle_text):
    """Given project_recents[project] (a dict with '_rows' list of
    (summary, days_ago)), return the smallest days_ago for any row whose
    summary contains the first 60 chars of angle_text. None if never used.
    """
    rows = (project_recents or {}).get("_rows") or []
    needle = (angle_text or "").strip()[:60].lower()
    if not needle:
        return None
    best = None
    for summary, days_ago in rows:
        if needle in (summary or "").lower():
            if best is None or days_ago < best:
                best = days_ago
    return best


def build_candidates(config, project_recents):
    candidates = []   # (project_dict, angle_text, floor_days, last_used_days_ago_or_None)
    for p in config.get("projects", []):
        tt = p.get("twitter_threads") or {}
        if not tt.get("enabled"):
            continue
        floor = int(tt.get("topic_floor_days", DEFAULT_TOPIC_FLOOR_DAYS))
        angles = tt.get("topic_angles") or []
        if not angles:
            continue
        recents_for_proj = project_recents.get(p["name"], {})
        for angle in angles:
            last = angle_recency(recents_for_proj, angle)
            if last is not None and last < floor:
                continue  # too recent
            candidates.append((p, angle, floor, last))
    return candidates, project_recents


def pick(candidates, recent_project_counts=None):
    if not candidates:
        return None
    recent_project_counts = recent_project_counts or {}
    by_project = {}
    for p, angle, floor, last in candidates:
        by_project.setdefault(p["name"], {"project": p, "entries": []})
        by_project[p["name"]]["entries"].append((angle, floor, last))
    names = list(by_project.keys())
    # Inverse recent-share: keep config weight as the prior, penalise projects
    # that already posted a lot in the last 7d.
    weights = [
        by_project[n]["project"].get("weight", 1)
        / (1 + recent_project_counts.get(n, 0))
        for n in names
    ]
    chosen_name = random.choices(names, weights=weights, k=1)[0]
    proj = by_project[chosen_name]["project"]
    angle, floor, last = random.choice(by_project[chosen_name]["entries"])
    return (proj, angle, floor, last)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--show-all", action="store_true")
    args = ap.parse_args()

    config = load_config()

    # One HTTP roundtrip for all picker context (daily count, recent angles
    # per project, recent post counts per project). Was three separate
    # psycopg2 SELECTs on `posts` before the 2026-05-18 routes migration.
    ctx = _fetch_picker_context(angle_window_days=14, counts_window_days=7)

    # Hard daily cap. Check FIRST so the picker exits cheap when the day is
    # already saturated.
    today_count = int(ctx.get("daily_count_today") or 0)
    if today_count >= TWITTER_DAILY_CAP and not args.show_all:
        print(f"DAILY_CAP_REACHED: {today_count}/{TWITTER_DAILY_CAP} posts today",
              file=sys.stderr)
        sys.exit(3)

    project_recents = recent_angles_by_project(ctx.get("project_angles"))
    candidates, _ = build_candidates(config, project_recents)
    recent_project_counts = ctx.get("recent_posts_by_project") or {}

    if args.show_all:
        print(f"Daily cap: {today_count}/{TWITTER_DAILY_CAP} posts today (UTC)")
        eligible_projects = {}
        for p, angle, floor, last in candidates:
            eligible_projects.setdefault(p["name"], p)
        print(f"\nProject weights (base / posts_7d / effective):")
        rows = []
        for name, p in eligible_projects.items():
            base = p.get("weight", 1)
            posts_7d = recent_project_counts.get(name, 0)
            eff = base / (1 + posts_7d)
            rows.append((name, base, posts_7d, eff))
        for name, base, posts_7d, eff in sorted(rows, key=lambda r: -r[3]):
            print(f"  {name:25} base={base:>3}  posts_7d={posts_7d:>2}  effective={eff:.3f}")
        print(f"\nEligible candidates: {len(candidates)}")
        for p, angle, floor, last in candidates:
            last_str = f"last={last:.2f}d" if last is not None else "last=never"
            angle_short = (angle[:70] + "...") if len(angle) > 73 else angle
            print(f"  {p['name']:20} floor={floor}d {last_str:14} {angle_short}")
        return

    choice = pick(candidates, recent_project_counts=recent_project_counts)
    if not choice:
        print("NO_ELIGIBLE_TARGET", file=sys.stderr)
        sys.exit(2)

    proj, angle, floor, last = choice
    if args.json:
        print(json.dumps({
            "project": proj,
            "topic_angle": angle,
            "floor_days": floor,
            "last_used_days_ago": last,
            "eligible_count": len(candidates),
            "daily_count_today": today_count,
            "daily_cap": TWITTER_DAILY_CAP,
        }, indent=2))
    else:
        print(f"{proj['name']}\t{angle}")


if __name__ == "__main__":
    main()
