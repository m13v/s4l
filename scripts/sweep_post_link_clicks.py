#!/usr/bin/env python3.11
"""
sweep_post_link_clicks.py — behavioral bot-flagger for short-link click logs.

Runs in addition to the per-hit UA regex in @m13v/seo-components. The UA
regex catches obvious crawlers; this sweep catches everything that looks
human in isolation but stops looking human when you correlate hits across
ip_hash + code + post + time.

Rules (all idempotent — re-running won't double-flag):

  Tier 1 (zero false positives):

    R1  same ip_hash + same code + >=3 hits
        Real users do not reload the same /r/<code> three times. Catches
        axios/python loops on a single short link, captive-portal probes,
        and CDN warm-up tools that hit the redirect repeatedly.

    R2  clicks on a post exceed views * platform_ctr_ceiling
        Twitter view count is a hard ceiling. Reddit upvote-count posts
        with 200 views can't honestly produce 150 short-link clicks. We
        cap clicks at views * ceiling and flag the excess (oldest-first
        among already-suspect rows, then any single-IP repeats, then
        no-referrer rows).

    R3  same ip_hash hits >=5 different codes within 24h
        A single fingerprint sweeping the /r/* namespace is a crawler.
        Real users tap one or two of our links a day at most.

  Tier 2 (very low false positives, applied after Tier 1):

    R4  no referrer + browser-looking UA + ip_hash also has another
        bot-flagged hit
        Twitter app and Twitter web both set Referer. A naked GET with
        no referrer from an ip_hash we already partly suspect is bot-y.

    R5  same ip_hash hits >=4 different codes within 60 seconds
        Burst fan-out across codes. A real human can't tap that fast.

Each flipped row records the rule in `bot_reason` so we can audit and
roll back per-rule if a false positive shows up.

After flipping, the script rebuilds `post_links.clicks` from the per-hit
log so the dashboard counter matches reality.

Usage:
  scripts/sweep_post_link_clicks.py [--dry-run] [--lookback-hours N]
                                    [--rules R1,R2,R3,R4,R5]

  --lookback-hours N   only consider clicks newer than N hours (default 720
                       on first run, 6 in cron mode; pass --cron to use 6)
  --cron               quick-sweep mode: looks back 6h, doesn't rebuild the
                       counter from scratch; meant for the launchd timer
  --rebuild-counter    force a full SUM(NOT is_bot) rebuild of
                       post_links.clicks across all codes (expensive but
                       safe; cron does NOT do this — it just decrements
                       for newly-flipped rows)

Idempotent: only flips rows where is_bot=false today; never un-flips.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, Tuple

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_DIR)

from scripts import db as dbmod  # noqa: E402


# Per-platform plausible upper bound on short-link CTR (clicks / views).
# These are deliberately generous — we'd rather miss a few bots than flag
# any legitimate viral post. Floor of 5 raw clicks before the rule even
# considers a post (so a 1-view tweet with 1 click can't trigger).
CTR_CEILING_BY_PLATFORM = {
    "twitter":      0.20,  # 20%; very-engaged reply context can spike
    "x":            0.20,
    "reddit":       0.30,
    "moltbook":     0.30,
    "linkedin":     0.10,
    "github":       0.40,  # tiny audiences, link is the main reason to click
    "github_issues":0.40,
    # default fallback below
}
CTR_CEILING_DEFAULT = 0.30
CTR_RAW_CLICK_FLOOR = 5         # don't apply R2 below this many clicks
CTR_VIEW_FLOOR      = 30        # don't apply R2 if views < 30 (noise)


def fetch_count(conn, sql: str, params: tuple = ()) -> int:
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return 0
    # DictCursor row supports both index and key
    return int(row[0]) if row[0] is not None else 0


def ensure_bot_reason_column(conn) -> None:
    """Add bot_reason TEXT column if it doesn't exist. Idempotent."""
    cur = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='post_link_clicks' AND column_name='bot_reason'"
    )
    if cur.fetchone() is None:
        conn.execute("ALTER TABLE post_link_clicks ADD COLUMN bot_reason TEXT")
        conn.commit()
        print("[init] added post_link_clicks.bot_reason column", flush=True)


