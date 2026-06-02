#!/usr/bin/env python3
"""Pick the next (project, subreddit) pair for an original Reddit thread.

Rules:
- Only consider projects with threads.enabled=true.
- A project's own_community (if set) is a candidate every run (subject to its
  own floor_days override, default 1 day for own community).
- External subreddits are subject to the default 3-day floor (configurable via
  threads.external_floor_days).
- Entry filter: skip any subreddit where this account has posted an original
  thread (thread_url == our_url) within that sub's floor window.
- Also skip any subreddit listed in subreddit_bans.thread_blocked.
- Among eligible candidates, prefer own_community if present. Otherwise, weight
  projects by config weight.

Usage:
  python3 scripts/pick_thread_target.py              # stdout: PROJECT\tSUBREDDIT
  python3 scripts/pick_thread_target.py --json       # full context
  python3 scripts/pick_thread_target.py --show-all   # debug view
"""

import argparse
import json
import os
import random
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get

CONFIG_PATH = os.path.expanduser("~/social-autoposter/config.json")
DEFAULT_OWN_FLOOR_DAYS = 1
DEFAULT_EXTERNAL_FLOOR_DAYS = 3


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _parse_dt(s):
    """Parse an ISO posted_at string to an aware datetime, or None."""
    if not s:
        return None
    s = str(s)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fetch_own_reddit_threads(days):
    """Fetch our original Reddit threads (thread_url == our_url) posted in the
    last `days` days via the HTTP API. Returns a list of (thread_url, days_ago)
    tuples. Replaces the former direct posts SELECT (own_threads_only mirrors
    the thread_url = our_url predicate)."""
    cutoff = datetime.now(timezone.utc).timestamp() - int(days) * 86400
    since = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    resp = api_get(
        "/api/v1/posts",
        query={
            "platform": "reddit",
            "own_threads_only": "true",
            "since": since,
            "order_by": "posted_at",
            "order_dir": "desc",
            "limit": 500,
        },
    )
    posts = (resp.get("data") or {}).get("posts") or []
    now_ts = datetime.now(timezone.utc).timestamp()
    out = []
    for p in posts:
        dt = _parse_dt(p.get("posted_at"))
        if dt is None:
            continue
        days_ago = (now_ts - dt.timestamp()) / 86400.0
        out.append((p.get("thread_url"), days_ago, p.get("project_name")))
    return out


def norm_sub(s):
    if not s:
        return ""
    s = s.strip()
    if s.lower().startswith("r/"):
        s = s[2:]
    return s.lower()


def _ban_entry_to_slug(entry):
    """Extract the sub slug from a comment_blocked / thread_blocked entry.

    Entries are either bare strings (pre-2026-05-11) or audit dicts
    {"sub": ..., "added_at": ..., "reason": ..., "project": ...}.
    Returns lowercased slug (no r/ prefix) or empty string.
    """
    if isinstance(entry, str):
        return norm_sub(entry)
    if isinstance(entry, dict):
        return norm_sub(entry.get("sub") or "")
    return ""


def load_thread_blocked_subs(config):
    """Load subreddits where we cannot create new threads.

    Reads subreddit_bans.thread_blocked. For the thread-creation pipeline
    only, the comment pipeline uses subreddit_bans.comment_blocked via
    reddit_tools._load_comment_blocked_subs().

    Handles both ban-list shapes: bare string (pre-2026-05-11) and audit
    dict {"sub": ..., "added_at": ..., "reason": ..., "project": ...}.
    """
    bans = config.get("subreddit_bans") or {}
    out = set()
    if isinstance(bans, dict):
        for entry in bans.get("thread_blocked") or []:
            slug = _ban_entry_to_slug(entry)
            if slug:
                out.add(slug)
    elif isinstance(bans, list):
        # Legacy flat-list form, treat as thread_blocked.
        for entry in bans:
            slug = _ban_entry_to_slug(entry)
            if slug:
                out.add(slug)
    return out


def recent_posts_by_sub(max_days):
    """Return dict: sub_slug (lowercased) -> days_since_last_our_thread."""
    rows = _fetch_own_reddit_threads(max_days)
    latest = {}
    for url, days_ago, _project in rows:
        if not url or "/r/" not in url:
            continue
        sub = url.split("/r/", 1)[1].split("/", 1)[0].lower()
        if sub not in latest or days_ago < latest[sub]:
            latest[sub] = float(days_ago)
    return latest


