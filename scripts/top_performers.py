#!/usr/bin/env python3
"""Generate a feedback report from top/bottom performing posts.

Queries Postgres for engagement data and outputs a factual report
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

MIN_CONTENT_LEN = 30  # skip posts with empty/placeholder content

# CTE that adds a bot-filtered `clicks` column to every row of `posts`.
# Sources from `post_link_clicks` (per-hit log, populated by the redirector
# after 2026-05-07) with `is_bot=false`. This is the same attribution path
# used by top_search_topics.py and matches what the dashboard reports on
# the Top Comments tab. The legacy `post_links.real_clicks` column is a
# stale PostHog backfill and is wildly inaccurate (twitter ~7x undercount,
# reddit permanently 0), so we do NOT use it here.
#
# Why a CTE: the score expression below references `clicks` in WHERE,
# ORDER BY, and SELECT clauses across multiple functions. A correlated
# subquery inline-repeated 3x per query would compile, but the CTE form
# stays readable and Postgres can hoist the per-post aggregation once.
POSTS_WITH_CLICKS_CTE = """
WITH posts_w_clicks AS (
  SELECT p.*,
    COALESCE((
      SELECT COUNT(plc.id)
        FROM post_links pl
        LEFT JOIN post_link_clicks plc
               ON plc.code = pl.code AND plc.is_bot = false
       WHERE pl.post_id = p.id
    ), 0) AS clicks
    FROM posts p
)
"""

# Composite score (2026-05-12 reweight): real human clicks are the ONLY
# signal that proves a comment drove someone to actually visit the
# project's link. Comments are the next-best imitation signal (real
# discussion). Upvotes are passive approval, kept faint. Views deliberately
# excluded (viral-by-algorithm ≠ a pattern worth imitating). Reddit and
# Moltbook upvotes get -1 to strip the OP's auto self-upvote.
#
# Click weight ×10 means one real human click outvalues 10 likes worth
# of vibes when ranking top examples for the generator's few-shot context.
# This is the same direction top_search_topics.py already takes (×100
# there because that script ranks SEARCH QUERIES, where a single click
# across a query's posts is rare). For per-post example ranking ×10
# keeps zero-click posts with very strong discussion (Reddit threads with
# 20 comments) still competitive.
SCORE_SQL = (
    "(COALESCE(clicks, 0) * 10 + "
    "COALESCE(comments_count,0) * 3 + "
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

# Recency window for every SCORE_SQL-driven query in this module. Lifetime
# aggregation drifted too far from current performance reality (old wins kept
# old styles in the picker pool even after the audience/algorithm shifted).
# 30 days keeps n large enough for stable averages while letting the report
# track the live algorithm. Set RECENCY_DAYS=0 to fall back to lifetime.
# Mirrors engagement_styles.RECENCY_DAYS so the picker and few-shot context
# never disagree on which window defines "top".
RECENCY_DAYS = 30


def _recency_clause():
    """Return a WHERE-clause fragment that limits posts to the recency window,
    or an empty string if RECENCY_DAYS == 0 (lifetime mode)."""
    if not RECENCY_DAYS or RECENCY_DAYS <= 0:
        return ""
    return f"posted_at >= NOW() - INTERVAL '{int(RECENCY_DAYS)} days'"

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


_ACTIVE_CAMPAIGN_SUFFIXES_CACHE = None


def _load_active_campaign_suffixes():
    """Best-effort: return a list of currently-active campaign suffix literals.

    Cached per-process. Used to strip the suffix from `our_content` before
    feeding it into the few-shot prompt context, so the LLM never learns to
    echo the suffix in its drafts (which then double-fires with the
    tool-layer injection, observed 2026-05-18 on Reddit IDs 70412 + 70413).
    On any failure returns []: missing strip is preferable to crashing
    the report pipeline. Routes through the HTTP API (/api/v1/campaigns).
    """
    global _ACTIVE_CAMPAIGN_SUFFIXES_CACHE
    if _ACTIVE_CAMPAIGN_SUFFIXES_CACHE is not None:
        return _ACTIVE_CAMPAIGN_SUFFIXES_CACHE
    suffixes = []
    try:
        from http_api import api_get
        resp = api_get(
            "/api/v1/campaigns",
            query={"status": "active", "has_suffix": "true", "limit": 500},
        )
        rows = ((resp or {}).get("data") or {}).get("campaigns") or []
        for r in rows:
            s = (r.get("suffix") or "").strip()
            if s and s not in suffixes:
                suffixes.append(s)
    except Exception as e:
        print(f"[top_performers] _load_active_campaign_suffixes (api) failed: {e}",
              file=sys.stderr)
    _ACTIVE_CAMPAIGN_SUFFIXES_CACHE = suffixes
    return suffixes


def _strip_active_campaign_suffixes(text, suffixes):
    """Trailing-only, idempotent strip of any active-campaign suffix.

    Idempotent loop also collapses an already-doubled historical suffix to
    clean text. Trailing-only so we never touch the body of the comment.
    """
    if not text or not suffixes:
        return text
    cleaned = text.rstrip()
    changed = True
    while changed:
        changed = False
        for sfx in suffixes:
            if sfx and cleaned.endswith(sfx):
                cleaned = cleaned[: -len(sfx)].rstrip()
                changed = True
    return cleaned


def format_post(row, include_thread_content=True, suffix_strip_list=None):
    """Format a single post as factual text.

    Upvotes are reported NET on Reddit and Moltbook: both platforms auto-apply
    a +1 OP self-upvote on every post, so the raw `upvotes` column starts at 1
    for a brand-new post with zero real engagement. Strip that +1 here so the
    display matches SCORE_SQL and the dashboard. Other platforms pass through.

    `suffix_strip_list`: list of active-campaign suffix literals to strip from
    `our_content` before emitting the "Our comment:" line. Without this, the
    LLM sees historical tagged comments in the few-shot block, copies the
    suffix into its draft, and the tool-layer injection (engage_reddit,
    twitter_browser) appends a second copy. See _strip_active_campaign_suffixes.
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
    if suffix_strip_list:
        our_content = _strip_active_campaign_suffixes(our_content, suffix_strip_list)
    thread_title = row[6] or ""
    thread_content = row[7] or ""
    project = row[8] or "(no project)"
    date = row[9]
    account = row[10] or ""
    # New column 11 = clicks. Pre-2026-05-12 rows fed from older callers
    # may not include this column, so guard with len() before indexing
    # to keep this function backward-compatible with the (now-rewritten)
    # SELECT lists in this file and anywhere else still on the old shape.
    clicks = row[11] if len(row) > 11 and row[11] is not None else 0

    # Clicks lead the header because they are the ground-truth conversion
    # signal: a click means a real human actually went to the project
    # link. Upvotes/comments/views are leading indicators of attention,
    # not behavior. If a top-tier example has 0 clicks, Claude should
    # see that and weight discussion shape (comments) over "this post
    # drove traffic".
    header = (
        f"[{clicks} clicks, {upvotes} upvotes, {comments} comments, "
        f"{views} views] {row[1]} | {project} | {date}"
    )
    lines.append(header)

    if thread_title:
        lines.append(f"  Thread: {thread_title}")
    if include_thread_content and thread_content:
        snippet = thread_content.replace('\n', ' ')
        lines.append(f"  Thread body: {snippet}")
    lines.append(f"  Our comment: {our_content}")
    return "\n".join(lines)


