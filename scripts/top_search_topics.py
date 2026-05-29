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
import db as dbmod


# --- non-reddit (posts-based) path -----------------------------------------
# composite: clicks dominate (×100), comments mid (×3 collapsed into +comments
# since post_links.real_clicks already encodes downstream conversion strength
# better than comments-as-virality-proxy used to). upvotes faint.
NON_REDDIT_COMPOSITE_SQL = (
    "(COALESCE(SUM(pl.real_clicks), 0) * 100"
    " + COALESCE(SUM(p.comments_count), 0) * 3"
    " + CASE WHEN LOWER(p.platform) IN ('reddit', 'moltbook') "
    "        THEN GREATEST(0, COALESCE(SUM(p.upvotes), 0) - COUNT(*)) "
    "        ELSE COALESCE(SUM(p.upvotes), 0) END)"
)


def _query_reddit(conn, project, window_days, limit):
    """Reddit-specific path: joins reddit_candidates, splits posted vs skipped.

    Click attribution: real clicks come from `post_link_clicks` (per-hit log
    joined via `pl.code = plc.code`, COUNT WHERE is_bot=false). The legacy
    `post_links.real_clicks` column is a stale PostHog-backfill rollup and is
    permanently 0 for reddit (the backfill never ran for the reddit rail).
    Using the per-hit log here matches what the dashboard shows on the Top
    Comments tab and recovers the ~10x of clicks that were silently lost.
    Bug observed 2026-05-10: pl.real_clicks reported 0/30d, per-hit log
    reported 10/30d for reddit; twitter same column underreported 148 vs
    actual 1028. Always join through plc to count clicks.
    """
    where_proj = ""
    params = [str(window_days)]
    if project:
        where_proj = "AND LOWER(c.matched_project) = LOWER(%s)"
        params.append(project)
    params.append(int(limit))

    # NOTE: a candidate can have multiple post_link rows (rare, but possible
    # if a draft was redrafted then re-minted). We aggregate at the candidate
    # row by counting distinct (plc.code, plc.id) pairs through the join.
    sql = f"""
        SELECT c.search_topic AS search_topic,
               c.matched_project AS project_name,
               COUNT(DISTINCT c.post_id) FILTER (WHERE c.status='posted' AND c.post_id IS NOT NULL) AS posts,
               COUNT(DISTINCT c.id) FILTER (WHERE c.status='posted') AS posted_n,
               COUNT(DISTINCT c.id) FILTER (WHERE c.status IN ('skipped','expired','failed')) AS skipped_n,
               AVG(c.delta_score) FILTER (WHERE c.status='posted')                        AS avg_delta_posted,
               AVG(c.delta_score) FILTER (WHERE c.status IN ('skipped','expired','failed')) AS avg_delta_skipped,
               COALESCE(SUM(p.upvotes)        FILTER (WHERE c.status='posted'), 0) AS upvotes_total,
               COALESCE(SUM(p.comments_count) FILTER (WHERE c.status='posted'), 0) AS comments_total,
               COUNT(plc.id) FILTER (WHERE c.status='posted' AND plc.is_bot = false) AS clicks_total,
               (COUNT(plc.id) FILTER (WHERE c.status='posted' AND plc.is_bot = false) * 100
                + COALESCE(SUM(p.comments_count) FILTER (WHERE c.status='posted'), 0)
                + COALESCE(SUM(p.upvotes)        FILTER (WHERE c.status='posted'), 0)) AS composite_score,
               MAX(c.posted_at)                   AS last_posted
        FROM reddit_candidates c
        LEFT JOIN posts      p   ON p.id  = c.post_id
        LEFT JOIN post_links pl  ON pl.post_id = c.post_id
        LEFT JOIN post_link_clicks plc ON plc.code = pl.code
        WHERE c.discovered_at > NOW() - (%s || ' days')::interval
          AND c.search_topic IS NOT NULL
          AND c.search_topic <> ''
          {where_proj}
        GROUP BY c.search_topic, c.matched_project
        HAVING COUNT(DISTINCT c.id) FILTER (WHERE c.status='posted') > 0
            OR COUNT(DISTINCT c.id) FILTER (WHERE c.status IN ('skipped','expired','failed')) > 0
        ORDER BY clicks_total DESC, composite_score DESC, posts DESC, last_posted DESC NULLS LAST
        LIMIT %s
    """
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "search_topic": r[0],
            "project": r[1],
            "posts": int(r[2] or 0),
            "posted_n": int(r[3] or 0),
            "skipped_n": int(r[4] or 0),
            "avg_delta_posted": round(float(r[5] or 0), 2),
            "avg_delta_skipped": round(float(r[6] or 0), 2),
            "upvotes_total": int(r[7] or 0),
            "comments_total": int(r[8] or 0),
            "clicks_total": int(r[9] or 0),
            "composite_score": round(float(r[10] or 0), 2),
            "last_used": r[11].isoformat() if r[11] else None,
        }
        for r in rows
    ]