def apply_rule_r1(conn, since_iso: str, dry_run: bool) -> int:
    """R1: same ip_hash + same code + >=3 hits inside a 240s sliding window → bot.

    Window: a row is bursty if there are >=3 hits (same ip+code, including
    itself) within +/-120s of it. That's the scripted-loop signature.

    Replaces the old "any 3 hits ever from same ip+code" rule (2026-05-10),
    which over-flagged real Twitter-for-iPhone in-app users who tap a link,
    close the in-app browser, scroll back, and re-tap minutes later. A 24h
    audit showed ~300 hits/day were misclassified by the old rule.

    Only the rows actually inside the burst get flagged; isolated re-taps
    hours apart from the same fingerprint stay as humans.
    """
    sql = """
    WITH events AS (
      SELECT id,
        COUNT(*) OVER (
          PARTITION BY ip_hash, code
          ORDER BY ts
          RANGE BETWEEN INTERVAL '120 seconds' PRECEDING
                    AND INTERVAL '120 seconds' FOLLOWING
        ) AS hits_in_window
      FROM post_link_clicks
      WHERE ts >= %s AND ip_hash IS NOT NULL
    ),
    burst_ids AS (SELECT id FROM events WHERE hits_in_window >= 3)
    UPDATE post_link_clicks plc
       SET is_bot = true,
           bot_reason = COALESCE(bot_reason, 'R1:burst-3-in-240s')
      FROM burst_ids b
     WHERE plc.id = b.id
       AND plc.is_bot = false
    """
    if dry_run:
        sel = """
        WITH events AS (
          SELECT id, is_bot,
            COUNT(*) OVER (
              PARTITION BY ip_hash, code
              ORDER BY ts
              RANGE BETWEEN INTERVAL '120 seconds' PRECEDING
                        AND INTERVAL '120 seconds' FOLLOWING
            ) AS hits_in_window
          FROM post_link_clicks
          WHERE ts >= %s AND ip_hash IS NOT NULL
        )
        SELECT COUNT(*) FROM events
         WHERE hits_in_window >= 3 AND is_bot = false
        """
        return fetch_count(conn, sel, (since_iso,))
    conn.execute(sql, (since_iso,))
    n = fetch_count(conn, "SELECT COUNT(*) FROM post_link_clicks WHERE bot_reason = 'R1:burst-3-in-240s' AND ts >= %s", (since_iso,))
    return n


def apply_rule_r2(conn, since_iso: str, dry_run: bool) -> int:
    """R2: per post, clicks > views * platform_ctr_ceiling → flag excess.

    Strategy per offending post:
      1. Compute allowed = max(views * ceiling, CTR_RAW_CLICK_FLOOR).
      2. Total raw rows R for this post in the window.
      3. Already-bot rows B; humans H = R - B.
      4. Excess E = max(0, H - allowed).
      5. Flip the E most-suspect human rows to bot.
         Suspect priority: rows whose ip_hash has >=2 hits across our log
         (more is more bot-like), then no-referrer rows, then oldest first.
    """
    flipped = 0
    # Find candidate posts: any post with views >= floor and human clicks
    # exceeding the ceiling.
    cur = conn.execute(
        """
        SELECT p.id, COALESCE(LOWER(p.platform), '') AS platform,
               COALESCE(p.views, 0) AS views,
               COUNT(*) FILTER (WHERE NOT plc.is_bot) AS humans
        FROM posts p
        JOIN post_links pl ON pl.post_id = p.id
        JOIN post_link_clicks plc ON plc.code = pl.code
        WHERE plc.ts >= %s
          AND COALESCE(p.views, 0) >= %s
        GROUP BY p.id, p.platform, p.views
        HAVING COUNT(*) FILTER (WHERE NOT plc.is_bot) >= %s
        """,
        (since_iso, CTR_VIEW_FLOOR, CTR_RAW_CLICK_FLOOR),
    )
    rows = cur.fetchall()

    for r in rows:
        post_id = r["id"]
        platform = (r["platform"] or "").lower().strip()
        if platform == "x":
            platform = "twitter"
        ceiling = CTR_CEILING_BY_PLATFORM.get(platform, CTR_CEILING_DEFAULT)
        views = int(r["views"] or 0)
        humans = int(r["humans"] or 0)
        # Allowed humans = ceiling * views, but never below the raw floor
        allowed = max(int(views * ceiling), CTR_RAW_CLICK_FLOOR)
        excess = humans - allowed
        if excess <= 0:
            continue

        # Pick the `excess` most-suspect human rows for this post.
        # Suspicion score: rows whose ip_hash repeats (across all log)
        # rank highest, then no-referrer, then oldest.
        sel = conn.execute(
            """
            WITH ip_repeats AS (
              SELECT ip_hash, COUNT(*) AS hits
              FROM post_link_clicks
              GROUP BY ip_hash
            )
            SELECT plc.id
              FROM post_link_clicks plc
              JOIN post_links pl ON pl.code = plc.code
         LEFT JOIN ip_repeats ir ON ir.ip_hash = plc.ip_hash
             WHERE pl.post_id = %s
               AND plc.is_bot = false
               AND plc.ts >= %s
          ORDER BY COALESCE(ir.hits, 1) DESC,
                   (plc.referrer IS NULL OR plc.referrer = '') DESC,
                   plc.ts ASC
             LIMIT %s
            """,
            (post_id, since_iso, excess),
        )
        ids = [int(rr["id"]) for rr in sel.fetchall()]
        if not ids:
            continue
        if dry_run:
            flipped += len(ids)
            continue
        conn.execute(
            "UPDATE post_link_clicks "
            "   SET is_bot = true, "
            "       bot_reason = COALESCE(bot_reason, 'R2:exceeds-views-ctr') "
            " WHERE id = ANY(%s) AND is_bot = false",
            (ids,),
        )
        flipped += len(ids)
    if not dry_run:
        conn.commit()
    return flipped


