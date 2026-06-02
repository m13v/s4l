#!/usr/bin/env python3
"""Pick which project(s) to post about. Shared across every platform.

Inverse-recent-share weighting: a project's selection weight is its config
`weight` divided by (1 + its posts in the last RECENT_WINDOW_DAYS), so a
project that has been posting heavily is dampened toward under-posted ones
(but never selected above its raw weight). Single-pick (pick_project) and
multi-pick (pick_projects / --count N) share one code path, so Twitter,
GitHub and Reddit all select projects the same way.

Usage:
    python3 scripts/pick_project.py                       # one project, any platform
    python3 scripts/pick_project.py --platform reddit     # one project for a platform
    python3 scripts/pick_project.py --json                # one project, full JSON
    python3 scripts/pick_project.py --platform twitter --count 8 --json  # N projects, JSON array
"""

import argparse
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from project_topics import topics_for_project  # noqa: E402

CONFIG_PATH = os.path.expanduser("~/social-autoposter/config.json")

# Rolling window (days) for inverse-recent-share weighting in pick_projects().
RECENT_WINDOW_DAYS = 7


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _counts_via_api(platform=None):
    from http_api import api_get
    query = {"platform": platform} if platform else None
    resp = api_get("/api/v1/posts/counts-today-by-project", query=query)
    data = (resp or {}).get("data") or {}
    counts = data.get("counts") or {}
    return {k: int(v) for k, v in counts.items()}


def get_posts_today_by_project(platform=None):
    """Return dict of project_name -> post count for today.

    Routes through /api/v1/posts/counts-today-by-project (HTTP-only).
    """
    return _counts_via_api(platform)


def _recent_counts_via_api(platform=None, days=RECENT_WINDOW_DAYS):
    from http_api import api_get
    query = {"days": str(int(days))}
    if platform:
        query["platform"] = platform
    resp = api_get("/api/v1/posts/counts-by-project-window", query=query)
    data = (resp or {}).get("data") or {}
    counts = data.get("counts") or {}
    return {k: int(v) for k, v in counts.items()}


def recent_posts_by_project(platform=None, days=RECENT_WINDOW_DAYS):
    """Return {project_name: post count} over the last `days` days.

    Routes through /api/v1/posts/counts-by-project-window (HTTP-only).
    Feeds the inverse-recent-share weighting in pick_projects().
    """
    return _recent_counts_via_api(platform, days)


def _eligible_pool(config, platform=None, exclude=None):
    """Projects eligible for selection: enabled, weight>0, platform-compatible."""
    pool = [
        p for p in config.get("projects", [])
        if p.get("enabled", True) and p.get("weight", 0) > 0
    ]
    if exclude:
        excluded = {n.lower() for n in exclude}
        pool = [p for p in pool if p.get("name", "").lower() not in excluded]
    # Explicit per-project platforms_disabled deny list.
    if platform:
        pool = [p for p in pool if platform not in (p.get("platforms_disabled") or [])]
    # twitter/linkedin/github draft a search query, so they need seed topics
    # (DB-backed project_search_topics, post 2026-05-27 config.json removal).
    if platform in ("twitter", "linkedin", "github"):
        pool = [p for p in pool if topics_for_project(p.get("name") or "")]
    return pool


def pick_projects(config, platform=None, n=1, exclude=None):
    """Pick up to `n` distinct projects. Shared by every platform's pipeline.

    Inverse-recent-share weighting: effective_weight = weight / (1 + posts in
    the last RECENT_WINDOW_DAYS). Sampled without replacement, so a project
    that has been posting heavily is dampened in favor of under-posted ones,
    but a project is never selected above its raw `weight`. Returns a list of
    project dicts (shorter than `n` only when the eligible pool is smaller).
    """
    pool = _eligible_pool(config, platform, exclude)
    if not pool:
        return []
    counts = recent_posts_by_project(platform)
    chosen = []
    remaining = list(pool)
    for _ in range(min(n, len(remaining))):
        weights = [p["weight"] / (1 + counts.get(p["name"], 0)) for p in remaining]
        idx = random.choices(range(len(remaining)), weights=weights, k=1)[0]
        chosen.append(remaining.pop(idx))
    return chosen