def _query_twitter(conn, project, window_days, limit):
    """Twitter-specific path: joins twitter_candidates, splits posted vs skipped.

    Mirrors _query_reddit: reads search_topic from twitter_candidates (NOT
    posts), because posts.search_topic is currently NULL on every Twitter
    post — the candidate->post path in twitter_post_plan.py / log_post.py
    drops the field. Aggregating from candidates is the same pattern Reddit
    uses and recovers full topic-level attribution.

    Quality split uses virality_score (set on every candidate at discovery
    by score_twitter_candidates.py) rather than delta_score. delta_score is
    only set if t1_checked_at fired, so it's NULL on a large fraction of
    skipped/expired rows, which would bias the avg toward posted-only.
    virality_score is set unconditionally at discovery.

    Supply join (2026-05-27): FULL OUTER JOIN twitter_search_attempts so
    every topic the scanner ATTEMPTED is visible to the picker, even when
    every tweet got stale-age-skipped in score_twitter_candidates.py BEFORE
    a candidate row was written (those skips currently `continue` without
    INSERT, so the candidate-only join previously missed them entirely).
    New fields per row:
      attempts_n           — how many times this topic was searched
      tweets_found_total   — sum of raw scrape counts across attempts
      zero_supply_attempts — attempts where X returned 0 tweets
    Together they let pick_search_topic separate FIT failure
    (tweets_found>0 but posted_n=0) from SUPPLY failure
    (tweets_found_total=0 across N attempts). FIT gets a heavy weight
    penalty, SUPPLY gets a milder one (partly outside our control).
    """
    where_proj_c = ""
    where_proj_a = ""
    params = [str(window_days)]
    if project:
        where_proj_c = "AND LOWER(c.matched_project) = LOWER(%s)"
        params.append(project)
    params.append(str(window_days))
    if project:
        where_proj_a = "AND LOWER(a.project_name) = LOWER(%s)"
        params.append(project)
    params.append(int(limit))

    # Cross-route guard (2026-05-29): the prep step can re-route a candidate to
    # a different project than the one whose query surfaced it (e.g. a broad
    # invented Podlog query with "codebase" surfaces a Claude Code thread that
    # gets routed to fazm). When that happens posts.project_name is the NEW
    # project but twitter_candidates.matched_project (our GROUP key here) stays
    # the origin, because the mark_posted API route does not rewrite it.
    # Counting that post's conversion (posts/clicks/likes/views) under the
    # origin project would credit a conversion that actually happened on a
    # different product, nudging pick_search_topic to favour the wrong topic.
    # So every POSTED-conversion aggregate requires the post's project to match
    # the candidate's matched_project. NULL post project is treated as
    # same-project so legacy rows (project_name stamped later) are not dropped.
    # Skip/attempt signals are NOT guarded: they are discovery facts, not
    # project-routed conversions.
    _posted = ("c.status='posted' AND (p.project_name IS NULL "
               "OR LOWER(p.project_name) = LOWER(c.matched_project))")

    sql = f"""
        WITH cand_agg AS (
            SELECT c.search_topic AS search_topic,
                   c.matched_project AS project_name,
                   COUNT(DISTINCT c.post_id) FILTER (WHERE {_posted} AND c.post_id IS NOT NULL) AS posts,
                   COUNT(DISTINCT c.id) FILTER (WHERE {_posted}) AS posted_n,
                   COUNT(DISTINCT c.id) FILTER (WHERE c.status IN ('skipped','expired','failed')) AS skipped_n,
                   AVG(c.virality_score) FILTER (WHERE {_posted})                                 AS avg_virality_posted,
                   AVG(c.virality_score) FILTER (WHERE c.status IN ('skipped','expired','failed'))   AS avg_virality_skipped,
                   COALESCE(SUM(p.views)   FILTER (WHERE {_posted}), 0) AS views_total,
                   COALESCE(SUM(p.upvotes) FILTER (WHERE {_posted}), 0) AS likes_total,
                   COUNT(plc.id) FILTER (WHERE {_posted} AND plc.is_bot = false) AS clicks_total,
                   (COUNT(plc.id) FILTER (WHERE {_posted} AND plc.is_bot = false) * 100
                    + COALESCE(SUM(p.upvotes) FILTER (WHERE {_posted}), 0)
                    + COALESCE(SUM(p.views)   FILTER (WHERE {_posted}), 0) * 0.001) AS composite_score,
                   MAX(c.posted_at) AS last_posted
              FROM twitter_candidates c
              LEFT JOIN posts            p   ON p.id = c.post_id
              LEFT JOIN post_links       pl  ON pl.post_id = c.post_id
              LEFT JOIN post_link_clicks plc ON plc.code = pl.code
             WHERE c.discovered_at > NOW() - (%s || ' days')::interval
               AND c.search_topic IS NOT NULL
               AND c.search_topic <> ''
               {where_proj_c}
             GROUP BY c.search_topic, c.matched_project
        ),
        attempt_agg AS (
            SELECT a.search_topic AS search_topic,
                   a.project_name AS project_name,
                   COUNT(*)::int AS attempts_n,
                   COALESCE(SUM(a.tweets_found), 0)::int AS tweets_found_total,
                   COUNT(*) FILTER (WHERE COALESCE(a.tweets_found, 0) = 0)::int AS zero_supply_attempts
              FROM twitter_search_attempts a
             WHERE a.ran_at > NOW() - (%s || ' days')::interval
               AND a.search_topic IS NOT NULL
               AND a.search_topic <> ''
               {where_proj_a}
             GROUP BY a.search_topic, a.project_name
        )
        SELECT COALESCE(c.search_topic, a.search_topic) AS search_topic,
               COALESCE(c.project_name, a.project_name) AS project_name,
               COALESCE(c.posts, 0) AS posts,
               COALESCE(c.posted_n, 0) AS posted_n,
               COALESCE(c.skipped_n, 0) AS skipped_n,
               COALESCE(c.avg_virality_posted, 0) AS avg_virality_posted,
               COALESCE(c.avg_virality_skipped, 0) AS avg_virality_skipped,
               COALESCE(c.views_total, 0) AS views_total,
               COALESCE(c.likes_total, 0) AS likes_total,
               COALESCE(c.clicks_total, 0) AS clicks_total,
               COALESCE(c.composite_score, 0) AS composite_score,
               c.last_posted,
               COALESCE(a.attempts_n, 0) AS attempts_n,
               COALESCE(a.tweets_found_total, 0) AS tweets_found_total,
               COALESCE(a.zero_supply_attempts, 0) AS zero_supply_attempts
          FROM cand_agg c
          FULL OUTER JOIN attempt_agg a
            ON c.search_topic = a.search_topic
           AND c.project_name = a.project_name
         ORDER BY clicks_total DESC, composite_score DESC, posts DESC, last_posted DESC NULLS LAST
         LIMIT %s
    """
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "search_topic": r[0],
            "project": r[1],
            "posts": int(r[2] or 0),
            "posted_n": int(r[3] or 0),
            "skipped_n": int(r[4] or 0),
            "avg_virality_posted": round(float(r[5] or 0), 2),
            "avg_virality_skipped": round(float(r[6] or 0), 2),
            "views_total": int(r[7] or 0),
            "likes_total": int(r[8] or 0),
            "clicks_total": int(r[9] or 0),
            "composite_score": round(float(r[10] or 0), 2),
            "last_used": r[11].isoformat() if r[11] else None,
            "attempts_n": int(r[12] or 0),
            "tweets_found_total": int(r[13] or 0),
            "zero_supply_attempts": int(r[14] or 0),
        }
        for r in rows
    ]