def format_report(summary, top, bottom, project=None, platform=None,
                   top_by_group=None, fallback_top=None, style_perf=None,
                   top_by_style=None, suffix_strip_list=None):
    """Format the full report.

    `suffix_strip_list` is forwarded to every `format_post` call so
    historical campaign-tagged comments don't leak the suffix into the
    LLM's few-shot context. Passed in by `main()` after loading from the
    `campaigns` table (cached per-process).
    """
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

    # "meaningful engagement" is scored as clicks*10 + comments*3 + upvotes
    # (Reddit upvote -1), with a per-platform floor (see PLATFORM_MIN_SCORE).
    # Report it to Claude so it understands why borderline posts are/aren't
    # included. Clicks dominate because a single human click is worth more
    # than 10 upvotes of vibes when picking examples for the few-shot prompt.
    threshold_label = (
        f">= score {min_score_for(platform)} "
        f"(clicks*10 + comments*3 + upvotes, Reddit upvote -1)"
    )

    # Style performance (live from DB). Report clicks AND comments AND
    # upvotes so click-driving styles surface FIRST, discussion-driving
    # styles second, and upvote-accumulating ones last. avg_clicks (col 4)
    # is the new column; legacy callers that grouped only on upvotes/
    # comments will not see it but every caller in this repo now does.
    if style_perf:
        lines.append("### Engagement Style Performance (live data, sorted by avg clicks → avg comments)")
        for row in style_perf:
            lines.append(
                f"  {row[0]:<22} {row[1]:>5} posts  "
                f"avg_clicks={row[4]}  avg_cm={row[3]}  avg_up={row[2]}  "
                f"best_clicks={row[7]}  best_cm={row[6]}  best_up={row[5]}"
            )
        lines.append("")

    # Per-style top exemplar. The style table above is just numbers; this
    # section shows the single highest-scoring real post we have for each
    # style, so when the model picks a style it can see what a great post
    # in that style actually reads like. Ordered to match the style table
    # (avg clicks DESC) so the click-winning styles and their exemplars
    # appear first. Styles with no clean example are listed so the absence
    # is itself a signal ("this style has never landed a usable post").
    if top_by_style and style_perf:
        exemplars = _best_exemplar_per_style(top_by_style)
        lines.append(
            "### Best Example Per Style (imitate this when you pick the style)"
        )
        lines.append(
            "One real post per style — the highest-scoring one we have. "
            "Pick the style, then write something with the same shape as its example."
        )
        lines.append("")
        for row in style_perf:
            style = row[0]
            header = (
                f"#### {style}  "
                f"(n={row[1]}, avg_clicks={row[4]}, avg_cm={row[3]}, avg_up={row[2]})"
            )
            lines.append(header)
            ex = exemplars.get(style)
            if ex:
                lines.append(format_post(ex, suffix_strip_list=suffix_strip_list))
            else:
                lines.append("  (no clean example yet — style unproven or all examples filtered)")
            lines.append("")

    # Summary table. Per-project/platform now shows total_clicks (col 9)
    # so Claude can see at-a-glance which projects converted at all.
    # Projects with zero total_clicks across many posts are the canaries
    # for "this product/voice combination isn't landing" (the 'General'
    # bucket in the 7d audit on 2026-05-12: 56 posts, 0 clicks).
    lines.append("### Posts per Project per Platform")
    for row in summary:
        lines.append(
            f"  {row[0]:<20} {row[1]:<12} {row[2]:>5} posts  "
            f"avg_clicks={row[5]}  avg_cm={row[4]}  avg_up={row[3]}  "
            f"best_clicks={row[8]}  best_cm={row[7]}  best_up={row[6]}  "
            f"total_clicks={row[9]}"
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
                lines.append(format_post(p, suffix_strip_list=suffix_strip_list))
                lines.append("")
    elif top:
        # Filtered view with results
        lines.append(
            f"### Top {len(top)} Posts for {project or 'all projects'} ({threshold_label})"
        )
        for p in top:
            lines.append(format_post(p, suffix_strip_list=suffix_strip_list))
            lines.append("")
    elif fallback_top:
        # No project-specific posts met threshold — show general high performers
        platform_label = f" on {platform}" if platform else ""
        lines.append(f"### No {project} posts meeting {threshold_label}{platform_label}.")
        lines.append(f"### Showing top posts from OTHER projects{platform_label} as reference:")
        lines.append("")
        for p in fallback_top:
            lines.append(format_post(p, suffix_strip_list=suffix_strip_list))
            lines.append("")

    # Bottom posts with failure annotations
    if bottom:
        lines.append(f"### Bottom {len(bottom)} Posts (avoid these patterns)")
        for p in bottom:
            lines.append(format_post(p, include_thread_content=False,
                                      suffix_strip_list=suffix_strip_list))
            reason = annotate_failure(p)
            lines.append(f"  >> FAILURE REASON: {reason}")
            lines.append("")

    return "\n".join(lines)


def _apply_top_filter(rows, limit):
    """Anti-pattern filter applied to top-N candidates.

    PRODUCT_NAMES: hard-drop self-promotional examples regardless of
    clicks (don't teach Claude to namedrop). URL/www. mention: only
    drop when clicks==0 (a URL-bearing post with real human clicks IS
    the gold example by definition; see 2026-05-12 click-aware fix).
    Caller passes overfetched rows; we trim to `limit` after filter.
    """
    clean = []
    for r in rows:
        content = (r[5] or "")
        clicks = r[11] if len(r) > 11 and r[11] is not None else 0
        lower = content.lower()
        if any(name in lower for name in PRODUCT_NAMES):
            continue
        has_url = ("http://" in lower or "https://" in lower or "www." in lower)
        if has_url and clicks == 0:
            continue
        clean.append(r)
    return clean[:limit]


def _best_exemplar_per_style(rows):
    """Collapse the flat get_top_post_per_style() result to {style: row}.

    Each style ships up to 3 candidate rows (ranked by SCORE_SQL). Run the
    shared anti-pattern filter per style and keep the best survivor. Styles
    whose every candidate is filtered out (e.g. all product-name posts) are
    simply absent from the dict — the caller renders them with no example.
    The engagement_style key is column 12 of each row.
    """
    by_style = {}
    for r in rows:
        if len(r) <= 12 or not r[12]:
            continue
        by_style.setdefault(r[12], []).append(r)
    out = {}
    for style, group in by_style.items():
        clean = _apply_top_filter(group, 1)
        if clean:
            out[style] = clean[0]
    return out


def _fetch_report_via_api(*, platform, project, top, bottom):
    """Pull all SQL aggregations in one call via the v1 route.

    Returns (summary, style_perf, top_posts, bottom_posts,
              fallback_top|None, top_by_group|None, top_by_style). Row
    shapes match the column order format_post / format_report expect.
    """
    from http_api import api_get
    resp = api_get(
        "/api/v1/posts/top-performers-report",
        query={
            "platform": platform or "",
            "project": project or "",
            "top": str(top),
            "bottom": str(bottom),
        },
    )
    data = (resp or {}).get("data") or {}
    summary = data.get("summary") or []
    style_perf = data.get("style_perf") or []
    raw_top = data.get("top_posts") or []
    raw_bottom = data.get("bottom_posts") or []
    raw_fallback = data.get("fallback_top") or []
    raw_group = data.get("top_by_group") or {}
    top_by_style = data.get("top_by_style") or []

    top_filtered = _apply_top_filter(raw_top, top) if raw_top else []
    fallback_filtered = None
    if project and not top_filtered and raw_fallback:
        fallback_filtered = _apply_top_filter(raw_fallback, top)
    top_by_group = None
    if not project:
        top_by_group = {
            proj: _apply_top_filter(rows, 5)
            for proj, rows in raw_group.items()
        }
    return (summary, style_perf, top_filtered, raw_bottom,
            fallback_filtered, top_by_group, top_by_style)


def main():
    parser = argparse.ArgumentParser(description="Generate top performers feedback report")
    parser.add_argument("--platform", default=None, help="Filter to specific platform")
    parser.add_argument("--project", default=None, help="Filter to specific project")
    parser.add_argument("--top", type=int, default=5, help="Number of top posts to show (per group or total)")
    parser.add_argument("--bottom", type=int, default=5, help="Number of bottom posts to show")
    parser.add_argument("--style", default=None,
                        help=("Restrict per-style exemplars + perf table to the "
                              "given engagement_style(s). Accepts a single style "
                              "(data_point_drop) or comma-separated (style1,style2). "
                              "Added 2026-05-19 for the assigned-style picker rollout: "
                              "when a post_*/engage_* orchestrator assigns one style "
                              "via pick_style_for_post(), it passes that style here so "
                              "the few-shot exemplar section shows only the matching "
                              "high-scoring posts instead of every style. Summary, "
                              "fallback_top, and top_by_group are not affected."))
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    (summary, style_perf, top, bottom, fallback_top,
     top_by_group, top_by_style) = _fetch_report_via_api(
        platform=args.platform, project=args.project, top=args.top, bottom=args.bottom,
    )

    if args.style:
        wanted = {s.strip() for s in args.style.split(",") if s.strip()}
        # style_perf row col 0 = style name. top_by_style row col 12 = style name.
        style_perf = [row for row in style_perf if row and row[0] in wanted]
        top_by_style = [
            row for row in top_by_style
            if row and len(row) > 12 and row[12] in wanted
        ]

    if args.json:
        output = {
            "summary": [list(row) for row in summary],
            "top_posts": [list(row) for row in top],
            "bottom_posts": [list(row) for row in bottom],
            "fallback_top": [list(row) for row in fallback_top] if fallback_top else [],
            "top_by_style": [list(row) for row in top_by_style],
            "style_perf": [list(row) for row in style_perf],
        }
        print(json.dumps(output, indent=2, default=str))
    else:
        # Load active-campaign suffix literals so format_report can strip them
        # from every embedded `our_content` snippet. Without this, the LLM
        # downstream (post_reddit, engage_reddit, twitter Phase 2b drafting,
        # post_github) sees historical campaign-tagged comments in the
        # few-shot context, copies the suffix into its draft, and the
        # tool-layer injection appends a SECOND suffix, producing
        # "written with s4lai written with s4lai" (Reddit 2026-05-18 incident).
        # API path is preferred; legacy direct-DB path passes a conn instead.
        suffix_list = _load_active_campaign_suffixes()
        print(format_report(summary, top, bottom,
                            project=args.project, platform=args.platform,
                            top_by_group=top_by_group, fallback_top=fallback_top,
                            style_perf=style_perf, top_by_style=top_by_style,
                            suffix_strip_list=suffix_list))


if __name__ == "__main__":
    main()
