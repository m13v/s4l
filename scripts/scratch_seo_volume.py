#!/usr/bin/env python3
"""Scratch: analytics on Twitter-generated SEO pages."""
import os, sys, re, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod

dbmod.load_env()
import psycopg2
import psycopg2.extras

conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

def section(t):
    print(f"\n=== {t} ===")

# 0. Quick: confirm seo_keywords table
section("seo_keywords columns")
cur.execute("""
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name='seo_keywords'
ORDER BY ordinal_position
""")
for r in cur.fetchall():
    print(f"  {r['column_name']:28s} {r['data_type']}")

section("seo_keywords source distribution")
cur.execute("SELECT source, COUNT(*) AS n FROM seo_keywords GROUP BY source ORDER BY n DESC")
for r in cur.fetchall():
    print(f"  {str(r['source']):20s} {r['n']}")

section("seo_keywords source=twitter status distribution")
cur.execute("SELECT status, COUNT(*) AS n FROM seo_keywords WHERE source='twitter' GROUP BY status ORDER BY n DESC")
for r in cur.fetchall():
    print(f"  {str(r['status']):20s} {r['n']}")

# 1. Total Twitter posts with link_source='seo_page'
section("Twitter posts: link_source='seo_page'")
cur.execute("""
SELECT
  COUNT(*) FILTER (WHERE link_source='seo_page') AS seo_count,
  COUNT(*) AS total_twitter
FROM posts WHERE platform IN ('twitter','x')
""")
r = cur.fetchone()
print(f"  seo_page: {r['seo_count']}    total twitter posts: {r['total_twitter']}    pct: {100*r['seo_count']/max(1,r['total_twitter']):.1f}%")

# 1b. Cross-check: seo_keywords WHERE source='twitter' AND status='done'
section("seo_keywords WHERE source='twitter' AND status='done'")
cur.execute("SELECT COUNT(*) AS n FROM seo_keywords WHERE source='twitter' AND status='done'")
print(f"  done: {cur.fetchone()['n']}")

# 2. By project: top 5
section("Top projects by seo_page count (link_source='seo_page', twitter)")
cur.execute("""
SELECT project_name, COUNT(*) AS n
FROM posts
WHERE platform IN ('twitter','x') AND link_source='seo_page'
GROUP BY project_name ORDER BY n DESC LIMIT 10
""")
rows_by_proj = cur.fetchall()
for r in rows_by_proj:
    print(f"  {str(r['project_name']):20s} {r['n']}")

# 3. By week (last 8 weeks)
section("By week (last 8 weeks): twitter seo_page posts")
cur.execute("""
SELECT date_trunc('week', posted_at)::date AS week, COUNT(*) AS n
FROM posts
WHERE platform IN ('twitter','x') AND link_source='seo_page'
  AND posted_at >= NOW() - INTERVAL '8 weeks'
GROUP BY week ORDER BY week DESC
""")
for r in cur.fetchall():
    print(f"  {r['week']}  {r['n']}")

# 4. Distinct page URLs generated (from seo_keywords source='twitter' status='done')
section("Distinct page URLs (from seo_keywords source='twitter')")
cur.execute("""
SELECT product, page_url, completed_at
FROM seo_keywords
WHERE source='twitter' AND page_url IS NOT NULL AND page_url <> ''
ORDER BY completed_at DESC
LIMIT 5
""")
for r in cur.fetchall():
    print(f"  {str(r['product']):14s} {r['page_url']}  ({r['completed_at']})")

# 5. Per-project URL list for PostHog/GSC lookup
section("Counts of distinct pages by product")
cur.execute("""
SELECT product, COUNT(*) AS n, COUNT(*) FILTER (WHERE page_url IS NOT NULL AND page_url <> '') AS with_url
FROM seo_keywords
WHERE source='twitter'
GROUP BY product
ORDER BY n DESC
""")
for r in cur.fetchall():
    print(f"  {str(r['product']):14s} total={r['n']}  with_url={r['with_url']}")

# 6. Dump all twitter SEO page URLs by product to /tmp
section("Dumping all twitter SEO page URLs to /tmp/seo_twitter_pages.json")
cur.execute("""
SELECT product, page_url, completed_at, updated_at, status
FROM seo_keywords
WHERE source='twitter' AND page_url IS NOT NULL AND page_url <> ''
ORDER BY product, completed_at DESC
""")
all_pages = []
for r in cur.fetchall():
    all_pages.append({
        "product": r["product"],
        "page_url": r["page_url"],
        "completed_at": str(r["completed_at"]) if r["completed_at"] else None,
        "updated_at": str(r["updated_at"]) if r["updated_at"] else None,
        "status": r["status"],
    })
with open("/tmp/seo_twitter_pages.json","w") as f:
    json.dump(all_pages, f, indent=2)
print(f"  wrote {len(all_pages)} pages")

# 7. By month last 6 months
section("By month (last 6 months): twitter seo_page posts")
cur.execute("""
SELECT date_trunc('month', posted_at)::date AS month, COUNT(*) AS n
FROM posts
WHERE platform IN ('twitter','x') AND link_source='seo_page'
  AND posted_at >= NOW() - INTERVAL '6 months'
GROUP BY month ORDER BY month DESC
""")
for r in cur.fetchall():
    print(f"  {r['month']}  {r['n']}")

cur.close()
conn.close()
