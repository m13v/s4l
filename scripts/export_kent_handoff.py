#!/usr/bin/env python3
"""Generate the complete Kent handoff package from the pre-minted post_links
pool: per-site CSV, per-site JSON code map, and drop-in redirect snippets for
Next.js / Vercel / Cloudflare Workers / Nginx.

Run after `mint_external_pool.py` has finished seeding the pool. Output lands
in onboarding/kent-handoff/.
"""

from __future__ import annotations
import csv
import json
import os
import sys
from urllib.parse import urlsplit

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_DIR, 'scripts'))
import db as dbmod  # noqa: E402

OUT_DIR = os.path.join(REPO_DIR, 'onboarding', 'kent-handoff')

KENT_PROJECTS = ('Runner', 'Agora', 'Podlog')


def _slug(name: str) -> str:
    return name.lower()


def export_all():
    os.makedirs(OUT_DIR, exist_ok=True)
    conn = dbmod.get_conn()
    written = {}
    combined_csv_rows = [['short_path', 'destination_url', 'project', 'platform']]
    try:
        for project in KENT_PROJECTS:
            cur = conn.execute(
                "SELECT code, platform, target_url FROM post_links "
                "WHERE minted_session LIKE 'pool:%%' AND project_name = %s "
                "ORDER BY platform, code",
                (project,),
            )
            rows = [dict(r) for r in cur.fetchall()]
            if not rows:
                print(f"  [skip] no pool rows for {project}")
                continue
            slug = _slug(project)
            # Per-site CSV
            csv_path = os.path.join(OUT_DIR, f"{slug}-shortlinks.csv")
            with open(csv_path, 'w', newline='') as f:
                w = csv.writer(f)
                w.writerow(['short_path', 'destination_url', 'platform'])
                for r in rows:
                    short_path = f"/r/{r['code']}"
                    w.writerow([short_path, r['target_url'], r['platform']])
                    combined_csv_rows.append([short_path, r['target_url'], project, r['platform']])
            # Per-site JSON code -> destination map (drop-in for Next.js handler)
            json_path = os.path.join(OUT_DIR, f"{slug}-shortlinks.json")
            code_map = {r['code']: r['target_url'] for r in rows}
            with open(json_path, 'w') as f:
                json.dump(code_map, f, indent=2)
            # Vercel-style redirects array (capped at 1024 — same cap Vercel enforces)
            vercel_path = os.path.join(OUT_DIR, f"{slug}-vercel-redirects.json")
            redirects = [
                {
                    'source': f"/r/{r['code']}",
                    'destination': r['target_url'],
                    'permanent': False,
                }
                for r in rows[:1024]
            ]
            with open(vercel_path, 'w') as f:
                json.dump({'redirects': redirects}, f, indent=2)
            written[project] = {
                'csv': csv_path,
                'json': json_path,
                'vercel': vercel_path,
                'rows': len(rows),
                'vercel_truncated': len(rows) > 1024,
            }
        # Combined CSV for audit
        combined_path = os.path.join(OUT_DIR, 'all-shortlinks.csv')
        with open(combined_path, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerows(combined_csv_rows)
        written['_combined'] = {'csv': combined_path, 'rows': len(combined_csv_rows) - 1}

        # Drop-in Next.js route handler (one file Kent can copy per site)
        nextjs_handler = '''// src/app/r/[code]/route.ts
// Drop this into any Next.js (app router) project at the path above.
// It looks up the inbound code in shortlinks.json (sibling file) and 302s
// to the destination. Unknown codes fall through to the homepage.

import { NextRequest, NextResponse } from 'next/server';
import codeMap from './shortlinks.json';

export const dynamic = 'force-dynamic';

export function GET(
  req: NextRequest,
  { params }: { params: { code: string } },
) {
  const code = params.code?.toLowerCase();
  const dest = (codeMap as Record<string, string>)[code];
  if (!dest) {
    // Unknown code: fall back to homepage with a marker UTM so we can
    // detect this in analytics if it happens often.
    const fallback = new URL('/?utm_source=unknown-shortlink', req.url);
    return NextResponse.redirect(fallback, 302);
  }
  return NextResponse.redirect(dest, 302);
}
'''
        with open(os.path.join(OUT_DIR, 'nextjs-route-handler.ts'), 'w') as f:
            f.write(nextjs_handler)

        # Cloudflare Worker (single-file)
        cf_worker = '''// Cloudflare Worker drop-in for /r/<code> short links.
// Bind shortlinks.json as a Workers KV or embed it inline as below.
// Deploy: wrangler deploy. Route: <yourdomain>.com/r/*

const codeMap = /* PASTE-CONTENTS-OF shortlinks.json HERE */ {};

export default {
  async fetch(req) {
    const url = new URL(req.url);
    const m = url.pathname.match(/^\\/r\\/([a-z0-9]+)\\/?$/i);
    if (!m) return new Response("Not found", { status: 404 });
    const code = m[1].toLowerCase();
    const dest = codeMap[code];
    if (!dest) {
      const fallback = new URL("/?utm_source=unknown-shortlink", url.origin);
      return Response.redirect(fallback.toString(), 302);
    }
    return Response.redirect(dest, 302);
  },
};
'''
        with open(os.path.join(OUT_DIR, 'cloudflare-worker.js'), 'w') as f:
            f.write(cf_worker)

        # Nginx snippet (using map directive)
        nginx_snippet = '''# Drop into your nginx http {} block.
# Each line: hash key (the code) → redirect target.
# Place an `include shortlinks-map.conf;` after the map directive.

map $shortlink_code $shortlink_dest {
    default "/?utm_source=unknown-shortlink";
    # Generated entries (one per code):
    include shortlinks-map.conf;
}

server {
    server_name your-domain.com;

    # Capture the code from /r/<code> and look it up in the map.
    location ~ ^/r/([a-z0-9]+)/?$ {
        set $shortlink_code $1;
        return 302 $shortlink_dest;
    }
}
'''
        with open(os.path.join(OUT_DIR, 'nginx-snippet.conf'), 'w') as f:
            f.write(nginx_snippet)

        # Build per-site nginx map files
        for project in KENT_PROJECTS:
            cur = conn.execute(
                "SELECT code, target_url FROM post_links "
                "WHERE minted_session LIKE 'pool:%%' AND project_name = %s "
                "ORDER BY code",
                (project,),
            )
            rows = [dict(r) for r in cur.fetchall()]
            if not rows:
                continue
            slug = _slug(project)
            map_path = os.path.join(OUT_DIR, f"{slug}-shortlinks-map.conf")
            with open(map_path, 'w') as f:
                for r in rows:
                    # Escape any quotes in destination just to be safe.
                    dest = r['target_url'].replace('"', '\\"')
                    f.write(f'    "{r["code"]}" "{dest}";\n')
        return written
    finally:
        conn.close()


if __name__ == '__main__':
    res = export_all()
    print(json.dumps(res, indent=2, default=str))