def pick_project(config, platform=None, exclude=None):
    """Pick a single project. Thin wrapper around pick_projects() kept for the
    existing callers (post_reddit.py, the bare CLI, --json, etc.)."""
    picks = pick_projects(config, platform, n=1, exclude=exclude)
    if picks:
        return picks[0]
    if exclude:
        return None
    # No eligible project at all: legacy fallback to any project in config.
    projects = config.get("projects", [])
    return random.choice(projects) if projects else None


def main():
    parser = argparse.ArgumentParser(description="Pick next project to post about")
    parser.add_argument("--platform", default=None, help="Platform to check distribution for")
    parser.add_argument("--json", action="store_true", help="Output full project config as JSON")
    parser.add_argument("--project", default=None, help="Select a specific project by name")
    parser.add_argument("--show-weights", action="store_true", help="Show all projects and their current distribution")
    parser.add_argument("--distribution", action="store_true", help="Show compact distribution for LLM prompts")
    parser.add_argument("--exclude", default=None, help="Comma-separated project names to exclude from picking")
    parser.add_argument("--count", type=int, default=1, help="Number of projects to pick; >1 emits a JSON array")
    args = parser.parse_args()

    exclude = None
    if args.exclude:
        exclude = [n.strip() for n in args.exclude.split(",") if n.strip()]

    config = load_config()

    if args.distribution:
        projects = config.get("projects", [])
        weighted = [p for p in projects if p.get("weight", 0) > 0]
        if args.platform:
            weighted = [p for p in weighted if args.platform not in (p.get("platforms_disabled") or [])]
        total_weight = sum(p.get("weight", 0) for p in weighted)
        counts = get_posts_today_by_project(args.platform)
        lines = []
        for p in sorted(weighted, key=lambda x: x["weight"], reverse=True):
            target_pct = (p["weight"] / total_weight * 100) if total_weight else 0
            actual = counts.get(p["name"], 0)
            lines.append(f"{p['name']}: {actual} posts today (target {target_pct:.0f}%)")
        print("\n".join(lines))
        return

    if args.show_weights:
        projects = config.get("projects", [])
        weighted = [p for p in projects if p.get("weight", 0) > 0]
        if args.platform:
            weighted = [p for p in weighted if args.platform not in (p.get("platforms_disabled") or [])]
        total_weight = sum(p.get("weight", 0) for p in weighted)
        counts = get_posts_today_by_project(args.platform)
        total_posts = sum(counts.values()) or 1

        print(f"{'Project':25} {'Weight':>8} {'Target%':>8} {'Today':>6} {'Actual%':>8} {'Deficit':>8}")
        print("-" * 73)
        for p in sorted(weighted, key=lambda x: x["weight"], reverse=True):
            target_pct = (p["weight"] / total_weight * 100) if total_weight else 0
            actual = counts.get(p["name"], 0)
            actual_pct = (actual / total_posts * 100) if total_posts > 0 else 0
            deficit = target_pct - actual_pct
            print(f"{p['name']:25} {p['weight']:>8} {target_pct:>7.1f}% {actual:>6} {actual_pct:>7.1f}% {deficit:>+7.1f}%")
        return

    if args.count and args.count > 1:
        picks = pick_projects(config, args.platform, n=args.count, exclude=exclude)
        print(json.dumps(picks, indent=2))
        return

    if args.project:
        project = None
        for p in config.get("projects", []):
            if p.get("name", "").lower() == args.project.lower():
                project = p
                break
        if not project:
            print(f"Unknown project: {args.project}", file=sys.stderr)
            sys.exit(1)
    else:
        project = pick_project(config, args.platform, exclude=exclude)
        if project is None:
            print("No eligible project (all excluded)", file=sys.stderr)
            sys.exit(2)

    if args.json:
        print(json.dumps(project, indent=2))
    else:
        print(project["name"])


if __name__ == "__main__":
    main()
