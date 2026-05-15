#!/usr/bin/env python3
"""Pick the next project to post about based on weight distribution.

Compares each project's target weight against actual posts today,
and picks the most underrepresented project.

Usage:
    python3 scripts/pick_project.py                    # pick for any platform
    python3 scripts/pick_project.py --platform reddit  # pick for specific platform
    python3 scripts/pick_project.py --json             # output full project config as JSON
"""

import argparse
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CONFIG_PATH = os.path.expanduser("~/social-autoposter/config.json")


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


def _counts_via_neon(platform=None):
    import db as dbmod
    conn = dbmod.get_conn()
    try:
        if platform:
            rows = conn.execute(
                "SELECT COALESCE(project_name, '(none)'), COUNT(*) "
                "FROM posts WHERE DATE(posted_at) = CURRENT_DATE AND platform = %s "
                "GROUP BY project_name",
                [platform],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT COALESCE(project_name, '(none)'), COUNT(*) "
                "FROM posts WHERE DATE(posted_at) = CURRENT_DATE "
                "GROUP BY project_name"
            ).fetchall()
    finally:
        conn.close()
    return {row[0]: row[1] for row in rows}


def get_posts_today_by_project(platform=None):
    """Return dict of project_name -> post count for today.

    Routes through /api/v1/posts/counts-today-by-project by default.
    Set SOCIAL_AUTOPOSTER_LEGACY_NEON=1 for direct Neon.
    """
    if os.environ.get("SOCIAL_AUTOPOSTER_LEGACY_NEON") == "1":
        return _counts_via_neon(platform)
    return _counts_via_api(platform)


def pick_project(config, platform=None, exclude=None):
    """Pick the most underrepresented project based on weights.

    Returns the project dict from config.json.
    """
    projects = config.get("projects", [])
    weighted = [p for p in projects if p.get("weight", 0) > 0]
    if exclude:
        excluded = {n.lower() for n in exclude}
        weighted = [p for p in weighted if p.get("name", "").lower() not in excluded]

    # Filter by explicit platforms_disabled deny list (config.json per-project field)
    if platform:
        weighted = [p for p in weighted if platform not in (p.get("platforms_disabled") or [])]

    # Filter by platform compatibility: skip projects with no search_topics
    # (the unified seed list, post 2026-04-30 legacy-field removal).
    if platform in ("twitter", "linkedin", "github"):
        weighted = [p for p in weighted if p.get("search_topics")]

    if not weighted:
        if exclude:
            return None
        return random.choice(projects)

    total_weight = sum(p["weight"] for p in weighted)
    counts = get_posts_today_by_project(platform)
    total_posts = sum(counts.values()) or 1  # avoid division by zero

    # Calculate deficit: target_share - actual_share
    # Higher deficit = more underrepresented = higher priority
    scored = []
    for p in weighted:
        target_share = p["weight"] / total_weight
        actual_count = counts.get(p["name"], 0)
        actual_share = actual_count / total_posts if total_posts > 0 else 0
        deficit = target_share - actual_share
        scored.append((deficit, p))

    # Sort by deficit descending (most underrepresented first)
    scored.sort(key=lambda x: x[0], reverse=True)

    # Pick from top candidates with some randomness to avoid always picking the same one
    # Take all projects with deficit >= top deficit - 0.05 (within 5% of most underrepresented)
    top_deficit = scored[0][0]
    candidates = [p for deficit, p in scored if deficit >= top_deficit - 0.05]

    return random.choice(candidates)


def main():
    parser = argparse.ArgumentParser(description="Pick next project to post about")
    parser.add_argument("--platform", default=None, help="Platform to check distribution for")
    parser.add_argument("--json", action="store_true", help="Output full project config as JSON")
    parser.add_argument("--project", default=None, help="Select a specific project by name")
    parser.add_argument("--show-weights", action="store_true", help="Show all projects and their current distribution")
    parser.add_argument("--distribution", action="store_true", help="Show compact distribution for LLM prompts")
    parser.add_argument("--exclude", default=None, help="Comma-separated project names to exclude from picking")
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