def apply_rule_r3(conn, since_iso: str, dry_run: bool) -> int:
    """R3: same ip_hash hits >=5 different codes within 24h → bot."""
    sql = """
    WITH crawlers AS (
      SELECT ip_hash
      FROM post_link_clicks
      WHERE ts >= %s AND ip_hash IS NOT NULL
      GROUP BY ip_hash
      HAVING COUNT(DISTINCT code) >= 5
    )
    UPDATE post_link_clicks plc
       SET is_bot = true,
           bot_reason = COALESCE(bot_reason, 'R3:ip-fanout-codes')
      FROM crawlers c
     WHERE plc.ip_hash = c.ip_hash
       AND plc.is_bot = false
       AND plc.ts >= %s
    """
    if dry_run:
        sel = """
        WITH crawlers AS (
          SELECT ip_hash FROM post_link_clicks
          WHERE ts >= %s AND ip_hash IS NOT NULL
          GROUP BY ip_hash HAVING COUNT(DISTINCT code) >= 5
        )
        SELECT COUNT(*) FROM post_link_clicks plc
        JOIN crawlers c USING (ip_hash)
        WHERE plc.is_bot = false AND plc.ts >= %s
        """
        return fetch_count(conn, sel, (since_iso, since_iso))
    conn.execute(sql, (since_iso, since_iso))
    n = fetch_count(conn, "SELECT COUNT(*) FROM post_link_clicks WHERE bot_reason = 'R3:ip-fanout-codes' AND ts >= %s", (since_iso,))
    return n


def apply_rule_r4(conn, since_iso: str, dry_run: bool) -> int:
    """R4: no referrer + browser-looking UA + ip_hash co-occurs with bot rows."""
    sql = """
    WITH dirty_ips AS (
      SELECT DISTINCT ip_hash
      FROM post_link_clicks
      WHERE is_bot = true AND ip_hash IS NOT NULL
    )
    UPDATE post_link_clicks plc
       SET is_bot = true,
           bot_reason = COALESCE(bot_reason, 'R4:no-ref-dirty-ip')
      FROM dirty_ips d
     WHERE plc.ip_hash = d.ip_hash
       AND plc.is_bot = false
       AND plc.ts >= %s
       AND (plc.referrer IS NULL OR plc.referrer = '')
       AND plc.user_agent ILIKE 'Mozilla/%%'
    """
    if dry_run:
        sel = """
        WITH dirty_ips AS (
          SELECT DISTINCT ip_hash FROM post_link_clicks WHERE is_bot=true AND ip_hash IS NOT NULL
        )
        SELECT COUNT(*) FROM post_link_clicks plc
        JOIN dirty_ips d USING (ip_hash)
        WHERE plc.is_bot=false AND plc.ts >= %s
          AND (plc.referrer IS NULL OR plc.referrer = '')
          AND plc.user_agent ILIKE 'Mozilla/%%'
        """
        return fetch_count(conn, sel, (since_iso,))
    conn.execute(sql, (since_iso,))
    n = fetch_count(conn, "SELECT COUNT(*) FROM post_link_clicks WHERE bot_reason = 'R4:no-ref-dirty-ip' AND ts >= %s", (since_iso,))
    return n


