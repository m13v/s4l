#!/usr/bin/env python3
"""Mint Kent's external short-link pool: 10k codes per site, 75% homepage,
25% across discovered subpages, distributed evenly across 5 platforms.

Designed to be fast (bulk INSERT + ON CONFLICT DO NOTHING, no per-row commit)
so the full 30k mint completes in seconds rather than the ~20min the legacy
mint_external_pool.py took for 3,750 rows.

Usage:
  python3 scripts/mint_kent_pool.py --dry-run     # preview the plan
  python3 scripts/mint_kent_pool.py               # mint it
  python3 scripts/mint_kent_pool.py --status      # show pool depth by destination
"""

from __future__ import annotations
import argparse
import json
import os
import secrets
import sys
from datetime import date
from typing import Any
from urllib.parse import urlencode

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_DIR, 'scripts'))

import db as dbmod  # noqa: E402
from dm_short_links import CODE_ALPHABET, CODE_LEN  # noqa: E402

PLATFORMS = ['reddit', 'twitter', 'linkedin', 'github_issues', 'moltbook']
TOTAL_PER_SITE = 10_000
HOME_FRACTION = 0.75

SITE_CONFIG: dict[str, dict[str, Any]] = {
    'Runner': {
        'origin': 'https://runner.now',
        'slug': 'runner',
        'subpages': [
            '/download/',
            '/workflows/',
            '/apps/',
            '/runner-for-business/',
            '/blog/',
            '/changelog/',
            '/workflows/ai-executive-assistant/',
            '/workflows/morning-founder-briefing/',
            '/workflows/meeting-notes-to-action-items/',
            '/workflows/ai-email-assistant/',
            '/workflows/qualify-inbound-leads-from-gmail-hubspot/',
            '/workflows/stakeholder-research-after-sales-call/',
            '/apps/gmail/',
            '/apps/google-calendar/',
            '/apps/slack/',
            '/apps/notion/',
            '/apps/hubspot/',
            '/apps/granola/',
            '/apps/linear/',
            '/blog/best-ai-apps-2026/',
        ],
    },
    'Agora': {
        'origin': 'https://www.agora.xyz',
        'slug': 'agora',
        'subpages': [
            '/about',
            '/blogs',
            '/talk-to-our-team',
            '/jobs',
        ],
    },
    'Podlog': {
        'origin': 'https://podlog.io',
        'slug': 'podlog',
        'subpages': [],
    },
}

POOL_PREFIX = 'pool:'


def _slug_path(path: str) -> str:
    if not path or path == '/':
        return 'home'
    cleaned = path.strip('/').replace('/', '-')
    return cleaned or 'home'


def _build_target(origin: str, path: str, *, platform: str, campaign_slug: str, code: str) -> str:
    base = origin.rstrip('/') + path
    params = {
        'utm_source': platform,
        'utm_medium': 'post',
        'utm_campaign': campaign_slug,
        'utm_content': code,
    }
    sep = '&' if '?' in base else '?'
    return f"{base}{sep}{urlencode(params)}"


def _session_tag(today_iso: str, slug: str, path: str, platform: str) -> str:
    return f"{POOL_PREFIX}kent-{today_iso}:{slug}:{_slug_path(path)}:{platform}"


def _gen_unique_codes(n: int, existing: set[str]) -> list[str]:
    out: set[str] = set()
    while len(out) < n:
        c = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        if c not in existing and c not in out:
            out.add(c)
    return list(out)


def _existing_codes(conn) -> set[str]:
    cur = conn.execute("SELECT code FROM post_links")
    return {dict(r)['code'] for r in cur.fetchall()}


def _plan(per_site: int = TOTAL_PER_SITE, home_frac: float = HOME_FRACTION) -> list[dict]:
    """Compute (project, platform, path, count) tuples for the full mint."""
    rows = []
    per_platform = per_site // len(PLATFORMS)
    home_per_platform = int(per_platform * home_frac)
    subpage_per_platform = per_platform - home_per_platform
    for project, cfg in SITE_CONFIG.items():
        subpages = cfg['subpages']
        if not subpages:
            actual_home = per_platform
            actual_sub_each = 0
        else:
            actual_home = home_per_platform
            actual_sub_each = subpage_per_platform // len(subpages)
            remainder = subpage_per_platform - actual_sub_each * len(subpages)
            actual_home += remainder
        for platform in PLATFORMS:
            rows.append({
                'project': project,
                'platform': platform,
                'path': '/',
                'count': actual_home,
            })
            for path in subpages:
                rows.append({
                    'project': project,
                    'platform': platform,
                    'path': path,
                    'count': actual_sub_each,
                })
    return rows


