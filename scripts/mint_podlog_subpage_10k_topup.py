#!/usr/bin/env python3
"""Top-up Podlog subpage pool from 500 -> 10,000 codes and export full CSV.

The 500-code batch shipped earlier (tag `subpage-500`) was undersized vs the
original 10k pool size Kent expects. This mints the additional 9,500 codes
with the SAME proportional subpage split, then exports the union of both
batches (10,000 rows) as one CSV.
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
PLATFORM = 'twitter'

# Same proportional split as the 500-code batch, scaled to 9,500.
# 500-code baseline at right; 10,000 target at far right. Diff = mint here.
SPLIT_DIFF = [
    ('/',                                 3_800),  # 200  ->  4,000
    ('/how-it-works',                     1_425),  # 75   ->  1,500
    ('/explore',                          1_425),  # 75   ->  1,500
    ('/features',                           475),  # 25   ->    500
    ('/pricing',                            285),  # 15   ->    300
    ('/listen/kubernetes-96a14974',         380),  # 20   ->    400
    ('/listen/linux-kernel-654e5f31',       190),  # 10   ->    200
    ('/listen/rust-ffe93d3a',               190),  # 10   ->    200
    ('/listen/postgresql-9847372b',         190),  # 10   ->    200
    ('/listen/vs-code-6ffbd97f',            190),  # 10   ->    200
    ('/listen/pytorch-2496be96',            190),  # 10   ->    200
    ('/listen/go-e282e2e6',                 190),  # 10   ->    200
    ('/listen/python-f98f669e',             190),  # 10   ->    200
    ('/listen/typescript-044e736b',          95),  # 5    ->    100
    ('/listen/react-daily-101f1abb',         95),  # 5    ->    100
    ('/listen/next-js-36fde2ae',             95),  # 5    ->    100
    ('/listen/django-b4aa223e',              57),  # 3    ->     60
    ('/listen/tailwindcss-ce7e5038',         38),  # 2    ->     40
]
DIFF_TOTAL = sum(c for _, c in SPLIT_DIFF)
assert DIFF_TOTAL == 9_500, DIFF_TOTAL


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
    session_tag = f"pool:kent-{today}:podlog:subpage-9500:twitter"
    csv_path = os.path.join(REPO_DIR, 'scripts', f'podlog-shortlinks-{today}-10k.csv')

    conn = dbmod.get_conn()
    raw = conn._conn
    try:
        cur = conn.execute("SELECT code FROM post_links")
        existing = {dict(r)['code'] for r in cur.fetchall()}
        print(f"existing post_links rows: {len(existing)}")

        rows_for_db = []
        for path, count in SPLIT_DIFF:
            codes = _gen_unique_codes(count, existing)
            existing.update(codes)
            for code in codes:
                rows_for_db.append((
                    code, PLATFORM, 'Podlog',
                    _build_target(path, code),
                    'website', 'Podlog', session_tag,
                ))
            print(f"  + {path:<40} count={count}")

        c = raw.cursor()
        execute_values(
            c,
            "INSERT INTO post_links "
            "  (code, platform, project_name, target_url, kind, project_at_mint, minted_session) "
            "VALUES %s ON CONFLICT (code) DO NOTHING",
            rows_for_db, page_size=1000,
        )
        raw.commit()
        c.close()
        print(f"\nminted {len(rows_for_db)} rows tagged {session_tag}")

        # Export union of both batches (500 + 9,500 = 10,000)
        cur = conn.execute(
            "SELECT code, target_url FROM post_links "
            "WHERE project_name='Podlog' "
            "  AND minted_session IN (%s, %s) "
            "ORDER BY target_url, code",
            (
                f"pool:kent-{today}:podlog:subpage-500:twitter",
                session_tag,
            ),
        )
        rows = [(r['code'], r['target_url']) for r in cur.fetchall()]
        with open(csv_path, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['code', 'target_url'])
            w.writerows(rows)
        print(f"wrote CSV: {csv_path} ({len(rows)} rows, {os.path.getsize(csv_path)} bytes)")
    finally:
        conn.close()


if __name__ == '__main__':
    main()