def _query_linkedin(conn, project, window_days, limit):
    """LinkedIn-specific path: mirrors Twitter's topic supply/conversion split.

    LinkedIn attempts log both the assigned search_topic and the literal query.
    For picker weighting, the topic is the durable unit: attempts measure supply
    and candidate rows measure whether that supply converted into posted or
    skipped opportunities.
    """
    try:
        from linkedin_search_topic_schema import ensure as ensure_linkedin_schema
        ensure_linkedin_schema(conn)
    except Exception:
        pass

    where_proj_c = ""
    where_proj_a = ""
    params = [str(window_days)]
    if project:
        where_proj_c = "AND LOWER(c.matched_project) = LOWER(%s)"
        params.append(project)
    params.append(str(window_days))
    if project:
        where_proj_a = "AND LOWER(a.project_name) = LOWER(%s)"
        params.append(project)
    params.append(int(limit))

    sql = f"""
        WITH cand_agg AS (
            SELECT c.search_topic AS search_topic,
                   c.matched_project AS project_name,
                   COUNT(DISTINCT c.id) FILTER (WHERE c.status='posted') AS posts,
                   COUNT(DISTINCT c.id) FILTER (WHERE c.status='posted') AS posted_n,
                   COUNT(DISTINCT c.id) FILTER (WHERE c.status IN ('skipped','expired','failed')) AS skipped_n,
                   AVG(c.velocity_score) FILTER (WHERE c.status='posted')                          AS avg_virality_posted,
                   AVG(c.velocity_score) FILTER (WHERE c.status IN ('skipped','expired','failed')) AS avg_virality_skipped,
                   COALESCE(SUM(p.views)   FILTER (WHERE c.status='posted'), 0) AS views_total,
                   COALESCE(SUM(p.upvotes) FILTER (WHERE c.status='posted'), 0) AS likes_total,
                   COUNT(plc.id) FILTER (WHERE c.status='posted' AND plc.is_bot = false) AS clicks_total,
                   (COUNT(plc.id) FILTER (WHERE c.status='posted' AND plc.is_bot = false) * 100
                    + COALESCE(SUM(p.upvotes) FILTER (WHERE c.status='posted'), 0)
                    + COALESCE(SUM(p.views)   FILTER (WHERE c.status='posted'), 0) * 0.001
                    + COALESCE(AVG(c.velocity_score) FILTER (WHERE c.status='posted'), 0)) AS composite_score,
                   MAX(c.posted_at) AS last_posted
              FROM linkedin_candidates c
              LEFT JOIN posts            p   ON p.id = c.post_id
              LEFT JOIN post_links       pl  ON pl.post_id = c.post_id
              LEFT JOIN post_link_clicks plc ON plc.code = pl.code
             WHERE c.discovered_at > NOW() - (%s || ' days')::interval
               AND c.search_topic IS NOT NULL
               AND c.search_topic <> ''
               {where_proj_c}
             GROUP BY c.search_topic, c.matched_project
        ),
        attempt_agg AS (
            SELECT a.search_topic AS search_topic,
                   a.project_name AS project_name,
                   COUNT(*)::int AS attempts_n,
                   COALESCE(SUM(COALESCE(a.candidates_found, 0)
                                + COALESCE(a.candidates_dropped_below_floor, 0)), 0)::int
                                AS tweets_found_total,
                   COUNT(*) FILTER (
                       WHERE COALESCE(a.candidates_found, 0)
                           + COALESCE(a.candidates_dropped_below_floor, 0) = 0
                   )::int AS zero_supply_attempts
              FROM linkedin_search_attempts a
             WHERE a.ran_at > NOW() - (%s || ' days')::interval
               AND a.search_topic IS NOT NULL
               AND a.search_topic <> ''
               {where_proj_a}
             GROUP BY a.search_topic, a.project_name
        )
        SELECT COALESCE(c.search_topic, a.search_topic) AS search_topic,
               COALESCE(c.project_name, a.project_name) AS project_name,
               COALESCE(c.posts, 0) AS posts,
               COALESCE(c.posted_n, 0) AS posted_n,
               COALESCE(c.skipped_n, 0) AS skipped_n,
               COALESCE(c.avg_virality_posted, 0) AS avg_virality_posted,
               COALESCE(c.avg_virality_skipped, 0) AS avg_virality_skipped,
               COALESCE(c.views_total, 0) AS views_total,
               COALESCE(c.likes_total, 0) AS likes_total,
               COALESCE(c.clicks_total, 0) AS clicks_total,
               COALESCE(c.composite_score, 0) AS composite_score,
               c.last_posted,
               COALESCE(a.attempts_n, 0) AS attempts_n,
               COALESCE(a.tweets_found_total, 0) AS tweets_found_total,
               COALESCE(a.zero_supply_attempts, 0) AS zero_supply_attempts
          FROM cand_agg c
          FULL OUTER JOIN attempt_agg a
            ON c.search_topic = a.search_topic
           AND c.project_name = a.project_name
         ORDER BY clicks_total DESC, composite_score DESC, posts DESC, last_posted DESC NULLS LAST
         LIMIT %s
    """
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "search_topic": r[0],
            "project": r[1],
            "posts": int(r[2] or 0),
            "posted_n": int(r[3] or 0),
            "skipped_n": int(r[4] or 0),
            "avg_virality_posted": round(float(r[5] or 0), 2),
            "avg_virality_skipped": round(float(r[6] or 0), 2),
            "views_total": int(r[7] or 0),
            "likes_total": int(r[8] or 0),
            "clicks_total": int(r[9] or 0),
            "composite_score": round(float(r[10] or 0), 2),
            "last_used": r[11].isoformat() if r[11] else None,
            "attempts_n": int(r[12] or 0),
            "tweets_found_total": int(r[13] or 0),
            "zero_supply_attempts": int(r[14] or 0),
        }
        for r in rows
    ]


