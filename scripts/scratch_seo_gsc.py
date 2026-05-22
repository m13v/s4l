#!/usr/bin/env python3
"""Query GSC for impressions/clicks on Twitter-generated SEO pages.

For each project with a gsc_property, fetch search analytics filtered by the
specific page URLs we generated, last 28 days.
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod

dbmod.load_env()
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta

CONFIG = os.path.expanduser("~/social-autoposter/config.json")
SA_PATH = os.path.expanduser("~/social-autoposter/seo/credentials/seo-autopilot-sa.json")

with open(CONFIG) as f:
    cfg = json.load(f)
projects = {p["name"]: p for p in cfg.get("projects", [])}

# Load page list
conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("""
SELECT product, page_url
FROM seo_keywords
WHERE source='twitter' AND page_url IS NOT NULL AND page_url <> ''
""")
pages_by_product = {}
for r in cur.fetchall():
    pages_by_product.setdefault(r["product"], set()).add(r["page_url"].rstrip("/"))
cur.close(); conn.close()

# Set up GSC
from google.oauth2 import service_account
from googleapiclient.discovery import build
creds = service_account.Credentials.from_service_account_file(
    SA_PATH,
    scopes=["https://www.googleapis.com/auth/webmasters.readonly"],
)
svc = build("searchconsole", "v1", credentials=creds)

end = datetime.utcnow().strftime("%Y-%m-%d")
start = (datetime.utcnow() - timedelta(days=28)).strftime("%Y-%m-%d")

results_by_product = {}
errors = []

for product, urls in sorted(pages_by_product.items()):
    proj = projects.get(product) or {}
    lp = proj.get("landing_pages") or {}
    gsc_prop = lp.get("gsc_property")
    if not gsc_prop:
        errors.append(f"{product}: no gsc_property in config")
        continue
    # Build a filter group with up to ~50 URL equals filters (GSC supports
    # filters list, but with size limits — try one shot).
    page_list = sorted(urls)
    # Use dimension=page, filter=URL equals each one would be too many; instead
    # fetch all page-dimension rows for the property and intersect.
    try:
        body = {
            "startDate": start,
            "endDate": end,
            "dimensions": ["page"],
            "rowLimit": 25000,
        }
        resp = svc.searchanalytics().query(siteUrl=gsc_prop, body=body).execute()
        rows = resp.get("rows", []) or []
    except Exception as e:
        errors.append(f"{product} ({gsc_prop}): {e}")
        continue
    url_set = set(page_list)
    # Allow trailing slash differences
    url_set |= {u + "/" for u in page_list}
    matched = []
    for row in rows:
        page = (row.get("keys") or [""])[0].rstrip("/")
        if page in url_set or (page + "/") in url_set:
            matched.append({
                "page": page,
                "clicks": int(row.get("clicks", 0)),
                "impressions": int(row.get("impressions", 0)),
                "ctr": float(row.get("ctr", 0.0)),
                "position": float(row.get("position", 0.0)),
            })
    results_by_product[product] = {
        "gsc_property": gsc_prop,
        "total_pages_we_have": len(urls),
        "matched_in_gsc": len(matched),
        "rows": matched,
    }

# Print summary
print(f"=== GSC last 28d ({start} to {end}) ===\n")
total_imp = 0; total_clk = 0; total_pages = 0
for product, d in sorted(results_by_product.items(), key=lambda kv: -sum(r["impressions"] for r in kv[1]["rows"])):
    imps = sum(r["impressions"] for r in d["rows"])
    clks = sum(r["clicks"] for r in d["rows"])
    matched = d["matched_in_gsc"]
    total_have = d["total_pages_we_have"]
    print(f"  {product:18s}  pages={matched}/{total_have}  impressions={imps:6d}  clicks={clks:4d}  gsc={d['gsc_property']}")
    total_imp += imps; total_clk += clks; total_pages += matched

print(f"\nTOTAL: impressions={total_imp}  clicks={total_clk}  matched_pages={total_pages}")
if errors:
    print(f"\nerrors: {errors}")

# Top pages overall
all_rows = []
for product, d in results_by_product.items():
    for r in d["rows"]:
        all_rows.append({**r, "product": product})
all_rows.sort(key=lambda r: -r["impressions"])
print("\n=== TOP 10 PAGES BY 28D IMPRESSIONS ===")
for r in all_rows[:10]:
    print(f"  imp={r['impressions']:5d}  clk={r['clicks']:3d}  pos={r['position']:5.1f}  {r['page']}")

# Per-project top 3 (by impressions)
print("\n=== TOP 3 PROJECTS BY 28D GSC IMPRESSIONS ===")
proj_totals = []
for product, d in results_by_product.items():
    imp = sum(r["impressions"] for r in d["rows"])
    clk = sum(r["clicks"] for r in d["rows"])
    proj_totals.append((product, imp, clk, d["matched_in_gsc"]))
proj_totals.sort(key=lambda x: -x[1])
for p, imp, clk, n in proj_totals[:3]:
    print(f"  {p:18s}  impressions={imp}  clicks={clk}  pages={n}")

with open("/tmp/seo_twitter_gsc.json","w") as f:
    json.dump({"results": results_by_product, "errors": errors, "start": start, "end": end}, f, indent=2)
print("\nwrote /tmp/seo_twitter_gsc.json")