def recent_posts_by_project(days=7):
    """Return dict: project_name -> count of original threads posted in last N days."""
    rows = _fetch_own_reddit_threads(days)
    counts = {}
    for _url, _days_ago, project in rows:
        if not project:
            continue
        counts[project] = counts.get(project, 0) + 1
    return counts


def build_candidates(config):
    recent = recent_posts_by_sub(max_days=max(
        DEFAULT_OWN_FLOOR_DAYS, DEFAULT_EXTERNAL_FLOOR_DAYS, 14))
    thread_blocked = load_thread_blocked_subs(config)
    candidates = []
    for p in config.get("projects", []):
        t = p.get("threads") or {}
        if not t.get("enabled"):
            continue
        ext_floor = int(t.get("external_floor_days", DEFAULT_EXTERNAL_FLOOR_DAYS))
        # Own community
        own = t.get("own_community")
        if own:
            if isinstance(own, dict):
                sub_display = own.get("subreddit")
                own_floor = int(own.get("floor_days", DEFAULT_OWN_FLOOR_DAYS))
            else:
                sub_display = own
                own_floor = DEFAULT_OWN_FLOOR_DAYS
            slug = norm_sub(sub_display)
            if sub_display and slug not in thread_blocked:
                last = recent.get(slug)
                if last is None or last >= own_floor:
                    candidates.append((p, sub_display, True, own_floor, last))
        # External subs
        for sub in t.get("external_subreddits") or []:
            slug = norm_sub(sub)
            if slug in thread_blocked:
                continue
            last = recent.get(slug)
            if last is not None and last < ext_floor:
                continue
            candidates.append((p, sub, False, ext_floor, last))
    return candidates, recent, thread_blocked


def pick(candidates, recent_project_counts=None):
    own_candidates = [c for c in candidates if c[2]]
    if own_candidates:
        return random.choice(own_candidates)
    if not candidates:
        return None
    recent_project_counts = recent_project_counts or {}
    by_project = {}
    for p, sub, is_own, floor, last in candidates:
        by_project.setdefault(p["name"], {"project": p, "entries": []})
        by_project[p["name"]]["entries"].append((sub, is_own, floor, last))
    names = list(by_project.keys())
    # Inverse recent-share weighting: keep config weight as the prior, but
    # penalise projects that already posted a lot in the last 7 days.
    # effective = base_weight / (1 + posts_last_7d). 0 posts => no change,
    # each recent post halves the odds relative to a never-posted peer at 1.
    weights = [
        by_project[n]["project"].get("weight", 1)
        / (1 + recent_project_counts.get(n, 0))
        for n in names
    ]
    chosen_name = random.choices(names, weights=weights, k=1)[0]
    proj = by_project[chosen_name]["project"]
    sub, is_own, floor, last = random.choice(by_project[chosen_name]["entries"])
    return (proj, sub, is_own, floor, last)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--show-all", action="store_true")
    args = ap.parse_args()

    config = load_config()
    candidates, recent, thread_blocked = build_candidates(config)
    recent_project_counts = recent_posts_by_project(days=7)

    if args.show_all:
        print(f"Thread-blocked subs ({len(thread_blocked)}): {sorted(thread_blocked)}")
        print(f"Recent thread subs: {len(recent)}")
        for sub, days in sorted(recent.items(), key=lambda x: x[1]):
            print(f"  {sub}: {days:.2f}d ago")
        eligible_projects = {}
        for p, sub, is_own, floor, last in candidates:
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
        for p, sub, is_own, floor, last in candidates:
            tag = "OWN" if is_own else "ext"
            last_str = f"last={last:.2f}d" if last is not None else "last=never"
            print(f"  [{tag}] {p['name']:25} {sub:30} floor={floor}d {last_str}")
        return

    choice = pick(candidates, recent_project_counts=recent_project_counts)
    if not choice:
        print("NO_ELIGIBLE_TARGET", file=sys.stderr)
        sys.exit(2)

    proj, sub, is_own, floor, last = choice
    if args.json:
        print(json.dumps({
            "project": proj,
            "subreddit": sub,
            "is_own_community": is_own,
            "floor_days": floor,
            "last_posted_days_ago": last,
            "eligible_count": len(candidates),
            "thread_blocked_count": len(thread_blocked),
        }, indent=2))
    else:
        print(f"{proj['name']}\t{sub}")


if __name__ == "__main__":
    main()