def _query_posts(conn, project, platform, window_days, limit):
    """Non-reddit path (github + fallback): posts-based, posts.platform filter.

    Click attribution: same per-hit-log fix as the reddit path. We join
    `post_link_clicks` via `pl.code = plc.code` and COUNT WHERE is_bot=false.
    `post_links.real_clicks` (the legacy PostHog backfill column) is wildly
    inaccurate (twitter reports ~7x undercount, reddit ~∞x), so we don't use
    it. Each post can have multiple post_link rows; COUNT(DISTINCT p.id) is
    the post tally, COUNT(plc.id) is the click tally.
    """
    filters = [
        "p.search_topic IS NOT NULL",
        "p.search_topic <> ''",
        f"p.posted_at > NOW() - INTERVAL '{int(window_days)} days'",
    ]
    params = []
    if project:
        filters.append("LOWER(p.project_name) = LOWER(%s)")
        params.append(project)
    if platform:
        filters.append("LOWER(p.platform) = LOWER(%s)")
        params.append(platform)
    where = " AND ".join(filters)
    sql = (
        f"SELECT p.search_topic, "
        f"       COUNT(DISTINCT p.id) AS posts, "
        f"       COUNT(plc.id) FILTER (WHERE plc.is_bot = false) AS clicks_total, "
        f"       COALESCE(SUM(p.comments_count), 0) AS comments_total, "
        f"       COALESCE(SUM(p.upvotes), 0) AS upvotes_total, "
        f"       (COUNT(plc.id) FILTER (WHERE plc.is_bot = false) * 100 "
        f"        + COALESCE(SUM(p.comments_count), 0) * 3 "
        f"        + COALESCE(SUM(p.upvotes), 0)) AS composite_score, "
        f"       MAX(p.posted_at) AS last_used "
        f"FROM posts p "
        f"LEFT JOIN post_links pl ON pl.post_id = p.id "
        f"LEFT JOIN post_link_clicks plc ON plc.code = pl.code "
        f"WHERE {where} "
        f"GROUP BY p.search_topic "
        f"ORDER BY clicks_total DESC, composite_score DESC, posts DESC, last_used DESC NULLS LAST "
        f"LIMIT %s"
    )
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "search_topic": r[0],
            "posts": int(r[1] or 0),
            "clicks_total": int(r[2] or 0),
            "comments_total": int(r[3] or 0),
            "upvotes_total": int(r[4] or 0),
            "composite_score": round(float(r[5] or 0), 2),
            "last_used": r[6].isoformat() if r[6] else None,
        }
        for r in rows
    ]


def query(project=None, platform=None, window_days=30, limit=10):
    dbmod.load_env()
    conn = dbmod.get_conn()
    try:
        plat = (platform or "").lower()
        if plat == "reddit":
            results = _query_reddit(conn, project, window_days, limit)
        elif plat == "twitter":
            results = _query_twitter(conn, project, window_days, limit)
        elif plat == "linkedin":
            results = _query_linkedin(conn, project, window_days, limit)
        else:
            results = _query_posts(conn, project, platform, window_days, limit)
    finally:
        conn.close()
    return results


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
