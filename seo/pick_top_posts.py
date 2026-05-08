#!/usr/bin/env python3
"""Per-project top-post picker for the seo_top_posts pipeline.

Logic (per project):
    1. Read posts table for last 14d:
         project_name = <product>
         status NULL or NOT IN ('deleted', 'removed')
         views >= MIN_VIEWS (default 10000)
       Twitter is the primary platform; LinkedIn/Reddit also eligible if
       their `views` field is populated. We rank by composite score:
           views + upvotes*200 + comments_count*100
       so a Reddit/LinkedIn post with 5000 views + 100 upvotes (rare,
       exceptional engagement) can also surface, but the 10k-view floor
       is per the requirement.
    2. Filter: drop posts whose our_url already references a /t/ slug
       (link_source != 'plain_url_*'). Those already got their dedicated
       page at post-time via twitter_gen_links.py's A/B gate.
    3. Cooldown: drop any (product, post_id) already in top_post_winners
       (cooldown is permanent — one viral post gets exactly one /t/ page,
       no repeats).
    4. Pick the top-scoring eligible post.
    5. Write a brief JSON the pipeline shell can hand to Claude.

Exits:
    0 - brief written / printed
    2 - no eligible post in window (skip)

Usage:
    python3 seo/pick_top_posts.py --product claude-meter
    python3 seo/pick_top_posts.py --product claude-meter --out /tmp/brief.json
    python3 seo/pick_top_posts.py --list-enabled
    python3 seo/pick_top_posts.py --product claude-meter --min-views 10000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
CONFIG_PATH = ROOT_DIR / "config.json"

ENV_PATH = ROOT_DIR / ".env"
if ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

import psycopg2  # noqa: E402

DEFAULT_MIN_VIEWS = 10_000
DEFAULT_WINDOW_DAYS = 14

# Composite score weights. Views dominate, but high-engagement posts on
# platforms with small view counts (Reddit, LinkedIn organic) still get a
# fair shake.
W_VIEWS = 1
W_UPVOTES = 200
W_COMMENTS = 100


def _load_config():
    return json.loads(CONFIG_PATH.read_text())


def _find_project(cfg, product):
    for p in cfg.get("projects", []):
        if (p.get("name") or "").lower() == (product or "").lower():
            return p
    return None


def _enabled_products(cfg):
    out = []
    for p in cfg.get("projects", []):
        lp = p.get("landing_pages") or {}
        # Reuse top_pages_enabled — same surface (homepage+/t/), same
        # constraint (must have a marketing site). No need for a new flag.
        # Also require weight > 0 so paused projects (Clone, tenxats,
        # macOS Session Replay as of 2026-05-08) don't get a /t/ page +
        # homepage NewsStrip swap they don't want. Posting + Moltbook +
        # SERP/GSC all already honor weight > 0; this brings top-posts
        # (and top-pages, see pick_top_pages.py) into line with the rest.
        if lp.get("top_pages_enabled") and (p.get("weight") or 0) > 0:
            out.append(p.get("name"))
    return out


def _candidates(cur, product, window_days, min_views):
    """Find posts from <product> in the last <window_days> with views >=
    <min_views>, that don't already have a dedicated /t/ landing page,
    and that haven't already won the seo_top_posts pipeline."""
    cur.execute(
        """
        SELECT
            p.id,
            p.platform,
            p.our_url,
            p.our_content,
            p.thread_title,
            p.thread_content,
            COALESCE(p.views, 0)          AS views,
            COALESCE(p.upvotes, 0)        AS upvotes,
            COALESCE(p.comments_count, 0) AS comments,
            p.posted_at,
            p.link_source,
            p.is_recommendation
        FROM posts p
        LEFT JOIN top_post_winners w
               ON w.product = p.project_name AND w.post_id = p.id
        WHERE p.project_name = %s
          AND p.posted_at >= NOW() - (%s || ' days')::interval
          AND (p.status IS NULL OR p.status NOT IN ('deleted', 'removed'))
          AND COALESCE(p.views, 0) >= %s
          AND w.id IS NULL                                -- not already a winner
          AND (
                p.link_source IS NULL
             OR p.link_source LIKE 'plain_url_%%'         -- only posts that
          )                                               -- LACK a /t/ page
          AND (p.is_recommendation IS NULL OR p.is_recommendation = FALSE)
        ORDER BY (
            COALESCE(p.views, 0) * %s
          + COALESCE(p.upvotes, 0) * %s
          + COALESCE(p.comments_count, 0) * %s
        ) DESC
        LIMIT 25
        """,
        (product, window_days, min_views, W_VIEWS, W_UPVOTES, W_COMMENTS),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _score(row):
    return (
        int(row["views"] or 0) * W_VIEWS
        + int(row["upvotes"] or 0) * W_UPVOTES
        + int(row["comments"] or 0) * W_COMMENTS
    )


def _siblings_24h(cur, product, post_id, window_hours=48):
    """Find sibling posts (same product, same calendar window) referencing
    the same news. We define sibling loosely: same product, posted within
    48h of the winner, not deleted, with views > 500 or upvotes > 5. The
    pipeline uses these to enrich the brief Claude sees so the new /t/
    page can quote the cluster of follow-up tweets, not just the winner.
    """
    cur.execute(
        """
        SELECT
            p.id, p.platform, p.our_url, p.our_content,
            COALESCE(p.views, 0) AS views,
            COALESCE(p.upvotes, 0) AS upvotes,
            COALESCE(p.comments_count, 0) AS comments,
            p.posted_at
        FROM posts p,
             (SELECT posted_at AS t FROM posts WHERE id = %s) anchor
        WHERE p.project_name = %s
          AND p.id <> %s
          AND p.posted_at BETWEEN
              anchor.t - (%s || ' hours')::interval
          AND anchor.t + (%s || ' hours')::interval
          AND (p.status IS NULL OR p.status NOT IN ('deleted', 'removed'))
          AND (COALESCE(p.views, 0) > 500 OR COALESCE(p.upvotes, 0) > 5)
        ORDER BY (
            COALESCE(p.views, 0) + COALESCE(p.upvotes, 0) * 50
          + COALESCE(p.comments_count, 0) * 25
        ) DESC
        LIMIT 8
        """,
        (post_id, product, post_id, window_hours, window_hours),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def build_brief(product, window_days=DEFAULT_WINDOW_DAYS, min_views=DEFAULT_MIN_VIEWS):
    cfg = _load_config()
    proj = _find_project(cfg, product)
    if not proj:
        raise SystemExit(f"ERROR: product '{product}' not found in config.json")

    # Defense in depth: even if someone runs the orchestrator with an
    # explicit `./run_top_posts_pipeline.sh tenxats`, refuse if the project
    # is paused (weight <= 0). Same exit code as "no eligible viral post"
    # so the orchestrator's case branch treats it as a benign skip.
    if (proj.get("weight") or 0) <= 0:
        print(
            f"SKIP: project '{product}' has weight <= 0 (paused), "
            f"refusing to ship a top-post page for it",
            file=sys.stderr,
        )
        sys.exit(2)

    lp = proj.get("landing_pages") or {}
    repo_raw = lp.get("repo") or ""
    repo_abs = os.path.expanduser(repo_raw)
    if not repo_abs or not os.path.isdir(repo_abs):
        raise SystemExit(f"ERROR: repo path missing for '{product}': {repo_raw!r}")

    base_url = (lp.get("base_url") or proj.get("website") or "").rstrip("/")

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        cur = conn.cursor()
        rows = _candidates(cur, product, window_days, min_views)
        if not rows:
            print(
                f"SKIP: no posts >= {min_views} views in last {window_days}d "
                f"for {product} (or all already have /t/ pages / are winners)",
                file=sys.stderr,
            )
            sys.exit(2)

        winner = rows[0]
        siblings = _siblings_24h(cur, product, winner["id"])
        cur.close()
    finally:
        conn.close()

    # Serialize datetimes for JSON.
    def _serialize(d):
        out = dict(d)
        for k, v in list(out.items()):
            if hasattr(v, "isoformat"):
                out[k] = v.isoformat()
        return out

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "per_project_top_post",
        "product": proj.get("name"),
        "domain": (proj.get("website") or "").replace("https://", "").replace("http://", "").rstrip("/"),
        "base_url": base_url,
        "repo_path": repo_abs,
        "window_days": window_days,
        "min_views": min_views,
        "weights": {"views": W_VIEWS, "upvotes": W_UPVOTES, "comments": W_COMMENTS},
        "winner": {
            **_serialize(winner),
            "score": _score(winner),
        },
        "siblings": [_serialize(s) for s in siblings],
        "candidates_total": len(rows),
        "ranking": [
            {**_serialize(r), "score": _score(r)} for r in rows[:10]
        ],
        "project_config": proj,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--product")
    ap.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS)
    ap.add_argument("--min-views", type=int, default=DEFAULT_MIN_VIEWS)
    ap.add_argument("--out")
    ap.add_argument("--list-enabled", action="store_true")
    args = ap.parse_args()

    if args.list_enabled:
        cfg = _load_config()
        for name in _enabled_products(cfg):
            print(name)
        return 0

    if not args.product:
        ap.error("--product required (or use --list-enabled)")

    brief = build_brief(args.product, window_days=args.days, min_views=args.min_views)
    payload = json.dumps(brief, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(payload)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
