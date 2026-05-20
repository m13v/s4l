#!/usr/bin/env python3
"""One-off: mint 500 Podlog short codes with subpage routing and export a CSV.

Why this exists: the original mint_kent_pool.py minted 10k Podlog codes ALL
pointing to homepage (subpages=[]). After analyzing 233 recent Podlog posts +
podlog.io's sitemap (3,137 /listen/<stack> pages), we know our posts target
specific stacks (kubernetes, rust, postgresql, etc.) and lifecycle intents
(NotebookLM comparison -> /how-it-works, podcast browsing -> /explore).

This script mints a fresh 500-code batch with that intent split, leaves the
existing 10k homepage pool alone, and writes a CSV Kent can ship as a static
fallback if he doesn't want his handler calling app.s4l.ai.
"""

from __future__ import annotations
import csv
import os
import secrets
import sys
from datetime import date
from urllib.parse import urlencode

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_DIR, 'scripts'))

import db as dbmod  # noqa: E402
from dm_short_links import CODE_ALPHABET, CODE_LEN  # noqa: E402
from psycopg2.extras import execute_values  # noqa: E402

ORIGIN = 'https://podlog.io'
CAMPAIGN = 'podlog'
PLATFORM = 'twitter'  # 100% of Podlog URL-bearing posts to date are Twitter

# (path, count) — sums to 500.
SPLIT = [
    ('/',                                 200),  # 40% homepage (marketing, null-topic, broad)
    ('/how-it-works',                      75),  # 15% NotebookLM comparison angle
    ('/explore',                           75),  # 15% "podcast recs", "rss for releases"
    ('/features',                          25),  # 5% AI voice quality
    ('/pricing',                           15),  # 3% pricing-aware intent
    ('/listen/kubernetes-96a14974',        20),
    ('/listen/linux-kernel-654e5f31',      10),
    ('/listen/rust-ffe93d3a',              10),
    ('/listen/postgresql-9847372b',        10),
    ('/listen/vs-code-6ffbd97f',           10),
    ('/listen/pytorch-2496be96',           10),
    ('/listen/go-e282e2e6',                10),
    ('/listen/python-f98f669e',            10),
    ('/listen/typescript-044e736b',         5),
    ('/listen/react-daily-101f1abb',        5),
    ('/listen/next-js-36fde2ae',            5),
    ('/listen/django-b4aa223e',             3),
    ('/listen/tailwindcss-ce7e5038',        2),
]
TOTAL = sum(c for _, c in SPLIT)
assert TOTAL == 500, TOTAL


def _build_target(path: str, code: str) -> str:
    base = ORIGIN.rstrip('/') + path
    params = {
        'utm_source': 's4l',
        'utm_medium': 'post',
        'utm_campaign': CAMPAIGN,
        'utm_term': PLATFORM,
        'utm_content': code,
    }
    sep = '&' if '?' in base else '?'
    return f"{base}{sep}{urlencode(params)}"


def _gen_unique_codes(n: int, existing: set[str]) -> list[str]:
    out: set[str] = set()
    while len(out) < n:
        c = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        if c not in existing and c not in out:
            out.add(c)
    return list(out)


def main():
    today = date.today().isoformat()
    session_tag = f"pool:kent-{today}:podlog:subpage-500:twitter"
    csv_path = os.path.join(REPO_DIR, 'scripts', f'podlog-shortlinks-{today}.csv')

    conn = dbmod.get_conn()
    raw = conn._conn
    try:
        cur = conn.execute("SELECT code FROM post_links")
        existing = {dict(r)['code'] for r in cur.fetchall()}
        print(f"existing post_links rows: {len(existing)}")

        rows_for_csv = []
        rows_for_db = []
        for path, count in SPLIT:
            codes = _gen_unique_codes(count, existing)
            existing.update(codes)
            for code in codes:
                target = _build_target(path, code)
                rows_for_db.append((
                    code, PLATFORM, 'Podlog', target,
                    'website', 'Podlog', session_tag,
                ))
                rows_for_csv.append((code, target))
            print(f"  + {path:<40} count={count}")

        c = raw.cursor()
        execute_values(
            c,
            "INSERT INTO post_links "
            "  (code, platform, project_name, target_url, kind, project_at_mint, minted_session) "
            "VALUES %s ON CONFLICT (code) DO NOTHING",
            rows_for_db, page_size=500,
        )
        raw.commit()
        c.close()
        print(f"\nminted {len(rows_for_db)} rows tagged {session_tag}")

        with open(csv_path, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['code', 'target_url'])
            w.writerows(rows_for_csv)
        print(f"wrote CSV: {csv_path}")
        print(f"size: {os.path.getsize(csv_path)} bytes, rows: {len(rows_for_csv)}")
    finally:
        conn.close()


if __name__ == '__main__':
    main()
