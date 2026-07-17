#!/usr/bin/env python3
"""Backfill post_links.real_clicks and dm_links.real_clicks from PostHog.

Background:
  Pre 2026-05-07 the `clicks` integer on post_links / dm_links was incremented by
  the redirector on every hit (humans + Twitter card prefetch + LinkedIn unfurl
  + Slack preview bots). Live measurement on a8558aj9 found ~95% of those hits
  were bots, only ~5% real humans. After 2026-05-07 we ship a per-click log
  (post_link_clicks) that splits humans/bots by UA.

  Historical rows have no per-click data, so this script asks PostHog for the
  ground truth: count `$pageview` events with utm_content=<code> and timestamp
  > minted_at. PostHog already filters bots out, so the count is the real
  human-click number.

What it does:
  - Iterates every row of post_links and dm_links.
  - Resolves the destination domain to a PostHog project_id via config.json.
  - Runs a HogQL count() query per code via the /query endpoint.
  - Writes the count into the new real_clicks column (default 0).
  - For external destinations (github.com, claude.ai, t8r.tech without
    PostHog, etc.) sets real_clicks=0 and prints a SKIP marker.

Idempotent: re-runs overwrite the column with the latest PostHog count.

Usage:
  python3 scripts/backfill_real_clicks.py [--dry-run] [--limit N]
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timezone

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_DIR)

from scripts import db as dbmod  # noqa: E402

# THE canonical config loader (scripts/config.py): S4L_CONFIG_PATH / state-dir /
# S4L_REPO_DIR aware, mtime-cached. Replaces this file's hand-rolled loader and
# its hardcoded config path (the S4L-4H dead-path class on customer boxes).
import os as _cfg_os, sys as _cfg_sys
_cfg_sys.path.insert(0, _cfg_os.path.dirname(_cfg_os.path.abspath(__file__)))
from config import config_path as _canonical_config_path, load_config
CONFIG_PATH = _canonical_config_path()




def domain_of(url):
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def build_domain_index(cfg):
    """domain -> {project_id, api_key_env, name, has_posthog}"""
    out = {}
    for p in cfg.get("projects", []):
        ph = p.get("posthog") or {}
        pid = ph.get("project_id")
        site = p.get("website") or ""
        if not site:
            continue
        d = domain_of(site)
        if not d:
            continue
        # collapse www. to bare
        if d.startswith("www."):
            d = d[4:]
        out[d] = {
            "project_id": str(pid) if pid is not None else None,
            "api_key_env": ph.get("api_key_env") or "POSTHOG_PERSONAL_API_KEY",
            "name": p.get("name"),
            "has_posthog": pid is not None,
        }
    return out


def project_for_url(url, idx):
    d = domain_of(url)
    if not d:
        return None, None
    if d.startswith("www."):
        d = d[4:]
    if d in idx:
        return d, idx[d]
    # also try bare suffix match (e.g. www.mediar.ai -> mediar.ai)
    for k, v in idx.items():
        if d.endswith("." + k) or d == k:
            return k, v
    return d, None


def utm_content_from_url(url):
    """Pull the utm_content query param from a target_url, if any."""
    try:
        qs = urllib.parse.urlparse(url).query
        params = urllib.parse.parse_qs(qs)
    except Exception:
        return None
    vals = params.get("utm_content")
    if vals:
        return vals[0]
    # also check metadata[utm_content] used in cal.com links
    for k, v in params.items():
        if k.endswith("[utm_content]") and v:
            return v[0]
    return None


def posthog_count_pageviews(api_key, project_id, utm_content_value, after_iso, host=None, timeout=30):
    """HogQL count of $pageview matching utm_content AND ts >= after.

    If `host` is supplied it is added to the WHERE so cross-domain noise from
    shared PostHog projects (project 330744 hosts ~14 different sites) does
    not leak in.
    """
    url = f"https://us.posthog.com/api/projects/{project_id}/query/"
    where = [
        "event = '$pageview'",
        f"properties.utm_content = {sql_str(utm_content_value)}",
        f"timestamp >= toDateTime({sql_str(after_iso)})",
    ]
    if host:
        where.append(f"properties.$host = {sql_str(host)}")
    hogql = "SELECT count() FROM events WHERE " + " AND ".join(where)
    body = json.dumps({"query": {"kind": "HogQLQuery", "query": hogql}}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    results = data.get("results") or []
    if not results:
        return 0
    first = results[0]
    if isinstance(first, list):
        first = first[0] if first else 0
    try:
        return int(first or 0)
    except (TypeError, ValueError):
        return 0


def sql_str(s):
    return "'" + str(s).replace("'", "''") + "'"


def to_iso(dt):
    if dt is None:
        return "1970-01-01T00:00:00"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def backfill_table(conn, table, idx, dry_run=False, limit=None):
    print(f"\n=== {table} ===", flush=True)
    sql = f"SELECT code, target_url, minted_at FROM {table} ORDER BY minted_at"
    if limit:
        sql += f" LIMIT {int(limit)}"
    cur = conn.execute(sql)
    rows = cur.fetchall()
    print(f"  {len(rows)} rows to process", flush=True)

    last_pid = None
    counters = {"updated": 0, "skipped_no_ph": 0, "errors": 0, "zero": 0}
    for i, r in enumerate(rows, 1):
        code = r["code"]
        url = r["target_url"]
        minted = r["minted_at"]
        domain, info = project_for_url(url, idx)
        if not info or not info["has_posthog"]:
            print(f"  [{i:3d}/{len(rows)}] {code} dest={domain or url[:40]} SKIP (no posthog project)", flush=True)
            if not dry_run:
                conn.execute(f"UPDATE {table} SET real_clicks = 0 WHERE code = %s", (code,))
            counters["skipped_no_ph"] += 1
            continue
        pid = info["project_id"]
        api_env = info["api_key_env"]
        api_key = os.environ.get(api_env)
        if not api_key and api_env != "POSTHOG_PERSONAL_API_KEY":
            api_key = os.environ.get("POSTHOG_PERSONAL_API_KEY")
        if not api_key:
            print(f"  [{i:3d}/{len(rows)}] {code} domain={domain} ERR no api key", flush=True)
            counters["errors"] += 1
            continue
        # Pace 0.5s between PROJECT switches (rate-limit guard)
        if last_pid is not None and last_pid != pid:
            time.sleep(0.5)
        last_pid = pid
        after = to_iso(minted)
        # Each target_url already carries its own utm_content (the post UUID
        # for posts, dm_<id> for DMs); the redirector's short code isn't what
        # PostHog sees, so we read the embedded utm_content instead.
        utm_val = utm_content_from_url(url) or code
        try:
            count = posthog_count_pageviews(api_key, pid, utm_val, after, host=domain)
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            print(f"  [{i:3d}/{len(rows)}] {code} domain={domain} pid={pid} HTTP ERR {e}", flush=True)
            counters["errors"] += 1
            continue
        except Exception as e:
            print(f"  [{i:3d}/{len(rows)}] {code} domain={domain} pid={pid} ERR {e}", flush=True)
            counters["errors"] += 1
            continue
        if not dry_run:
            conn.execute(f"UPDATE {table} SET real_clicks = %s WHERE code = %s", (count, code))
        if count == 0:
            counters["zero"] += 1
        counters["updated"] += 1
        print(f"  [{i:3d}/{len(rows)}] {code} domain={domain} pid={pid} utm={utm_val[:50]} real_clicks={count}", flush=True)

    if not dry_run:
        conn.commit()
    print(f"  Summary: {counters}", flush=True)
    return counters


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Query but do not write to DB")
    ap.add_argument("--limit", type=int, default=None, help="Process at most N rows per table")
    ap.add_argument("--table", choices=["post_links", "dm_links", "both"], default="both")
    args = ap.parse_args()

    dbmod.load_env()
    cfg = load_config()
    idx = build_domain_index(cfg)
    print(f"Domain index ({len(idx)} entries):", flush=True)
    for d, v in sorted(idx.items()):
        print(f"  {d:40s} -> pid={v['project_id']} ({v['name']})")

    conn = dbmod.get_conn()
    if args.table in ("post_links", "both"):
        backfill_table(conn, "post_links", idx, dry_run=args.dry_run, limit=args.limit)
    if args.table in ("dm_links", "both"):
        backfill_table(conn, "dm_links", idx, dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
