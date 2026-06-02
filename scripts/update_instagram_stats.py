#!/usr/bin/env python3
"""Refresh engagement stats for Instagram posts via the IG Graph API.

For each posts.platform='instagram' row, look up its media_id (matching by
permalink against /me/media for the account), then call /{media-id}/insights
to fetch views, reach, likes, comments, saved, shares. Write into the same
flat columns the rest of the dashboard uses (posts.upvotes/comments_count/
views) plus engagement_updated_at, and snapshot to post_views_daily for the
Trends tab.

Source-of-truth mapping (per `media_posts.target_account`):
  matt_diak       -> IG_USER_ID + IG_LONG_TOKEN  OR
                     IG_USER_ID_MATTDIAK + IG_LONG_TOKEN_MATTDIAK
  matthewheartful -> IG_USER_ID_MATTHEWHEARTFUL + IG_LONG_TOKEN_MATTHEWHEARTFUL
  omidotme        -> IG_USER_ID_OMIDOTME + IG_LONG_TOKEN_OMIDOTME

Likes -> posts.upvotes (LinkedIn/Twitter convention).
Views -> posts.views.
Comments -> posts.comments_count.

Usage:
    python3 scripts/update_instagram_stats.py [--quiet] [--limit N]
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_patch, api_post


IG_ENV_PATH = Path.home() / "instagram-graph-api" / ".env"
GRAPH = "https://graph.instagram.com/v22.0"
SA_CONFIG = Path(__file__).resolve().parent.parent / "config.json"


# ── env / credentials ─────────────────────────────────────────────────────────

def load_ig_env():
    env = {}
    for line in IG_ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def resolve_account_creds(account_name, ig_env, accounts_cfg):
    match = next(
        (a for a in accounts_cfg if a.get("username", "").lower() == account_name.lower()),
        None,
    )
    if match:
        uid = ig_env.get(match.get("ig_user_id_env", "IG_USER_ID"))
        tok = ig_env.get(match.get("ig_long_token_env", "IG_LONG_TOKEN"))
        if uid and tok:
            return uid, tok
    # Legacy bare-env fallback (matt_diak historically used IG_USER_ID/IG_LONG_TOKEN).
    uid = ig_env.get("IG_USER_ID")
    tok = ig_env.get("IG_LONG_TOKEN")
    return uid, tok


# ── Graph API helpers ─────────────────────────────────────────────────────────

def graph_get(path, token, **params):
    params["access_token"] = token
    url = f"{GRAPH}/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read())


def shortcode_from_url(url):
    """Pull the IG shortcode out of a permalink.
    https://www.instagram.com/reel/DYkkj8RDo9P/ -> DYkkj8RDo9P
    """
    m = re.search(r"/(?:reel|p|tv)/([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else None


def fetch_media_map(ig_user_id, token, max_pages=10):
    """Return {shortcode: {id, media_product_type, like_count, comments_count}}
    for the account's recent media. Pages through /me/media until exhaustion or
    max_pages safety cap.
    """
    out = {}
    fields = "id,media_type,media_product_type,permalink,like_count,comments_count"
    url = f"{GRAPH}/{ig_user_id}/media?fields={fields}&limit=100&access_token={token}"
    pages = 0
    while url and pages < max_pages:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read())
        for item in data.get("data", []):
            code = shortcode_from_url(item.get("permalink"))
            if code:
                out[code] = item
        url = (data.get("paging") or {}).get("next")
        pages += 1
    return out


def fetch_insights(media_id, mtype, token):
    """Return {metric_name: value}. Pick metrics based on media type."""
    if mtype == "REELS":
        metrics = "views,reach,likes,comments,saved,shares,total_interactions"
    elif mtype == "VIDEO":
        metrics = "views,reach,likes,comments,saved,shares"
    else:
        metrics = "reach,likes,comments,saved,shares"
    try:
        data = graph_get(f"{media_id}/insights", token, metric=metrics)
    except urllib.error.HTTPError as e:
        return {"__error__": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    result = {}
    for m in data.get("data", []):
        name = m.get("name")
        vals = m.get("values") or []
        result[name] = (vals[0] or {}).get("value") if vals else None
    return result


# ── main ──────────────────────────────────────────────────────────────────────

def log(msg, quiet=False):
    if not quiet:
        print(msg)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    ig_env = load_ig_env()
    try:
        cfg = json.loads(SA_CONFIG.read_text())
    except FileNotFoundError:
        cfg = {}
    accounts_cfg = ((cfg.get("instagram") or {}).get("accounts") or [])

    resp = api_get(
        "/api/v1/posts",
        query={
            "platform": "instagram",
            "status": "active",
            "has_our_url": "true",
            "order_by": "id",
            "order_dir": "asc",
            "limit": 500,
        },
    )
    rows = (resp.get("data") or {}).get("posts") or []
    if args.limit:
        rows = rows[: args.limit]

    log(f"[stats-ig] {len(rows)} active IG rows to check", args.quiet)

    by_account = {}
    for r in rows:
        by_account.setdefault(r["our_account"], []).append(r)

    checked = 0
    updated = 0
    failed = 0
    not_found = 0
    views_refreshed = 0

    for account, account_rows in by_account.items():
        uid, tok = resolve_account_creds(account, ig_env, accounts_cfg)
        if not uid or not tok:
            log(f"[stats-ig] missing creds for account={account}; skipping {len(account_rows)} rows", args.quiet)
            failed += len(account_rows)
            continue
        try:
            media_map = fetch_media_map(uid, tok)
        except Exception as e:
            log(f"[stats-ig] media list failed for {account}: {e}; skipping {len(account_rows)} rows", args.quiet)
            failed += len(account_rows)
            continue
        log(f"[stats-ig] account={account} media-map size={len(media_map)}", args.quiet)

        for r in account_rows:
            checked += 1
            code = shortcode_from_url(r["our_url"])
            if not code:
                log(f"[stats-ig]   id={r['id']} no shortcode in {r['our_url']}", args.quiet)
                not_found += 1
                continue
            item = media_map.get(code)
            if not item:
                log(f"[stats-ig]   id={r['id']} shortcode={code} not in /me/media listing", args.quiet)
                not_found += 1
                continue

            media_id = item["id"]
            mtype = item.get("media_product_type") or item.get("media_type")
            ins = fetch_insights(media_id, mtype, tok)
            if "__error__" in ins:
                log(f"[stats-ig]   id={r['id']} insights error: {ins['__error__']}", args.quiet)
                failed += 1
                continue

            likes = ins.get("likes") or item.get("like_count") or 0
            comments = ins.get("comments") or item.get("comments_count") or 0
            views = ins.get("views")  # None for photos; that's fine

            old = (r["upvotes"] or 0, r["comments_count"] or 0, r["views"] or 0)
            new = (likes, comments, views or 0)

            api_patch(
                f"/api/v1/posts/{r['id']}",
                {
                    "upvotes": likes,
                    "comments_count": comments,
                    "views": views,
                    "stamp_engagement_now": True,
                    "stamp_status_checked_now": True,
                    "reset_deletion_detect_count": True,
                },
            )
            if views is not None:
                api_post(
                    "/api/v1/post-views-daily/snapshot",
                    {"post_id": r["id"], "views": views},
                )
                views_refreshed += 1
            if new != old:
                updated += 1
            log(
                f"[stats-ig]   id={r['id']} code={code} type={mtype} "
                f"likes={likes} comments={comments} views={views}",
                args.quiet,
            )
            # Be polite to the Graph API.
            time.sleep(0.2)

    log(
        f"[stats-ig] done: checked={checked} updated={updated} "
        f"not_found={not_found} failed={failed} views_refreshed={views_refreshed}",
        args.quiet,
    )
    # Machine-readable summary for the shell wrapper to consume.
    print(
        f"SUMMARY:CHECKED={checked} UPDATED={updated} NOT_FOUND={not_found} "
        f"FAILED={failed} VIEWS_REFRESHED={views_refreshed}"
    )


if __name__ == "__main__":
    main()
