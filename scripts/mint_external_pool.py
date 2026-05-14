#!/usr/bin/env python3
"""Pre-mint a pool of `post_links` codes for projects whose redirector lives on
the CLIENT'S domain (external_short_links=true in config.json).

Why this exists: for projects where we own the domain (fazm.ai, cyrano.systems,
etc.) we ship a /r/[code] route via @m13v/seo-components and resolve codes
live by hitting our DB. For projects where the CLIENT owns the domain and
doesn't want a PR (Kent: runner.now, agora.xyz, podlog.io), we hand them a
static CSV of `code -> destination` pairs they drop into their own redirector.

The pool is rows in `post_links` with post_id IS NULL AND reply_id IS NULL
and minted_session LIKE 'pool:%'. When a pipeline posts for a project with
external_short_links=true, wrap_text_for_post pops the next unclaimed pool
row matching (project_name, platform) instead of minting a fresh code, so the
client's CSV stays valid forever (until the pool runs dry, then we top up).

Usage:
  python3 scripts/mint_external_pool.py \
    --project Runner --platforms reddit,twitter,linkedin,github_issues,moltbook \
    --per-platform 250

  python3 scripts/mint_external_pool.py --status                 # show pool depth
  python3 scripts/mint_external_pool.py --export-csv DIR         # write CSVs
"""

from __future__ import annotations
import argparse
import csv
import json
import os
import secrets
import sys
from urllib.parse import urlencode

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_DIR, 'scripts'))

import db as dbmod  # noqa: E402
from dm_short_links import CODE_ALPHABET, CODE_LEN, _load_projects  # noqa: E402

POOL_SESSION_PREFIX = 'pool:'


def _slug(name: str) -> str:
    return ''.join(c.lower() if c.isalnum() else '-' for c in name).strip('-')


def _website(projects: list, project_name: str) -> str:
    for p in projects:
        if p.get('name') == project_name:
            return (p.get('website') or '').rstrip('/')
    raise SystemExit(f"project '{project_name}' not found in config.json")


def _gen_code() -> str:
    return ''.join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def _build_target(homepage: str, *, platform: str, slug: str, code: str) -> str:
    params = {
        'utm_source': platform,
        'utm_medium': 'post',
        'utm_campaign': slug,
        'utm_content': code,
    }
    sep = '&' if '?' in homepage else '?'
    return f"{homepage}{sep}{urlencode(params)}"


def mint_pool(*, project_name: str, platforms: list, per_platform: int,
              session_tag: str | None = None) -> dict:
    projects = _load_projects()
    homepage = _website(projects, project_name)
    slug = _slug(project_name)
    session = session_tag or f"{POOL_SESSION_PREFIX}{slug}-{platforms[0] if len(platforms)==1 else 'multi'}"

    conn = dbmod.get_conn()
    minted = {plat: 0 for plat in platforms}
    skipped = {plat: 0 for plat in platforms}
    try:
        for platform in platforms:
            tries = 0
            while minted[platform] < per_platform and tries < per_platform * 3:
                tries += 1
                code = _gen_code()
                target = _build_target(homepage, platform=platform, slug=slug, code=code)
                try:
                    conn.execute(
                        "INSERT INTO post_links "
                        "  (code, platform, project_name, target_url, kind, "
                        "   project_at_mint, minted_session) "
                        "VALUES (%s, %s, %s, %s, 'website', %s, %s)",
                        (code, platform, project_name, target, project_name,
                         f"{POOL_SESSION_PREFIX}{slug}-{platform}"),
                    )
                    conn.commit()
                    minted[platform] += 1
                except Exception as e:
                    if 'duplicate key' in str(e).lower():
                        conn.execute("ROLLBACK")
                        skipped[platform] += 1
                        continue
                    raise
        return {
            'project': project_name,
            'homepage': homepage,
            'minted': minted,
            'skipped_collisions': skipped,
            'total_minted': sum(minted.values()),
        }
    finally:
        conn.close()


def pool_status(project_filter: str | None = None) -> list[dict]:
    conn = dbmod.get_conn()
    try:
        cur = conn.execute(
            "SELECT project_name, platform, "
            "       COUNT(*) FILTER (WHERE post_id IS NULL AND reply_id IS NULL) AS available, "
            "       COUNT(*) FILTER (WHERE post_id IS NOT NULL OR reply_id IS NOT NULL) AS claimed, "
            "       COUNT(*) AS total, "
            "       MAX(minted_at) AS last_minted "
            "FROM post_links "
            "WHERE minted_session LIKE 'pool:%%' "
            + ("AND project_name = %s " if project_filter else "")
            + "GROUP BY project_name, platform ORDER BY project_name, platform",
            (project_filter,) if project_filter else (),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def export_csv(out_dir: str, project_filter: str | None = None) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    conn = dbmod.get_conn()
    written = {}
    try:
        cur = conn.execute(
            "SELECT DISTINCT project_name FROM post_links "
            "WHERE minted_session LIKE 'pool:%%' "
            + ("AND project_name = %s " if project_filter else "")
            + "ORDER BY project_name",
            (project_filter,) if project_filter else (),
        )
        projects = [dict(r)['project_name'] for r in cur.fetchall()]
        for project_name in projects:
            cur = conn.execute(
                "SELECT code, platform, target_url FROM post_links "
                "WHERE minted_session LIKE 'pool:%%' AND project_name = %s "
                "ORDER BY platform, code",
                (project_name,),
            )
            rows = [dict(r) for r in cur.fetchall()]
            slug = _slug(project_name)
            path = os.path.join(out_dir, f"kent-shortlinks-{slug}.csv")
            with open(path, 'w', newline='') as f:
                w = csv.writer(f)
                w.writerow(['short_path', 'destination_url', 'platform', 'project'])
                for r in rows:
                    w.writerow([f"/r/{r['code']}", r['target_url'], r['platform'], project_name])
            written[project_name] = {'path': path, 'rows': len(rows)}
        return written
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--project', help='project_name from config.json')
    ap.add_argument('--platforms', default='reddit,twitter,linkedin,github_issues,moltbook',
                    help='comma-separated platforms to seed')
    ap.add_argument('--per-platform', type=int, default=250,
                    help='codes to mint per platform (default 250)')
    ap.add_argument('--status', action='store_true', help='print pool depth per project/platform')
    ap.add_argument('--export-csv', metavar='DIR', help='export CSVs to DIR (one per project)')
    args = ap.parse_args()

    if args.status:
        rows = pool_status(args.project)
        if not rows:
            print('no pool rows found')
            return
        print(f"{'project':<22} {'platform':<14} {'avail':>6} {'claim':>6} {'total':>6} {'last_mint'}")
        for r in rows:
            print(f"{r['project_name']:<22} {r['platform']:<14} {r['available']:>6} {r['claimed']:>6} {r['total']:>6} {r['last_minted']}")
        return

    if args.export_csv:
        out = export_csv(args.export_csv, args.project)
        print(json.dumps(out, indent=2, default=str))
        return

    if not args.project:
        ap.error('--project required (unless using --status or --export-csv)')

    platforms = [p.strip() for p in args.platforms.split(',') if p.strip()]
    result = mint_pool(project_name=args.project, platforms=platforms,
                      per_platform=args.per_platform)
    print(json.dumps(result, indent=2, default=str))


if __name__ == '__main__':
    main()
