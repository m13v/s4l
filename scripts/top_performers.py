#!/usr/bin/env python3
"""Generate a feedback report from top/bottom performing posts.

Queries NeonDB for engagement data and outputs a factual report
organized by project and platform. This is the self-improvement
feedback loop — Claude reads this before drafting new comments.

Usage:
    python3 scripts/top_performers.py
    python3 scripts/top_performers.py --platform reddit
    python3 scripts/top_performers.py --project Fazm
    python3 scripts/top_performers.py --project Fazm --platform reddit
    python3 scripts/top_performers.py --top 20
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod

MIN_CONTENT_LEN = 30  # skip posts with empty/placeholder content

# Composite score: comments are the strongest imitation signal (real discussion),
# upvotes are second, views deliberately excluded (viral-by-algorithm ≠ a pattern
# worth imitating). Reddit and Moltbook upvotes get -1 because the OP's self-upvote
# inflates the score (Reddit via API default, Moltbook via our own moltbook_post.py
# self_upvote() call after every create). SQL expression is reused across queries.
SCORE_SQL = (
    "(COALESCE(comments_count,0) * 3 + "
    "CASE WHEN LOWER(platform) IN ('reddit', 'moltbook') "
    "THEN GREATEST(0, COALESCE(upvotes,0) - 1) "
    "ELSE COALESCE(upvotes,0) END)"
)

# Per-row net upvotes: Reddit and Moltbook auto-apply a +1 OP self-upvote on
# every post, so the raw `upvotes` column starts at 1 for a brand-new post with
# zero real engagement. Strip that +1 per row (clamped at 0 so downvoted posts
# don't go negative). All human-facing display, AVG, MAX, etc. in this script
# should aggregate this expression instead of `upvotes` directly so the report
# matches the score and the dashboard.
UPVOTES_NET_SQL = (
    "(CASE WHEN LOWER(platform) IN ('reddit', 'moltbook') "
    "THEN GREATEST(0, COALESCE(upvotes,0) - 1) "
    "ELSE COALESCE(upvotes,0) END)"
)

# Per-platform "meaningful engagement" floor for the SCORE_SQL composite.
# Twitter/LinkedIn reactions are rarer than Reddit upvotes, so thresholds differ.
PLATFORM_MIN_SCORE = {
    "reddit":   10,
    "twitter":  5,
    "x":        5,
    "linkedin": 3,
    "moltbook": 3,
    "github":   3,
}
DEFAULT_MIN_SCORE = 5

def min_score_for(platform):
    if platform is None:
        return DEFAULT_MIN_SCORE
    return PLATFORM_MIN_SCORE.get(str(platform).lower(), DEFAULT_MIN_SCORE)

# =====================================================================
# DO NOT REMOVE OR SIMPLIFY THE FUNCTIONS BELOW.
# These are data-driven improvements based on analysis of 3,000+ posts.
# They have been reverted by other agents twice already.
# Protected by pre-commit hook. See CLAUDE.md.
# =====================================================================

# Product names that indicate self-promotion (teaching Claude bad habits)
PRODUCT_NAMES = [
    "fazm", "assrt", "pieline", "cyrano", "terminator", "mk0r", "s4l",
    "vipassana.cool", "vipassana-cool",
]


def get_distilled_rules(platform):
    """Return guidance on how to interpret the performance data below."""
    if platform == "reddit":
        return """## HOW TO USE THIS REPORT