def apply_rule_r5(conn, since_iso: str, dry_run: bool) -> int:
    """R5: same ip_hash hits >=4 different codes within any 60-second window.

    Postgres doesn't support COUNT(DISTINCT) over a window, so we bucket by
    30-second floor + ip_hash and look at adjacent buckets via self-join
    (a hit at second 29 and a hit at second 31 should count together).
    """
    # Step 1: per (ip_hash, 30s-bucket), distinct-code count.
    # Step 2: a row counts as bursty if its bucket OR an adjacent bucket
    # (same ip_hash, +/- 1) collectively has >=4 distinct codes.
    sql = """
    WITH buckets AS (
      SELECT ip_hash,
             FLOOR(EXTRACT(EPOCH FROM ts) / 30)::bigint AS bkt,
             code, id, ts
      FROM post_link_clicks
      WHERE ts >= %s AND ip_hash IS NOT NULL AND is_bot = false
    ),
    bursty_buckets AS (
      SELECT b.ip_hash, b.bkt
      FROM buckets b
      JOIN buckets b2
        ON b2.ip_hash = b.ip_hash
       AND b2.bkt BETWEEN b.bkt - 1 AND b.bkt + 1
      GROUP BY b.ip_hash, b.bkt
      HAVING COUNT(DISTINCT b2.code) >= 4
    ),
    flagged AS (
      SELECT DISTINCT b.id
      FROM buckets b
      JOIN bursty_buckets bb
        ON bb.ip_hash = b.ip_hash AND bb.bkt = b.bkt
    )
    UPDATE post_link_clicks plc
       SET is_bot = true,
           bot_reason = COALESCE(bot_reason, 'R5:ip-burst-fanout')
      FROM flagged f
     WHERE plc.id = f.id AND plc.is_bot = false
    """
    if dry_run:
        sel = """
        WITH buckets AS (
          SELECT ip_hash,
                 FLOOR(EXTRACT(EPOCH FROM ts) / 30)::bigint AS bkt,
                 code, id
          FROM post_link_clicks
          WHERE ts >= %s AND ip_hash IS NOT NULL AND is_bot = false
        ),
        bursty_buckets AS (
          SELECT b.ip_hash, b.bkt
          FROM buckets b
          JOIN buckets b2
            ON b2.ip_hash = b.ip_hash
           AND b2.bkt BETWEEN b.bkt - 1 AND b.bkt + 1
          GROUP BY b.ip_hash, b.bkt
          HAVING COUNT(DISTINCT b2.code) >= 4
        )
        SELECT COUNT(DISTINCT b.id) FROM buckets b
        JOIN bursty_buckets bb ON bb.ip_hash = b.ip_hash AND bb.bkt = b.bkt
        """
        return fetch_count(conn, sel, (since_iso,))
    conn.execute(sql, (since_iso,))
    n = fetch_count(conn, "SELECT COUNT(*) FROM post_link_clicks WHERE bot_reason = 'R5:ip-burst-fanout' AND ts >= %s", (since_iso,))
    return n


def rebuild_counter(conn, dry_run: bool) -> Tuple[int, int]:
    """Rebuild post_links.clicks = COUNT(*) FROM post_link_clicks WHERE NOT is_bot.

    Returns (rows_changed, total_after).
    """
    if dry_run:
        cur = conn.execute(
            """
            SELECT
              SUM(pl.clicks)::int AS counter_before,
              SUM(COALESCE(rc.humans, 0))::int AS humans
            FROM post_links pl
            LEFT JOIN (
              SELECT code, COUNT(*) FILTER (WHERE NOT is_bot) AS humans
              FROM post_link_clicks GROUP BY code
            ) rc ON rc.code = pl.code
            """
        )
        r = cur.fetchone()
        return (int(r['counter_before'] or 0) - int(r['humans'] or 0), int(r['humans'] or 0))

    cur = conn.execute(
        """
        WITH humans AS (
          SELECT code, COUNT(*) FILTER (WHERE NOT is_bot)::int AS h
          FROM post_link_clicks GROUP BY code
        )
        UPDATE post_links pl
           SET clicks = COALESCE(humans.h, 0)
          FROM humans
         WHERE humans.code = pl.code
           AND pl.clicks <> COALESCE(humans.h, 0)
        """
    )
    conn.commit()
    cur = conn.execute("SELECT SUM(clicks)::int FROM post_links")
    return (0, int(cur.fetchone()[0] or 0))