def mint_all(*, dry_run: bool = False) -> dict:
    plan = _plan()
    if dry_run:
        per_site_totals: dict[str, int] = {}
        for r in plan:
            per_site_totals[r['project']] = per_site_totals.get(r['project'], 0) + r['count']
        return {
            'plan_rows': len(plan),
            'codes_by_site': per_site_totals,
            'sample': plan[:3] + plan[-3:],
        }

    today = date.today().isoformat()
    conn = dbmod.get_conn()
    raw = conn._conn  # type: ignore[attr-defined]
    minted = {p: 0 for p in SITE_CONFIG}
    total = 0
    try:
        existing = _existing_codes(conn)
        for entry in plan:
            project = entry['project']
            platform = entry['platform']
            path = entry['path']
            n = entry['count']
            if n <= 0:
                continue
            cfg = SITE_CONFIG[project]
            slug = cfg['slug']
            session_tag = _session_tag(today, slug, path, platform)
            codes = _gen_unique_codes(n, existing)
            existing.update(codes)
            values = []
            for code in codes:
                target = _build_target(
                    cfg['origin'], path,
                    platform=platform, campaign_slug=slug, code=code,
                )
                values.append((code, platform, project, target, 'website', project, session_tag))
            cur = raw.cursor()
            from psycopg2.extras import execute_values
            execute_values(
                cur,
                "INSERT INTO post_links "
                "  (code, platform, project_name, target_url, kind, project_at_mint, minted_session) "
                "VALUES %s "
                "ON CONFLICT (code) DO NOTHING",
                values,
                template=None,
                page_size=1000,
            )
            raw.commit()
            cur.close()
            minted[project] += len(values)
            total += len(values)
            print(f"  + {project:<8} {platform:<14} {path:<60} count={len(values)}", flush=True)
        return {
            'minted_total': total,
            'minted_by_project': minted,
            'session_date': today,
        }
    finally:
        conn.close()


def pool_status_detailed() -> list[dict]:
    conn = dbmod.get_conn()
    try:
        cur = conn.execute(
            "SELECT project_name, platform, minted_session, "
            "       COUNT(*) FILTER (WHERE post_id IS NULL AND reply_id IS NULL) AS available, "
            "       COUNT(*) FILTER (WHERE post_id IS NOT NULL OR reply_id IS NOT NULL) AS claimed, "
            "       COUNT(*) AS total, "
            "       MIN(target_url) AS sample_target, "
            "       MAX(minted_at) AS last_minted "
            "FROM post_links "
            "WHERE minted_session LIKE 'pool:kent-%' "
            "GROUP BY project_name, platform, minted_session "
            "ORDER BY project_name, platform, minted_session"
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dry-run', action='store_true', help='print the plan, do not write')
    ap.add_argument('--status', action='store_true', help='print pool depth grouped by destination')
    args = ap.parse_args()

    if args.status:
        rows = pool_status_detailed()
        if not rows:
            print('no kent pool rows found')
            return
        print(f"{'project':<10} {'platform':<14} {'session':<70} {'avail':>6} {'claim':>6}")
        for r in rows:
            sess = (r['minted_session'] or '')[-70:]
            print(f"{r['project_name']:<10} {r['platform']:<14} {sess:<70} {r['available']:>6} {r['claimed']:>6}")
        totals: dict[str, int] = {}
        for r in rows:
            totals[r['project_name']] = totals.get(r['project_name'], 0) + r['available']
        print('---')
        for k, v in sorted(totals.items()):
            print(f"  {k}: {v} available")
        return

    if args.dry_run:
        print(json.dumps(mint_all(dry_run=True), indent=2))
        return

    print(json.dumps(mint_all(), indent=2, default=str))


if __name__ == '__main__':
    main()
