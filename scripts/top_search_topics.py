#!/usr/bin/env python3
"""Return top-performing search_topic seeds per project + platform.

This is the Reddit + GitHub feedback feed; the Twitter analog lives in
`scripts/top_twitter_queries.py`. As of 2026-05-10 the Reddit path is at
parity with the Twitter feed: it reads `reddit_candidates` instead of
`posts`, surfaces the full conversion funnel (posted/skipped sample
sizes, posted/skipped delta_score split, upvotes/comments/clicks),
and ranks by clicks first.

What changed (2026-05-10): the previous version scored only
`comments_count*3 + upvotes` from the `posts` table, missed clicks
entirely, had no posted-vs-skipped split, and could not tell the model
"this query keeps surfacing viral threads we keep skipping" — i.e. a
mismatch signal. Twitter has had that signal since the Phase 0 batch
salvage rebuild; Reddit was flying blind on which subreddits/queries
actually convert to clicks.

Reddit path (platform='reddit'):
  Source = reddit_candidates (one row per discovered thread, status in
  pending/posted/skipped/expired/failed). Joins posts via post_id for
  upvotes/comments_count and post_links via post_id for real_clicks
  (clicks are only meaningful on posted rows; the FILTER clauses gate
  to status='posted' inside the SUM).

  Fields surfaced per (search_topic, project):
    posts                    — distinct posted candidates
    posted_n                 — count(*) FILTER (status='posted')
    skipped_n                — count(*) FILTER (status IN ('skipped','expired','failed'))
    avg_delta_posted         — avg reddit_candidates.delta_score for posted rows
    avg_delta_skipped        — avg reddit_candidates.delta_score for skipped/expired/failed rows
    upvotes_total            — sum upvotes on our replies (posted only)
    comments_total           — sum comments_count on our replies (posted only)
    clicks_total             — sum post_links.real_clicks on our replies (posted only)
    composite_score          — clicks*100 + comments + upvotes (clicks dominate)

  delta_score is reddit's velocity proxy (Δup + 4*Δcomments computed
  during the T1 ripen step in ripen_reddit_plan.py). It is set on every
  ripened row regardless of eventual status, which is what lets us
  split the average by posted vs skipped — same diagnostic shape as
  Twitter's avg_virality_posted / avg_virality_skipped:
    high avg_delta_posted + many posts        → keep this query, mimic style
    high avg_delta_skipped + few posts        → on-rank but off-topic, reword
    low avg_delta_skipped + few posts         → dead supply, drop the seed

Non-reddit path (platform='github' or unset):
  Source = posts (search_topic stamped at INSERT time). Joins
  post_links via posts.id for clicks_total. Same composite + clicks-DESC
  ordering as the reddit path. Reddit-style status splits are not
  available here because GitHub posts directly without a candidates
  table.

Usage:
    python3 scripts/top_search_topics.py --project "fazm" --platform reddit
    python3 scripts/top_search_topics.py --project "fazm" --platform github
    python3 scripts/top_search_topics.py --project "fazm" --platform reddit --json
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get  # noqa: E402


def query(project=None, platform=None, window_days=30, limit=10):
    """Top-performing search_topic seeds per (project, platform).

    Migrated 2026-05-30 off direct DB (db.get_conn) onto the HTTP lane:
    GET /api/v1/search-topics/ranked?platform=&project=&window_days=&limit=.
    The route mirrors the four legacy `_query_*` SQL paths one-for-one
    (reddit / twitter / linkedin / posts-fallback), including the 2026-05-29
    cross-route guard on the twitter posted-conversion aggregates, and returns
    rows already shaped as the dicts this function used to build, so we read
    `data.rows` verbatim. There is intentionally NO direct-DB fallback.
    """
    q = {"window_days": int(window_days), "limit": int(limit)}
    if platform:
        q["platform"] = platform
    if project:
        q["project"] = project
    resp = api_get("/api/v1/search-topics/ranked", q)
    data = (resp or {}).get("data") or {}
    return list(data.get("rows") or [])


def format_text(results, project=None, platform=None, window_days=30):
    plat = (platform or "").lower()
    is_reddit = plat == "reddit"
    is_twitter = plat == "twitter"
    is_linkedin = plat == "linkedin"
    if not results:
        return (
            f"(no search_topic data yet in the last {window_days}d"
            + (f" for {project}" if project else "")
            + (f" on {platform}" if platform else "")
            + ")"
        )
    header = f"Top search_topic seeds (last {window_days}d"
    if project:
        header += f", project={project}"
    if platform:
        header += f", platform={platform}"
    if is_reddit:
        header += ", ranked by clicks_total DESC then composite (clicks×100 + comments + upvotes))"
    elif is_twitter:
        header += ", ranked by clicks_total DESC then composite (clicks×100 + likes + views×0.001))"
    elif is_linkedin:
        header += ", ranked by clicks_total DESC then composite (clicks×100 + likes + views×0.001 + velocity))"
    else:
        header += ", ranked by clicks_total DESC then composite (clicks×100 + comments×3 + upvotes))"
    lines = [header]
    if is_reddit:
        lines.append(
            f"  {'clicks':>6} {'comm':>5} {'upv':>5} "
            f"{'posts':>5} {'pN':>3} {'sN':>3} "
            f"{'Δpost':>6} {'Δskip':>6}  topic"
        )
        for r in results:
            lines.append(
                f"  {r['clicks_total']:>6} {r['comments_total']:>5} {r['upvotes_total']:>5} "
                f"{r['posts']:>5} {r['posted_n']:>3} {r['skipped_n']:>3} "
                f"{r['avg_delta_posted']:>6.1f} {r['avg_delta_skipped']:>6.1f}  {r['search_topic']}"
            )
        lines.append(
            "  (Δpost = avg ripen delta_score on posted rows; "
            "Δskip = avg ripen delta_score on skipped/expired/failed rows. "
            "High Δskip + few posts = query is on-rank but off-topic — reword. "
            "Low Δskip + few posts = dead supply, drop the seed.)"
        )
    elif is_twitter or is_linkedin:
        lines.append(
            f"  {'clicks':>6} {'views':>7} {'likes':>5} "
            f"{'posts':>5} {'pN':>3} {'sN':>3} "
            f"{'Vpost':>6} {'Vskip':>6}  topic"
        )
        for r in results:
            lines.append(
                f"  {r['clicks_total']:>6} {r['views_total']:>7} {r['likes_total']:>5} "
                f"{r['posts']:>5} {r['posted_n']:>3} {r['skipped_n']:>3} "
                f"{r['avg_virality_posted']:>6.1f} {r['avg_virality_skipped']:>6.1f}  {r['search_topic']}"
            )
        lines.append(
            "  (Vpost = avg virality_score on posted rows; "
            "Vskip = avg virality_score on skipped/expired/failed rows. "
            "High Vskip + few posts = topic finds viral noise we keep skipping - reword. "
            "Low Vskip + few posts = dead supply, drop the seed.)"
        )
    else:
        lines.append(
            f"  {'clicks':>6} {'comm':>5} {'upv':>5} {'posts':>5}  topic"
        )
        for r in results:
            lines.append(
                f"  {r['clicks_total']:>6} {r['comments_total']:>5} {r['upvotes_total']:>5} "
                f"{r['posts']:>5}  {r['search_topic']}"
            )
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=None)
    ap.add_argument("--platform", default=None)
    ap.add_argument("--window-days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--json", action="store_true", help="Output JSON instead of text")
    args = ap.parse_args()

    results = query(args.project, args.platform, args.window_days, args.limit)
    if args.json:
        json.dump(results, sys.stdout)
        sys.stdout.write("\n")
    else:
        print(format_text(results, args.project, args.platform, args.window_days))


if __name__ == "__main__":
    main()