def decrement_counter_for_window(conn, since_iso: str, dry_run: bool) -> int:
    """Cron-mode: only decrement post_links.clicks by the count of newly-flipped
    rows in the window. Cheaper than rebuilding the whole counter."""
    cur = conn.execute(
        """
        SELECT code, COUNT(*) AS n
        FROM post_link_clicks
        WHERE bot_reason IS NOT NULL AND ts >= %s
        GROUP BY code
        """,
        (since_iso,),
    )
    deltas = [(int(r['n']), r['code']) for r in cur.fetchall()]
    if dry_run or not deltas:
        return sum(d[0] for d in deltas)
    # We don't know which of those bot_reason rows were flipped THIS run vs.
    # an earlier run. Cron mode keys off ts only, so re-running within the
    # same window would double-decrement. Instead, rebuild the counter for
    # ONLY the affected codes — cheap, and idempotent.
    codes = [c for _, c in deltas]
    conn.execute(
        """
        WITH humans AS (
          SELECT code, COUNT(*) FILTER (WHERE NOT is_bot)::int AS h
          FROM post_link_clicks
          WHERE code = ANY(%s)
          GROUP BY code
        )
        UPDATE post_links pl
           SET clicks = COALESCE(humans.h, 0)
          FROM humans
         WHERE humans.code = pl.code
           AND pl.clicks <> COALESCE(humans.h, 0)
        """,
        (codes,),
    )
    conn.commit()
    return sum(d[0] for d in deltas)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--lookback-hours", type=int, default=None)
    ap.add_argument("--cron", action="store_true",
                    help="quick-sweep mode: 6h lookback, decrement-only counter update")
    ap.add_argument("--rebuild-counter", action="store_true",
                    help="full counter rebuild from is_bot=false rows (safe, idempotent)")
    ap.add_argument("--rules", default="R1,R2,R3,R4,R5",
                    help="comma-separated rule list, default all five")
    args = ap.parse_args()

    rules = {r.strip().upper() for r in args.rules.split(",") if r.strip()}
    lookback = args.lookback_hours
    if lookback is None:
        lookback = 6 if args.cron else 720  # 30d default for first/manual run

    conn = dbmod.get_conn()
    if not args.dry_run:
        ensure_bot_reason_column(conn)

    cur = conn.execute(f"SELECT (NOW() - INTERVAL '{int(lookback)} hours')::timestamptz AS since")
    since = cur.fetchone()['since']
    since_iso = since.isoformat()

    # Snapshot before
    cur = conn.execute(
        "SELECT COUNT(*) FILTER (WHERE NOT is_bot) AS humans, "
        "       COUNT(*) FILTER (WHERE is_bot)     AS bots, "
        "       COUNT(*)                           AS total "
        "  FROM post_link_clicks WHERE ts >= %s",
        (since_iso,),
    )
    b = cur.fetchone()
    print(f"[before] window={lookback}h humans={b['humans']} bots={b['bots']} total={b['total']}", flush=True)

    counts: Dict[str, int] = {}
    if "R1" in rules: counts["R1"] = apply_rule_r1(conn, since_iso, args.dry_run)
    if "R2" in rules: counts["R2"] = apply_rule_r2(conn, since_iso, args.dry_run)
    if "R3" in rules: counts["R3"] = apply_rule_r3(conn, since_iso, args.dry_run)
    if "R4" in rules: counts["R4"] = apply_rule_r4(conn, since_iso, args.dry_run)
    if "R5" in rules: counts["R5"] = apply_rule_r5(conn, since_iso, args.dry_run)

    # After-snapshot before counter rebuild
    cur = conn.execute(
        "SELECT COUNT(*) FILTER (WHERE NOT is_bot) AS humans, "
        "       COUNT(*) FILTER (WHERE is_bot)     AS bots, "
        "       COUNT(*)                           AS total "
        "  FROM post_link_clicks WHERE ts >= %s",
        (since_iso,),
    )
    a = cur.fetchone()

    print("[flips]", " ".join(f"{k}={v}" for k, v in counts.items()), flush=True)
    print(f"[after]  window={lookback}h humans={a['humans']} bots={a['bots']} total={a['total']}", flush=True)

    # Counter sync
    if args.dry_run:
        delta, total = rebuild_counter(conn, dry_run=True)
        print(f"[counter] dry-run: would change SUM by ~{delta}; humans-total now {total}", flush=True)
    elif args.cron:
        n = decrement_counter_for_window(conn, since_iso, dry_run=False)
        print(f"[counter] cron-mode: rebuilt counters for codes touching {n} flagged rows", flush=True)
    elif args.rebuild_counter or not args.cron:
        # Full-run mode (manual): rebuild all counters
        _, total = rebuild_counter(conn, dry_run=False)
        print(f"[counter] full rebuild done; SUM(post_links.clicks) now = {total}", flush=True)


if __name__ == "__main__":
    main()
