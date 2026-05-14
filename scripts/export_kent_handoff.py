#!/usr/bin/env python3
"""Export Kent's handoff CSVs from the current Kent pool.

For each site (Runner, Agora, Podlog), writes:
  - <slug>-shortlinks.csv  — code, destination_url, platform, path
  - all-shortlinks.csv     — combined audit file

The CSVs are reference-only. The route handler resolves codes by calling
https://app.s4l.ai/api/short-links/<code> server-side; Kent does NOT need to
host the CSV anywhere. The CSV is so Kent can spot-check minted destinations.
"""
from __future__ import annotations
import csv
import os
import sys
from urllib.parse import urlparse

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_DIR, 'scripts'))

import db as dbmod  # noqa: E402

PROJECTS = [('Runner', 'runner'), ('Agora', 'agora'), ('Podlog', 'podlog')]
OUT_DIR = os.path.join(REPO_DIR, 'onboarding', 'kent-handoff')


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    conn = dbmod.get_conn()
    all_rows = []
    try:
        for project, slug in PROJECTS:
            cur = conn.execute(
                "SELECT code, platform, target_url FROM post_links "
                "WHERE minted_session LIKE %s AND project_name = %s "
                "  AND post_id IS NULL AND reply_id IS NULL "
                "ORDER BY platform, code",
                ('pool:kent-%', project),
            )
            rows = [dict(r) for r in cur.fetchall()]
            out_csv = os.path.join(OUT_DIR, f"{slug}-shortlinks.csv")
            with open(out_csv, 'w', newline='') as f:
                w = csv.writer(f)
                w.writerow(['short_path', 'destination_url', 'platform', 'path'])
                for r in rows:
                    path = urlparse(r['target_url']).path or '/'
                    w.writerow([f"/r/{r['code']}", r['target_url'], r['platform'], path])
                    all_rows.append([f"/r/{r['code']}", r['target_url'], r['platform'], path, project])
            print(f"  {project:<8} -> {out_csv} ({len(rows)} rows)")

        all_csv = os.path.join(OUT_DIR, 'all-shortlinks.csv')
        with open(all_csv, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['short_path', 'destination_url', 'platform', 'path', 'project'])
            w.writerows(all_rows)
        print(f"  ALL       -> {all_csv} ({len(all_rows)} rows)")
    finally:
        conn.close()


if __name__ == '__main__':
    main()