- Comments are the strongest signal: a post that sparked replies taught people something or hit a nerve. Prioritize imitating posts with high comment counts, even if upvotes are modest.
- Upvotes are second-tier (passive approval). Views are excluded because viral-by-algorithm is not a pattern worth copying.
- Study the top posts: what style, length, and tone got real discussion? Do more of that.
- Study the bottom posts and their FAILURE REASON annotations: avoid those patterns entirely.
- Compare avg_cm (then avg_up) across styles in the summary. Pick styles that actually drive conversation, not just familiar ones.
- Posts with product mentions or URLs consistently underperform. The top posts never contain them.
- Look at content length in top vs bottom posts. Let the data guide whether to go short or long.
"""
    return ""


def has_anti_pattern(content):
    """Check if content contains product names or links (bad teaching examples)."""
    if not content:
        return False
    lower = content.lower()
    for name in PRODUCT_NAMES:
        if name in lower:
            return True
    if "http://" in lower or "https://" in lower or "www." in lower:
        return True
    return False


def annotate_failure(row):
    """Detect why a bottom post likely failed and return a reason string."""
    content = (row[5] or "").lower()
    reasons = []
    for name in PRODUCT_NAMES:
        if name in content:
            reasons.append(f"mentions '{name}'")
            break
    if "http://" in content or "https://" in content or "www." in content:
        reasons.append("contains URL/link")
    if any(phrase in content for phrase in [
        "phone order", "missed call", "phone call", "unanswered call",
        "call capture", "answering service",
    ]):
        reasons.append("product-adjacent pitch (phone/call capture)")
    if any(phrase in content for phrase in [
        "macOS app", "macos app", "desktop agent", "accessibility api",
        "mcp server", "mcp layer",
    ]):
        reasons.append("product-adjacent (mentions own project)")
    if content.count("?") >= 3:
        reasons.append("too many questions (reads as interrogation)")
    if "curious" in content and ("?" in content):
        reasons.append("curious_probe style (negative avg on Reddit)")
    if len(content) < 100:
        reasons.append("too short without being punchy")
    if not reasons:
        reasons.append("likely wrong subreddit or off-topic")
    return " | ".join(reasons)


def get_style_performance(conn, platform=None):
    """Avg upvotes AND avg comments per engagement_style for the given platform.

    Comments are reported alongside upvotes so Claude can see which styles
    drive discussion (the strongest imitation signal), not just which accumulate
    passive upvotes.
    """
    where_clauses = [
        "status = 'active'",
        "engagement_style IS NOT NULL",
        "our_content IS NOT NULL",
        f"LENGTH(our_content) >= {MIN_CONTENT_LEN}",
        "upvotes IS NOT NULL",
    ]
    params = []
    if platform:
        where_clauses.append("platform = %s")
        params.append(platform)
    where = " AND ".join(where_clauses)
    cur = conn.execute(
        f"SELECT engagement_style, COUNT(*) as cnt, "
        f"AVG({UPVOTES_NET_SQL})::numeric(10,2) as avg_up, "
        f"AVG(COALESCE(comments_count,0))::numeric(10,2) as avg_cm, "
        f"MAX({UPVOTES_NET_SQL}) as max_up, "
        f"MAX(comments_count) as max_cm "
        f"FROM posts WHERE {where} "
        f"GROUP BY engagement_style ORDER BY avg_cm DESC, avg_up DESC",
        params,
    )
    return cur.fetchall()


def get_project_platform_summary(conn, project=None, platform=None):
    """Post count and avg upvotes per project per platform.

    When project/platform are given, show:
    - The filtered project across all platforms (for cross-platform context)
    - The filtered platform across all projects (for competitive context)
    """
    where_clauses = [
        "status = 'active'",
        "platform NOT IN ('github_issues','hackernews','dev','youtube','github')",
        "our_content IS NOT NULL",
        f"LENGTH(our_content) >= {MIN_CONTENT_LEN}",
    ]

    if project and platform:
        # Show this project on all platforms + this platform for all projects
        filter_sql = f"(COALESCE(project_name, '(no project)') = %s OR platform = %s)"
        params = [project, platform]
    elif project:
        filter_sql = "COALESCE(project_name, '(no project)') = %s"
        params = [project]
    elif platform:
        filter_sql = "platform = %s"
        params = [platform]
    else:
        filter_sql = None
        params = []

    if filter_sql:
        where_clauses.append(filter_sql)

    where = " AND ".join(where_clauses)
    cur = conn.execute(
        f"SELECT COALESCE(project_name, '(no project)') as proj, platform, "
        f"COUNT(*) as cnt, "
        f"AVG({UPVOTES_NET_SQL})::numeric(10,1) as avg_up, "
        f"AVG(COALESCE(comments_count,0))::numeric(10,1) as avg_cm, "
        f"MAX({UPVOTES_NET_SQL}) as max_up, "
        f"MAX(comments_count) as max_cm "
        f"FROM posts WHERE {where} "
        f"GROUP BY project_name, platform ORDER BY proj, avg_cm DESC, avg_up DESC",
        params
    )
    return cur.fetchall()


def get_top_posts(conn, project=None, platform=None, limit=15, min_score=None):
    """Top performing posts with full factual details.

    Ranks by composite SCORE_SQL (comments*3 + upvotes, Reddit -1) so that
    discussion-driving posts surface even with modest upvote counts. Threshold
    is per-platform (see PLATFORM_MIN_SCORE) since reactions/likes/upvotes have
    different scales. If project is given and has no posts meeting the
    threshold, returns None so the caller can fall back to general posts.
    """
    if min_score is None:
        min_score = min_score_for(platform)
    where_clauses = [
        "status = 'active'",
        "upvotes IS NOT NULL",
        f"{SCORE_SQL} >= {min_score}",
        "our_content IS NOT NULL",
        f"LENGTH(our_content) >= {MIN_CONTENT_LEN}",
        "platform NOT IN ('github_issues')",
    ]
    params = []
    if project:
        where_clauses.append("project_name = %s")
        params.append(project)
    if platform:
        where_clauses.append("platform = %s")
        params.append(platform)

    where = " AND ".join(where_clauses)
    # Fetch extra rows so we can filter out anti-pattern examples.
    # Tiebreaker uses UPVOTES_NET_SQL so two posts with identical scores on
    # Reddit/Moltbook order by net upvotes (not net + self-upvote noise) —
    # matches every other display in this script and on the dashboard.
    fetch_limit = limit * 3
    cur = conn.execute(
        f"SELECT id, platform, upvotes, comments_count, views, "
        f"our_content, thread_title, thread_content, "
        f"project_name, posted_at::date, our_account "
        f"FROM posts WHERE {where} "
        f"ORDER BY {SCORE_SQL} DESC, {UPVOTES_NET_SQL} DESC LIMIT %s",
        params + [fetch_limit]
    )
    rows = cur.fetchall()
    # Filter out top posts that contain product names or links
    # (these send mixed signals about what works)
    clean = [r for r in rows if not has_anti_pattern(r[5])]
    return clean[:limit]


def get_bottom_posts(conn, project=None, platform=None, limit=10):
    """Worst performing posts (zero-engagement by composite score).

    Uses SCORE_SQL = 0 as the failure gate instead of `upvotes < 1`. This
    correctly catches Reddit posts sitting at upvotes=1 with no comments
    (which the old filter missed because the OP self-upvote clears `< 1`),
    and still captures zero-engagement posts across all platforms.
    """
    where_clauses = [
        "status = 'active'",
        "upvotes IS NOT NULL",
        f"{SCORE_SQL} = 0",
        "our_content IS NOT NULL",
        f"LENGTH(our_content) >= {MIN_CONTENT_LEN}",
        "platform NOT IN ('github_issues')",
    ]
    params = []
    if project:
        where_clauses.append("project_name = %s")
        params.append(project)
    if platform:
        where_clauses.append("platform = %s")
        params.append(platform)

    where = " AND ".join(where_clauses)
    cur = conn.execute(
        f"SELECT id, platform, upvotes, comments_count, views, "
        f"our_content, thread_title, thread_content, "
        f"project_name, posted_at::date, our_account "
        f"FROM posts WHERE {where} "
        f"ORDER BY posted_at DESC LIMIT %s",
        params + [limit]
    )
    return cur.fetchall()


def format_post(row, include_thread_content=True):
    """Format a single post as factual text.

    Upvotes are reported NET on Reddit and Moltbook: both platforms auto-apply
    a +1 OP self-upvote on every post, so the raw `upvotes` column starts at 1
    for a brand-new post with zero real engagement. Strip that +1 here so the
    display matches SCORE_SQL and the dashboard. Other platforms pass through.
    """
    lines = []
    platform_lc = str(row[1] or "").lower()
    raw_upvotes = row[2] if row[2] is not None else 0
    if platform_lc in ("reddit", "moltbook"):
        upvotes = max(0, raw_upvotes - 1)
    else:
        upvotes = raw_upvotes
    comments = row[3] if row[3] is not None else 0
    views = row[4] if row[4] is not None else 0
    our_content = row[5] or ""
    thread_title = row[6] or ""
    thread_content = row[7] or ""
    project = row[8] or "(no project)"
    date = row[9]
    account = row[10] or ""

    header = f"[{upvotes} upvotes, {comments} comments, {views} views] {row[1]} | {project} | {date}"
    lines.append(header)

    if thread_title:
        lines.append(f"  Thread: {thread_title}")
    if include_thread_content and thread_content:
        snippet = thread_content.replace('\n', ' ')
        lines.append(f"  Thread body: {snippet}")
    lines.append(f"  Our comment: {our_content}")
    return "\n".join(lines)


def format_report(summary, top, bottom, project=None, platform=None,
                   top_by_group=None, fallback_top=None, style_perf=None):
    """Format the full report."""
    lines = []
    filters = []
    if project:
        filters.append(f"project={project}")
    if platform:
        filters.append(f"platform={platform}")
    scope = f" ({', '.join(filters)})" if filters else ""
    lines.append(f"## Performance Feedback Report{scope}")
    lines.append("")

    # Distilled rules first (most important part of the report)
    if platform:
        rules = get_distilled_rules(platform)
        if rules:
            lines.append(rules)

    # "meaningful engagement" is scored as comments*3 + upvotes (Reddit -1),
    # with a per-platform floor (see PLATFORM_MIN_SCORE). Report it to Claude so
    # it understands why borderline posts are/aren't included.
    threshold_label = (
        f">= score {min_score_for(platform)} "
        f"(comments*3 + upvotes, Reddit upvote -1)"
    )

    # Style performance (live from DB). Report both upvotes AND comments so
    # discussion-driving styles surface, not just upvote-accumulating ones.
    if style_perf:
        lines.append("### Engagement Style Performance (live data, sorted by avg comments)")
        for row in style_perf:
            lines.append(
                f"  {row[0]:<22} {row[1]:>5} posts  "
                f"avg_cm={row[3]}  avg_up={row[2]}  "
                f"best_cm={row[5]}  best_up={row[4]}"
            )
        lines.append("")

    # Summary table
    lines.append("### Posts per Project per Platform")
    for row in summary:
        lines.append(
            f"  {row[0]:<20} {row[1]:<12} {row[2]:>5} posts  "
            f"avg_cm={row[4]}  avg_up={row[3]}  "
            f"best_cm={row[6]}  best_up={row[5]}"
        )
    lines.append("")

    # Per-project top performers (when no project filter)
    if top_by_group:
        lines.append(f"### Top Posts by Project ({threshold_label})")
        for group_name, posts in top_by_group.items():
            if not posts:
                continue
            lines.append(f"\n#### {group_name}")
            for p in posts:
                lines.append(format_post(p))
                lines.append("")
    elif top:
        # Filtered view with results
        lines.append(
            f"### Top {len(top)} Posts for {project or 'all projects'} ({threshold_label})"
        )
        for p in top:
            lines.append(format_post(p))
            lines.append("")
    elif fallback_top:
        # No project-specific posts met threshold — show general high performers
        platform_label = f" on {platform}" if platform else ""
        lines.append(f"### No {project} posts meeting {threshold_label}{platform_label}.")
        lines.append(f"### Showing top posts from OTHER projects{platform_label} as reference:")
        lines.append("")
        for p in fallback_top:
            lines.append(format_post(p))
            lines.append("")

    # Bottom posts with failure annotations
    if bottom:
        lines.append(f"### Bottom {len(bottom)} Posts (avoid these patterns)")
        for p in bottom:
            lines.append(format_post(p, include_thread_content=False))
            reason = annotate_failure(p)
            lines.append(f"  >> FAILURE REASON: {reason}")
            lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Generate top performers feedback report")
    parser.add_argument("--platform", default=None, help="Filter to specific platform")
    parser.add_argument("--project", default=None, help="Filter to specific project")
    parser.add_argument("--top", type=int, default=5, help="Number of top posts to show (per group or total)")
    parser.add_argument("--bottom", type=int, default=5, help="Number of bottom posts to show")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    conn = dbmod.get_conn()

    summary = get_project_platform_summary(conn, project=args.project, platform=args.platform)
    style_perf = get_style_performance(conn, platform=args.platform)
    top = get_top_posts(conn, project=args.project, platform=args.platform, limit=args.top)
    bottom = get_bottom_posts(conn, project=args.project, platform=args.platform, limit=args.bottom)

    # If project was specified but no posts met the threshold, fall back to
    # general high-performing posts on the same platform
    fallback_top = None
    if args.project and not top:
        fallback_top = get_top_posts(conn, project=None, platform=args.platform, limit=args.top)

    # When no project filter, also get top 5 per project for focused examples.
    # Uses the same SCORE_SQL composite as get_top_posts so ranking is consistent.
    top_by_group = None
    if not args.project:
        top_by_group = {}
        min_score = min_score_for(args.platform)
        platform_filter = "AND platform = %s" if args.platform else ""
        platform_params = [args.platform] if args.platform else []
        # Get distinct projects (respecting platform filter)
        cur = conn.execute(
            f"SELECT DISTINCT COALESCE(project_name, '(no project)') FROM posts "
            f"WHERE status = 'active' AND platform NOT IN ('github_issues') "
            f"AND our_content IS NOT NULL AND LENGTH(our_content) >= %s "
            f"AND upvotes IS NOT NULL AND {SCORE_SQL} >= %s "
            f"{platform_filter} "
            f"ORDER BY 1",
            [MIN_CONTENT_LEN, min_score] + platform_params
        )
        projects = [row[0] for row in cur.fetchall()]
        for proj in projects:
            proj_filter = proj if proj != "(no project)" else None
            where_extra = "AND project_name = %s" if proj_filter else "AND project_name IS NULL"
            params = ([proj_filter] if proj_filter else []) + platform_params
            cur = conn.execute(
                f"SELECT id, platform, upvotes, comments_count, views, "
                f"our_content, thread_title, thread_content, "
                f"project_name, posted_at::date, our_account "
                f"FROM posts WHERE status = 'active' AND {SCORE_SQL} >= {min_score} "
                f"AND our_content IS NOT NULL AND LENGTH(our_content) >= {MIN_CONTENT_LEN} "
                f"AND platform NOT IN ('github_issues') "
                f"{where_extra} {platform_filter} "
                f"ORDER BY {SCORE_SQL} DESC, {UPVOTES_NET_SQL} DESC LIMIT 5",
                params
            )
            top_by_group[proj] = cur.fetchall()

    conn.close()

    if args.json:
        output = {
            "summary": [dict(row) for row in summary],
            "top_posts": [dict(row) for row in top],
            "bottom_posts": [dict(row) for row in bottom],
            "fallback_top": [dict(row) for row in fallback_top] if fallback_top else [],
        }
        print(json.dumps(output, indent=2, default=str))
    else:
        print(format_report(summary, top, bottom,
                            project=args.project, platform=args.platform,
                            top_by_group=top_by_group, fallback_top=fallback_top,
                            style_perf=style_perf))


if __name__ == "__main__":
    main()
