#!/usr/bin/env python3
"""Precompute dashboard stat snapshots to disk so the dashboard never cold-starts.

Writes atomic JSON snapshots under ~/social-autoposter/skill/cache/:
  - funnel_stats_<N>d.json  for N in {1, 7, 14, 30, 90}   (Top -> Pages + funnel)
  - activity_stats_24h.json                                (Activity tab counts)
  - style_stats_24h.json                                   (Style tab, all/all)

Run on a launchd timer (see com.m13v.social-precompute-stats.plist). The
/api/funnel/stats, /api/activity/stats, and /api/style/stats endpoints in
bin/server.js read these files when fresh; live queries only run on miss.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import load_env, get_conn

REPO_DIR = os.path.expanduser("~/social-autoposter")
CACHE_DIR = os.path.join(REPO_DIR, "skill", "cache")
SCRIPTS_DIR = os.path.join(REPO_DIR, "scripts")


_DB_CONN = None

def _db():
    """Shared connection for dashboard_cache upserts. Lazy so scripts that
    only precompute and don't need local disk still work when DATABASE_URL
    is missing (the upsert just no-ops)."""
    global _DB_CONN
    if _DB_CONN is not None:
        return _DB_CONN
    try:
        _DB_CONN = get_conn()
    except Exception as e:
        print(f"  [db] get_conn failed, skipping Postgres mirror: {e}", file=sys.stderr)
        _DB_CONN = False
    return _DB_CONN


def upsert_cache(key, payload):
    """Mirror a snapshot to Postgres so Cloud Run (which has no access to the
    operator's filesystem) can serve it. Silent no-op if the DB connection
    is unavailable; local disk is still the primary path for local use."""
    conn = _db()
    if not conn:
        return
    try:
        conn.execute(
            "INSERT INTO dashboard_cache (cache_key, payload, updated_at) "
            "VALUES (%s, %s::jsonb, NOW()) "
            "ON CONFLICT (cache_key) DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()",
            (key, json.dumps(payload)),
        )
        conn.commit()
    except Exception as e:
        print(f"  [db] upsert {key} failed: {e}", file=sys.stderr)
        try: conn._conn.rollback()
        except Exception: pass


def atomic_write_json(path, payload):
    """Write JSON to `path` atomically (temp file + rename). Also mirrors
    to Postgres dashboard_cache under the filename stem so hosted deploys can
    read the same snapshot."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except Exception: pass
        raise
    key = os.path.splitext(os.path.basename(path))[0]
    upsert_cache(key, payload)


def precompute_funnel(days):
    """Shell out to project_stats_json.py (it already knows how to build the
    payload and hits PostHog + bookings DB). Returns parsed JSON or None."""
    script = os.path.join(SCRIPTS_DIR, "project_stats_json.py")
    t0 = time.time()
    try:
        out = subprocess.check_output(
            ["python3", script, "--days", str(days)],
            cwd=REPO_DIR,
            env=os.environ.copy(),
            timeout=180,
        )
    except subprocess.CalledProcessError as e:
        print(f"  funnel days={days} FAILED exit={e.returncode}: {e.stderr or e.output!r}", file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print(f"  funnel days={days} TIMEOUT after 180s", file=sys.stderr)
        return None
    try:
        data = json.loads(out)
    except Exception as e:
        print(f"  funnel days={days} JSON decode failed: {e}", file=sys.stderr)
        return None
    # Match the wire shape /api/funnel/stats returns: { days, ...data, cachedAt }
    payload = {"days": days, **data, "cachedAt": int(time.time() * 1000)}
    path = os.path.join(CACHE_DIR, f"funnel_stats_{days}d.json")
    atomic_write_json(path, payload)
    elapsed = time.time() - t0
    print(f"  funnel days={days} ok ({elapsed:.1f}s) -> {path}")
    return payload


def precompute_activity(hours=24):
    """Mirror the 9-way UNION in bin/server.js /api/activity/stats."""
    conn = get_conn()
    t0 = time.time()
    win = f"INTERVAL '{int(hours)} hours'"
    norm = "CASE WHEN LOWER(pl) = 'x' THEN 'twitter' ELSE LOWER(pl) END"
    q = (
        "SELECT json_agg(row_to_json(r)) FROM ("
        "SELECT type, " + norm + " AS platform, COUNT(*)::int AS count FROM ("
        "SELECT CASE WHEN thread_url = our_url AND (thread_author IS NULL OR thread_author = our_account) THEN 'posted_thread' ELSE 'posted_comment' END AS type, platform AS pl FROM posts WHERE posted_at >= NOW() - " + win + " "
        "UNION ALL SELECT 'replied', platform FROM replies WHERE status='replied' AND replied_at >= NOW() - " + win + " "
        "UNION ALL SELECT 'skipped', platform FROM replies WHERE status='skipped' AND COALESCE(processing_at, discovered_at) >= NOW() - " + win + " "
        "UNION ALL SELECT 'mention', platform FROM octolens_mentions WHERE COALESCE(source_timestamp, received_at) >= NOW() - " + win + " "
        # dm_sent: dms whose FIRST outbound dm_message landed in the window.
        # Mirrors bin/server.js so the snapshot and live query agree. Counts
        # via dm_messages avoids public_only artifact rows that ensure_dm
        # creates with status='sent' for cross-thread prospect history.
        "UNION ALL SELECT 'dm_sent', d.platform FROM dms d WHERE EXISTS ("
        "SELECT 1 FROM dm_messages m WHERE m.dm_id = d.id AND m.direction='outbound' "
        "AND m.message_at >= NOW() - " + win + " "
        "AND NOT EXISTS (SELECT 1 FROM dm_messages m2 WHERE m2.dm_id = d.id AND m2.direction='outbound' AND m2.message_at < m.message_at)) "
        "UNION ALL SELECT 'dm_reply_sent', d.platform FROM dm_messages m JOIN dms d ON d.id = m.dm_id WHERE m.direction='outbound' AND m.message_at >= NOW() - " + win + " AND EXISTS (SELECT 1 FROM dm_messages m2 WHERE m2.dm_id = m.dm_id AND m2.direction='inbound' AND m2.message_at < m.message_at) "
        # Page-generation buckets. Mirrors bin/server.js /api/activity/stats
        # exactly. Pre-2026-05-16 a single 'page_published_serp' caught any
        # seo_keywords row whose source was not (reddit, top_page); but the
        # real SERP pipeline was unloaded 2026-04-17 and the dominant active
        # producer became the Twitter cycle page-gen A/B lane
        # (twitter_gen_links.py -> generate_page.py --trigger twitter, source=
        # 'twitter'). Split into honest twitter / misc / roundup / top_post
        # buckets so the dashboard tooltip can show real per-pipeline counts.
        "UNION ALL SELECT 'page_published_twitter', 'seo' FROM seo_keywords WHERE completed_at >= NOW() - " + win + " AND page_url IS NOT NULL AND source = 'twitter' "
        "UNION ALL SELECT 'page_published_misc', 'seo' FROM seo_keywords WHERE completed_at >= NOW() - " + win + " AND page_url IS NOT NULL AND COALESCE(source, '') NOT IN ('reddit', 'top_page', 'top_post', 'roundup', 'twitter') "
        "UNION ALL SELECT 'page_published_gsc', 'seo' FROM gsc_queries WHERE completed_at >= NOW() - " + win + " AND page_url IS NOT NULL "
        "UNION ALL SELECT 'page_published_reddit', 'seo' FROM seo_keywords WHERE completed_at >= NOW() - " + win + " AND page_url IS NOT NULL AND source='reddit' "
        "UNION ALL SELECT 'page_published_top', 'seo' FROM seo_keywords WHERE completed_at >= NOW() - " + win + " AND page_url IS NOT NULL AND source='top_page' "
        "UNION ALL SELECT 'page_published_top_post', 'seo' FROM seo_keywords WHERE completed_at >= NOW() - " + win + " AND page_url IS NOT NULL AND source='top_post' "
        "UNION ALL SELECT 'page_published_roundup', 'seo' FROM seo_keywords WHERE completed_at >= NOW() - " + win + " AND page_url IS NOT NULL AND source='roundup' "
        "UNION ALL SELECT 'page_improved', 'seo' FROM seo_page_improvements WHERE completed_at >= NOW() - " + win + " AND status='committed' "
        "UNION ALL SELECT 'resurrected', platform FROM posts WHERE resurrected_at >= NOW() - " + win + " " +
        ") u GROUP BY type, platform ORDER BY type, platform) r"
    )
    cur = conn.execute(q)
    row = cur.fetchone()
    value = (row[0] if row and row[0] else []) or []
    payload = {
        "windowHours": int(hours),
        "rows": value,
        "cachedAt": int(time.time() * 1000),
    }
    path = os.path.join(CACHE_DIR, f"activity_stats_{int(hours)}h.json")
    atomic_write_json(path, payload)
    elapsed = time.time() - t0
    print(f"  activity hours={hours} ok ({elapsed:.1f}s) -> {path}")
    return payload


def precompute_style(hours=24):
    """Mirror the engagement-style aggregate in bin/server.js /api/style/stats
    for the default all/all filter the dashboard asks for on load.

    Adds reply_* columns (replies count, reply_upvotes, reply_comments,
    reply_views) sourced from the `replies` table, joined on engagement_style.
    Existing post-side columns are unchanged — the dashboard JS keeps working;
    new fields are additive and ignored until surfaced."""
    conn = get_conn()
    t0 = time.time()
    # upvotes_discounted applies the Reddit/Moltbook -1 clamp per row before summing,
    # so the per-post score computed client-side matches top_performers.SCORE_SQL.
    # Both platforms have a default OP self-upvote that inflates the raw count.
    win = f"INTERVAL '{int(hours)} hours'"
    q_rows = (
        "WITH p AS ("
        "  SELECT COALESCE(engagement_style, '(none)') AS style, "
        "  COUNT(*)::int AS posts, "
        "  COUNT(*) FILTER (WHERE LOWER(platform) NOT IN ('moltbook', 'github', 'github_issues'))::int AS views_posts, "
        "  COALESCE(SUM(upvotes), 0)::int AS upvotes, "
        "  COALESCE(SUM(CASE WHEN LOWER(platform) IN ('reddit', 'moltbook') "
        "    THEN GREATEST(0, COALESCE(upvotes,0) - 1) "
        "    ELSE COALESCE(upvotes,0) END), 0)::int AS upvotes_discounted, "
        "  COALESCE(SUM(comments_count), 0)::int AS comments, "
        "  COALESCE(SUM(views) FILTER (WHERE LOWER(platform) NOT IN ('moltbook', 'github', 'github_issues')), 0)::int AS views, "
        # post_clicks: bot-filtered click events from post_link_clicks for
        # short links minted for these posts (post_id-keyed). Mirrors the
        # live query in bin/server.js /api/style/stats and the picker source
        # (engagement_styles._fetch_style_stats / top_performers.SCORE_SQL).
        "  COALESCE(SUM(pl.total_clicks), 0)::int AS post_clicks, "
        "  COALESCE(SUM(CASE WHEN is_recommendation THEN 1 ELSE 0 END), 0)::int AS recommendations "
        f"  FROM posts LEFT JOIN ("
        "    SELECT pl2.post_id, COUNT(plc.id)::int AS total_clicks "
        "    FROM post_links pl2 "
        "    LEFT JOIN post_link_clicks plc "
        "      ON plc.code = pl2.code AND plc.is_bot = false "
        "    WHERE pl2.post_id IS NOT NULL "
        "    GROUP BY pl2.post_id"
        f"  ) pl ON pl.post_id = posts.id WHERE posted_at >= NOW() - {win} "
        "  GROUP BY engagement_style"
        "), r AS ("
        "  SELECT COALESCE(engagement_style, '(none)') AS style, "
        "  COUNT(*)::int AS replies, "
        "  COALESCE(SUM(upvotes), 0)::int AS reply_upvotes, "
        "  COALESCE(SUM(comments_count), 0)::int AS reply_comments, "
        "  COALESCE(SUM(views) FILTER (WHERE LOWER(platform) NOT IN ('reddit', 'github', 'linkedin', 'moltbook')), 0)::int AS reply_views "
        f"  FROM replies WHERE status='replied' AND replied_at >= NOW() - {win} "
        "  GROUP BY engagement_style"
        ") "
        "SELECT json_agg(row_to_json(s)) FROM ("
        "  SELECT COALESCE(p.style, r.style) AS style, "
        "  COALESCE(p.posts, 0) AS posts, "
        "  COALESCE(p.views_posts, 0) AS views_posts, "
        "  COALESCE(p.upvotes, 0) AS upvotes, "
        "  COALESCE(p.upvotes_discounted, 0) AS upvotes_discounted, "
        "  COALESCE(p.comments, 0) AS comments, "
        "  COALESCE(p.views, 0) AS views, "
        "  COALESCE(p.post_clicks, 0) AS post_clicks, "
        "  COALESCE(p.recommendations, 0) AS recommendations, "
        "  COALESCE(r.replies, 0) AS replies, "
        "  COALESCE(r.reply_upvotes, 0) AS reply_upvotes, "
        "  COALESCE(r.reply_comments, 0) AS reply_comments, "
        "  COALESCE(r.reply_views, 0) AS reply_views "
        "  FROM p FULL OUTER JOIN r ON p.style = r.style "
        "  ORDER BY COALESCE(p.posts, 0) + COALESCE(r.replies, 0) DESC"
        ") s"
    )
    q_platforms = (
        "SELECT json_agg(p) FROM ("
        "SELECT DISTINCT LOWER(CASE WHEN LOWER(platform)='x' THEN 'twitter' ELSE platform END) AS p "
        f"FROM posts WHERE posted_at >= NOW() - INTERVAL '{int(hours)} hours' "
        "AND platform IS NOT NULL ORDER BY p) s"
    )
    q_projects = (
        "SELECT json_agg(p) FROM ("
        "SELECT DISTINCT project_name AS p FROM posts "
        f"WHERE posted_at >= NOW() - INTERVAL '{int(hours)} hours' "
        "AND project_name IS NOT NULL "
        "ORDER BY p) s"
    )
    def _one(q):
        cur = conn.execute(q)
        row = cur.fetchone()
        return (row[0] if row and row[0] else []) or []
    rows = _one(q_rows)
    platforms = _one(q_platforms)
    projects = _one(q_projects)
    payload = {
        "windowHours": int(hours),
        "platform": "all",
        "project": "all",
        "rows": rows,
        "platforms": platforms,
        "projects": projects,
        "cachedAt": int(time.time() * 1000),
    }
    path = os.path.join(CACHE_DIR, f"style_stats_{int(hours)}h.json")
    atomic_write_json(path, payload)
    elapsed = time.time() - t0
    print(f"  style hours={hours} ok ({elapsed:.1f}s) -> {path}")
    return payload


def main():
    load_env()
    os.makedirs(CACHE_DIR, exist_ok=True)

    started = datetime.now(timezone.utc).isoformat()
    print(f"=== precompute_dashboard_stats: {started} ===")
    overall_t0 = time.time()

    try:
        precompute_activity(24)
    except Exception as e:
        print(f"  activity FAILED: {e}", file=sys.stderr)

    try:
        precompute_style(24)
    except Exception as e:
        print(f"  style FAILED: {e}", file=sys.stderr)

    # Funnel snapshots: one per window the dashboard pills can show.
    #
    # The job fires every 5 min, but each funnel window re-queries every
    # PostHog bucket (~10 HogQL queries each). Recomputing all 5 windows
    # every cycle = ~5x the query burst, which trips PostHog's short-window
    # rate limiter (429 "throttled") and leaves whole buckets errored ('err'
    # on the dashboard). The longer windows barely move between 5-min cycles,
    # so only 1d + 7d refresh every cycle; 14/30/90d refresh at most every
    # ~25 min (skipped while their snapshot is still fresh). This cuts the
    # steady-state PostHog query volume by ~3/5 with no meaningful staleness.
    HEAVY_WINDOW_MIN_AGE_S = 25 * 60
    # Small gap between window runs so two adjacent window subprocesses don't
    # stack their bursts back-to-back into the rate limiter.
    INTER_WINDOW_SLEEP_S = 3
    windows = (1, 7, 14, 30, 90)
    for d in windows:
        if d >= 14:
            snap_path = os.path.join(CACHE_DIR, f"funnel_stats_{d}d.json")
            try:
                age = time.time() - os.path.getmtime(snap_path)
            except OSError:
                age = None  # missing -> always compute
            if age is not None and age < HEAVY_WINDOW_MIN_AGE_S:
                print(f"  funnel days={d} skipped (snapshot {age/60:.0f}m old < 25m)")
                continue
        try:
            precompute_funnel(d)
        except Exception as e:
            print(f"  funnel days={d} FAILED: {e}", file=sys.stderr)
        time.sleep(INTER_WINDOW_SLEEP_S)

    # Stamp a marker so ops can see when the last full cycle finished.
    atomic_write_json(
        os.path.join(CACHE_DIR, "_last_run.json"),
        {"finished_at": datetime.now(timezone.utc).isoformat(),
         "elapsed_sec": round(time.time() - overall_t0, 2)},
    )
    print(f"=== done in {time.time() - overall_t0:.1f}s ===")


if __name__ == "__main__":
    main()
